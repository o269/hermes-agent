"""Unit tests for the post-PR41 broker board helper (no live board I/O)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import sys

HELPER_DIR = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "merge-truth-reconciler"
sys.path.insert(0, str(HELPER_DIR))

import merge_truth_board_helper as helper  # noqa: E402


PR_URL = "https://github.com/acme/widget/pull/42"
PR_URL_B = "https://github.com/acme/widget/pull/99"


class FakeConn:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def execute(self, sql: str, params: Any = ()):
        return self.db.execute(sql, params)


def build_db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "board.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            status TEXT,
            assignee TEXT,
            skills TEXT
        );
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_pr_ownership (
            task_id TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            first_seen_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            source_comment_id INTEGER,
            declared INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, canonical_url)
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_links (
            parent_id TEXT NOT NULL,
            child_id TEXT NOT NULL
        );
        """
    )
    return conn


def make_helper(conn: sqlite3.Connection) -> helper.BoardHelper:
    comments: dict[str, list[str]] = {}
    completed: set[str] = set()
    ready_before = {"t_child"}

    def query(sql: str, params=None, max_rows=None):
        cur = conn.execute(sql, list(params or []))
        rows = [dict(row) for row in cur.fetchall()]
        if max_rows is not None:
            rows = rows[: int(max_rows)]
        return rows

    def add_comment(task_id: str, author: str, body: str, *, op_id: str | None = None):
        del author, op_id
        comments.setdefault(task_id, []).append(body)
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
            (task_id, helper.IDENTITY, body, 1),
        )
        conn.commit()
        return {"comment_id": 1}

    def complete_task(task_id: str, receipt: str) -> bool:
        del receipt
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return False
        if str(row["status"]).lower() in helper.TERMINAL_STATUSES:
            return False
        if str(row["status"]).lower() not in {"running", "ready", "blocked", "todo"}:
            return False
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        conn.commit()
        completed.add(task_id)
        return True

    def recompute_ready() -> int:
        # Promote todo child with done parent.
        rows = conn.execute(
            "SELECT c.child_id AS id FROM task_links c "
            "JOIN tasks p ON p.id = c.parent_id "
            "JOIN tasks t ON t.id = c.child_id "
            "WHERE t.status='todo' AND p.status='done'"
        ).fetchall()
        count = 0
        for row in rows:
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (row["id"],))
            count += 1
        conn.commit()
        return count

    def exec_write(sql: str, params=None, *, op_id: str):
        del op_id
        cur = conn.execute(sql, list(params or []))
        conn.commit()
        return {"rowcount": cur.rowcount, "lastrowid": cur.lastrowid}

    def run_txn(work):
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = work(FakeConn(conn))
            conn.execute("COMMIT")
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    return helper.BoardHelper(
        query=query,
        add_comment=add_comment,
        complete_task=complete_task,
        recompute_ready=recompute_ready,
        exec_write=exec_write,
        run_txn=run_txn,
        now=lambda: 1_700_000_000,
    )


def test_capabilities_advertise_required_set(tmp_path: Path) -> None:
    h = make_helper(build_db(tmp_path))
    result = h.handle({"action": "capabilities", "author": helper.IDENTITY, "payload": {}})
    assert result["ok"] is True
    assert set(result["result"]["capabilities"]) == helper.REQUIRED_CAPABILITIES


def test_inventory_returns_hex_and_ownership_urls(tmp_path: Path) -> None:
    conn = build_db(tmp_path)
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, assignee, skills) VALUES (?,?,?,?,?,?)",
        ("t_a", "A", "see https://github.com/Acme/Widget/pull/42", "blocked", "eng", "[]"),
    )
    conn.execute(
        "INSERT INTO task_pr_ownership "
        "(task_id, canonical_url, first_seen_at, last_seen_at, declared) "
        "VALUES (?,?,?,?,1)",
        ("t_a", PR_URL, 1, 1),
    )
    conn.commit()
    h = make_helper(conn)
    result = h.handle(
        {
            "action": "inventory",
            "author": helper.IDENTITY,
            "payload": {"body_encoding": "hex"},
        }
    )
    assert result["ok"] is True
    assert PR_URL in result["result"]["canonical_urls"]
    assert result["result"]["hex_texts"]
    # Round-trip one hex body
    decoded = bytes.fromhex(result["result"]["hex_texts"][0]).decode("utf-8")
    assert "pull/42" in decoded


def test_list_cards_citing_authority_booleans(tmp_path: Path) -> None:
    conn = build_db(tmp_path)
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, assignee, skills) VALUES (?,?,?,?,?,?)",
        (
            "t_gate",
            "GATE card",
            "GATE: PR-MERGE https://github.com/acme/widget/pull/42",
            "blocked",
            "eng1",
            json.dumps(["frontend"]),
        ),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, assignee, skills) VALUES (?,?,?,?,?,?)",
        ("t_run", "running", "await https://github.com/acme/widget/pull/42", "running", "eng2", None),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, assignee, skills) VALUES (?,?,?,?,?,?)",
        ("t_fable", "hold", "https://github.com/acme/widget/pull/42", "blocked", "fable", None),
    )
    conn.commit()
    h = make_helper(conn)
    result = h.handle(
        {
            "action": "list_cards_citing",
            "author": helper.IDENTITY,
            "payload": {"canonical_urls": [PR_URL], "body_encoding": "hex"},
        }
    )
    assert result["ok"] is True
    cards = {card["task_id"]: card for card in result["result"]["cards"]}
    assert cards["t_gate"]["terminal"] is False
    assert cards["t_gate"]["active_run"] is False
    assert cards["t_gate"]["protected_custody"] is False
    assert cards["t_run"]["active_run"] is True
    assert cards["t_fable"]["protected_custody"] is True
    assert isinstance(cards["t_gate"]["terminal"], bool)
    body = bytes.fromhex(cards["t_gate"]["body_hex"]).decode()
    assert "GATE: PR-MERGE" in body


