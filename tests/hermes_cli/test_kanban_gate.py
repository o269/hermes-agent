from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import boardd_shim
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BROKER", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run_cli(*argv: str) -> int:
    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subparsers)
    args = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(args)


def test_gate_cli_marks_title_without_changing_custody(kanban_home, capsys):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="PR17 cross-agent preflight",
            assignee="fable",
            triage=True,
        )

    assert (
        _run_cli(
            "gate",
            task_id,
            "OPERATOR-GATE",
            "--field",
            "title",
            "--author",
            "engineer",
        )
        == 0
    )
    assert "status and assignee preserved" in capsys.readouterr().out

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
    assert task is not None
    assert task.title.endswith("[OPERATOR-GATE]")
    assert task.assignee == "fable"
    assert task.status == "triage"
    assert any(event.kind == "gate_marked" for event in events)


def test_gate_cli_can_mark_body_and_rejects_unknown_keyword(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="PR19 preflight", triage=True)

    assert _run_cli("gate", task_id, "QUIESCE", "--field", "body") == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.body == "[QUIESCE-GATE]"
    assert task.status == "triage"
    assert task.assignee is None

    assert _run_cli("gate", task_id, "GATE-FABLE-LAND", "--field", "body") == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.body is not None
    assert task.body.endswith("[GATE-FABLE-LAND]")

    assert _run_cli("gate", task_id, "NOT-A-GATE") == 1


def test_gate_write_runs_through_real_broker_transaction(kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="broker-owned gate",
            assignee="fable",
            triage=True,
        )

    raw = sqlite3.connect(kb.kanban_db_path())
    raw.row_factory = sqlite3.Row

    class SqliteBrokerClient:
        def txn_begin(self):
            raw.execute("BEGIN IMMEDIATE")
            return "txn"

        def txn_exec(self, token, sql, params):
            assert token == "txn"
            cursor = raw.execute(sql, params)
            rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
            return {
                "rows": rows,
                "rowcount": cursor.rowcount,
                "lastrowid": cursor.lastrowid,
            }

        def txn_commit(self, token):
            assert token == "txn"
            raw.commit()

        def txn_rollback(self, token):
            assert token == "txn"
            raw.rollback()

    monkeypatch.setattr(boardd_shim, "_c", lambda: SqliteBrokerClient())
    broker = boardd_shim.BrokerConnection()
    try:
        assert kb.append_task_gate(
            broker,
            task_id,
            "OPERATOR-HOLD",
            field="body",
            author="engineer",
        )
    finally:
        broker.close()
        raw.close()

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
    assert task is not None
    assert task.body == "[OPERATOR-HOLD]"
    assert task.assignee == "fable"
    assert task.status == "triage"
    assert any(event.kind == "gate_marked" for event in events)
