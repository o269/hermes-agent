"""Tests for the Kanban DB layer (hermes_cli.kanban_db)."""

from __future__ import annotations

import concurrent.futures
import contextlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def continuation_runtime_stubs(monkeypatch):
    """Keep continuation tests hermetic while production APIs own verifiers."""
    monkeypatch.setattr(kb, "_default_github_pr_verifier", _open_draft_pr)
    monkeypatch.setattr(
        kb,
        "_default_profile_provider_resolver",
        lambda _profile: "openai-codex",
    )
    # Production requires the caller to run inside the root gateway's
    # ephemeral control-plane context AND to prove ownership of the retained
    # gateway runtime lock for the board root. Tests model pytest as that
    # control plane; the double-fork / forged-pid regressions deliberately
    # escape it and must remain denied.
    monkeypatch.setattr(kb, "_operator_control_plane_active", lambda: True)
    monkeypatch.setattr(kb, "_operator_gateway_lock_owned", lambda _root: True)


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------

def test_init_db_is_idempotent(kanban_home):
    # Second call should not error or drop data.
    with kb.connect() as conn:
        kb.create_task(conn, title="persisted")
    kb.init_db()
    with kb.connect() as conn:
        tasks = kb.list_tasks(conn)
    assert len(tasks) == 1
    assert tasks[0].title == "persisted"


def test_init_creates_expected_tables(kanban_home):
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert {"tasks", "task_links", "task_comments", "task_events"} <= names


def test_connect_honors_kanban_busy_timeout_env(kanban_home, monkeypatch):
    """All kanban connections should use the explicit busy-timeout knob.

    A worker stampede should wait for SQLite's writer lock instead of failing
    immediately with ``database is locked`` during first-connect/WAL/schema
    setup.  The timeout must be queryable via PRAGMA so CLI, gateway, and tool
    connections behave the same way.
    """
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "123456")

    with kb.connect() as conn:
        row = conn.execute("PRAGMA busy_timeout").fetchone()

    assert row[0] == 123456


def test_cross_process_init_lock_uses_windows_byte_range_lock(tmp_path, monkeypatch):
    """Windows must use a real (non-blocking) process lock, not a no-op open.

    The init lock acquires with LK_NBLCK in a bounded retry loop (#36644) so a
    wedged holder can never block connect() forever; a clean acquire takes the
    lock once and releases it once.
    """
    calls: list[tuple[int, int, int]] = []
    fake_msvcrt = types.SimpleNamespace(
        LK_NBLCK=3,
        LK_UNLCK=2,
        locking=lambda fd, mode, nbytes: calls.append((fd, mode, nbytes)),
    )
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    db_path = tmp_path / "kanban.db"
    with kb._cross_process_init_lock(db_path):
        # Acquired exactly once via the non-blocking byte-range lock.
        assert [call[1:] for call in calls] == [(fake_msvcrt.LK_NBLCK, 1)]

    # Released once on exit.
    assert [call[1:] for call in calls] == [
        (fake_msvcrt.LK_NBLCK, 1),
        (fake_msvcrt.LK_UNLCK, 1),
    ]


def test_connect_rejects_tls_record_in_sqlite_header(tmp_path, monkeypatch):
    """Kanban should classify TLS-looking page-0 clobbers before WAL setup."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    corrupt = home / "kanban.db"
    corrupt.write_bytes(b"SQLit" + bytes.fromhex("17 03 03 00 13") + b"x" * 32)

    with pytest.raises(sqlite3.DatabaseError) as exc_info:
        kb.connect(board="default")

    msg = str(exc_info.value)
    assert "file is not a database" in msg
    assert "TLS record header detected at byte offset 5" in msg
    assert "53 51 4c 69 74 17 03 03 00 13" in msg


def test_connect_migrates_legacy_db_before_optional_column_indexes(tmp_path):
    """Legacy DBs missing additive indexed columns must migrate cleanly.

    SCHEMA_SQL runs in ``connect()`` before ``_migrate_add_optional_columns``.
    Indexes over additive columns therefore must be created after the
    migration adds those columns, or boards predating the column fail to
    open before migration can run.

    Covers all four indexes that sit on additive columns:
    - ``tasks.session_id``       -> ``idx_tasks_session_id``    (#28447)
    - ``tasks.tenant``           -> ``idx_tasks_tenant``        (#16081)
    - ``tasks.idempotency_key``  -> ``idx_tasks_idempotency``   (#17805)
    - ``task_events.run_id``     -> ``idx_events_run``          (#17805)
    """
    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(str(db_path))
    # Pre-#16081 ``tasks`` shape: missing tenant, idempotency_key, session_id.
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
    """)
    # Pre-#17805 ``task_events`` shape: missing run_id. Required because
    # ``_migrate_add_optional_columns`` unconditionally runs PRAGMA on
    # ``task_events`` for run_id back-fill.
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'old board task', 'ready', 1)"
    )
    conn.commit()
    conn.close()

    with kb.connect(db_path) as migrated:
        task_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")
        }
        event_columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(task_events)")
        }
        indexes = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    # Additive columns added by migration:
    assert "session_id" in task_columns
    assert "tenant" in task_columns
    assert "idempotency_key" in task_columns
    assert "run_id" in event_columns
    # And their indexes — the regression scope of this test:
    assert "idx_tasks_session_id" in indexes
    assert "idx_tasks_tenant" in indexes
    assert "idx_tasks_idempotency" in indexes
    assert "idx_events_run" in indexes


# ---------------------------------------------------------------------------
# Task creation + status inference
# ---------------------------------------------------------------------------

def test_create_task_no_parents_is_ready(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="alice")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.status == "ready"
    assert t.assignee == "alice"
    assert t.workspace_kind == "scratch"


def test_create_task_with_parent_is_todo_until_parent_done(kanban_home):
    with kb.connect() as conn:
        p = kb.create_task(conn, title="parent")
        c = kb.create_task(conn, title="child", parents=[p])
        assert kb.get_task(conn, c).status == "todo"
        kb.complete_task(conn, p, result="ok")
        assert kb.get_task(conn, c).status == "ready"


def test_create_task_unknown_parent_errors(kanban_home):
    with kb.connect() as conn, pytest.raises(ValueError, match="unknown parent"):
        kb.create_task(conn, title="orphan", parents=["t_ghost"])


def test_workspace_kind_validation(kanban_home):
    with kb.connect() as conn, pytest.raises(ValueError, match="workspace_kind"):
        kb.create_task(conn, title="bad ws", workspace_kind="cloud")


def test_create_task_persists_worktree_branch_name(kanban_home, tmp_path):
    target = tmp_path / ".worktrees" / "t6-wire"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="ship worktree",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name=" wt/t6-wire ",
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        context = kb.build_worker_context(conn, tid)

    assert task.branch_name == "wt/t6-wire"
    assert events[0].payload["branch_name"] == "wt/t6-wire"
    assert "Branch:   wt/t6-wire" in context


def test_branch_name_requires_worktree_workspace(kanban_home):
    with kb.connect() as conn, pytest.raises(ValueError, match="worktree"):
        kb.create_task(
            conn,
            title="bad branch",
            workspace_kind="scratch",
            branch_name="wt/bad",
        )


# ---------------------------------------------------------------------------
# Links + dependency resolution
# ---------------------------------------------------------------------------

def test_link_demotes_ready_child_to_todo_when_parent_not_done(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        assert kb.get_task(conn, b).status == "ready"
        kb.link_tasks(conn, a, b)
        assert kb.get_task(conn, b).status == "todo"


def test_link_keeps_ready_child_when_parent_already_done(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a")
        kb.complete_task(conn, a)
        b = kb.create_task(conn, title="b")
        assert kb.get_task(conn, b).status == "ready"
        kb.link_tasks(conn, a, b)
        assert kb.get_task(conn, b).status == "ready"


def test_link_rejects_self_loop(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a")
        with pytest.raises(ValueError, match="itself"):
            kb.link_tasks(conn, a, a)


def test_link_detects_cycle(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b", parents=[a])
        c = kb.create_task(conn, title="c", parents=[b])
        with pytest.raises(ValueError, match="cycle"):
            kb.link_tasks(conn, c, a)
        with pytest.raises(ValueError, match="cycle"):
            kb.link_tasks(conn, b, a)


def test_recompute_ready_cascades_through_chain(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b", parents=[a])
        c = kb.create_task(conn, title="c", parents=[b])
        assert [kb.get_task(conn, x).status for x in (a, b, c)] == \
               ["ready", "todo", "todo"]
        kb.complete_task(conn, a)
        assert kb.get_task(conn, b).status == "ready"
        kb.complete_task(conn, b)
        assert kb.get_task(conn, c).status == "ready"


def test_recompute_ready_promotes_blocked_with_done_parents(kanban_home):
    """blocked tasks with all parents done should be promoted to ready,
    unless the circuit-breaker failure limit has been reached."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        # Complete the parent
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        # Manually block the child with zero failures (simulates a
        # dependency block, not a circuit-breaker block).
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=0, "
            "last_failure_error=NULL WHERE id=?",
            (child,),
        )
        conn.commit()
        assert kb.get_task(conn, child).status == "blocked"
        # recompute_ready should promote blocked → ready
        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_recompute_ready_fan_in_waits_for_all_parents(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        c = kb.create_task(conn, title="c", parents=[a, b])
        kb.complete_task(conn, a)
        assert kb.get_task(conn, c).status == "todo"
        kb.complete_task(conn, b)
        assert kb.get_task(conn, c).status == "ready"


# ---------------------------------------------------------------------------
# Atomic claim (CAS)
# ---------------------------------------------------------------------------

def test_claim_once_wins_second_loses(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        first = kb.claim_task(conn, t, claimer="host:1")
        assert first is not None and first.status == "running"
        second = kb.claim_task(conn, t, claimer="host:2")
        assert second is None


def test_claim_uses_env_default_ttl(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "3600")
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t, claimer="host:1")
        expires = kb.get_task(conn, t).claim_expires
    assert expires is not None
    assert expires > int(time.time()) + 3000


def test_claim_event_preserves_active_worker_model(kanban_home):
    (kanban_home / "config.yaml").write_text(
        "model:\n  default: openai-codex/gpt-5.6-sol\n",
        encoding="utf-8",
    )
    kb._worker_model_for_home.cache_clear()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="model audit", assignee="a")
        assert kb.claim_task(conn, task_id, claimer="host:1") is not None
        claimed = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "claimed"
        ][-1]

    assert claimed.payload["model"] == "openai-codex/gpt-5.6-sol"


def test_claim_fails_on_non_ready(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        # Move to todo by introducing an unsatisfied parent.
        p = kb.create_task(conn, title="p")
        kb.link_tasks(conn, p, t)
        assert kb.get_task(conn, t).status == "todo"
        assert kb.claim_task(conn, t) is None


def test_schedule_task_parks_time_delay_without_dispatching(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="delayed recheck", assignee="ops")
        assert kb.schedule_task(conn, t, reason="run next week") is True
        task = kb.get_task(conn, t)
        assert task.status == "scheduled"
        assert kb.claim_task(conn, t) is None

        events = kb.list_events(conn, t)
        assert any(e.kind == "scheduled" and e.payload == {"reason": "run next week"} for e in events)


def test_unblock_scheduled_rechecks_parent_gate(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"
        assert kb.schedule_task(conn, child, reason="wait until tomorrow") is True

        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "todo"

        kb.complete_task(conn, parent)
        assert kb.schedule_task(conn, child, reason="second timer") is True
        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "ready"


def test_stale_claim_reclaimed(kanban_home, monkeypatch):
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        killed: list[int] = []

        def _signal(_pid, sig):
            killed.append(sig)

        kb._set_worker_pid(conn, t, 12345)
        # Rewind claim_expires so it looks stale.
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 3600, t),
        )
        # Worker PID has died. Reclaim the row, but never signal a PID without
        # a live, generation-verified process identity.
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        reclaimed = kb.release_stale_claims(conn, signal_fn=_signal)
        assert reclaimed == 1
        assert kb.get_task(conn, t).status == "ready"
        assert killed == []


def test_stale_claim_with_live_pid_extends_instead_of_reclaiming(
    kanban_home, monkeypatch,
):
    """A stale-by-TTL claim whose worker PID is still alive should be
    extended, not reclaimed (#23025). Slow models can spend longer than
    ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM call;
    killing those healthy workers produces a respawn loop with zero
    progress."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)

        old_expires = int(time.time()) - 60
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (old_expires, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        killed: list[int] = []
        reclaimed = kb.release_stale_claims(
            conn, signal_fn=lambda _p, sig: killed.append(sig),
        )
        assert reclaimed == 0
        task = kb.get_task(conn, t)
        assert task.status == "running"
        assert task.claim_expires is not None
        assert task.claim_expires > old_expires
        assert killed == []  # live worker not killed

        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (t,),
            ).fetchall()
        ]
        assert "claim_extended" in kinds
        assert "reclaimed" not in kinds


def test_stale_claim_with_live_pid_uses_env_ttl_override(
    kanban_home, monkeypatch,
):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "3600")

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 60, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 0

        task = kb.get_task(conn, t)
        assert task is not None
        assert task.claim_expires is not None
        assert task.claim_expires > int(time.time()) + 3000


def test_stale_claim_deferred_when_live_worker_survives_termination(
    kanban_home, monkeypatch,
):
    """A TTL-expired claim whose worker survives the kill must NOT be released.

    Releasing would let the dispatcher spawn a duplicate beside the still-alive
    worker — the runaway seen when a cgroup memory.high throttle parks a worker
    in uninterruptible (D) state, where a pending SIGKILL cannot land. The claim
    is held (extended) and retried next tick instead.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)

        old_expires = int(time.time()) - 60
        # Heartbeat stale by > 1h so the live-pid EXTEND branch is skipped and
        # the terminate path (the wedged-worker case) runs.
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, int(time.time()) - 7200, t),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": True,
                "host_local": True,
                "terminated": False,
            },
        )
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 0

        assert kb.get_task(conn, t).status == "running"
        worker_pid = conn.execute(
            "SELECT worker_pid FROM tasks WHERE id = ?", (t,),
        ).fetchone()[0]
        assert worker_pid == 12345  # worker not orphaned
        claim_expires = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (t,),
        ).fetchone()[0]
        assert claim_expires > old_expires  # claim held, not released

        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (t,),
            ).fetchall()
        ]
        assert "reclaim_deferred" in kinds
        assert "reclaimed" not in kinds


def test_stale_claim_reclaimed_when_termination_succeeds(
    kanban_home, monkeypatch,
):
    """When the worker is actually killed, the claim is released as before."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (int(time.time()) - 60, int(time.time()) - 7200, t),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": True,
                "host_local": True,
                "terminated": True,
            },
        )
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 1
        assert kb.get_task(conn, t).status == "ready"


def test_stale_claim_released_when_worker_not_host_local(
    kanban_home, monkeypatch,
):
    """The defer guard only holds OUR own surviving workers.

    A claim we cannot manage (different host, or no kill attempted) must still
    be released, otherwise a foreign-host claim could strand a task forever.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (int(time.time()) - 60, int(time.time()) - 7200, t),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": False,
                "host_local": False,
                "terminated": False,
            },
        )
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 1
        assert kb.get_task(conn, t).status == "ready"


def test_detect_stale_defers_when_live_worker_survives(kanban_home, monkeypatch):
    """detect_stale_running must also hold the claim when the worker survives."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="wedged", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = NULL "
                "WHERE id = ?",
                (five_hours_ago, t),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": True,
                "host_local": True,
                "terminated": False,
            },
        )
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == []
        assert kb.get_task(conn, t).status == "running"
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (t,),
            ).fetchall()
        ]
        assert "reclaim_deferred" in kinds


def test_stale_claim_reclaim_event_records_diagnostic_payload(
    kanban_home, monkeypatch,
):
    """``reclaimed`` events should carry claim_expires, last_heartbeat_at,
    and worker_pid so operators can diagnose why a claim went stale
    (#23025: previous payload only had ``stale_lock`` which gives no
    timing context)."""
    import json
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        old_expires = int(time.time()) - 3600
        hb_at = int(time.time()) - 1800
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, hb_at, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'reclaimed'",
            (t,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["claim_expires"] == old_expires
        assert payload["last_heartbeat_at"] == hb_at
        assert payload["worker_pid"] == 12345
        assert payload["host_local"] is True


def test_detect_crashed_workers_systemic_failure_fast_block(
    kanban_home, monkeypatch,
):
    """When many tasks crash with the same error, trip the breaker faster."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect() as conn:
        task_ids = []
        for i in range(4):
            tid = kb.create_task(conn, title=f"task-{i}", assignee="a")
            host = _kb._claimer_id().split(":", 1)[0]
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (90000 + i, f"{host}:w{i}", tid),
            )
            task_ids.append(tid)
        conn.commit()

        crashed = kb.detect_crashed_workers(conn)
        assert len(crashed) == 4

        for tid in task_ids:
            task = kb.get_task(conn, tid)
            assert task.status == "blocked", (
                f"task {tid} should be blocked (systemic), got {task.status}"
            )


def test_detect_crashed_workers_isolated_failure_normal_retry(
    kanban_home, monkeypatch,
):
    """Below the systemic threshold, tasks retain normal retry budget."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect() as conn:
        task_ids = []
        for i in range(2):
            tid = kb.create_task(conn, title=f"iso-{i}", assignee="a")
            host = _kb._claimer_id().split(":", 1)[0]
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (80000 + i, f"{host}:w{i}", tid),
            )
            task_ids.append(tid)
        conn.commit()

        crashed = kb.detect_crashed_workers(conn)
        assert len(crashed) == 2

        for tid in task_ids:
            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"task {tid} should stay ready (isolated), got {task.status}"
            )


def test_detect_crashed_workers_skips_freshly_claimed_tasks(
    kanban_home, monkeypatch,
):
    """Grace period prevents reclaim of freshly-started tasks."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.delenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", raising=False)

    now = 1_000_000.0
    monkeypatch.setattr(_kb.time, "time", lambda: now)

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="grace test", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, "
            "claim_lock=?, started_at=? WHERE id=?",
            (99999, f"{host}:w", int(now), tid),
        )
        conn.commit()

        # With time = now (just claimed), grace period should suppress reclaim.
        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed, "should not reclaim freshly-started task"

        # With time = now + 60 (past default 30s grace), should reclaim.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 60)
        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed, "should reclaim task past grace period"


def test_detect_crashed_workers_grace_period_env_override(
    kanban_home, monkeypatch,
):
    """HERMES_KANBAN_CRASH_GRACE_SECONDS env var adjusts the window."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "5")

    now = 2_000_000.0

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="env override test", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, "
            "claim_lock=?, started_at=? WHERE id=?",
            (99999, f"{host}:w", int(now), tid),
        )
        conn.commit()

        # 3s after claim: within 5s grace → no reclaim.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 3)
        assert tid not in kb.detect_crashed_workers(conn)

        # 6s after claim: past 5s grace → reclaim.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 6)
        assert tid in kb.detect_crashed_workers(conn)


def test_resolve_crash_grace_seconds_handles_bad_env(monkeypatch):
    """Bad env values fall back to DEFAULT_CRASH_GRACE_SECONDS."""
    import hermes_cli.kanban_db as _kb

    for bad_val in ("notanumber", "-5", ""):
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", bad_val)
        result = _kb._resolve_crash_grace_seconds()
        assert result == _kb.DEFAULT_CRASH_GRACE_SECONDS, (
            f"expected default for {bad_val!r}, got {result}"
        )


# ---------------------------------------------------------------------------
# Rate-limit requeue: a worker that bails on a provider quota wall must be
# released back to ``ready`` WITHOUT counting a failure, so a long (e.g.
# 5-hour) quota window can't trip the circuit breaker and permanently block
# the card. The respawn guard then defers it on a cooldown until quota
# returns. Regression coverage for the kanban-rate-limit-failure report.
# ---------------------------------------------------------------------------


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


def test_classify_worker_exit_recognizes_rate_limit_sentinel(kanban_home):
    import hermes_cli.kanban_db as _kb

    pid = 31337
    _kb._record_worker_exit(pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    kind, code = _kb._classify_worker_exit(pid)
    assert kind == "rate_limited"
    assert code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE

    # Plain non-zero exit is still a normal crash, not rate-limited.
    _kb._record_worker_exit(pid + 1, _exited_status(1))
    assert _kb._classify_worker_exit(pid + 1) == ("nonzero_exit", 1)


def test_rate_limit_exit_requeues_without_counting_failure(
    kanban_home, monkeypatch,
):
    """A rate-limit sentinel exit releases the task to ``ready`` and leaves
    ``consecutive_failures`` untouched — the breaker must never trip on a
    transient throttle, even across many quota-wall hits."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="rl", assignee="a")

        # Simulate FAR more quota-wall hits than DEFAULT_FAILURE_LIMIT (2).
        # If any of these counted as a failure the task would be blocked.
        for i in range(6):
            pid = 70000 + i
            # Claim to open a real run (so detect_crashed_workers can close
            # it with a rate_limited outcome), then point the claim at this
            # host + a dead pid so the crash path acts on it.
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute(
                "UPDATE tasks SET worker_pid=?, consecutive_failures=? "
                "WHERE id=?",
                (pid, 0, tid),
            )
            conn.commit()
            _kb._record_worker_exit(
                pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE)
            )

            crashed = kb.detect_crashed_workers(conn)
            # Rate-limited requeues are NOT crashes.
            assert tid not in crashed
            rl = getattr(_kb.detect_crashed_workers, "_last_rate_limited", [])
            assert tid in rl

            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"hit {i}: should requeue ready, got {task.status}"
            )
            assert task.consecutive_failures == 0, (
                f"hit {i}: rate-limit must not count a failure, "
                f"got {task.consecutive_failures}"
            )

        # Last failure error stamped so the respawn guard recognizes the
        # quota wall.
        assert task.last_failure_error and "rate-limited" in task.last_failure_error

        # A ``rate_limited`` run outcome was recorded (not ``crashed``).
        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,),
            ).fetchall()
        ]
        assert "rate_limited" in outcomes
        assert "crashed" not in outcomes


def test_real_crash_still_counts_and_trips_breaker(kanban_home, monkeypatch):
    """Sanity: a genuine non-zero crash (not the sentinel) still increments
    the failure counter and trips the breaker — the rate-limit carve-out is
    surgical, not a blanket "never count crashes"."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="crash", assignee="a")

        for i in range(2):  # DEFAULT_FAILURE_LIMIT == 2
            pid = 60000 + i
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (pid, f"{host}:w{i}", tid),
            )
            conn.commit()
            _kb._record_worker_exit(pid, _exited_status(1))  # generic failure
            kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"genuine crashes should still trip the breaker, got {task.status}"
        )


def test_respawn_guard_defers_rate_limited_within_cooldown(
    kanban_home, monkeypatch,
):
    """Within the cooldown after a rate-limit requeue, the guard defers the
    respawn; after the cooldown it allows a probe — and crucially does NOT
    fall into ``blocker_auth`` (which would defer forever)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rl-guard", assignee="a")
        # Seed a rate_limited run that just ended + the stamped error.
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall) — requeued", tid),
        )
        conn.commit()

        # Inside cooldown → defer with the rate-limit-specific reason.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        decision = kb.evaluate_respawn_guard(conn, tid)
        assert decision.reason == "rate_limit_cooldown"
        assert decision.detail == {
            "expires_at": now + 300,
            "window_seconds": 300,
        }

        # Past cooldown → allowed (None), NOT trapped by blocker_auth even
        # though last_failure_error contains "rate-limited".
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kb.check_respawn_guard(conn, tid) is None


def test_respawn_guard_rate_limit_cooldown_zero_allows_immediately(
    kanban_home, monkeypatch,
):
    """Cooldown of 0 disables the wait — task is spawnable on the next tick,
    and the stamped rate-limit text does not re-trap it via blocker_auth."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    now = 6_000_000

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rl-zero", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall)", tid),
        )
        conn.commit()

        monkeypatch.setattr(_kb.time, "time", lambda: now + 1)
        assert kb.check_respawn_guard(conn, tid) is None


def test_resolve_rate_limit_cooldown_handles_bad_env(monkeypatch):
    import hermes_cli.kanban_db as _kb

    for bad_val in ("notanumber", "-5", ""):
        monkeypatch.setenv(
            "HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", bad_val
        )
        assert (
            _kb._resolve_rate_limit_cooldown_seconds()
            == _kb.DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
        )


def test_max_runtime_uses_current_run_start_after_retry(kanban_home, monkeypatch):
    """A retry should get a fresh max-runtime window.

    ``tasks.started_at`` intentionally records the first time the task ever
    started. Runtime enforcement must therefore use the active
    ``task_runs.started_at`` row; otherwise every retry of an old task is
    immediately timed out again.
    """
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        t = kb.create_task(
            conn, title="retry", assignee="a", max_runtime_seconds=10,
        )

        kb.claim_task(conn, t, claimer=f"{host}:first")
        first_run_id = kb.latest_run(conn, t).id
        old_started = int(time.time()) - 20
        conn.execute(
            "UPDATE tasks SET started_at = ?, worker_pid = ? WHERE id = ?",
            (old_started, 999999, t),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ?, worker_pid = ? WHERE id = ?",
            (old_started, 999999, first_run_id),
        )

        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None)
        assert timed_out == [t]
        assert kb.get_task(conn, t).status == "ready"

        kb.claim_task(conn, t, claimer=f"{host}:retry")
        retry_run = kb.latest_run(conn, t)
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (999999, t),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (999999, retry_run.id),
        )

        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None)
        assert timed_out == []
        assert kb.get_task(conn, t).status == "running"


def test_heartbeat_extends_claim(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        claimer = "host:hb"
        kb.claim_task(conn, t, claimer=claimer, ttl_seconds=60)
        original = kb.get_task(conn, t).claim_expires
        # Rewind then heartbeat.
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (0, t))
        ok = kb.heartbeat_claim(conn, t, claimer=claimer, ttl_seconds=3600)
        assert ok
        new = kb.get_task(conn, t).claim_expires
        assert new > int(time.time()) + 3000


