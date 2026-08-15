"""Custody proofs for deferred parent scratch cleanup."""

from __future__ import annotations

import sqlite3
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


def _managed_workspace(
    conn,
    kanban_home: Path,
    task_id: str,
) -> Path:
    root = kanban_home / "kanban" / "workspaces"
    workspace = root / task_id
    workspace.mkdir(parents=True)
    (workspace / "custody.txt").write_text("owned", encoding="utf-8")
    kb.set_workspace_path(conn, task_id, workspace)
    return workspace


def test_completion_captures_live_worker_before_custody_is_cleared(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable row, not the post-completion task row, owns PID custody."""
    live = {424242}
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) in live)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="live worker", assignee="worker")
        assert kb.claim_task(conn, task_id, claimer="dispatcher") is not None
        kb._set_worker_pid(conn, task_id, 424242)
        workspace = _managed_workspace(conn, kanban_home, task_id)

        assert kb.complete_task(conn, task_id, result="done") is True
        reservation = conn.execute(
            "SELECT state, worker_pid FROM workspace_cleanup_reservations "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert reservation is not None
        assert reservation["state"] == "pending"
        assert reservation["worker_pid"] == 424242
        assert workspace.is_dir()

        live.clear()
        assert kb.recover_workspace_cleanups(conn) == [task_id]
        state = conn.execute(
            "SELECT state FROM workspace_cleanup_reservations WHERE task_id = ?",
            (task_id,),
        ).fetchone()["state"]

    assert state == "completed"
    assert not workspace.exists()


def test_direct_terminal_status_captures_custody_before_run_close(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_status closes runs first internally, so it explicitly pre-reserves."""
    live = {515151}
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) in live)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="manual archive", assignee="worker")
        workspace = _managed_workspace(conn, kanban_home, task_id)
        assert kb.set_status(conn, task_id, "running") is True
        kb._set_worker_pid(conn, task_id, 515151)
        before = kb.get_task(conn, task_id)
        assert before is not None and before.current_run_id is not None

        assert kb.set_status(conn, task_id, "archived") is True
        reservation = conn.execute(
            "SELECT state, worker_pid, run_id FROM workspace_cleanup_reservations "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert dict(reservation) == {
            "state": "pending",
            "worker_pid": 515151,
            "run_id": before.current_run_id,
        }
        assert workspace.exists()

        live.clear()
        assert kb.recover_workspace_cleanups(conn) == [task_id]

    assert not workspace.exists()


def test_unexpired_claim_without_pid_defers_until_lease_expires(
    kanban_home: Path,
) -> None:
    """A claim-to-spawn race has custody even before a PID is published."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="claimed before spawn")
        workspace = _managed_workspace(conn, kanban_home, task_id)
        assert kb.claim_task(
            conn, task_id, claimer="dispatcher", ttl_seconds=3600,
        ) is not None
        assert kb.complete_task(conn, task_id, result="done") is True
        reservation = conn.execute(
            "SELECT state, worker_pid, claim_lock, claim_expires, last_error "
            "FROM workspace_cleanup_reservations WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert reservation["state"] == "pending"
        assert reservation["worker_pid"] is None
        assert reservation["claim_lock"] == "dispatcher"
        assert reservation["claim_expires"] is not None
        assert "unexpired" in reservation["last_error"]
        assert workspace.exists()

        conn.execute(
            "UPDATE workspace_cleanup_reservations SET claim_expires = 0 "
            "WHERE task_id = ?",
            (task_id,),
        )
        conn.commit()
        assert kb.recover_workspace_cleanups(conn) == [task_id]

    assert not workspace.exists()


def test_cleanup_recovers_crash_after_rename_and_never_retries_success(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost post-rename DB update is recoverable without reusing source path."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="rename crash")
        workspace = _managed_workspace(conn, kanban_home, task_id)
        original_update = kb._update_workspace_cleanup_reservation
        dropped = False

        def lose_first_rename_commit(*args, **kwargs):
            nonlocal dropped
            if kwargs.get("state") == "renamed" and not dropped:
                dropped = True
                return False
            return original_update(*args, **kwargs)

        monkeypatch.setattr(
            kb, "_update_workspace_cleanup_reservation", lose_first_rename_commit,
        )
        assert kb.complete_task(conn, task_id, result="done") is True
        reservation = conn.execute(
            "SELECT token, state FROM workspace_cleanup_reservations WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        quarantine = workspace.parent / f".hermes-cleanup-{reservation['token']}"
        assert reservation["state"] == "pending"
        assert not workspace.exists()
        assert quarantine.is_dir()

        monkeypatch.setattr(kb, "_update_workspace_cleanup_reservation", original_update)
        assert kb.recover_workspace_cleanups(conn) == [task_id]
        assert not quarantine.exists()

        # Recreating the old pathname after success must never authorize a
        # second delete: recovery consults only pending/renamed generations.
        workspace.mkdir()
        (workspace / "new-owner.txt").write_text("new", encoding="utf-8")
        assert kb.recover_workspace_cleanups(conn) == []

    assert (workspace / "new-owner.txt").read_text(encoding="utf-8") == "new"


def test_malformed_reservation_refuses_without_wedging_positive_control(
    kanban_home: Path,
) -> None:
    """An unsafe path is terminally refused; a following valid cleanup works."""
    outside = kanban_home / "source-repository"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    with kb.connect() as conn:
        unsafe = kb.create_task(
            conn,
            title="unsafe",
            workspace_kind="scratch",
            workspace_path=str(outside),
        )
        assert kb.complete_task(conn, unsafe, result="done") is True
        refused = conn.execute(
            "SELECT state, last_error FROM workspace_cleanup_reservations "
            "WHERE task_id = ?",
            (unsafe,),
        ).fetchone()
        assert refused["state"] == "refused"
        assert "outside managed scratch" in refused["last_error"]

        valid = kb.create_task(conn, title="positive control")
        valid_workspace = _managed_workspace(conn, kanban_home, valid)
        assert kb.complete_task(conn, valid, result="done") is True

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not valid_workspace.exists()


def test_nul_path_is_terminally_refused_not_retried(
    kanban_home: Path,
) -> None:
    """A path API cannot evaluate must not remain an eternal pending row."""
    del kanban_home
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="nul path",
            workspace_kind="scratch",
            workspace_path="/tmp/managed\x00suffix",
        )
        assert kb.complete_task(conn, task_id, result="done") is True
        reservation = conn.execute(
            "SELECT state, attempts, last_error "
            "FROM workspace_cleanup_reservations WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert reservation["state"] == "refused"
        attempts = reservation["attempts"]
        assert reservation["last_error"].startswith("refused:")
        assert kb.recover_workspace_cleanups(conn) == []
        after = conn.execute(
            "SELECT state, attempts FROM workspace_cleanup_reservations "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    assert dict(after) == {"state": "refused", "attempts": attempts}


def test_raw_sql_terminal_transition_reserves_and_delete_fails_closed(
    kanban_home: Path,
) -> None:
    """Triggers cover broker/raw SQL paths, including pre-transition PID."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="raw lifecycle")
        workspace = _managed_workspace(conn, kanban_home, task_id)
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=919191 WHERE id=?",
            (task_id,),
        )
        conn.commit()
        conn.execute(
            "UPDATE tasks SET status='done', worker_pid=NULL WHERE id=?",
            (task_id,),
        )
        conn.commit()
        reservation = conn.execute(
            "SELECT state, worker_pid FROM workspace_cleanup_reservations "
            "WHERE task_id=?",
            (task_id,),
        ).fetchone()
        assert reservation["state"] == "pending"
        assert reservation["worker_pid"] == 919191

        with pytest.raises(sqlite3.IntegrityError, match="cleanup is not complete"):
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.rollback()
        assert workspace.exists()


