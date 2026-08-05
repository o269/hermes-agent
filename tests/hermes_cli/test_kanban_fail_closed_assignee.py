"""Fail-closed dispatch on unresolvable / default assignees.

Regression for fleet incident t_534efda8 and operator ruling 2026-08-05:
``assignee=default`` (and any missing named profile) must NEVER spawn a
metered base-config worker. Disposition is skipped/nonspawnable_assignee
with a compact audit event; resolvable named profiles are unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def empty_worker_snapshot(monkeypatch):
    monkeypatch.setattr(kb, "_snapshot_worker_processes", lambda **_kwargs: [])


def _fake_spawn(*_args, **_kwargs):
    raise AssertionError("spawn must not run for unresolvable/default assignees")


def _dispositions(conn, task_id):
    return [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == "dispatch_disposition"
    ]


def test_default_assignee_is_nonspawnable_and_never_spawns(kanban_home):
    """assignee=default must skip with nonspawnable_assignee — no spawn."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="parked on default", assignee="default")
        result = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

        events = _dispositions(conn, task_id)
        row = conn.execute(
            "SELECT status, claim_lock FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert task_id in result.skipped_nonspawnable
    assert result.spawned == []
    assert kb._assignee_has_spawn_target("default") is False
    assert kb._assignee_has_spawn_target("Default") is False
    assert len(result.dispositions) == 1
    entry = result.dispositions[0]
    assert (entry.outcome, entry.reason) == ("skipped", "nonspawnable_assignee")
    assert entry.detail.get("assignee") == "default"
    assert len(events) == 1
    assert events[0].payload == {
        "outcome": "skipped",
        "reason": "nonspawnable_assignee",
        "detail": {"assignee": "default"},
    }
    assert row["status"] == "ready"
    assert row["claim_lock"] is None


def test_unknown_named_assignee_is_nonspawnable_and_never_spawns(kanban_home):
    """Missing profiles/<name> must skip — never fall through to base config."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="typo lane", assignee="no-such-profile-xyz"
        )
        result = kb.dispatch_once(conn, spawn_fn=_fake_spawn)
        events = _dispositions(conn, task_id)

    assert task_id in result.skipped_nonspawnable
    assert result.spawned == []
    assert kb._assignee_has_spawn_target("no-such-profile-xyz") is False
    assert (result.dispositions[0].outcome, result.dispositions[0].reason) == (
        "skipped",
        "nonspawnable_assignee",
    )
    assert result.dispositions[0].detail.get("assignee") == "no-such-profile-xyz"
    assert len(events) == 1
    assert events[0].payload["reason"] == "nonspawnable_assignee"


def test_resolvable_named_assignee_still_spawns(kanban_home):
    """A real profiles/<name> directory remains spawnable (unchanged semantics)."""
    profile_dir = kanban_home / "profiles" / "worker"
    profile_dir.mkdir(parents=True)
    spawns: list[tuple[str, str]] = []

    def spawn(task, workspace):
        spawns.append((task.id, task.assignee))
        return 424242

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="real lane", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        events = _dispositions(conn, task_id)
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert kb._assignee_has_spawn_target("worker") is True
    assert result.spawned and result.spawned[0][0] == task_id
    assert spawns == [(task_id, "worker")]
    assert result.skipped_nonspawnable == []
    assert any(
        e.payload.get("outcome") == "spawned" for e in events
    ) or row["status"] == "running"
    assert row["status"] == "running"


def test_kanban_default_assignee_default_does_not_auto_assign_or_spawn(kanban_home):
    """kanban.default_assignee=default must not mint a metered worker."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="unassigned", assignee=None)
        result = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            default_assignee="default",
        )
        row = conn.execute(
            "SELECT assignee, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert task_id in result.skipped_unassigned
    assert result.auto_assigned_default == []
    assert result.spawned == []
    assert row["assignee"] is None
    assert row["status"] == "ready"


def test_kanban_default_assignee_named_profile_still_auto_assigns(kanban_home):
    """Named default_assignee with a real profile still auto-assigns + spawns."""
    (kanban_home / "profiles" / "worker").mkdir(parents=True)
    spawns: list[tuple[str, str]] = []

    def spawn(task, _workspace):
        spawns.append((task.id, task.assignee))
        return 99

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="unassigned", assignee=None)
        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn,
            default_assignee="worker",
        )
        row = conn.execute(
            "SELECT assignee, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert result.auto_assigned_default == [task_id]
    assert result.skipped_unassigned == []
    assert spawns == [(task_id, "worker")]
    assert row["assignee"] == "worker"
    assert row["status"] == "running"


def test_default_spawn_refuses_default_assignee(kanban_home, monkeypatch):
    """Direct _default_spawn path also fails closed (no subprocess)."""
    import subprocess

    def boom(*_a, **_k):
        raise AssertionError("Popen must not run for default assignee")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="direct", assignee="default")
        task = kb.get_task(conn, task_id)

    with pytest.raises(ValueError, match="no spawn target"):
        kb._default_spawn(task, str(kanban_home / "ws"))


def test_has_spawnable_ready_false_for_default_only_queue(kanban_home):
    with kb.connect() as conn:
        kb.create_task(conn, title="parked", assignee="default")
        assert kb.has_spawnable_ready(conn) is False


def test_disposition_payload_is_compact_json_serializable(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="audit", assignee="default")
        kb.dispatch_once(conn, spawn_fn=_fake_spawn)
        events = _dispositions(conn, task_id)

    payload = events[0].payload
    encoded = json.dumps(payload)
    assert "default" in encoded
    assert "nonspawnable_assignee" in encoded
    # No secret-bearing keys.
    assert "OPENROUTER" not in encoded
    assert "api_key" not in encoded.lower()