def test_heartbeat_uses_env_default_ttl(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "3600")
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        claimer = "host:hb"
        kb.claim_task(conn, t, claimer=claimer, ttl_seconds=60)
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (0, t))
        ok = kb.heartbeat_claim(conn, t, claimer=claimer)
        assert ok
        new = kb.get_task(conn, t).claim_expires
        assert new is not None
        assert new > int(time.time()) + 3000


def test_concurrent_claims_only_one_wins(kanban_home):
    """Fire N threads claiming the same task; exactly one must win."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="race", assignee="a")

    def attempt(i):
        with kb.connect() as c:
            return kb.claim_task(c, t, claimer=f"host:{i}")

    n_workers = 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        results = list(ex.map(attempt, range(n_workers)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].status == "running"


# ---------------------------------------------------------------------------
# Complete / block / unblock / archive / assign
# ---------------------------------------------------------------------------

def test_complete_records_result(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        assert kb.complete_task(conn, t, result="done and dusted")
        task = kb.get_task(conn, t)
    assert task.status == "done"
    assert task.result == "done and dusted"
    assert task.completed_at is not None


def test_block_then_unblock(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        assert kb.get_task(conn, t).status == "blocked"
        assert kb.unblock_task(conn, t)
        assert kb.get_task(conn, t).status == "ready"


def test_unblock_resets_failure_counters(kanban_home):
    """unblock_task must reset consecutive_failures and last_failure_error."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        # Simulate accumulated failures from the circuit breaker
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 5, "
            "last_failure_error = 'test error' WHERE id = ?",
            (t,),
        )
        conn.commit()
        assert kb.unblock_task(conn, t)
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_recompute_ready_skips_tasks_at_failure_limit(kanban_home):
    """recompute_ready must not auto-recover tasks whose consecutive_failures
    has reached the circuit-breaker limit (#35072).

    Without this guard, a task that repeatedly exhausts its iteration
    budget would cycle forever: block → auto-recover (counter reset)
    → respawn → budget exhausted → block → …
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a",
                               parents=[parent])
        # Complete the parent so the child's dependencies are satisfied.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, summary="done")

        # Simulate the child having exhausted its budget twice,
        # hitting the default failure limit (2).
        kb.claim_task(conn, child)
        kb._record_task_failure(
            conn, child, error="budget exhausted 1",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        kb._record_task_failure(
            conn, child, error="budget exhausted 2",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        task = kb.get_task(conn, child)
        assert task.status == "blocked"
        assert task.consecutive_failures >= 2

        # recompute_ready must NOT promote this task — the circuit
        # breaker has tripped and it should stay blocked.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"

        # Explicit unblock should still work and reset the counter.
        assert kb.unblock_task(conn, child)
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.consecutive_failures == 0


def test_recompute_ready_recovers_below_limit(kanban_home):
    """recompute_ready auto-recovers blocked tasks that haven't hit the
    failure limit yet — the counter is preserved across recovery."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="task", assignee="a")
        kb.claim_task(conn, t)
        # One failure, below the default limit of 2.
        kb._record_task_failure(
            conn, t, error="budget exhausted 1",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 1

        # Simulate being blocked by something else (not circuit breaker).
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        # Counter must be preserved, not reset.
        assert task.consecutive_failures == 1


def test_recompute_ready_honours_dispatcher_failure_limit(kanban_home):
    """The guard's effective limit must follow the same resolution order
    as the circuit breaker (#35072): per-task max_retries → dispatcher
    failure_limit → DEFAULT_FAILURE_LIMIT.

    Without threading the dispatcher's ``kanban.failure_limit`` through,
    the guard falls back to DEFAULT_FAILURE_LIMIT and disagrees with the
    breaker — sticking a task prematurely (config limit > default) or
    letting a tripped task escape (config limit < default).
    """
    with kb.connect() as conn:
        # Config allows MORE retries than the default. A task blocked
        # with failures below the configured limit must still recover.
        t = kb.create_task(conn, title="lenient", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=? "
            "WHERE id=?",
            (kb.DEFAULT_FAILURE_LIMIT, t),
        )
        conn.commit()
        # Default-limit call would stick it (failures >= default).
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"
        # Dispatcher configured a higher limit → recover, preserve counter.
        promoted = kb.recompute_ready(
            conn, failure_limit=kb.DEFAULT_FAILURE_LIMIT + 2
        )
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT

        # Config allows FEWER retries than the default. A task at the
        # stricter limit must stay blocked even though it's below default.
        t2 = kb.create_task(conn, title="strict", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1 "
            "WHERE id=?",
            (t2,),
        )
        conn.commit()
        # Default-limit (2) would recover it (1 < 2).
        # Stricter config limit (1) must keep it blocked (1 >= 1).
        assert kb.recompute_ready(conn, failure_limit=1) == 0
        assert kb.get_task(conn, t2).status == "blocked"


def test_recompute_ready_per_task_max_retries_overrides_dispatcher(kanban_home):
    """A per-task ``max_retries`` wins over the dispatcher failure_limit,
    matching ``_record_task_failure``'s resolution order."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="per-task", assignee="a")
        # Per-task allows 4 retries; dispatcher config says 2.
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=2, "
            "max_retries=4 WHERE id=?",
            (t,),
        )
        conn.commit()
        # failures(2) < per-task limit(4) → recover, despite dispatcher=2.
        promoted = kb.recompute_ready(conn, failure_limit=2)
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 2


# ---------------------------------------------------------------------------
# Parent-completion invariant at the claim gate (RCA t_a6acd07d)
# ---------------------------------------------------------------------------

def test_claim_rejects_when_parents_not_done(kanban_home):
    """claim_task must refuse ready->running if any parent isn't 'done'.

    Simulates the create-then-link race: a task gets status='ready' via a
    racy writer while it still has undone parents. The claim gate must
    detect the violation, demote the child back to 'todo', append a
    'claim_rejected' event, and return None. Covers Fix 1 of the RCA.
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        # Child correctly starts 'todo' because parent is not 'done'.
        assert kb.get_task(conn, child).status == "todo"
        # Simulate the race: a racy writer force-promotes the child to
        # 'ready' while parent is still pending.
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (child,),
        )
        conn.commit()
        assert kb.get_task(conn, child).status == "ready"

        result = kb.claim_task(conn, child, claimer="host:1")

    assert result is None
    with kb.connect() as conn:
        assert kb.get_task(conn, child).status == "todo"
        events = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? ORDER BY id",
            (child,),
        ).fetchall()
    kinds = [e["kind"] for e in events]
    assert "claim_rejected" in kinds
    # No 'claimed' event was emitted for the blocked attempt.
    assert "claimed" not in kinds


def test_claim_succeeds_once_parents_done(kanban_home):
    """After parents complete, recompute_ready -> claim_task must succeed."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        kb.claim_task(conn, parent)
        assert kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        claimed = kb.claim_task(conn, child, claimer="host:1")
    assert claimed is not None
    assert claimed.status == "running"


def test_create_with_parents_stays_todo_until_parents_done(kanban_home):
    """kanban_create(parents=[...]) must land in 'todo' and only promote on parent done."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        assert kb.get_task(conn, child).status == "todo"
        # Dispatcher tick between create and some later event must NOT
        # produce a winner for this child.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "todo"
        # Complete parent; complete_task internally runs recompute_ready,
        # which promotes the child to 'ready'.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        assert kb.get_task(conn, child).status == "ready"


def test_unblock_with_pending_parents_goes_to_todo(kanban_home):
    """unblock_task must re-gate on parent completion (Fix 3).

    A task blocked while parents are still in progress must return to
    'todo' (not 'ready') on unblock. Otherwise the dispatcher will claim
    it immediately, repeating Bug 2 from the RCA.
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        # Force child into 'blocked' regardless of parent progress
        # (simulates a worker that self-blocked, or an operator block).
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?", (child,),
        )
        conn.commit()
        assert kb.unblock_task(conn, child)
        assert kb.get_task(conn, child).status == "todo"
        # After parent completes + recompute, the child is ready.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_unblock_without_parents_goes_to_ready(kanban_home):
    """Parent-free unblock still produces 'ready' (behavior preserved)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="lone", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        assert kb.unblock_task(conn, t)
        assert kb.get_task(conn, t).status == "ready"


def test_assign_refuses_while_running(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        with pytest.raises(RuntimeError, match="currently running"):
            kb.assign_task(conn, t, "b")


def test_assign_reassigns_when_not_running(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        assert kb.assign_task(conn, t, "b")
        assert kb.get_task(conn, t).assignee == "b"


def test_assignee_normalized_to_lowercase_on_create_and_assign(kanban_home):
    """Dashboard/CLI may pass title-cased profile labels; DB + spawn use canonical id."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="cased", assignee="Jules")
        assert kb.get_task(conn, tid).assignee == "jules"
        assert kb.assign_task(conn, tid, "Librarian")
        assert kb.get_task(conn, tid).assignee == "librarian"


def test_list_tasks_assignee_filter_case_insensitive(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="q", assignee="jules")
        found = kb.list_tasks(conn, assignee="Jules")
        assert len(found) == 1 and found[0].id == tid


def test_archive_hides_from_default_list(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        kb.complete_task(conn, t)
        assert kb.archive_task(conn, t)
        assert len(kb.list_tasks(conn)) == 0
        assert len(kb.list_tasks(conn, include_archived=True)) == 1


def test_delete_archived_task_removes_related_rows(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        tid = kb.create_task(conn, title="child", parents=[parent], assignee="worker")
        kb.add_comment(conn, tid, "user", "cleanup me")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="done")
        assert kb.archive_task(conn, tid)
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, created_at, last_event_id) "
            "VALUES (?, 'telegram', '123', '', 'u', 0, 0)",
            (tid,),
        )
        conn.commit()

        assert kb.delete_archived_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id = ? OR parent_id = ?", (tid, tid)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone()[0] == 0


def test_delete_archived_task_rejects_non_archived_rows(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="live")
        assert kb.delete_archived_task(conn, tid) is False
        assert kb.get_task(conn, tid) is not None


def test_list_tasks_order_by(kanban_home):
    with kb.connect() as conn:
        # Create tasks with different titles and priorities
        t_a = kb.create_task(conn, title="alpha", priority=1)
        t_b = kb.create_task(conn, title="beta", priority=2)
        t_c = kb.create_task(conn, title="gamma", priority=1)

        # Default sort: priority DESC, created ASC
        default = kb.list_tasks(conn)
        assert [t.id for t in default] == [t_b, t_a, t_c]

        # Sort by title ASC
        by_title = kb.list_tasks(conn, order_by="title")
        assert [t.id for t in by_title] == [t_a, t_b, t_c]

        # Sort by assignee
        kb.assign_task(conn, t_a, "alice")
        kb.assign_task(conn, t_b, "bob")
        kb.assign_task(conn, t_c, "alice")
        by_assignee = kb.list_tasks(conn, order_by="assignee")
        # alice's tasks first (alphabetically), then bob's
        assignees = [t.assignee for t in by_assignee]
        assert assignees[:2] == ["alice", "alice"]
        assert assignees[2] == "bob"

        # Invalid sort order raises ValueError
        try:
            kb.list_tasks(conn, order_by="bogus")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "order_by must be one of" in str(e)

def test_delete_task_removes_task_and_cascades(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="to-delete", assignee="alice")
        kb.add_comment(conn, t, "user", "comment")
        kb.add_comment(conn, t, "user", "another")
        assert kb.delete_task(conn, t)
        assert kb.get_task(conn, t) is None
        assert len(kb.list_comments(conn, t)) == 0
        assert len(kb.list_events(conn, t)) == 0
        assert len(kb.list_runs(conn, t)) == 0


def test_delete_task_returns_false_for_missing_task(kanban_home):
    with kb.connect() as conn:
        assert not kb.delete_task(conn, "t_nonexistent")


def test_delete_task_cascades_links(kanban_home):
    with kb.connect() as conn:
        p = kb.create_task(conn, title="parent")
        c = kb.create_task(conn, title="child", parents=[p])
        child = kb.get_task(conn, c)
        assert child is not None and child.status == "todo"
        kb.delete_task(conn, p)
        assert kb.get_task(conn, p) is None
        child_after = kb.get_task(conn, c)
        assert child_after is not None and child_after.status == "ready"


# ---------------------------------------------------------------------------
# Comments / events / worker context
# ---------------------------------------------------------------------------

def test_comments_recorded_in_order(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "user", "first")
        kb.add_comment(conn, t, "researcher", "second")
        comments = kb.list_comments(conn, t)
    assert [c.body for c in comments] == ["first", "second"]
    assert [c.author for c in comments] == ["user", "researcher"]


def test_empty_comment_rejected(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        with pytest.raises(ValueError, match="body is required"):
            kb.add_comment(conn, t, "user", "")


def test_events_capture_lifecycle(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        kb.complete_task(conn, t, result="ok")
        events = kb.list_events(conn, t)
    kinds = [e.kind for e in events]
    assert "created" in kinds
    assert "claimed" in kinds
    assert "completed" in kinds


def test_worker_context_includes_parent_results_and_comments(kanban_home):
    with kb.connect() as conn:
        p = kb.create_task(conn, title="p")
        kb.complete_task(conn, p, result="PARENT_RESULT_MARKER")
        c = kb.create_task(conn, title="child", parents=[p])
        kb.add_comment(conn, c, "user", "CLARIFICATION_MARKER")
        ctx = kb.build_worker_context(conn, c)
    assert "PARENT_RESULT_MARKER" in ctx
    assert "CLARIFICATION_MARKER" in ctx
    assert c in ctx
    assert "child" in ctx


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def test_dispatch_dry_run_does_not_claim(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="a", assignee="alice")
        t2 = kb.create_task(conn, title="b", assignee="bob")
        res = kb.dispatch_once(conn, dry_run=True)
    assert {s[0] for s in res.spawned} == {t1, t2}
    with kb.connect() as conn:
        # Dry run must NOT mutate status.
        assert kb.get_task(conn, t1).status == "ready"
        assert kb.get_task(conn, t2).status == "ready"


def test_dispatch_skips_unassigned(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="floater")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_unassigned
    assert t not in res.skipped_nonspawnable
    assert not res.spawned


def test_dispatch_skips_nonspawnable_into_separate_bucket(kanban_home, monkeypatch):
    """Tasks whose assignee fails profile_exists() must NOT land in
    ``skipped_unassigned`` (which is operator-actionable) — they go in
    the dedicated ``skipped_nonspawnable`` bucket so health telemetry
    can suppress false-positive "stuck" warnings."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        t = kb.create_task(conn, title="for-terminal", assignee="orion-cc")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_nonspawnable
    assert t not in res.skipped_unassigned
    assert not res.spawned


def test_has_spawnable_ready_false_when_only_terminal_lanes(kanban_home, monkeypatch):
    """``has_spawnable_ready`` returns False when every ready task is
    assigned to a control-plane lane — used by gateway/CLI dispatchers
    to silence the stuck-warn while terminals still have queued work."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        kb.create_task(conn, title="t1", assignee="orion-cc")
        kb.create_task(conn, title="t2", assignee="orion-research")
        assert kb.has_spawnable_ready(conn) is False


def test_has_spawnable_ready_true_when_real_profile_present(kanban_home, monkeypatch):
    """``has_spawnable_ready`` returns True as soon as ANY ready task
    has an assignee that maps to a real Hermes profile — preserves the
    real "stuck" signal when a daily/agent task is queued."""
    from hermes_cli import profiles
    monkeypatch.setattr(
        profiles, "profile_exists", lambda name: name == "daily"
    )
    with kb.connect() as conn:
        kb.create_task(conn, title="terminal-task", assignee="orion-cc")
        kb.create_task(conn, title="hermes-task", assignee="daily")
        assert kb.has_spawnable_ready(conn) is True


def test_has_spawnable_ready_false_on_empty_queue(kanban_home):
    """Empty queue is the trivial false case — no ready tasks at all."""
    with kb.connect() as conn:
        assert kb.has_spawnable_ready(conn) is False


def test_dispatch_promotes_ready_and_spawns(kanban_home, all_assignees_spawnable):
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee, workspace))

    with kb.connect() as conn:
        p = kb.create_task(conn, title="p", assignee="alice")
        c = kb.create_task(conn, title="c", assignee="bob", parents=[p])
        # Finish parent outside dispatch; promotion happens inside.
        kb.complete_task(conn, p)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
    # Spawned c (a was already done when dispatch was called).
    assert len(spawns) == 1
    assert spawns[0][0] == c
    assert spawns[0][1] == "bob"
    # c is now running
    with kb.connect() as conn:
        assert kb.get_task(conn, c).status == "running"


def test_dispatch_spawn_failure_releases_claim(kanban_home, all_assignees_spawnable):
    def boom(task, workspace):
        raise RuntimeError("spawn failed")

    with kb.connect() as conn:
        t = kb.create_task(conn, title="boom", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=boom)
        # Must return to ready so the next tick can retry.
        assert kb.get_task(conn, t).status == "ready"
        assert kb.get_task(conn, t).claim_lock is None


def test_dispatch_max_spawn_counts_existing_running_tasks(
    kanban_home, all_assignees_spawnable
):
    """max_spawn is a live concurrency cap, not a per-tick spawn cap.

    Without counting tasks already in ``running``, every dispatcher tick can
    launch up to ``max_spawn`` more workers while previous workers are still
    alive. Long-running boards then accumulate unbounded worker subprocesses.
    """
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect() as conn:
        running_a = kb.create_task(conn, title="running-a", assignee="alice")
        running_b = kb.create_task(conn, title="running-b", assignee="bob")
        ready = kb.create_task(conn, title="ready", assignee="carol")
        kb.claim_task(conn, running_a)
        kb.claim_task(conn, running_b)

        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)

        assert res.spawned == []
        assert spawns == []
        assert kb.get_task(conn, ready).status == "ready"


def test_dispatch_max_spawn_fills_remaining_capacity(
    kanban_home, all_assignees_spawnable
):
    """When below cap, dispatch only fills available worker slots."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        ready_a = kb.create_task(conn, title="ready-a", assignee="bob")
        ready_b = kb.create_task(conn, title="ready-b", assignee="carol")
        kb.claim_task(conn, running)

        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)

        assert len(res.spawned) == 1
        assert spawns == [ready_a]
        assert kb.get_task(conn, ready_a).status == "running"
        assert kb.get_task(conn, ready_b).status == "ready"


def test_dispatch_reclaims_stale_before_spawning(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="alice")
        kb.claim_task(conn, t)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 1, t),
        )
        res = kb.dispatch_once(conn, dry_run=True)
    assert res.reclaimed == 1


# ---------------------------------------------------------------------------
# Respawn guard (check_respawn_guard + dispatch_once integration)
# ---------------------------------------------------------------------------

_CONTINUATION_SHA_A = "a" * 40
_CONTINUATION_SHA_B = "b" * 40


def _continuation_tuple(owner: str, repo: str, number: int, sha: str) -> str:
    return f"{owner}/{repo}#{number}@{sha}"


def _open_draft_pr(pr: kb.ContinuationPR) -> kb.GitHubPRState:
    return kb.GitHubPRState(
        canonical_url=pr.canonical_url,
        state="OPEN",
        is_draft=True,
        head_sha=pr.head_sha,
    )


def _create_continuation_task(
    conn: sqlite3.Connection,
    *pr_tuples: str,
    assignee: str = "engineer",
) -> str:
    task_id = kb.create_task(conn, title="repair active PR", assignee=assignee)
    for raw in pr_tuples:
        pr = kb.parse_continuation_pr_tuple(raw)
        kb.add_comment(conn, task_id, "worker", f"Opened {pr.canonical_url}")
    kb.record_continuation_review(
        conn,
        task_id,
        verdict="fix-required",
        reason="repair the existing PR",
    )
    return task_id


def _authorize_continuation(
    conn: sqlite3.Connection,
    task_id: str,
    *pr_tuples: str,
) -> kb.ContinuationAuthorization:
    return kb.authorize_continuation(
        conn,
        task_id,
        pr_tuples,
        reason="address unresolved review findings",
        authorized_profile="engineer",
        authorized_provider="openai-codex",
    )

def test_respawn_guard_none_on_fresh_task(kanban_home):
    """A fresh task with no failures or runs is not guarded."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="fresh", assignee="alice")
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_blocker_auth_on_quota_error(kanban_home):
    """'quota' in last_failure_error triggers blocker_auth."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="quota-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("API quota exceeded: rate limit hit", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_auth_error(kanban_home):
    """'unauthorized' in last_failure_error triggers blocker_auth."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="auth-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("403 Forbidden: unauthorized to access resource", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_authentication_error(kanban_home):
    """Full word 'Authentication' triggers blocker_auth (regex covers auth\\w*)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="authn-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("Authentication failed: invalid credentials", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_authorization_error(kanban_home):
    """Full word 'authorization' triggers blocker_auth (regex covers auth\\w*)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="authz-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("authorization denied for scope repo", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_recent_success(kanban_home):
    """A completed run within the guard window triggers recent_success."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="already-done", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        decision = kb.evaluate_respawn_guard(conn, t)
    assert decision.reason == "recent_success"
    assert decision.detail == {
        "expires_at": now - 60 + kb._RESPAWN_GUARD_SUCCESS_WINDOW,
        "window_seconds": kb._RESPAWN_GUARD_SUCCESS_WINDOW,
    }


def test_respawn_guard_recent_success_bypassed_by_requeue(kanban_home):
    """An explicit re-queue after a recent success (operator done->ready,
    promote, unblock, reclaim) is a deliberate re-run and must bypass the
    recent_success guard — otherwise a manual done->ready just sits there
    until the window elapses."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="rerun-me", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        # Baseline: a recent completion defers the respawn.
        assert kb.check_respawn_guard(conn, t) == "recent_success"
        # Operator drags done -> ready: a 'status' event after completion.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'status', ?)",
            (t, now - 10),
        )
        assert kb.check_respawn_guard(conn, t) is None


def test_respawn_guard_stale_success_not_guarded(kanban_home):
    """A completed run outside the guard window does not block re-spawn."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="old-done", assignee="alice")
        old_end = int(time.time()) - kb._RESPAWN_GUARD_SUCCESS_WINDOW - 60
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, old_end - 300, old_end),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_active_pr_in_comment(kanban_home):
    """A GitHub PR URL in a recent comment triggers active_pr."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.add_comment(
            conn, t, "worker",
            "PR created: https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "active_pr"


# ---------------------------------------------------------------------------
# active_pr ownership. Regression cover for the defect where ANY GitHub PR URL
# in ANY comment inside the 24h window silently froze a card for a full day,
# even when the PR provably belonged to a different card in a different repo.
# Cross-referencing a companion PR is behaviour the fleet asks workers for, so
# the guard has to tell custody apart from citation.
# ---------------------------------------------------------------------------

_COMPANION_PR = "https://github.com/o269/omnia-v2/pull/222"
_OWN_PR = "https://github.com/o269/omnia/pull/681"


def _card_declaring_pr(conn, url: str, *, title: str = "companion") -> str:
    """A card whose OWN completed run named ``url`` — i.e. real custody."""
    owner = kb.create_task(conn, title=title, assignee="cursor2")
    kb.complete_task(
        conn,
        owner,
        result=f"Opened {url}",
        summary=f"AUTHOR COMPLETE: {url} is OPEN/MERGEABLE",
    )
    return owner


def _requeue_after_completed_work_product(
    conn: sqlite3.Connection, task_id: str
) -> None:
    """Model an explicit post-completion retry without recent-success masking."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?",
            (task_id,),
        )
        kb._append_event(
            conn,
            task_id,
            "promoted",
            {"source": "test_explicit_requeue"},
        )


def test_respawn_guard_ignores_cross_repo_pr_owned_by_another_card(kanban_home):
    """The live repro: a companion PR quoted in a comment must not freeze us.

    ``t_45e612bd`` (engine work) sat Ready for hours because an unrelated
    comment quoted the full URL of the *frontend* companion PR — different
    repo, different card, different work item.
    """
    with kb.connect() as conn:
        owner = _card_declaring_pr(conn, _COMPANION_PR)
        cited = kb.create_task(conn, title="engine signing URLs", assignee="alice")
        kb.add_comment(
            conn,
            cited,
            "cursor2",
            "FRONTEND COMPANION AUTHOR-COMPLETE: o269/omnia-v2 PR #222 is "
            f"OPEN/CLEAN/MERGEABLE: {_COMPANION_PR}. It depends on this engine "
            "card's bounded resolver; please coordinate land order.",
        )

        assert kb.check_respawn_guard(conn, cited) is None
        # Custody is not laundered away — the PR is still attributed to the
        # card whose own work product opened it, and only to that card.
        declared_by = [
            row["task_id"]
            for row in conn.execute(
                "SELECT task_id FROM task_pr_ownership "
                "WHERE canonical_url = ? AND declared = 1",
                (_COMPANION_PR,),
            ).fetchall()
        ]
        assert declared_by == [owner]


def test_respawn_guard_keeps_active_pr_for_self_authored_comment(kanban_home):
    """The card's OWN worker announcing its own PR is custody, not a citation.

    This is the over-freeing case, and it is the common one. Replayed against
    the live board, custody-by-work-product alone freed 14 cards and 13 were
    this shape — the assigned worker posting ``opened PR #N`` as a comment and
    never mirroring it into a run summary. A reviewer/successor card that also
    names the PR in its own run summary must not be able to strip the author
    card's guard, or two workers end up writing one PR.
    """
    with kb.connect() as conn:
        # A separate review card declares the same PR from its own run.
        reviewer = _card_declaring_pr(conn, _OWN_PR, title="review PR681")

        author_card = kb.create_task(conn, title="hub-config hardening", assignee="codex7")
        kb.add_comment(
            conn,
            author_card,
            "codex7",  # == assignee: this card's own worker
            f"AUTHOR COMPLETE / awaiting review. Opened draft PR: {_OWN_PR}",
        )

        assert kb.check_respawn_guard(conn, author_card) == "active_pr"
        # Both cards legitimately hold the PR; neither disowns the other.
        _requeue_after_completed_work_product(conn, reviewer)
        assert kb.check_respawn_guard(conn, reviewer) == "active_pr"
        owned, disowned = kb._active_pr_candidates(
            conn, author_card, cutoff=int(time.time()) - kb._RESPAWN_GUARD_PR_WINDOW
        )
        assert [o["ownership"] for o in owned] == ["declared"]
        assert disowned == ()


