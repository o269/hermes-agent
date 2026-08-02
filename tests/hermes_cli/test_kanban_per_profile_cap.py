"""Regression tests for the one-active-card-per-profile dispatch invariant.

Every dispatcher pulse may start at most one distinct card for each profile,
regardless of legacy per-profile cap inputs or global headroom. This protects
provider quota and keeps duplicate workers from racing on the same lane.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading

import pytest


@pytest.fixture()
def isolated_kanban_home_with_profiles(monkeypatch):
    """Spin up a fresh HERMES_HOME with kanban DB + alpha/beta profiles."""
    test_home = tempfile.mkdtemp(prefix="kanban_per_profile_cap_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    monkeypatch.setenv("HERMES_KANBAN_BROKER", "0")
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db

    yield kanban_db


def _fake_spawn(*args, **kwargs):
    return 12345


def test_same_assignee_ready_pair_spawns_only_highest_priority(
    isolated_kanban_home_with_profiles,
):
    """The proven canary: max=2 must not claim/run/PID lower-priority B."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        high = kb.create_task(conn, title="A", assignee="alpha", priority=100)
        low = kb.create_task(conn, title="B", assignee="alpha", priority=10)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, max_spawn=2)
        high_row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (high,)
        ).fetchone()
        low_row = conn.execute(
            "SELECT status, claim_lock, current_run_id FROM tasks WHERE id = ?",
            (low,),
        ).fetchone()
        low_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (low,)
        ).fetchone()[0]
    assert [item[0] for item in res.spawned] == [high]
    assert res.skipped_per_profile_capped == [(low, "alpha", 1)]
    assert high_row["status"] == "running" and high_row["current_run_id"]
    assert tuple(low_row) == ("ready", None, None)
    assert low_runs == 0


def test_different_profiles_fill_global_max(isolated_kanban_home_with_profiles):
    """The profile invariant must not reduce global utilization."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="a", assignee="alpha")
        kb.create_task(conn, title="b", assignee="beta")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            max_spawn=2,
            max_in_progress_per_profile=99,
        )
    spawn_assignees = [s[1] for s in res.spawned]
    assert spawn_assignees == ["alpha", "beta"]
    assert not res.skipped_per_profile_capped


def test_pre_existing_running_counts_against_cap(isolated_kanban_home_with_profiles):
    """An existing active card prevents another card for only that profile."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        running_alpha = kb.create_task(
            conn, title="running alpha", assignee="alpha"
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'test:1' "
                "WHERE id = ?",
                (running_alpha,),
            )
        for i in range(2):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")
        for i in range(2):
            kb.create_task(conn, title=f"b{i}", assignee="beta")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            max_in_progress_per_profile=1,
        )
    spawn_assignees = [s[1] for s in res.spawned]
    capped_assignees = [c[1] for c in res.skipped_per_profile_capped]
    assert spawn_assignees.count("alpha") == 0
    assert spawn_assignees.count("beta") == 1
    assert capped_assignees.count("alpha") == 2
    assert capped_assignees.count("beta") == 1


@pytest.mark.parametrize("cap", [0, -1, "abc", None, 2, 99])
def test_config_cannot_relax_one_active_invariant(
    isolated_kanban_home_with_profiles, cap,
):
    """Legacy config remains accepted but can never permit a second card."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(3):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            max_in_progress_per_profile=cap,
        )
    assert len(res.spawned) == 1
    assert len(res.skipped_per_profile_capped) == 2
    assert all(item[1:] == ("alpha", 1) for item in res.skipped_per_profile_capped)


def test_capped_tasks_dispatched_on_subsequent_tick(
    isolated_kanban_home_with_profiles,
):
    """A deferred card becomes eligible after the active card completes."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(3):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")

    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=False,
            max_in_progress_per_profile=1,
        )
    assert len(res1.spawned) == 1
    assert len(res1.skipped_per_profile_capped) == 2

    spawned_id = res1.spawned[0][0]
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', claim_lock = NULL WHERE id = ?",
                (spawned_id,),
            )

    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=False,
            max_in_progress_per_profile=1,
        )
    assert len(res2.spawned) == 1
    assert len(res2.skipped_per_profile_capped) == 1
    assert res2.spawned[0][0] != spawned_id


