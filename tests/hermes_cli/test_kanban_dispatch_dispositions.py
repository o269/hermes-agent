"""Ready-snapshot disposition invariants for the Kanban dispatcher."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty Kanban database."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _ready_ids(conn):
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM tasks WHERE status = 'ready' "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()
    ]


def _outcomes(result):
    return {
        entry.task_id: (entry.outcome, entry.reason, dict(entry.detail))
        for entry in result.dispositions
    }


def test_ready_snapshot_has_one_priority_ordered_disposition_per_card(
    kanban_home, monkeypatch
):
    """Mixed guards/skips/spawns cannot omit or duplicate a Ready row."""
    monkeypatch.setattr(
        kb,
        "_assignee_has_spawn_target",
        lambda assignee: assignee != "terminal",
    )
    with kb.connect() as conn:
        spawn_a = kb.create_task(conn, title="spawn a", assignee="alpha", priority=100)
        cap_a = kb.create_task(conn, title="cap a", assignee="alpha", priority=90)
        unassigned = kb.create_task(conn, title="unassigned", priority=80)
        terminal = kb.create_task(
            conn, title="terminal", assignee="terminal", priority=70
        )
        active_pr = kb.create_task(
            conn, title="active pr", assignee="beta", priority=60
        )
        pr_url = "https://github.com/o269/hermes-agent/pull/99999"
        kb.add_comment(conn, active_pr, "beta", f"AUTHOR COMPLETE: {pr_url}")
        spawn_c = kb.create_task(conn, title="spawn c", assignee="charlie", priority=50)
        budget_d = kb.create_task(conn, title="budget d", assignee="delta", priority=40)
        ready_ids = _ready_ids(conn)

        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=2,
            max_in_progress_per_profile=1,
        )

    assert ready_ids == [
        spawn_a,
        cap_a,
        unassigned,
        terminal,
        active_pr,
        spawn_c,
        budget_d,
    ]
    disposition_ids = [entry.task_id for entry in result.dispositions]
    assert disposition_ids == ready_ids
    assert len(disposition_ids) == len(set(disposition_ids))
    outcomes = _outcomes(result)
    assert outcomes[spawn_a][:2] == ("spawned", None)
    assert outcomes[cap_a][:2] == ("held", "per_profile_cap")
    assert outcomes[unassigned][:2] == ("skipped", "unassigned")
    assert outcomes[terminal][:2] == ("skipped", "nonspawnable_assignee")
    assert outcomes[active_pr][:2] == ("held", "active_pr")
    assert outcomes[active_pr][2]["pr_url"] == pr_url
    assert outcomes[spawn_c][:2] == ("spawned", None)
    assert outcomes[budget_d][:2] == ("held", "max_spawn")


def test_global_cap_holds_every_ready_card_and_persists_events(
    kanban_home, all_assignees_spawnable
):
    """The old max_in_progress early return now accounts for every row."""
    with kb.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="runner")
        assert kb.claim_task(conn, running) is not None
        kb._set_worker_pid(conn, running, os.getpid())
        first = kb.create_task(conn, title="first", assignee="alpha", priority=20)
        second = kb.create_task(conn, title="second", assignee="beta", priority=10)
        ready_ids = _ready_ids(conn)

        result = kb.dispatch_once(
            conn,
            max_in_progress=1,
        )

        persisted = {
            task_id: [
                event
                for event in kb.list_events(conn, task_id)
                if event.kind == "dispatch_disposition"
            ]
            for task_id in ready_ids
        }

    assert ready_ids == [first, second]
    assert [entry.task_id for entry in result.dispositions] == ready_ids
    assert all(
        (entry.outcome, entry.reason) == ("held", "max_in_progress")
        for entry in result.dispositions
    )
    assert all(len(events) == 1 for events in persisted.values())
    assert all(
        events[0].payload
        == {
            "outcome": "held",
            "reason": "max_in_progress",
            "detail": {"current": 1, "limit": 1},
        }
        for events in persisted.values()
    )


@pytest.mark.parametrize(
    ("failure_stage", "expected_reason"),
    [("workspace", "workspace_failure"), ("spawn", "spawn_failure")],
)
def test_ready_failure_paths_emit_compact_secret_free_dispositions(
    kanban_home,
    all_assignees_spawnable,
    monkeypatch,
    failure_stage,
    expected_reason,
):
    """Workspace and spawn exceptions are terminal for this tick, not silent."""
    secret = "TOP-SECRET-CREDENTIAL"
    if failure_stage == "workspace":

        def fail_workspace(*_args, **_kwargs):
            raise RuntimeError(secret)

        monkeypatch.setattr(kb, "resolve_workspace", fail_workspace)

    def spawn(_task, _workspace):
        if failure_stage == "spawn":
            raise RuntimeError(secret)
        raise AssertionError("spawn must not run after workspace resolution fails")

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=failure_stage, assignee="alpha")
        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn,
            failure_limit=99,
        )
        events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "dispatch_disposition"
        ]

    assert [entry.task_id for entry in result.dispositions] == [task_id]
    entry = result.dispositions[0]
    assert (entry.outcome, entry.reason) == ("skipped", expected_reason)
    assert entry.detail["error_type"] == "RuntimeError"
    assert len(events) == 1
    assert secret not in json.dumps(events[0].payload)


def test_claim_rejection_race_has_explicit_skipped_disposition(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    monkeypatch.setattr(kb, "claim_task", lambda *_args, **_kwargs: None)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="claim race", assignee="alpha")
        result = kb.dispatch_once(conn)

    assert [entry.task_id for entry in result.dispositions] == [task_id]
    assert (result.dispositions[0].outcome, result.dispositions[0].reason) == (
        "skipped",
        "claim_rejected",
    )


def test_active_pr_claim_race_keeps_local_custody_detail(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A PR appearing between guard evaluation and claim is still diagnosable."""
    pr_url = "https://github.com/o269/hermes-agent/pull/99998"

    def race_claim(conn, task_id, **_kwargs):
        kb.add_comment(conn, task_id, "alpha", f"PR opened: {pr_url}")
        decision = kb.evaluate_respawn_guard(conn, task_id, process_snapshot=[])
        assert decision.reason == "active_pr"
        kb.record_respawn_guard_decision(conn, task_id, decision, phase="claim")
        return None

    monkeypatch.setattr(kb, "claim_task", race_claim)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="claim custody race", assignee="alpha")
        result = kb.dispatch_once(conn)

    assert [entry.task_id for entry in result.dispositions] == [task_id]
    entry = result.dispositions[0]
    assert (entry.outcome, entry.reason) == ("held", "active_pr")
    assert entry.detail["pr_url"] == pr_url
    assert (task_id, "active_pr") in result.respawn_guarded
