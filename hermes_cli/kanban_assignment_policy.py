"""Pure assignment/state policy for non-spawnable Kanban authority lanes.

The Kanban kernel, broker-backed custom bridges, and external schedulers such as
BoardQB all need the same vocabulary.  Keep classification pure in this module;
``kanban_db`` supplies board/config facts (configured authority profiles and
whether a task still has an open parent) before enforcing the result.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Optional

# Executor-shaped cards may wait on a non-spawnable authority lane, but only in
# states that cannot dispatch.  Terminal rows are also safe and must remain
# editable/archivable after policy activation.
EXECUTOR_AUTHORITY_PARKED_STATUSES = frozenset({"todo", "scheduled", "blocked"})
TERMINAL_STATUSES = frozenset({"done", "archived", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({"ready", "review", "running"})

# Exact incident rows are durable authority controls even if a raw writer tries
# to erase every semantic marker in the same UPDATE that changes custody/state.
# External schedulers should pass ``task_id`` to the classifier so they share
# this invariant with the kernel triggers.
NONSPAWNABLE_TASK_IDS = frozenset(
    {
        "t_69961c40",
        "t_1ec8fc59",
        "t_5f21b5af",
        "t_db63bc2b",
        "t_f38d52ac",
        "t_2317c762",
        "t_b2eaff49",
        "t_5f2431f2",
        "t_75e912b7",
        "t_e34b1a38",
    }
)

# Strong, explicit contracts.  These intentionally inspect both title and body:
# live cards often carry a terse title and put the actual non-spawnable custody
# ruling in the body.
_NONSPAWNABLE_CONTRACT_RE = re.compile(
    r"(?:"
    r"\bNON[- ]?SPAWNABLE\b|"
    r"\bDO NOT (?:AUTO[- ]?)?DISPATCH\b|"
    r"\bNEVER DISPATCH\b|"
    r"\bOPERATOR[- ](?:HOLD|GATE|ACTION|COMMITTED)\b|"
    r"\bOWNER[- ]DECISION\b|"
    r"\bDECISION REQUIRED\b|"
    r"\b(?:RECURRING|PERSISTENT)\s+(?:STEWARD|TRACKER|WATCHDOG)\b|"
    r"\b(?:FABLE|OPERATOR|OWNER) APPROVAL REQUIRED\b|"
    r"\bAPPROVAL REQUIRED (?:BY|FROM) (?:FABLE|OPERATOR|OWNER)\b|"
    r"\b(?:FABLE(?:/OPERATOR)?|OPERATOR)[- ]ONLY\b|"
    r"\bSOLE[- ](?:LANDER|APPLICATOR|CLOSER)\b|"
    r"\bFABLE (?:IS )?SOLE (?:LANDER|APPLICATOR|CLOSER)\b"
    r")",
    re.IGNORECASE,
)

# Title markers whose whole purpose is authority, a live/private action, or a
# recurring control-plane hold.  Marker matching avoids treating ordinary prose
# such as "apply a patch in a fresh clone" as a live-apply authority card.
_AUTHORITY_TITLE_MARKER_RE = re.compile(
    r"\[(?:"
    r"OPERATOR-(?:HOLD|GATE|ACTION)|OWNER-DECISION|DECISION REQUIRED|"
    r"LAND(?:\+[^\]]*)?|ORDER-LAND|LAND-GATE|LIVE-GATE|"
    r"ACCEPT(?:ANCE)?(?:-GATE)?|APPROVAL|VERDICT|SIGN-?OFF|GO(?:/|-)NO-GO|"
    r"POST-LAUNCH|WATCHDOG|TRACKER|RECURRING|APPSTORE|STORE-REVIEW|"
    r"LIVE-APPLY|INSTALL|RESTART|DESTRUCTIVE|PRODUCTION|QUIESCE|"
    r"(?:[A-Z0-9+]+-)*GATE(?:-[A-Z0-9+]+)*"
    r")\]",
    re.IGNORECASE,
)
_APPROVAL_AUTHORITY_RE = re.compile(
    r"(?:\b(?:FABLE|OPERATOR|OWNER) APPROVAL REQUIRED\b|"
    r"\bAPPROVAL REQUIRED (?:BY|FROM) (?:FABLE|OPERATOR|OWNER)\b)",
    re.IGNORECASE,
)
_FABLE_AUTHORITY_ACTION_RE = re.compile(
    r"\[FABLE(?:-[^\]]*)?\].{0,96}"
    r"(?:\[(?:APPLY|LAND|LIVE|INSTALL|RESTART|CONTROL|CAPACITY|PR\d+)[^\]]*\]"
    r"|\b(?:apply|install|restart|merge|land|rewrite canonical|sole)\b)",
    re.IGNORECASE | re.DOTALL,
)
_LIVE_AUTHORITY_ACTION_RE = re.compile(
    r"(?:"
    r"\[(?:PORT|APPLY|INSTALL|RESTART|CONTROL)\][^\n]{0,120}"
    r"\b(?:LIVE|PRODUCTION|OPERATOR|FABLE|SERVICE|SYSTEMD|STORE)\b|"
    r"\b(?:LIVE|PRODUCTION)\b[^\n]{0,80}\b(?:APPLY|INSTALL|RESTART|MUTATION)\b"
    r")",
    re.IGNORECASE,
)
_UNBRACKETED_AUTHORITY_ACTION_RE = re.compile(
    r"(?:"
    r"\bOPERATOR\b.{0,64}\b(?:ACTION|APPROVAL|DECISION|RULING|SIGN[- ]?OFF|WALKTHROUGH)\b|"
    r"\b(?:ACTION|APPROVAL|DECISION|RULING|SIGN[- ]?OFF|WALKTHROUGH)\b.{0,64}\bOPERATOR\b|"
    r"\bDESTRUCTIVE\b.{0,64}\b(?:DELETE|LIVE|PRODUCTION|PURGE|STORAGE)\b|"
    r"\b(?:LIVE|PRODUCTION)\b.{0,64}\b(?:DESTRUCTIVE|PURGE)\b|"
    r"\bLIVE[- ]APPLY\b|"
    r"\b(?:INSTALL|RESTART|RELEASE)\b.{0,64}\b(?:APP|DISPATCHER|GATEWAY|LIVE|PRODUCTION|SERVICE|STORE|SYSTEMD)\b|"
    r"\b(?:APP|DISPATCHER|GATEWAY|LIVE|PRODUCTION|SERVICE|STORE|SYSTEMD)\b.{0,64}\b(?:INSTALL|RESTART|RELEASE)\b|"
    r"\b(?:PERSISTENT|POST[- ]LAUNCH|PRODUCTION|RECURRING)\b.{0,64}\b(?:STEWARD|TRACKER|WATCHDOG)\b|"
    r"\b(?:STEWARD|TRACKER|WATCHDOG)\b.{0,64}\b(?:GATE|HYGIENE|LAUNCH|POST[- ]LAUNCH|PRODUCTION|RELEASE)\b"
    r")",
    re.IGNORECASE,
)

_EXECUTOR_TITLE_RE = re.compile(
    r"\[(?:"
    r"AUTHOR|FIX(?:-[^\]]*)?|REBIND|REWORK|REREVIEW|REVIEW|VERIFY|"
    r"AUDIT|INVESTIGATE|REPRODUCE|IMPLEMENT|BUILD|MIGRATION|REPORT|"
    r"RESEARCH|TEST|QA|DOCS?|SWEEP"
    r")\]",
    re.IGNORECASE,
)
_EXECUTOR_DELIVERABLE_RE = re.compile(
    r"\b(?:open (?:a )?PR|push (?:the )?branch|author (?:a )?(?:patch|fix)|"
    r"implement (?:the )?(?:fix|change)|produce (?:a )?(?:patch|report))\b",
    re.IGNORECASE,
)

# A dependency-held executor card may be parked on an authority lane even when
# its title advertises AUTHOR/REVIEW work.  The contract must be explicit if no
# open parent exists; otherwise a parked card could silently become permanent
# authority-lane backlog.
_PARKING_CONTRACT_RE = re.compile(
    r"(?:"
    r"\bon-promote\s*:|"
    r"\bPARENT-BYPASS FENCE\b|"
    r"\bOPERATOR[- ](?:HOLD|GATE)\b|"
    r"\bNON[- ]?SPAWNABLE\b|"
    r"\bDO NOT (?:AUTO[- ]?)?DISPATCH\b|"
    r"\bNEVER DISPATCH\b|"
    r"\bKEEP (?:BLOCKED|SCHEDULED|IN TODO)\b|"
    r"\bWAIT(?:ING)? (?:FOR|ON) (?:PARENT|DEPENDENC|GATE)\b"
    r")",
    re.IGNORECASE,
)


def _blob(title: Optional[str], body: Optional[str]) -> str:
    return f"{title or ''}\n{body or ''}".strip()


def is_nonspawnable_contract(
    title: Optional[str],
    body: Optional[str] = None,
    *,
    task_id: Optional[str] = None,
) -> bool:
    """Return whether a card is authority/private/live-action control work.

    External schedulers should call this before age-based assignment or
    promotion.  It deliberately recognizes recurring trackers and post-launch
    holds, which do not necessarily contain the older ``GATE-X`` spelling.
    """

    if str(task_id or "").strip().casefold() in NONSPAWNABLE_TASK_IDS:
        return True

    blob = _blob(title, body)
    title_text = str(title or "")
    title_is_executor = bool(_EXECUTOR_TITLE_RE.search(title_text))
    card_is_executor = bool(title_is_executor or _EXECUTOR_DELIVERABLE_RE.search(blob))

    # Title-level authority markers are deliberate custody contracts and win
    # even if a later marker is executor-shaped (for example
    # ``[LIVE-APPLY][VERIFY]``).
    if (
        _AUTHORITY_TITLE_MARKER_RE.search(title_text)
        or _APPROVAL_AUTHORITY_RE.search(title_text)
        or _LIVE_AUTHORITY_ACTION_RE.search(title_text)
    ):
        return True

    if _UNBRACKETED_AUTHORITY_ACTION_RE.search(title_text) and not card_is_executor:
        return True

    # A Fable marker plus an authority action is a strong live-custody signal,
    # except when the card explicitly advertises worker output. This keeps
    # ``[FABLE][AUTHOR]`` / ``[FABLE][REVIEW]`` routable to an executor.
    if _FABLE_AUTHORITY_ACTION_RE.search(title_text) and not card_is_executor:
        return True

    # Executor cards routinely mention the sole lander, production, or a
    # do-not-dispatch parking contract in their body. Those instructions govern
    # custody; they do not turn the mechanical deliverable into authority work.
    if card_is_executor:
        return False

    return bool(
        _NONSPAWNABLE_CONTRACT_RE.search(blob)
        or _LIVE_AUTHORITY_ACTION_RE.search(blob)
        or _UNBRACKETED_AUTHORITY_ACTION_RE.search(blob)
    )


def is_executor_shaped(title: Optional[str], body: Optional[str] = None) -> bool:
    """Return whether the card advertises a mechanical worker deliverable."""

    return bool(
        _EXECUTOR_TITLE_RE.search(str(title or ""))
        or _EXECUTOR_DELIVERABLE_RE.search(_blob(title, body))
    )


def has_nonspawnable_parking_contract(
    title: Optional[str], body: Optional[str] = None
) -> bool:
    """Return whether a parked executor card names its fail-closed custody."""

    return bool(_PARKING_CONTRACT_RE.search(_blob(title, body)))


def assignment_guard_reason(
    *,
    title: Optional[str],
    body: Optional[str],
    assignee: Optional[str],
    status: Optional[str],
    authority_profiles: Iterable[str] | str,
    has_open_parent: bool = False,
    task_id: Optional[str] = None,
    old_title: Optional[str] = None,
    old_body: Optional[str] = None,
    old_assignee: Optional[str] = None,
    old_status: Optional[str] = None,
) -> Optional[str]:
    """Return a stable denial code, or ``None`` when the state is allowed.

    The policy is opt-in: an empty ``authority_profiles`` collection preserves
    legacy behavior.  Once enabled it is bidirectional:

    * authority/live/private-action cards may be parked only unassigned or on a
      configured authority profile, and never in ``ready``/``running``;
    * executor cards may wait on an authority profile only in a non-dispatching
      parked state with an open parent or an explicit parking contract.
    """

    profile_values: Iterable[str]
    if isinstance(authority_profiles, str):
        profile_values = authority_profiles.split(",")
    else:
        profile_values = authority_profiles
    authorities = {
        str(profile).strip().casefold()
        for profile in profile_values
        if str(profile).strip()
    }
    if not authorities:
        return None

    candidate = str(assignee or "").strip().casefold()
    normalized_status = str(status or "").strip().casefold()

    # Terminal rows cannot spawn. Preserve the ability to complete/archive
    # legacy cards that predate policy activation even if their old assignee
    # would be invalid for new active work.
    if normalized_status in TERMINAL_STATUSES:
        return None

    new_nonspawnable = is_nonspawnable_contract(title, body, task_id=task_id)
    new_executor = is_executor_shaped(title, body)
    old_nonspawnable = old_status is not None and is_nonspawnable_contract(
        old_title,
        old_body,
        task_id=task_id,
    )
    old_executor_held_by_authority = (
        old_status is not None
        and str(old_assignee or "").strip().casefold() in authorities
        and is_executor_shaped(old_title, old_body)
    )

    # A parked executor may move back to an executor profile, but a raw writer
    # may not erase its executor semantics while leaving it in authority custody.
    # Otherwise a second UPDATE could promote the now ordinary-looking row while
    # the same authority profile still owns it.
    if (
        old_executor_held_by_authority
        and (not candidate or candidate in authorities)
        and normalized_status not in TERMINAL_STATUSES
        and not new_executor
        and not new_nonspawnable
    ):
        return "authority_executor_semantics_erased"

    # On UPDATE, the prior authority classification remains binding for the
    # candidate assignee/state. A raw writer therefore cannot atomically erase
    # the authority words while moving the same row to an executor/active state.
    if old_nonspawnable:
        if normalized_status in ACTIVE_STATUSES:
            return "nonspawnable_contract_active"
        if candidate and candidate not in authorities:
            return "nonspawnable_contract_executor_assignee"
        if not new_nonspawnable and not new_executor:
            return "nonspawnable_contract_semantics_erased"

    if new_nonspawnable:
        if normalized_status in ACTIVE_STATUSES:
            return "nonspawnable_contract_active"
        if candidate and candidate not in authorities:
            return "nonspawnable_contract_executor_assignee"
        return None

    if is_executor_shaped(title, body) and candidate in authorities:
        if normalized_status in TERMINAL_STATUSES:
            return None
        if normalized_status not in EXECUTOR_AUTHORITY_PARKED_STATUSES:
            return "authority_executor_not_parked"
        if not has_open_parent and not has_nonspawnable_parking_contract(title, body):
            return "authority_executor_missing_parking_contract"

    return None
