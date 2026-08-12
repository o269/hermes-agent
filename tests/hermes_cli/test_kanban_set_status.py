"""Tests for kanban set_status functionality and review / set-status CLI subcommands (task t_76ad4258)."""

from __future__ import annotations

import argparse
import contextlib
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def test_set_status_running_sets_sticky_claim(conn):
    task_id = kb.create_task(conn, title="Test task", assignee="worker1")
    t = kb.get_task(conn, task_id)
    assert t is not None
    assert t.status == "ready"
    assert t.claim_lock is None

    ok = kb.set_status(conn, task_id, "running")
    assert ok is True

    t_running = kb.get_task(conn, task_id)
    assert t_running is not None
    assert t_running.status == "running"
    assert t_running.claim_lock == "seat-sticky"
    assert t_running.claim_expires is not None
    assert t_running.claim_expires > 1000000000
    assert t_running.last_heartbeat_at is not None
    assert t_running.current_run_id is not None
    run = kb.get_run(conn, t_running.current_run_id)
    assert run is not None
    assert run.status == "running"
    assert run.claim_lock == "seat-sticky"


def test_set_status_running_to_ready_releases_dispatch_candidate(conn):
    """The exact p174 regression: ready must never retain seat-sticky custody."""
    task_id = kb.create_task(conn, title="manual then queued", assignee="worker1")
    assert kb.set_status(conn, task_id, "running") is True
    active = kb.get_task(conn, task_id)
    assert active is not None and active.current_run_id is not None
    run_id = active.current_run_id

    assert kb.set_status(conn, task_id, "ready") is True

    queued = kb.get_task(conn, task_id)
    closed = kb.get_run(conn, run_id)
    assert queued is not None
    assert queued.status == "ready"
    assert queued.claim_lock is None
    assert queued.claim_expires is None
    assert queued.current_run_id is None
    assert closed is not None
    assert closed.outcome == "reclaimed"
    assert closed.ended_at is not None

    # claim_task is the dispatcher's final ready->running CAS.  Winning here
    # proves the row is no longer absent behind ``claim_lock IS NULL``.
    claimed = kb.claim_task(conn, task_id, claimer="test-dispatcher")
    assert claimed is not None
    assert claimed.status == "running"


@pytest.mark.parametrize("target_status", ["ready", "review"])
def test_set_status_rejects_dispatcher_owned_running_claim(
    conn, target_status,
):
    task_id = kb.create_task(
        conn,
        title="dispatcher-owned task",
        assignee="worker1",
    )
    claimed = kb.claim_task(
        conn,
        task_id,
        claimer="dispatcher:test-host:123",
    )
    assert claimed is not None
    assert claimed.current_run_id is not None

    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (424242, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (424242, claimed.current_run_id),
        )

    task_before = conn.execute(
        "SELECT status, current_run_id, claim_lock, claim_expires, worker_pid "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    run_before = conn.execute(
        "SELECT status, outcome, ended_at, claim_lock, claim_expires, worker_pid "
        "FROM task_runs WHERE id = ?",
        (claimed.current_run_id,),
    ).fetchone()

    assert kb.set_status(conn, task_id, target_status) is False

    task_after = conn.execute(
        "SELECT status, current_run_id, claim_lock, claim_expires, worker_pid "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    run_after = conn.execute(
        "SELECT status, outcome, ended_at, claim_lock, claim_expires, worker_pid "
        "FROM task_runs WHERE id = ?",
        (claimed.current_run_id,),
    ).fetchone()
    assert tuple(task_after) == tuple(task_before)
    assert tuple(run_after) == tuple(run_before)


def test_set_status_snapshot_race_preserves_new_dispatcher_claim(
    conn, monkeypatch,
):
    task_id = kb.create_task(
        conn,
        title="manual snapshot race",
        assignee="worker1",
    )
    manual_waiting = threading.Event()
    resume_manual = threading.Event()
    manual_results: list[bool] = []
    manual_errors: list[BaseException] = []
    real_write_txn = kb.write_txn

    @contextlib.contextmanager
    def gated_write_txn(candidate_conn):
        if threading.current_thread().name == "manual-set-status":
            manual_waiting.set()
            if not resume_manual.wait(timeout=5):
                raise AssertionError("manual status transition was not resumed")
        with real_write_txn(candidate_conn):
            yield

    monkeypatch.setattr(kb, "write_txn", gated_write_txn)

    def set_review_from_snapshot() -> None:
        try:
            with kb.connect() as manual_conn:
                manual_results.append(
                    kb.set_status(manual_conn, task_id, "review")
                )
        except BaseException as exc:
            manual_errors.append(exc)

    manual_thread = threading.Thread(
        target=set_review_from_snapshot,
        name="manual-set-status",
    )
    manual_thread.start()
    assert manual_waiting.wait(timeout=5)

    claimed = kb.claim_task(
        conn,
        task_id,
        claimer="dispatcher:race-winner",
    )
    assert claimed is not None
    assert claimed.current_run_id is not None
    with real_write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (515151, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (515151, claimed.current_run_id),
        )

    resume_manual.set()
    manual_thread.join(timeout=5)
    assert not manual_thread.is_alive()
    assert manual_errors == []
    assert manual_results == [False]

    task_after = kb.get_task(conn, task_id)
    run_after = kb.get_run(conn, claimed.current_run_id)
    assert task_after is not None
    assert task_after.status == "running"
    assert task_after.current_run_id == claimed.current_run_id
    assert task_after.claim_lock == "dispatcher:race-winner"
    assert task_after.worker_pid == 515151
    assert run_after is not None
    assert run_after.status == "running"
    assert run_after.ended_at is None
    assert run_after.claim_lock == "dispatcher:race-winner"
    assert run_after.worker_pid == 515151


def test_second_manual_running_claim_for_profile_remains_rejected(conn):
    first = kb.create_task(conn, title="manual owner", assignee="worker1")
    second = kb.create_task(conn, title="manual contender", assignee="worker1")

    assert kb.set_status(conn, first, "running") is True
    assert kb.set_status(conn, second, "running") is False

    contender = kb.get_task(conn, second)
    assert contender is not None
    assert contender.status == "ready"
    assert contender.claim_lock is None
    assert contender.current_run_id is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
        (second,),
    ).fetchone()[0] == 0


def test_set_status_repairs_already_ready_stale_claim(conn):
    """Re-applying ready repairs rows corrupted by the retired native op."""
    task_id = kb.create_task(conn, title="stale ready", assignee="worker1")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET claim_lock = 'seat-sticky', claim_expires = ? "
            "WHERE id = ? AND status = 'ready'",
            (2_101_406_907, task_id),
        )

    assert kb.set_status(conn, task_id, "ready") is True
    repaired = kb.get_task(conn, task_id)
    assert repaired is not None
    assert repaired.status == "ready"
    assert repaired.claim_lock is None
    assert repaired.claim_expires is None
    assert kb.claim_task(conn, task_id, claimer="test-dispatcher") is not None


