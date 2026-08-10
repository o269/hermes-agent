"""Tests for kanban set_status functionality and review / set-status CLI subcommands (task t_76ad4258)."""

from __future__ import annotations

import argparse
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
