"""Regression coverage for canonical manual status and dispatch custody."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kb_client


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Open an isolated local board; no test may touch the fleet broker DB."""
    monkeypatch.delenv("HERMES_KANBAN_BROKER", raising=False)
    monkeypatch.delenv("BOARDD_SOCK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as db:
        yield db


def test_running_to_ready_releases_custody_and_spawns_same_tick(
    conn, tmp_path, monkeypatch, all_assignees_spawnable,
):
    task_id = kb.create_task(
        conn,
        title="manual then queued",
        assignee="worker1",
        workspace_kind="dir",
        workspace_path=str(tmp_path),
    )
    assert kb.set_status(conn, task_id, "running") is True
    active = kb.get_task(conn, task_id)
    assert active is not None
    assert active.claim_lock == "seat-sticky"
    assert active.current_run_id is not None
    run_id = active.current_run_id

    assert kb.set_status(conn, task_id, "ready") is True
    queued = kb.get_task(conn, task_id)
    closed = kb.get_run(conn, run_id)
    assert queued is not None
    assert queued.status == "ready"
    assert queued.claim_lock is None
    assert queued.claim_expires is None
    assert queued.current_run_id is None
    assert queued.worker_pid is None
    assert closed is not None
    assert closed.outcome == "reclaimed"
    assert closed.ended_at is not None
    assert closed.claim_lock is None
    assert closed.claim_expires is None
    assert closed.worker_pid is None

    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _name: True)
    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *_args, **_kwargs: 4242,
    )
    assert task_id in [spawned_id for spawned_id, _, _ in result.spawned]
    respawned = kb.get_task(conn, task_id)
    assert respawned is not None
    assert respawned.status == "running"
    assert respawned.claim_lock != "seat-sticky"
    assert respawned.worker_pid == 4242


def test_reconciler_is_narrow_and_preserves_legitimate_running_claim(conn):
    stale_id = kb.create_task(conn, title="legacy invisible ready", assignee="worker1")
    assert kb.claim_task(conn, stale_id, claimer="seat-sticky") is not None
    stale_task = kb.get_task(conn, stale_id)
    assert stale_task is not None and stale_task.current_run_id is not None
    stale_run_id = stale_task.current_run_id
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ready', worker_pid = NULL WHERE id = ?",
            (stale_id,),
        )

    running_id = kb.create_task(conn, title="legitimate running", assignee="worker2")
    assert kb.claim_task(conn, running_id, claimer="dispatcher-live") is not None
    kb._set_worker_pid(conn, running_id, os.getpid())
    running_before = kb.get_task(conn, running_id)
    assert running_before is not None and running_before.current_run_id is not None
    running_run_id = running_before.current_run_id

    assert kb.repair_stale_manual_queue_custody(conn) == [stale_id]

    repaired = kb.get_task(conn, stale_id)
    repaired_run = kb.get_run(conn, stale_run_id)
    assert repaired is not None
    assert repaired.status == "ready"
    assert repaired.claim_lock is None
    assert repaired.claim_expires is None
    assert repaired.current_run_id is None
    assert repaired.worker_pid is None
    assert repaired_run is not None
    assert repaired_run.ended_at is not None
    assert repaired_run.outcome == "reclaimed"

    running_after = kb.get_task(conn, running_id)
    running_run = kb.get_run(conn, running_run_id)
    assert running_after is not None
    assert running_after.status == "running"
    assert running_after.claim_lock == "dispatcher-live"
    assert running_after.current_run_id == running_run_id
    assert running_after.worker_pid == os.getpid()
    assert running_run is not None
    assert running_run.ended_at is None
    assert running_run.claim_lock == "dispatcher-live"


