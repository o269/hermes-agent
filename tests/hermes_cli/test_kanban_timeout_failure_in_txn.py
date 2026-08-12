"""Timeout reclaim and failure accounting share one write_txn."""

from __future__ import annotations

import time
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


def test_timeout_does_not_call_outer_record_task_failure(kanban_home, monkeypatch):
    """Regression: a second write_txn was the PR37 successor-claim race."""

    def _boom(*_a, **_k):
        raise AssertionError("timeout path must use _apply_task_failure_in_txn")

    monkeypatch.setattr(kb, "_record_task_failure", _boom)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="timeout", assignee="alice", max_runtime_seconds=1
        )
        assert kb.claim_task(conn, task_id) is not None
        kb._set_worker_pid(conn, task_id, 4242)
        old_started = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at=? WHERE id=?",
                (old_started, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET started_at=? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id=?)",
                (old_started, task_id),
            )
        timed = kb.enforce_max_runtime(conn, signal_fn=lambda *_a, **_k: None)
        assert task_id in timed
        row = conn.execute(
            "SELECT status, consecutive_failures FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    assert int(row["consecutive_failures"]) >= 1
    assert row["status"] in {"ready", "blocked"}
