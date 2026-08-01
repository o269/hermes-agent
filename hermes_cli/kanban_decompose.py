"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the
auto-decompose path in the gateway dispatcher loop. Reads the user's
profile roster (with descriptions) and asks the auxiliary LLM to
return a task graph in JSON. Then atomically creates the children,
links them under the root, and flips the root ``triage -> todo``.

The root task stays alive and becomes the parent of every leaf child,
so when the whole graph completes the root wakes back up — its
assignee (the orchestrator profile) gets a chance to judge completion
and add more tasks if the work isn't done yet.

Design notes
------------

* Mirrors the shape of ``hermes_cli/kanban_specify.py``: lazy aux
  client import inside the function, lenient response parse, never
  raises on expected failure modes.

* The system prompt sees the *configured* profile roster — names plus
  descriptions plus the default fallback. Profiles without a
  description are still listed (with a note) so the decomposer can
  match on name as a fallback, but the user has an obvious incentive
  to describe them.

* ``fanout=false`` collapses to the same effect as ``kanban specify``:
  we tighten the body and flip ``triage -> todo`` as a single task,
  no children created. This makes ``decompose`` a strict superset of
  ``specify`` from the user's perspective.

* If the LLM picks an assignee that doesn't exist as a profile, we
  rewrite it to the configured ``default_assignee`` (or the default
  profile if unset). A child task NEVER ends up with ``assignee=None``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import profiles as profiles_mod

logger = logging.getLogger(__name__)

_MIN_FANOUT_TASKS = 2
_MAX_FANOUT_TASKS = 6


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (name, capability description, and any
    configured provider/model defaults)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism. If two tasks can be done independently, give
    them no parents so the dispatcher fans them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Provider/model values are profile defaults. Use them only as routing
    context or a tie-breaker; never invent an unlisted assignee or override
    a profile's defaults in a child task.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None
    skipped: bool = False
    root_status: Optional[str] = None
    dependency_edges: int = 0
    root_dependencies: int = 0
    leaf_count: int = 0


@dataclass
class OrchestrationProfileResolution:
    """One canonical live-profile resolution shared by every surface."""

    roster: list[dict]
    valid_names: set[str]
    active_profile: Optional[str]
    orchestrator_profile: Optional[str]
    default_assignee: Optional[str]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val


def _response_content(response: object) -> str:
    """Return aux JSON text without normalizing or re-serializing its values."""
    if isinstance(response, str):
        # A few compatible adapters and test doubles return the content
        # directly. Preserve the exact wrapper text for the JSON parser.
        return response
    try:
        content = response.choices[0].message.content  # type: ignore[attr-defined]
    except (AttributeError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else ""


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "decomposer"
    )


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _profile_is_resolved(name: str, valid_names: set[str]) -> bool:
    """Return whether ``name`` is proven routable on a known host."""
    candidate = (name or "").strip()
    return bool(candidate and candidate in valid_names)


def _fleet_named_lanes_only() -> bool:
    """Return whether the resolved DB uses the fleet's named-lane contract."""
    try:
        from hermes_cli.boardd_shim import routes_to_fleet

        routes_to_fleet_board, _requested, _fleet = routes_to_fleet()
        if routes_to_fleet_board:
            return True
    except Exception:
        pass
    try:
        return kb.get_current_board() == "fleet"
    except Exception:
        return False


def _resolve_orchestrator_profile(
    cfg: dict,
    *,
    existing_assignee: Optional[str],
    valid_names: set[str],
    active_profile: Optional[str],
) -> Optional[str]:
    """Resolve root custody without inventing the literal ``default`` lane.

    An existing task assignee wins unconditionally because preserving it is not
    a new routing write. Otherwise only configured/active profiles proven by
    the current roster are candidates. ``None`` means resolution failed and
    the caller must leave the task in triage with a visible error.
    """
    existing = (existing_assignee or "").strip()
    if existing:
        return existing

    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("orchestrator_profile") or "").strip()
    if explicit and _profile_is_resolved(explicit, valid_names):
        return explicit

    active = (active_profile or "").strip()
    if active and _profile_is_resolved(active, valid_names):
        return active
    return None