@pytest.mark.parametrize("work_product_source", ["task_result", "run_summary"])
def test_respawn_guard_work_product_only_custody_blocks_dispatch_and_direct_claim(
    kanban_home, all_assignees_spawnable, work_product_source
):
    """A declaration needs no PR-bearing comment to retain one-writer custody."""
    spawned: list[str] = []
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"{work_product_source}-only custody",
            assignee="alice",
        )
        if work_product_source == "task_result":
            kb.complete_task(
                conn,
                task_id,
                result=f"AUTHOR COMPLETE: {_OWN_PR}",
                summary="result-only PR handoff",
            )
        else:
            kb.complete_task(
                conn,
                task_id,
                result="completed without a PR URL",
                summary=f"AUTHOR COMPLETE: {_OWN_PR}",
            )
        _requeue_after_completed_work_product(conn, task_id)

        comment_count = conn.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert comment_count is not None
        assert comment_count[0] == 0
        task_row = conn.execute(
            "SELECT result FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT summary, metadata, error FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert task_row is not None
        assert run_row is not None
        if work_product_source == "task_result":
            assert _OWN_PR in task_row["result"]
            assert all(
                _OWN_PR not in str(run_row[field] or "")
                for field in ("summary", "metadata", "error")
            )
        else:
            assert _OWN_PR not in str(task_row["result"] or "")
            assert _OWN_PR in run_row["summary"]

        ownership = conn.execute(
            "SELECT declared, source_comment_id, last_seen_at "
            "FROM task_pr_ownership WHERE task_id = ? AND canonical_url = ?",
            (task_id, _OWN_PR),
        ).fetchone()
        assert ownership is not None
        assert ownership["declared"] == 1
        assert ownership["source_comment_id"] is None
        assert ownership["last_seen_at"] >= (
            int(time.time()) - kb._RESPAWN_GUARD_PR_WINDOW
        )

        owned, disowned = kb._active_pr_candidates(
            conn,
            task_id,
            cutoff=int(time.time()) - kb._RESPAWN_GUARD_PR_WINDOW,
        )
        assert disowned == ()
        assert len(owned) == 1
        assert owned[0]["canonical_url"] == _OWN_PR
        assert owned[0]["ownership"] == "declared"
        assert owned[0]["source_comment_id"] is None
        assert owned[0]["expires_at"] == (
            ownership["last_seen_at"] + kb._RESPAWN_GUARD_PR_WINDOW
        )

        decision = kb.evaluate_respawn_guard(conn, task_id)
        assert decision.reason == "active_pr"
        assert decision.detail is not None
        assert decision.detail["source_comment_id"] is None
        assert decision.detail["pr_urls"] == [_OWN_PR]

        dispatch = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )
        assert (task_id, "active_pr") in dispatch.respawn_guarded
        diagnostic = dispatch.respawn_guard_details[-1]
        assert diagnostic["task_id"] == task_id
        assert diagnostic["reason"] == "active_pr"
        assert diagnostic["pr_url"] == _OWN_PR
        assert diagnostic["expires_at"] == decision.detail["expires_at"]
        assert diagnostic["phase"] == "ready"
        after_dispatch = kb.get_task(conn, task_id)
        assert after_dispatch is not None
        assert after_dispatch.status == "ready"

        assert kb.claim_task(conn, task_id) is None
        after_claim = kb.get_task(conn, task_id)
        assert after_claim is not None
        assert after_claim.status == "ready"
        guarded = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "respawn_guarded"
        ]
        assert guarded[-1].payload is not None
        assert guarded[-1].payload["source_comment_id"] is None

        expired_at = int(time.time()) - kb._RESPAWN_GUARD_PR_WINDOW - 1
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_pr_ownership SET last_seen_at = ? "
                "WHERE task_id = ? AND canonical_url = ?",
                (expired_at, task_id, _OWN_PR),
            )
        assert kb.evaluate_respawn_guard(conn, task_id).reason is None

    assert spawned == []


def test_respawn_guard_ownership_read_failure_does_not_fail_open(kanban_home):
    """A ledger read failure cannot turn guarded work into a claimable card."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="guard read failure", assignee="alice")
        kb.complete_task(
            conn,
            task_id,
            result=f"AUTHOR COMPLETE: {_OWN_PR}",
            summary="result-only PR handoff",
        )
        _requeue_after_completed_work_product(conn, task_id)

        def deny_ownership_read(action, table, _column, _database, _trigger):
            if action == sqlite3.SQLITE_READ and table == "task_pr_ownership":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_ownership_read)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                kb.claim_task(conn, task_id)
        finally:
            conn.set_authorizer(None)

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"


def test_respawn_guard_active_pr_is_recent_custody_lease_not_remote_state(
    kanban_home, monkeypatch
):
    """Ordinary ``active_pr`` stays local even for a known merged PR URL."""
    monkeypatch.setattr(
        kb,
        "_default_github_pr_verifier",
        lambda _pr: pytest.fail("ordinary active_pr must not probe GitHub"),
    )
    merged_pr = "https://github.com/o269/hermes-agent/pull/3"
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="merged PR lease", assignee="alice")
        kb.add_comment(
            conn,
            task_id,
            "alice",
            f"MERGED upstream; retaining recent custody evidence: {merged_pr}",
        )

        decision = kb.evaluate_respawn_guard(conn, task_id)

    assert decision.reason == "active_pr"
    assert decision.detail is not None
    assert decision.detail["pr_url"] == merged_pr


def test_respawn_guard_frees_cross_posted_pr_but_not_self_authored(kanban_home):
    """Same PR, same wording, two cards — only the cross-post is freed.

    Pins the exact discriminator to *who wrote the comment*, not what it says.
    Both comments below are verbatim-identical handoff prose; the only
    difference is that one author matches its card's assignee.
    """
    prose = f"AUTHOR-COMPLETE: PR is OPEN/CLEAN/MERGEABLE: {_COMPANION_PR}"
    with kb.connect() as conn:
        _card_declaring_pr(conn, _COMPANION_PR)

        mine = kb.create_task(conn, title="own work", assignee="codex7")
        kb.add_comment(conn, mine, "codex7", prose)

        theirs = kb.create_task(conn, title="engine work", assignee="fable")
        kb.add_comment(conn, theirs, "cursor2", prose)

        assert kb.check_respawn_guard(conn, mine) == "active_pr"
        assert kb.check_respawn_guard(conn, theirs) is None


def test_respawn_guard_keeps_active_pr_for_own_declared_pr(kanban_home):
    """Custody the guard was built for: this card's own run opened the PR."""
    with kb.connect() as conn:
        # Another card also cites the PR — that must not launder away custody.
        mine = kb.create_task(conn, title="engine work", assignee="alice")
        kb.claim_task(conn, mine)
        kb.block_task(
            conn,
            mine,
            reason=f"review-required: opened {_OWN_PR}",
            kind="needs_input",
        )
        kb.add_comment(conn, mine, "worker", f"PR opened: {_OWN_PR}")
        kb.unblock_task(conn, mine)

        assert kb.check_respawn_guard(conn, mine) == "active_pr"


def test_respawn_guard_keeps_active_pr_when_nobody_declared_it(kanban_home):
    """Comment-only handoffs stay guarded — no card declared the PR anywhere.

    This is the common legacy shape ("PR opened: <url>" posted as a comment and
    never mirrored into a run summary). Dropping the guard here would have
    reintroduced duplicate PRs, so the conservative default is preserved.
    """
    with kb.connect() as conn:
        t = kb.create_task(conn, title="comment-only handoff", assignee="alice")
        kb.add_comment(conn, t, "worker", f"PR opened: {_OWN_PR}")
        assert kb.check_respawn_guard(conn, t) == "active_pr"


def test_respawn_guarded_event_names_the_pr_and_its_expiry(kanban_home):
    """A suppressed respawn must be diagnosable without reading the DB."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="comment-only handoff", assignee="alice")
        comment_id = kb.add_comment(conn, t, "worker", f"PR opened: {_OWN_PR}")
        decision = kb.evaluate_respawn_guard(conn, t)
        assert decision.reason == "active_pr"
        kb.record_respawn_guard_decision(conn, t, decision)

        guarded = [
            event for event in kb.list_events(conn, t)
            if event.kind == "respawn_guarded"
        ][-1]

    payload = guarded.payload
    assert payload["reason"] == "active_pr"
    assert payload["pr_url"] == _OWN_PR
    assert payload["ownership"] == "referenced"
    assert payload["source_comment_id"] == comment_id
    assert payload["expires_at"] > int(time.time())
    assert payload["window_seconds"] == kb._RESPAWN_GUARD_PR_WINDOW


def test_active_pr_diagnostics_keep_each_pr_paired_with_its_own_expiry(
    kanban_home, monkeypatch
):
    """Unequal PR leases must never render one PR beside another PR's expiry."""
    pr_a = "https://github.com/o269/hermes-agent/pull/20"
    pr_b = "https://github.com/o269/hermes-agent/pull/21"
    seen_a = 10_000_000
    seen_b = seen_a + 100

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="two active PRs", assignee="worker")
        monkeypatch.setattr(kb.time, "time", lambda: seen_a)
        comment_a = kb.add_comment(conn, task_id, "worker", f"Opened {pr_a}")
        monkeypatch.setattr(kb.time, "time", lambda: seen_b)
        comment_b = kb.add_comment(conn, task_id, "worker", f"Opened {pr_b}")
        monkeypatch.setattr(kb.time, "time", lambda: seen_b + 1)

        decision = kb.evaluate_respawn_guard(conn, task_id)

    assert decision.reason == "active_pr"
    assert decision.detail is not None
    assert decision.detail["pr_url"] == pr_a
    assert decision.detail["expires_at"] == (
        seen_a + kb._RESPAWN_GUARD_PR_WINDOW
    )
    assert decision.detail["pr_details"] == [
        {
            "pr_url": pr_a,
            "ownership": "declared",
            "source_comment_id": comment_a,
            "last_seen_at": seen_a,
            "expires_at": seen_a + kb._RESPAWN_GUARD_PR_WINDOW,
        },
        {
            "pr_url": pr_b,
            "ownership": "declared",
            "source_comment_id": comment_b,
            "last_seen_at": seen_b,
            "expires_at": seen_b + kb._RESPAWN_GUARD_PR_WINDOW,
        },
    ]

    result = kb.DispatchResult()
    result.add_respawn_guard(
        task_id,
        "active_pr",
        detail=decision.detail,
        phase="ready",
    )
    assert result.respawn_guard_log_lines() == [
        f"SKIP {task_id} respawn_guarded=active_pr "
        f"pr={pr_a} expires={seen_a + kb._RESPAWN_GUARD_PR_WINDOW} "
        f"pr={pr_b} expires={seen_b + kb._RESPAWN_GUARD_PR_WINDOW} phase=ready"
    ]


def test_respawn_guard_log_corrupt_per_pr_payload_falls_back_to_legacy_fields():
    """A malformed optional per-PR payload cannot erase known diagnostics."""
    result = kb.DispatchResult()
    result.add_respawn_guard(
        "t_owned",
        "active_pr",
        detail={
            "pr_url": _OWN_PR,
            "expires_at": 1785660000,
            "pr_details": [None, "corrupt", {"expires_at": 1785660300}],
        },
        phase="ready",
    )

    assert result.respawn_guard_log_lines() == [
        "SKIP t_owned respawn_guarded=active_pr "
        f"pr={_OWN_PR} expires=1785660000 phase=ready"
    ]


def test_disowned_pr_mention_is_audited_not_silent(kanban_home):
    """Letting a citation through is recorded too — silence hid the old bug."""
    with kb.connect() as conn:
        owner = _card_declaring_pr(conn, _COMPANION_PR)
        cited = kb.create_task(conn, title="engine work", assignee="alice")
        kb.add_comment(conn, cited, "cursor2", f"companion FE PR: {_COMPANION_PR}")

        decision = kb.evaluate_respawn_guard(conn, cited)
        assert decision.reason is None
        kb.record_respawn_guard_decision(conn, cited, decision)
        # Rate-limited: a 2-minute dispatcher tick must not flood the ledger.
        kb.record_respawn_guard_decision(conn, cited, decision)

        ignored = [
            event for event in kb.list_events(conn, cited)
            if event.kind == "respawn_guard_pr_ignored"
        ]

    assert len(ignored) == 1
    assert ignored[0].payload["ignored_pr_urls"] == [
        {"pr_url": _COMPANION_PR, "declared_by": owner}
    ]


def test_dispatch_spawns_card_that_only_cites_another_cards_pr(
    kanban_home, all_assignees_spawnable
):
    """End-to-end: the card the old guard starved now actually spawns."""
    spawned: list[str] = []
    with kb.connect() as conn:
        _card_declaring_pr(conn, _COMPANION_PR)
        cited = kb.create_task(conn, title="engine work", assignee="alice")
        kb.add_comment(conn, cited, "cursor2", f"companion FE PR: {_COMPANION_PR}")

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )

    assert spawned == [cited]
    assert not [
        entry for entry in result.respawn_guarded if entry[0] == cited
    ]


def test_continuation_exact_authorization_consumes_atomically_and_passes_once(
    kanban_home, all_assignees_spawnable
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    spawned: list[str] = []

    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        authorization = _authorize_continuation(conn, task_id, pr_tuple)
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )

        assert result.spawned and result.spawned[0][0] == task_id
        assert spawned == [task_id]
        consumed = kb.get_continuation_authorization(conn, authorization.id)
        assert consumed is not None
        assert consumed.status() == "consumed"
        assert consumed.consumed_run_id == kb.get_task(conn, task_id).current_run_id
        events = kb.list_events(conn, task_id)
        assert [event.kind for event in events].count("continuation_authorized") == 1
        assert [event.kind for event in events].count("continuation_consumed") == 1

        assert kb.reclaim_task(conn, task_id, reason="simulate completed worker process")
        second = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )
        assert (task_id, "active_pr") in second.respawn_guarded
        assert spawned == [task_id]
        denial = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial.payload["reason"] == "authorization_consumed"
        before = len(
            [
                event
                for event in kb.list_events(conn, task_id)
                if event.kind == "continuation_denied"
            ]
        )
        kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )
        after = len(
            [
                event
                for event in kb.list_events(conn, task_id)
                if event.kind == "continuation_denied"
            ]
        )
        assert after == before


def test_dispatch_never_supersedes_its_evaluated_authorization(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A grant minted after evaluate cannot replace the decisioned grant."""
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    spawned: list[str] = []

    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        evaluated = _authorize_continuation(conn, task_id, pr_tuple)
        original_claim = kb.claim_task
        observed: dict[str, object] = {}

        def supersede_then_claim(connection, claimed_task_id, **kwargs):
            observed["passed_id"] = kwargs.get("continuation_authorization_id")
            replacement = _authorize_continuation(
                connection, claimed_task_id, pr_tuple
            )
            observed["replacement"] = replacement
            return original_claim(connection, claimed_task_id, **kwargs)

        monkeypatch.setattr(kb, "claim_task", supersede_then_claim)
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )

        replacement = observed["replacement"]
        assert isinstance(replacement, kb.ContinuationAuthorization)
        assert observed["passed_id"] == evaluated.id
        evaluated_after = kb.get_continuation_authorization(conn, evaluated.id)
        replacement_after = kb.get_continuation_authorization(conn, replacement.id)
        task_after = kb.get_task(conn, task_id)
        assert evaluated_after is not None and evaluated_after.status() == "revoked"
        assert replacement_after is not None and replacement_after.status() == "active"
        assert task_after is not None and task_after.status == "ready"
        assert result.spawned == []
        assert spawned == []
        assert not [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_consumed"
        ]
        denial = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial == {
            "authorization_id": evaluated.id,
            "phase": "claim_race",
            "reason": "authorization_revoked",
        }


def test_dispatch_claim_exception_preserves_active_pr_identity_and_expiry(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A claim-time authorization race retains the evaluated PR diagnosis."""
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    canonical_url = kb.parse_continuation_pr_tuple(pr_tuple).canonical_url

    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        authorization = _authorize_continuation(conn, task_id, pr_tuple)

        def expire_before_claim(_connection, _task_id, **_kwargs):
            raise kb.ContinuationAuthorizationError("authorization_expired")

        monkeypatch.setattr(kb, "claim_task", expire_before_claim)
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args: None)

    assert result.spawned == []
    assert result.respawn_guarded == [(task_id, "active_pr")]
    diagnostic = result.respawn_guard_details[-1]
    assert diagnostic["pr_url"] == canonical_url
    assert diagnostic["pr_details"][0]["pr_url"] == canonical_url
    assert diagnostic["pr_details"][0]["expires_at"] is not None
    assert diagnostic["continuation_authorization_id"] == authorization.id
    assert diagnostic["continuation_denial"] == "authorization_expired"
    assert diagnostic["phase"] == "claim_exception"
    rendered = result.respawn_guard_log_lines()[0]
    assert f"pr={canonical_url}" in rendered
    assert "expires=" in rendered
    assert "phase=claim_exception" in rendered
    assert "denial=authorization_expired" in rendered


def test_post_claim_guard_audits_consumed_continuation_without_spawn(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A post-claim owner consumes once but emits an explicit reauth audit."""
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    spawned: list[str] = []
    holder = types.SimpleNamespace(pid=54321)

    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        authorization = _authorize_continuation(conn, task_id, pr_tuple)
        monkeypatch.setattr(kb, "_snapshot_worker_processes", lambda **_kw: [])

        def live_holders(_task_id, *, snapshot=None, **_kwargs):
            return [] if snapshot is not None else [holder]

        monkeypatch.setattr(kb, "_live_task_env_holders", live_holders)
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )

        task = kb.get_task(conn, task_id)
        consumed = kb.get_continuation_authorization(conn, authorization.id)
        run = kb.list_runs(conn, task_id)[0]
        events = kb.list_events(conn, task_id)

    assert task is not None
    assert consumed is not None
    assert result.spawned == []
    assert result.respawn_guarded == [(task_id, "live_worker_process")]
    assert spawned == []
    assert task.status == "ready" and task.current_run_id is None
    assert consumed.status() == "consumed"
    assert consumed.consumed_run_id == run.id
    assert run.status == "released" and run.outcome == "released"
    abandoned = [
        event for event in events
        if event.kind == "continuation_consumed_without_spawn"
    ]
    assert len(abandoned) == 1
    assert abandoned[0].payload is not None
    assert abandoned[0].payload == {
        "authorization_id": authorization.id,
        "run_id": run.id,
        "reason": "post_claim_live_worker_process",
        "reauthorization_required": True,
    }
    guarded = [event for event in events if event.kind == "respawn_guarded"][-1]
    assert guarded.payload is not None
    assert guarded.payload["continuation_authorization_id"] == authorization.id
    assert guarded.payload["continuation_consumed_without_spawn"] is True
    assert guarded.payload["continuation_reauthorization_required"] is True


def test_broker_rebind_preserves_real_claim_policy_surface(kanban_home):
    """Broker mode must not replace guarded claim_task with the legacy shim."""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(kanban_home)
    env["HERMES_KANBAN_HOME"] = str(kanban_home)
    env["HERMES_KANBAN_BROKER"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from hermes_cli import kanban_db as kb\n"
                "from hermes_cli import boardd_shim as shim\n"
                "assert kb.claim_task.__module__ == 'hermes_cli.kanban_db'\n"
                "assert 'claim_task' not in shim.REBIND_NAMES\n"
                "try:\n"
                " shim.claim_task(None, 't_probe', continuation_authorization_id=1)\n"
                "except RuntimeError:\n"
                " pass\n"
                "else:\n"
                " raise AssertionError('legacy shim claim did not fail closed')\n"
                "print(kb.claim_task.__module__)"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert completed.stdout.strip() == "hermes_cli.kanban_db"


def test_broker_rebind_passthrough_mutators_stay_local(kanban_home):
    """Broker mode must not redirect a non-fleet connection's writes to fleet."""
    local_db = kanban_home / "scratch.db"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(kanban_home)
    env["HERMES_KANBAN_HOME"] = str(kanban_home)
    env["HERMES_KANBAN_BROKER"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path\n"
                "import sys\n"
                "from hermes_cli import kanban_db as kb\n"
                "db = Path(sys.argv[1])\n"
                "kb.init_db(db_path=db)\n"
                "with kb.connect(db_path=db) as conn:\n"
                " tid = kb.create_task(conn, title='local', assignee='worker')\n"
                " cid = kb.add_comment(conn, tid, 'worker', 'local-only')\n"
                " kb.set_workspace_path(conn, tid, '/tmp/local-workspace')\n"
                " kb.set_branch_name(conn, tid, 'fix/local-only')\n"
                " conn.execute(\"UPDATE tasks SET status = 'running' WHERE id = ?\", (tid,))\n"
                " assert kb.heartbeat_worker(conn, tid, note='local heartbeat')\n"
                " row = conn.execute(\"SELECT workspace_path, branch_name, last_heartbeat_at FROM tasks WHERE id = ?\", (tid,)).fetchone()\n"
                " assert row['workspace_path'] == '/tmp/local-workspace'\n"
                " assert row['branch_name'] == 'fix/local-only'\n"
                " assert row['last_heartbeat_at'] is not None\n"
                " assert kb.list_comments(conn, tid)[0].id == cid\n"
                " assert kb.list_comments(conn, tid)[0].body == 'local-only'\n"
                " assert any(e.kind == 'heartbeat' and e.payload == {'note': 'local heartbeat'} for e in kb.list_events(conn, tid))\n"
                "print('passthrough-local-ok')"
            ),
            str(local_db),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert completed.stdout.strip() == "passthrough-local-ok"


def _run_local_rebind_probe(
    kanban_home: Path,
    local_db: Path,
    *,
    shim_first: bool,
    install_count: int,
) -> subprocess.CompletedProcess[str]:
    imports = (
        "from hermes_cli import boardd_shim as shim\n"
        "from hermes_cli import kanban_db as kb\n"
        if shim_first
        else
        "from hermes_cli import kanban_db as kb\n"
        "from hermes_cli import boardd_shim as shim\n"
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(kanban_home)
    env["HERMES_KANBAN_HOME"] = str(kanban_home)
    env.pop("HERMES_KANBAN_BROKER", None)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            imports
            + (
                "from pathlib import Path\n"
                "import sys\n"
                "db = Path(sys.argv[1])\n"
                "native_add_comment = kb.add_comment\n"
                "native_heartbeat = kb.heartbeat_worker\n"
                "native_set_workspace = kb.set_workspace_path\n"
                "native_set_branch = kb.set_branch_name\n"
                "native_flen = kb._check_file_length_invariant\n"
                "kb.init_db(db_path=db)\n"
                "with kb.connect(db_path=db) as conn:\n"
                " tid = kb.create_task(conn, title='local', assignee='worker')\n"
                " for _ in range(int(sys.argv[2])):\n"
                "  shim.install_rebind(kb)\n"
                " assert shim._ORIG_ADD_COMMENT is native_add_comment\n"
                " assert shim._ORIG_HEARTBEAT_WORKER is native_heartbeat\n"
                " assert shim._ORIG_SET_WORKSPACE_PATH is native_set_workspace\n"
                " assert shim._ORIG_SET_BRANCH_NAME is native_set_branch\n"
                " assert shim._ORIG_CHECK_FILE_LENGTH_INVARIANT is native_flen\n"
                " cid = kb.add_comment(conn, tid, 'worker', 'local-only')\n"
                " kb.set_workspace_path(conn, tid, '/tmp/local-workspace')\n"
                " kb.set_branch_name(conn, tid, 'fix/local-only')\n"
                " conn.execute(\"UPDATE tasks SET status = 'running' WHERE id = ?\", (tid,))\n"
                " assert kb.heartbeat_worker(conn, tid, note='local heartbeat')\n"
                " kb._check_file_length_invariant(conn)\n"
                " row = conn.execute(\"SELECT workspace_path, branch_name, last_heartbeat_at FROM tasks WHERE id = ?\", (tid,)).fetchone()\n"
                " assert row['workspace_path'] == '/tmp/local-workspace'\n"
                " assert row['branch_name'] == 'fix/local-only'\n"
                " assert row['last_heartbeat_at'] is not None\n"
                " assert kb.list_comments(conn, tid)[0].id == cid\n"
                "print('local-rebind-ok')"
            ),
            str(local_db),
            str(install_count),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )


def test_install_rebind_twice_preserves_native_delegates(kanban_home):
    """A second install must retain genuine local mutators and file checks."""
    completed = _run_local_rebind_probe(
        kanban_home,
        kanban_home / "duplicate-install.db",
        shim_first=False,
        install_count=2,
    )
    assert completed.stdout.strip() == "local-rebind-ok"


def test_boardd_shim_import_before_install_preserves_native_delegates(kanban_home):
    """Importing the broker path first must not poison a later install."""
    completed = _run_local_rebind_probe(
        kanban_home,
        kanban_home / "shim-import-first.db",
        shim_first=True,
        install_count=1,
    )
    assert completed.stdout.strip() == "local-rebind-ok"


def test_continuation_changed_head_fails_closed_with_audit_event(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)

    def changed_head(pr: kb.ContinuationPR) -> kb.GitHubPRState:
        return kb.GitHubPRState(
            canonical_url=pr.canonical_url,
            state="OPEN",
            is_draft=True,
            head_sha=_CONTINUATION_SHA_B,
        )

    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        authorization = _authorize_continuation(conn, task_id, pr_tuple)
        monkeypatch.setattr(kb, "_default_github_pr_verifier", changed_head)
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert (task_id, "active_pr") in result.respawn_guarded
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.get_continuation_authorization(conn, authorization.id).status() == "active"
        denial = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial.payload == {
            "authorization_id": authorization.id,
            "phase": "dispatch",
            "reason": "head_mismatch",
        }


@pytest.mark.parametrize(
    ("state", "is_draft", "verifier_raises", "expected_reason"),
    [
        ("CLOSED", True, False, "pr_not_open"),
        ("OPEN", True, True, "verifier_failure"),
    ],
)
def test_continuation_live_pr_verification_failures_are_closed(
    kanban_home,
    all_assignees_spawnable,
    state,
    is_draft,
    verifier_raises,
    expected_reason,
    monkeypatch,
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)

    def verifier(pr: kb.ContinuationPR) -> kb.GitHubPRState:
        if verifier_raises:
            raise RuntimeError("github unavailable")
        return kb.GitHubPRState(
            canonical_url=pr.canonical_url,
            state=state,
            is_draft=is_draft,
            head_sha=pr.head_sha,
        )

    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        _authorize_continuation(conn, task_id, pr_tuple)
        monkeypatch.setattr(kb, "_default_github_pr_verifier", verifier)
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert (task_id, "active_pr") in result.respawn_guarded
        denial = [
            event.payload["reason"]
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial == expected_reason


def test_continuation_accepts_open_non_draft_pr(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)

    def open_non_draft(pr: kb.ContinuationPR) -> kb.GitHubPRState:
        return kb.GitHubPRState(
            canonical_url=pr.canonical_url,
            state="OPEN",
            is_draft=False,
            head_sha=pr.head_sha,
        )

    monkeypatch.setattr(kb, "_default_github_pr_verifier", open_non_draft)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        authorization = kb.authorize_continuation(
            conn,
            task_id,
            [pr_tuple],
            reason="repair regular PR",
            authorized_profile="engineer",
            authorized_provider="openai-codex",
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert result.spawned and result.spawned[0][0] == task_id
        assert kb.get_continuation_authorization(conn, authorization.id).status() == "consumed"


@pytest.mark.parametrize("outcome", ["blocked", "gave_up"])
def test_continuation_accepts_budget_and_breaker_run_evidence(
    kanban_home, outcome
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    now = int(time.time())
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="capped repair", assignee="engineer")
        pr = kb.parse_continuation_pr_tuple(pr_tuple)
        kb.add_comment(conn, task_id, "engineer", f"Opened {pr.canonical_url}")
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, outcome, outcome, now - 30, now - 1),
        )
        authorization = _authorize_continuation(conn, task_id, pr_tuple)
        assert authorization.status() == "active"


def test_continuation_operator_gate_and_trusted_comment_author(
    kanban_home, monkeypatch
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="operator gate", assignee="engineer")
        pr = kb.parse_continuation_pr_tuple(pr_tuple)
        kb.add_comment(conn, task_id, "engineer", f"Opened {pr.canonical_url}")
        kb.add_comment(conn, task_id, "engineer", "FIX-REQUIRED: self-labelled")

        common = dict(
            reason="repair",
            authorized_profile="engineer",
            authorized_provider="openai-codex",
        )
        monkeypatch.setattr(
            kb,
            "_continuation_operator_context",
            lambda _conn: ("engineer", ("engineer",), None),
        )
        with pytest.raises(kb.ContinuationAuthorizationError) as self_auth:
            kb.authorize_continuation(
                conn,
                task_id,
                [pr_tuple],
                **common,
            )
        assert self_auth.value.code == "self_authorization_forbidden"

        monkeypatch.setattr(
            kb,
            "_continuation_operator_context",
            lambda _conn: ("reviewer", ("fable",), None),
        )
        with pytest.raises(kb.ContinuationAuthorizationError) as not_allowed:
            kb.authorize_continuation(
                conn,
                task_id,
                [pr_tuple],
                **common,
            )
        assert not_allowed.value.code == "authorizer_not_allowed"

        monkeypatch.setattr(
            kb,
            "_continuation_operator_context",
            lambda _conn: ("fable", ("fable",), "t_worker"),
        )
        with pytest.raises(kb.ContinuationAuthorizationError) as worker:
            kb.authorize_continuation(
                conn,
                task_id,
                [pr_tuple],
                **common,
            )
        assert worker.value.code == "worker_authorization_forbidden"

        monkeypatch.setattr(
            kb,
            "_continuation_operator_context",
            lambda _conn: ("default", ("default",), None),
        )
        with pytest.raises(kb.ContinuationAuthorizationError) as evidence:
            kb.authorize_continuation(
                conn,
                task_id,
                [pr_tuple],
                **common,
            )
        assert evidence.value.code == "repair_evidence_missing"
        denial_reasons = [
            event.payload["reason"]
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ]
        assert {
            "self_authorization_forbidden",
            "authorizer_not_allowed",
            "worker_authorization_forbidden",
            "repair_evidence_missing",
        } <= set(denial_reasons)


def test_continuation_worker_ancestry_cannot_be_hidden_by_env(
    kanban_home, monkeypatch
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="operator gate", assignee="engineer")
        pr = kb.parse_continuation_pr_tuple(pr_tuple)
        kb.add_comment(conn, task_id, "engineer", f"Opened {pr.canonical_url}")
        kb.add_comment(conn, task_id, "fable", "FIX-REQUIRED: repair")
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.setattr(
            kb,
            "_continuation_operator_context",
            lambda _conn: ("fable", ("fable",), None),
        )
        monkeypatch.setattr(
            kb,
            "_current_process_ancestry_pids",
            lambda: (os.getpid(), 424242),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, worker_pid, started_at) "
            "VALUES ('t_worker', 'running', 424242, ?)",
            (int(time.time()),),
        )

        with pytest.raises(kb.ContinuationAuthorizationError) as denied:
            kb.authorize_continuation(
                conn,
                task_id,
                [pr_tuple],
                reason="repair",
                authorized_profile="engineer",
                authorized_provider="openai-codex",
            )

        assert denied.value.code == "worker_authorization_forbidden"


