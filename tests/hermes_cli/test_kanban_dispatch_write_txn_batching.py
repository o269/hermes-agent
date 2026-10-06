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
        # v0.19.0 (8208fc52) liveness check is _pid_alive; the worker-ownership
        # machinery (_recorded_worker_alive et al.) does not exist on this base.
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(
            kb, "_classify_worker_exit", lambda pid: ("nonzero_exit", 1),
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


# ---------------------------------------------------------------------------
# Tunnel-latency simulation (vps2 -> boardd over boardd-tunnel-vps2).
#
# Evidence (boardd journal, 2026-10-05): vps2 clients reach boardd over a
# reverse-SSH tunnel at ~0.13s RTT per broker call, and boardd force-rolls
# back any interactive txn that exceeds TXN_MAX_S=2.0s ("interactive txn ...
# exceeded absolute cap 2.0s (slow holder) — ROLLBACK"). These tests charge
# every SQL call a per-call RTT on a virtual clock (no real sleeping, so the
# suite stays fast) and assert that a full dispatch tick — promotions AND
# crash reclaims — never holds a single interactive txn past the cap.
# ---------------------------------------------------------------------------

# boardd's absolute interactive-txn cap (scripts/fleet/boardd.py TXN_MAX_S,
# default 2.0s). boardd does not ship in this repo at 8208fc52, so the value
# is pinned from the deployed config / brief evidence here.
BOARDD_TXN_MAX_S = 2.0


class _TunnelLatencyConn:
    """sqlite3.Connection delegate that charges each SQL call a per-call
    broker RTT on a virtual clock and records every interactive txn's
    simulated hold time (BEGIN IMMEDIATE .. COMMIT/ROLLBACK).

    Only the window between the transaction boundaries counts against
    boardd's absolute cap, so autocommit scans and local I/O (process
    snapshots, exit classification) correctly stay out of the measurement.
    """

    def __init__(self, real: sqlite3.Connection, *, per_call_s: float):
        self._real = real
        self._per_call_s = float(per_call_s)
        self.clock = 0.0
        self.txn_holds: list[float] = []
        self._txn_start: float | None = None

    def execute(self, sql, *args, **kwargs):
        self.clock += self._per_call_s
        if isinstance(sql, str):
            boundary = sql.lstrip().upper()
            if boundary.startswith("BEGIN"):
                assert self._txn_start is None, (
                    "nested interactive write_txn: a txn opened inside "
                    "another multiplies tunnel hold time"
                )
                self._txn_start = self.clock
            elif boundary.startswith(("COMMIT", "ROLLBACK", "END")):
                if self._txn_start is not None:
                    self.txn_holds.append(self.clock - self._txn_start)
                    self._txn_start = None
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_dispatch_tick_over_simulated_tunnel_no_txn_exceeds_cap(
    kanban_home, monkeypatch,
):
    """A dispatch tick with N promotions + M crash reclaims, charged 0.15s
    per broker call (vps2 tunnel RTT is ~0.13s), must keep EVERY interactive
    write_txn under boardd's TXN_MAX_S. A regression that re-merges the
    per-task txns into one scan-spanning mega-txn fails here, because the
    merged hold scales with the candidate count."""
    per_call_s = 0.15
    cap_s = BOARDD_TXN_MAX_S
    real = kb.connect()
    conn = _TunnelLatencyConn(real, per_call_s=per_call_s)
    try:
        # Host-local crash-detection fixtures (same shape as the reclaim
        # batching test above). v0.19.0 (8208fc52) liveness check is
        # _pid_alive; the worker-ownership machinery does not exist here.
        monkeypatch.setattr(kb, "_claimer_id", lambda: "testhost:1")
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(
            kb, "_classify_worker_exit", lambda pid: ("nonzero_exit", 1),
        )

        # N promotions pending for the tick's recompute_ready phase.
        n_promote = 8
        child_ids = []
        for i in range(n_promote):
            parent = kb.create_task(real, title=f"lat-parent-{i}")
            child = kb.create_task(
                real, title=f"lat-child-{i}",
                parents=[parent], assignee="worker",
            )
            kb.complete_task(real, parent, result="ok")
            child_ids.append(child)
        with kb.write_txn(real):
            for cid in child_ids:
                real.execute(
                    "UPDATE tasks SET status = 'todo' WHERE id = ?", (cid,),
                )

        # M host-local crashed workers for the tick's crash phase.
        n_crash = 5
        crash_ids = []
        for i in range(n_crash):
            tid = kb.create_task(
                real, title=f"lat-crash-{i}", assignee=f"worker-crash-{i}",
            )
            assert kb.claim_task(real, tid, claimer="testhost:1") is not None
            with kb.write_txn(real):
                real.execute(
                    "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                    (91000 + i, tid),
                )
                run_id = real.execute(
                    "SELECT current_run_id FROM tasks WHERE id = ?", (tid,),
                ).fetchone()["current_run_id"]
                real.execute(
                    "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                    (91000 + i, run_id),
                )
            crash_ids.append(tid)

        # Measure only the dispatch tick, not the fixture setup.
        conn.txn_holds.clear()
        res = kb.dispatch_once(conn, dry_run=True, max_spawn=0)

        assert conn._txn_start is None, (
            "dispatch tick leaked an open interactive txn"
        )
        holds = conn.txn_holds
        assert holds, "fixture must exercise at least one write_txn"

        # The tick actually did the work (no false-success short-circuit).
        # Crash-reclaimed tasks land back in todo/ready and may themselves
        # be re-promoted by the same tick's recompute_ready phase, so the
        # contract is "at least the N fixture children promoted", not an
        # exact count.
        assert res.promoted >= n_promote
        for cid in child_ids:
            task = kb.get_task(real, cid)
            assert task is not None and task.status == "ready"
        assert set(res.crashed) == set(crash_ids)
        # Note: DispatchResult.write_failures does not exist at 8208fc52
        # (that false-success defense is t_e036dad5, not part of this
        # batching port), so there is no write_failures assertion here.

        # Core contract: no single interactive txn holds the broker past
        # the cap, with at least two tunnel calls of headroom.
        worst = max(holds)
        assert worst < cap_s, (
            f"interactive txn held {worst:.2f}s under simulated "
            f"{per_call_s:.2f}s/RTT tunnel — exceeds boardd TXN_MAX_S="
            f"{cap_s:.1f}s (holds: {['%.2f' % h for h in holds]})"
        )
        assert worst <= cap_s - 2 * per_call_s, (
            f"worst txn hold {worst:.2f}s leaves less than two tunnel "
            f"calls of headroom under TXN_MAX_S={cap_s:.1f}s"
        )

        # Teeth: if these per-task txns were merged back into one
        # scan-spanning mega-txn, the same tick WOULD exceed the cap — so
        # this fixture is heavy enough to catch that regression.
        assert sum(holds) > cap_s, (
            "fixture too light to catch a mega-txn regression: total "
            f"simulated hold {sum(holds):.2f}s <= TXN_MAX_S={cap_s:.1f}s"
        )
    finally:
        real.close()
