from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "buzz_kanban_bridge.py"
SPEC = importlib.util.spec_from_file_location("buzz_kanban_bridge", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bridge_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge_module
SPEC.loader.exec_module(bridge_module)

NOW = 2_000_000_000
CHANNEL = "4a439bfc-f3da-498e-bb38-f8248d7abe35"
OWNER = "7c6cc0316f80d711920878750ff673b7f716ded717a4bd6de44f35261da946d6"
BRIDGE = "d" * 64
SOURCE_EVENT = "a" * 64
CARD_ID = "t_deadbeef"
RECEIPT_EVENT = "b" * 64
WORKSPACE_ROOT = Path("/mnt/fleet-workspaces")


def envelope_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "buzz.kanban.intake/v1",
        "idempotency_key": "buzz-intake-test-v1",
        "title": "Implement structured intake",
        "objective": "Create one parked card and acknowledge it only after broker read-back.",
        "acceptance_criteria": [
            "Reject unauthorized senders.",
            "Create exactly one todo card.",
        ],
        "requested_specialist": "orchestrator",
        "urgency": "high",
        "risk": "low",
        "workspace": "dir:/mnt/fleet-workspaces/buzz-intake",
        "evidence": ["focused tests", "broker read-back receipt"],
        "reviewer": "independent Grok security review",
        "approval_gate": "Fable land and deployment gate",
        "parents": ["t_a29728ae"],
    }
    payload.update(overrides)
    return payload


def raw_event(
    *, payload: dict[str, Any] | None = None, pubkey: str = OWNER, created_at: int = NOW
) -> dict[str, Any]:
    body = json.dumps(payload or envelope_payload(), sort_keys=True)
    return {
        "id": SOURCE_EVENT,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": 9,
        "tags": [["h", CHANNEL]],
        "content": f"FLEET_KANBAN_INTAKE_V1\n```json\n{body}\n```",
    }


class MemoryState:
    def __init__(self) -> None:
        self.data = {
            "version": 1,
            "buzz_cursor": 0,
            "board_event_cursor": 0,
            "board_event_initialized": False,
            "events": {},
        }
        self.save_count = 0

    def save(self) -> None:
        self.save_count += 1

    def prune(self, *, max_events: int = 5000) -> None:
        assert max_events > 0


class FakeBroker:
    def __init__(
        self, *, fail: bool = False, mismatches: dict[str, Any] | None = None
    ) -> None:
        self.fail = fail
        self.mismatches = mismatches or {}
        self.create_calls = 0
        self.read_calls = 0
        self.rows: list[dict[str, Any]] = []

    def create(self, intake: Any) -> str:
        self.create_calls += 1
        if self.fail:
            raise bridge_module.RetryableBridgeError(
                "broker_unavailable", "broker unavailable"
            )
        return CARD_ID

    def read_back(self, card_id: str) -> dict[str, Any]:
        self.read_calls += 1
        intake = self.last_intake
        kind, path = bridge_module._workspace_parts(intake.envelope.workspace)
        card = {
            "id": card_id,
            "title": intake.envelope.title,
            "body": bridge_module.render_card_body(intake),
            "assignee": intake.envelope.requested_specialist,
            "status": "todo",
            "priority": intake.envelope.priority,
            "workspace_kind": kind,
            "workspace_path": path,
            "created_by": intake.owner_pubkey,
            "idempotency_key": intake.envelope.idempotency_key,
        }
        card.update(self.mismatches)
        return {"task": card, "parents": list(intake.envelope.parents)}

    @property
    def last_intake(self) -> Any:
        return self._last_intake

    @last_intake.setter
    def last_intake(self, value: Any) -> None:
        self._last_intake = value

    def query_events(self, *, after_id: int, limit: int) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["id"] > after_id][:limit]

    def latest_event_id(self) -> int:
        return max((row["id"] for row in self.rows), default=0)


class FakeBuzz:
    def __init__(self) -> None:
        self.receipts: dict[str, tuple[str, str]] = {}
        self.receipt_send_count = 0
        self.channel_messages: dict[str, tuple[str, str]] = {}
        self.messages: list[dict[str, Any]] = []

    def fetch_messages(self, *, since: int | None, limit: int) -> list[dict[str, Any]]:
        return self.messages[:limit]

    def ensure_thread_receipt(self, *, source_event_id: str, content: str) -> str:
        previous = self.receipts.get(source_event_id)
        if previous:
            event_id, previous_content = previous
            assert previous_content == content
            return event_id
        self.receipt_send_count += 1
        self.receipts[source_event_id] = (RECEIPT_EVENT, content)
        return RECEIPT_EVENT

    def ensure_channel_message(self, *, marker: str, content: str) -> str:
        previous = self.channel_messages.get(marker)
        if previous:
            event_id, previous_content = previous
            assert previous_content == content
            return event_id
        event_id = f"{len(self.channel_messages) + 1:064x}"
        self.channel_messages[marker] = (event_id, content)
        return event_id


