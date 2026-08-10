"""Option-2 batching: dispatch write_txn scopes stay per-task.

boardd caps interactive write_txns at TXN_MAX_S=2.0s. vps2-dispatch reaches
the fleet board over a reverse-SSH tunnel, so a single multi-card write_txn
(scan/promote or multi-crash reclaim) exceeds the cap even on Spawned:0
ticks. These tests pin the contract that recompute_ready and
detect_crashed_workers open one short write_txn per mutated task — never one
write_txn spanning the whole candidate set.
"""

from __future__ import annotations

import sqlite3
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
        # One write_txn per reclaim including in-txn failure accounting.
        # Must not collapse to a single mega write_txn across all crashes.
        assert txn_count == n, (
            f"expected {n} per-crash write_txns (reclaim+accounting), "
            f"got {txn_count}"
        )
        for tid in tids:
            task = kb.get_task(conn, tid)
            assert task is not None and task.status in ("ready", "blocked")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# False-success defense (review t_e036dad5): the CLI must NOT return 0 when a
# write_txn is rolled back by boardd's TXN_MAX_S cap. boardd signals the cap by
# force-rolling-back the txn and answering the client's subsequent COMMIT with
# TxnStale, which the shim translates to sqlite3.OperationalError. The dispatch
# tick catches that per-phase (so partial progress is still reported) and the
# CLI exits non-zero via DispatchResult.write_failures.
# ---------------------------------------------------------------------------


def _make_exploding_write_txn(real_write_txn, fail_on: int):
    """Return a write_txn replacement that raises OperationalError (TxnStale
    shape) on the Nth invocation, simulating a boardd TXN_MAX_S cap rollback
    that fails at COMMIT time."""
    import contextlib
    calls = {"n": 0}

    @contextlib.contextmanager
    def exploding(conn):
        calls["n"] += 1
        # Run the real BEGIN IMMEDIATE + body, but replace the COMMIT with
        # a raise on the target invocation. write_txn does BEGIN on enter,
        # yields, then COMMITs on clean exit. We enter the real context
        # manager so BEGIN runs, let the body execute, then raise BEFORE
        # the real __exit__ can COMMIT.
        #
        # We can't partially enter real_write_txn and stop its commit, so
        # instead we replicate the structure: BEGIN via the real conn, run
        # the body, then either COMMIT or raise.
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        if calls["n"] >= fail_on:
            # Simulate boardd cap: the txn was force-rolled-back, so the
            # client's COMMIT hits TxnStale.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise sqlite3.OperationalError(
                "no such open transaction (etype=TxnStale)"
            )
        conn.execute("COMMIT")

    exploding.calls = calls
    return exploding


def test_dispatch_records_write_failure_on_txn_rollback(kanban_home):
    """A rolled-back write_txn (TxnStale) must surface in write_failures,
    NOT be silently swallowed as a successful tick."""
    import sqlite3
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent-rollback")
        child = kb.create_task(
            conn, title="child-rollback", parents=[parent], assignee="worker",
        )
        kb.complete_task(conn, parent, result="ok")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ?", (child,),
            )

        real = kb.write_txn
        # Explode on the first write_txn COMMIT (the promote phase).
        exploding = _make_exploding_write_txn(real, fail_on=1)
        with mock.patch.object(kb, "write_txn", exploding):
            res = kb.dispatch_once(conn, dry_run=True, max_spawn=0)
        assert "promote" in res.write_failures, (
            "a rolled-back write_txn must be recorded in write_failures, "
            f"got {res.write_failures}"
        )
        # The child must NOT have been promoted (the write rolled back).
        task = kb.get_task(conn, child)
        assert task is not None and task.status == "todo", (
            "rolled-back promotion must not persist"
        )
    finally:
        conn.close()


