#!/usr/bin/env python3
"""Fail-closed Buzz <-> Kanban fleet control bridge.

Phase 1 accepts only owner-signed, marker-prefixed JSON envelopes from one
configured Buzz channel. It creates parked ``todo`` cards through Hermes'
boardd-backed CLI, reads the card back, and only then emits a thread receipt.
It also replaces aggregate ticker posts with cursor-based high-signal Kanban
activity messages.

The process expects Buzz credentials in its environment. Configuration that is
not secret is supplied as CLI arguments so it does not become another env-var
surface. Production enablement and service installation are intentionally out
of scope for this script; Fable remains the sole lander/operator.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence

INTAKE_MARKER = "FLEET_KANBAN_INTAKE_V1"
RECEIPT_MARKER_PREFIX = "KANBAN_INTAKE_RECEIPT_V1"
BOARD_EVENT_MARKER_PREFIX = "KANBAN_HIGH_SIGNAL_V1"
SCHEMA = "buzz.kanban.intake/v1"
STATE_VERSION = 1

HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8}$")
SPECIALIST_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|private[_-]?key|password|token|secret)\b\s*[:=]\s*\S{12,}"
)
KNOWN_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|nsec1[023456789acdefghjklmnpqrstuvwxyz]{20,})\b"
)

REQUIRED_FIELDS = frozenset({
    "schema",
    "idempotency_key",
    "title",
    "objective",
    "acceptance_criteria",
    "requested_specialist",
    "urgency",
    "risk",
    "workspace",
    "evidence",
    "reviewer",
    "approval_gate",
})
OPTIONAL_FIELDS = frozenset({"parents"})
URGENCY_PRIORITIES = {"low": 0, "normal": 100, "high": 500, "critical": 1000}
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})

HIGH_SIGNAL_KINDS = {
    "assigned": "assignment",
    "review": "review ready",
    "blocked": "blocker",
    "dependency_wait": "dependency wait",
    "block_loop_detected": "repeated blocker",
    "completion_blocked_hallucination": "failed verification",
    "crashed": "worker crashed",
    "spawn_failed": "worker spawn failed",
    "gave_up": "worker gave up",
    "timed_out": "worker timed out",
    "rate_limited": "provider quota hold",
    "scheduled": "hold",
    "unblocked": "resume",
    "promoted_manual": "operator resume",
    "completed": "completion",
}


class BridgeError(RuntimeError):
    """Base class for bridge failures with a stable, non-secret code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class IntakeRejected(BridgeError):
    """Permanent rejection of an intake event."""


class RetryableBridgeError(BridgeError):
    """Transient failure; the event must remain eligible for retry."""


@dataclasses.dataclass(frozen=True)
class IntakeEnvelope:
    schema: str
    idempotency_key: str
    title: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    requested_specialist: str
    urgency: str
    risk: str
    workspace: str
    evidence: tuple[str, ...]
    reviewer: str
    approval_gate: str
    parents: tuple[str, ...] = ()

    @property
    def priority(self) -> int:
        return URGENCY_PRIORITIES[self.urgency]

    def canonical_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": self.schema,
            "idempotency_key": self.idempotency_key,
            "title": self.title,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "requested_specialist": self.requested_specialist,
            "urgency": self.urgency,
            "risk": self.risk,
            "workspace": self.workspace,
            "evidence": list(self.evidence),
            "reviewer": self.reviewer,
            "approval_gate": self.approval_gate,
        }
        if self.parents:
            value["parents"] = list(self.parents)
        return value


@dataclasses.dataclass(frozen=True)
class ValidatedIntake:
    event_id: str
    channel: str
    owner_pubkey: str
    created_at: int
    envelope: IntakeEnvelope