def _resolve_default_assignee(
    cfg: dict,
    *,
    orchestrator: Optional[str],
    valid_names: set[str],
    active_profile: Optional[str],
) -> Optional[str]:
    """Resolve the child fallback to a real profile or return ``None``."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("default_assignee") or "").strip()
    if explicit and _profile_is_resolved(explicit, valid_names):
        return explicit

    if orchestrator and _profile_is_resolved(orchestrator, valid_names):
        return orchestrator

    active = (active_profile or "").strip()
    if active and _profile_is_resolved(active, valid_names):
        return active
    return None


def _build_roster() -> tuple[list[dict], set[str]]:
    """Return (roster_for_prompt, valid_assignee_names).

    Each roster entry includes the profile's capability description and its
    configured provider/model defaults. The defaults are routing context, not
    hard constraints. The
    valid-set is used after the LLM responds to rewrite invalid
    assignees to the default fallback.
    """
    roster: list[dict] = []
    valid: set[str] = set()
    fleet_named_lanes_only = _fleet_named_lanes_only()
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list local profiles: %s", exc)
        all_profiles = []
    for p in all_profiles:
        # ``default`` is Hermes' implicit root profile, not a named fleet lane.
        # Treating it as routable on the shared fleet board recreated the exact
        # custody-loss bug this validation is meant to prevent.
        if fleet_named_lanes_only and p.name == "default":
            continue
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
            "provider": p.provider,
            "model": p.model,
        })
        valid.add(p.name)
    return roster, valid


def resolve_orchestration_profiles(
    cfg: dict,
    *,
    existing_assignee: Optional[str] = None,
) -> OrchestrationProfileResolution:
    """Resolve routes from the current configured profile set.

    This is the sole resolver used by the dispatcher/decomposer and dashboard.
    Historical task/run rows are intentionally absent: a retired profile cannot
    remain routing authority merely because it completed work in the past.
    """
    roster, valid_names = _build_roster()
    try:
        active_profile = (profiles_mod.get_active_profile_name() or "").strip() or None
    except Exception:
        active_profile = None
    orchestrator = _resolve_orchestrator_profile(
        cfg,
        existing_assignee=existing_assignee,
        valid_names=valid_names,
        active_profile=active_profile,
    )
    default_assignee = _resolve_default_assignee(
        cfg,
        orchestrator=orchestrator,
        valid_names=valid_names,
        active_profile=active_profile,
    )
    return OrchestrationProfileResolution(
        roster=roster,
        valid_names=valid_names,
        active_profile=(
            active_profile
            if _profile_is_resolved(active_profile or "", valid_names)
            else None
        ),
        orchestrator_profile=orchestrator,
        default_assignee=default_assignee,
    )


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    lines = []
    for entry in roster:
        tag = "" if entry["has_description"] else " ⚠ undescribed"
        defaults = []
        if entry.get("provider"):
            defaults.append(f"provider={entry['provider']}")
        if entry.get("model"):
            defaults.append(f"model={entry['model']}")
        default_tag = (
            f" [profile defaults: {', '.join(defaults)}]" if defaults else ""
        )
        lines.append(
            f"  - {entry['name']}{tag}: {entry['description']}{default_tag}"
        )
    return "\n".join(lines)


def _normalize_assignee_choice(
    assignee: object,
    *,
    default_assignee: Optional[str],
    valid_names: set[str],
) -> Optional[str]:
    """Return a routed profile, or ``None`` when no safe fallback exists."""
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    if chosen not in valid_names:
        return default_assignee
    return chosen


def _skip_outcome(
    conn,
    task_id: str,
    fallback: str,
    *,
    expected_assignee: Optional[str],
) -> DecomposeOutcome:
    """Describe a compare-and-set miss without misreporting it as an LLM error."""
    current = kb.get_task(conn, task_id)
    if current is None:
        reason = "task disappeared before the decomposition write"
        status = None
    elif current.status != "triage":
        reason = f"task status changed to {current.status!r} before the decomposition write"
        status = current.status
    elif current.assignee != expected_assignee:
        reason = (
            "task assignee changed from "
            f"{expected_assignee!r} to {current.assignee!r} before the decomposition write"
        )
        status = current.status
    else:
        hold_reason = kb.decomposition_hold_reason(conn, task_id)
        reason = (
            f"control-plane hold appeared before the decomposition write: {hold_reason}"
            if hold_reason
            else fallback
        )
        status = current.status
    return DecomposeOutcome(
        task_id,
        False,
        f"skipped: {reason}; task left unchanged",
        skipped=True,
        root_status=status,
    )


def _dependency_diagnostics(children: list[dict]) -> tuple[int, int]:
    """Validate the child DAG and return ``(internal_edges, leaf_count)``."""
    state = [0] * len(children)
    stack: list[int] = []

    def visit(index: int) -> None:
        if state[index] == 2:
            return
        if state[index] == 1:
            cycle_start = stack.index(index)
            cycle = stack[cycle_start:] + [index]
            rendered = " -> ".join(str(item) for item in cycle)
            raise ValueError(f"cyclic dependency among task indexes: {rendered}")
        state[index] = 1
        stack.append(index)
        for parent in children[index]["parents"]:
            visit(parent)
        stack.pop()
        state[index] = 2

    for child_index in range(len(children)):
        visit(child_index)

    parent_indexes = {
        parent
        for child in children
        for parent in child["parents"]
    }
    internal_edges = sum(len(child["parents"]) for child in children)
    leaf_count = len(children) - len(parent_indexes)
    return internal_edges, leaf_count


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks.

    Returns an outcome describing what happened. Never raises for
    expected failure modes (task not in triage, no aux client
    configured, API error, malformed response, decomposer returned
    fanout=true with empty task list) — those surface via ``ok=False``.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        hold_reason = (
            kb.decomposition_hold_reason(conn, task_id) if task is not None else None
        )
    if task is None:
        return DecomposeOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return DecomposeOutcome(
            task_id,
            False,
            f"skipped: task is not in triage (status={task.status!r}); task left unchanged",
            skipped=True,
            root_status=task.status,
        )
    if hold_reason is not None:
        return DecomposeOutcome(
            task_id,
            False,
            f"skipped: control-plane hold: {hold_reason}; task left unchanged",
            skipped=True,
            root_status=task.status,
        )

    cfg = _load_config()
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    auto_promote = bool(kanban_cfg.get("auto_promote_children", True))
    resolution = resolve_orchestration_profiles(
        cfg,
        existing_assignee=task.assignee,
    )
    roster = resolution.roster
    valid_names = resolution.valid_names
    orchestrator = resolution.orchestrator_profile
    if task.assignee is None and orchestrator is None:
        return DecomposeOutcome(
            task_id,
            False,
            "no resolvable orchestrator profile; task left in triage",
        )
    default_assignee = resolution.default_assignee
    if default_assignee is None:
        return DecomposeOutcome(
            task_id,
            False,
            "no resolvable default assignee profile; task left in triage",
        )

    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception as exc:
        logger.debug("decompose: auxiliary client import failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
        roster=_format_roster(roster),
        default_assignee=default_assignee,
    )

    try:
        # Route through call_llm so auxiliary.kanban_decomposer.* config
        # (provider/model/base_url, extra_body, reasoning_effort, retries)
        # all apply — the previous direct client.chat.completions.create()
        # path dropped auxiliary.<task>.extra_body entirely (#35566).
        resp = call_llm(
            task="kanban_decomposer",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=4000,
            timeout=timeout or 180,
        )
    except Exception as exc:
        logger.info(
            "decompose: API call failed for %s (%s)", task_id, exc,
        )
        return DecomposeOutcome(task_id, False, f"LLM error: {type(exc).__name__}")

    raw = _response_content(resp)

    parsed = _extract_json_blob(raw)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    fanout = bool(parsed.get("fanout"))
    audit_author = author or _profile_author()

    if not fanout:
        # Fall back to single-task spec promotion (same effect as specify).
        new_title = parsed.get("title")
        new_body = parsed.get("body")
        title_val = new_title.strip() if isinstance(new_title, str) and new_title.strip() else None
        body_val = new_body if isinstance(new_body, str) and new_body.strip() else None
        assignee_val = None
        if not task.assignee:
            assignee_val = _normalize_assignee_choice(
                parsed.get("assignee"),
                default_assignee=default_assignee,
                valid_names=valid_names,
            )
        if title_val is None and body_val is None:
            return DecomposeOutcome(
                task_id, False, "decomposer returned fanout=false with no title/body",
            )
        with kb.connect_closing() as conn:
            ok = kb.specify_triage_task(
                conn,
                task_id,
                title=title_val,
                body=body_val,
                assignee=assignee_val,
                author=audit_author,
                preserve_status=bool(task.assignee),
                valid_assignees=valid_names,
                decomposition_guard=True,
                expected_assignee=task.assignee,
            )
            if not ok:
                return _skip_outcome(
                    conn,
                    task_id,
                    "task changed concurrently before single-task promotion",
                    expected_assignee=task.assignee,
                )
            promoted = kb.get_task(conn, task_id)
        return DecomposeOutcome(
            task_id, True, "single task (no fanout)",
            fanout=False,
            new_title=title_val,
            root_status=promoted.status if promoted else None,
        )

    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(
            task_id, False, "decomposer returned fanout=true with empty tasks list",
        )
    if not _MIN_FANOUT_TASKS <= len(raw_tasks) <= _MAX_FANOUT_TASKS:
        return DecomposeOutcome(
            task_id,
            False,
            "decomposer returned fanout=true with "
            f"{len(raw_tasks)} tasks; expected {_MIN_FANOUT_TASKS}-{_MAX_FANOUT_TASKS}",
        )

    # Rewrite invalid assignees to the default fallback. Never leave a
    # task with assignee=None — the user explicitly does not want that.
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}] is not an object",
            )
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}].title is missing or empty",
            )
        body = entry.get("body")
        if not isinstance(body, str):
            body = ""
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee,
            default_assignee=default_assignee,
            valid_names=valid_names,
        )
        if (
            isinstance(assignee, str)
            and assignee.strip()
            and assignee.strip() not in valid_names
        ):
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, default_assignee,
            )
        parents = entry.get("parents", [])
        if parents is None:
            parents = []
        if not isinstance(parents, list):
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}].parents must be a list",
            )
        clean_parents: list[int] = []
        for parent_position, parent_index in enumerate(parents):
            if isinstance(parent_index, bool) or not isinstance(parent_index, int):
                return DecomposeOutcome(
                    task_id,
                    False,
                    f"tasks[{idx}].parents[{parent_position}] must be an integer index",
                )
            if parent_index < 0 or parent_index >= len(raw_tasks):
                return DecomposeOutcome(
                    task_id,
                    False,
                    f"tasks[{idx}].parents[{parent_position}]={parent_index} "
                    f"is outside 0..{len(raw_tasks) - 1}",
                )
            if parent_index == idx:
                return DecomposeOutcome(
                    task_id, False, f"tasks[{idx}] cannot depend on itself",
                )
            if parent_index in clean_parents:
                return DecomposeOutcome(
                    task_id,
                    False,
                    f"tasks[{idx}].parents contains duplicate index {parent_index}",
                )
            clean_parents.append(parent_index)
        children.append({
            "title": title.strip()[:200],
            "body": body,
            "assignee": chosen,
            "parents": clean_parents,
        })

    try:
        dependency_edges, leaf_count = _dependency_diagnostics(children)
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, str(exc))

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee=orchestrator,
                children=children,
                valid_assignees=valid_names,
                expected_root_assignee=task.assignee,
                author=audit_author,
                auto_promote=auto_promote,
            )
            if child_ids is None:
                return _skip_outcome(
                    conn,
                    task_id,
                    "task changed concurrently before graph creation",
                    expected_assignee=task.assignee,
                )
            decomposed_root = kb.get_task(conn, task_id)
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")

    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children",
        fanout=True,
        child_ids=child_ids,
        root_status=decomposed_root.status if decomposed_root else None,
        dependency_edges=dependency_edges,
        root_dependencies=len(child_ids),
        leaf_count=leaf_count,
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return only triage cards eligible for automatic decomposition."""
    with kb.connect_closing() as conn:
        return kb.list_decomposition_eligible_triage_ids(
            conn,
            tenant=tenant,
            limit=1000,
        )