def test_hard_delete_waits_for_reserved_workspace_cleanup(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical hard delete cannot discard the row while a worker owns cwd."""
    live = {626262}
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) in live)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="delete waits", assignee="worker")
        workspace = _managed_workspace(conn, kanban_home, task_id)
        assert kb.claim_task(conn, task_id, claimer="dispatcher") is not None
        kb._set_worker_pid(conn, task_id, 626262)

        assert kb.delete_task(conn, task_id) is False
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "archived"
        assert workspace.exists()

        live.clear()
        assert kb.recover_workspace_cleanups(conn) == [task_id]
        assert kb.delete_task(conn, task_id) is True
        assert kb.get_task(conn, task_id) is None

    assert not workspace.exists()


def test_pending_reservation_freezes_path_and_reopen_mutations(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No writer path can retarget or reopen a task during cleanup custody."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == 737373)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="frozen reservation", assignee="worker")
        workspace = _managed_workspace(conn, kanban_home, task_id)
        assert kb.claim_task(conn, task_id, claimer="dispatcher") is not None
        kb._set_worker_pid(conn, task_id, 737373)
        assert kb.complete_task(conn, task_id, result="done") is True

        with pytest.raises(
            sqlite3.IntegrityError, match="cleanup reservation is in flight",
        ):
            kb.set_workspace_path(conn, task_id, workspace.parent / "retargeted")
        with pytest.raises(
            sqlite3.IntegrityError, match="cleanup reservation is in flight",
        ):
            kb.set_status(conn, task_id, "ready")
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.status == "done"
    assert task.workspace_path == str(workspace)
    assert workspace.exists()


def test_filesystem_rename_occurs_outside_write_transaction(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boardd 2s transaction budget never encloses filesystem authority."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="transaction boundary")
        workspace = _managed_workspace(conn, kanban_home, task_id)
        real_rename = Path.rename
        observed: list[bool] = []

        def checked_rename(path: Path, target: Path):
            observed.append(conn.in_transaction)
            return real_rename(path, target)

        monkeypatch.setattr(Path, "rename", checked_rename)
        assert kb.complete_task(conn, task_id, result="done") is True

    assert observed == [False]
    assert not workspace.exists()