class _BrokerRuntimeClient(kb_client.Client):
    """Exercise BrokerConnection transactions against an isolated SQLite board."""

    def __init__(self, conn, claim_task, heartbeat_worker):
        self.conn = conn
        self._claim_task = claim_task
        self._heartbeat_worker = heartbeat_worker
        self._txn_token = None
        self.transaction_count = 0

    @staticmethod
    def _result(cur):
        rows = [dict(row) for row in cur.fetchall()] if cur.description else []
        return {
            "rows": rows,
            "rowcount": cur.rowcount,
            "lastrowid": cur.lastrowid,
        }

    def query(self, sql, params=None, max_rows=None):
        del max_rows
        return [dict(row) for row in self.conn.execute(sql, params or []).fetchall()]

    def exec_write(self, sql, params=None, *, op_id=None):
        del op_id
        cur = self.conn.execute(sql, params or [])
        self.conn.commit()
        return {"rowcount": cur.rowcount, "lastrowid": cur.lastrowid}

    def txn_begin(self):
        assert self._txn_token is None
        self.conn.execute("BEGIN IMMEDIATE")
        self.transaction_count += 1
        self._txn_token = f"txn-{self.transaction_count}"
        return self._txn_token

    def txn_exec(self, txn, sql, params=None):
        assert txn == self._txn_token
        return self._result(self.conn.execute(sql, params or []))

    def txn_commit(self, txn):
        assert txn == self._txn_token
        self.conn.commit()
        self._txn_token = None
        return {"committed": True}

    def txn_rollback(self, txn):
        assert txn == self._txn_token
        self.conn.rollback()
        self._txn_token = None
        return {"rolled_back": True}

    def claim(self, task_id, *, claimer=None, ttl_seconds=7200, op_id=None):
        del op_id
        claimed = self._claim_task(
            self.conn,
            task_id,
            claimer=claimer,
            ttl_seconds=ttl_seconds,
        )
        return {
            "won": claimed is not None,
            "run_id": claimed.current_run_id if claimed is not None else None,
        }

    def heartbeat(self, task_id, note=None, *, op_id=None):
        del op_id
        return {
            "ok": self._heartbeat_worker(self.conn, task_id, note=note),
        }

    def get_task(self, task_id):
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def test_kb_client_set_status_uses_broker_connection_runtime(conn, monkeypatch):
    from hermes_cli import boardd_shim

    original_claim = kb.claim_task
    original_heartbeat = kb.heartbeat_worker
    original_names = {
        name: getattr(kb, name)
        for name in (*boardd_shim.REBIND_NAMES, "_check_file_length_invariant")
    }
    for name, original in original_names.items():
        monkeypatch.setattr(kb, name, original)
    for name in (
        "_KDB",
        "_ORIG_CONNECT",
        "_ORIG_CONNECT_CLOSING",
        "_ORIG_SET_STATUS",
    ):
        monkeypatch.setattr(boardd_shim, name, None, raising=False)
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", lambda *_a, **_k: None)
    boardd_shim.install_rebind(kb)

    client = _BrokerRuntimeClient(conn, original_claim, original_heartbeat)
    task_id = kb.create_task(
        conn,
        title="broker status lifecycle",
        assignee="worker1",
    )

    assert client.set_status(task_id, "running") == {
        "rowcount": 1,
        "status": "running",
    }
    running = client.get_task(task_id)
    assert running is not None
    run_id = running["current_run_id"]
    assert run_id is not None
    assert running["claim_lock"] == "seat-sticky"

    assert client.set_status(task_id, "ready") == {
        "rowcount": 1,
        "status": "ready",
    }
    assert client.query(
        "SELECT status, claim_lock, claim_expires, current_run_id, worker_pid "
        "FROM tasks WHERE id = ?",
        [task_id],
    ) == [{
        "status": "ready",
        "claim_lock": None,
        "claim_expires": None,
        "current_run_id": None,
        "worker_pid": None,
    }]
    closed_runs = client.query(
        "SELECT ended_at, outcome, claim_lock, claim_expires, worker_pid "
        "FROM task_runs WHERE id = ?",
        [run_id],
    )
    assert len(closed_runs) == 1
    assert closed_runs[0].pop("ended_at") is not None
    assert closed_runs == [{
        "outcome": "reclaimed",
        "claim_lock": None,
        "claim_expires": None,
        "worker_pid": None,
    }]
    assert client.transaction_count >= 2
