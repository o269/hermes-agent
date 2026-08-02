"""Broker-path regressions for the canonical per-profile claim authority."""
from __future__ import annotations

import sqlite3
import threading

from hermes_cli import boardd_shim
from hermes_cli import kanban_db as kb
from hermes_cli.kb_client import Client


class _ScratchBrokerClient(Client):
    """Policy test broker backed by one real SQLite connection per client.

    It implements only the interactive transaction surface used by
    ``BrokerConnection``. Any fallback to boardd's legacy raw ``claim`` request
    fails loudly, which keeps this regression independent of a host-installed
    boardd script while exercising the real claim transaction.
    """

    def __init__(self, db_path):
        super().__init__(sock_path="scratch-broker-unused")
        self._db = sqlite3.connect(
            str(db_path), timeout=10, check_same_thread=False, isolation_level=None
        )
        self._db.row_factory = sqlite3.Row

    def _request(self, op, args=None, *, mutation, op_id=None):
        raise AssertionError(f"legacy broker request must not be used: {op}")

    def query(self, sql, params=None, max_rows=None):
        del max_rows
        cur = self._db.execute(sql, params or [])
        return [dict(row) for row in cur.fetchall()]

    def exec_write(self, sql, params=None, *, op_id=None):
        del op_id
        cur = self._db.execute(sql, params or [])
        return {"rowcount": cur.rowcount, "lastrowid": cur.lastrowid}

    def txn_begin(self):
        self._db.execute("BEGIN IMMEDIATE")
        return "scratch-txn"

    def txn_exec(self, txn, sql, params=None):
        assert txn == "scratch-txn"
        cur = self._db.execute(sql, params or [])
        rows = [dict(row) for row in cur.fetchall()] if cur.description else []
        return {
            "rows": rows,
            "rowcount": cur.rowcount,
            "lastrowid": cur.lastrowid,
        }

    def txn_commit(self, txn):
        assert txn == "scratch-txn"
        self._db.execute("COMMIT")
        return {"ok": True}

    def txn_rollback(self, txn):
        assert txn == "scratch-txn"
        self._db.execute("ROLLBACK")
        return {"ok": True}

    def shutdown(self):
        self._db.close()


def _init_tasks(db_path, *assignees):
    kb.init_db(db_path=db_path)
    with kb.connect_closing(db_path=db_path) as conn:
        return [
            kb.create_task(conn, title=f"candidate {index}", assignee=assignee)
            for index, assignee in enumerate(assignees)
        ]


def test_raw_client_claim_uses_canonical_profile_fence_and_exact_lock(tmp_path):
    db_path = tmp_path / "scratch-kanban.db"
    first, second = _init_tasks(db_path, "alpha", "alpha")
    client = _ScratchBrokerClient(db_path)
    try:
        won = client.claim(first, claimer="blitz:vps2-ssh:alpha:101", ttl_seconds=60)
        lost = client.claim(second, claimer="blitz:vps2-ssh:alpha:202", ttl_seconds=60)
        rows = client.query(
            "SELECT id, status, claim_lock, current_run_id FROM tasks "
            "WHERE id IN (?, ?) ORDER BY id",
            [first, second],
        )
    finally:
        client.shutdown()

    assert won["won"] is True
    assert won["task_id"] == first
    assert won["claim_lock"] == "blitz:vps2-ssh:alpha:101"
    assert lost == {"won": False, "task_id": second}
    assert [row["status"] for row in rows].count("running") == 1
    assert [row["status"] for row in rows].count("ready") == 1
    assert sum(row["current_run_id"] is not None for row in rows) == 1


def test_raw_client_claim_releases_only_after_reclaim(tmp_path):
    db_path = tmp_path / "scratch-release.db"
    first, second = _init_tasks(db_path, "alpha", "alpha")
    client = _ScratchBrokerClient(db_path)
    try:
        assert client.claim(first, claimer="ssh:first")["won"] is True
        assert client.claim(second, claimer="ssh:second")["won"] is False
        with kb.connect_closing(db_path=db_path) as conn:
            assert kb.reclaim_task(conn, first, reason="test handoff") is True
        assert client.claim(second, claimer="ssh:second")["won"] is True
    finally:
        client.shutdown()


def test_raw_client_claim_allows_distinct_profiles(tmp_path):
    db_path = tmp_path / "scratch-distinct-profiles.db"
    alpha, beta = _init_tasks(db_path, "alpha", "beta")
    client = _ScratchBrokerClient(db_path)
    try:
        assert client.claim(alpha, claimer="ssh:alpha")["won"] is True
        assert client.claim(beta, claimer="ssh:beta")["won"] is True
        rows = client.query(
            "SELECT status, current_run_id FROM tasks WHERE id IN (?, ?)",
            [alpha, beta],
        )
    finally:
        client.shutdown()

    assert [row["status"] for row in rows] == ["running", "running"]
    assert all(row["current_run_id"] is not None for row in rows)


def test_raw_and_python_claim_race_cannot_duplicate_running_profile(tmp_path):
    db_path = tmp_path / "scratch-race.db"
    kb.init_db(db_path=db_path)

    for iteration in range(20):
        profile = f"race-{iteration}"
        with kb.connect_closing(db_path=db_path) as conn:
            raw_task = kb.create_task(
                conn, title=f"raw {iteration}", assignee=profile
            )
            python_task = kb.create_task(
                conn, title=f"python {iteration}", assignee=profile
            )

        barrier = threading.Barrier(2)
        outcomes = []
        errors = []

        def raw_claim():
            client = _ScratchBrokerClient(db_path)
            try:
                barrier.wait(timeout=10)
                outcomes.append(client.claim(raw_task, claimer=f"raw:{iteration}")["won"])
            except BaseException as exc:
                errors.append(exc)
            finally:
                client.shutdown()

        def python_claim():
            client = _ScratchBrokerClient(db_path)
            conn = boardd_shim.BrokerConnection(client=client)
            try:
                barrier.wait(timeout=10)
                outcomes.append(
                    kb.claim_task(conn, python_task) is not None  # type: ignore[arg-type]
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()
                client.shutdown()

        callers = [threading.Thread(target=raw_claim), threading.Thread(target=python_claim)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=20)

        assert not [caller for caller in callers if caller.is_alive()]
        assert errors == []
        assert sorted(outcomes) == [False, True]
        with kb.connect_closing(db_path=db_path) as conn:
            rows = conn.execute(
                "SELECT status, current_run_id FROM tasks WHERE id IN (?, ?)",
                (raw_task, python_task),
            ).fetchall()
            run_count = conn.execute(
                "SELECT COUNT(*) FROM task_runs WHERE task_id IN (?, ?)",
                (raw_task, python_task),
            ).fetchone()[0]
        assert [row["status"] for row in rows].count("running") == 1
        assert [row["status"] for row in rows].count("ready") == 1
        assert run_count == 1