def make_bridge(
    *,
    broker: FakeBroker | None = None,
    buzz: FakeBuzz | None = None,
    state: MemoryState | None = None,
) -> Any:
    broker = broker or FakeBroker()
    buzz = buzz or FakeBuzz()
    state = state or MemoryState()
    instance = bridge_module.BuzzKanbanBridge(
        channel=CHANNEL,
        owner_pubkey=OWNER,
        allowed_workspace_roots=[WORKSPACE_ROOT],
        allowed_specialists=frozenset({"orchestrator"}),
        max_age_seconds=900,
        future_skew_seconds=60,
        broker=broker,
        buzz=buzz,
        state=state,
        clock=lambda: NOW,
    )

    original_create = broker.create

    def remember_intake(intake: Any) -> str:
        broker.last_intake = intake
        return original_create(intake)

    broker.create = remember_intake
    return instance


def test_duplicate_delivery_creates_one_card_and_one_receipt() -> None:
    broker = FakeBroker()
    buzz = FakeBuzz()
    state = MemoryState()
    bridge = make_bridge(broker=broker, buzz=buzz, state=state)

    first = bridge.process_intake(raw_event())
    second = bridge.process_intake(raw_event())

    assert first.outcome == "accepted"
    assert first.card_id == CARD_ID
    assert first.receipt_event_id == RECEIPT_EVENT
    assert second.outcome == "duplicate"
    assert broker.create_calls == 1
    assert broker.read_calls == 1
    assert buzz.receipt_send_count == 1
    receipt = buzz.receipts[SOURCE_EVENT][1]
    assert "card_id: t_deadbeef" in receipt
    assert f"owner: {OWNER}" in receipt
    assert "status: todo" in receipt
    assert "broker read-back receipt" in receipt
    assert "reviewer: independent Grok security review" in receipt


def test_crash_after_receipt_does_not_duplicate_buzz_reply() -> None:
    broker = FakeBroker()
    buzz = FakeBuzz()

    first = make_bridge(broker=broker, buzz=buzz, state=MemoryState())
    assert first.process_intake(raw_event()).outcome == "accepted"

    recovered = make_bridge(broker=broker, buzz=buzz, state=MemoryState())
    assert recovered.process_intake(raw_event()).outcome == "accepted"

    assert (
        broker.create_calls == 2
    )  # the board idempotency key returns the same canonical card
    assert (
        buzz.receipt_send_count == 1
    )  # marker read-back suppresses a second thread reply


def test_malformed_envelope_is_rejected_without_side_effects() -> None:
    broker = FakeBroker()
    buzz = FakeBuzz()
    state = MemoryState()
    bridge = make_bridge(broker=broker, buzz=buzz, state=state)
    payload = envelope_payload()
    payload.pop("reviewer")

    result = bridge.process_intake(raw_event(payload=payload))

    assert result.outcome == "rejected"
    assert result.reason == "missing_fields"
    assert broker.create_calls == 0
    assert buzz.receipt_send_count == 0
    assert state.data["events"][SOURCE_EVENT]["reason"] == "missing_fields"


def test_duplicate_json_keys_are_rejected() -> None:
    event = raw_event()
    event["content"] = (
        "FLEET_KANBAN_INTAKE_V1\n"
        '{"schema":"buzz.kanban.intake/v1","schema":"buzz.kanban.intake/v1"}'
    )
    result = make_bridge().process_intake(event)
    assert result.outcome == "rejected"
    assert result.reason == "duplicate_json_key"


def test_unauthorized_sender_is_silent_and_never_touches_broker() -> None:
    broker = FakeBroker()
    buzz = FakeBuzz()
    bridge = make_bridge(broker=broker, buzz=buzz)

    result = bridge.process_intake(raw_event(pubkey="c" * 64))

    assert result.outcome == "rejected"
    assert result.reason == "unauthorized_sender"
    assert broker.create_calls == 0
    assert buzz.receipt_send_count == 0


def test_stale_event_is_rejected_before_parsing_or_broker_write() -> None:
    broker = FakeBroker()
    bridge = make_bridge(broker=broker)
    result = bridge.process_intake(raw_event(created_at=NOW - 901))
    assert result.reason == "stale_event"
    assert broker.create_calls == 0