def test_transactional_claim_guard_releases_after_running_owner_is_reclaimed(
    isolated_kanban_home_with_profiles,
):
    """Direct/attached claimers share the same fail-closed profile fence."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        first = kb.create_task(conn, title="A", assignee="alpha", priority=100)
        second = kb.create_task(conn, title="B", assignee="alpha", priority=10)

        assert kb.claim_task(conn, first) is not None
        assert kb.claim_task(conn, second) is None
        assert kb.get_task(conn, second).status == "ready"
        rejection = [
            event
            for event in kb.list_events(conn, second)
            if event.kind == "claim_rejected"
        ][-1]
        assert rejection.payload == {
            "reason": "profile_running",
            "profile": "alpha",
            "running_task_id": first,
            "source_status": "ready",
        }

        assert kb.reclaim_task(conn, first, reason="test profile handoff") is True
        assert kb.claim_task(conn, second) is not None
        assert kb.get_task(conn, second).status == "running"


def test_review_claim_transaction_shares_running_profile_fence(
    isolated_kanban_home_with_profiles,
):
    """A review caller cannot race a ready caller onto the same profile."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        ready = kb.create_task(conn, title="ready A", assignee="alpha")
        review = kb.create_task(conn, title="review B", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?",
                (review,),
            )

        assert kb.claim_task(conn, ready) is not None
        assert kb.claim_review_task(conn, review) is None
        assert kb.get_task(conn, review).status == "review"


