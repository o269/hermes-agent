"""Crash/timeout failure accounting must preserve successor custody."""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_stale_failure_accounting_cannot_mutate_running_successor(
    kanban_home: Path,
) -> None:
    """Delayed predecessor accounting must not block a live claimed run."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="successor", assignee="worker")
        claimed = kb.claim_task(conn, task_id, claimer="successor:worker")
        assert claimed is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET consecutive_failures = ? WHERE id = ?",
                (kb.DEFAULT_FAILURE_LIMIT - 1, task_id),
            )
        before = conn.execute(
            "SELECT status, claim_lock, current_run_id, consecutive_failures "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

        tripped = kb._record_task_failure(
            conn,
            task_id,
            error="stale predecessor crash",
            outcome="crashed",
            release_claim=False,
            end_run=False,
        )
        after = conn.execute(
            "SELECT status, claim_lock, current_run_id, consecutive_failures "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert tripped is False
    assert dict(after) == dict(before)
    assert after["status"] == "running"
    assert after["claim_lock"] == "successor:worker"
    assert after["current_run_id"] is not None


def test_crash_reclaim_and_accounting_share_one_writer_transaction(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No claim window exists between predecessor reclaim and accounting."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("nonzero_exit", 1))
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="crashed", assignee="worker")
        claimed = kb.claim_task(conn, task_id, claimer=f"{host}:worker")
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 987654)

        real_write_txn = kb.write_txn
        writer_count = 0

        @contextlib.contextmanager
        def counted_write_txn(connection):
            nonlocal writer_count
            writer_count += 1
            with real_write_txn(connection) as transaction:
                yield transaction

        monkeypatch.setattr(kb, "write_txn", counted_write_txn)
        crashed = kb.detect_crashed_workers(conn)
        row = conn.execute(
            "SELECT status, claim_lock, current_run_id, consecutive_failures "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert crashed == [task_id]
    assert writer_count == 1
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    assert row["current_run_id"] is None
    assert row["consecutive_failures"] == 1
