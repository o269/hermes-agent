"""Fail-closed dispatch for default and unresolvable assignees."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _must_not_spawn(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("spawn must not run for an unresolvable assignee")


@pytest.mark.parametrize("assignee", ["default", "Default", "missing-worker"])
def test_unresolvable_assignee_stays_ready_and_unclaimed(
    kanban_home: Path,
    assignee: str,
) -> None:
    """A refused target cannot acquire custody or create a worker run."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="fail closed", assignee=assignee)
        result = kb.dispatch_once(conn, spawn_fn=_must_not_spawn)
        row = conn.execute(
            "SELECT status, claim_lock, current_run_id, worker_pid "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert task_id in result.skipped_nonspawnable
    assert result.spawned == []
    assert dict(row) == {
        "status": "ready",
        "claim_lock": None,
        "current_run_id": None,
        "worker_pid": None,
    }


def test_named_profile_remains_spawnable(kanban_home: Path) -> None:
    (kanban_home / "profiles" / "worker").mkdir(parents=True)
    spawns: list[tuple[str, str | None]] = []

    def spawn(task: kb.Task, _workspace: str) -> int:
        spawns.append((task.id, task.assignee))
        return 424242

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="real worker",
            assignee="worker",
            body="Resource-Class: light",
        )
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert result.skipped_nonspawnable == []
    assert spawns == [(task_id, "worker")]
    assert row["status"] == "running"


def test_default_fallback_does_not_assign_or_spawn(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="unassigned", assignee=None)
        result = kb.dispatch_once(
            conn,
            spawn_fn=_must_not_spawn,
            default_assignee="default",
        )
        row = conn.execute(
            "SELECT assignee, status, claim_lock, current_run_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert task_id in result.skipped_unassigned
    assert result.auto_assigned_default == []
    assert result.spawned == []
    assert dict(row) == {
        "assignee": None,
        "status": "ready",
        "claim_lock": None,
        "current_run_id": None,
    }


def test_direct_default_spawn_refuses_before_subprocess(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", _must_not_spawn)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="direct", assignee="default")
        task = kb.get_task(conn, task_id)

    assert task is not None
    with pytest.raises(ValueError, match="no spawn target"):
        kb._default_spawn(task, str(kanban_home / "workspace"))