def test_continuation_runtime_api_has_no_verifier_injection_surface():
    for func in (
        kb.authorize_continuation,
        kb.evaluate_respawn_guard,
        kb.claim_task,
        kb.dispatch_once,
    ):
        parameters = inspect.signature(func).parameters
        assert "github_pr_verifier" not in parameters
        assert "profile_provider_resolver" not in parameters


def test_continuation_review_events_are_authoritative_across_operators(
    kanban_home, monkeypatch
):
    first = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    second = _continuation_tuple("o269", "omnia", 569, _CONTINUATION_SHA_B)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="cross-review", assignee="engineer")
        pr = kb.parse_continuation_pr_tuple(first)
        kb.add_comment(conn, task_id, "worker", f"Opened {pr.canonical_url}")

        monkeypatch.setattr(
            kb,
            "_continuation_operator_context",
            lambda _conn: ("fable", ("fable",), None),
        )
        review_id = kb.record_continuation_review(
            conn,
            task_id,
            verdict="fix-required",
            reason="independent security review found a defect",
        )

        monkeypatch.setattr(
            kb,
            "_continuation_operator_context",
            lambda _conn: ("default", ("default",), None),
        )
        authorization = _authorize_continuation(conn, task_id, first)
        assert authorization.status() == "active"
        evidence = kb._continuation_evidence(conn, task_id)
        assert evidence == {
            "kind": "fix_required",
            "at": evidence["at"],
            "event_id": review_id,
        }

        resolved_id = kb.create_task(
            conn,
            title="cross-review resolved",
            assignee="engineer",
        )
        resolved_pr = kb.parse_continuation_pr_tuple(second)
        kb.add_comment(
            conn,
            resolved_id,
            "worker",
            f"Opened {resolved_pr.canonical_url}",
        )
        monkeypatch.setattr(
            kb,
            "_continuation_operator_context",
            lambda _conn: ("fable", ("fable",), None),
        )
        kb.record_continuation_review(
            conn,
            resolved_id,
            verdict="fix-required",
            reason="first reviewer found a defect",
        )
        monkeypatch.setattr(
            kb,
            "_continuation_operator_context",
            lambda _conn: ("security", ("security",), None),
        )
        kb.record_continuation_review(
            conn,
            resolved_id,
            verdict="resolved",
            reason="second allowlisted reviewer verified the repair",
        )
        assert kb._continuation_evidence(conn, resolved_id) is None


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process lineage test")
def test_continuation_double_fork_reparent_cannot_authorize(
    kanban_home, monkeypatch
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
    db_path = kb.kanban_db_path()
    # Bind both operator gates to the REAL gateway.status implementations so
    # the reparented child genuinely lacks the ephemeral control-plane
    # capability and does not own the retained gateway runtime lock.
    from gateway import status as gateway_status

    monkeypatch.setattr(
        kb,
        "_operator_control_plane_active",
        gateway_status.gateway_control_plane_active,
    )
    monkeypatch.setattr(
        kb,
        "_operator_gateway_lock_owned",
        gateway_status.process_owns_gateway_runtime_lock,
    )
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    read_fd, write_fd = os.pipe()
    first_child = os.fork()
    if first_child == 0:  # pragma: no cover - assertions happen in parent
        os.close(read_fd)
        try:
            os.setsid()
            second_child = os.fork()
            if second_child > 0:
                os._exit(0)
            deadline = time.monotonic() + 3
            while os.getppid() == first_child and time.monotonic() < deadline:
                time.sleep(0.01)
            try:
                with kb.connect(db_path) as child_conn:
                    kb.authorize_continuation(
                        child_conn,
                        task_id,
                        [pr_tuple],
                        reason="attempt from reparented worker",
                        authorized_profile="engineer",
                        authorized_provider="openai-codex",
                    )
            except kb.ContinuationAuthorizationError as exc:
                result = exc.code
            except BaseException as exc:
                result = f"unexpected:{type(exc).__name__}:{exc}"
            else:
                result = "authorized"
            os.write(write_fd, result.encode("utf-8", "replace"))
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    os.waitpid(first_child, 0)
    child_result = os.read(read_fd, 4096).decode("utf-8", "replace")
    os.close(read_fd)
    assert child_result == "operator_gateway_context_required"
    with kb.connect(db_path) as conn:
        assert kb.list_continuation_authorizations(conn, task_id) == []
        denials = [
            event.payload["reason"]
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ]
        assert denials[-1] == "operator_gateway_context_required"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process lineage test")
def test_continuation_forged_gateway_pid_record_cannot_claim_authority(
    kanban_home, monkeypatch, tmp_path
):
    """Adversarial regression: a double-forked, setsid, reparented helper that
    replaces the writable ``gateway.pid`` primary record (gateway-looking
    argv) while the REAL gateway lock stays held by another process must
    remain denied — even with the control-plane context artificially armed.
    Authority is proven by retained lock file-description ownership, never by
    the forgeable PID record."""
    import fcntl

    from gateway import status as gateway_status

    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
    db_path = kb.kanban_db_path()
    authority_root = db_path.parent
    # A child that ONLY arms the context stub still fails the lock proof; a
    # child with the real context check fails the context gate. Both orders
    # are asserted below via per-probe results.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    # The REAL gateway runtime lock is held by this (parent) process for the
    # whole probe, exactly like the live root gateway holding it.
    lock_path = authority_root / "gateway.lock"
    lock_handle = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "kind": "hermes-gateway",
                "argv": ["hermes", "gateway", "run"],
            }
        )
    )
    lock_handle.flush()

    read_fd, write_fd = os.pipe()
    first_child = os.fork()
    if first_child == 0:  # pragma: no cover - assertions happen in parent
        os.close(read_fd)
        try:
            os.setsid()
            second_child = os.fork()
            if second_child > 0:
                os._exit(0)
            deadline = time.monotonic() + 3
            while os.getppid() == first_child and time.monotonic() < deadline:
                time.sleep(0.01)
            results = {}
            try:
                # Forge the writable gateway.pid PRIMARY record: helper PID,
                # gateway-looking argv — the exact spoof from the security
                # review. The real lock above stays held by the parent.
                (authority_root / "gateway.pid").write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "kind": "hermes-gateway",
                            "argv": [sys.executable, "gateway/run.py"],
                            "start_time": None,
                        }
                    ),
                    encoding="utf-8",
                )
                real_context = kb._operator_control_plane_active
                real_lock = kb._operator_gateway_lock_owned
                with kb.connect(db_path) as child_conn:
                    # Probe 1: real context gate (the exact attack shape).
                    kb._operator_control_plane_active = (
                        gateway_status.gateway_control_plane_active
                    )
                    kb._operator_gateway_lock_owned = (
                        gateway_status.process_owns_gateway_runtime_lock
                    )
                    try:
                        kb.record_continuation_review(
                            child_conn,
                            task_id,
                            verdict="fix-required",
                            reason="forged authority probe",
                        )
                        results["review_real"] = "recorded"
                    except kb.ContinuationAuthorizationError as exc:
                        results["review_real"] = exc.code
                    # Probe 2: worst case — context capability somehow armed;
                    # the retained-lock proof must STILL deny.
                    kb._operator_control_plane_active = lambda: True
                    try:
                        kb.record_continuation_review(
                            child_conn,
                            task_id,
                            verdict="fix-required",
                            reason="forged authority probe",
                        )
                        results["review_armed"] = "recorded"
                    except kb.ContinuationAuthorizationError as exc:
                        results["review_armed"] = exc.code
                    try:
                        kb.authorize_continuation(
                            child_conn,
                            task_id,
                            [pr_tuple],
                            reason="forged authority probe",
                            authorized_profile="engineer",
                            authorized_provider="openai-codex",
                        )
                        results["authorize_armed"] = "authorized"
                    except kb.ContinuationAuthorizationError as exc:
                        results["authorize_armed"] = exc.code
                    try:
                        claimed = kb.claim_task(
                            child_conn,
                            task_id,
                            operator_override_reason="forged authority probe",
                        )
                        results["claim_armed"] = (
                            "claimed" if claimed is not None else "not_claimed"
                        )
                    except kb.ContinuationAuthorizationError as exc:
                        results["claim_armed"] = exc.code
                    kb._operator_control_plane_active = real_context
                    kb._operator_gateway_lock_owned = real_lock
            except BaseException as exc:  # pragma: no cover - defensive
                results["fatal"] = f"{type(exc).__name__}:{exc}"
            os.write(write_fd, json.dumps(results).encode("utf-8", "replace"))
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    os.waitpid(first_child, 0)
    child_results = json.loads(os.read(read_fd, 65536).decode("utf-8", "replace"))
    os.close(read_fd)
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    lock_handle.close()

    assert child_results.get("fatal") is None
    assert child_results["review_real"] == "operator_gateway_context_required"
    assert child_results["review_armed"] == "operator_gateway_process_required"
    assert child_results["authorize_armed"] == "operator_gateway_process_required"
    assert child_results["claim_armed"] == "operator_gateway_process_required"
    with kb.connect(db_path) as conn:
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.list_continuation_authorizations(conn, task_id) == []
        events = kb.list_events(conn, task_id)
        # Exactly one continuation_reviewed event exists — the legitimate one
        # recorded by the parent during task setup. The forged helper added
        # nothing: no review evidence, no guard bypass, no claim.
        reviewed = [e for e in events if e.kind == "continuation_reviewed"]
        assert len(reviewed) == 1
        assert reviewed[0].payload["reason"] == "repair the existing PR"
        assert not [e for e in events if e.kind == "respawn_guard_bypassed"]
        assert not [e for e in events if e.kind == "claimed"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock exploit regression")
def test_exec_fresh_fake_retained_handle_and_public_context_cannot_claim_authority(
    kanban_home, monkeypatch
):
    """Exact R9 regression: an exec-fresh helper may open (but not acquire)
    the real held lock, assign that handle to the legacy public module state,
    and rewrite the same inode's PID/start record.  It still cannot arm the
    private control context, append authoritative review evidence, or claim an
    operator override."""
    from gateway import status as gateway_status

    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
    db_path = kb.kanban_db_path()
    authority_root = db_path.parent
    monkeypatch.setenv("HERMES_HOME", str(authority_root))
    assert gateway_status.acquire_gateway_runtime_lock() is True

    helper = r'''
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from gateway import status
from hermes_cli import kanban_db as kb

db_arg, authority_arg, task_id = sys.argv[1:]
db_path = Path(db_arg)
authority_root = Path(authority_arg)
lock_path = authority_root / "gateway.lock"

# The helper is already exec-fresh (spawned with ``python -c``). Complete the
# reviewed setsid/double-fork/reparent shape before touching authority state.
first_pid = os.getpid()
os.setsid()
second_pid = os.fork()
if second_pid > 0:
    os._exit(0)
deadline = time.monotonic() + 3
while os.getppid() == first_pid and time.monotonic() < deadline:
    time.sleep(0.01)
reparented = os.getppid() != first_pid

handle = open(lock_path, "r+", encoding="utf-8")
lock_contended = False
try:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    lock_contended = True
else:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

# The rejected R8 implementation trusted both of these attacker-controlled
# mutations: assignable module state plus a same-inode PID/start rewrite.
status._gateway_lock_handle = handle
handle.seek(0)
handle.truncate()
json.dump(status._build_pid_record(), handle)
handle.flush()
os.fsync(handle.fileno())

def attempt_privileged_mutations():
    result = {
        "lock_contended": lock_contended,
        "reparented": reparented,
        "owns": status.process_owns_gateway_runtime_lock(authority_root),
    }
    with kb.connect(db_path) as conn:
        try:
            kb.record_continuation_review(
                conn,
                task_id,
                verdict="fix-required",
                reason="exec-fresh fake retained handle",
            )
        except kb.ContinuationAuthorizationError as exc:
            result["review"] = exc.code
        else:
            result["review"] = "recorded"
        try:
            claimed = kb.claim_task(
                conn,
                task_id,
                operator_override_reason="exec-fresh fake retained handle",
            )
        except kb.ContinuationAuthorizationError as exc:
            result["claim"] = exc.code
        else:
            result["claim"] = "claimed" if claimed is not None else "not_claimed"
    return result

try:
    with status.gateway_control_plane_context():
        public_context_armed = status.gateway_control_plane_active()
        result = attempt_privileged_mutations()
except RuntimeError:
    public_context_armed = False
    result = attempt_privileged_mutations()
result["public_context_armed"] = public_context_armed
handle.close()
print(json.dumps(result, sort_keys=True))
'''
    env = os.environ.copy()
    env["HERMES_HOME"] = str(authority_root)
    env["HERMES_PROFILE_NAME"] = "default"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", helper, str(db_path), str(authority_root), task_id],
            cwd=Path(__file__).parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
    finally:
        gateway_status.release_gateway_runtime_lock()

    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {
        "claim": "operator_gateway_context_required",
        "lock_contended": True,
        "owns": False,
        "public_context_armed": False,
        "reparented": True,
        "review": "operator_gateway_context_required",
    }
    with kb.connect(db_path) as conn:
        assert kb.get_task(conn, task_id).status == "ready"
        events = kb.list_events(conn, task_id)
        assert len([event for event in events if event.kind == "continuation_reviewed"]) == 1
        assert not [event for event in events if event.kind == "claimed"]
        assert not [event for event in events if event.kind == "respawn_guard_bypassed"]


def test_continuation_real_gateway_lock_and_context_authorize(
    kanban_home, monkeypatch
):
    """Positive path: a process that genuinely owns the retained runtime lock
    AND runs inside the armed control-plane context passes the operator gate."""
    from gateway import status as gateway_status

    monkeypatch.setattr(
        kb,
        "_operator_control_plane_active",
        gateway_status.gateway_control_plane_active,
    )
    monkeypatch.setattr(
        kb,
        "_operator_gateway_lock_owned",
        gateway_status.process_owns_gateway_runtime_lock,
    )
    assert gateway_status.acquire_gateway_runtime_lock() is True
    try:
        pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
        # Without the armed context the lock alone is insufficient.
        with kb.connect() as conn:
            task_id = kb.create_task(conn, title="ctx gate", assignee="engineer")
            with pytest.raises(kb.ContinuationAuthorizationError) as exc_info:
                kb.record_continuation_review(
                    conn,
                    task_id,
                    verdict="fix-required",
                    reason="context must be armed",
                )
            assert exc_info.value.code == "operator_gateway_context_required"
        arm_control_plane = gateway_status._claim_gateway_control_plane_context()
        with arm_control_plane():
            with kb.connect() as conn:
                task_id = _create_continuation_task(conn, pr_tuple)
                authorization = _authorize_continuation(conn, task_id, pr_tuple)
                assert authorization.status() == "active"
        # Capability is gone again after the context exits.
        assert gateway_status.gateway_control_plane_active() is False
    finally:
        gateway_status.release_gateway_runtime_lock()
    assert gateway_status.process_owns_gateway_runtime_lock() is False


def test_continuation_ttl_starts_after_writer_lock_acquisition(
    kanban_home, monkeypatch
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        clock = {"now": 1_000}
        original_write_txn = kb.write_txn

        @contextlib.contextmanager
        def acquire_then_advance(connection):
            with original_write_txn(connection):
                clock["now"] = 2_000
                yield

        monkeypatch.setattr(kb.time, "time", lambda: clock["now"])
        monkeypatch.setattr(kb, "write_txn", acquire_then_advance)
        authorization = kb.authorize_continuation(
            conn,
            task_id,
            [pr_tuple],
            reason="writer lock expiry regression",
            authorized_profile="engineer",
            authorized_provider="openai-codex",
            ttl_seconds=60,
        )

        assert authorization.created_at == 2_000
        assert authorization.expires_at == 2_060


def test_claim_rechecks_late_active_pr_under_writer_lock(kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="late PR", assignee="engineer")
        calls = 0

        def custody_after_precheck(_conn, _task_id, *, cutoff):
            nonlocal calls
            calls += 1
            if calls == 1:
                return (), ()
            return (
                (
                    {
                        "canonical_url": "https://github.com/o269/omnia/pull/568",
                        "source_comment_id": None,
                        "last_seen_at": cutoff,
                        "expires_at": cutoff + kb._RESPAWN_GUARD_PR_WINDOW,
                        "ownership": "declared",
                    },
                ),
                (),
            )

        # The unlocked precheck sees no custody. A declaration appears before
        # BEGIN IMMEDIATE is acquired, and the in-transaction recheck denies
        # the claim rather than opening a second writer.
        monkeypatch.setattr(kb, "_active_pr_candidates", custody_after_precheck)
        assert kb.claim_task(conn, task_id) is None
        assert calls == 2
        assert kb.get_task(conn, task_id).status == "ready"
        denial = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial == {
            "authorization_id": None,
            "phase": "claim_race",
            "reason": "missing_authorization",
        }


def test_claim_revalidates_remote_pr_state_under_writer_lock(
    kanban_home, monkeypatch
):
    """TOCTOU regression: pre-lock verification sees OPEN@A, post-lock
    verification sees CLOSED@B. The claim must be denied with an audited
    claim_race event, the task must stay ready, and the one-shot grant must
    remain active (unconsumed)."""
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        authorization = _authorize_continuation(conn, task_id, pr_tuple)
        remote = {"state": "OPEN", "head": _CONTINUATION_SHA_A}

        def verifier(pr):
            return kb.GitHubPRState(
                canonical_url=pr.canonical_url,
                state=remote["state"],
                is_draft=True,
                head_sha=remote["head"],
            )

        original_write_txn = kb.write_txn

        @contextlib.contextmanager
        def close_pr_after_lock_acquired(connection):
            with original_write_txn(connection):
                # PR owner closed + force-pushed while this writer was queued.
                remote["state"] = "CLOSED"
                remote["head"] = _CONTINUATION_SHA_B
                yield

        monkeypatch.setattr(kb, "_default_github_pr_verifier", verifier)
        monkeypatch.setattr(kb, "write_txn", close_pr_after_lock_acquired)

        assert kb.claim_task(conn, task_id) is None
        assert kb.get_task(conn, task_id).status == "ready"
        refreshed = kb.get_continuation_authorization(conn, authorization.id)
        assert refreshed.status() == "active"
        assert refreshed.consumed_at is None
        denial = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial == {
            "authorization_id": authorization.id,
            "phase": "claim_race",
            "reason": "pr_not_open",
        }


def test_claim_revalidates_remote_head_change_under_writer_lock(
    kanban_home, monkeypatch
):
    """TOCTOU regression: PR still open but head moved A -> B after the
    writer lock was acquired. Must deny with head_mismatch, no consume."""
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        authorization = _authorize_continuation(conn, task_id, pr_tuple)
        remote = {"state": "OPEN", "head": _CONTINUATION_SHA_A}

        def verifier(pr):
            return kb.GitHubPRState(
                canonical_url=pr.canonical_url,
                state=remote["state"],
                is_draft=True,
                head_sha=remote["head"],
            )

        original_write_txn = kb.write_txn

        @contextlib.contextmanager
        def force_push_after_lock_acquired(connection):
            with original_write_txn(connection):
                remote["head"] = _CONTINUATION_SHA_B
                yield

        monkeypatch.setattr(kb, "_default_github_pr_verifier", verifier)
        monkeypatch.setattr(kb, "write_txn", force_push_after_lock_acquired)

        assert kb.claim_task(conn, task_id) is None
        assert kb.get_task(conn, task_id).status == "ready"
        refreshed = kb.get_continuation_authorization(conn, authorization.id)
        assert refreshed.status() == "active"
        denial = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial == {
            "authorization_id": authorization.id,
            "phase": "claim_race",
            "reason": "head_mismatch",
        }


def test_claim_consumes_grant_when_remote_state_stable_under_writer_lock(
    kanban_home, monkeypatch
):
    """The post-lock revalidation must not break the happy path: a stable
    OPEN@A remote still claims and consumes the grant exactly once."""
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        authorization = _authorize_continuation(conn, task_id, pr_tuple)
        verifier_calls = {"count": 0}

        def verifier(pr):
            verifier_calls["count"] += 1
            return kb.GitHubPRState(
                canonical_url=pr.canonical_url,
                state="OPEN",
                is_draft=True,
                head_sha=pr.head_sha,
            )

        monkeypatch.setattr(kb, "_default_github_pr_verifier", verifier)

        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert claimed.status == "running"
        refreshed = kb.get_continuation_authorization(conn, authorization.id)
        assert refreshed.status() == "consumed"
        # Pre-lock guard evaluation + post-lock revalidation both ran.
        assert verifier_calls["count"] >= 2


_INVISIBLE_AUDIT_REASONS = [
    "\u200b",                    # ZERO WIDTH SPACE alone
    "\ufeff",                    # ZERO WIDTH NO-BREAK SPACE alone
    "\u2060",                    # WORD JOINER alone
    "\u034f",                    # COMBINING GRAPHEME JOINER (Mn)
    "\ufe00",                    # VARIATION SELECTOR-1 (Mn)
    "\U000e0100",                # VARIATION SELECTOR-17 (Mn)
    "\u115f",                    # HANGUL CHOSEONG FILLER (Lo)
    "\u1160",                    # HANGUL JUNGSEONG FILLER (Lo)
    "\u3164",                    # HANGUL FILLER (Lo)
    "\uffa0",                    # HALFWIDTH HANGUL FILLER (Lo)
    "\u2800",                    # BRAILLE PATTERN BLANK (So)
    "\u0301",                    # isolated COMBINING ACUTE ACCENT (Mn)
    "\u200b\ufeff\u2060",        # the exact original review-probe combination
    " \u034f \ufe00 \U000e0100 ", # mixed residual default-ignorables + whitespace
    "\t\n \u200b",               # mixed whitespace + format
    "\u00a0\u200b",              # NBSP (Zs) + format
]


@pytest.mark.parametrize("invisible", _INVISIBLE_AUDIT_REASONS)
def test_continuation_review_rejects_invisible_reasons(kanban_home, invisible):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="invisible review", assignee="engineer")
        with pytest.raises(kb.ContinuationAuthorizationError) as exc_info:
            kb.record_continuation_review(
                conn,
                task_id,
                verdict="fix-required",
                reason=invisible,
            )
        assert exc_info.value.code == "review_reason_required"


@pytest.mark.parametrize("invisible", _INVISIBLE_AUDIT_REASONS)
def test_authorize_continuation_rejects_invisible_reasons(kanban_home, invisible):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        with pytest.raises(kb.ContinuationAuthorizationError) as exc_info:
            kb.authorize_continuation(
                conn,
                task_id,
                [pr_tuple],
                reason=invisible,
                authorized_profile="engineer",
                authorized_provider="openai-codex",
            )
        assert exc_info.value.code == "reason_required"
        denial = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial["reason"] == "reason_required"


@pytest.mark.parametrize("invisible", _INVISIBLE_AUDIT_REASONS)
def test_operator_override_rejects_invisible_reasons(kanban_home, invisible):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        with pytest.raises(kb.ContinuationAuthorizationError) as exc_info:
            kb.claim_task(conn, task_id, operator_override_reason=invisible)
        assert exc_info.value.code == "operator_override_reason_required"
        assert kb.get_task(conn, task_id).status == "ready"
        denial = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "operator_claim_denied"
        ][-1]
        assert denial["reason"] == "operator_override_reason_required"


