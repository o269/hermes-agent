"""Regression coverage for terminal-custody rows in ``recompute_ready``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# Keep the incident matrix literal instead of deriving it from production code:
# deleting or renaming one protected lane must fail a named regression case.
TERMINAL_CUSTODY_MATRIX = (
    "fable",
    "s4",
    "operator-gate",
    "terminal",
)


def _must_get(conn: sqlite3.Connection, task_id: str) -> kb.Task:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize("assignee", TERMINAL_CUSTODY_MATRIX)
@pytest.mark.parametrize(
    ("held_status", "block_kind"),
    (
        pytest.param("todo", None, id="raw-todo"),
        pytest.param("todo", "dependency", id="typed-todo"),
        pytest.param("blocked", None, id="raw-blocked"),
        pytest.param("blocked", "needs_input", id="typed-blocked"),
    ),
)
def test_terminal_custody_survives_two_recompute_cycles_until_explicit_release(
    kanban_home: Path,
    assignee: str,
    held_status: str,
    block_kind: str | None,
) -> None:
    """Raw/typed terminal holds without a sticky event must not refloat."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title=f"terminal custody: {assignee}",
            assignee=assignee,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = ?, block_kind = ? WHERE id = ?",
                (held_status, block_kind, task_id),
            )

        assert not any(
            event.kind == "blocked" for event in kb.list_events(conn, task_id)
        )
        assert _must_get(conn, task_id).block_kind == block_kind
        for cycle in range(2):
            assert kb.recompute_ready(conn) == 0, (
                f"{assignee}/{held_status}/{block_kind} refloated "
                f"on cycle {cycle + 1}"
            )
            assert _must_get(conn, task_id).status == held_status

        # The exemption applies only to automatic recomputation. Deliberate
        # operator release remains authoritative for both held source states.
        if held_status == "blocked":
            assert kb.unblock_task(conn, task_id) is True
        else:
            promoted, reason = kb.promote_task(
                conn,
                task_id,
                actor="operator-test",
                reason="explicit terminal-custody release",
            )
            assert (promoted, reason) == (True, None)
        assert _must_get(conn, task_id).status == "ready"


def test_ordinary_executor_dependency_recovery_still_promotes_once(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="dependency", assignee="engineer")
        child = kb.create_task(
            conn,
            title="ordinary dependency-held executor",
            assignee="engineer",
            parents=[parent],
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))

        assert _must_get(conn, child).status == "todo"
        assert kb.recompute_ready(conn) == 1
        assert _must_get(conn, child).status == "ready"
        assert kb.recompute_ready(conn) == 0


def test_ordinary_executor_circuit_breaker_recovery_still_promotes_once(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary transient executor failure",
            assignee="engineer",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'blocked', consecutive_failures = 1 "
                "WHERE id = ?",
                (task_id,),
            )

        assert not any(
            event.kind == "blocked" for event in kb.list_events(conn, task_id)
        )
        assert kb.recompute_ready(conn, failure_limit=2) == 1
        task = _must_get(conn, task_id)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        assert kb.recompute_ready(conn, failure_limit=2) == 0
