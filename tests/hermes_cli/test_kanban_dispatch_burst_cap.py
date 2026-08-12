"""Tests for the dispatcher per-tick spawn burst fence (recurrence fence:
BURST CAP).

The dispatcher has repeatedly spawned workers in an unbounded burst,
exhausting a provider's quota in minutes and degrading cards into an
unrecoverable triage state. The pre-existing caps did not stop this:
``max_spawn`` / ``max_in_progress`` are *live concurrency* caps, so a
fast-draining queue (instant-failing or instant-completing workers) still
allowed unbounded NEW spawns inside a single tick. ``max_spawn_per_tick``
closes that: a hard ceiling on new spawns per tick, shared by the ready
and review loops. Held tasks are NOT failed — they stay ``ready`` and are
reconsidered on the next tick.

Both real dispatch entry points (CLI ``_cmd_dispatch``, gateway dispatcher
watcher) default the fence to ``DEFAULT_MAX_SPAWN_PER_TICK`` when
``kanban.max_spawn_per_tick`` is unset, so the recurrence cannot silently
return on an install that never configured the key.

Tests below come in the required two shapes:

* Normal operation is not disrupted — a tick whose ready queue fits the
  budget spawns everything; ``None`` keeps library back-compat
  (unbounded); held work is picked up on the following tick.
* Must-fire — a ready queue larger than the budget is created on purpose
  and the fence is asserted to hold the overflow. These tests FAIL if the
  ``_spawn_budget`` check in ``_dispatch_once_locked`` is removed, which
  is the proof that they exercise the fence and not incidental behavior.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
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


def _spawn_spy(calls):
    def spy(task, workspace_path, board=None):
        calls.append(task.id)
        return 999999

    return spy


def _create_ready(conn, n):
    ids = []
    for i in range(n):
        ids.append(kb.create_task(conn, title=f"job-{i}", assignee="worker"))
    return ids


def _held_reasons(result, task_id):
    return [
        d.reason
        for d in result.dispositions
        if d.task_id == task_id and d.outcome == "held"
    ]


# ---------------------------------------------------------------------------
# Normal operation — the fence must not disrupt legitimate ticks
# ---------------------------------------------------------------------------


def test_queue_within_budget_spawns_everything(conn, all_assignees_spawnable):
    ids = _create_ready(conn, 3)
    calls = []
    result = kb.dispatch_once(
        conn, spawn_fn=_spawn_spy(calls), max_spawn_per_tick=4
    )
    assert sorted(t for t, _, _ in result.spawned) == sorted(ids)
    assert len(calls) == 3


def test_none_budget_is_unbounded(conn, all_assignees_spawnable):
    """Library back-compat: callers that never pass the new knob keep the
    old unbounded behavior. Only the CLI/gateway entry points apply the
    default fence."""
    ids = _create_ready(conn, 6)
    calls = []
    result = kb.dispatch_once(conn, spawn_fn=_spawn_spy(calls))
    assert sorted(t for t, _, _ in result.spawned) == sorted(ids)


# ---------------------------------------------------------------------------
# Normal operation — the fence must not disrupt legitimate ticks
# ---------------------------------------------------------------------------


def test_queue_within_budget_spawns_everything(conn, all_assignees_spawnable):
    ids = _create_ready(conn, 3)
    calls = []
    result = kb.dispatch_once(
        conn, spawn_fn=_spawn_spy(calls), max_spawn_per_tick=4
    )
    assert sorted(t for t, _, _ in result.spawned) == sorted(ids)
    assert len(calls) == 3


def test_none_budget_is_unbounded(conn, all_assignees_spawnable):
    """Library back-compat: callers that never pass the new knob keep the
    old unbounded behavior. Only the CLI/gateway entry points apply the
    default fence."""
    ids = _create_ready(conn, 6)
    calls = []
    result = kb.dispatch_once(conn, spawn_fn=_spawn_spy(calls))
    assert sorted(t for t, _, _ in result.spawned) == sorted(ids)


# ---------------------------------------------------------------------------
# Must-fire — create the exact bad condition, assert the fence blocks it.
# These tests FAIL when the ``_spawn_budget`` check is removed from
# ``_dispatch_once_locked`` (verified: 4 failed / 6 passed with the fence
# disabled) — that failure is the proof they exercise the fence.
# ---------------------------------------------------------------------------


def test_held_tasks_spawn_on_next_tick(conn, all_assignees_spawnable):
    """The fence defers, never drops: work held by the budget this tick is
    still ready and spawns once the next tick arrives with a fresh budget.
    (Asserts cap semantics, so it also fails with the fence removed.)"""
    ids = _create_ready(conn, 4)
    calls = []
    r1 = kb.dispatch_once(conn, spawn_fn=_spawn_spy(calls), max_spawn_per_tick=2)
    assert len(r1.spawned) == 2
    # Workers from tick 1 finish and complete their tasks.
    for tid, _, _ in r1.spawned:
        kb.complete_task(conn, tid, result="done")
    r2 = kb.dispatch_once(conn, spawn_fn=_spawn_spy(calls), max_spawn_per_tick=2)
    assert len(r2.spawned) == 2
    spawned_all = sorted(t for t, _, _ in r1.spawned + r2.spawned)
    assert spawned_all == sorted(ids)


def test_fence_holds_burst_beyond_budget(conn, all_assignees_spawnable):
    """Six ready tasks, budget 2: exactly 2 spawn, 4 are held with reason
    max_spawn_per_tick and remain ready + unclaimed (not failed/blocked)."""
    ids = _create_ready(conn, 6)
    calls = []
    result = kb.dispatch_once(
        conn, spawn_fn=_spawn_spy(calls), max_spawn_per_tick=2
    )
    spawned_ids = [t for t, _, _ in result.spawned]
    assert len(spawned_ids) == 2, f"burst fence must cap spawns: {spawned_ids}"
    assert len(calls) == 2, "spawn_fn must not be invoked past the budget"
    held_ids = [t for t in ids if t not in spawned_ids]
    assert len(held_ids) == 4
    for tid in held_ids:
        assert _held_reasons(result, tid) == ["max_spawn_per_tick"]
        task = kb.get_task(conn, tid)
        assert task.status == "ready", "held work must stay ready for next tick"
        assert task.claim_lock is None, "held work must not be claimed"


def test_fence_applies_in_dry_run(conn, all_assignees_spawnable):
    """A dry-run tick must report the same cap behavior it would enact —
    otherwise operators cannot preview the fence."""
    _create_ready(conn, 5)
    result = kb.dispatch_once(conn, dry_run=True, max_spawn_per_tick=2)
    assert len(result.spawned) == 2
    held = [d for d in result.dispositions if d.reason == "max_spawn_per_tick"]
    assert len(held) == 3


def test_fence_budget_shared_with_review_spawns(conn, all_assignees_spawnable):
    """Review-column spawns are worker processes against the same provider
    quota — they must draw from the same per-tick budget."""
    ready_ids = _create_ready(conn, 2)
    review_id = kb.create_task(conn, title="review me", assignee="worker")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='review' WHERE id=?", (review_id,)
        )
    calls = []
    result = kb.dispatch_once(
        conn, spawn_fn=_spawn_spy(calls), max_spawn_per_tick=2
    )
    assert len(result.spawned) == 2
    # Both ready spawns consumed the budget; the review task was not spawned.
    assert sorted(t for t, _, _ in result.spawned) == sorted(ready_ids)
    assert kb.get_task(conn, review_id).status == "review"


# ---------------------------------------------------------------------------
# Entry-point wiring — the fence must be ON by default where real
# dispatchers run, not just when a library caller opts in
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_burst_cap_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    yield test_home


def _run_cli_dispatch(monkeypatch, kanban_cfg):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"kanban": kanban_cfg}
    )
    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kanban_db.DispatchResult()

    monkeypatch.setattr(kanban_db, "dispatch_once", fake_dispatch_once)
    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    kb_cli._cmd_dispatch(args)
    return captured


def test_cli_defaults_burst_fence_on(isolated_kanban_home, monkeypatch):
    """Unset kanban.max_spawn_per_tick must not mean unbounded on the CLI:
    the dispatcher defaults to DEFAULT_MAX_SPAWN_PER_TICK."""
    captured = _run_cli_dispatch(monkeypatch, {})
    from hermes_cli import kanban_db

    assert captured.get("max_spawn_per_tick") == kanban_db.DEFAULT_MAX_SPAWN_PER_TICK


def test_cli_honors_config_override(isolated_kanban_home, monkeypatch):
    captured = _run_cli_dispatch(monkeypatch, {"max_spawn_per_tick": 7})
    assert captured.get("max_spawn_per_tick") == 7


def test_cli_zero_disables_explicitly(isolated_kanban_home, monkeypatch):
    """0 is the documented off switch — distinct from unset."""
    captured = _run_cli_dispatch(monkeypatch, {"max_spawn_per_tick": 0})
    assert captured.get("max_spawn_per_tick") is None


def test_cli_invalid_value_fails_safe_to_default(
    isolated_kanban_home, monkeypatch
):
    from hermes_cli import kanban_db

    captured = _run_cli_dispatch(monkeypatch, {"max_spawn_per_tick": "lots"})
    assert captured.get("max_spawn_per_tick") == kanban_db.DEFAULT_MAX_SPAWN_PER_TICK