def test_visible_unicode_reasons_are_accepted_and_stored(kanban_home):
    """Normal Unicode text (CJK, accented Latin, emoji) stays intact."""
    reason = "修复 active PR — re\u0301paration 👩\u200d💻 ✓"
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="unicode review", assignee="engineer")
        event_id = kb.record_continuation_review(
            conn,
            task_id,
            verdict="fix-required",
            reason=reason,
        )
        events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_reviewed"
        ]
        assert events[-1].id == event_id
        assert events[-1].payload["reason"] == reason
        assert kb._normalize_visible_audit_reason(f"  {reason}  ") == reason


def test_operator_override_revalidates_identity_under_writer_lock(
    kanban_home, monkeypatch
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="override race", assignee="engineer")
        original_write_txn = kb.write_txn

        @contextlib.contextmanager
        def reassign_after_lock(connection):
            with original_write_txn(connection):
                connection.execute(
                    "UPDATE tasks SET assignee = 'default' WHERE id = ?",
                    (task_id,),
                )
                yield

        monkeypatch.setattr(kb, "write_txn", reassign_after_lock)
        claimed = kb.claim_task(
            conn,
            task_id,
            operator_override_reason="operator-approved follow-up",
        )
        assert claimed is None
        assert kb.get_task(conn, task_id).status == "ready"
        denied = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "operator_claim_denied"
        ][-1]
        assert denied["reason"] == "self_authorization_forbidden"


def test_worker_process_identity_is_persisted_on_spawn(kanban_home):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        with kb.connect() as conn:
            task_id = kb.create_task(conn, title="kernel identity", assignee="engineer")
            assert kb.claim_task(conn, task_id) is not None
            kb._set_worker_pid(conn, task_id, child.pid)
            run = conn.execute(
                "SELECT worker_pid, worker_pid_started, worker_boot_id, "
                "worker_sid, worker_pgid, worker_started_at, "
                "worker_group_started_at "
                "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            assert run["worker_pid"] == child.pid
            assert run["worker_pid_started"] is not None
            assert run["worker_boot_id"]
            assert run["worker_sid"] == child.pid
            assert run["worker_pgid"] == child.pid
            assert run["worker_started_at"] is not None
            assert run["worker_group_started_at"] is not None
            assert abs(
                run["worker_started_at"] - run["worker_group_started_at"]
            ) < 0.001
            run_model = kb.list_runs(conn, task_id)[0]
            assert run_model.worker_pid_started == run["worker_pid_started"]
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_delete_task_removes_continuation_and_pr_ownership_rows(kanban_home):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        authorization = _authorize_continuation(conn, task_id, pr_tuple)
        assert conn.execute(
            "SELECT 1 FROM task_pr_ownership WHERE task_id = ?",
            (task_id,),
        ).fetchone() is not None

        assert kb.delete_task(conn, task_id)
        assert kb.get_continuation_authorization(conn, authorization.id) is None
        assert conn.execute(
            "SELECT 1 FROM task_pr_ownership WHERE task_id = ?",
            (task_id,),
        ).fetchone() is None


def test_continuation_supersede_revokes_with_explicit_audit_event(kanban_home):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        first = _authorize_continuation(conn, task_id, pr_tuple)
        second = _authorize_continuation(conn, task_id, pr_tuple)

        first_readback = kb.get_continuation_authorization(conn, first.id)
        second_readback = kb.get_continuation_authorization(conn, second.id)
        assert first_readback is not None and first_readback.status() == "revoked"
        assert first_readback.revoked_at is not None
        assert second_readback is not None and second_readback.status() == "active"
        revoked = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_revoked"
        ]
        assert revoked[-1].payload == {
            "authorization_id": first.id,
            "reason": "superseded",
            "revoked_at": first_readback.revoked_at,
        }
        assert revoked[-1].created_at >= first_readback.revoked_at


@pytest.mark.parametrize(
    ("pr_mode", "overrides", "expected"),
    [
        ("none", {}, "pr_required"),
        ("duplicate", {}, "duplicate_pr_tuple"),
        ("valid", {"reason": ""}, "reason_required"),
        ("valid", {"authorized_profile": ""}, "profile_required"),
        ("valid", {"authorized_provider": ""}, "provider_required"),
        ("valid", {"ttl_seconds": 1}, "invalid_expiry"),
        ("valid", {"ttl_seconds": "soon"}, "invalid_expiry"),
    ],
)
def test_continuation_authorize_input_validation_fails_closed(
    kanban_home, pr_mode, overrides, expected
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        prs = {
            "none": [],
            "duplicate": [pr_tuple, pr_tuple],
            "valid": [pr_tuple],
        }[pr_mode]
        kwargs = {
            "reason": "repair",
            "authorized_profile": "engineer",
            "authorized_provider": "openai-codex",
            "ttl_seconds": kb.DEFAULT_CONTINUATION_AUTH_TTL_SECONDS,
        }
        kwargs.update(overrides)

        with pytest.raises(kb.ContinuationAuthorizationError) as denied:
            kb.authorize_continuation(conn, task_id, prs, **kwargs)

        assert denied.value.code == expected
        assert any(
            event.kind == "continuation_denied"
            and event.payload["reason"] == expected
            for event in kb.list_events(conn, task_id)
        )


def test_continuation_task_and_provider_state_fail_closed(kanban_home, monkeypatch):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        blocked_task = _create_continuation_task(conn, pr_tuple)
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?",
            (blocked_task,),
        )
        with pytest.raises(kb.ContinuationAuthorizationError) as not_ready:
            _authorize_continuation(conn, blocked_task, pr_tuple)
        assert not_ready.value.code == "task_not_ready"

        unresolved_task = _create_continuation_task(conn, pr_tuple)
        monkeypatch.setattr(
            kb,
            "_default_profile_provider_resolver",
            lambda _profile: None,
        )
        with pytest.raises(kb.ContinuationAuthorizationError) as unresolved:
            kb.authorize_continuation(
                conn,
                unresolved_task,
                [pr_tuple],
                reason="repair",
                authorized_profile="engineer",
                authorized_provider="openai-codex",
            )
        assert unresolved.value.code == "provider_unresolved"

        verifier_failure_task = _create_continuation_task(conn, pr_tuple)

        def fail_provider_lookup(_profile):
            raise RuntimeError("profile config unavailable")

        monkeypatch.setattr(
            kb,
            "_default_profile_provider_resolver",
            fail_provider_lookup,
        )
        with pytest.raises(kb.ContinuationAuthorizationError) as verifier_failure:
            kb.authorize_continuation(
                conn,
                verifier_failure_task,
                [pr_tuple],
                reason="repair",
                authorized_profile="engineer",
                authorized_provider="openai-codex",
            )
        assert verifier_failure.value.code == "provider_verifier_failure"


def test_continuation_active_set_and_verifier_identity_fail_closed(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    first = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    second = _continuation_tuple("o269", "omnia-v2", 198, _CONTINUATION_SHA_B)
    identity_tuple = _continuation_tuple(
        "o269", "omnia", 569, _CONTINUATION_SHA_B
    )
    with kb.connect() as conn:
        changed_task = _create_continuation_task(conn, first)
        _authorize_continuation(conn, changed_task, first)
        second_pr = kb.parse_continuation_pr_tuple(second)
        kb.add_comment(
            conn,
            changed_task,
            "engineer",
            f"Also opened {second_pr.canonical_url}",
        )
        changed = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert (changed_task, "active_pr") in changed.respawn_guarded
        changed_denial = [
            event.payload["reason"]
            for event in kb.list_events(conn, changed_task)
            if event.kind == "continuation_denied"
        ][-1]
        assert changed_denial == "active_pr_set_mismatch"

        identity_task = _create_continuation_task(conn, identity_tuple)
        _authorize_continuation(conn, identity_task, identity_tuple)

        def wrong_identity(pr):
            return kb.GitHubPRState(
                canonical_url="https://github.com/o269/other/pull/569",
                state="OPEN",
                is_draft=True,
                head_sha=pr.head_sha,
            )

        monkeypatch.setattr(kb, "_default_github_pr_verifier", wrong_identity)
        identity = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert (identity_task, "active_pr") in identity.respawn_guarded
        identity_denial = [
            event.payload["reason"]
            for event in kb.list_events(conn, identity_task)
            if event.kind == "continuation_denied"
        ][-1]
        assert identity_denial == "verifier_identity_mismatch"


def test_continuation_claim_race_expiry_fails_closed_without_consuming(
    kanban_home, monkeypatch
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        authorization = _authorize_continuation(conn, task_id, pr_tuple)
        original_get = kb.get_continuation_authorization

        def expire_during_claim(connection, authorization_id):
            readback = original_get(connection, authorization_id)
            assert readback is not None
            readback.expires_at = 0
            return readback

        monkeypatch.setattr(
            kb,
            "get_continuation_authorization",
            expire_during_claim,
        )
        claimed = kb.claim_task(
            conn,
            task_id,
        )

        assert claimed is None
        assert kb.get_task(conn, task_id).status == "ready"
        stored = conn.execute(
            "SELECT consumed_at FROM continuation_authorizations WHERE id = ?",
            (authorization.id,),
        ).fetchone()
        assert stored["consumed_at"] is None
        denial = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial == {
            "authorization_id": authorization.id,
            "phase": "claim_race",
            "reason": "authorization_expired",
        }


def test_operator_manual_claim_override_is_gated_and_audited(kanban_home):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        assert kb.claim_task(conn, task_id) is None
        claimed = kb.claim_task(
            conn,
            task_id,
            operator_override_reason="PR merged; run follow-up verification",
        )
        assert claimed is not None
        bypass = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "respawn_guard_bypassed"
        ][-1]
        assert bypass.payload == {
            "operator": "default",
            "reason": "PR merged; run follow-up verification",
            "guard_reason": "active_pr",
        }


def test_rate_limited_active_pr_retries_after_cooldown(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = int(time.time())
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="quota retry", assignee="engineer")
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'rate_limited', 'rate_limited', ?, ?)",
            (task_id, now - 700, now - 600),
        )
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("provider quota rate limit", task_id),
        )
        kb.add_comment(
            conn,
            task_id,
            "engineer",
            "Opened https://github.com/o269/omnia/pull/568",
        )
        assert kb.check_respawn_guard(conn, task_id) is None


def test_continuation_authorization_bypasses_prior_recent_success(
    kanban_home, all_assignees_spawnable
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    now = int(time.time())
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (task_id, now - 120, now - 60),
        )
        assert kb.check_respawn_guard(conn, task_id) == "recent_success"
        authorization = _authorize_continuation(conn, task_id, pr_tuple)
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert result.spawned and result.spawned[0][0] == task_id
        consumed = kb.get_continuation_authorization(conn, authorization.id)
        assert consumed is not None and consumed.status() == "consumed"


def test_spawn_failure_is_fresh_evidence_for_reauthorization(
    kanban_home, all_assignees_spawnable
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        first = _authorize_continuation(conn, task_id, pr_tuple)

        def fail_spawn(_task, _workspace):
            raise RuntimeError("launcher unavailable")

        kb.dispatch_once(
            conn,
            spawn_fn=fail_spawn,
            failure_limit=2,
        )
        assert kb.get_continuation_authorization(conn, first.id).status() == "consumed"
        assert kb.get_task(conn, task_id).status == "ready"
        second = _authorize_continuation(conn, task_id, pr_tuple)
        assert second.status() == "active"


def test_historic_nonterminal_card_still_owns_pr(kanban_home):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        old_owner = kb.create_task(conn, title="historic tracker", assignee="security")
        pr = kb.parse_continuation_pr_tuple(pr_tuple)
        kb.add_comment(conn, old_owner, "security", f"Tracked {pr.canonical_url}")
        conn.execute(
            "UPDATE task_comments SET created_at = ? WHERE task_id = ?",
            (int(time.time()) - kb._RESPAWN_GUARD_PR_WINDOW - 1, old_owner),
        )
        with pytest.raises(kb.ContinuationAuthorizationError) as denied:
            _authorize_continuation(conn, task_id, pr_tuple)
        assert denied.value.code == f"duplicate_pr_owner:{old_owner}"


def test_continuation_live_writer_fails_closed(
    kanban_home, all_assignees_spawnable
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        _authorize_continuation(conn, task_id, pr_tuple)
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) "
            "VALUES (?, 'other-writer', 'running', ?)",
            (task_id, int(time.time())),
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert (task_id, "active_pr") in result.respawn_guarded
        denial = [
            event.payload["reason"]
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ]
        assert "live_writer" in denial


def test_continuation_duplicate_card_pr_owner_fails_closed(
    kanban_home, all_assignees_spawnable
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        _authorize_continuation(conn, task_id, pr_tuple)
        duplicate_id = kb.create_task(conn, title="duplicate", assignee="security")
        pr = kb.parse_continuation_pr_tuple(pr_tuple)
        kb.add_comment(conn, duplicate_id, "worker", f"Also owns {pr.canonical_url}")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert (task_id, "active_pr") in result.respawn_guarded
        denial = [
            event.payload["reason"]
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial == f"duplicate_pr_owner:{duplicate_id}"


def test_continuation_multi_pr_ordered_exact_tuple_works(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    first = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    second = _continuation_tuple("o269", "omnia-v2", 198, _CONTINUATION_SHA_B)
    verified: list[str] = []

    def verifier(pr: kb.ContinuationPR) -> kb.GitHubPRState:
        verified.append(pr.canonical_url)
        return _open_draft_pr(pr)

    monkeypatch.setattr(kb, "_default_github_pr_verifier", verifier)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, first, second)
        authorization = kb.authorize_continuation(
            conn,
            task_id,
            [first, second],
            reason="repair both exact draft heads",
            authorized_profile="engineer",
            authorized_provider="openai-codex",
        )
        assert [pr.tuple_text for pr in authorization.prs] == [first, second]
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert result.spawned and result.spawned[0][0] == task_id
        assert verified == [
            kb.parse_continuation_pr_tuple(first).canonical_url,
            kb.parse_continuation_pr_tuple(second).canonical_url,
        ] * 4  # authorize, dispatch guard, claim guard, in-lock revalidation


def test_continuation_provider_mismatch_fails_closed(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        _authorize_continuation(conn, task_id, pr_tuple)
        monkeypatch.setattr(
            kb,
            "_default_profile_provider_resolver",
            lambda _profile: "deepseek",
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert (task_id, "active_pr") in result.respawn_guarded
        denial = [
            event.payload["reason"]
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial == "provider_mismatch"

        conn.execute(
            "UPDATE tasks SET assignee = 'security' WHERE id = ?",
            (task_id,),
        )
        reassigned = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert (task_id, "active_pr") in reassigned.respawn_guarded
        reassigned_denial = [
            event.payload["reason"]
            for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_denied"
        ][-1]
        assert reassigned_denial == "assignee_mismatch"


def test_continuation_timeout_evidence_must_still_be_unresolved(kanban_home):
    now = int(time.time())
    active_tuple = _continuation_tuple(
        "o269", "omnia", 568, _CONTINUATION_SHA_A
    )
    resolved_tuple = _continuation_tuple(
        "o269", "omnia", 569, _CONTINUATION_SHA_B
    )
    with kb.connect() as conn:
        active_task = kb.create_task(
            conn, title="timed out repair", assignee="engineer"
        )
        active_pr = kb.parse_continuation_pr_tuple(active_tuple)
        kb.add_comment(
            conn, active_task, "worker", f"Opened {active_pr.canonical_url}"
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'timed_out', 'timed_out', ?, ?)",
            (active_task, now - 60, now - 30),
        )
        authorization = _authorize_continuation(conn, active_task, active_tuple)
        assert authorization.status() == "active"

        resolved_task = kb.create_task(
            conn, title="resolved timeout", assignee="engineer"
        )
        resolved_pr = kb.parse_continuation_pr_tuple(resolved_tuple)
        kb.add_comment(
            conn, resolved_task, "worker", f"Opened {resolved_pr.canonical_url}"
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'timed_out', 'timed_out', ?, ?)",
            (resolved_task, now - 60, now - 30),
        )
        kb.record_continuation_review(
            conn,
            resolved_task,
            verdict="resolved",
            reason="security review passed",
        )
        with pytest.raises(
            kb.ContinuationAuthorizationError,
            match="repair evidence missing",
        ):
            _authorize_continuation(conn, resolved_task, resolved_tuple)


def test_continuation_expired_or_missing_exact_head_fails_closed(
    kanban_home, all_assignees_spawnable
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        expired_task = _create_continuation_task(conn, pr_tuple)
        expired = _authorize_continuation(conn, expired_task, pr_tuple)
        conn.execute(
            "UPDATE continuation_authorizations SET expires_at = ? WHERE id = ?",
            (int(time.time()) - 1, expired.id),
        )
        expired_result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert (expired_task, "active_pr") in expired_result.respawn_guarded

    with pytest.raises(kb.ContinuationAuthorizationError, match="PR tuples must"):
        kb.parse_continuation_pr_tuple("o269/omnia#568")

    with kb.connect() as conn:
        corrupt_tuple = _continuation_tuple(
            "o269", "omnia", 569, _CONTINUATION_SHA_B
        )
        corrupt_task = _create_continuation_task(conn, corrupt_tuple)
        corrupt = _authorize_continuation(conn, corrupt_task, corrupt_tuple)
        conn.execute(
            "UPDATE continuation_authorizations SET pr_tuples = ? WHERE id = ?",
            ('["o269/omnia#568"]', corrupt.id),
        )
        corrupt_result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: None,
        )
        assert (corrupt_task, "active_pr") in corrupt_result.respawn_guarded
        denial = [
            event.payload["reason"]
            for event in kb.list_events(conn, corrupt_task)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial == "missing_exact_head"


def test_continuation_schema_upgrade_is_idempotent(tmp_path):
    db_path = tmp_path / "pre-continuation-kanban.db"
    kb.init_db(db_path)
    with kb.connect(db_path) as conn:
        task_id = kb.create_task(conn, title="preserved", assignee="engineer")
        conn.execute("DROP INDEX idx_continuation_auth_one_live")
        conn.execute("DROP INDEX idx_continuation_auth_task")
        conn.execute("DROP TABLE continuation_authorizations")

    kb.init_db(db_path)
    kb.init_db(db_path)
    with kb.connect(db_path) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'continuation_authorizations'"
        ).fetchone()
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert table is not None
        assert kb.get_task(conn, task_id).title == "preserved"
        assert {
            "idx_continuation_auth_task",
            "idx_continuation_auth_one_live",
            "idx_comments_created_at",
        } <= indexes
        run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")
        }
        assert {
            "worker_pid",
            "worker_pid_started",
            "worker_pgid",
            "worker_sid",
            "worker_boot_id",
            "worker_started_at",
            "worker_group_started_at",
        } <= run_columns


def test_respawn_guard_live_worker_snapshot_is_structured(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="live owner", assignee="alice")
        board_db, board_slug = kb._connection_worker_board_identity(conn)
        snapshot = [
            kb._WorkerProcessSnapshot(
                pid=43210,
                task_id=task_id,
                run_id=None,
                pgid=43210,
                sid=43210,
                create_time=123.0,
                board_db=board_db,
                board_slug=board_slug,
                boot_id="boot-test",
            )
        ]

        decision = kb.evaluate_respawn_guard(
            conn,
            task_id,
            process_snapshot=snapshot,
        )
        reason = kb.check_respawn_guard(
            conn,
            task_id,
            process_snapshot=snapshot,
        )

    assert isinstance(decision, kb.RespawnGuardDecision)
    assert decision.reason == "live_worker_process"
    assert reason == "live_worker_process"


def test_respawn_guard_old_pr_comment_not_guarded(kanban_home):
    """Integer and legacy-text timestamps older than the PR window do not block."""
    old_ts = int(time.time()) - kb._RESPAWN_GUARD_PR_WINDOW - 60
    old_created_ats = (
        old_ts,
        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(old_ts)),
    )
    with kb.connect() as conn:
        for index, old_created_at in enumerate(old_created_ats):
            task_id = kb.create_task(
                conn,
                title=f"old-pr-{index}",
                assignee="alice",
            )
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, 'worker', "
                "'PR: https://github.com/totemx-AI/subsidysmart/pull/10', ?)",
                (task_id, old_created_at),
            )
            assert kb.check_respawn_guard(conn, task_id) is None


def test_dispatch_respawn_guard_defers_auth_error_without_auto_block(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once defers (does NOT auto-block) a ready task whose last
    error is a blocker_auth.

    The old behaviour auto-blocked on first occurrence, which was too
    aggressive: a transient 429 rate-limit (which typically clears in
    seconds to minutes) would end up requiring manual unblock. The new
    behaviour defers the spawn this tick; the task stays in ``ready``
    and gets another chance next tick. If the auth error genuinely
    persists, the existing ``consecutive_failures`` circuit breaker
    will auto-block via the normal failure-limit path.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="quota-storm", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("rate limit exceeded: 429 Too Many Requests", t),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    # Critical: task is NOT auto-blocked on first occurrence.
    assert t not in res.auto_blocked, (
        f"blocker_auth should defer, not auto-block on first occurrence; "
        f"got auto_blocked={res.auto_blocked!r}"
    )
    # It IS recorded as respawn_guarded with the reason.
    assert (t, "blocker_auth") in res.respawn_guarded, (
        f"expected (task_id, 'blocker_auth') in respawn_guarded; "
        f"got {res.respawn_guarded!r}"
    )
    # And it's NOT spawned this tick.
    assert t not in spawned_ids
    # Status stays ``ready`` so a future tick (or operator action) can
    # retry without manual unblock.
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_skips_recent_success(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with a recent completed run."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="recent-winner", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "recent_success") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"  # not blocked, just skipped


def test_dispatch_respawn_guard_skips_active_pr(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with an active PR comment."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.add_comment(
            conn, t, "worker",
            "Opened https://github.com/totemx-AI/subsidysmart/pull/99",
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "active_pr") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"
        denial = [
            event.payload["reason"]
            for event in kb.list_events(conn, t)
            if event.kind == "continuation_denied"
        ][-1]
        assert denial == "missing_authorization"


def test_dispatch_respawn_guard_dry_run_no_auto_block(
    kanban_home, all_assignees_spawnable
):
    """In dry_run mode, blocker_auth tasks are recorded in respawn_guarded (not auto-blocked)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="dry-quota", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("quota exceeded", t),
        )
        res = kb.dispatch_once(conn, dry_run=True)

    assert (t, "blocker_auth") in res.respawn_guarded
    assert t not in res.auto_blocked
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"  # dry_run: no writes


def test_dispatch_respawn_guard_allows_clean_task(
    kanban_home, all_assignees_spawnable
):
    """A task with no guard triggers is spawned normally."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="clean-task", assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert t in spawned_ids
    assert not res.respawn_guarded
    assert t not in res.auto_blocked


def test_dispatch_respawn_guard_emits_event_for_skipped_task(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once emits a respawn_guarded task_event so operators can diagnose stuck-ready tasks."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="event-check", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert "respawn_guarded" in kinds
    guarded_evt = next(e for e in events if e.kind == "respawn_guarded")
    # Event.payload is already parsed as a dict by list_events.
    assert isinstance(guarded_evt.payload, dict)
    assert guarded_evt.payload.get("reason") == "recent_success"


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def test_scratch_workspace_created_under_hermes_home(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)
    assert ws.exists()
    assert ws.is_dir()
    assert "kanban" in str(ws)


def test_dir_workspace_honors_given_path(kanban_home, tmp_path):
    target = tmp_path / "my-vault"
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="biz", workspace_kind="dir", workspace_path=str(target)
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)
    assert ws == target
    assert ws.exists()


def test_worktree_workspace_repo_root_anchor_materializes_linked_worktree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="ship", workspace_kind="worktree", workspace_path=str(repo)
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)

    expected = repo / ".worktrees" / t
    assert ws == expected
    assert ws.exists()
    repo_common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ws_common = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws_common == repo_common
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {expected}" in listed
    assert f"branch refs/heads/wt/{t}" in listed


def test_worktree_no_path_anchors_on_board_default_workdir(kanban_home, tmp_path):
    """A worktree task created with no explicit path inherits the board's
    default_workdir as its anchor and materializes a per-task linked worktree
    at ``<repo>/.worktrees/<id>`` — NOT the dispatcher's CWD, and NOT the
    shared default_workdir verbatim (which would collapse every task into one
    directory)."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    kb.create_board("wt-default-board", default_workdir=str(repo))
    with kb.connect(board="wt-default-board") as conn:
        t = kb.create_task(
            conn, title="ship", workspace_kind="worktree", board="wt-default-board"
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task, board="wt-default-board")

    expected = repo / ".worktrees" / t
    assert ws == expected
    assert ws.exists()
    assert ws != repo  # not the shared default verbatim


def test_worktree_no_path_no_board_default_raises(kanban_home, tmp_path, monkeypatch):
    """With neither an explicit workspace_path nor a board default_workdir,
    resolution fails loudly pointing at default_workdir / worktree:<path> —
    rather than silently materializing under the dispatcher's CWD (the old
    behavior that scattered worktrees under whatever dir launched the
    gateway)."""
    # Park the dispatcher CWD inside a real git repo so the OLD cwd-anchored
    # code would have "succeeded" — proving the new code does NOT use cwd.
    decoy_repo = tmp_path / "decoy"
    _init_git_repo(decoy_repo)
    monkeypatch.chdir(decoy_repo)
    with kb.connect() as conn:
        t = kb.create_task(conn, title="ship", workspace_kind="worktree")
        task = kb.get_task(conn, t)
        assert task is not None
        with pytest.raises(ValueError, match="default_workdir"):
            kb.resolve_workspace(task)


def test_worktree_workspace_explicit_target_materializes_linked_worktree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / ".worktrees" / "custom-task"
    branch = "wt/custom-task"
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="ship",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name=branch,
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)

    assert ws == target
    assert ws.exists()
    repo_common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ws_common = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws_common == repo_common
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {target}" in listed
    assert f"branch refs/heads/{branch}" in listed


def test_dispatch_worktree_task_persists_materialized_workspace_and_branch(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    kb.create_board("worktree-board", default_workdir=str(repo))
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return None

    with kb.connect(board="worktree-board") as conn:
        tid = kb.create_task(
            conn,
            title="ship",
            assignee="sentinel",
            workspace_kind="worktree",
            board="worktree-board",
        )
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="worktree-board")
        task = kb.get_task(conn, tid)

    expected = repo / ".worktrees" / tid
    assert result.spawned == [(tid, "sentinel", str(expected))]
    assert spawns == [(tid, str(expected))]
    assert task is not None
    assert task.workspace_path == str(expected)
    assert task.branch_name == f"wt/{tid}"
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {expected}" in listed
    assert f"branch refs/heads/wt/{tid}" in listed


def test_dispatch_worktree_task_rerun_reuses_existing_linked_worktree_and_branch(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    kb.create_board("worktree-rerun-board", default_workdir=str(repo))
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return None

    with kb.connect(board="worktree-rerun-board") as conn:
        tid = kb.create_task(
            conn,
            title="ship",
            assignee="sentinel",
            workspace_kind="worktree",
            board="worktree-rerun-board",
        )
        first = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="worktree-rerun-board")
        first_task = kb.get_task(conn, tid)
        assert first_task is not None
        expected = repo / ".worktrees" / tid
        assert first_task.workspace_path == str(expected)
        assert first_task.branch_name == f"wt/{tid}"

        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (tid,),
        )
        conn.commit()

        second = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="worktree-rerun-board")
        second_task = kb.get_task(conn, tid)

    assert first.spawned == [(tid, "sentinel", str(expected))]
    assert second.spawned == [(tid, "sentinel", str(expected))]
    assert spawns == [(tid, str(expected)), (tid, str(expected))]
    assert second_task is not None
    assert second_task.workspace_path == str(expected)
    actual_branch = subprocess.run(
        ["git", "-C", str(expected), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert actual_branch == f"wt/{tid}"
    assert second_task.branch_name == actual_branch
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert listed.count(f"worktree {expected}\n") == 1
    assert f"worktree {expected}/.worktrees/{tid}" not in listed
    assert f"branch refs/heads/{actual_branch}" in listed


# ---------------------------------------------------------------------------
# Scratch cleanup containment (#28818)
# ---------------------------------------------------------------------------

def test_cleanup_workspace_removes_managed_scratch_dir(kanban_home):
    """A scratch workspace under the kanban workspaces root is removed."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="scratchy")
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        assert ws.is_dir()
        kb.complete_task(conn, t, result="ok")
    assert not ws.exists(), "Hermes-managed scratch dir should be cleaned up"


def test_complete_task_persists_scratch_artifacts_before_cleanup(kanban_home):
    """Completion artifacts from scratch workspaces survive workspace cleanup."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="render chart")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        artifact = ws / "chart.png"
        artifact.write_bytes(b"png-bytes")

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(artifact)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        persisted = Path(completed.payload["artifacts"][0])
        run = kb.latest_run(conn, t)

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert persisted.exists(), "artifact copy should survive scratch cleanup"
    assert persisted.parent == kb.task_attachments_dir(t)
    assert persisted.name == "chart.png"
    assert persisted.read_bytes() == b"png-bytes"
    assert str(persisted) != str(artifact)
    assert run is not None
    assert run.metadata["artifacts"] == [str(persisted)]
    with kb.connect() as conn:
        attachments = kb.list_attachments(conn, t)
    assert [(a.filename, a.stored_path) for a in attachments] == [
        ("chart.png", str(persisted.resolve()))
    ]


def test_complete_task_rejects_missing_declared_scratch_artifact(kanban_home):
    """A declared scratch deliverable must not disappear behind a false Done."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="missing report")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        missing = ws / "report.md"

        with pytest.raises(kb.ArtifactPreservationError, match="unavailable"):
            kb.complete_task(
                conn,
                t,
                result="report complete",
                metadata={"artifacts": [str(missing)]},
            )

        assert kb.get_task(conn, t).status == "ready"
        assert kb.list_attachments(conn, t) == []
    assert ws.exists(), "failed completion must keep scratch available for retry"


def test_complete_task_preserves_legacy_artifact_path_from_summary(kanban_home):
    """Summary-only workers keep the file they tell the user was delivered."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="legacy report")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        report = ws / "report.md"
        report.write_text("legacy deliverable", encoding="utf-8")

        assert kb.complete_task(
            conn,
            t,
            summary=f"Task complete — delivered {report}",
        )
        run = kb.latest_run(conn, t)

    persisted = Path(run.metadata["artifacts"][0])
    assert not ws.exists()
    assert persisted.read_text(encoding="utf-8") == "legacy deliverable"
    assert persisted.parent == kb.task_attachments_dir(t)


def test_complete_task_leaves_non_scratch_artifact_paths_unchanged(
    kanban_home,
    tmp_path,
):
    """Only artifacts inside the managed scratch workspace are copied."""
    external = tmp_path / "report.md"
    external.write_text("keep me here", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(conn, title="external report")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(external)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        run = kb.latest_run(conn, t)

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert external.exists()
    assert completed.payload["artifacts"] == [str(external)]
    assert run is not None
    assert run.metadata["artifacts"] == [str(external)]


def test_complete_task_persists_duplicate_scratch_artifact_names(kanban_home):
    """Scratch artifact persistence does not overwrite duplicate basenames."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="render reports")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        first = ws / "a" / "report.txt"
        second = ws / "b" / "report.txt"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(first), str(second)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        persisted = [Path(p) for p in completed.payload["artifacts"]]

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert [p.name for p in persisted] == ["report.txt", "report_1.txt"]
    assert [p.read_text(encoding="utf-8") for p in persisted] == ["first", "second"]
    assert all(p.parent == kb.task_attachments_dir(t) for p in persisted)


def test_complete_task_persists_board_scratch_artifacts_to_board_attachments(kanban_home):
    """Board scratch artifacts are copied under that board's attachment root."""
    kb.create_board("work-proj")

    with kb.connect(board="work-proj") as conn:
        t = kb.create_task(conn, title="board chart", board="work-proj")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task, board="work-proj")
        kb.set_workspace_path(conn, t, ws)
        artifact = ws / "chart.png"
        artifact.write_bytes(b"board-png")

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(artifact)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        persisted = Path(completed.payload["artifacts"][0])

    assert not ws.exists(), "board scratch workspace should still be cleaned up"
    assert persisted.exists()
    assert persisted.parent == kb.task_attachments_dir(t, board="work-proj")


