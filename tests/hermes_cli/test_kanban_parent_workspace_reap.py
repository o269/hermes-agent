"""Fail-closed parent scratch cleanup: never rmtree a live parent's workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _scratch_parent_with_child(conn, *, parent_status, workspaces_root, worker_pid=None):
    parent = kb.create_task(conn, title="parent", assignee="a")
    child = kb.create_task(conn, title="child", assignee="b", parents=[parent])
    wp = Path(workspaces_root) / parent
    wp.mkdir(parents=True, exist_ok=True)
    (wp / "deliverable.md").write_text("777 seconds of work", encoding="utf-8")
    conn.execute(
        "UPDATE tasks SET workspace_kind='scratch', workspace_path=?, "
        "status=?, worker_pid=? WHERE id=?",
        (str(wp), parent_status, worker_pid, parent),
    )
    conn.commit()
    return parent, child, wp


def test_running_parent_workspace_survives_child_completion(kanban_home, monkeypatch):
    """A still-running decomposition parent must keep its scratch dir."""
    monkeypatch.setattr(kb, "_is_managed_scratch_path", lambda _p: True)
    with kb.connect() as conn:
        parent, child, wp = _scratch_parent_with_child(
            conn, parent_status="running", workspaces_root=kanban_home / "ws",
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
        conn.commit()
        kb._try_cleanup_parent_workspaces(conn, child)

    assert wp.is_dir(), "a running parent's workspace must not be reaped"
    assert (wp / "deliverable.md").exists()


def test_done_parent_workspace_is_reaped_after_children_finish(kanban_home, monkeypatch):
    """Positive control: a finished parent with no live PID is reclaimed."""
    monkeypatch.setattr(kb, "_is_managed_scratch_path", lambda _p: True)
    with kb.connect() as conn:
        parent, child, wp = _scratch_parent_with_child(
            conn, parent_status="done", workspaces_root=kanban_home / "ws",
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
        conn.commit()
        kb._try_cleanup_parent_workspaces(conn, child)

    assert not wp.exists(), "a finished parent's workspace should still be reclaimed"


def test_done_parent_with_live_worker_is_deferred(kanban_home, monkeypatch):
    """Terminal status can still race a worker mid-teardown — hold the dir."""
    monkeypatch.setattr(kb, "_is_managed_scratch_path", lambda _p: True)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 4242)
    with kb.connect() as conn:
        parent, child, wp = _scratch_parent_with_child(
            conn,
            parent_status="done",
            workspaces_root=kanban_home / "ws",
            worker_pid=4242,
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
        conn.commit()
        kb._try_cleanup_parent_workspaces(conn, child)

    assert wp.is_dir(), "a terminal parent with a live worker_pid must not be reaped"
    assert (wp / "deliverable.md").exists()
