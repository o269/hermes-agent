"""Mutation-sensitive claim-to-spawn custody tests.

A dispatcher claim is a spawn reservation, not permission to ignore later
custody changes. These fixtures revoke that reservation at deterministic points
around the external spawn side effect.
"""

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
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _name: True)
    kb.init_db()
    return home


def _disposition(result: kb.DispatchResult, task_id: str):
    return next(entry for entry in result.dispositions if entry.task_id == task_id)


def test_dispatch_refuses_spawn_when_claim_is_revoked_during_workspace_resolution(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A planted claim→workspace race must be detected before spawn_fn runs."""
    spawned: list[str] = []

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="pre-spawn race", assignee="worker")
        real_resolve = kb.resolve_workspace

        def racing_resolve(task: kb.Task, *, board=None):
            workspace = real_resolve(task, board=board)
            with kb.connect() as rival:
                assert kb.set_status(rival, task.id, "blocked") is True
            return workspace

        monkeypatch.setattr(kb, "resolve_workspace", racing_resolve)
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)

    assert spawned == []
    assert result.spawned == []
    entry = _disposition(result, task_id)
    assert (entry.outcome, entry.reason) == ("skipped", "claim_lost_before_spawn")
    assert entry.detail["assignee"] == "worker"
    assert task is not None
    assert (task.status, task.claim_lock, task.current_run_id, task.worker_pid) == (
        "blocked",
        None,
        None,
        None,
    )


def test_dispatch_terminates_child_when_claim_is_revoked_during_spawn(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If custody changes inside Popen, PID attachment must CAS-fail and reap."""
    fake_pid = 424242
    terminations: list[tuple[int | None, str | None]] = []

    def fake_terminate(pid, claim_lock, **_kwargs):
        terminations.append((pid, claim_lock))
        return {
            "host_local": True,
            "termination_attempted": True,
            "terminated": True,
            "sigkill": False,
        }

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", fake_terminate)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="in-spawn race", assignee="worker")

        def racing_spawn(task: kb.Task, _workspace: str) -> int:
            with kb.connect() as rival:
                assert kb.set_status(rival, task.id, "blocked") is True
            return fake_pid

        result = kb.dispatch_once(conn, spawn_fn=racing_spawn)
        task = kb.get_task(conn, task_id)

    assert result.spawned == []
    assert len(terminations) == 1
    assert terminations[0][0] == fake_pid
    entry = _disposition(result, task_id)
    assert (entry.outcome, entry.reason) == ("skipped", "claim_lost_during_spawn")
    assert entry.detail["child_terminated"] is True
    assert task is not None
    assert (task.status, task.claim_lock, task.current_run_id, task.worker_pid) == (
        "blocked",
        None,
        None,
        None,
    )


def test_dispatch_attaches_pid_when_claim_identity_is_unchanged(
    kanban_home: Path,
) -> None:
    """Positive control: the probe must not reject an intact reservation."""
    fake_pid = 31337

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="normal spawn", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=lambda _task, _workspace: fake_pid)
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)

    assert [item[0] for item in result.spawned] == [task_id]
    assert task is not None
    assert task.status == "running"
    assert task.worker_pid == fake_pid
    assert run is not None
    assert run.worker_pid == fake_pid