def test_cleanup_workspace_refuses_path_outside_scratch_root(kanban_home, tmp_path):
    """A scratch task with a user path outside the workspaces root must NOT be deleted (#28818).

    Reproduces the data-loss vector where a board's ``default_workdir`` is set
    to a real source directory; tasks created without an explicit
    ``workspace_kind`` inherit ``scratch`` semantics, and the old cleanup path
    would ``shutil.rmtree`` the user's source tree on task completion.
    """
    real_source = tmp_path / "real-source"
    real_source.mkdir()
    (real_source / ".git").mkdir()
    (real_source / "README.md").write_text("important", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(conn, title="ship")
        # Simulate the bad state directly: workspace_kind='scratch' (default)
        # but workspace_path pointing at the user's real source tree, which is
        # exactly what board.default_workdir produces when the task is created
        # without an explicit workspace_kind.
        conn.execute(
            "UPDATE tasks SET workspace_kind=?, workspace_path=? WHERE id=?",
            ("scratch", str(real_source), t),
        )
        conn.commit()
        kb.complete_task(conn, t, result="ok")

    assert real_source.exists(), "User source tree must not be deleted by scratch cleanup"
    assert (real_source / ".git").exists()
    assert (real_source / "README.md").read_text(encoding="utf-8") == "important"


def test_cleanup_workspace_honors_workspaces_root_env_override(tmp_path, monkeypatch):
    """``HERMES_KANBAN_WORKSPACES_ROOT`` extends the managed-scratch set.

    Worker subprocesses run with this env var injected by the dispatcher. The
    cleanup containment check must treat paths under it as managed even when
    they sit outside the active kanban home.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspaces_override = tmp_path / "ext-workspaces"
    workspaces_override.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(workspaces_override))
    kb.init_db()

    with kb.connect() as conn:
        t = kb.create_task(conn, title="ext")
        scratch_dir = workspaces_override / t
        scratch_dir.mkdir()
        conn.execute(
            "UPDATE tasks SET workspace_kind=?, workspace_path=? WHERE id=?",
            ("scratch", str(scratch_dir), t),
        )
        conn.commit()
        kb.complete_task(conn, t, result="ok")

    assert not scratch_dir.exists(), "Override-root scratch dir should be cleaned up"


# ---------------------------------------------------------------------------
# Deferred scratch cleanup for parent/child handoff (#33774)
# ---------------------------------------------------------------------------

def test_cleanup_workspace_deferred_while_child_active(kanban_home):
    """A scratch parent's workspace survives completion while a child is still active.

    The dependency chain (parents=[A]) must guarantee child B can read A's
    handoff artifacts. The old cleanup deleted A's scratch dir immediately on
    A's completion, before B ever ran.
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent, child)  # child depends on parent
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)
        assert parent_ws.is_dir()
        # Parent completes; child is still 'todo' -> cleanup must be deferred.
        kb.complete_task(conn, parent, result="handoff written")

    assert parent_ws.exists(), (
        "Parent scratch workspace must survive while a linked child is active"
    )


def test_cleanup_workspace_swept_after_last_child_completes(kanban_home):
    """Once all children are terminal, the deferred parent scratch dir is removed."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent, child)
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)
        # Give the child its own scratch dir too.
        c_task = kb.get_task(conn, child)
        child_ws = kb.resolve_workspace(c_task)
        kb.set_workspace_path(conn, child, child_ws)

        kb.complete_task(conn, parent, result="ok")
        assert parent_ws.exists(), "deferred while child active"

        # Child completes -> recompute promotes nothing new; the child's
        # cleanup sweep should now reap the parent's deferred workspace.
        kb.complete_task(conn, child, result="done")

    assert not parent_ws.exists(), (
        "Parent scratch workspace should be swept once all children are terminal"
    )
    assert not child_ws.exists(), "Child scratch workspace should be cleaned up too"


def test_dir_child_completion_unblocks_deferred_scratch_parent(kanban_home, tmp_path):
    """A non-scratch ('dir') child completing must still sweep its scratch parent.

    Regression for the gap where ``_cleanup_workspace`` returned early for a
    non-scratch task and never ran the parent sweep — leaking the parent's
    deferred scratch dir forever.
    """
    child_dir = tmp_path / "persistent-child"
    child_dir.mkdir()
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="scratch parent")
        child = kb.create_task(
            conn, title="dir child", workspace_kind="dir",
            workspace_path=str(child_dir),
        )
        kb.link_tasks(conn, parent, child)
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)

        kb.complete_task(conn, parent, result="handoff")
        assert parent_ws.exists(), "deferred while dir child active"

        kb.complete_task(conn, child, result="built")

    assert not parent_ws.exists(), (
        "A 'dir' child completing must trigger the parent scratch sweep"
    )
    assert child_dir.exists(), "Non-scratch 'dir' child workspace is never deleted"


def test_is_managed_scratch_path_accepts_per_board_workspaces(kanban_home, tmp_path):
    """Per-board scratch dirs under ``<kanban_home>/kanban/boards/<slug>/workspaces`` are managed."""
    board_scratch = kanban_home / "kanban" / "boards" / "my-board" / "workspaces" / "task-1"
    board_scratch.mkdir(parents=True)
    assert kb._is_managed_scratch_path(board_scratch)


def test_is_managed_scratch_path_rejects_real_source_tree(kanban_home, tmp_path):
    """A path outside any managed root (e.g. a user's repo) is NOT managed."""
    real = tmp_path / "code" / "my-project"
    real.mkdir(parents=True)
    assert not kb._is_managed_scratch_path(real)


def test_is_managed_scratch_path_rejects_kanban_metadata_subtrees(kanban_home):
    """Hermes' own DB/metadata/log subtrees under ``<kanban_home>/kanban`` are NOT managed.

    Regression guard for the Copilot finding on #28819: a scratch task whose
    ``workspace_path`` was mis-set to the kanban home, the logs dir, or a
    board's metadata dir (i.e. the board root itself, not its ``workspaces/``
    child) must be refused. Without this, the containment check would happily
    ``shutil.rmtree`` Hermes' DB/metadata/logs on task completion.
    """
    kanban_root = kanban_home / "kanban"
    kanban_root.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(kanban_root)

    logs_dir = kanban_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(logs_dir)

    board_root = kanban_root / "boards" / "my-board"
    board_root.mkdir(parents=True, exist_ok=True)
    # The board root itself is NOT a managed scratch dir — only the
    # ``workspaces/`` child (and its descendants) are.
    assert not kb._is_managed_scratch_path(board_root)

    # Sibling subtrees of ``workspaces/`` under a board (e.g. its kanban.db
    # or board.json living next to ``workspaces/``) are also not managed.
    board_logs = board_root / "logs"
    board_logs.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(board_logs)

    # Now create the board's workspaces dir and a task scratch dir under it —
    # the latter is the only thing the guard should allow.
    board_workspaces = board_root / "workspaces"
    board_workspaces.mkdir(parents=True, exist_ok=True)
    # The workspaces root itself is also NOT managed — deleting it would
    # wipe every task's scratch dir at once.
    assert not kb._is_managed_scratch_path(board_workspaces)
    task_dir = board_workspaces / "task-42"
    task_dir.mkdir(parents=True, exist_ok=True)
    assert kb._is_managed_scratch_path(task_dir)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------

def test_tenant_column_filters_listings(kanban_home):
    with kb.connect() as conn:
        kb.create_task(conn, title="a1", tenant="biz-a")
        kb.create_task(conn, title="b1", tenant="biz-b")
        kb.create_task(conn, title="shared")  # no tenant
        biz_a = kb.list_tasks(conn, tenant="biz-a")
        biz_b = kb.list_tasks(conn, tenant="biz-b")
    assert [t.title for t in biz_a] == ["a1"]
    assert [t.title for t in biz_b] == ["b1"]


def test_list_tasks_filters_workflow_template_and_step(kanban_home):
    with kb.connect() as conn:
        ta = kb.create_task(conn, title="alpha")
        tb = kb.create_task(conn, title="beta")
        conn.execute(
            "UPDATE tasks SET workflow_template_id=?, current_step_key=? WHERE id=?",
            ("wf1", "step_x", ta),
        )
        conn.execute(
            "UPDATE tasks SET workflow_template_id=?, current_step_key=? WHERE id=?",
            ("wf1", "step_y", tb),
        )
        conn.commit()
        by_wf = kb.list_tasks(conn, workflow_template_id="wf1")
        by_step = kb.list_tasks(conn, current_step_key="step_x")
    assert {x.id for x in by_wf} == {ta, tb}
    assert [x.id for x in by_step] == [ta]


def test_list_runs_state_filter_requires_pair_and_valid_type(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="both"):
            kb.list_runs(conn, tid, state_type="status", state_name=None)
        with pytest.raises(ValueError, match="both"):
            kb.list_runs(conn, tid, state_type=None, state_name="done")
        with pytest.raises(ValueError, match="state_type"):
            kb.list_runs(conn, tid, state_type="nope", state_name="done")


def test_list_runs_filters_by_outcome_value(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        kb.complete_task(conn, tid, summary="ok")
        matching = kb.list_runs(conn, tid, state_type="outcome", state_name="completed")
        empty = kb.list_runs(conn, tid, state_type="outcome", state_name="blocked")
    assert matching
    assert not empty


def test_tenant_propagates_to_events(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="tenant-task", tenant="biz-a")
        events = kb.list_events(conn, t)
    # The "created" event should have tenant in its payload.
    created = [e for e in events if e.kind == "created"]
    assert created and created[0].payload.get("tenant") == "biz-a"


# ---------------------------------------------------------------------------
# Originating session id (ACP propagation)
# ---------------------------------------------------------------------------

def test_create_task_stamps_session_id(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="from chat", session_id="acp-sess-123"
        )
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.session_id == "acp-sess-123"


def test_create_task_session_id_defaults_to_none(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="cli-created")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.session_id is None


def test_session_id_filters_listings(kanban_home):
    with kb.connect() as conn:
        kb.create_task(conn, title="s1-a", session_id="sess-1")
        kb.create_task(conn, title="s1-b", session_id="sess-1")
        kb.create_task(conn, title="s2-a", session_id="sess-2")
        kb.create_task(conn, title="cli-only")  # no session
        sess1 = kb.list_tasks(conn, session_id="sess-1")
        sess2 = kb.list_tasks(conn, session_id="sess-2")
        unscoped = kb.list_tasks(conn)
    assert sorted(t.title for t in sess1) == ["s1-a", "s1-b"]
    assert [t.title for t in sess2] == ["s2-a"]
    # Unscoped list still returns everything (legacy NULL rows visible).
    assert len(unscoped) == 4


def test_session_id_index_exists(kanban_home):
    """The migration creates an index on session_id for cheap per-session
    list queries on busy boards. Without it, a chat-scoped poll would
    full-scan the tasks table."""
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='tasks'"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert "idx_tasks_session_id" in names


def test_session_id_compose_with_tenant_filter(kanban_home):
    """A client may want both `tenant=scarf:foo` AND `session=acp-x` —
    the filters must AND, not replace."""
    with kb.connect() as conn:
        kb.create_task(
            conn, title="match", tenant="scarf:foo", session_id="acp-x"
        )
        kb.create_task(
            conn, title="wrong-tenant", tenant="other", session_id="acp-x"
        )
        kb.create_task(
            conn, title="wrong-session",
            tenant="scarf:foo", session_id="acp-y",
        )
        rows = kb.list_tasks(
            conn, tenant="scarf:foo", session_id="acp-x"
        )
    assert [t.title for t in rows] == ["match"]


# ---------------------------------------------------------------------------
# Shared-board path resolution (issue #19348)
#
# The kanban board is a cross-profile coordination primitive: a worker
# spawned with `hermes -p <profile>` must read/write the same kanban.db
# as the dispatcher that claimed the task. These tests exercise the
# path-resolution layer directly and would have caught the regression
# where `kanban_db_path()` resolved to the active profile's HERMES_HOME.
# ---------------------------------------------------------------------------

class TestSharedBoardPaths:
    """`kanban_home`/`kanban_db_path`/`workspaces_root`/`worker_log_path`
    must anchor at the **shared root**, not the active profile's HERMES_HOME."""

    def _set_home(self, monkeypatch, tmp_path, hermes_home):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)

    def test_default_install_anchors_at_home_dot_hermes(
        self, tmp_path, monkeypatch
    ):
        # Standard install: HERMES_HOME == ~/.hermes, no profile active.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)

        assert kb.kanban_home() == default_home
        assert kb.kanban_db_path() == default_home / "kanban.db"
        assert kb.workspaces_root() == default_home / "kanban" / "workspaces"
        assert (
            kb.worker_log_path("t_demo")
            == default_home / "kanban" / "logs" / "t_demo.log"
        )

    def test_profile_worker_resolves_to_shared_root(
        self, tmp_path, monkeypatch
    ):
        # Reproduces the bug: dispatcher uses ~/.hermes/kanban.db,
        # worker spawned with -p <profile> previously resolved to
        # ~/.hermes/profiles/<profile>/kanban.db. After the fix both
        # converge on ~/.hermes/kanban.db.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "nehemiahkanban"
        profile_home.mkdir(parents=True)
        self._set_home(monkeypatch, tmp_path, profile_home)

        # All four resolvers must anchor at the shared root, not the
        # profile-local HERMES_HOME.
        assert kb.kanban_home() == default_home
        assert kb.kanban_db_path() == default_home / "kanban.db"
        assert kb.workspaces_root() == default_home / "kanban" / "workspaces"
        assert (
            kb.worker_log_path("t_0d214f19")
            == default_home / "kanban" / "logs" / "t_0d214f19.log"
        )

        # Sanity: the profile-local path that used to be returned is
        # explicitly NOT what we resolve to anymore.
        assert kb.kanban_db_path() != profile_home / "kanban.db"

    def test_dispatcher_and_profile_worker_converge(
        self, tmp_path, monkeypatch
    ):
        # End-to-end convergence: resolve the path under each side's
        # HERMES_HOME and confirm equality. This is the property the
        # dispatcher/worker handoff actually depends on.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "coder"
        profile_home.mkdir(parents=True)

        # Dispatcher's perspective.
        self._set_home(monkeypatch, tmp_path, default_home)
        dispatcher_db = kb.kanban_db_path()
        dispatcher_ws = kb.workspaces_root()
        dispatcher_log = kb.worker_log_path("t_handoff")

        # Worker's perspective (profile activated by `hermes -p coder`).
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        worker_db = kb.kanban_db_path()
        worker_ws = kb.workspaces_root()
        worker_log = kb.worker_log_path("t_handoff")

        assert dispatcher_db == worker_db
        assert dispatcher_ws == worker_ws
        assert dispatcher_log == worker_log

    def test_docker_custom_hermes_home_uses_env_path_directly(
        self, tmp_path, monkeypatch
    ):
        # Docker / custom deployment: HERMES_HOME points outside ~/.hermes.
        # `get_default_hermes_root()` returns env_home directly when it
        # is not a `<root>/profiles/<name>` shape and not under
        # `Path.home() / ".hermes"`.
        custom_root = tmp_path / "opt" / "hermes"
        custom_root.mkdir(parents=True)
        self._set_home(monkeypatch, tmp_path, custom_root)

        assert kb.kanban_home() == custom_root
        assert kb.kanban_db_path() == custom_root / "kanban.db"

    def test_docker_profile_layout_uses_grandparent(
        self, tmp_path, monkeypatch
    ):
        # Docker profile shape: HERMES_HOME=/opt/hermes/profiles/coder;
        # `get_default_hermes_root()` walks up to /opt/hermes because
        # the immediate parent dir is named "profiles".
        custom_root = tmp_path / "opt" / "hermes"
        profile = custom_root / "profiles" / "coder"
        profile.mkdir(parents=True)
        self._set_home(monkeypatch, tmp_path, profile)

        assert kb.kanban_home() == custom_root
        assert kb.kanban_db_path() == custom_root / "kanban.db"

    def test_explicit_override_via_hermes_kanban_home(
        self, tmp_path, monkeypatch
    ):
        # Explicit override: HERMES_KANBAN_HOME beats every other
        # resolution rule.
        default_home = tmp_path / ".hermes"
        profile_home = default_home / "profiles" / "any"
        profile_home.mkdir(parents=True)
        override = tmp_path / "shared-board"
        override.mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(override))

        assert kb.kanban_home() == override
        assert kb.kanban_db_path() == override / "kanban.db"
        assert kb.workspaces_root() == override / "kanban" / "workspaces"

    def test_empty_override_falls_through(self, tmp_path, monkeypatch):
        # Empty/whitespace override is treated as unset.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", "   ")

        assert kb.kanban_home() == default_home

    def test_dispatcher_and_worker_share_a_real_database(
        self, tmp_path, monkeypatch
    ):
        # Belt-and-suspenders: round-trip a task across the two
        # HERMES_HOME perspectives via a real SQLite file. Without the
        # fix the worker would open a different file and see no rows.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "nehemiahkanban"
        profile_home.mkdir(parents=True)

        # Dispatcher creates the board and a task.
        self._set_home(monkeypatch, tmp_path, default_home)
        kb.init_db()
        with kb.connect() as conn:
            task_id = kb.create_task(conn, title="cross-profile")

        # Worker switches to the profile HERMES_HOME and reads.
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        with kb.connect() as conn:
            task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.title == "cross-profile"

    def test_hermes_kanban_db_pin_beats_kanban_home(
        self, tmp_path, monkeypatch
    ):
        # HERMES_KANBAN_DB pins the file path directly and beats both
        # HERMES_KANBAN_HOME and the `get_default_hermes_root()` path.
        # This is the env the dispatcher injects into workers.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        umbrella = tmp_path / "umbrella"
        umbrella.mkdir()
        pinned_db = tmp_path / "pinned" / "board.db"
        pinned_db.parent.mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(umbrella))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned_db))

        assert kb.kanban_db_path() == pinned_db
        # workspaces_root still follows HERMES_KANBAN_HOME -- the pins
        # are independent.
        assert kb.workspaces_root() == umbrella / "kanban" / "workspaces"

    def test_hermes_kanban_workspaces_root_pin_beats_kanban_home(
        self, tmp_path, monkeypatch
    ):
        # HERMES_KANBAN_WORKSPACES_ROOT pins the workspaces root directly.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        umbrella = tmp_path / "umbrella"
        umbrella.mkdir()
        pinned_ws = tmp_path / "pinned-workspaces"
        pinned_ws.mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(umbrella))
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(pinned_ws))

        assert kb.workspaces_root() == pinned_ws
        # kanban_db_path still follows HERMES_KANBAN_HOME.
        assert kb.kanban_db_path() == umbrella / "kanban.db"

    def test_empty_per_path_overrides_fall_through(
        self, tmp_path, monkeypatch
    ):
        # Empty/whitespace pins are treated as unset, same as
        # HERMES_KANBAN_HOME.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.setenv("HERMES_KANBAN_DB", "   ")
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", "")

        assert kb.kanban_db_path() == default_home / "kanban.db"
        assert kb.workspaces_root() == default_home / "kanban" / "workspaces"

    def test_dispatcher_spawn_injects_kanban_db_and_workspaces_root(
        self, tmp_path, monkeypatch
    ):
        # The dispatcher's `_default_spawn` must inject HERMES_KANBAN_DB
        # and HERMES_KANBAN_WORKSPACES_ROOT into the worker env so the
        # worker converges on the dispatcher's paths even when the
        # `-p <profile>` flag rewrites HERMES_HOME.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env", {})
                self.pid = 4242

        monkeypatch.setattr("subprocess.Popen", _FakePopen)

        task = kb.Task(
            id="t_dispatch_env",
            title="x",
            body=None,
            assignee="coder",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
            branch_name="wt/t_dispatch_env",
        )
        kb._default_spawn(task, str(tmp_path / "ws"))

        env = captured["env"]
        assert env["HERMES_KANBAN_DB"] == str(default_home / "kanban.db")
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(
            default_home / "kanban" / "workspaces"
        )
        assert env["HERMES_KANBAN_TASK"] == "t_dispatch_env"
        assert env["HERMES_KANBAN_BRANCH"] == "wt/t_dispatch_env"