def test_broker_failure_leaves_event_retryable_and_sends_no_receipt() -> None:
    broker = FakeBroker(fail=True)
    buzz = FakeBuzz()
    state = MemoryState()
    bridge = make_bridge(broker=broker, buzz=buzz, state=state)

    with pytest.raises(bridge_module.RetryableBridgeError, match="broker unavailable"):
        bridge.process_intake(raw_event())

    assert SOURCE_EVENT not in state.data["events"]
    assert buzz.receipt_send_count == 0


def test_readback_mismatch_fails_closed_without_receipt() -> None:
    broker = FakeBroker(mismatches={"status": "ready"})
    buzz = FakeBuzz()
    state = MemoryState()
    bridge = make_bridge(broker=broker, buzz=buzz, state=state)

    with pytest.raises(bridge_module.RetryableBridgeError) as excinfo:
        bridge.process_intake(raw_event())

    assert excinfo.value.code == "broker_readback_mismatch"
    assert SOURCE_EVENT not in state.data["events"]
    assert buzz.receipt_send_count == 0


def test_high_risk_requires_real_approval_gate() -> None:
    result = make_bridge().process_intake(
        raw_event(payload=envelope_payload(risk="high", approval_gate="none"))
    )
    assert result.outcome == "rejected"
    assert result.reason == "approval_gate_required"


def test_workspace_outside_allowlist_is_rejected() -> None:
    result = make_bridge().process_intake(
        raw_event(payload=envelope_payload(workspace="dir:/etc/fleet"))
    )
    assert result.outcome == "rejected"
    assert result.reason == "workspace_not_allowlisted"


def test_unknown_specialist_and_provider_routing_fields_are_rejected() -> None:
    unsupported = make_bridge().process_intake(
        raw_event(payload=envelope_payload(requested_specialist="unknown-profile"))
    )
    assert unsupported.reason == "unsupported_specialist"

    with_provider = envelope_payload()
    with_provider["provider"] = "nous"
    spoofed = make_bridge().process_intake(raw_event(payload=with_provider))
    assert spoofed.reason == "unknown_fields"


def test_secret_like_envelope_value_is_rejected_before_card_creation() -> None:
    broker = FakeBroker()
    result = make_bridge(broker=broker).process_intake(
        raw_event(
            payload=envelope_payload(
                objective="Use token=sk-abcdefghijklmnopqrstuvwxyz123456 for this task."
            )
        )
    )
    assert result.outcome == "rejected"
    assert result.reason == "secret_material_rejected"
    assert broker.create_calls == 0


def test_hermes_create_is_broker_pinned_parked_and_has_no_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intake = bridge_module.validate_intake_event(
        raw_event(),
        channel=CHANNEL,
        owner_pubkey=OWNER,
        allowed_workspace_roots=[WORKSPACE_ROOT],
        allowed_specialists=frozenset({"orchestrator"}),
        allowed_kinds={9},
        now=NOW,
        max_age_seconds=900,
        future_skew_seconds=60,
    )
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return bridge_module.subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"id": CARD_ID}),
            stderr="",
        )

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_should_not_leak")
    monkeypatch.setattr(bridge_module.subprocess, "run", fake_run)
    broker = bridge_module.HermesBroker(
        hermes_bin="/usr/local/bin/hermes",
        board="fleet",
        board_socket=Path("/run/hermes/fleet-boardd.sock"),
    )

    assert broker.create(intake) == CARD_ID
    argv = captured["argv"]
    env = captured["env"]
    assert argv[:5] == [
        "/usr/local/bin/hermes",
        "kanban",
        "--board",
        "fleet",
        "create",
    ]
    assert argv[argv.index("--initial-status") + 1] == "todo"
    assert argv[argv.index("--idempotency-key") + 1] == "buzz-intake-test-v1"
    assert "--model" not in argv
    assert "--provider" not in argv
    assert env["HERMES_KANBAN_BROKER"] == "1"
    assert env["BOARDD_SOCK"] == "/run/hermes/fleet-boardd.sock"
    assert "HERMES_KANBAN_TASK" not in env


