"""Tests for the kanban dispatcher single-writer lock (issue #35240).

A ``hermes gateway run --replace`` / ``gateway restart`` from a shell on a
systemd/launchd host can leave an orphan dispatcher that escapes the
service cgroup, survives ``systemctl restart``, and becomes a second
long-lived writer on the same ``kanban.db`` — the documented root cause of
multi-writer SQLite WAL corruption. ``dispatch_once`` now wraps each tick in
a non-blocking, board-scoped dispatch lock so two dispatchers can never run
a reclaim/spawn/write tick concurrently. The losing dispatcher returns an
empty ``DispatchResult`` with ``skipped_locked=True`` and does no DB writes.
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "profiles" / "w").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BROKER", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def test_uncontended_tick_runs_and_is_not_skipped(conn):
    """With no other holder, a tick runs normally and skipped_locked is False."""
    kb.create_task(conn, title="t", assignee="w")
    result = kb.dispatch_once(conn)
    assert result.skipped_locked is False


def test_held_lock_skips_the_tick_without_writes(conn):
    """While another holder owns the board lock, dispatch_once must skip and
    must NOT invoke spawn_fn (no DB writes happen on a skipped tick)."""
    kb.create_task(conn, title="t", assignee="w")
    db_path = kb.kanban_db_path(board="default")

    spawn_calls: list = []

    def spy_spawn(task, workspace_path, board=None):
        spawn_calls.append(getattr(task, "id", task))
        return 999999

    # Hold the lock, then attempt a contended tick.
    with kb._dispatch_tick_lock(db_path) as held:
        assert held is True  # we genuinely acquired it
        result = kb.dispatch_once(conn, spawn_fn=spy_spawn)

    assert result.skipped_locked is True
    assert result.spawned == []
    assert spawn_calls == [], "spawn_fn must not run while the tick is locked out"


def test_lock_releases_so_next_tick_runs(conn):
    """After the holder releases, the next tick is no longer skipped."""
    kb.create_task(conn, title="t", assignee="w")
    db_path = kb.kanban_db_path(board="default")

    with kb._dispatch_tick_lock(db_path) as held:
        assert held is True
        assert kb.dispatch_once(conn).skipped_locked is True

    # Lock released — a fresh tick proceeds.
    assert kb.dispatch_once(conn).skipped_locked is False


def test_lock_is_board_scoped(conn):
    """Holding board A's dispatch lock must not block a tick on board B —
    distinct boards have distinct DB files and tick independently."""
    db_default = kb.kanban_db_path(board="default")
    db_other = db_default.with_name("other-board-kanban.db")

    # Two different lock files → both acquirable simultaneously.
    with kb._dispatch_tick_lock(db_default) as held_a:
        assert held_a is True
        with kb._dispatch_tick_lock(db_other) as held_b:
            assert held_b is True, "a lock on a different board must be independent"


def test_reentrant_same_path_lock_is_exclusive(conn):
    """A second acquisition of the SAME board's lock from a sibling context
    must report not-held (the flock is exclusive within the host)."""
    db_path = kb.kanban_db_path(board="default")
    with kb._dispatch_tick_lock(db_path) as held_a:
        assert held_a is True
        with kb._dispatch_tick_lock(db_path) as held_b:
            assert held_b is False, "same-board lock must be exclusive"


def _dispatch_lock_holder(home: str, entered, release, output) -> None:
    os.environ["HERMES_HOME"] = home
    os.environ["HERMES_KANBAN_HOME"] = home
    os.environ["HERMES_KANBAN_BROKER"] = "0"
    from hermes_cli import kanban_db as child_kb

    def blocking_spawn(*_args, **_kwargs):
        assert _kwargs.get("heavy_workspace_lease") is None
        entered.set()
        assert release.wait(10)
        return 99101

    with child_kb.connect() as child_conn:
        result = child_kb.dispatch_once(child_conn, spawn_fn=blocking_spawn)
    output.put((result.skipped_locked, len(result.spawned)))


def _dispatch_lock_contender(home: str, output) -> None:
    os.environ["HERMES_HOME"] = home
    os.environ["HERMES_KANBAN_HOME"] = home
    os.environ["HERMES_KANBAN_BROKER"] = "0"
    from hermes_cli import kanban_db as child_kb

    with child_kb.connect() as child_conn:
        result = child_kb.dispatch_once(
            child_conn, spawn_fn=lambda *_args, **_kwargs: 99102
        )
    output.put((result.skipped_locked, len(result.spawned)))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-lock regression")
def test_two_concurrent_dispatcher_processes_allow_only_lock_holder(
    conn, kanban_home
):
    """The losing invocation must perform no claim, run creation, or spawn."""
    task_id = kb.create_task(
        conn,
        title="one",
        body="Resource-Class: light\nDispatch-lock unit test.",
        assignee="w",
    )
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    release = ctx.Event()
    holder_output = ctx.Queue()
    contender_output = ctx.Queue()
    holder = ctx.Process(
        target=_dispatch_lock_holder,
        args=(str(kanban_home), entered, release, holder_output),
    )
    contender = ctx.Process(
        target=_dispatch_lock_contender,
        args=(str(kanban_home), contender_output),
    )
    holder.start()
    assert entered.wait(10), "holder never reached spawn while owning dispatch lock"
    contender.start()
    contender.join(10)
    assert contender.exitcode == 0
    release.set()
    holder.join(10)
    assert holder.exitcode == 0
    assert holder_output.get(timeout=2) == (False, 1)
    assert contender_output.get(timeout=2) == (True, 0)
    # Re-open after cross-process writes; the fixture connection may retain a
    # pre-fork read snapshot under WAL.
    with kb.connect() as verify_conn:
        row = verify_conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        run_count = verify_conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    assert row["status"] == "running" and row["current_run_id"]
    assert run_count == 1