def test_noop_dispatch_tick_without_rollback_exits_zero(kanban_home):
    """NEGATIVE CONTROL: a no-op tick (nothing to promote, no crashes) with
    NO rolled-back writes MUST exit zero and report no write_failures.

    This is the flip side of the false-success defense: the negative control
    proves the defense does not cry wolf on healthy ticks. A no-op worker
    (Spawned:0, nothing to reclaim/promote/crash) is exactly the vps2 steady
    state, and it must still report success when nothing was rolled back."""
    conn = kb.connect()
    try:
        # Empty board — nothing to reclaim, promote, or crash.
        res = kb.dispatch_once(conn, dry_run=True, max_spawn=0)
        assert res.write_failures == [], (
            "a healthy no-op tick must not record write_failures"
        )
        # The CLI exit code for a clean no-op tick must be 0.
        assert not res.write_failures  # _cmd_dispatch returns 0 when empty
    finally:
        conn.close()


def test_crash_accounting_not_surfaced_on_rolled_back_txn(
    kanban_home, monkeypatch,
):
    """EXACT-HEAD CRASH-ACCOUNTING RACE: detect_crashed_workers must NOT
    append a task id to its return list if the per-task write_txn was rolled
    back. Before the fix, crashed.append() ran inside the write_txn block, so
    a TxnStale on COMMIT left the id in the caller-facing list even though
    the DB still showed status='running'."""
    import sqlite3
    conn = kb.connect()
    try:
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

        tid = kb.create_task(conn, title="crash-race", assignee="worker-race")
        claimed = kb.claim_task(conn, tid, claimer="testhost:1")
        assert claimed is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (99999, tid),
            )
            run_id = conn.execute(
                "SELECT current_run_id FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()["current_run_id"]
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (99999, run_id),
            )

        real = kb.write_txn
        # Explode on the first COMMIT so the crash reclaim txn rolls back.
        exploding = _make_exploding_write_txn(real, fail_on=1)
        crashed: list[str] = []
        with mock.patch.object(kb, "write_txn", exploding):
            try:
                crashed = kb.detect_crashed_workers(conn)
            except sqlite3.OperationalError:
                pass  # standalone call propagates; dispatch_once catches it
        # The id must NOT be in the crashed list — the write rolled back,
        # so the task is still 'running' in the DB and reporting it as
        # crashed would be a false positive. (When called via dispatch_once
        # the per-phase catch records "crash" in write_failures instead.)
        assert tid not in crashed, (
            "crash-accounting race: id surfaced after rolled-back txn; "
            "the append must happen only after COMMIT succeeds"
        )
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "running", (
            "rolled-back crash reclaim must leave the task running"
        )
    finally:
        conn.close()


