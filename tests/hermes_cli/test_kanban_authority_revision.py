"""Authority revision CAS coverage for broker-backed board automation."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kanban_cli


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as connection:
        yield connection


def _revision(conn, task_id: str) -> int:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task.authority_revision


def test_revision_covers_task_comment_link_and_parent_status_mutations(conn):
    parent = kb.create_task(conn, title="parent", assignee="worker")
    child = kb.create_task(conn, title="child", assignee="worker")
    assert _revision(conn, parent) == 0
    assert _revision(conn, child) == 0

    conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("updated", child))
    assert _revision(conn, child) == 1
    with pytest.raises(sqlite3.IntegrityError, match="authority_revision cannot decrease"):
        conn.execute(
            "UPDATE tasks SET authority_revision = 0 WHERE id = ?",
            (child,),
        )
    assert _revision(conn, child) == 1

    comment_id = kb.add_comment(conn, child, "operator", "OPERATOR-HOLD")
    assert _revision(conn, child) == 2
    conn.execute(
        "UPDATE task_comments SET body = ? WHERE id = ?",
        ("OPERATOR-HOLD revised", comment_id),
    )
    assert _revision(conn, child) == 3
    conn.execute("DELETE FROM task_comments WHERE id = ?", (comment_id,))
    assert _revision(conn, child) == 4

    kb.link_tasks(conn, parent, child)
    linked_revision = _revision(conn, child)
    assert linked_revision > 4

    parent_before = _revision(conn, parent)
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    assert _revision(conn, parent) > parent_before
    assert _revision(conn, child) > linked_revision

    before_unlink = _revision(conn, child)
    assert kb.unlink_tasks(conn, parent, child)
    assert _revision(conn, child) > before_unlink


def test_stale_revision_refuses_promote_after_final_read_comment(conn):
    parent = kb.create_task(conn, title="parent", assignee="worker")
    child = kb.create_task(
        conn,
        title="child",
        assignee="worker",
        parents=[parent],
    )
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    expected = _revision(conn, child)

    kb.add_comment(conn, child, "operator", "DO NOT DISPATCH; OPERATOR-HOLD")
    with pytest.raises(kb.AuthorityRevisionMismatch, match="authority revision changed"):
        kb.promote_task(
            conn,
            child,
            actor="boardqb",
            expected_authority_revision=expected,
        )
    task = kb.get_task(conn, child)
    assert task is not None
    assert task.status == "todo"


def test_stale_revision_refuses_assign_unblock_and_comment(conn):
    task_id = kb.create_task(conn, title="task", assignee="worker")
    expected = _revision(conn, task_id)
    kb.add_comment(conn, task_id, "operator", "OPERATOR-HOLD")

    with pytest.raises(kb.AuthorityRevisionMismatch):
        kb.assign_task(
            conn,
            task_id,
            "other",
            expected_authority_revision=expected,
        )

    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
    blocked_revision = _revision(conn, task_id)
    kb.add_comment(conn, task_id, "operator", "new hold evidence")
    with pytest.raises(kb.AuthorityRevisionMismatch):
        kb.unblock_task(
            conn,
            task_id,
            expected_authority_revision=blocked_revision,
        )

    comment_revision = _revision(conn, task_id)
    conn.execute("UPDATE tasks SET priority = priority + 1 WHERE id = ?", (task_id,))
    with pytest.raises(kb.AuthorityRevisionMismatch):
        kb.add_comment(
            conn,
            task_id,
            "boardqb",
            "automation note",
            expected_authority_revision=comment_revision,
        )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.assignee == "worker"
    assert task.status == "blocked"
    assert all(c.body != "automation note" for c in kb.list_comments(conn, task_id))


def test_matching_revision_allows_each_guarded_mutation(conn):
    task_id = kb.create_task(conn, title="task", assignee="worker")

    revision = _revision(conn, task_id)
    assert kb.assign_task(
        conn,
        task_id,
        "other",
        expected_authority_revision=revision,
    )

    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
    revision = _revision(conn, task_id)
    assert kb.unblock_task(
        conn,
        task_id,
        expected_authority_revision=revision,
    )

    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
    revision = _revision(conn, task_id)
    promoted, error = kb.promote_task(
        conn,
        task_id,
        actor="boardqb",
        expected_authority_revision=revision,
    )
    assert promoted and error is None

    revision = _revision(conn, task_id)
    kb.add_comment(
        conn,
        task_id,
        "boardqb",
        "guarded note",
        expected_authority_revision=revision,
    )
    assert kb.list_comments(conn, task_id)[-1].body == "guarded note"


def test_cli_promote_wires_expected_revision_and_show_projects_comment_ids(
    conn,
    capsys: pytest.CaptureFixture[str],
):
    task_id = kb.create_task(conn, title="task", assignee="worker")
    before = kb.get_task(conn, task_id)
    assert before is not None
    expected = _revision(conn, task_id)
    comment_id = kb.add_comment(conn, task_id, "operator", "OPERATOR-HOLD")
    assert _revision(conn, task_id) > expected

    stale_args = argparse.Namespace(
        task_id=task_id,
        ids=[],
        reason=None,
        force=False,
        dry_run=False,
        json=False,
        expected_authority_revision=expected,
    )
    with pytest.raises(kb.AuthorityRevisionMismatch):
        kanban_cli._cmd_promote(stale_args)
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == before.status

    show_args = argparse.Namespace(task_id=task_id, json=True)
    assert kanban_cli._cmd_show(show_args) == 0
    shown = capsys.readouterr().out
    document = json.loads(shown)
    assert document["task"]["authority_revision"] == _revision(conn, task_id)
    assert document["comments"][-1]["id"] == comment_id
