"""Tests for the SessionDB read-path split (per-thread read-only connections).

The gateway shares ONE SessionDB across every agent, so recall/browse reads
used to queue behind writer flushes on self._lock — a measured production
convoy (a 0.2s FTS query stretched to 112s while 6-8 concurrent turns
flushed tool results). These tests pin the new contract: reads run on a
per-thread read-only connection under WAL, never touch self._lock, and fall
back to the legacy locked path when WAL or the read connection is missing.
"""

import concurrent.futures
import os
import threading
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    d.create_session(session_id="s1", source="cli", model="m")
    d.append_message("s1", role="user", content="hello graphiti world")
    d.append_message("s1", role="assistant", content="the neo4j daemon is healthy")
    yield d
    d.close()


@pytest.mark.requires_wal
def test_read_conn_is_per_thread(db):
    conns = {}

    def grab(key):
        conns[key] = db._get_read_conn()

    t1 = threading.Thread(target=grab, args=(1,))
    t2 = threading.Thread(target=grab, args=(2,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert conns[1] is not None and conns[2] is not None
    assert conns[1] is not conns[2]


def test_read_conn_reused_within_thread(db):
    assert db._get_read_conn() is db._get_read_conn()


@pytest.mark.requires_wal
def test_reads_do_not_take_writer_lock(db):
    """Reads must complete while another thread holds self._lock."""
    acquired = db._lock.acquire()
    assert acquired
    try:
        done = {}

        def reader():
            done["session"] = db.get_session("s1")
            done["search"] = db.search_messages("graphiti", limit=10)
            done["messages"] = db.get_messages("s1")

        t = threading.Thread(target=reader)
        t.start()
        t.join(timeout=5.0)
        assert not t.is_alive(), "read path blocked on writer lock"
        assert done["session"]["id"] == "s1"
        assert any("graphiti" in (m.get("snippet") or "") for m in done["search"])
        assert len(done["messages"]) == 2
    finally:
        db._lock.release()




def test_read_your_writes(db):
    """A fresh committed write must be visible to the read connection."""
    db.append_message("s1", role="user", content="zanzibar checkpoint")
    rows = db.search_messages("zanzibar", limit=5)
    assert rows, "committed write invisible to read connection"




@pytest.mark.requires_wal
def test_dead_thread_read_conns_are_pruned(db):
    """Short-lived reader threads must not leave fds in _read_conns forever.

    The dashboard/TUI gateway keeps one long-lived SessionDB; without pruning,
    every ephemeral worker thread that touches a recall/browse path leaked a
    mode=ro connection (state.db + state.db-wal fds) until process exit.
    """
    opened = []
    barrier = threading.Barrier(9)  # 8 workers + main release

    def worker():
        c = db._get_read_conn()
        opened.append(c)
        barrier.wait(timeout=5.0)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    barrier.wait(timeout=5.0)
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive()

    assert len(opened) == 8
    assert all(c is not None for c in opened)
    # Connections stay registered until a subsequent open triggers prune.
    assert len(db._read_conns) >= 8

    # A new open on this thread must prune the 8 dead owners.
    survivors_before = len(db._read_conns)
    mine = db._get_read_conn()
    assert mine is not None
    # Dead owners closed; only live-thread conns remain (at least `mine`).
    assert len(db._read_conns) < survivors_before
    assert mine in db._read_conns
    # Every surviving owner must still be alive.
    for owner, conn in db._read_conn_owners:
        assert owner.is_alive(), "pruned list still holds a dead thread"
        assert conn in db._read_conns
    # Closed connections must not remain in the strong set.
    for c in opened:
        if c is not mine:
            assert c not in db._read_conns


def test_non_wal_uses_locked_path(db):
    db._wal_active = False
    assert db._get_read_conn() is None
    # And queries still work via the legacy path.
    assert db.get_session("s1")["id"] == "s1"


@pytest.mark.requires_wal
def test_read_conn_open_failure_marks_thread(db, monkeypatch, tmp_path):
    """A failed read-conn open must not retry per query; fallback still works."""
    import sqlite3 as _sqlite3

    calls = {"n": 0}
    real_connect = _sqlite3.connect

    def failing_connect(*a, **k):
        if a and isinstance(a[0], str) and a[0].startswith("file:") and "mode=ro" in a[0]:
            calls["n"] += 1
            raise _sqlite3.OperationalError("simulated open failure")
        return real_connect(*a, **k)

    fresh = SessionDB(db_path=tmp_path / "state2.db")
    try:
        fresh.create_session(session_id="x", source="cli", model="m")
        monkeypatch.setattr("hermes_state.sqlite3.connect", failing_connect)
        assert fresh.get_session("x")["id"] == "x"
        assert fresh.get_session("x")["id"] == "x"
        assert calls["n"] == 1, "open failure should be remembered per thread"
    finally:
        fresh.close()


@pytest.mark.requires_wal
def test_anchored_view_and_around_use_read_path(db):
    msgs = db.get_messages("s1")
    anchor = msgs[0]["id"]
    acquired = db._lock.acquire()
    try:
        done = {}

        def reader():
            done["around"] = db.get_messages_around("s1", anchor, window=2)
            done["view"] = db.get_anchored_view("s1", anchor, window=2, bookend=1)

        t = threading.Thread(target=reader)
        t.start(); t.join(timeout=5.0)
        assert not t.is_alive(), "anchored reads blocked on writer lock"
        assert done["around"]["window"]
        assert done["view"]["window"]
    finally:
        db._lock.release()


@pytest.mark.requires_wal
def test_session_resume_reads_do_not_take_writer_lock(db):
    """session.resume's three read paths must not convoy behind writer flushes.

    get_messages_as_conversation / get_resume_conversations /
    get_ancestor_display_prefix are the hottest reads in the file — every
    resume across the gateway, CLI, and ACP adapter goes through one of
    them — so they must use the same per-thread read-only connection as
    get_messages, not the legacy self._lock path.
    """
    db.create_session(session_id="parent1", source="cli", model="m")
    db.append_message("parent1", role="user", content="parent turn")
    db.append_message("parent1", role="assistant", content="parent reply")
    db.create_session(session_id="child1", source="cli", model="m", parent_session_id="parent1")
    db.append_message("child1", role="user", content="child turn")
    db.append_message("child1", role="assistant", content="child reply")

    acquired = db._lock.acquire()
    try:
        done = {}

        def reader():
            done["conversation"] = db.get_messages_as_conversation("s1")
            done["resume"] = db.get_resume_conversations("child1")
            done["ancestor_prefix"] = db.get_ancestor_display_prefix("child1")

        t = threading.Thread(target=reader)
        t.start(); t.join(timeout=5.0)
        assert not t.is_alive(), "session resume reads blocked on writer lock"
        assert len(done["conversation"]) == 2
        model_history, display_history = done["resume"]
        assert len(model_history) == 2
        assert len(display_history) == 4
        assert len(done["ancestor_prefix"]) == 2
    finally:
        db._lock.release()


@pytest.mark.requires_wal
def test_idle_read_connection_is_closed_then_reopened(db, monkeypatch):
    import hermes_state as hs

    conn = db._get_read_conn()
    assert conn is not None
    monkeypatch.setattr(hs, "_READ_CONN_IDLE_TIMEOUT_S", 0.0)
    db._read_conn_last_used[conn] = time.monotonic() - 1
    assert db._sweep_idle_read_conns() == 1
    assert conn in db._read_conn_doomed

    replacement = db._get_read_conn()
    assert replacement is not None
    assert replacement is not conn
    assert conn not in db._read_conns
    assert conn not in db._read_conn_last_used


@pytest.mark.requires_wal
def test_sweeper_is_singleton_and_tracks_live_db(db):
    assert db._get_read_conn() is not None
    import hermes_state as hs

    thread = hs._read_conn_sweeper
    assert thread is not None and thread.is_alive()
    assert db in hs._live_session_dbs
    hs._ensure_read_conn_sweeper()
    assert hs._read_conn_sweeper is thread


@pytest.mark.requires_wal
def test_idle_sweep_bounds_real_fds_across_pool_thread_waves(db, monkeypatch):
    """Reused pool threads replace doomed conns instead of accumulating SQLite FDs."""
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("Linux /proc fd proof")
    import hermes_state as hs

    monkeypatch.setattr(hs, "_READ_CONN_IDLE_TIMEOUT_S", 0.0)
    barrier = threading.Barrier(9)

    def open_and_wait(barrier):
        conn = db._get_read_conn()
        assert conn is not None
        barrier.wait(timeout=5)

    db_targets = {str(db.db_path), str(db.db_path) + "-wal", str(db.db_path) + "-shm"}

    def db_fd_count():
        count = 0
        for name in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{name}")
            except OSError:
                continue
            if target in db_targets:
                count += 1
        return count

    before = db_fd_count()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(open_and_wait, barrier) for _ in range(8)]
        barrier.wait(timeout=5)
        for future in futures:
            future.result(timeout=5)
        opened = db_fd_count()
        assert opened > before
        assert db._sweep_idle_read_conns() >= 8
        second_barrier = threading.Barrier(9)
        futures = [pool.submit(open_and_wait, second_barrier) for _ in range(8)]
        second_barrier.wait(timeout=5)
        for future in futures:
            future.result(timeout=5)
        second_wave = db_fd_count()
        assert second_wave <= opened


@pytest.mark.requires_wal
def test_close_drains_idle_tracking(db):
    assert db._get_read_conn() is not None
    db.close()
    assert db._read_conns == set()
    assert db._read_conn_owners == []
    assert db._read_conn_last_used == {}
    assert db._read_conn_doomed == set()
