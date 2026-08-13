"""Worker-session visibility (Symptom D) regression tests.

``tasks.worker_session_id`` records the EXECUTING worker's own state.db
session id — distinct from ``tasks.session_id`` (creator provenance) — so
Desktop/TUI can open a running card's live session even though the sidebar
deny-lists ``source=kanban`` rows.

The wiring test at the bottom is the one that bites: it exercises the REAL
``AIAgent._ensure_db_session`` hook and fails if the back-fill call is ever
removed from the session-creation path.
"""

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """Fresh on-disk board; env pinned the way a dispatched worker sees it."""
    db_path = tmp_path / "kanban.db"
    # Direct sqlite in this test: the broker shim must not intercept connect().
    monkeypatch.delenv("HERMES_KANBAN_BROKER", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    conn = kanban_db.connect(db_path=db_path)
    yield conn, db_path
    conn.close()


def _make_running_task(conn, *, claim_lock="host-a:host-b:1234"):
    task_id = kanban_db.create_task(conn, title="symptom-d probe")
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=? WHERE id=?",
        (claim_lock, task_id),
    )
    conn.commit()
    return task_id


def _worker_session_id(db_path, task_id):
    with sqlite3.connect(db_path) as raw:
        row = raw.execute(
            "SELECT worker_session_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    return row[0] if row else None


def test_schema_has_worker_session_column(board):
    conn, _ = board
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "worker_session_id" in cols
    # The creator-provenance column must still exist separately: the worker
    # field is an ADDITION, never an overload of session_id.
    assert "session_id" in cols


def test_set_worker_session_cas_guard(board):
    conn, db_path = board
    task_id = _make_running_task(conn, claim_lock="lockA")

    # Wrong lock (a stale, reclaimed attempt) must not write.
    assert not kanban_db.set_worker_session(
        conn, task_id, "stale_sess", claim_lock="lockB"
    )
    assert _worker_session_id(db_path, task_id) is None

    # The owning attempt writes.
    assert kanban_db.set_worker_session(
        conn, task_id, "sess_live", claim_lock="lockA"
    )
    assert _worker_session_id(db_path, task_id) == "sess_live"

    # And leaves an audit event.
    kinds = [
        r[0]
        for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (task_id,)
        )
    ]
    assert "worker_session" in kinds


def test_set_worker_session_requires_running(board):
    conn, db_path = board
    task_id = kanban_db.create_task(conn, title="not running")
    assert not kanban_db.set_worker_session(conn, task_id, "sess_x")
    assert _worker_session_id(db_path, task_id) is None


def test_backfill_from_env(board, monkeypatch):
    conn, db_path = board
    task_id = _make_running_task(conn, claim_lock="lockA")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "lockA")
    assert kanban_db.backfill_worker_session_from_env("sess_env")
    assert _worker_session_id(db_path, task_id) == "sess_env"


def test_backfill_never_raises_without_board(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_BROKER", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_nonexistent")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "missing" / "x.db"))
    # Contract: session creation must never be breakable by the board.
    assert kanban_db.backfill_worker_session_from_env("sess_x") is False


def test_ensure_db_session_backfills_worker_session(board, monkeypatch):
    """THE regression gate: the real ``_ensure_db_session`` must invoke the
    back-fill. Removing the hook from run_agent makes this test fail."""
    conn, db_path = board
    task_id = _make_running_task(conn, claim_lock="lockA")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "lockA")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")

    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._persist_disabled = False
    agent._session_db_created = False
    agent._session_db = SimpleNamespace(create_session=lambda **kw: None)
    agent.session_id = "sess_agent_hook"
    agent.platform = "cli"
    agent.model = "test-model"
    agent._session_init_model_config = {}
    agent._cached_system_prompt = None
    agent._parent_session_id = None

    agent._ensure_db_session()

    assert agent._session_db_created is True
    assert _worker_session_id(db_path, task_id) == "sess_agent_hook"


def test_ensure_db_session_no_board_write_for_non_workers(board, monkeypatch):
    """A plain (non-kanban) session must never touch the board."""
    conn, db_path = board
    task_id = _make_running_task(conn, claim_lock="lockA")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._persist_disabled = False
    agent._session_db_created = False
    agent._session_db = SimpleNamespace(create_session=lambda **kw: None)
    agent.session_id = "sess_plain_chat"
    agent.platform = "cli"
    agent.model = "test-model"
    agent._session_init_model_config = {}
    agent._cached_system_prompt = None
    agent._parent_session_id = None

    agent._ensure_db_session()

    assert _worker_session_id(db_path, task_id) is None
