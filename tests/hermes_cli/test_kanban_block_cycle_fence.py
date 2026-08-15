"""Tests for the cross-kind unblock-loop fence (recurrence fence:
CLOSE-LOOP).

Cards have dead-ended in a blocked → retried → blocked cycle with no
forward exit. The pre-existing loop breaker only incremented
``block_recurrences`` when a re-block carried the SAME ``kind`` as the
stored one; a task alternating kinds (needs_input → capability →
needs_input …) reset the counter to 1 on every flip, so the escalation to
``triage`` never fired and the card spun in ``blocked`` forever. The fence:
ANY truly-blocked re-block of a task with surviving loop memory
(``block_recurrences > 0``) increments the counter, so alternating-kind
loops also reach ``BLOCK_RECURRENCE_LIMIT`` and route to ``triage`` — a
forward exit with a human-in-the-loop decision instead of a dead end.

Tests below come in the required two shapes:

* Normal operation is not disrupted — a first block still lands in
  ``blocked`` with recurrences=1; same-kind re-block still escalates
  (pre-existing behavior); ``dependency`` blocks still route to ``todo``
  without consuming loop memory; ``complete_task`` still resets the
  counter.
* Must-fire — the exact bad condition (cross-kind re-block after unblock)
  is created on purpose and the fence is asserted to escalate to
  ``triage``. These tests FAIL if the fence in ``block_task`` is removed
  (restoring the same-cause-only rule), which is the proof that they
  exercise the fence and not incidental behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tools.kanban_tools import KANBAN_BLOCK_SCHEMA


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _recurrences(conn, tid) -> int:
    row = conn.execute(
        "SELECT block_recurrences FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    return int(row["block_recurrences"] or 0)


def test_agent_tool_description_matches_cross_kind_fence() -> None:
    description = KANBAN_BLOCK_SCHEMA["description"]
    assert "even when the reason or block kind changes" in description
    assert "re-blocked for the same reason" not in description


# ---------------------------------------------------------------------------
# Normal operation — the fence must not disrupt legitimate blocking
# ---------------------------------------------------------------------------


def test_first_block_lands_in_blocked_with_recurrence_one(conn):
    tid = _running_task(conn)
    assert kb.block_task(conn, tid, reason="need creds", kind="needs_input")
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert _recurrences(conn, tid) == 1


def test_same_kind_reblock_still_escalates(conn):
    """Pre-existing behavior is preserved: a same-kind re-block after an
    unblock still trips the loop breaker at BLOCK_RECURRENCE_LIMIT."""
    tid = _running_task(conn)
    kb.block_task(conn, tid, reason="x", kind="capability")
    kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    kb.block_task(conn, tid, reason="x", kind="capability")
    assert kb.get_task(conn, tid).status == "triage"
    assert _recurrences(conn, tid) == 2


def test_dependency_block_still_routes_to_todo_without_loop_memory(conn):
    tid = _running_task(conn)
    kb.block_task(conn, tid, reason="waiting on parent", kind="dependency")
    assert kb.get_task(conn, tid).status == "todo"
    assert _recurrences(conn, tid) == 0
    # A later first truly-blocked block starts the counter at 1, not 2 —
    # dependency parking must not consume loop memory.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    kb.claim_task(conn, tid, claimer="worker")
    kb.block_task(conn, tid, reason="real blocker", kind="capability")
    assert kb.get_task(conn, tid).status == "blocked"
    assert _recurrences(conn, tid) == 1


def test_completion_resets_loop_memory(conn):
    tid = _running_task(conn)
    kb.block_task(conn, tid, reason="x", kind="needs_input")
    assert _recurrences(conn, tid) == 1
    kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    kb.complete_task(conn, tid, result="done")
    assert _recurrences(conn, tid) == 0


# ---------------------------------------------------------------------------
# Must-fire — create the exact bad condition, assert the fence blocks it
# ---------------------------------------------------------------------------


def test_fence_escalates_cross_kind_reblock(conn):
    """The dead-end: blocked(needs_input) → unblock → blocked(capability)
    must escalate to triage instead of resetting the counter and parking
    the card in blocked forever."""
    tid = _running_task(conn)
    kb.block_task(conn, tid, reason="need creds", kind="needs_input")
    assert kb.get_task(conn, tid).status == "blocked"
    kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    kb.block_task(conn, tid, reason="missing tool", kind="capability")
    task = kb.get_task(conn, tid)
    assert task.status == "triage", (
        "cross-kind re-block must route to triage, not dead-end in blocked"
    )
    assert _recurrences(conn, tid) == 2
    events = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"]
    assert events, "escalation must emit block_loop_detected"
    payload = events[-1].payload or {}
    assert payload.get("recurrences") == 2
    assert payload.get("kind") == "capability"


def test_fence_escalates_untyped_then_typed(conn):
    """An un-typed legacy block followed by a typed re-block is the same
    loop — the kind flip must not reset the counter."""
    tid = _running_task(conn)
    kb.block_task(conn, tid, reason="opaque failure")
    assert kb.get_task(conn, tid).status == "blocked"
    kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    kb.block_task(conn, tid, reason="need creds", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    assert _recurrences(conn, tid) == 2


def test_fence_escalates_three_way_kind_rotation(conn):
    """A → B → A rotation: the second re-block trips the limit regardless
    of which kind it carries."""
    tid = _running_task(conn)
    kb.block_task(conn, tid, reason="a", kind="needs_input")
    kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    kb.block_task(conn, tid, reason="b", kind="capability")
    assert kb.get_task(conn, tid).status == "triage"
    # Even after a human pulls the card out of triage and another run, loop
    # memory survives (only completion resets it), so a further block
    # escalates immediately rather than reopening a fresh cycle.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    kb.claim_task(conn, tid, claimer="worker")
    kb.block_task(conn, tid, reason="a again", kind="needs_input")
    task = kb.get_task(conn, tid)
    assert task.status == "triage"
    assert _recurrences(conn, tid) == 3
