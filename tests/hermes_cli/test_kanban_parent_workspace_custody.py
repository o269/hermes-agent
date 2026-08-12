"""Custody proofs for deferred parent scratch cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _parent_and_terminal_child(
    conn,
    *,
    parent_status: str,
    workspaces_root: Path,
) -> tuple[str, str, Path]:
    parent_id = kb.create_task(conn, title="parent", assignee="worker")
    child_id = kb.create_task(
        conn,
        title="child",
        assignee="worker",
        parents=[parent_id],
    )
    workspace = workspaces_root / parent_id
    workspace.mkdir(parents=True)
    (workspace / "deliverable.md").write_text("still in custody", encoding="utf-8")
    conn.execute(
        "UPDATE tasks SET workspace_kind='scratch', workspace_path=?, status=? "
        "WHERE id=?",
        (str(workspace), parent_status, parent_id),
    )
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child_id,))
    conn.commit()
    return parent_id, child_id, workspace


def test_running_parent_workspace_survives_child_completion(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The destructive path must not act on a running parent."""
    monkeypatch.setattr(kb, "_is_managed_scratch_path", lambda _path: True)
    with kb.connect() as conn:
        _parent_id, child_id, workspace = _parent_and_terminal_child(
            conn,
            parent_status="running",
            workspaces_root=kanban_home / "workspaces",
        )
        kb._try_cleanup_parent_workspaces(conn, child_id)

    assert workspace.is_dir()
    assert (workspace / "deliverable.md").read_text(encoding="utf-8") == (
        "still in custody"
    )


def test_terminal_parent_with_live_worker_is_deferred(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal status alone cannot authorize deletion during teardown."""
    monkeypatch.setattr(kb, "_is_managed_scratch_path", lambda _path: True)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 424242)
    with kb.connect() as conn:
        parent_id, child_id, workspace = _parent_and_terminal_child(
            conn,
            parent_status="done",
            workspaces_root=kanban_home / "workspaces",
        )
        conn.execute(
            "UPDATE tasks SET worker_pid=? WHERE id=?",
            (424242, parent_id),
        )
        conn.commit()
        kb._try_cleanup_parent_workspaces(conn, child_id)

    assert workspace.is_dir()


def test_finished_parent_without_live_worker_is_reaped(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the safety fence does not disable valid cleanup."""
    monkeypatch.setattr(kb, "_is_managed_scratch_path", lambda _path: True)
    with kb.connect() as conn:
        _parent_id, child_id, workspace = _parent_and_terminal_child(
            conn,
            parent_status="done",
            workspaces_root=kanban_home / "workspaces",
        )
        kb._try_cleanup_parent_workspaces(conn, child_id)

    assert not workspace.exists()
