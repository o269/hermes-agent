"""Tests for the SessionDB read-path split (per-thread read-only connections).

The gateway shares ONE SessionDB across every agent, so recall/browse reads
used to queue behind writer flushes on self._lock — a measured production
convoy (a 0.2s FTS query stretched to 112s while 6-8 concurrent turns
flushed tool results). These tests pin the new contract: reads run on a
per-thread read-only connection under WAL, never touch self._lock, and fall
back to the legacy locked path when WAL or the read connection is missing.
"""

import threading

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


# ── FD leak prevention tests (p87) ──
#
# The dashboard/TUI gateway keeps one long-lived SessionDB; without pruning,
# every ephemeral worker thread that touches a recall/browse path leaked a
# mode=ro connection (state.db + state.db-wal + state.db-shm fds) for the
# process lifetime.  These tests pin the contract that dead-thread and idle
# connections are closed.


@pytest.mark.requires_wal
def test_dead_thread_read_conns_are_pruned(db):
    """Short-lived reader threads must not leave fds in _read_conns forever.

    Without dead-thread pruning, each ThreadPoolExecutor worker that touches
    the read path leaks a mode=ro connection (db+wal+shm fds) until process
    exit.  A new open on a surviving thread must prune all dead owners.
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


@pytest.mark.requires_wal
def test_idle_read_conns_are_doomed_and_closed(db):
    """Idle-but-alive connections must be marked doomed and then closed.

    The background sweeper marks connections idle beyond the timeout as
    doomed.  The owning thread closes doomed connections on its next
    _get_read_conn call, preventing fd accumulation from long-lived
    ThreadPoolExecutor threads that never exit.
    """
    # Get a read conn on this thread (alive).
    conn = db._get_read_conn()
    assert conn is not None
    assert conn in db._read_conns

    # Simulate the sweeper marking the connection as doomed.
    conn._hermes_doomed = True

    # Next _get_read_conn on this thread should close the doomed conn
    # and open a fresh one.
    new_conn = db._get_read_conn()
    assert new_conn is not None
    assert new_conn is not conn
    assert conn not in db._read_conns
    assert new_conn in db._read_conns


@pytest.mark.requires_wal
def test_sweep_idle_read_conns_closes_dead_threads(db):
    """_sweep_idle_read_conns must close connections from dead threads.

    Uses a barrier to keep worker threads alive while they hold connections,
    so dead-thread pruning during open doesn't race with the test setup.
    After releasing the barrier and joining, the sweeper closes all
    dead-thread connections.
    """
    opened = []
    barrier = threading.Barrier(5)  # 4 workers + main release

    def worker():
        c = db._get_read_conn()
        opened.append(c)
        barrier.wait(timeout=5.0)  # hold conn while alive

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    barrier.wait(timeout=5.0)  # release workers
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive()

    assert len(opened) == 4
    assert len(db._read_conns) >= 4

    # The sweeper should close all dead-thread connections.
    closed = db._sweep_idle_read_conns()
    assert closed >= 4
    assert len(db._read_conns) == 0


@pytest.mark.requires_wal
def test_sweep_marks_idle_alive_conns_doomed(db, monkeypatch):
    """_sweep_idle_read_conns must mark idle-but-alive conns as doomed.

    The sweeper should not close idle connections cross-thread (that would
    race with an in-flight query); it marks them doomed so the owning
    thread closes them on its next access.
    """
    import hermes_state as hs

    # Shorten the idle timeout so we don't have to wait 5 minutes.
    monkeypatch.setattr(hs, "_READ_CONN_IDLE_TIMEOUT_S", 0.0)

    # Get a read conn on this thread (alive).
    conn = db._get_read_conn()
    assert conn is not None
    assert conn in db._read_conns

    # Backdate the last-used timestamp so it's considered idle.
    conn._hermes_last_used = 0.0

    # The sweeper should mark it doomed (not close it — thread is alive).
    closed = db._sweep_idle_read_conns()
    assert closed >= 1
    assert getattr(conn, "_hermes_doomed", False)

    # The connection is still in _read_conns (sweeper didn't close it).
    assert conn in db._read_conns

    # Next _get_read_conn on this thread should close the doomed conn
    # and open a fresh one.
    new_conn = db._get_read_conn()
    assert new_conn is not None
    assert new_conn is not conn
    assert conn not in db._read_conns
    assert new_conn in db._read_conns


@pytest.mark.requires_wal
def test_check_same_thread_false_on_read_conns(db):
    """Read connections must be opened with check_same_thread=False.

    Without this, cross-thread close() during dead-thread pruning and
    SessionDB.close() raises ProgrammingError (silently swallowed),
    permanently leaking the db+wal+shm fds — the root cause of the
    dashboard Errno 24 outage.
    """
    conn = db._get_read_conn()
    assert conn is not None
    # The connection must be closeable from a different thread without
    # raising ProgrammingError.  If check_same_thread=True were set,
    # this would raise: "SQLite objects created in a thread can only
    # be used in that same thread."
    errors = []

    def closer():
        try:
            conn.close()
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=closer)
    t.start()
    t.join(timeout=5.0)
    assert not errors, f"cross-thread close raised: {errors}"


@pytest.mark.requires_wal
def test_close_drains_owners(db):
    """close() must drain _read_conn_owners as well as _read_conns."""
    # Create a read conn on a worker thread.
    def worker():
        db._get_read_conn()

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5.0)

    assert len(db._read_conn_owners) >= 1
    db.close()
    assert len(db._read_conn_owners) == 0
    assert len(db._read_conns) == 0