@dataclasses.dataclass(frozen=True)
class ProcessResult:
    event_id: str
    outcome: str
    card_id: str | None = None
    receipt_event_id: str | None = None
    reason: str | None = None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntakeRejected("duplicate_json_key", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _clean_string(
    value: Any,
    field: str,
    *,
    max_length: int,
    multiline: bool = True,
) -> str:
    if not isinstance(value, str):
        raise IntakeRejected("invalid_field_type", f"{field} must be a string")
    if not value or value != value.strip():
        raise IntakeRejected(
            "invalid_field_value", f"{field} must be non-empty and trimmed"
        )
    if len(value) > max_length:
        raise IntakeRejected(
            "field_too_long", f"{field} exceeds {max_length} characters"
        )
    if "\x00" in value or any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise IntakeRejected(
            "invalid_control_character", f"{field} contains a control character"
        )
    if not multiline and ("\n" in value or "\r" in value):
        raise IntakeRejected("invalid_field_value", f"{field} must be one line")
    if SECRET_ASSIGNMENT_RE.search(value) or KNOWN_TOKEN_RE.search(value):
        raise IntakeRejected(
            "secret_material_rejected",
            f"{field} appears to contain secret material",
        )
    return value


def _clean_string_list(
    value: Any, field: str, *, max_items: int, max_length: int
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > max_items:
        raise IntakeRejected(
            "invalid_field_value",
            f"{field} must be a non-empty array with at most {max_items} entries",
        )
    cleaned = tuple(
        _clean_string(item, f"{field}[{index}]", max_length=max_length)
        for index, item in enumerate(value)
    )
    if len(set(cleaned)) != len(cleaned):
        raise IntakeRejected(
            "duplicate_array_value", f"{field} contains duplicate entries"
        )
    return cleaned


def _extract_envelope_json(content: str) -> str:
    prefix = f"{INTAKE_MARKER}\n"
    if not content.startswith(prefix):
        raise IntakeRejected(
            "missing_marker", f"message must start with {INTAKE_MARKER}"
        )
    payload = content[len(prefix) :].strip()
    if payload.startswith("```json\n") and payload.endswith("```"):
        payload = payload[len("```json\n") : -len("```")].strip()
    if not payload or not payload.startswith("{") or not payload.endswith("}"):
        raise IntakeRejected(
            "invalid_envelope", "marker must be followed by one JSON object"
        )
    return payload


def _validate_workspace(workspace: str, allowed_workspace_roots: Sequence[Path]) -> str:
    if workspace == "scratch":
        return workspace
    if ":" not in workspace:
        raise IntakeRejected(
            "invalid_workspace",
            "workspace must be scratch, dir:/absolute/path, or worktree:/absolute/path",
        )
    kind, raw_path = workspace.split(":", 1)
    if kind not in {"dir", "worktree"} or not raw_path.startswith("/"):
        raise IntakeRejected(
            "invalid_workspace",
            "workspace must be scratch, dir:/absolute/path, or worktree:/absolute/path",
        )
    candidate = Path(os.path.normpath(raw_path))
    if ".." in Path(raw_path).parts:
        raise IntakeRejected("invalid_workspace", "workspace must not contain '..'")
    roots = tuple(root.resolve(strict=False) for root in allowed_workspace_roots)
    if not roots:
        raise IntakeRejected(
            "workspace_not_allowlisted",
            "non-scratch workspace requires an operator-configured workspace root",
        )
    candidate_resolved = candidate.resolve(strict=False)
    if not any(
        candidate_resolved == root or root in candidate_resolved.parents
        for root in roots
    ):
        raise IntakeRejected(
            "workspace_not_allowlisted", "workspace is outside configured roots"
        )
    return f"{kind}:{candidate_resolved}"


def parse_envelope(
    content: str,
    *,
    allowed_workspace_roots: Sequence[Path],
    allowed_specialists: frozenset[str],
) -> IntakeEnvelope:
    raw_json = _extract_envelope_json(content)
    try:
        payload = json.loads(raw_json, object_pairs_hook=_strict_object)
    except IntakeRejected:
        raise
    except (TypeError, json.JSONDecodeError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise IntakeRejected(
            "invalid_json", f"invalid JSON envelope: {detail}"
        ) from exc
    if not isinstance(payload, dict):
        raise IntakeRejected("invalid_envelope", "envelope must be a JSON object")

    keys = frozenset(payload)
    missing = REQUIRED_FIELDS - keys
    extra = keys - REQUIRED_FIELDS - OPTIONAL_FIELDS
    if missing:
        raise IntakeRejected(
            "missing_fields", "missing fields: " + ", ".join(sorted(missing))
        )
    if extra:
        raise IntakeRejected(
            "unknown_fields", "unknown fields: " + ", ".join(sorted(extra))
        )

    schema = _clean_string(payload["schema"], "schema", max_length=64, multiline=False)
    if schema != SCHEMA:
        raise IntakeRejected("unsupported_schema", f"schema must be {SCHEMA}")

    idempotency_key = _clean_string(
        payload["idempotency_key"], "idempotency_key", max_length=128, multiline=False
    )
    if not IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise IntakeRejected(
            "invalid_idempotency_key", "idempotency_key has invalid characters"
        )

    title = _clean_string(payload["title"], "title", max_length=160, multiline=False)
    objective = _clean_string(payload["objective"], "objective", max_length=4000)
    acceptance = _clean_string_list(
        payload["acceptance_criteria"],
        "acceptance_criteria",
        max_items=30,
        max_length=1000,
    )
    specialist = _clean_string(
        payload["requested_specialist"],
        "requested_specialist",
        max_length=64,
        multiline=False,
    ).lower()
    if not SPECIALIST_RE.fullmatch(specialist):
        raise IntakeRejected(
            "invalid_specialist",
            "requested_specialist must be a lowercase Hermes profile name",
        )
    if specialist not in allowed_specialists:
        raise IntakeRejected(
            "unsupported_specialist",
            "requested_specialist is not in the configured profile allowlist",
        )

    urgency = _clean_string(
        payload["urgency"], "urgency", max_length=16, multiline=False
    ).lower()
    if urgency not in URGENCY_PRIORITIES:
        raise IntakeRejected(
            "invalid_urgency", "urgency must be low, normal, high, or critical"
        )
    risk = _clean_string(
        payload["risk"], "risk", max_length=16, multiline=False
    ).lower()
    if risk not in RISK_LEVELS:
        raise IntakeRejected(
            "invalid_risk", "risk must be low, medium, high, or critical"
        )

    workspace = _clean_string(
        payload["workspace"], "workspace", max_length=1024, multiline=False
    )
    workspace = _validate_workspace(workspace, allowed_workspace_roots)
    evidence = _clean_string_list(
        payload["evidence"], "evidence", max_items=30, max_length=1000
    )
    reviewer = _clean_string(
        payload["reviewer"], "reviewer", max_length=160, multiline=False
    )
    approval_gate = _clean_string(
        payload["approval_gate"], "approval_gate", max_length=240
    )
    if risk in {"high", "critical"} and approval_gate.casefold() in {
        "none",
        "no gate",
        "n/a",
    }:
        raise IntakeRejected(
            "approval_gate_required",
            "high or critical risk intake requires an explicit approval gate",
        )

    parents_raw = payload.get("parents", [])
    if not isinstance(parents_raw, list) or len(parents_raw) > 8:
        raise IntakeRejected(
            "invalid_parents", "parents must be an array of at most 8 task IDs"
        )
    parents: list[str] = []
    for index, value in enumerate(parents_raw):
        parent = _clean_string(
            value, f"parents[{index}]", max_length=10, multiline=False
        )
        if not TASK_ID_RE.fullmatch(parent):
            raise IntakeRejected("invalid_parent", f"invalid parent task ID: {parent}")
        if parent in parents:
            raise IntakeRejected(
                "duplicate_parent", f"duplicate parent task ID: {parent}"
            )
        parents.append(parent)

    return IntakeEnvelope(
        schema=schema,
        idempotency_key=idempotency_key,
        title=title,
        objective=objective,
        acceptance_criteria=acceptance,
        requested_specialist=specialist,
        urgency=urgency,
        risk=risk,
        workspace=workspace,
        evidence=evidence,
        reviewer=reviewer,
        approval_gate=approval_gate,
        parents=tuple(parents),
    )


def validate_intake_event(
    raw: Mapping[str, Any],
    *,
    channel: str,
    owner_pubkey: str,
    allowed_workspace_roots: Sequence[Path],
    allowed_specialists: frozenset[str],
    now: int,
    max_age_seconds: int,
    future_skew_seconds: int,
    allowed_kinds: frozenset[int] = frozenset({9}),
) -> ValidatedIntake:
    event_id = raw.get("id")
    pubkey = raw.get("pubkey")
    content = raw.get("content")
    created_at = raw.get("created_at")
    kind = raw.get("kind")
    tags = raw.get("tags")

    if not isinstance(event_id, str) or not HEX_64_RE.fullmatch(event_id):
        raise IntakeRejected(
            "invalid_event_id", "event ID must be 64 lowercase hex characters"
        )
    if pubkey != owner_pubkey:
        raise IntakeRejected(
            "unauthorized_sender", "sender is not the configured owner"
        )
    if not isinstance(kind, int) or kind not in allowed_kinds:
        raise IntakeRejected("invalid_event_kind", "event kind is not allowlisted")
    if not isinstance(tags, list) or not any(
        isinstance(tag, list) and len(tag) >= 2 and tag[0] == "h" and tag[1] == channel
        for tag in tags
    ):
        raise IntakeRejected(
            "wrong_channel", "event is not tagged for the configured channel"
        )
    if not isinstance(created_at, int):
        raise IntakeRejected("invalid_created_at", "created_at must be an integer")
    if created_at > now + future_skew_seconds:
        raise IntakeRejected("future_event", "event timestamp is too far in the future")
    if now - created_at > max_age_seconds:
        raise IntakeRejected(
            "stale_event", "event is outside the configured freshness window"
        )
    if not isinstance(content, str):
        raise IntakeRejected("invalid_content", "event content must be text")

    envelope = parse_envelope(
        content,
        allowed_workspace_roots=allowed_workspace_roots,
        allowed_specialists=allowed_specialists,
    )
    return ValidatedIntake(
        event_id=event_id,
        channel=channel,
        owner_pubkey=owner_pubkey,
        created_at=created_at,
        envelope=envelope,
    )


def _canonical_envelope_json(envelope: IntakeEnvelope) -> str:
    return json.dumps(
        envelope.canonical_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def render_card_body(intake: ValidatedIntake) -> str:
    envelope = intake.envelope
    canonical = _canonical_envelope_json(envelope)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    acceptance = "\n".join(
        f"{index}. {item}" for index, item in enumerate(envelope.acceptance_criteria, 1)
    )
    evidence = "\n".join(f"- {item}" for item in envelope.evidence)
    parents = ", ".join(envelope.parents) if envelope.parents else "none"
    return (
        "BUZZ_STRUCTURED_INTAKE_V1\n"
        f"SOURCE_EVENT: {intake.event_id}\n"
        f"CHANNEL: {intake.channel}\n"
        f"OWNER: {intake.owner_pubkey}\n"
        f"IDEMPOTENCY_KEY: {envelope.idempotency_key}\n"
        f"ENVELOPE_SHA256: {digest}\n"
        f"REQUESTED_SPECIALIST: {envelope.requested_specialist}\n"
        f"URGENCY: {envelope.urgency}\n"
        f"RISK: {envelope.risk}\n"
        f"WORKSPACE: {envelope.workspace}\n"
        f"REVIEWER: {envelope.reviewer}\n"
        f"APPROVAL_GATE: {envelope.approval_gate}\n"
        f"PARENTS: {parents}\n\n"
        f"OBJECTIVE:\n{envelope.objective}\n\n"
        f"ACCEPTANCE_CRITERIA:\n{acceptance}\n\n"
        f"EVIDENCE:\n{evidence}\n\n"
        f"CANONICAL_ENVELOPE_JSON:\n{canonical}"
    )


def render_receipt(intake: ValidatedIntake, card: Mapping[str, Any]) -> str:
    marker = f"{RECEIPT_MARKER_PREFIX}:{intake.event_id}"
    evidence = "\n".join(f"- {item}" for item in intake.envelope.evidence)
    return (
        f"{marker}\n"
        f"card_id: {card['id']}\n"
        f"owner: {card['created_by']}\n"
        f"status: {card['status']}\n"
        f"evidence_required:\n{evidence}\n"
        f"reviewer: {intake.envelope.reviewer}"
    )


def _workspace_parts(workspace: str) -> tuple[str, str | None]:
    if workspace == "scratch":
        return "scratch", None
    kind, path = workspace.split(":", 1)
    return kind, path


def validate_card_readback(
    intake: ValidatedIntake,
    card_id: str,
    show_payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    card = show_payload.get("task")
    if not isinstance(card, dict):
        raise RetryableBridgeError(
            "broker_readback_missing", "broker show response has no task"
        )
    expected_kind, expected_path = _workspace_parts(intake.envelope.workspace)
    expected: dict[str, Any] = {
        "id": card_id,
        "title": intake.envelope.title,
        "body": render_card_body(intake),
        "assignee": intake.envelope.requested_specialist,
        "status": "todo",
        "priority": intake.envelope.priority,
        "workspace_kind": expected_kind,
        "workspace_path": expected_path,
        "created_by": intake.owner_pubkey,
        "idempotency_key": intake.envelope.idempotency_key,
    }
    mismatches = [
        field
        for field, expected_value in expected.items()
        if card.get(field) != expected_value
    ]
    parents = show_payload.get("parents")
    if sorted(parents or []) != sorted(intake.envelope.parents):
        mismatches.append("parents")
    if mismatches:
        raise RetryableBridgeError(
            "broker_readback_mismatch",
            "broker read-back mismatch: " + ", ".join(sorted(set(mismatches))),
        )
    return card


def _decode_json_output(output: str, *, code: str) -> Any:
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RetryableBridgeError(code, "command returned invalid JSON") from exc


def _bounded_error(completed: subprocess.CompletedProcess[str]) -> str:
    text = (
        (completed.stderr or completed.stdout or "command failed")
        .strip()
        .replace("\x00", "")
    )
    return text[:500]


class HermesBroker:
    """Create/read cards through the Hermes CLI and query events through boardd."""

    def __init__(self, *, hermes_bin: str, board: str, board_socket: Path):
        self.hermes_bin = hermes_bin
        self.board = board
        self.board_socket = board_socket

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HERMES_KANBAN_BROKER"] = "1"
        env["BOARDD_SOCK"] = str(self.board_socket)
        env["HERMES_KANBAN_BOARD"] = self.board
        for name in (
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_WORKSPACE",
            "HERMES_KANBAN_BRANCH",
        ):
            env.pop(name, None)
        return env

    def _run(self, args: Sequence[str]) -> Any:
        completed = subprocess.run(
            [self.hermes_bin, "kanban", "--board", self.board, *args],
            capture_output=True,
            text=True,
            env=self._env(),
            check=False,
        )
        if completed.returncode != 0:
            raise RetryableBridgeError(
                "broker_command_failed", _bounded_error(completed)
            )
        return _decode_json_output(completed.stdout, code="broker_invalid_json")

    def create(self, intake: ValidatedIntake) -> str:
        envelope = intake.envelope
        args = [
            "create",
            envelope.title,
            "--body",
            render_card_body(intake),
            "--assignee",
            envelope.requested_specialist,
            "--workspace",
            envelope.workspace,
            "--priority",
            str(envelope.priority),
            "--idempotency-key",
            envelope.idempotency_key,
            "--created-by",
            intake.owner_pubkey,
            "--initial-status",
            "todo",
            "--json",
        ]
        for parent in envelope.parents:
            args.extend(("--parent", parent))
        payload = self._run(args)
        card_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(card_id, str) or not TASK_ID_RE.fullmatch(card_id):
            raise RetryableBridgeError(
                "broker_create_invalid", "broker create returned an invalid task ID"
            )
        return card_id

    def read_back(self, card_id: str) -> Mapping[str, Any]:
        payload = self._run(("show", card_id, "--json"))
        if not isinstance(payload, dict):
            raise RetryableBridgeError(
                "broker_readback_invalid", "broker show returned an invalid object"
            )
        return payload

    def query_events(self, *, after_id: int, limit: int) -> list[dict[str, Any]]:
        try:
            from hermes_cli.kb_client import Client

            client = Client(
                sock_path=str(self.board_socket), worker_id="buzz-kanban-bridge"
            )
            try:
                rows = client.query(
                    "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at, "
                    "t.title, t.assignee, t.status "
                    "FROM task_events e JOIN tasks t ON t.id = e.task_id "
                    "WHERE e.id > ? ORDER BY e.id ASC LIMIT ?",
                    [int(after_id), int(limit)],
                    max_rows=limit,
                )
            finally:
                client.close()
        except Exception as exc:
            raise RetryableBridgeError(
                "broker_event_query_failed", str(exc)[:500]
            ) from exc
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RetryableBridgeError(
                "broker_event_query_invalid", "broker event query returned invalid data"
            )
        return rows

    def latest_event_id(self) -> int:
        try:
            from hermes_cli.kb_client import Client

            client = Client(
                sock_path=str(self.board_socket), worker_id="buzz-kanban-bridge"
            )
            try:
                rows = client.query(
                    "SELECT COALESCE(MAX(id), 0) AS latest_id FROM task_events",
                    max_rows=1,
                )
            finally:
                client.close()
        except Exception as exc:
            raise RetryableBridgeError(
                "broker_event_query_failed", str(exc)[:500]
            ) from exc
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
            or not isinstance(rows[0].get("latest_id"), int)
        ):
            raise RetryableBridgeError(
                "broker_event_query_invalid", "latest event query returned invalid data"
            )
        return int(rows[0]["latest_id"])


class BuzzClient:
    """Minimal Buzz CLI adapter with read-after-write verification."""

    def __init__(self, *, buzz_bin: str, channel: str, bridge_pubkey: str):
        self.buzz_bin = buzz_bin
        self.channel = channel
        self.bridge_pubkey = bridge_pubkey

    def _run(self, args: Sequence[str], *, input_text: str | None = None) -> Any:
        completed = subprocess.run(
            [self.buzz_bin, *args],
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RetryableBridgeError("buzz_command_failed", _bounded_error(completed))
        return _decode_json_output(completed.stdout, code="buzz_invalid_json")

    @staticmethod
    def _items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("messages", "events", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def fetch_messages(self, *, since: int | None, limit: int) -> list[dict[str, Any]]:
        args = ["messages", "get", "--channel", self.channel, "--limit", str(limit)]
        if since is not None:
            args.extend(("--since", str(max(0, since))))
        return self._items(self._run(args))

    def _thread_messages(
        self, source_event_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        return self._items(
            self._run((
                "messages",
                "thread",
                "--channel",
                self.channel,
                "--event",
                source_event_id,
                "--limit",
                str(limit),
            ))
        )

    def _find_verified_marker(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        marker: str,
        expected_content: str,
    ) -> str | None:
        for message in messages:
            content = message.get("content")
            if message.get("pubkey") != self.bridge_pubkey or not isinstance(
                content, str
            ):
                continue
            if not content.startswith(marker):
                continue
            if content != expected_content:
                raise RetryableBridgeError(
                    "buzz_marker_collision",
                    "bridge-authored marker exists with different content",
                )
            event_id = message.get("id") or message.get("event_id")
            if isinstance(event_id, str) and HEX_64_RE.fullmatch(event_id):
                return event_id
        return None

    @staticmethod
    def _sent_event_id(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        candidates = [payload.get("id"), payload.get("event_id")]
        event = payload.get("event")
        if isinstance(event, dict):
            candidates.extend((event.get("id"), event.get("event_id")))
        for value in candidates:
            if isinstance(value, str) and HEX_64_RE.fullmatch(value):
                return value
        return None

    def ensure_thread_receipt(self, *, source_event_id: str, content: str) -> str:
        marker = content.splitlines()[0]
        existing = self._find_verified_marker(
            self._thread_messages(source_event_id),
            marker=marker,
            expected_content=content,
        )
        if existing:
            return existing

        payload = self._run(
            (
                "messages",
                "send",
                "--channel",
                self.channel,
                "--reply-to",
                source_event_id,
                "--content",
                "-",
            ),
            input_text=content,
        )
        sent_id = self._sent_event_id(payload)
        verified = self._find_verified_marker(
            self._thread_messages(source_event_id),
            marker=marker,
            expected_content=content,
        )
        if not verified or (sent_id and sent_id != verified):
            raise RetryableBridgeError(
                "buzz_receipt_readback_failed", "receipt was not read back from Buzz"
            )
        return verified

    def ensure_channel_message(self, *, marker: str, content: str) -> str:
        recent = self.fetch_messages(since=None, limit=200)
        existing = self._find_verified_marker(
            recent, marker=marker, expected_content=content
        )
        if existing:
            return existing
        payload = self._run(
            ("messages", "send", "--channel", self.channel, "--content", "-"),
            input_text=content,
        )
        sent_id = self._sent_event_id(payload)
        verified = self._find_verified_marker(
            self.fetch_messages(since=None, limit=200),
            marker=marker,
            expected_content=content,
        )
        if not verified or (sent_id and sent_id != verified):
            raise RetryableBridgeError(
                "buzz_board_event_readback_failed",
                "board event message was not read back from Buzz",
            )
        return verified


class StateStore:
    """0600, atomic, fsync-backed cursor and receipt state."""

    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "buzz_cursor": 0,
            "board_event_cursor": 0,
            "board_event_initialized": False,
            "events": {},
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise BridgeError(
                "unsafe_state_permissions",
                "state file must not be group/world accessible",
            )
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError(
                "invalid_state", "state file is unreadable or invalid JSON"
            ) from exc
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            raise BridgeError("invalid_state_version", "unsupported state file version")
        if not isinstance(payload.get("events"), dict):
            raise BridgeError("invalid_state", "state events must be an object")
        for field in ("buzz_cursor", "board_event_cursor"):
            if not isinstance(payload.get(field), int) or payload[field] < 0:
                raise BridgeError(
                    "invalid_state", f"{field} must be a non-negative integer"
                )
        if not isinstance(payload.get("board_event_initialized"), bool):
            raise BridgeError(
                "invalid_state", "board_event_initialized must be a boolean"
            )
        return payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = (
            json.dumps(self.data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        )
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()

    def prune(self, *, max_events: int = 5000) -> None:
        events = self.data["events"]
        if len(events) <= max_events:
            return
        ordered = sorted(
            events.items(), key=lambda item: int(item[1].get("handled_at", 0))
        )
        self.data["events"] = dict(ordered[-max_events:])


@contextlib.contextmanager
def exclusive_lock(state_path: Path) -> Iterator[None]:
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BridgeError(
                "already_running", "another bridge process holds the state lock"
            ) from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _is_intake_candidate(raw: Mapping[str, Any]) -> bool:
    content = raw.get("content")
    return isinstance(content, str) and content.startswith(f"{INTAKE_MARKER}\n")


class BuzzKanbanBridge:
    def __init__(
        self,
        *,
        channel: str,
        owner_pubkey: str,
        allowed_workspace_roots: Sequence[Path],
        allowed_specialists: frozenset[str],
        max_age_seconds: int,
        future_skew_seconds: int,
        broker: Any,
        buzz: Any,
        state: StateStore,
        clock: Callable[[], float] = time.time,
    ):
        self.channel = channel
        self.owner_pubkey = owner_pubkey
        self.allowed_workspace_roots = tuple(allowed_workspace_roots)
        self.allowed_specialists = allowed_specialists
        self.max_age_seconds = max_age_seconds
        self.future_skew_seconds = future_skew_seconds
        self.broker = broker
        self.buzz = buzz
        self.state = state
        self.clock = clock

    def process_intake(self, raw: Mapping[str, Any]) -> ProcessResult:
        event_id = raw.get("id")
        if isinstance(event_id, str):
            prior = self.state.data["events"].get(event_id)
            if isinstance(prior, dict):
                return ProcessResult(
                    event_id=event_id,
                    outcome="duplicate",
                    card_id=prior.get("card_id"),
                    receipt_event_id=prior.get("receipt_event_id"),
                    reason=prior.get("reason"),
                )

        try:
            intake = validate_intake_event(
                raw,
                channel=self.channel,
                owner_pubkey=self.owner_pubkey,
                allowed_workspace_roots=self.allowed_workspace_roots,
                allowed_specialists=self.allowed_specialists,
                now=int(self.clock()),
                max_age_seconds=self.max_age_seconds,
                future_skew_seconds=self.future_skew_seconds,
            )
        except IntakeRejected as exc:
            safe_event_id = (
                event_id
                if isinstance(event_id, str) and HEX_64_RE.fullmatch(event_id)
                else "invalid"
            )
            if safe_event_id != "invalid":
                self.state.data["events"][safe_event_id] = {
                    "outcome": "rejected",
                    "reason": exc.code,
                    "handled_at": int(self.clock()),
                }
            return ProcessResult(
                event_id=safe_event_id, outcome="rejected", reason=exc.code
            )

        card_id = self.broker.create(intake)
        show_payload = self.broker.read_back(card_id)
        card = validate_card_readback(intake, card_id, show_payload)
        receipt_content = render_receipt(intake, card)
        receipt_event_id = self.buzz.ensure_thread_receipt(
            source_event_id=intake.event_id,
            content=receipt_content,
        )
        self.state.data["events"][intake.event_id] = {
            "outcome": "accepted",
            "card_id": card_id,
            "receipt_event_id": receipt_event_id,
            "owner": intake.owner_pubkey,
            "status": card["status"],
            "handled_at": int(self.clock()),
        }
        return ProcessResult(
            event_id=intake.event_id,
            outcome="accepted",
            card_id=card_id,
            receipt_event_id=receipt_event_id,
        )

    def poll_inbound_once(self, *, limit: int) -> list[ProcessResult]:
        cursor = int(self.state.data["buzz_cursor"])
        # The installed Buzz CLI exposes timestamp (not event-id) cursors. Keep
        # a full freshness-window overlap and dedupe marker events by event ID,
        # so equal-timestamp events and a crash between card/receipt/state do
        # not disappear on the next bounded poll.
        since = max(0, cursor - self.max_age_seconds) if cursor else None
        messages = sorted(
            self.buzz.fetch_messages(since=since, limit=limit),
            key=lambda item: (int(item.get("created_at", 0)), str(item.get("id", ""))),
        )
        results: list[ProcessResult] = []
        for raw in messages:
            created_at = raw.get("created_at")
            if not isinstance(created_at, int):
                continue
            event_id = raw.get("id")
            candidate = _is_intake_candidate(raw)
            already_handled = (
                isinstance(event_id, str) and event_id in self.state.data["events"]
            )
            if created_at <= cursor and (not candidate or already_handled):
                continue
            if candidate:
                result = self.process_intake(raw)
                results.append(result)
            self.state.data["buzz_cursor"] = max(
                int(self.state.data["buzz_cursor"]),
                created_at,
            )
            self.state.prune()
            self.state.save()
        return results

    def poll_board_once(self, *, limit: int) -> list[int]:
        if not self.state.data["board_event_initialized"]:
            # First enablement starts at the live frontier. Replaying months of
            # historical board events would recreate the aggregate-ticker spam
            # this bridge replaces and could bury an operator alert.
            self.state.data["board_event_cursor"] = self.broker.latest_event_id()
            self.state.data["board_event_initialized"] = True
            self.state.save()
            return []
        cursor = int(self.state.data["board_event_cursor"])
        rows = self.broker.query_events(after_id=cursor, limit=limit)
        emitted: list[int] = []
        for row in rows:
            event_id = row.get("id")
            if not isinstance(event_id, int) or event_id <= cursor:
                raise RetryableBridgeError(
                    "broker_event_order_invalid", "board event cursor is not monotonic"
                )
            kind = row.get("kind")
            if kind in HIGH_SIGNAL_KINDS:
                marker = f"{BOARD_EVENT_MARKER_PREFIX}:{event_id}"
                content = render_high_signal_event(row, marker=marker)
                self.buzz.ensure_channel_message(marker=marker, content=content)
                emitted.append(event_id)
            cursor = event_id
            self.state.data["board_event_cursor"] = cursor
            self.state.save()
        return emitted


def _payload_dict(raw_payload: Any) -> dict[str, Any]:
    if isinstance(raw_payload, dict):
        return raw_payload
    if not isinstance(raw_payload, str) or not raw_payload:
        return {}
    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _summary_from_payload(payload: Mapping[str, Any]) -> str | None:
    for key in ("reason", "summary", "result", "message", "assignee", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("summary", "evidence", "decision"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return None


def _redact_secret_like(value: str) -> str:
    value = SECRET_ASSIGNMENT_RE.sub("[REDACTED SECRET ASSIGNMENT]", value)
    return KNOWN_TOKEN_RE.sub("[REDACTED TOKEN]", value)


def render_high_signal_event(row: Mapping[str, Any], *, marker: str) -> str:
    kind = str(row.get("kind"))
    if kind not in HIGH_SIGNAL_KINDS:
        raise ValueError(f"not a high-signal event kind: {kind}")
    payload = _payload_dict(row.get("payload"))
    summary = _summary_from_payload(payload)
    lines = [
        marker,
        f"event: {HIGH_SIGNAL_KINDS[kind]}",
        f"card_id: {row.get('task_id')}",
        f"title: {row.get('title')}",
        f"status: {row.get('status')}",
        f"owner: {row.get('assignee') or 'unassigned'}",
    ]
    if summary:
        lines.append(f"detail: {_redact_secret_like(summary)}")
    return "\n".join(lines)


def _validate_cli_args(args: argparse.Namespace) -> None:
    for field in ("owner_pubkey", "bridge_pubkey"):
        value = getattr(args, field)
        if not HEX_64_RE.fullmatch(value):
            raise BridgeError(
                "invalid_configuration",
                f"--{field.replace('_', '-')} must be 64 lowercase hex",
            )
    if args.max_age_seconds < 1 or args.future_skew_seconds < 0:
        raise BridgeError("invalid_configuration", "freshness values are invalid")
    if args.limit < 1 or args.limit > 1000:
        raise BridgeError("invalid_configuration", "--limit must be between 1 and 1000")
    if args.interval < 1:
        raise BridgeError(
            "invalid_configuration", "--interval must be at least 1 second"
        )
    if len(set(args.allowed_specialist)) != len(args.allowed_specialist) or any(
        not SPECIALIST_RE.fullmatch(value) for value in args.allowed_specialist
    ):
        raise BridgeError(
            "invalid_configuration",
            "--allowed-specialist values must be unique profile slugs",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True, help="exact Buzz channel UUID")
    parser.add_argument(
        "--owner-pubkey", required=True, help="only pubkey authorized for intake"
    )
    parser.add_argument(
        "--bridge-pubkey", required=True, help="pubkey used by this bridge for receipts"
    )
    parser.add_argument(
        "--board", default="fleet", help="Kanban board slug (default: fleet)"
    )
    parser.add_argument(
        "--board-socket", type=Path, required=True, help="boardd Unix socket path"
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        required=True,
        help="durable 0600 cursor/receipt state",
    )
    parser.add_argument(
        "--workspace-root",
        action="append",
        default=[],
        type=Path,
        help="allowlisted root for dir:/ or worktree:/ intake (repeatable)",
    )
    parser.add_argument(
        "--allowed-specialist",
        action="append",
        required=True,
        help="valid Kanban assignee profile slug (repeatable)",
    )
    parser.add_argument("--buzz-bin", default="buzz")
    parser.add_argument("--hermes-bin", default="hermes")
    parser.add_argument("--max-age-seconds", type=int, default=900)
    parser.add_argument("--future-skew-seconds", type=int, default=60)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument(
        "--once", action="store_true", help="run one bounded polling pass"
    )
    parser.add_argument(
        "--inbound-only", action="store_true", help="skip Kanban event delivery"
    )
    parser.add_argument(
        "--outbound-only", action="store_true", help="skip Buzz intake polling"
    )
    return parser


def _log_result(result: ProcessResult) -> None:
    safe = dataclasses.asdict(result)
    print(
        json.dumps(
            {key: value for key, value in safe.items() if value is not None},
            sort_keys=True,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_cli_args(args)
        if args.inbound_only and args.outbound_only:
            raise BridgeError(
                "invalid_configuration",
                "cannot combine --inbound-only and --outbound-only",
            )
        with exclusive_lock(args.state_file):
            state = StateStore(args.state_file)
            broker = HermesBroker(
                hermes_bin=args.hermes_bin,
                board=args.board,
                board_socket=args.board_socket,
            )
            buzz = BuzzClient(
                buzz_bin=args.buzz_bin,
                channel=args.channel,
                bridge_pubkey=args.bridge_pubkey,
            )
            bridge = BuzzKanbanBridge(
                channel=args.channel,
                owner_pubkey=args.owner_pubkey,
                allowed_workspace_roots=args.workspace_root,
                allowed_specialists=frozenset(args.allowed_specialist),
                max_age_seconds=args.max_age_seconds,
                future_skew_seconds=args.future_skew_seconds,
                broker=broker,
                buzz=buzz,
                state=state,
            )
            while True:
                try:
                    if not args.outbound_only:
                        for result in bridge.poll_inbound_once(limit=args.limit):
                            _log_result(result)
                    if not args.inbound_only:
                        emitted = bridge.poll_board_once(limit=args.limit)
                        if emitted:
                            print(json.dumps({"board_events_emitted": emitted}))
                except RetryableBridgeError as exc:
                    print(
                        json.dumps({"error": exc.code, "message": str(exc)}),
                        file=sys.stderr,
                    )
                    if args.once:
                        return 2
                if args.once:
                    return 0
                time.sleep(args.interval)
    except BridgeError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
