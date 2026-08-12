"""Tests for the create_task open-duplicate fence (recurrence fence: DEDUP).

The board has repeatedly minted duplicate cards for work that is already
open: an auto-minter (decompose fan-out, swarm, dashboard retry) creates a
card whose title matches a task that is already ``ready``/``running``/
``blocked``/``triage``, and the duplicate then double-dispatches the same
work. ``create_task`` now refuses the second mint: when a non-terminal
(``done``/``archived`` excluded) task carries the same normalized title
(casefolded, whitespace collapsed), it returns the existing task id and
records a ``duplicate_open_task`` event on it instead of inserting a row.

Tests below come in the required two shapes:

* Normal operation is not disrupted — distinct titles coexist, terminal
  (done/archived) tasks do NOT block re-creation, and the explicit
  ``allow_open_duplicate=True`` escape hatch still mints a second card.
* Must-fire — the exact bad condition (second mint of an open same-title
  card, including case/whitespace variants) is created on purpose and the
  fence is asserted to block it. These tests FAIL if the fence block in
  ``create_task`` is removed, which is the proof that they exercise the
  fence and not incidental behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _task_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


# ---------------------------------------------------------------------------
# Normal operation — the fence must not disrupt legitimate creates
# ---------------------------------------------------------------------------


def test_distinct_titles_create_distinct_tasks(conn):
    t1 = kb.create_task(conn, title="Fix login redirect", assignee="worker")
    t2 = kb.create_task(conn, title="Fix logout redirect", assignee="worker")
    t3 = kb.create_task(conn, title="Fix login redirect — staging", assignee="worker")
    assert len({t1, t2, t3}) == 3
    assert _task_count(conn) == 3


def test_done_task_does_not_block_recreation(conn):
    """Terminal work must not freeze a title forever: re-opening finished
    work under the same title (a regression, a repeat job) is legitimate."""
    t1 = kb.create_task(conn, title="Rotate TLS cert", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (t1,))
    kb.claim_task(conn, t1, claimer="worker")
    kb.complete_task(conn, t1, result="done")
    assert kb.get_task(conn, t1).status == "done"

    t2 = kb.create_task(conn, title="Rotate TLS cert", assignee="worker")
    assert t2 != t1
    assert _task_count(conn) == 2


def test_archived_task_does_not_block_recreation(conn):
    t1 = kb.create_task(conn, title="Weekly digest", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (t1,))

    t2 = kb.create_task(conn, title="Weekly digest", assignee="worker")
    assert t2 != t1
    assert _task_count(conn) == 2


def test_allow_open_duplicate_opt_out_still_mints(conn):
    """The escape hatch keeps working for callers that genuinely want a
    second open card with the same title."""
    t1 = kb.create_task(conn, title="Recurring sweep", assignee="worker")
    t2 = kb.create_task(
        conn, title="Recurring sweep", assignee="worker", allow_open_duplicate=True
    )
    assert t2 != t1
    assert _task_count(conn) == 2


def test_idempotency_key_path_unaffected(conn):
    """The pre-existing idempotency-key dedup keeps its own semantics."""
    t1 = kb.create_task(conn, title="Keyed job", idempotency_key="k-1")
    t2 = kb.create_task(conn, title="Keyed job", idempotency_key="k-1")
    assert t2 == t1
    assert _task_count(conn) == 1


# ---------------------------------------------------------------------------
# Must-fire — create the exact bad condition, assert the fence blocks it
# ---------------------------------------------------------------------------


def test_fence_blocks_exact_duplicate_of_open_task(conn):
    t1 = kb.create_task(conn, title="Fix login redirect", assignee="worker")
    t2 = kb.create_task(conn, title="Fix login redirect", assignee="worker")
    assert t2 == t1, "second mint of an open same-title card must be suppressed"
    assert _task_count(conn) == 1, "no duplicate row may be inserted"


def test_fence_blocks_case_and_whitespace_variants(conn):
    t1 = kb.create_task(conn, title="Fix login redirect", assignee="worker")
    t2 = kb.create_task(conn, title="  fix  LOGIN   redirect ", assignee="worker")
    assert t2 == t1
    assert _task_count(conn) == 1


@pytest.mark.parametrize("open_status", ["ready", "todo", "running", "blocked", "triage"])
def test_fence_blocks_duplicate_across_all_open_statuses(conn, open_status):
    """A card that is open in ANY non-terminal status is still open work —
    the duplicate must be suppressed no matter where the original sits."""
    t1 = kb.create_task(conn, title="Open work item", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (open_status, t1))

    t2 = kb.create_task(conn, title="open work item", assignee="worker")
    assert t2 == t1
    assert _task_count(conn) == 1
    assert kb.get_task(conn, t1).status == open_status


def test_fence_records_audit_event_on_existing_task(conn):
    t1 = kb.create_task(conn, title="Fix login redirect", assignee="worker")
    t2 = kb.create_task(
        conn, title="fix login redirect", assignee="worker", created_by="auto-minter"
    )
    assert t2 == t1
    events = [e for e in kb.list_events(conn, t1) if e.kind == "duplicate_open_task"]
    assert events, "suppressed mint must leave an audit event on the existing task"
    payload = events[-1].payload or {}
    assert payload.get("attempted_title") == "fix login redirect"
    assert payload.get("created_by") == "auto-minter"
    assert payload.get("matched_task_id") == t1
