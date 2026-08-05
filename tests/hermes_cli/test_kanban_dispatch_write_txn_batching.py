"""Option-2 batching: dispatch write_txn scopes stay per-task.

boardd caps interactive write_txns at TXN_MAX_S=2.0s. vps2-dispatch reaches
the fleet board over a reverse-SSH tunnel, so a single multi-card write_txn
(scan/promote or multi-crash reclaim) exceeds the cap even on Spawned:0
ticks. These tests pin the contract that recompute_ready and
detect_crashed_workers open one short write_txn per mutated task — never one
write_txn spanning the whole candidate set.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _count_write_txns(fn, *args, **kwargs):
    """Run ``fn`` while counting entries into ``kanban_db.write_txn``."""
    real_write_txn = kb.write_txn
    entries = {"n": 0}

    def counting_write_txn(conn):
        entries["n"] += 1
        return real_write_txn(conn)

    with mock.patch.object(kb, "write_txn", side_effect=counting_write_txn):
        result = fn(*args, **kwargs)
    return result, entries["n"]


def test_recompute_ready_opens_one_write_txn_per_promotion(kanban_home):
    """N independent promotions must open N write_txns, not 1 mega-txn."""
    conn = kb.connect()
    try:
        n = 8
        child_ids = []
        parent_ids = []
        for i in range(n):
            parent = kb.create_task(conn, title=f"parent-{i}")
            child = kb.create_task(
                conn, title=f"child-{i}", parents=[parent], assignee="worker",
            )
            parent_ids.append(parent)
            child_ids.append(child)

        # Complete every parent first. complete_task runs recompute_ready
        # internally, so children land in ready — demote them ALL after the
        # last complete so the measured recompute_ready is the sole promoter.
        for parent in parent_ids:
            kb.complete_task(conn, parent, result="ok")
        with kb.write_txn(conn):
            for cid in child_ids:
                conn.execute(
                    "UPDATE tasks SET status = 'todo' WHERE id = ?",
                    (cid,),
                )

        for cid in child_ids:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (cid,),
            ).fetchone()
            assert row["status"] == "todo"

        promoted, txn_count = _count_write_txns(kb.recompute_ready, conn)
        assert promoted == n
        # Exactly one write_txn per successful promotion (no outer shell txn).
        assert txn_count == n, (
            f"expected {n} per-task write_txns, got {txn_count} "
            "(mega-txn batching regresses boardd TXN_MAX_S over tunnel)"
        )
        for cid in child_ids:
            task = kb.get_task(conn, cid)
            assert task is not None and task.status == "ready"
    finally:
        conn.close()


def test_recompute_ready_zero_promotions_opens_zero_write_txns(kanban_home):
    """Spawned:0 / promote-only-noop path must not open a write_txn at all."""
    conn = kb.connect()
    try:
        # Parent still open → child stays todo; nothing to promote.
        parent = kb.create_task(conn, title="open-parent")
        kb.create_task(conn, title="held-child", parents=[parent])
        promoted, txn_count = _count_write_txns(kb.recompute_ready, conn)
        assert promoted == 0
        assert txn_count == 0, (
            f"noop recompute_ready opened {txn_count} write_txn(s); "
            "scan must be autocommit-only when nothing promotes"
        )
    finally:
        conn.close()


def test_detect_crashed_workers_opens_one_write_txn_per_reclaim(
    kanban_home, monkeypatch,
):
    """N crashed host-local workers → N reclaim write_txns, not 1."""
    conn = kb.connect()
    try:
        # Force host-local claimer prefix so detect_crashed_workers considers us.
        monkeypatch.setattr(kb, "_claimer_id", lambda: "testhost:1")
        monkeypatch.setattr(kb, "_recorded_worker_alive", lambda *a, **k: False)
        monkeypatch.setattr(
            kb, "_classify_worker_exit", lambda pid: ("nonzero_exit", 1),
        )
        monkeypatch.setattr(kb, "_snapshot_worker_processes", lambda **k: [])
        monkeypatch.setattr(kb, "_owned_worker_processes", lambda *a, **k: [])
        monkeypatch.setattr(
            kb, "_connection_worker_board_identity", lambda conn: (None, None),
        )

        n = 5
        tids = []
        for i in range(n):
            # Distinct assignees avoid the per-profile running fence.
            tid = kb.create_task(
                conn, title=f"crash-{i}", assignee=f"worker-{i}",
            )
            claimed = kb.claim_task(conn, tid, claimer="testhost:1")
            assert claimed is not None, f"claim failed for {tid}"
            # Plant a fake dead worker_pid + current_run_id already set by claim.
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                    (90000 + i, tid),
                )
                run_id = conn.execute(
                    "SELECT current_run_id FROM tasks WHERE id = ?",
                    (tid,),
                ).fetchone()["current_run_id"]
                conn.execute(
                    "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                    (90000 + i, run_id),
                )
            tids.append(tid)

        crashed, txn_count = _count_write_txns(kb.detect_crashed_workers, conn)
        assert set(crashed) == set(tids)
        # One write_txn per reclaim. Failure accounting opens its own txn per
        # crash via _record_task_failure, so total is typically 2N. Must not
        # collapse to a single mega write_txn.
        assert txn_count >= n
        assert txn_count != 1, (
            "single mega write_txn for all crashes regresses tunnel TXN_MAX_S"
        )
        assert txn_count >= 2 * n or txn_count == n
        for tid in tids:
            task = kb.get_task(conn, tid)
            assert task is not None and task.status in ("ready", "blocked")
    finally:
        conn.close()