def test_crash_failure_accounting_does_not_mutate_successor_claim(
    kanban_home, monkeypatch,
):
    """PR37 FIX_REQUIRED race: deferred post-reclaim failure accounting must
    never block a newly claimed successor while leaving its claim/run open.

    Two-candidate, two-connection repro:
      - candidate 2 is classified/reclaimed first in the mutation order
      - between candidate-1 reclaim commit and any deferred accounting, a
        second connection claims candidate 1 as a successor attempt
      - final state must NOT be blocked+claimed with an open successor run
    """
    conn = kb.connect()
    try:
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

        # Two host-local running tasks that will both be crash candidates.
        # Use failure_limit-equivalent by planting consecutive_failures so the
        # next failure trips the breaker (DEFAULT_FAILURE_LIMIT is typically 2).
        tids = []
        for i in range(2):
            tid = kb.create_task(
                conn, title=f"race-{i}", assignee=f"worker-race-{i}",
            )
            claimed = kb.claim_task(conn, tid, claimer="testhost:1")
            assert claimed is not None
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET worker_pid = ?, consecutive_failures = ? "
                    "WHERE id = ?",
                    (91000 + i, max(0, kb.DEFAULT_FAILURE_LIMIT - 1), tid),
                )
                run_id = conn.execute(
                    "SELECT current_run_id FROM tasks WHERE id = ?",
                    (tid,),
                ).fetchone()["current_run_id"]
                conn.execute(
                    "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                    (91000 + i, run_id),
                )
            tids.append(tid)

        # Interleave a second connection's claim on the first candidate the
        # moment its reclaim write_txn commits, simulating a concurrent
        # dispatcher that picks up the reclaimed card before deferred
        # accounting would have run under the old two-phase design.
        real_write_txn = kb.write_txn
        claim_state = {"n": 0, "claimed": False, "claim_ok": False, "depth": 0}

        import contextlib

        @contextlib.contextmanager
        def interleaving_write_txn(c):
            # Re-entrancy guard: claim_task opens its own write_txn; those
            # nested calls must use the real helper or we recurse forever.
            claim_state["depth"] += 1
            try:
                with real_write_txn(c) as yielded:
                    yield yielded
            finally:
                claim_state["depth"] -= 1
            if claim_state["depth"] != 0 or claim_state["claimed"]:
                return
            claim_state["n"] += 1
            other = kb.connect()
            try:
                # Prefer whichever candidate is ready after this commit.
                for tid in tids:
                    row = other.execute(
                        "SELECT status FROM tasks WHERE id = ?",
                        (tid,),
                    ).fetchone()
                    if row is not None and row["status"] == "ready":
                        got = kb.claim_task(
                            other, tid, claimer="testhost:competitor",
                        )
                        claim_state["claimed"] = True
                        claim_state["claim_ok"] = got is not None
                        claim_state["claimed_tid"] = tid
                        break
            finally:
                other.close()

        with mock.patch.object(kb, "write_txn", interleaving_write_txn):
            crashed = kb.detect_crashed_workers(conn)

        # At least one crash was recorded; the other may have been claimed
        # as a successor before the loop finished.
        assert crashed, "expected at least one durable crash reclaim"

        # Invariant must hold for EVERY candidate.
        for tid in tids:
            task = kb.get_task(conn, tid)
            assert task is not None
            run = conn.execute(
                "SELECT id, status, outcome, ended_at, claim_lock "
                "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            blocked_with_live_claim_and_open_run = (
                task.status == "blocked"
                and task.claim_lock is not None
                and task.current_run_id is not None
                and run is not None
                and run["status"] == "running"
                and run["outcome"] is None
                and run["ended_at"] is None
            )
            assert not blocked_with_live_claim_and_open_run, (
                "deferred crash failure accounting mutated a successor "
                f"attempt on {tid}: status={task.status} "
                f"claim={task.claim_lock} run={task.current_run_id} "
                f"run_row={dict(run) if run else None}"
            )

        if claim_state["claim_ok"]:
            tid = claim_state["claimed_tid"]
            task = kb.get_task(conn, tid)
            assert task is not None
            # Successor won the race: healthy running claim, not blocked.
            assert task.status == "running"
            assert task.claim_lock == "testhost:competitor"
            assert task.current_run_id is not None
            # Old-attempt failure counter must not land on the successor.
            assert int(task.consecutive_failures) == max(
                0, kb.DEFAULT_FAILURE_LIMIT - 1
            )
    finally:
        conn.close()


def test_record_task_failure_cas_ignores_running_successor(kanban_home):
    """Standalone CAS: release_claim=False must not accept status=running."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cas-running", assignee="w")
        claimed = kb.claim_task(conn, tid, claimer="testhost:comp")
        assert claimed is not None
        # Plant high failure count so a trip would fire if CAS accepted running.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET consecutive_failures = ? WHERE id = ?",
                (max(0, kb.DEFAULT_FAILURE_LIMIT - 1), tid),
            )
        tripped = kb._record_task_failure(
            conn, tid,
            error="stale accounting",
            outcome="crashed",
            release_claim=False,
            end_run=False,
        )
        assert tripped is False
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"
        assert task.claim_lock is not None
        assert task.current_run_id is not None
    finally:
        conn.close()
