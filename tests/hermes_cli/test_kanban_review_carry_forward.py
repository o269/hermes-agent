"""Regression coverage for review-lane dispatch and run lifecycle fencing."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a real temp HERMES_HOME and board, never the operator's DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_BROKER",
        "BOARDD_SOCK",
    ):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    return home


@pytest.fixture
def all_profiles_spawnable(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _profile: True)


def _move_to_review(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = 'review' WHERE id = ?",
        (task_id,),
    )


def _claim_review(
    conn: sqlite3.Connection,
    *,
    max_runtime_seconds: int | None = None,
) -> tuple[str, kb.Run]:
    task_id = kb.create_task(
        conn,
        title="review carry-forward",
        assignee="default",
        max_runtime_seconds=max_runtime_seconds,
    )
    _move_to_review(conn, task_id)
    host = kb._claimer_id().split(":", 1)[0]
    claimed = kb.claim_review_task(conn, task_id, claimer=f"{host}:reviewer")
    assert claimed is not None
    run = kb.latest_run(conn, task_id)
    assert run is not None
    assert claimed.dispatch_origin == "review"
    assert run.dispatch_origin == "review"
    return task_id, run


@pytest.mark.parametrize(
    ("recovery", "expected_outcome"),
    [
        ("stale_claim", "reclaimed"),
        ("timeout", "timed_out"),
        ("crash", "crashed"),
        ("rate_limit", "rate_limited"),
        ("spawn_failure", "spawn_failed"),
    ],
)
def test_review_origin_survives_every_recovery_path(
    isolated_board: Path,
    all_profiles_spawnable: None,
    monkeypatch: pytest.MonkeyPatch,
    recovery: str,
    expected_outcome: str,
) -> None:
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        if recovery == "spawn_failure":
            task_id = kb.create_task(
                conn,
                title="review spawn failure",
                assignee="default",
            )
            _move_to_review(conn, task_id)

            def fail_spawn(_task, _workspace, board=None):
                raise RuntimeError("review worker launch failed")

            result = kb.dispatch_once(
                conn,
                spawn_fn=fail_spawn,
                skill_validator=lambda _profile, _skills: [],
                failure_limit=3,
            )
            assert task_id not in result.auto_blocked
        else:
            task_id, run = _claim_review(
                conn,
                max_runtime_seconds=1 if recovery == "timeout" else None,
            )
            if recovery == "stale_claim":
                conn.execute(
                    "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                    (int(time.time()) - 60, task_id),
                )
                assert kb.release_stale_claims(conn, signal_fn=lambda *_: None) == 1
            elif recovery == "timeout":
                old = int(time.time()) - 10
                conn.execute(
                    "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                    (910001, task_id),
                )
                conn.execute(
                    "UPDATE task_runs SET worker_pid = ?, started_at = ? WHERE id = ?",
                    (910001, old, run.id),
                )
                assert kb.enforce_max_runtime(conn, signal_fn=lambda *_: None) == [
                    task_id
                ]
            else:
                pid = 910002 if recovery == "crash" else 910003
                conn.execute(
                    "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                    (pid, task_id),
                )
                conn.execute(
                    "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                    (pid, run.id),
                )
                exit_code = 1 if recovery == "crash" else kb.KANBAN_RATE_LIMIT_EXIT_CODE
                kb._record_worker_exit(pid, exit_code << 8)
                kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, task_id)
        latest = kb.latest_run(conn, task_id)
        assert task is not None
        assert latest is not None
        assert task.status == "review"
        assert task.dispatch_origin == "review"
        assert task.current_run_id is None
        assert latest.dispatch_origin == "review"
        assert latest.outcome == expected_outcome
        assert latest.ended_at is not None


def test_forced_skill_failure_then_review_retry_delivers_verdict(
    isolated_board: Path,
    all_profiles_spawnable: None,
) -> None:
    """Exercise the real profile-scoped skill probe and complete lifecycle."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="review with required skill",
            assignee="default",
        )
        _move_to_review(conn, task_id)

        assert kb._missing_forced_skills("default", ["sdlc-review"]) == ["sdlc-review"]
        first = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: None)
        task = kb.get_task(conn, task_id)
        assert first.forced_skill_blocked == [task_id]
        assert not first.spawned
        assert task is not None
        assert task.status == "blocked"
        assert task.dispatch_origin == "review"
        assert task.current_run_id is None
        assert kb.list_runs(conn, task_id) == []
        assert task.last_failure_error is not None
        assert task.last_failure_error.startswith("forced_skills_unavailable:")
        blocker = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'forced_skill_preflight_blocked' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert blocker is not None
        assert '"blocker": "forced_skills_unavailable"' in blocker["payload"]

        skill_dir = isolated_board / "skills" / "sdlc-review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: sdlc-review\n"
            "description: Use when reviewing a test PR.\n"
            "---\n\n"
            "# Review\n\nReport PASS or FIX_REQUIRED; never merge.\n",
            encoding="utf-8",
        )
        assert kb._missing_forced_skills("default", ["sdlc-review"]) == []

        assert kb.unblock_task(conn, task_id)
        unblocked = kb.get_task(conn, task_id)
        assert unblocked is not None
        assert unblocked.status == "review"

        spawned: list[kb.Task] = []

        def capture_spawn(task: kb.Task, _workspace: str, board=None):
            spawned.append(task)
            return None

        second = kb.dispatch_once(conn, spawn_fn=capture_spawn)
        assert [item[0] for item in second.spawned] == [task_id]
        assert spawned[0].skills == ["sdlc-review"]
        running = kb.get_task(conn, task_id)
        assert running is not None
        assert running.status == "running"
        assert running.dispatch_origin == "review"
        run_id = running.current_run_id
        assert run_id is not None

        verdict = "PASS: verified; PR remains for Fable, the sole lander."
        assert kb.complete_task(
            conn,
            task_id,
            result=verdict,
            summary=verdict,
            expected_run_id=run_id,
        )
        done = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert done is not None and done.status == "done"
        assert done.current_run_id is None
        assert done.claim_lock is None
        assert run is not None
        assert run.dispatch_origin == "review"
        assert run.outcome == "completed"
        assert run.ended_at is not None
        assert run.summary == verdict