def test_converge_ownership_clears_declared_and_referenced(tmp_path: Path) -> None:
    conn = build_db(tmp_path)
    conn.execute(
        "INSERT INTO task_pr_ownership "
        "(task_id, canonical_url, first_seen_at, last_seen_at, declared) VALUES (?,?,?,?,1)",
        ("t_owner", PR_URL, 1, 1),
    )
    conn.execute(
        "INSERT INTO task_pr_ownership "
        "(task_id, canonical_url, first_seen_at, last_seen_at, declared) VALUES (?,?,?,?,0)",
        ("t_cite", PR_URL, 1, 1),
    )
    conn.execute(
        "INSERT INTO task_pr_ownership "
        "(task_id, canonical_url, first_seen_at, last_seen_at, declared) VALUES (?,?,?,?,1)",
        ("t_other", PR_URL_B, 1, 1),
    )
    conn.commit()
    h = make_helper(conn)
    result = h.handle(
        {
            "action": "converge_ownership",
            "author": helper.IDENTITY,
            "payload": {
                "atomic": True,
                "operation_id": "opid-own-1",
                "merge": {
                    "canonical_url": PR_URL,
                    "repository": "acme/widget",
                    "number": 42,
                    "merge_sha": "abc",
                    "merged_at": "2026-08-05T10:00:00Z",
                },
            },
        }
    )
    assert result["ok"] is True
    assert result["result"]["semantic_released"] is True
    assert result["result"]["cleared_rows"] == 2
    assert set(result["result"]["affected_task_ids"]) == {"t_owner", "t_cite"}
    left = conn.execute(
        "SELECT task_id, canonical_url FROM task_pr_ownership"
    ).fetchall()
    assert [(r["task_id"], r["canonical_url"]) for r in left] == [("t_other", PR_URL_B)]


def test_complete_and_evidence_are_opid_idempotent(tmp_path: Path) -> None:
    conn = build_db(tmp_path)
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, assignee, skills) VALUES (?,?,?,?,?,?)",
        ("t_gate", "g", "GATE: PR-MERGE " + PR_URL, "blocked", "eng", None),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, assignee, skills) VALUES (?,?,?,?,?,?)",
        ("t_child", "child", "depends", "todo", "eng", None),
    )
    conn.execute(
        "INSERT INTO task_links (parent_id, child_id) VALUES (?,?)",
        ("t_gate", "t_child"),
    )
    conn.commit()
    h = make_helper(conn)

    complete1 = h.handle(
        {
            "action": "complete_gate_card",
            "author": helper.IDENTITY,
            "payload": {
                "task_id": "t_gate",
                "receipt": "MERGE-RECEIPT URL=" + PR_URL,
                "operation_id": "opid-complete-1",
            },
        }
    )
    complete2 = h.handle(
        {
            "action": "complete_gate_card",
            "author": helper.IDENTITY,
            "payload": {
                "task_id": "t_gate",
                "receipt": "MERGE-RECEIPT URL=" + PR_URL,
                "operation_id": "opid-complete-1",
            },
        }
    )
    assert complete1["result"]["changed"] is True
    assert complete2["result"]["changed"] is False
    status = conn.execute("SELECT status FROM tasks WHERE id='t_gate'").fetchone()["status"]
    assert status == "done"
    n_comments = conn.execute(
        "SELECT COUNT(*) AS c FROM task_comments WHERE task_id='t_gate'"
    ).fetchone()["c"]
    assert n_comments == 1

    evidence1 = h.handle(
        {
            "action": "add_evidence_comment",
            "author": helper.IDENTITY,
            "payload": {
                "task_id": "t_child",
                "receipt": "MERGE-EVIDENCE URL=" + PR_URL,
                "operation_id": "opid-evidence-1",
            },
        }
    )
    evidence2 = h.handle(
        {
            "action": "add_evidence_comment",
            "author": helper.IDENTITY,
            "payload": {
                "task_id": "t_child",
                "receipt": "MERGE-EVIDENCE URL=" + PR_URL,
                "operation_id": "opid-evidence-1",
            },
        }
    )
    assert evidence1["result"]["changed"] is True
    assert evidence2["result"]["changed"] is False

    ready = h.handle(
        {
            "action": "recompute_ready",
            "author": helper.IDENTITY,
            "payload": {"operation_id": "opid-ready-1"},
        }
    )
    assert ready["result"]["actual_promoted_count"] == 1
    child = conn.execute("SELECT status FROM tasks WHERE id='t_child'").fetchone()["status"]
    assert child == "ready"


def test_wrong_author_rejected(tmp_path: Path) -> None:
    h = make_helper(build_db(tmp_path))
    with pytest.raises(helper.HelperError):
        h.handle({"action": "capabilities", "author": "not-mtr", "payload": {}})


def test_from_env_requires_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_BROKER", raising=False)
    monkeypatch.delenv("BOARDD_SOCK", raising=False)
    with pytest.raises(helper.CapabilityError):
        helper.BoardHelper.from_env()