def test_buzz_receipt_is_sent_once_and_verified_by_thread_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = bridge_module.BuzzClient(
        buzz_bin="/usr/local/bin/buzz",
        channel=CHANNEL,
        bridge_pubkey=BRIDGE,
    )
    content = "[buzz-kanban-receipt:v1:source]\ncard_id: t_deadbeef"
    sent = False
    thread_reads = 0

    def fake_thread(source_event_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        nonlocal thread_reads
        assert source_event_id == SOURCE_EVENT
        assert limit == 200
        thread_reads += 1
        if not sent:
            # An owner-authored spoof of the marker must not suppress the real receipt.
            return [{"id": "c" * 64, "pubkey": OWNER, "content": content}]
        return [{"id": RECEIPT_EVENT, "pubkey": BRIDGE, "content": content}]

    def fake_run(args: tuple[str, ...], *, input_text: str | None = None) -> Any:
        nonlocal sent
        assert args[-2:] == ("--content", "-")
        assert "--reply-to" in args
        assert input_text == content
        sent = True
        return {"id": RECEIPT_EVENT}

    monkeypatch.setattr(client, "_thread_messages", fake_thread)
    monkeypatch.setattr(client, "_run", fake_run)

    assert (
        client.ensure_thread_receipt(source_event_id=SOURCE_EVENT, content=content)
        == RECEIPT_EVENT
    )
    assert sent is True
    assert thread_reads == 2


def test_high_signal_filter_advances_durable_cursor_without_ticker_noise() -> None:
    broker = FakeBroker()
    broker.rows = [
        {
            "id": 1,
            "task_id": "t_00000001",
            "kind": "assigned",
            "payload": json.dumps({"assignee": "security"}),
            "created_at": NOW,
            "title": "Audit bridge",
            "assignee": "security",
            "status": "todo",
        },
        {
            "id": 2,
            "task_id": "t_00000001",
            "kind": "heartbeat",
            "payload": json.dumps({"note": "50%"}),
            "created_at": NOW,
            "title": "Audit bridge",
            "assignee": "security",
            "status": "running",
        },
        {
            "id": 3,
            "task_id": "t_00000002",
            "kind": "blocked",
            "payload": json.dumps({"reason": "operator approval required"}),
            "created_at": NOW,
            "title": "Enable bridge",
            "assignee": "orchestrator",
            "status": "blocked",
        },
        {
            "id": 4,
            "task_id": "t_00000003",
            "kind": "completed",
            "payload": json.dumps({"summary": "exact-head verification passed"}),
            "created_at": NOW,
            "title": "Verify bridge",
            "assignee": "grok1",
            "status": "done",
        },
    ]
    buzz = FakeBuzz()
    state = MemoryState()
    state.data["board_event_initialized"] = True
    bridge = make_bridge(broker=broker, buzz=buzz, state=state)

    emitted = bridge.poll_board_once(limit=100)

    assert emitted == [1, 3, 4]
    assert state.data["board_event_cursor"] == 4
    assert state.save_count == 4
    assert len(buzz.channel_messages) == 3
    contents = "\n".join(content for _, content in buzz.channel_messages.values())
    assert "event: assignment" in contents
    assert "event: blocker" in contents
    assert "event: completion" in contents
    assert "50%" not in contents


def test_high_signal_detail_redacts_secret_like_values() -> None:
    content = bridge_module.render_high_signal_event(
        {
            "task_id": "t_00000004",
            "kind": "blocked",
            "payload": json.dumps({
                "reason": "operator pasted token=abcdefghijklmnopqrstuvwxyz123456"
            }),
            "title": "Redaction check",
            "assignee": "security",
            "status": "blocked",
        },
        marker="[kanban-event:v1:5]",
    )
    assert "abcdefghijklmnopqrstuvwxyz123456" not in content
    assert "[REDACTED SECRET ASSIGNMENT]" in content


def test_first_outbound_poll_bootstraps_at_live_frontier_without_flooding() -> None:
    broker = FakeBroker()
    broker.rows = [
        {
            "id": 99,
            "task_id": "t_00000001",
            "kind": "completed",
            "payload": "{}",
            "created_at": NOW,
            "title": "Historical task",
            "assignee": "engineer",
            "status": "done",
        }
    ]
    buzz = FakeBuzz()
    state = MemoryState()
    bridge = make_bridge(broker=broker, buzz=buzz, state=state)

    assert bridge.poll_board_once(limit=100) == []
    assert state.data["board_event_initialized"] is True
    assert state.data["board_event_cursor"] == 99
    assert buzz.channel_messages == {}


def test_state_file_is_atomic_and_private(tmp_path: Path) -> None:
    path = tmp_path / "bridge-state.json"
    state = bridge_module.StateStore(path)
    state.data["buzz_cursor"] = 42
    state.save()

    assert path.stat().st_mode & 0o077 == 0
    assert bridge_module.StateStore(path).data["buzz_cursor"] == 42

    path.chmod(0o644)
    with pytest.raises(bridge_module.BridgeError) as excinfo:
        bridge_module.StateStore(path)
    assert excinfo.value.code == "unsafe_state_permissions"