@pytest.mark.parametrize(
    ("transition", "expected_status", "expected_outcome"),
    [
        ("complete", "done", "completed"),
        ("block", "blocked", "blocked"),
        # Archiving an in-flight attempt preserves the established lifecycle
        # meaning: the task is archived, while its interrupted run is reclaimed.
        ("archive", "archived", "reclaimed"),
    ],
)
def test_terminal_transition_closes_orphan_open_run_atomically(
    isolated_board: Path,
    transition: str,
    expected_status: str,
    expected_outcome: str,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title=f"terminal {transition}", assignee="default"
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run = kb.latest_run(conn, task_id)
        assert run is not None

        # Reproduce the external-transition corruption class: the run is open,
        # but its denormalized task pointer was cleared before terminalization.
        conn.execute(
            "UPDATE tasks SET current_run_id = NULL WHERE id = ?",
            (task_id,),
        )
        if transition == "complete":
            assert kb.complete_task(conn, task_id, summary="complete orphan")
        elif transition == "block":
            assert kb.block_task(conn, task_id, reason="block orphan")
        else:
            assert kb.archive_task(conn, task_id)

        task = kb.get_task(conn, task_id)
        closed = kb.list_runs(conn, task_id)
        assert task is not None
        assert task.status == expected_status
        assert task.current_run_id is None
        assert task.claim_lock is None
        assert task.claim_expires is None
        assert task.worker_pid is None
        assert len(closed) == 1
        assert closed[0].outcome == expected_outcome
        assert closed[0].ended_at is not None
        assert closed[0].claim_lock is None
        assert closed[0].claim_expires is None
        assert closed[0].worker_pid is None
