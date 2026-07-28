"""Regression tests for fail-closed exact-task kanban dispatch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def exact_dispatch_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    for profile in ("alpha", "beta", "default"):
        (home / "profiles" / profile).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BROKER", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as conn:
        yield conn


def _spawn_recorder(calls: list[str]):
    def spawn(task, _workspace, board=None):
        calls.append(task.id)
        return 90001 + len(calls)

    return spawn


def _outcomes(result: kb.DispatchResult) -> dict[str, kb.RequestedDispatchOutcome]:
    return {item.task_id: item for item in result.requested_outcomes}


def _task(conn, task_id: str) -> kb.Task:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def test_exact_target_does_not_spawn_higher_priority_unrequested_ready_task(
    exact_dispatch_board,
):
    conn = exact_dispatch_board
    heavy = kb.create_task(
        conn,
        title="heavy build",
        assignee="alpha",
        priority=1000,
    )
    target = kb.create_task(
        conn,
        title="low cpu read only",
        assignee="beta",
        priority=1,
    )
    calls: list[str] = []

    result = kb.dispatch_once(
        conn,
        task_ids=[target],
        spawn_fn=_spawn_recorder(calls),
        max_spawn=4,
    )

    assert calls == [target]
    assert [item[0] for item in result.spawned] == [target]
    assert _outcomes(result)[target].outcome == "spawned"
    assert _task(conn, heavy).status == "ready"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (heavy,)
        ).fetchone()[0]
        == 0
    )


def test_recompute_can_promote_unrequested_task_without_dispatching_it(
    exact_dispatch_board,
):
    conn = exact_dispatch_board
    parent = kb.create_task(conn, title="parent", assignee="alpha")
    child = kb.create_task(
        conn,
        title="newly promoted heavy child",
        assignee="alpha",
        priority=1000,
        parents=[parent],
    )
    target = kb.create_task(
        conn,
        title="explicit safe target",
        assignee="beta",
        priority=1,
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'done', claim_lock = NULL WHERE id = ?",
            (parent,),
        )
    assert _task(conn, child).status == "todo"
    calls: list[str] = []

    result = kb.dispatch_once(
        conn,
        task_ids=[target],
        spawn_fn=_spawn_recorder(calls),
        max_spawn=4,
    )

    assert result.promoted == 1
    assert calls == [target]
    assert _task(conn, child).status == "ready"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (child,)
        ).fetchone()[0]
        == 0
    )


def test_empty_unknown_and_malformed_target_sets_never_fall_back(
    exact_dispatch_board,
):
    conn = exact_dispatch_board
    heavy = kb.create_task(conn, title="generic heavy", assignee="alpha")
    calls: list[str] = []
    spawn = _spawn_recorder(calls)

    empty = kb.dispatch_once(conn, task_ids=[], spawn_fn=spawn)
    mixed = kb.dispatch_once(
        conn,
        task_ids=["t_deadbeef", "not-a-task"],
        spawn_fn=spawn,
    )
    malformed_with_valid = kb.dispatch_once(
        conn,
        task_ids=[heavy, "BAD"],
        spawn_fn=spawn,
    )

    assert empty.targeted is True
    assert empty.requested_outcomes == []
    assert [(item.task_id, item.outcome) for item in mixed.requested_outcomes] == [
        ("t_deadbeef", "not_found"),
        ("not-a-task", "malformed"),
    ]
    assert [
        (item.task_id, item.outcome) for item in malformed_with_valid.requested_outcomes
    ] == [
        (heavy, "target_set_invalid"),
        ("BAD", "malformed"),
    ]
    assert calls == []
    assert _task(conn, heavy).status == "ready"


def test_targeted_dry_run_has_no_database_writes_or_spawn_calls(
    exact_dispatch_board,
):
    conn = exact_dispatch_board
    target = kb.create_task(conn, title="target", assignee="alpha")
    kb.create_task(conn, title="unrequested", assignee="beta", priority=100)
    total_changes_before = conn.total_changes
    dump_before = "\n".join(conn.iterdump())

    def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("dry-run must not invoke spawn_fn")

    result = kb.dispatch_once(
        conn,
        task_ids=[target],
        spawn_fn=forbidden_spawn,
        dry_run=True,
    )

    assert _outcomes(result)[target].outcome == "spawned"
    assert result.spawned == [(target, "alpha", "")]
    assert conn.total_changes == total_changes_before
    assert "\n".join(conn.iterdump()) == dump_before


def test_targeted_dispatch_lock_contention_fails_closed_without_writes(
    exact_dispatch_board,
):
    conn = exact_dispatch_board
    target = kb.create_task(conn, title="target", assignee="alpha")
    total_changes_before = conn.total_changes
    dump_before = "\n".join(conn.iterdump())
    calls: list[str] = []

    with kb._dispatch_tick_lock(kb.kanban_db_path(board="default")) as held:
        assert held is True
        result = kb.dispatch_once(
            conn,
            task_ids=[target, "BAD"],
            spawn_fn=_spawn_recorder(calls),
        )

    assert result.skipped_locked is True
    assert result.targeted is True
    assert [(item.task_id, item.outcome) for item in result.requested_outcomes] == [
        (target, "locked"),
        ("BAD", "malformed"),
    ]
    assert calls == []
    assert conn.total_changes == total_changes_before
    assert "\n".join(conn.iterdump()) == dump_before


@pytest.mark.parametrize(
    "limit_kwargs",
    [{"max_spawn": 1}, {"max_in_progress": 1}],
    ids=["cli-max", "configured-max-in-progress"],
)
def test_targeted_dispatch_preserves_global_running_ceiling(
    exact_dispatch_board,
    limit_kwargs,
):
    conn = exact_dispatch_board
    running = kb.create_task(conn, title="already running", assignee="alpha")
    assert kb.claim_task(conn, running) is not None
    target = kb.create_task(conn, title="target", assignee="beta")
    calls: list[str] = []

    result = kb.dispatch_once(
        conn,
        task_ids=[target],
        spawn_fn=_spawn_recorder(calls),
        **limit_kwargs,
    )

    assert calls == []
    assert _outcomes(result)[target].outcome == "ceiling_reached"
    assert _task(conn, target).status == "ready"
    assert (
        conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[
            0
        ]
        == 1
    )


def test_target_outcomes_cover_claimed_status_and_missing(exact_dispatch_board):
    conn = exact_dispatch_board
    claimed = kb.create_task(conn, title="claimed", assignee="alpha")
    assert kb.claim_task(conn, claimed) is not None
    not_ready = kb.create_task(conn, title="done", assignee="beta")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (not_ready,))

    result = kb.dispatch_once(
        conn,
        task_ids=[claimed, not_ready, "t_deadbeef", "BAD"],
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("nothing is spawnable"),
    )

    assert [(item.task_id, item.outcome) for item in result.requested_outcomes] == [
        (claimed, "claimed"),
        (not_ready, "status_not_ready"),
        ("t_deadbeef", "not_found"),
        ("BAD", "malformed"),
    ]


def test_target_outcome_profile_capped(exact_dispatch_board):
    conn = exact_dispatch_board
    running = kb.create_task(conn, title="running", assignee="alpha")
    assert kb.claim_task(conn, running) is not None
    target = kb.create_task(conn, title="target", assignee="alpha")

    result = kb.dispatch_once(
        conn,
        task_ids=[target],
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("profile is capped"),
    )

    outcome = _outcomes(result)[target]
    assert (outcome.outcome, outcome.assignee, outcome.current) == (
        "profile_capped",
        "alpha",
        1,
    )


def test_target_state_race_fails_closed(exact_dispatch_board, monkeypatch):
    conn = exact_dispatch_board
    target = kb.create_task(conn, title="racing target", assignee="alpha")

    def lose_claim_race(race_conn, task_id, **_kwargs):
        with kb.write_txn(race_conn):
            race_conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?",
                (task_id,),
            )
        return None

    monkeypatch.setattr(kb, "claim_task", lose_claim_race)
    result = kb.dispatch_once(
        conn,
        task_ids=[target],
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("race loser must not spawn"),
    )

    outcome = _outcomes(result)[target]
    assert (outcome.outcome, outcome.detail) == ("status_not_ready", "done")
    assert _task(conn, target).status == "done"


def test_target_outcome_nonspawnable(exact_dispatch_board):
    conn = exact_dispatch_board
    target = kb.create_task(conn, title="terminal lane", assignee="not-a-profile")

    result = kb.dispatch_once(
        conn,
        task_ids=[target],
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("lane must not spawn"),
    )

    outcome = _outcomes(result)[target]
    assert (outcome.outcome, outcome.assignee) == (
        "nonspawnable",
        "not-a-profile",
    )


def test_target_outcome_respawn_guarded(exact_dispatch_board, monkeypatch):
    conn = exact_dispatch_board
    target = kb.create_task(conn, title="guarded", assignee="alpha")
    monkeypatch.setattr(
        kb,
        "evaluate_respawn_guard",
        lambda *_args, **_kwargs: kb.RespawnGuardDecision(reason="recent_success"),
    )

    result = kb.dispatch_once(
        conn,
        task_ids=[target],
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("guarded task must not spawn"),
    )

    outcome = _outcomes(result)[target]
    assert (outcome.outcome, outcome.detail) == (
        "respawn_guarded",
        "recent_success",
    )


def test_dispatch_parser_collects_repeatable_exact_task_ids():
    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="command")
    kanban_cli.build_parser(subparsers)

    args = root.parse_args([
        "kanban",
        "dispatch",
        "--task-id",
        "t_deadbeef",
        "--task-id",
        "t_feedface",
    ])

    assert args.task_ids == ["t_deadbeef", "t_feedface"]


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_reports_each_exact_requested_outcome(
    exact_dispatch_board,
    monkeypatch,
    capsys,
    as_json,
):
    outcomes = [
        kb.RequestedDispatchOutcome("BAD", "malformed"),
        kb.RequestedDispatchOutcome("t_deadbeef", "not_found"),
        kb.RequestedDispatchOutcome("t_00000001", "status_not_ready", detail="todo"),
        kb.RequestedDispatchOutcome("t_00000002", "claimed", detail="running"),
        kb.RequestedDispatchOutcome(
            "t_00000003", "profile_capped", assignee="alpha", current=1
        ),
        kb.RequestedDispatchOutcome(
            "t_00000004", "nonspawnable", assignee="terminal-lane"
        ),
        kb.RequestedDispatchOutcome(
            "t_00000005", "respawn_guarded", detail="recent_success"
        ),
        kb.RequestedDispatchOutcome("t_00000006", "ceiling_reached"),
        kb.RequestedDispatchOutcome(
            "t_00000007", "spawned", assignee="beta", workspace="/tmp/w"
        ),
    ]
    fake_result = kb.DispatchResult(
        targeted=True,
        requested_outcomes=outcomes,
        spawned=[("t_00000007", "beta", "/tmp/w")],
    )
    monkeypatch.setattr(kb, "dispatch_once", lambda *_args, **_kwargs: fake_result)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    args = argparse.Namespace(
        dry_run=False,
        max=None,
        failure_limit=2,
        json=as_json,
        task_ids=[item.task_id for item in outcomes],
    )

    assert kanban_cli._cmd_dispatch(args) == 1
    output = capsys.readouterr().out
    if as_json:
        payload = json.loads(output)
        assert [(row["task_id"], row["outcome"]) for row in payload["requested"]] == [
            (item.task_id, item.outcome) for item in outcomes
        ]
        assert payload["targeted"] is True
    else:
        for item in outcomes:
            assert f"{item.task_id}: {item.outcome}" in output


def test_cli_empty_target_result_is_nonzero_and_explicit(
    exact_dispatch_board,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        kb,
        "dispatch_once",
        lambda *_args, **_kwargs: kb.DispatchResult(targeted=True),
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    args = argparse.Namespace(
        dry_run=False,
        max=None,
        failure_limit=2,
        json=False,
        task_ids=[],
    )

    assert kanban_cli._cmd_dispatch(args) == 1
    assert "empty target set" in capsys.readouterr().out