# ---------------------------------------------------------------------------
# latest_summary / latest_summaries — surface task_runs.summary handoffs
# ---------------------------------------------------------------------------

def test_latest_summary_returns_none_when_no_runs(kanban_home):
    """A freshly-created task has no runs and therefore no summary."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="fresh", assignee="alice")
        assert kb.latest_summary(conn, t) is None


def test_latest_summary_returns_summary_after_complete(kanban_home):
    """``complete_task(summary=...)`` is the canonical kanban-worker
    handoff; ``latest_summary`` must surface it so dashboards/CLI can
    render what the worker actually did."""
    handoff = "shipped 3 files, ran tests, opened PR #42"
    with kb.connect() as conn:
        t = kb.create_task(conn, title="work", assignee="alice")
        kb.complete_task(conn, t, summary=handoff)
        assert kb.latest_summary(conn, t) == handoff


def test_latest_summary_picks_newest_when_multiple_runs(kanban_home):
    """When a task has been re-run (block → unblock → complete), the
    newest run's summary wins. We unblock to take the task back to
    ``ready``, then complete a second time and verify the second
    summary surfaces."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="retry", assignee="alice")
        kb.complete_task(conn, t, summary="first attempt")
        # Move back to ready by direct SQL — block_task / unblock_task
        # paths require an active claim, but we just want a second run
        # row to exist with a later ended_at.
        conn.execute(
            "UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?",
            (t,),
        )
        # Sleep 1s so the second run's ended_at is provably later than
        # the first (complete_task uses int(time.time())).
        time.sleep(1.05)
        kb.complete_task(conn, t, summary="second attempt — final")
        assert kb.latest_summary(conn, t) == "second attempt — final"


def test_latest_summary_skips_empty_string(kanban_home):
    """A run with an empty-string summary should not mask an earlier
    populated one — empty strings carry no information."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="t", assignee="alice")
        kb.complete_task(conn, t, summary="real handoff")
        # Inject a later run with empty summary directly. Workers
        # writing "" instead of None is a real shape we want to ignore.
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at, "
            "outcome, summary) VALUES (?, 'done', ?, ?, 'completed', ?)",
            (t, int(time.time()) + 1, int(time.time()) + 2, ""),
        )
        conn.commit()
        assert kb.latest_summary(conn, t) == "real handoff"


def test_latest_summaries_batch_omits_tasks_without_summary(kanban_home):
    """``latest_summaries`` is the dashboard's N+1 escape hatch — it
    must return only entries for tasks that actually have a summary,
    keep the per-task latest, and accept an empty input gracefully."""
    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="a", assignee="alice")
        t2 = kb.create_task(conn, title="b", assignee="bob")
        t3 = kb.create_task(conn, title="c", assignee="carol")
        kb.complete_task(conn, t1, summary="alpha")
        kb.complete_task(conn, t3, summary="charlie")
        out = kb.latest_summaries(conn, [t1, t2, t3])
        assert out == {t1: "alpha", t3: "charlie"}
        # Empty input → empty dict, no SQL syntax error from "IN ()".
        assert kb.latest_summaries(conn, []) == {}



# ---------------------------------------------------------------------------
# NFS / network-filesystem fallback (see hermes_state.apply_wal_with_fallback)
# ---------------------------------------------------------------------------

def test_connect_falls_back_to_delete_on_locking_protocol(tmp_path, monkeypatch, caplog):
    """kanban_db.connect() must handle ``locking protocol`` on NFS/SMB.

    Without this fallback, the gateway's kanban dispatcher crashes every
    60s and the kanban migration (``consecutive_failures`` ADD COLUMN) is
    retried forever — which is what the real-world user report shows
    (see hermes-agent issue #22032).

    NOTE: We do NOT use the ``kanban_home`` fixture here because that
    fixture pre-initializes the DB via ``kb.init_db()`` — putting the
    file in WAL on disk. The Bug D safety guard now refuses to downgrade
    to DELETE when the on-disk header is already WAL, so testing the
    NFS-fallback path requires a truly-fresh DB file (NFS scenario in
    production: first connection of the first process ever to touch the
    file, where downgrading is safe because nobody else has WAL state
    yet).
    """
    import sqlite3 as _sqlite3
    from unittest.mock import patch as _patch

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Clear module cache so a fresh connect() is attempted
    kb._INITIALIZED_PATHS.clear()

    real_connect = _sqlite3.connect

    class _WalBlockingConnection(_sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                raise _sqlite3.OperationalError("locking protocol")
            return super().execute(sql, *args, **kwargs)

    def wal_blocking_connect(*args, **kwargs):
        return real_connect(
            *args, factory=_WalBlockingConnection, **kwargs
        )

    with _patch("hermes_cli.kanban_db.sqlite3.connect", side_effect=wal_blocking_connect):
        with caplog.at_level("WARNING", logger="hermes_state"):
            conn = kb.connect()

    # One fallback warning, naming kanban.db
    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "kanban.db" in r.getMessage()
    ]
    assert len(warnings) >= 1, (
        f"Expected a kanban.db WARNING, got: {[r.getMessage() for r in caplog.records]}"
    )

    # DB still usable end-to-end — create + list a task
    t = kb.create_task(conn, title="post-fallback task")
    tasks = kb.list_tasks(conn)
    assert any(row.id == t for row in tasks)
    conn.close()


def test_unlink_tasks_triggers_recompute_ready(kanban_home):
    """Regression test for issue #22459.

    Removing a dependency via unlink_tasks must immediately promote the child
    to ready when all remaining parents are done — same contract as
    complete_task and unblock_task.

    Before the fix, child stayed 'todo' indefinitely after unlink; only the
    next dispatcher tick or a manual 'hermes kanban recompute' would promote it.
    """
    with kb.connect() as conn:
        # A is done.
        a = kb.create_task(conn, title="parent-done")
        kb.complete_task(conn, a)

        # C is running (not done) — blocks child B.
        c = kb.create_task(conn, title="parent-running")
        kb.claim_task(conn, c, claimer="worker:1")

        # B depends on both A (done) and C (running) → stays todo.
        b = kb.create_task(conn, title="child", parents=[a, c])
        assert kb.get_task(conn, b).status == "todo"

        # Remove the blocking dependency C → B.
        removed = kb.unlink_tasks(conn, c, b)
        assert removed is True

        # B's only remaining parent is A (done) → must be ready immediately.
        assert kb.get_task(conn, b).status == "ready", (
            "child should promote to ready immediately after unlink_tasks "
            "removes its last blocking dependency"
        )


def test_archive_task_triggers_recompute_ready_for_dependents(kanban_home):
    """Archiving a parent must immediately unblock its children.

    ``recompute_ready()`` already treats ``archived`` parents as satisfied
    dependencies, just like ``done``. Regression: ``archive_task()`` updated
    the parent row but never ran the ready-promotion pass, so children stayed
    stuck in ``todo`` until a later dispatcher tick.
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="obsolete parent")
        child = kb.create_task(conn, title="child", parents=[parent])

        assert kb.get_task(conn, child).status == "todo"
        assert kb.archive_task(conn, parent) is True

        assert kb.get_task(conn, child).status == "ready", (
            "child should promote to ready immediately after its last blocking "
            "parent is archived"
        )

# ---------------------------------------------------------------------------
# _add_column_if_missing / _migrate_add_optional_columns idempotency (#21708)
# ---------------------------------------------------------------------------

def test_add_column_if_missing_is_idempotent_on_race(kanban_home):
    """``_add_column_if_missing`` must swallow 'duplicate column name' errors.

    Regression for #21708: the kanban dispatcher opens the DB twice per tick
    (once via _tick_once_for_board, once via init_db's discard-and-reconnect
    path).  A second concurrent connection runs _migrate_add_optional_columns
    before the first one commits, so ALTER TABLE raises OperationalError with
    'duplicate column name: consecutive_failures'.  Without the idempotency
    guard that crashes the dispatcher on the first tick after every restart.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
    )

    # First call adds the column — returns True.
    added = kb._add_column_if_missing(conn, "tasks", "extra_col", "extra_col TEXT")
    assert added is True
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "extra_col" in cols

    # Second call on same connection — column already exists — must return
    # False without raising, simulating the race the dispatcher hits.
    added_again = kb._add_column_if_missing(
        conn, "tasks", "extra_col", "extra_col TEXT"
    )
    assert added_again is False

    conn.close()


def test_migrate_add_optional_columns_tolerates_concurrent_migration(kanban_home):
    """Full _migrate_add_optional_columns must not raise when columns already
    exist (issue #21708 race window — two connections migrate concurrently)."""
    import sqlite3

    # Schema already in fully-migrated state (all optional columns present).
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            tenant TEXT,
            result TEXT,
            idempotency_key TEXT,
            branch_name TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            worker_pid INTEGER,
            last_failure_error TEXT,
            max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER,
            current_run_id INTEGER,
            workflow_template_id TEXT,
            current_step_key TEXT,
            skills TEXT,
            max_retries INTEGER,
            session_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL DEFAULT '',
            run_id     INTEGER,
            kind       TEXT NOT NULL DEFAULT '',
            payload    TEXT,
            created_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    # Running migration on an already-migrated schema must not raise.
    kb._migrate_add_optional_columns(conn)
    conn.close()


# ---------------------------------------------------------------------------
# Dispatcher spawn invocation — _resolve_hermes_argv()
#
# Workers spawned by the dispatcher must use a `hermes` invocation that does
# not depend on PATH being set up correctly. cron jobs, systemd User= services,
# launchd jobs, and other detached processes routinely run with a stripped
# $PATH that doesn't include the venv's bin/, so a bare `["hermes", ...]`
# spawn fails with FileNotFoundError and the task gets stuck. The resolver
# prefers the PATH shim (familiar `ps` output) but falls back to the module
# form so the spawn keeps working when PATH is missing the shim.
# ---------------------------------------------------------------------------


def test_resolve_hermes_argv_prefers_path_shim(monkeypatch):
    """When `hermes` is on PATH, use the shim — preserves familiar ps output."""
    import shutil
    import hermes_cli.kanban_db as kb

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/hermes")
    argv = kb._resolve_hermes_argv()
    assert argv == ["/usr/local/bin/hermes"]


def test_resolve_hermes_argv_absolutizes_relative_exe_shim(monkeypatch, tmp_path):
    """A relative executable override must not remain workspace-cwd-dependent."""
    import hermes_cli.kanban_db as kb

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_BIN", ".\\hermes.exe")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [os.path.abspath(".\\hermes.exe")]


def test_resolve_hermes_argv_avoids_implicit_windows_batch_shim(monkeypatch, tmp_path):
    """Implicit .cmd/.bat shims use the module fallback, not batch argv[0]."""
    import sys
    import hermes_cli.kanban_db as kb

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hermes.CMD").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_honors_hermes_bin_path_override(monkeypatch, tmp_path):
    """An explicit path-like HERMES_BIN lets service managers pin the executable."""
    import shutil
    import hermes_cli.kanban_db as kb

    shim = tmp_path / "bin" / "hermes"
    shim.parent.mkdir()
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_BIN", str(shim))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert kb._resolve_hermes_argv() == [str(shim)]


def test_resolve_hermes_argv_hermes_bin_bare_name_uses_path(monkeypatch, tmp_path):
    """Bare HERMES_BIN values keep PATH semantics instead of cwd shadowing."""
    import stat
    import hermes_cli.kanban_db as kb

    cwd_hermes = tmp_path / "hermes"
    cwd_hermes.write_text("wrong\n", encoding="utf-8")
    cwd_hermes.chmod(cwd_hermes.stat().st_mode | stat.S_IXUSR)
    path_hermes = tmp_path / "bin" / "hermes"
    path_hermes.parent.mkdir()
    path_hermes.write_text("right\n", encoding="utf-8")
    path_hermes.chmod(path_hermes.stat().st_mode | stat.S_IXUSR)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(path_hermes.parent))
    monkeypatch.setenv("HERMES_BIN", "hermes")

    assert kb._resolve_hermes_argv() == [str(path_hermes)]


def test_resolve_hermes_argv_hermes_bin_bare_name_ignores_cwd(monkeypatch, tmp_path):
    """Bare HERMES_BIN does not accept current-directory shadow executables."""
    import sys
    import hermes_cli.kanban_db as kb

    (tmp_path / "hermes.exe").write_text("wrong\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HERMES_BIN", "hermes")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_hermes_bin_bare_cmd_uses_module_fallback(monkeypatch, tmp_path):
    """A PATH-resolved HERMES_BIN batch shim is not used as worker argv[0]."""
    import sys
    import hermes_cli.kanban_db as kb

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hermes.CMD").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setenv("HERMES_BIN", "hermes")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_hermes_bin_unresolved_bare_name_falls_back(monkeypatch):
    """Unresolved HERMES_BIN command names do not delegate cwd search to Popen."""
    import sys
    import hermes_cli.kanban_db as kb

    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HERMES_BIN", "hermes")

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_falls_back_to_module_form_when_no_path_shim(monkeypatch):
    """When the shim is not on PATH, fall back to `python -m hermes_cli.main`.

    Pins the correct module name (NOT `hermes` — there is no top-level
    `hermes` package). Regression for #23198: the original PR shipped
    `python -m hermes` which fails with `No module named hermes` on every
    invocation.
    """
    import shutil
    import sys
    import hermes_cli.kanban_db as kb

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv = kb._resolve_hermes_argv()
    assert argv == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_module_actually_runs():
    """The fallback module name must be importable + runnable.

    A unit test that pins the literal string is necessary but not
    sufficient — if `hermes_cli.main` ever loses `if __name__ == "__main__"`
    handling or its argparse setup, `python -m hermes_cli.main --version`
    would fail and so would every dispatcher spawn that hits the fallback.
    Run it as a real subprocess to catch that regression.
    """
    import subprocess
    import hermes_cli.kanban_db as kb
    import shutil
    import unittest.mock as mock

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_BIN", None)
        with mock.patch.object(shutil, "which", return_value=None):
            argv = kb._resolve_hermes_argv()
    r = subprocess.run(argv + ["--version"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (
        f"`{' '.join(argv)} --version` failed (rc={r.returncode}); "
        f"stderr={r.stderr[:200]!r}"
    )
    assert "Hermes Agent" in r.stdout, f"unexpected output: {r.stdout[:200]!r}"


# ---------------------------------------------------------------------------
# task_age — guard against corrupt timestamp values
#
# The Task dataclass declares ``created_at: int`` but rows come from sqlite
# without coercion at the boundary. A row that ever held a non-int (e.g. an
# unsubstituted ``'%s'`` from a logged format string, ``None``, an arbitrary
# string, or a float-as-string) used to crash ``task_age`` with ``ValueError``
# and turn ``GET /api/plugins/kanban/board`` into a 500 because the dashboard
# calls ``task_age`` unguarded for every task in the response.
#
# After the fix, ``_safe_int`` returns ``None`` on bad input and ``task_age``
# degrades gracefully (per-field ``None`` rather than a hard crash).
# ---------------------------------------------------------------------------


def _make_task(**overrides) -> "kb.Task":
    """Minimal Task with all required fields filled in. Override anything."""
    defaults = dict(
        id="t_age",
        title="x",
        body=None,
        assignee=None,
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    defaults.update(overrides)
    return kb.Task(**defaults)


def test_safe_int_accepts_int_and_int_string():
    """Sanity: well-typed values pass through."""
    # PR d8ad431de renamed _safe_int → _to_epoch (now also handles ISO-8601).
    assert kb._to_epoch(0) == 0
    assert kb._to_epoch(1700000000) == 1700000000
    assert kb._to_epoch("1700000000") == 1700000000


def test_safe_int_returns_none_on_corrupt_inputs():
    """All the failure modes that used to crash task_age."""
    # None — common when the column was never written
    assert kb._to_epoch(None) is None
    # Unsubstituted format string — the literal case the PR title cites
    assert kb._to_epoch("%s") is None
    # Arbitrary non-numeric strings
    assert kb._to_epoch("abc") is None
    assert kb._to_epoch("") is None
    # Float-ish strings: int("1.5") raises ValueError too — caller wants None.
    assert kb._to_epoch("1.5") is None
    # Random object — covered by TypeError branch
    assert kb._to_epoch(object()) is None


def test_task_age_handles_corrupt_created_at():
    """Pre-fix this raised ValueError and 500'd /api/plugins/kanban/board."""
    t = _make_task(created_at="%s")
    age = kb.task_age(t)
    assert age["created_age_seconds"] is None
    assert age["started_age_seconds"] is None
    assert age["time_to_complete_seconds"] is None


def test_task_age_handles_corrupt_started_and_completed():
    """All three timestamp fields share the same _safe_int treatment."""
    t = _make_task(
        created_at=1700000000,
        started_at="garbage",
        completed_at=None,
    )
    age = kb.task_age(t)
    assert isinstance(age["created_age_seconds"], int)
    assert age["started_age_seconds"] is None
    assert age["time_to_complete_seconds"] is None


def test_task_age_well_formed_task():
    """Regression: the safe-int path must not change behavior for normal data."""
    import time
    now = int(time.time())
    t = _make_task(
        created_at=now - 60,
        started_at=now - 30,
        completed_at=now,
    )
    age = kb.task_age(t)
    assert 55 <= age["created_age_seconds"] <= 65
    assert 25 <= age["started_age_seconds"] <= 35
    assert 25 <= age["time_to_complete_seconds"] <= 35


def test_task_dict_survives_corrupt_created_at(tmp_path, monkeypatch):
    """Defense in depth: even if task_age ever raised, plugin_api must not 500.

    The PR also added a try/except around the task_age call in
    `plugins/kanban/dashboard/plugin_api.py::_task_dict`. Verify a single
    corrupt row doesn't turn the whole board response into an error.
    """
    # Set up an isolated kanban home so we can write a corrupt created_at.
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    # Insert a row with a non-int created_at (simulates the historical
    # bug that produced corrupt rows).
    conn = kb.connect()
    try:
        good_id = kb.create_task(conn, title="good")
        # Now write a row with corrupt created_at directly.
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ("%s", good_id),
        )
    finally:
        conn.close()

    # Re-read and pass through task_age — must not raise.
    conn = kb.connect()
    try:
        task = kb.get_task(conn, good_id)
    finally:
        conn.close()
    age = kb.task_age(task)
    assert age["created_age_seconds"] is None


# ---------------------------------------------------------------------------
# Board-level default_workdir
# ---------------------------------------------------------------------------


def test_create_task_scratch_without_workspace_ignores_board_default_workdir(kanban_home, monkeypatch):
    """Scratch tasks must NOT inherit board.default_workdir — would point auto-cleanup
    at the user's source tree on completion (#28818)."""
    default_wd = "/home/user/project"
    kb.create_board("work-proj", default_workdir=default_wd)

    with kb.connect(board="work-proj") as conn:
        tid = kb.create_task(conn, title="scratch-task", board="work-proj")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_kind == "scratch"
    assert t.workspace_path is None


def test_create_task_dir_without_workspace_inherits_board_default_workdir(kanban_home, monkeypatch):
    """Board default_workdir is for persistent dir/worktree workspaces, not scratch."""
    default_wd = "/home/user/project"
    kb.create_board("work-proj-dir", default_workdir=default_wd)

    with kb.connect(board="work-proj-dir") as conn:
        tid = kb.create_task(
            conn,
            title="inherited",
            workspace_kind="dir",
            board="work-proj-dir",
        )
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_path == default_wd


def test_create_task_without_workspace_no_default_stays_none(kanban_home):
    """Board without default_workdir → create_task without workspace_path → stays None."""
    kb.create_board("empty-board")

    with kb.connect(board="empty-board") as conn:
        tid = kb.create_task(conn, title="none", board="empty-board")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_path is None


def test_create_task_with_explicit_workspace_ignores_board_default(kanban_home):
    """create_task with explicit workspace_path → ignores board default."""
    kb.create_board("custom-ws-board", default_workdir="/board/default")

    explicit = "/my/explicit/path"
    with kb.connect(board="custom-ws-board") as conn:
        tid = kb.create_task(conn, title="explicit", workspace_path=explicit, board="custom-ws-board")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_path == explicit
    assert t.workspace_path != "/board/default"


# ---------------------------------------------------------------------------
# dispatch_once — max_in_progress
# ---------------------------------------------------------------------------


def test_dispatch_max_in_progress_skips_when_at_limit(kanban_home, all_assignees_spawnable):
    """When max_in_progress=N and N tasks are already running, spawn nothing."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect() as conn:
        # Two running tasks.
        t1 = kb.create_task(conn, title="a", assignee="alice")
        t2 = kb.create_task(conn, title="b", assignee="bob")
        kb.claim_task(conn, t1)
        kb.claim_task(conn, t2)
        # Two more ready to spawn — but cap is 2 so none should fire.
        kb.create_task(conn, title="c", assignee="bob")
        kb.create_task(conn, title="d", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=2)

    assert len(spawns) == 0, f"expected 0 spawns, got {len(spawns)}"


def test_dispatch_max_in_progress_spawns_up_to_cap(
    kanban_home, all_assignees_spawnable
):
    """When max_in_progress=3 and one runs, fill two slots with two profiles."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect() as conn:
        # One running task.
        t1 = kb.create_task(conn, title="a", assignee="alice")
        kb.claim_task(conn, t1)
        # Three ready tasks — global headroom permits only the first two.
        kb.create_task(conn, title="b", assignee="bob")
        kb.create_task(conn, title="c", assignee="carol")
        kb.create_task(conn, title="d", assignee="dave")
        kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=3)

    assert len(spawns) == 2


def test_dispatch_max_in_progress_none_is_unlimited(
    kanban_home, all_assignees_spawnable
):
    """Default None means no global limit; distinct profiles all spawn."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect() as conn:
        for title, assignee in zip(
            ["a", "b", "c", "d"], ["alice", "bob", "carol", "dave"]
        ):
            kb.create_task(conn, title=title, assignee=assignee)
        kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=None)

    assert len(spawns) == 4

# Review column dispatch
# ---------------------------------------------------------------------------


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    """Test helper: set a task's status directly."""
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def test_claim_review_task_transitions_to_running(kanban_home):
    """claim_review_task atomically transitions review -> running."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.claim_lock is not None


def test_claim_review_task_fails_on_non_review(kanban_home):
    """claim_review_task returns None if task is not in review status."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="ready task", assignee="alice")
        # Task is in 'ready', not 'review'
        claimed = kb.claim_review_task(conn, t)
    assert claimed is None


def test_claim_review_task_fails_when_already_claimed(kanban_home):
    """claim_review_task returns None if the task was already claimed."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        first = kb.claim_review_task(conn, t)
        assert first is not None
        second = kb.claim_review_task(conn, t)
    assert second is None


def test_dispatch_review_dry_run(kanban_home, all_assignees_spawnable):
    """dispatch_once dry-run sees review tasks and reports them as spawned."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == t
    # Dry run must NOT mutate status.
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "review"


def test_dispatch_review_spawns_with_correct_skills(
    kanban_home, all_assignees_spawnable,
):
    """Review tasks get sdlc-review skill set before spawning."""
    spawned_tasks = []

    def capture_spawn(task, workspace, board=None):
        spawned_tasks.append(task)
        return 42  # fake PID

    with kb.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, spawn_fn=capture_spawn)
    assert len(res.spawned) == 1
    assert len(spawned_tasks) == 1
    assert spawned_tasks[0].skills == ["sdlc-review"]