def test_dispatch_tick_repairs_preexisting_sticky_ready_and_spawns(
    conn, monkeypatch, tmp_path,
):
    """Rollout heals already-corrupted queue rows in the same dispatch tick."""
    task_id = kb.create_task(
        conn,
        title="legacy invisible ready",
        body="Resource-Class: light",
        assignee="worker1",
        workspace_kind="dir",
        workspace_path=str(tmp_path),
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET claim_lock = 'seat-sticky', claim_expires = ? "
            "WHERE id = ? AND status = 'ready'",
            (2_101_406_907, task_id),
        )
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _assignee: True)

    dry = kb.dispatch_once(
        conn,
        dry_run=True,
        spawn_fn=lambda *_args, **_kwargs: 4242,
    )
    assert dry.reclaimed == 0
    assert any(
        disposition.task_id == task_id
        and disposition.reason == "stale_manual_custody"
        for disposition in dry.dispositions
    )
    still_stale = kb.get_task(conn, task_id)
    assert still_stale is not None
    assert still_stale.claim_lock == "seat-sticky"

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *_args, **_kwargs: 4242,
    )
    assert result.reclaimed == 1
    assert task_id in [spawned_id for spawned_id, _, _ in result.spawned]
    running = kb.get_task(conn, task_id)
    assert running is not None
    assert running.status == "running"
    assert running.claim_lock != "seat-sticky"


def test_dispatch_dry_run_surfaces_stale_review_without_mutation(conn):
    task_id = kb.create_task(
        conn,
        title="legacy invisible review",
        assignee="worker1",
    )
    assert kb.set_status(conn, task_id, "review") is True
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET claim_lock = 'seat-sticky', claim_expires = ? "
            "WHERE id = ? AND status = 'review'",
            (2_101_406_907, task_id),
        )
    before = conn.execute(
        "SELECT status, claim_lock, claim_expires, current_run_id, worker_pid "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()

    dry = kb.dispatch_once(
        conn,
        dry_run=True,
        spawn_fn=lambda *_args, **_kwargs: 4242,
    )

    assert any(
        disposition.task_id == task_id
        and disposition.reason == "stale_manual_custody"
        for disposition in dry.dispositions
    )
    after = conn.execute(
        "SELECT status, claim_lock, claim_expires, current_run_id, worker_pid "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert tuple(after) == tuple(before)


def test_set_status_review(conn):
    task_id = kb.create_task(conn, title="Test task", assignee="worker1")
    ok = kb.set_status(conn, task_id, "review")
    assert ok is True

    t_review = kb.get_task(conn, task_id)
    assert t_review is not None
    assert t_review.status == "review"


def test_set_status_invalid_status_raises(conn):
    task_id = kb.create_task(conn, title="Test task", assignee="worker1")
    with pytest.raises(ValueError, match="status must be one of"):
        kb.set_status(conn, task_id, "invalid_status_foo")


def test_cli_review_command(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Task for review", assignee="worker1")

    args = argparse.Namespace(
        task_ids=[task_id],
        reason="Finished implementation",
        board=None,
    )
    rc = kb_cli._cmd_review(args)
    assert rc == 0

    with kb.connect() as conn:
        t = kb.get_task(conn, task_id)
        assert t is not None
        assert t.status == "review"
        comments = kb.list_comments(conn, task_id)
        assert len(comments) == 1
        assert "REVIEW: Finished implementation" in comments[0].body


def test_cli_set_status_command(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Task for set status", assignee="worker1")

    args = argparse.Namespace(
        task_id=task_id,
        status="running",
        reason="Manual session start",
        board=None,
    )
    rc = kb_cli._cmd_set_status(args)
    assert rc == 0

    with kb.connect() as conn:
        t = kb.get_task(conn, task_id)
        assert t is not None
        assert t.status == "running"
        assert t.claim_lock == "seat-sticky"
        comments = kb.list_comments(conn, task_id)
        assert len(comments) == 1
        assert "SET STATUS (running): Manual session start" in comments[0].body


def test_cli_invalid_status_with_reason_has_no_side_effects(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Task for invalid set status",
            assignee="worker1",
        )
        events_before = list(kb.list_events(conn, task_id))

    args = argparse.Namespace(
        task_id=task_id,
        status="typo-status",
        reason="must not persist",
        board=None,
    )
    assert kb_cli._cmd_set_status(args) == 1

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert kb.list_comments(conn, task_id) == []
        assert kb.list_events(conn, task_id) == events_before