@pytest.mark.parametrize("assignee", [None, "", "   ", "Alpha"])
@pytest.mark.parametrize("source_status", ["ready", "review"])
def test_missing_assignee_claims_fail_closed_without_corruption(
    isolated_kanban_home_with_profiles,
    assignee,
    source_status,
):
    """Legacy NULL/blank holds cannot escape the profile claim authority."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        owner = kb.create_task(conn, title="running alpha", assignee="alpha")
        candidate = kb.create_task(conn, title="unassigned hold", assignee="alpha")
        assert kb.claim_task(conn, owner) is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = ?, status = ? WHERE id = ?",
                (assignee, source_status, candidate),
            )

        claim = kb.claim_task if source_status == "ready" else kb.claim_review_task
        assert claim(conn, candidate) is None
        row = conn.execute(
            "SELECT status, assignee, claim_lock, claim_expires, current_run_id "
            "FROM tasks WHERE id = ?",
            (candidate,),
        ).fetchone()
        runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (candidate,)
        ).fetchone()[0]
        rejection = [
            event
            for event in kb.list_events(conn, candidate)
            if event.kind == "claim_rejected"
        ][-1]

    assert tuple(row) == (source_status, assignee, None, None, None)
    assert runs == 0
    assert rejection.payload == {
        "reason": "assignee_required",
        "source_status": source_status,
    }


def test_subsequent_pulse_while_one_card_active_does_not_claim_second(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        first = kb.create_task(conn, title="A", assignee="alpha", priority=100)
        second = kb.create_task(conn, title="B", assignee="alpha", priority=10)
        pulse1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, max_spawn=2)
        pulse2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, max_spawn=2)
        second_row = conn.execute(
            "SELECT status, claim_lock, current_run_id FROM tasks WHERE id = ?",
            (second,),
        ).fetchone()
    assert [item[0] for item in pulse1.spawned] == [first]
    assert pulse2.spawned == []
    assert pulse2.skipped_per_profile_capped == [(second, "alpha", 1)]
    assert tuple(second_row) == ("ready", None, None)


def test_concurrent_dispatch_calls_cannot_claim_two_cards_for_one_profile(
    isolated_kanban_home_with_profiles,
    monkeypatch,
):
    """The claim transaction must close the pre-scan-to-claim race.

    The normal board lock keeps one canonical dispatcher serialized. This test
    deliberately admits two callers to model a mismatched/stale lock path and
    synchronizes both after their profile snapshots but before either claim.
    The SQLite claim transaction is the final authority: only one distinct card
    may become Running for ``alpha``.
    """
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        first = kb.create_task(conn, title="A", assignee="alpha", priority=100)
        second = kb.create_task(conn, title="B", assignee="alpha", priority=10)

    @contextlib.contextmanager
    def admit_competing_tick(_db_path):
        yield True

    real_claim_task = kb.claim_task
    claim_barrier = threading.Barrier(2)
    call_count = 0
    call_count_lock = threading.Lock()

    def synchronized_claim(conn, task_id, **kwargs):
        nonlocal call_count
        with call_count_lock:
            call_count += 1
            synchronize = call_count <= 2
        if synchronize:
            claim_barrier.wait(timeout=10)
        return real_claim_task(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, "_dispatch_tick_lock", admit_competing_tick)
    monkeypatch.setattr(kb, "claim_task", synchronized_claim)

    results = []
    errors = []

    def dispatch_pulse():
        try:
            with kb.connect_closing() as conn:
                results.append(
                    kb.dispatch_once(conn, spawn_fn=_fake_spawn, max_spawn=2)
                )
        except BaseException as exc:  # surface thread failures in the test body
            errors.append(exc)

    callers = [threading.Thread(target=dispatch_pulse) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=20)

    assert not [caller for caller in callers if caller.is_alive()]
    assert errors == []
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, status, claim_lock, current_run_id FROM tasks "
            "WHERE id IN (?, ?) ORDER BY id",
            (first, second),
        ).fetchall()
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id IN (?, ?)",
            (first, second),
        ).fetchone()[0]

    assert sum(len(result.spawned) for result in results) == 1
    assert [row["status"] for row in rows].count("running") == 1
    assert [row["status"] for row in rows].count("ready") == 1
    assert run_count == 1


def test_ready_and_review_same_profile_share_one_card_limit(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        ready = kb.create_task(
            conn, title="ready A", assignee="alpha", priority=100
        )
        review = kb.create_task(
            conn, title="review B", assignee="alpha", priority=10
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?", (review,)
            )
        result = kb.dispatch_once(conn, spawn_fn=_fake_spawn, max_spawn=2)
        review_row = conn.execute(
            "SELECT status, claim_lock, current_run_id FROM tasks WHERE id = ?",
            (review,),
        ).fetchone()
    assert [item[0] for item in result.spawned] == [ready]
    assert result.skipped_per_profile_capped == [(review, "alpha", 1)]
    assert tuple(review_row) == ("review", None, None)


def test_live_descendant_holds_profile_but_same_card_uses_respawn_guard(
    isolated_kanban_home_with_profiles, monkeypatch,
):
    """A wrapper-exit child blocks distinct B without changing A telemetry."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        first = kb.create_task(conn, title="A", assignee="alpha", priority=100)
        second = kb.create_task(conn, title="B", assignee="alpha", priority=10)
        claimed = kb.claim_task(conn, first)
        assert claimed is not None and claimed.current_run_id is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL WHERE id = ?",
                (first,),
            )
        board_db, board_slug = kb._connection_worker_board_identity(conn)
        descendant = kb._WorkerProcessSnapshot(
            pid=87654,
            task_id=first,
            run_id=claimed.current_run_id,
            pgid=87600,
            sid=87600,
            create_time=1234.0,
            board_db=board_db,
            board_slug=board_slug,
            boot_id=kb._read_host_boot_id(),
        )
        monkeypatch.setattr(
            kb, "_snapshot_worker_processes", lambda **_kw: [descendant]
        )
        result = kb.dispatch_once(conn, spawn_fn=_fake_spawn, max_spawn=2)
        second_row = conn.execute(
            "SELECT status, claim_lock, current_run_id FROM tasks WHERE id = ?",
            (second,),
        ).fetchone()
    assert result.spawned == []
    assert (first, "live_worker_process") in result.respawn_guarded
    assert result.skipped_per_profile_capped == [(second, "alpha", 1)]
    assert tuple(second_row) == ("ready", None, None)


def test_dispatch_result_has_skipped_per_profile_capped_field():
    """DispatchResult exposes structured per-profile-cap telemetry."""
    from hermes_cli.kanban_db import DispatchResult

    result = DispatchResult()
    assert hasattr(result, "skipped_per_profile_capped")
    assert result.skipped_per_profile_capped == []