def test_dispatch_review_skips_unassigned(kanban_home):
    """Unassigned review tasks go to skipped_unassigned, not spawned."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review floater")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_unassigned
    assert not res.spawned


def test_dispatch_review_counts_toward_max_spawn(
    kanban_home, all_assignees_spawnable,
):
    """Review spawns count against max_spawn alongside ready tasks."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        # Create 2 ready tasks + 1 review task, max_spawn=2
        t1 = kb.create_task(conn, title="ready 1", assignee="alice")
        t2 = kb.create_task(conn, title="ready 2", assignee="bob")
        t3 = kb.create_task(conn, title="review", assignee="alice")
        _set_task_status(conn, t3, "review")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)
    # Only 2 should spawn (ready tasks get priority in the loop)
    assert len(res.spawned) == 2
    assert len(spawns) == 2


def test_dispatch_review_spawns_when_ready_empty(
    kanban_home, all_assignees_spawnable,
):
    """When only review tasks exist, they still get dispatched."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
    assert len(res.spawned) == 1
    assert spawns[0] == t


def test_has_spawnable_review_true(kanban_home):
    """has_spawnable_review returns True when review tasks exist with real profiles."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="default")
        _set_task_status(conn, t, "review")
        # default profile should exist in the test env
        assert kb.has_spawnable_review(conn) is True


def test_has_spawnable_review_false_on_empty(kanban_home):
    """has_spawnable_review returns False when no review tasks exist."""
    with kb.connect() as conn:
        assert kb.has_spawnable_review(conn) is False


def test_has_spawnable_review_false_when_only_terminal_lanes(
    kanban_home, monkeypatch,
):
    """has_spawnable_review returns False when review tasks are terminal lanes."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review", assignee="orion-cc")
        _set_task_status(conn, t, "review")
        assert kb.has_spawnable_review(conn) is False


def test_dispatch_review_skips_nonspawnable(kanban_home, monkeypatch):
    """Review tasks with non-existent profiles go to skipped_nonspawnable."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review", assignee="orion-cc")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_nonspawnable
    assert not res.spawned


def test_review_status_in_valid_statuses():
    """'review' is a valid task status."""
    assert "review" in kb.VALID_STATUSES


def test_dispatch_review_does_not_claim_ready_tasks(
    kanban_home, all_assignees_spawnable,
):
    """Review dispatch uses claim_review_task, which only claims review tasks."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="ready task", assignee="alice")
        # claim_review_task should NOT claim a ready task
        claimed = kb.claim_review_task(conn, t)
    assert claimed is None

# Stale detection — detect_stale_running
# ---------------------------------------------------------------------------

def test_detect_stale_returns_running_task_with_no_heartbeat(kanban_home, monkeypatch):
    """A task running > timeout with zero heartbeats gets reclaimed as stale."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="stale-no-hb", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        # Rewind started_at so the task appears to have been running for 5 hours.
        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )
        # No heartbeat set — last_heartbeat_at stays NULL.

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        killed = []
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: killed.append(s),
        )
        assert t in stale, "Task with no heartbeat for >4h should be reclaimed"
        task = kb.get_task(conn, t)
        assert task.status == "ready"


def test_detect_stale_returns_task_with_stale_heartbeat(kanban_home, monkeypatch):
    """A task running > timeout with a heartbeat older than 1h gets reclaimed."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="stale-hb", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        heartbeat_2h_ago = int(time.time()) - (2 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? "
                "WHERE id = ?",
                (five_hours_ago, heartbeat_2h_ago, t),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert t in stale, (
            "Task with heartbeat >1h old and started >4h ago should be stale"
        )
        assert kb.get_task(conn, t).status == "ready"


def test_detect_stale_skips_task_with_recent_heartbeat(kanban_home, monkeypatch):
    """A task running > timeout but with a recent heartbeat is NOT reclaimed."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="alive-hb", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        heartbeat_now = int(time.time())  # heartbeat just happened
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? "
                "WHERE id = ?",
                (five_hours_ago, heartbeat_now, t),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == [], "Task with recent heartbeat should not be reclaimed"
        assert kb.get_task(conn, t).status == "running"


def test_detect_stale_skips_recently_started_task(kanban_home, monkeypatch):
    """A task started < timeout ago is NOT reclaimed even with no heartbeat."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="fresh", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        # Started only 1 hour ago — well within the 4h threshold.
        one_hour_ago = int(time.time()) - 3600
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (one_hour_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (one_hour_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == [], "Task started <4h ago should not be reclaimed"
        assert kb.get_task(conn, t).status == "running"


def test_detect_stale_skips_when_timeout_zero(kanban_home, monkeypatch):
    """stale_timeout_seconds=0 disables stale detection entirely."""

    with kb.connect() as conn:
        t = kb.create_task(conn, title="disabled", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=0, signal_fn=lambda p, s: None,
        )
        assert stale == [], "timeout=0 should disable stale detection"
        assert kb.get_task(conn, t).status == "running"


def test_detect_stale_skips_blocked_tasks(kanban_home, monkeypatch):
    """Blocked tasks are NOT reclaimed by stale detection."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="blocked-task", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )
        # Block the task explicitly.
        kb.block_task(conn, t, reason="human requested block")

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == [], "Blocked task should not be reclaimed by stale detection"
        assert kb.get_task(conn, t).status == "blocked"


def test_detect_stale_does_not_tick_failure_counter(kanban_home, monkeypatch):
    """Stale reclaim must NOT tick consecutive_failures.

    Stale detection is dispatcher-side absence-of-heartbeat detection,
    not a worker failure. Counting it as a failure would let two
    legitimately-long-running tasks (>4h without explicit heartbeat) trip
    the circuit breaker and auto-block at the default failure_limit=2,
    even though no worker actually failed. The 'stale' event in
    task_events is the right audit surface; the consecutive_failures
    counter is reserved for spawn_failed / timed_out / crashed.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="stale-no-counter-tick", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )
            # Counter starts at 0; assert that's our baseline.
            row = conn.execute(
                "SELECT consecutive_failures FROM tasks WHERE id = ?", (t,)
            ).fetchone()
            assert row["consecutive_failures"] in (0, None)

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert t in stale, "Task should be reclaimed by stale detection"

        # Critical assertion: the failure counter MUST NOT have ticked.
        # Stale reclaim resets to ready for re-dispatch without penalty.
        row = conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id = ?", (t,)
        ).fetchone()
        assert row["consecutive_failures"] in (0, None), (
            f"Stale reclaim ticked consecutive_failures to "
            f"{row['consecutive_failures']!r}; should remain 0/NULL."
        )

        # And the audit trail still records the stale event so operators
        # can see what happened.
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t,),
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "stale" in kinds, (
            f"Expected 'stale' event in task_events; got {kinds!r}"
        )


# ---------------------------------------------------------------------------
# Corruption guard (issue #30687)
# ---------------------------------------------------------------------------

def _write_corrupt_db(path: Path) -> bytes:
    """Write a kanban DB with a VALID SQLite header but malformed page content.

    This is the corruption shape the integrity guard specifically targets
    (e.g. issue #29507 follow-up reports where the file's first 16 bytes
    pass the header byte check but ``PRAGMA integrity_check`` then fails
    because the internal pages are damaged). It's what main's header-only
    validator was letting through, and what this PR adds the full guard
    for.
    """
    # 100-byte SQLite header (magic + minimal valid-looking fields) so the
    # cheap header check passes, then deliberate garbage so sqlite refuses
    # to read the file past the header.
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    header = header.ljust(100, b"\x00")
    payload = b"definitely not a valid sqlite page \x00\x01\x02\x03" * 64
    blob = header + payload
    path.write_bytes(blob)
    return blob


def test_init_db_refuses_corrupt_existing_file(tmp_path):
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)
    # Ensure the cache doesn't mask the guard.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
        kb.init_db(db_path=db_path)

    err = excinfo.value
    assert err.db_path == db_path
    assert err.backup_path is not None
    assert err.backup_path.exists()
    assert err.backup_path.read_bytes() == original
    # Original bytes untouched — no schema was written on top.
    assert db_path.read_bytes() == original
    assert str(db_path) in str(err)
    assert str(err.backup_path) in str(err)


def test_connect_refuses_corrupt_existing_file(tmp_path):
    db_path = tmp_path / "kanban.db"
    _write_corrupt_db(db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with pytest.raises(kb.KanbanDbCorruptError):
        kb.connect(db_path=db_path)


def test_repeated_corrupt_open_reuses_single_backup(tmp_path):
    """Repeated quarantines of the same corrupt bytes must not amplify disk usage.

    Regression for the gateway dispatcher's 5-min retry loop on shared kanban
    DBs across multi-profile fleets: each retry on an unchanged corrupt file
    used to create a fresh ``.corrupt.<timestamp>.bak`` until disk filled. The
    content-addressed backup name is deterministic in the DB's sha256, so
    N retries of the same bytes share one backup.
    """
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)

    backups: set[Path] = set()
    for _ in range(10):
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
        with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
            kb.connect(db_path=db_path)
        assert excinfo.value.backup_path is not None
        backups.add(excinfo.value.backup_path)

    assert len(backups) == 1, f"expected 1 deterministic backup, got {len(backups)}"
    (backup,) = backups
    assert backup.exists()
    assert backup.read_bytes() == original

    # Mutate the corrupt bytes — fingerprint changes, separate backup preserved.
    with db_path.open("r+b") as f:
        f.seek(4096)
        f.write(b"\xAB" * 64)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with pytest.raises(kb.KanbanDbCorruptError) as excinfo2:
        kb.connect(db_path=db_path)
    second_backup = excinfo2.value.backup_path
    assert second_backup is not None
    assert second_backup != backup
    assert second_backup.exists()


def test_locked_healthy_db_does_not_classify_as_corrupt(tmp_path, monkeypatch):
    """A transient lock during the probe must not produce a .corrupt backup
    and must not be reported as :class:`KanbanDbCorruptError`. Raw sqlite
    ``OperationalError`` (lock/busy) is acceptable and expected."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    real_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        # First call is the integrity probe — simulate a lock.
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kb.sqlite3, "connect", flaky_connect)

    with pytest.raises(sqlite3.OperationalError):
        kb.connect(db_path=db_path)

    # No .corrupt backup may be produced for a healthy-but-locked DB.
    backups = list(tmp_path.glob("*.corrupt.*"))
    assert backups == [], f"unexpected corrupt backups: {backups}"

    # And once the lock clears, normal access still works.
    monkeypatch.setattr(kb.sqlite3, "connect", real_connect)
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="still here")
        titles = [t.title for t in kb.list_tasks(conn)]
    assert "still here" in titles


def test_init_db_allows_missing_then_healthy(tmp_path):
    db_path = tmp_path / "fresh.db"
    assert not db_path.exists()
    kb.init_db(db_path=db_path)
    assert db_path.exists() and db_path.stat().st_size > 0

    # Idempotent on a healthy DB: data survives a second init.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="keeps")
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        tasks = kb.list_tasks(conn)
    assert [t.title for t in tasks] == ["keeps"]


# ---------------------------------------------------------------------------
# First-use tip for scratch workspaces
# ---------------------------------------------------------------------------

def test_maybe_emit_scratch_tip_fires_once_per_install(kanban_home, caplog):
    """First scratch workspace materialization warns + emits an event.

    Subsequent scratch workspaces on the SAME install stay silent — the
    sentinel file under kanban_home() flips after the first emit.
    """
    import logging

    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="first scratch")
        t2 = kb.create_task(conn, title="second scratch")

    # Sentinel must not exist yet on a fresh install.
    assert not kb._scratch_tip_shown()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect() as conn:
            kb._maybe_emit_scratch_tip(conn, t1, "scratch")

    # Sentinel is now set.
    assert kb._scratch_tip_shown()
    assert kb._scratch_tip_sentinel_path().exists()

    # Warning was logged exactly once.
    tip_records = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert len(tip_records) == 1, (
        f"Expected exactly one tip warning, got {len(tip_records)}: "
        f"{[r.getMessage() for r in tip_records]!r}"
    )

    # An event row was appended on the first task.
    with kb.connect() as conn:
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t1,),
        ).fetchall()
    kinds = [e["kind"] for e in events]
    assert "tip_scratch_workspace" in kinds, (
        f"Expected tip_scratch_workspace event on first scratch task; "
        f"got {kinds!r}"
    )

    # Second scratch materialization on the same install stays silent.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect() as conn:
            kb._maybe_emit_scratch_tip(conn, t2, "scratch")
    tip_records2 = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert tip_records2 == [], (
        f"Tip should not re-fire after sentinel is set; got "
        f"{[r.getMessage() for r in tip_records2]!r}"
    )
    with kb.connect() as conn:
        events2 = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t2,),
        ).fetchall()
    assert "tip_scratch_workspace" not in [e["kind"] for e in events2], (
        "Tip event should not be appended for subsequent scratch tasks."
    )


def test_maybe_emit_scratch_tip_skips_non_scratch_workspaces(kanban_home, caplog):
    """worktree/dir workspaces are preserved on completion and must not
    trigger the scratch-cleanup tip."""
    import logging

    with kb.connect() as conn:
        t_wt = kb.create_task(conn, title="worktree task")
        t_dir = kb.create_task(conn, title="dir task")

    assert not kb._scratch_tip_shown()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect() as conn:
            kb._maybe_emit_scratch_tip(conn, t_wt, "worktree")
            kb._maybe_emit_scratch_tip(conn, t_dir, "dir")

    # Sentinel stays unset — these workspaces are preserved by design,
    # so the warning is irrelevant for them and we save the one-shot
    # for a real scratch user.
    assert not kb._scratch_tip_shown()
    tip_records = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert tip_records == []
    with kb.connect() as conn:
        for tid in (t_wt, t_dir):
            events = conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (tid,),
            ).fetchall()
            assert "tip_scratch_workspace" not in [e["kind"] for e in events]


# ---------------------------------------------------------------------------
# Connection pragmas (secure_delete, cell_size_check, synchronous=FULL)
# ---------------------------------------------------------------------------


def test_connect_sets_secure_delete_on(tmp_path):
    """secure_delete=ON must be active on every new connection."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA secure_delete").fetchone()
    assert row[0] == 1, f"expected secure_delete=1, got {row[0]}"


def test_connect_sets_cell_size_check_on(tmp_path):
    """cell_size_check=ON must be active on every new connection."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA cell_size_check").fetchone()
    assert row[0] == 1, f"expected cell_size_check=1, got {row[0]}"


def test_connect_sets_synchronous_full(tmp_path):
    """synchronous must be FULL (=2), not NORMAL (=1)."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA synchronous").fetchone()
    assert row[0] == 2, f"expected synchronous=2 (FULL), got {row[0]}"


def test_connect_pragmas_applied_on_reconnect(tmp_path):
    """All three pragmas must be re-applied on every connect(), not just the first."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # First connection: write a task and close.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="reconnect-check")
    # Force re-init path by discarding path cache.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # Second connection: pragmas must still be applied.
    with kb.connect(db_path=db_path) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2



def test_pragmas_not_accidentally_disabled_by_migrate_path(tmp_path):
    """Migration path must not reset connection pragmas."""
    db_path = tmp_path / "legacy.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # Initialise with a fresh connect so schema + init run.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="pre-migration-task")
    # Simulate a re-entry through the init/migration path by discarding path cache.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2

# write_txn — rollback handler must not mask the original exception
# ---------------------------------------------------------------------------


def test_write_txn_preserves_original_exception_when_rollback_fails(kanban_home):
    """When a write inside write_txn raises an OperationalError that SQLite
    has already auto-rolled-back (e.g. ``disk I/O error``,
    ``database is locked``, ``database disk image is malformed``), the
    explicit ROLLBACK in ``write_txn.__exit__`` itself raises
    ``cannot rollback - no transaction is active``. The original cause
    must NOT be masked by the secondary rollback failure — operators rely
    on the original cause to diagnose the underlying issue.
    """

    class FailingConnWrapper:
        """Delegate to a real connection, simulating an EIO during an INSERT
        that SQLite has already auto-rolled-back."""

        def __init__(self, real):
            self._real = real
            self._fail_armed = True

        def execute(self, sql, *args, **kwargs):
            if (
                self._fail_armed
                and sql.lstrip().upper().startswith("INSERT")
                and "task_events" in sql.lower()
            ):
                self._fail_armed = False  # one-shot
                # Simulate SQLite auto-rolling back the transaction by
                # issuing a real ROLLBACK now. After this, BEGIN IMMEDIATE
                # is no longer active and an explicit ROLLBACK would error.
                try:
                    self._real.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with kb.connect() as conn:
        wrapper = FailingConnWrapper(conn)
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            with kb.write_txn(wrapper):
                kb._append_event(wrapper, "t_bogus", "promoted", None)

    msg = str(excinfo.value)
    assert "disk I/O error" in msg, (
        f"write_txn masked the original exception with rollback failure; "
        f"got {msg!r} (expected to contain 'disk I/O error')"
    )
    assert "cannot rollback" not in msg, (
        f"write_txn surfaced the rollback failure instead of the original "
        f"OperationalError; got {msg!r}"
    )
def test_write_txn_healthy_commit_no_exception(tmp_path):
    """Normal commit does not trigger the torn-extend check."""
    from hermes_cli.kanban_db import connect, write_txn
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    # Should not raise
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
            "VALUES ('t_test01', 'test task', 'tester', 'todo', 0, 1234567890)"
        )
    row = conn.execute("SELECT title FROM tasks WHERE id='t_test01'").fetchone()
    assert row["title"] == "test task"
    conn.close()


def test_write_txn_raises_on_truncated_file(tmp_path):
    """A mocked smaller file size triggers the torn-extend check."""
    from hermes_cli.kanban_db import connect, write_txn
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    # Get actual page size so we can fake a smaller file
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    original_getsize = os.path.getsize

    def fake_getsize(path):
        # Return a size that implies at least 1 fewer page than header claims
        real_size = original_getsize(path)
        return max(0, real_size - page_size)

    with pytest.raises(sqlite3.DatabaseError, match="torn-extend|page count mismatch"):
        with unittest.mock.patch("hermes_cli.kanban_db.os.path.getsize", side_effect=fake_getsize):
            with write_txn(conn) as c:
                c.execute(
                    "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
                    "VALUES ('t_test02', 'test task 2', 'tester', 'todo', 0, 1234567890)"
                )
    conn.close()


def test_write_txn_post_commit_check_fires_every_call(tmp_path):
    """The invariant check runs on every write_txn call."""
    from hermes_cli.kanban_db import connect, write_txn
    import hermes_cli.kanban_db as kanban_db_module
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    call_count = 0
    real_check = kanban_db_module._check_file_length_invariant

    def counting_check(c):
        nonlocal call_count
        call_count += 1
        real_check(c)

    with unittest.mock.patch.object(kanban_db_module, "_check_file_length_invariant", counting_check):
        for i in range(3):
            with write_txn(conn) as c:
                c.execute(
                    f"INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
                    f"VALUES ('t_fire{i:02d}', 'task {i}', 'tester', 'todo', 0, 1234567890)"
                )
    assert call_count == 3
    conn.close()


def test_connect_sets_wal_autocheckpoint_100(tmp_path):
    """connect() sets wal_autocheckpoint to 100."""
    from hermes_cli.kanban_db import connect
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    val = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    assert val == 100
    conn.close()


def test_write_txn_check_reads_correct_header_fields(tmp_path):
    """Synthetic DB file with mismatched header page_count triggers the check."""
    import struct
    from hermes_cli.kanban_db import connect, _check_file_length_invariant
    db = tmp_path / "synthetic.db"
    conn = connect(db_path=db)
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    # Now corrupt the file: claim N pages but truncate to N-1 pages
    with open(db, "rb") as f:
        data = bytearray(f.read())
    # Read current page_count from header bytes 28-31
    real_page_count = struct.unpack(">I", data[28:32])[0]
    if real_page_count < 2:
        # Need at least 2 pages to fake a truncation
        pytest.skip("DB too small for synthetic truncation test")
    # Truncate to N-1 pages
    truncated = bytes(data[: (real_page_count - 1) * page_size])
    with open(db, "wb") as f:
        f.write(truncated)
    # Now open and check — should raise
    # We can't use connect() because _validate_sqlite_header may block; use a raw connection
    raw_conn = sqlite3.connect(str(db), isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="torn-extend|page count mismatch"):
        _check_file_length_invariant(raw_conn)
    raw_conn.close()


# ---------------------------------------------------------------------------
# reap_worker_zombies() tests
# ---------------------------------------------------------------------------


def test_reap_worker_zombies_returns_count():
    """reap_worker_zombies() returns the list of reaped PIDs."""
    from unittest.mock import patch

    fake_pids = [12345, 67890, 11111]
    call_count = [0]

    def fake_waitpid(pid, flags):
        if call_count[0] < len(fake_pids):
            p = fake_pids[call_count[0]]
            call_count[0] += 1
            return p, 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch("hermes_cli.kanban_db._record_worker_exit"):
            pids = kb.reap_worker_zombies()
    assert pids == [12345, 67890, 11111]


def test_reap_worker_zombies_noop_on_windows(monkeypatch):
    """reap_worker_zombies() returns 0 and never calls os.waitpid on Windows."""
    from unittest.mock import patch

    monkeypatch.setattr("hermes_cli.kanban_db.os.name", "nt")
    with patch("hermes_cli.kanban_db.os.waitpid") as mock_waitpid:
        result = kb.reap_worker_zombies()
    mock_waitpid.assert_not_called()
    assert result == []


def test_reap_worker_zombies_noop_no_children():
    """reap_worker_zombies() returns 0 without error when there are no children."""
    from unittest.mock import patch

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=ChildProcessError):
        result = kb.reap_worker_zombies()
    assert result == []


def test_reap_worker_zombies_records_exit_status():
    """reap_worker_zombies() calls _record_worker_exit for each reaped pid."""
    from unittest.mock import patch

    calls = []
    call_count = [0]

    def fake_waitpid(pid, flags):
        call_count[0] += 1
        if call_count[0] == 1:
            return 12345, 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch(
            "hermes_cli.kanban_db._record_worker_exit",
            side_effect=lambda p, s: calls.append((p, s)),
        ):
            kb.reap_worker_zombies()

    assert calls == [(12345, 0)]


def test_reap_worker_zombies_handles_waitpid_os_error():
    """reap_worker_zombies() does not propagate generic OSError from os.waitpid."""
    from unittest.mock import patch

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=OSError("test error")):
        result = kb.reap_worker_zombies()
    assert result == []


def test_zombie_reaper_runs_despite_board_connect_failure():
    """reap_worker_zombies runs even when a board tick raises an error."""
    from unittest.mock import patch

    call_count = [0]

    def fake_waitpid(pid, flags):
        call_count[0] += 1
        if call_count[0] <= 2:
            return [12345, 67890][call_count[0] - 1], 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch("hermes_cli.kanban_db._record_worker_exit"):
            # Simulate a board tick failure before reaping
            try:
                raise sqlite3.OperationalError("disk I/O error")
            except sqlite3.OperationalError:
                pass

            # Reaper still runs independently
            pids = kb.reap_worker_zombies()

    assert pids == [12345, 67890]


def test_zombie_reaper_survives_all_boards_failing():
    """reap_worker_zombies runs each tick regardless of board tick failures."""
    from unittest.mock import patch

    total_reaped = 0

    def make_fake_waitpid(zombie_pids):
        call_count = [0]

        def fake_waitpid(pid, flags):
            if call_count[0] < len(zombie_pids):
                p = zombie_pids[call_count[0]]
                call_count[0] += 1
                return p, 0
            return 0, 0

        return fake_waitpid

    # 5 ticks, 2 zombies per tick = 10 total
    for tick in range(5):
        pids = [tick * 100 + 1, tick * 100 + 2]
        with patch(
            "hermes_cli.kanban_db.os.waitpid", side_effect=make_fake_waitpid(pids)
        ):
            with patch("hermes_cli.kanban_db._record_worker_exit"):
                pids = kb.reap_worker_zombies()
        total_reaped += len(pids)

    assert total_reaped == 10


def test_dispatch_once_still_reaps_via_extracted_fn(kanban_home):
    """The reaper inside dispatch_once still works after refactor to reap_worker_zombies()."""
    from unittest.mock import patch

    call_count = [0]

    def fake_waitpid(pid, flags):
        call_count[0] += 1
        if call_count[0] == 1:
            return 99999, 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch("hermes_cli.kanban_db._record_worker_exit"):
            with patch("hermes_cli.kanban_db.os.name", "posix"):
                pids = kb.reap_worker_zombies()

    assert pids == [99999]



# ---------------------------------------------------------------------------
# connect_closing(): context manager that actually closes the FD
# Regression coverage for #33159 (kanban.db FD leak — gateway crashes after
# ~4 days). sqlite3.Connection's built-in __exit__ commits/rollbacks but
# does NOT close, so `with kb.connect() as conn:` leaks the FD in
# long-lived processes (gateway run_slash, dashboard decompose handler).
# `connect_closing()` is the leak-safe replacement.
# ---------------------------------------------------------------------------


def test_connect_closing_closes_connection_on_exit(tmp_path):
    """The new context manager MUST actually close the underlying FD."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect_closing(db_path=db_path) as conn:
        conn.execute("SELECT 1").fetchone()
    # After exit, the connection MUST be closed — subsequent execute
    # should raise ProgrammingError.
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connect_closing_closes_on_exception(tmp_path):
    """Connection closed even when the body raises."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    captured = []
    with pytest.raises(RuntimeError, match="boom"):
        with kb.connect_closing(db_path=db_path) as conn:
            captured.append(conn)
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")


def test_connect_closing_yields_usable_connection(tmp_path):
    """Smoke test: schema is initialized and basic ops work."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect_closing(db_path=db_path) as conn:
        tid = kb.create_task(conn, title="closing-cm test")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.title == "closing-cm test"


def test_bare_connect_does_not_close_on_context_exit(tmp_path):
    """Document the leak that connect_closing exists to prevent.

    sqlite3.Connection's __exit__ commits/rollbacks but doesn't close.
    This is the upstream behaviour we cannot change; the regression
    guard is to make sure connect_closing() does the right thing.
    """
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        pass
    # Still usable after with-block exit (the leak).
    conn.execute("SELECT 1").fetchone()
    conn.close()  # explicit close to avoid leaking THIS test
