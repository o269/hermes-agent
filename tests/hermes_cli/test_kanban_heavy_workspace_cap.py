from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import pytest

from hermes_cli import kanban_db as kb

_PRODUCTION_SLOTS_ROOT = kb._heavy_workspace_slots_root


pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="heavy-workspace leases require POSIX flock"
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    # Production ignores caller-controlled runtime/home variables so every
    # profile shares one host lock domain. Tests pin the private helper directly
    # to avoid contending with live workers on the test host.
    monkeypatch.setattr(
        kb, "_heavy_workspace_slots_root", lambda: runtime_dir / "slots"
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _acquire_many(limit: int) -> list[kb._HeavyWorkspaceLease]:
    leases: list[kb._HeavyWorkspaceLease] = []
    for _ in range(limit):
        lease, reason = kb._try_acquire_heavy_workspace_lease(limit)
        assert reason == "acquired"
        assert lease is not None
        leases.append(lease)
    return leases


def _close_all(leases: list[kb._HeavyWorkspaceLease]) -> None:
    for lease in leases:
        lease.close()


def _wait_until_slot_is_released(limit: int = 1) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        lease, _reason = kb._try_acquire_heavy_workspace_lease(limit)
        if lease is not None:
            lease.close()
            return
        time.sleep(0.05)
    raise AssertionError("heavy-workspace slot was not released")


def test_host_slots_are_atomic_and_release_after_worker_crash(
    kanban_home: Path,
) -> None:
    lease, reason = kb._try_acquire_heavy_workspace_lease(1)
    assert reason == "acquired"
    assert lease is not None

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        pass_fds=lease.filenos(),
    )
    # The worker owns the inherited descriptor now; closing the dispatcher's
    # copy must not release capacity while the worker is alive.
    lease.close()
    blocked, reason = kb._try_acquire_heavy_workspace_lease(1)
    assert blocked is None
    assert reason == "capacity"

    child.kill()
    child.wait(timeout=5)

    replacement, reason = kb._try_acquire_heavy_workspace_lease(1)
    assert reason == "acquired"
    assert replacement is not None
    replacement.close()


def test_slot_cap_is_shared_with_an_independent_process(
    kanban_home: Path,
) -> None:
    leases = _acquire_many(3)
    try:
        script = """
import json
import os
from pathlib import Path
from hermes_cli import kanban_db as kb
kb._heavy_workspace_slots_root = lambda: Path(os.environ["TEST_SLOT_ROOT"])
lease, reason = kb._try_acquire_heavy_workspace_lease(3)
print(json.dumps({"acquired": lease is not None, "reason": reason}))
if lease is not None:
    lease.close()
"""
        child_env = dict(os.environ)
        child_env["HERMES_HOME"] = str(kanban_home.parent / "other-hermes-home")
        child_env["TEST_SLOT_ROOT"] = str(kb._heavy_workspace_slots_root())
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=child_env,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        assert json.loads(proc.stdout) == {
            "acquired": False,
            "reason": "capacity",
        }
    finally:
        _close_all(leases)


def test_limit_is_non_disableable_and_clamped_to_three() -> None:
    assert kb.resolve_heavy_workspace_limit(None) == 3
    assert kb.resolve_heavy_workspace_limit(0) == 3
    assert kb.resolve_heavy_workspace_limit("invalid") == 3
    assert kb.resolve_heavy_workspace_limit(4) == 3
    assert kb.resolve_heavy_workspace_limit(2) == 2


def test_lower_limit_fails_closed_while_higher_slot_is_occupied(
    kanban_home: Path,
) -> None:
    leases = _acquire_many(3)
    high_slot_lease = leases[2]
    leases[0].close()
    leases[1].close()
    try:
        blocked, reason = kb._try_acquire_heavy_workspace_lease(1)
        assert blocked is None
        assert reason == "capacity"

        snapshot = kb.heavy_workspace_capacity_snapshot(1)
        assert snapshot["in_use"] == 0
        assert snapshot["in_use_host"] == 1
        assert snapshot["active_slots"] == [2]
        assert snapshot["available"] == 0
    finally:
        high_slot_lease.close()


def test_host_lock_domain_ignores_profile_and_xdg_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", "/tmp/profile-a")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/runtime-a")
    first = _PRODUCTION_SLOTS_ROOT()
    monkeypatch.setenv("HERMES_HOME", "/tmp/profile-b")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/runtime-b")
    second = _PRODUCTION_SLOTS_ROOT()
    assert first == second


def test_capacity_snapshot_reports_slots_waiters_and_private_root(
    kanban_home: Path,
) -> None:
    leases = _acquire_many(2)
    try:
        waiting, waiting_reason = kb._try_acquire_fair_heavy_workspace_lease(
            2,
            board_key="waiting-board",
            task_id="waiting-task",
        )
        assert waiting is None and waiting_reason == "capacity"

        snapshot = kb.heavy_workspace_capacity_snapshot(2)
        assert snapshot["state"] == "ok"
        assert snapshot["limit"] == 2
        assert snapshot["in_use"] == 2
        assert snapshot["in_use_host"] == 2
        assert snapshot["available"] == 0
        assert snapshot["active_slots"] == [0, 1]
        assert snapshot["waiter_count"] == 1
        assert snapshot["waiters"][0]["board_key"] == "waiting-board"
        assert snapshot["waiters"][0]["task_id"] == "waiting-task"
        assert kb._heavy_workspace_slots_root().stat().st_mode & 0o777 == 0o700
    finally:
        _close_all(leases)


def test_cross_board_fifo_gives_waiting_board_the_next_slot(
    kanban_home: Path,
) -> None:
    first, reason = kb._try_acquire_fair_heavy_workspace_lease(
        1, board_key="board-a", task_id="a-1"
    )
    assert first is not None and reason == "acquired"
    try:
        blocked_b, reason_b = kb._try_acquire_fair_heavy_workspace_lease(
            1, board_key="board-b", task_id="b-1"
        )
        assert blocked_b is None and reason_b == "capacity"
        blocked_a, reason_a = kb._try_acquire_fair_heavy_workspace_lease(
            1, board_key="board-a", task_id="a-2"
        )
        assert blocked_a is None and reason_a == "fairness"
    finally:
        first.close()

    next_lease, next_reason = kb._try_acquire_fair_heavy_workspace_lease(
        1, board_key="board-b", task_id="b-1"
    )
    assert next_lease is not None and next_reason == "acquired"
    next_lease.close()


def test_fifo_waiter_survives_default_dispatch_interval_jitter(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held, reason = kb._try_acquire_heavy_workspace_lease(1)
    assert held is not None and reason == "acquired"
    real_time = time.time
    try:
        older, older_reason = kb._try_acquire_fair_heavy_workspace_lease(
            1,
            board_key="older-board",
            task_id="older-task",
        )
        assert older is None and older_reason == "capacity"

        monkeypatch.setattr(kb.time, "time", lambda: real_time() + 61)
        newer, newer_reason = kb._try_acquire_fair_heavy_workspace_lease(
            1,
            board_key="newer-board",
            task_id="newer-task",
        )
        assert newer is None and newer_reason == "fairness"
    finally:
        held.close()

    older, older_reason = kb._try_acquire_fair_heavy_workspace_lease(
        1,
        board_key="older-board",
        task_id="older-task",
    )
    assert older is not None and older_reason == "acquired"
    older.close()


def test_dead_dispatcher_waiter_does_not_block_live_board(
    kanban_home: Path,
) -> None:
    held, reason = kb._try_acquire_heavy_workspace_lease(1)
    assert held is not None and reason == "acquired"
    script = """
import json
import os
from pathlib import Path
from hermes_cli import kanban_db as kb
kb._heavy_workspace_slots_root = lambda: Path(os.environ["TEST_SLOT_ROOT"])
lease, reason = kb._try_acquire_fair_heavy_workspace_lease(
    1, board_key="dead-board", task_id="dead-task"
)
print(json.dumps({"acquired": lease is not None, "reason": reason}))
if lease is not None:
    lease.close()
"""
    child_env = dict(os.environ)
    child_env["TEST_SLOT_ROOT"] = str(kb._heavy_workspace_slots_root())
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=child_env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert json.loads(proc.stdout) == {"acquired": False, "reason": "capacity"}
    held.close()

    live, live_reason = kb._try_acquire_fair_heavy_workspace_lease(
        1, board_key="live-board", task_id="live-task"
    )
    assert live is not None and live_reason == "acquired"
    live.close()


def test_dispatch_admits_only_three_heavy_tasks_in_priority_order(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _assignee: True)
    children: list[subprocess.Popen[bytes]] = []

    def spawn(
        _task: kb.Task,
        _workspace: str,
        *,
        heavy_workspace_lease: kb._HeavyWorkspaceLease | None = None,
        **_kwargs: Any,
    ) -> int:
        assert heavy_workspace_lease is not None
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            pass_fds=(heavy_workspace_lease.fileno(),),
        )
        children.append(child)
        return child.pid

    try:
        with kb.connect() as conn:
            task_ids = [
                kb.create_task(
                    conn,
                    title=f"heavy {index}",
                    assignee=f"profile-{index}",
                    priority=50 - index,
                )
                for index in range(5)
            ]
            result = kb.dispatch_once(
                conn,
                spawn_fn=spawn,
                max_heavy_workspaces=3,
            )

            assert [task_id for task_id, _who, _ws in result.spawned] == task_ids[:3]
            assert result.skipped_heavy_workspace_capped == [
                (task_ids[3], 3, "capacity"),
                (task_ids[4], 3, "capacity"),
            ]
            for task_id in task_ids[:3]:
                assert kb.get_task(conn, task_id).status == "running"  # type: ignore[union-attr]
            for task_id in task_ids[3:]:
                task = kb.get_task(conn, task_id)
                assert task is not None
                assert task.status == "ready"
                assert task.current_run_id is None
                assert task.consecutive_failures == 0
                assert kb.list_runs(conn, task_id) == []

        extra, reason = kb._try_acquire_heavy_workspace_lease(3)
        assert extra is None
        assert reason == "capacity"
    finally:
        for child in children:
            child.kill()
        for child in children:
            child.wait(timeout=5)


def test_legacy_custom_spawn_is_rejected_before_side_effects(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _assignee: True)
    children: list[subprocess.Popen[bytes]] = []
    invoked: list[str] = []

    # Historical two-argument callbacks cannot prove that the returned PID is
    # the actual worker rather than a short-lived launcher. Reject them before
    # invocation so an untracked descendant can never escape the host cap.
    def legacy_spawn(task: kb.Task, _workspace: str) -> int:
        invoked.append(task.id)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        children.append(child)
        return child.pid

    try:
        with kb.connect() as conn:
            task_ids = [
                kb.create_task(
                    conn,
                    title=f"legacy heavy {index}",
                    assignee=f"legacy-profile-{index}",
                )
                for index in range(4)
            ]
            result = kb.dispatch_once(
                conn,
                spawn_fn=legacy_spawn,
                max_heavy_workspaces=3,
            )
            tasks = [kb.get_task(conn, task_id) for task_id in task_ids]

        assert invoked == []
        assert children == []
        assert result.spawned == []
        assert result.skipped_heavy_workspace_capped == [
            (task_id, 3, "lease_protocol") for task_id in task_ids
        ]
        assert all(task is not None and task.status == "ready" for task in tasks)
        assert all(task is not None and task.consecutive_failures == 0 for task in tasks)
        assert [entry.reason for entry in result.dispositions] == [
            "heavy_workspace_lease_protocol"
        ] * len(task_ids)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
        for child in children:
            child.wait(timeout=5)


def test_custom_spawn_without_live_pid_fails_closed_and_restores_task(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _assignee: True)

    def missing_pid(
        _task: kb.Task,
        _workspace: str,
        *,
        heavy_workspace_lease: kb._HeavyWorkspaceLease | None = None,
    ) -> None:
        assert heavy_workspace_lease is not None
        return None

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="missing pid", assignee="local")
        result = kb.dispatch_once(
            conn,
            spawn_fn=missing_pid,
            max_heavy_workspaces=3,
        )
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert result.spawned == []
    assert task is not None
    assert task.status == "ready"
    assert task.current_run_id is None
    assert task.consecutive_failures == 1
    assert "did not inherit heavy-workspace lease" in (task.last_failure_error or "")
    assert len(runs) == 1
    assert runs[0].outcome == "spawn_failed"
    assert [entry.reason for entry in result.dispositions] == ["spawn_failure"]

    lease, reason = kb._try_acquire_heavy_workspace_lease(3)
    assert lease is not None and reason == "acquired"
    lease.close()


def test_saturated_heavy_queue_does_not_block_explicit_light_work(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _assignee: True)
    leases = _acquire_many(3)
    spawned: list[str] = []

    def spawn(
        task: kb.Task,
        _workspace: str,
        *,
        heavy_workspace_lease: kb._HeavyWorkspaceLease | None = None,
        **_kwargs: Any,
    ) -> None:
        assert heavy_workspace_lease is None
        spawned.append(task.id)
        return None

    try:
        with kb.connect() as conn:
            heavy = kb.create_task(
                conn,
                title="highest priority heavy",
                assignee="heavy-profile",
                priority=100,
            )
            light = kb.create_task(
                conn,
                title="lower priority GET-only",
                body="Read metadata only.\nResource-Class: light\nNo clone or build.",
                assignee="light-profile",
                priority=10,
            )

            first = kb.dispatch_once(
                conn,
                spawn_fn=spawn,
                max_heavy_workspaces=3,
            )
            assert first.skipped_heavy_workspace_capped == [
                (heavy, 3, "capacity")
            ]
            assert [task_id for task_id, _who, _ws in first.spawned] == [light]
            assert spawned == [light]

            heavy_task = kb.get_task(conn, heavy)
            assert heavy_task is not None
            assert heavy_task.status == "ready"
            assert heavy_task.current_run_id is None
            assert heavy_task.consecutive_failures == 0
            assert kb.list_runs(conn, heavy) == []

            events = conn.execute(
                "SELECT payload FROM task_events "
                "WHERE task_id = ? AND kind = 'heavy_workspace_deferred'",
                (heavy,),
            ).fetchall()
            assert len(events) == 1
            payload = json.loads(events[0]["payload"])
            assert payload == {
                "scope": "host",
                "resource_class": "heavy",
                "limit": 3,
                "reason": "capacity",
                "state": "queue_wait",
            }

            # A second tick remains a queue wait and the bounded telemetry event
            # is not duplicated within the five-minute interval.
            second = kb.dispatch_once(
                conn,
                spawn_fn=spawn,
                max_heavy_workspaces=3,
            )
            assert second.skipped_heavy_workspace_capped == [
                (heavy, 3, "capacity")
            ]
            event_count = conn.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE task_id = ? AND kind = 'heavy_workspace_deferred'",
                (heavy,),
            ).fetchone()[0]
            assert event_count == 1
    finally:
        _close_all(leases)


def test_default_spawn_passes_lease_fd_to_local_worker(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeProc:
        pid = 2_000_000_000

    def fake_popen(_cmd: list[str], **kwargs: Any) -> FakeProc:
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kb, "_read_worker_process_identity", lambda _pid: (None, None, None))
    monkeypatch.setattr(kb, "worker_log_rotation_config", lambda: (1024, 1))

    task = kb.Task(
        id="t_heavy_fd",
        title="heavy fd",
        body=None,
        assignee="default",
        status="running",
        priority=0,
        created_by="test",
        created_at=0,
        started_at=0,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=str(tmp_path),
        claim_lock="claim",
        claim_expires=999,
        tenant=None,
    )
    lease, reason = kb._try_acquire_heavy_workspace_lease(1)
    assert reason == "acquired"
    assert lease is not None
    fds = lease.filenos()
    try:
        kb._default_spawn(
            task,
            str(tmp_path),
            heavy_workspace_lease=lease,
        )
    finally:
        lease.close()

    assert captured["pass_fds"] == fds
    assert captured["stdout"].closed


def test_gateway_dispatcher_passes_host_cap_to_every_board(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.run import GatewayRunner
    import hermes_cli.config as config_module

    runner = object.__new__(GatewayRunner)
    runner._running = True
    captured: list[dict[str, Any]] = []

    class FakeConn:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "max_heavy_workspaces": 3,
            }
        },
    )
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda include_archived=False: [{"slug": "alpha"}, {"slug": "beta"}],
    )
    monkeypatch.setattr(kb, "connect", lambda board=None: FakeConn())
    monkeypatch.setattr(
        kb,
        "kanban_db_path",
        lambda board=None: tmp_path / f"{board or 'default'}.db",
    )
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda _conn: False)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda _conn: False)

    def fake_dispatch_once(_conn: FakeConn, **kwargs: Any) -> kb.DispatchResult:
        captured.append(dict(kwargs))
        return kb.DispatchResult()

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)

    async def immediate_sleep(_delay: float) -> None:
        return None

    async def inline_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        if getattr(fn, "__name__", "") == "_tick_once":
            runner._running = False
        return result

    monkeypatch.setattr("gateway.kanban_watchers.asyncio.sleep", immediate_sleep)
    monkeypatch.setattr("gateway.kanban_watchers.asyncio.to_thread", inline_to_thread)

    asyncio.run(
        asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=3.0)
    )

    assert [entry["board"] for entry in captured] == ["alpha", "beta"]
    assert [entry["max_heavy_workspaces"] for entry in captured] == [3, 3]


def test_post_claim_exception_releases_slot_and_restores_ready(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _assignee: True)
    original_claim = kb.claim_task

    def claim_then_raise(*args: Any, **kwargs: Any) -> kb.Task:
        claimed = original_claim(*args, **kwargs)
        assert claimed is not None and claimed.status == "running"
        raise RuntimeError("synthetic post-claim failure")

    monkeypatch.setattr(kb, "claim_task", claim_then_raise)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="claim cleanup", assignee="profile-a")
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: None)
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.current_run_id is None
        assert task.claim_lock is None
        assert task_id not in [item[0] for item in result.spawned]

    replacement, reason = kb._try_acquire_heavy_workspace_lease(1)
    assert replacement is not None and reason == "acquired"
    replacement.close()


def test_ready_and_review_heavy_work_alternate_without_starvation(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _assignee: True)
    monkeypatch.setattr(kb, "_preflight_forced_skills", lambda *_args, **_kwargs: True)
    children: list[subprocess.Popen[bytes]] = []

    def spawn(
        task: kb.Task,
        _workspace: str,
        *,
        heavy_workspace_lease: kb._HeavyWorkspaceLease | None = None,
        **_kwargs: Any,
    ) -> int:
        assert heavy_workspace_lease is not None
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            pass_fds=heavy_workspace_lease.filenos(),
        )
        children.append(child)
        return child.pid

    try:
        with kb.connect() as conn:
            ready_id = kb.create_task(
                conn, title="ready heavy", assignee="ready-profile"
            )
            review_id = kb.create_task(
                conn, title="review heavy", assignee="review-profile"
            )
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,)
            )
            conn.commit()

            first = kb.dispatch_once(
                conn, spawn_fn=spawn, max_heavy_workspaces=1
            )
            assert [item[0] for item in first.spawned] == [review_id]
            assert (ready_id, 1, "phase_fairness") in (
                first.skipped_heavy_workspace_capped
            )
            kb.complete_task(conn, review_id)

            children[0].kill()
            children[0].wait(timeout=5)
            _wait_until_slot_is_released()
            second = kb.dispatch_once(
                conn, spawn_fn=spawn, max_heavy_workspaces=1
            )
            assert [item[0] for item in second.spawned] == [ready_id]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
        for child in children:
            child.wait(timeout=5)


@pytest.mark.parametrize("guarded_phase", ["ready", "review"])
def test_guarded_heavy_phase_rotates_so_opposite_phase_progresses_next_tick(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    guarded_phase: str,
) -> None:
    """A pre-admission hold must not pin the same phase turn forever."""
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _assignee: True)
    monkeypatch.setattr(kb, "_preflight_forced_skills", lambda *_args, **_kwargs: True)
    guarded_profile = f"{guarded_phase}-profile"
    opposite_phase = "review" if guarded_phase == "ready" else "ready"
    opposite_profile = f"{opposite_phase}-profile"
    children: list[subprocess.Popen[bytes]] = []

    def spawn(
        _task: kb.Task,
        _workspace: str,
        *,
        heavy_workspace_lease: kb._HeavyWorkspaceLease | None = None,
        **_kwargs: Any,
    ) -> int:
        assert heavy_workspace_lease is not None
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            pass_fds=heavy_workspace_lease.filenos(),
        )
        children.append(child)
        return child.pid

    try:
        with kb.connect() as conn:
            busy_id = kb.create_task(
                conn, title="already running", assignee=guarded_profile
            )
            kb.claim_task(conn, busy_id)
            guarded_id = kb.create_task(
                conn, title=f"guarded {guarded_phase}", assignee=guarded_profile
            )
            opposite_id = kb.create_task(
                conn, title=f"spawnable {opposite_phase}", assignee=opposite_profile
            )
            if guarded_phase == "review":
                conn.execute(
                    "UPDATE tasks SET status = 'review' WHERE id = ?", (guarded_id,)
                )
            if opposite_phase == "review":
                conn.execute(
                    "UPDATE tasks SET status = 'review' WHERE id = ?", (opposite_id,)
                )
            conn.commit()

            # Equivalent to the state immediately after the opposite phase had
            # the previous fair turn: this tick selects the guarded phase.
            kb._advance_heavy_workspace_phase_turn(
                conn,
                guarded_id,
                attempted_phase=opposite_phase,
            )

            first = kb.dispatch_once(
                conn,
                spawn_fn=spawn,
                max_heavy_workspaces=1,
                max_in_progress=99,
                skill_validator=lambda _profile, _skills: [],
            )
            second = kb.dispatch_once(
                conn,
                spawn_fn=spawn,
                max_heavy_workspaces=1,
                max_in_progress=99,
                skill_validator=lambda _profile, _skills: [],
            )

            assert first.spawned == []
            assert (guarded_id, guarded_profile, 1) in (
                first.skipped_per_profile_capped
            )
            assert (opposite_id, 1, "phase_fairness") in (
                first.skipped_heavy_workspace_capped
            )
            assert [item[0] for item in second.spawned] == [opposite_id]
            guarded = kb.get_task(conn, guarded_id)
            assert guarded is not None and guarded.status == guarded_phase
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
        for child in children:
            child.wait(timeout=5)


def test_dashboard_dispatch_cannot_bypass_host_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.kanban.dashboard import plugin_api
    import hermes_cli.config as config_module

    captured: dict[str, Any] = {}

    class FakeConn:
        def close(self) -> None:
            return None

    monkeypatch.setattr(plugin_api, "_resolve_board", lambda board: board or "default")
    monkeypatch.setattr(plugin_api, "_conn", lambda board=None: FakeConn())
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {"kanban": {"max_heavy_workspaces": 99}},
    )

    def fake_dispatch(_conn: FakeConn, **kwargs: Any) -> kb.DispatchResult:
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(plugin_api.kanban_db, "dispatch_once", fake_dispatch)
    plugin_api.dispatch(dry_run=True, max_n=99, board="default")
    assert captured["max_spawn"] == 99
    assert captured["max_heavy_workspaces"] == 3


def test_daemon_forwards_heavy_limit_to_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    captured: dict[str, Any] = {}
    stop = threading.Event()

    class FakeConn:
        def close(self) -> None:
            return None

    monkeypatch.setattr(kb, "connect", lambda: FakeConn())

    def fake_dispatch(_conn: FakeConn, **kwargs: Any) -> kb.DispatchResult:
        captured.update(kwargs)
        stop.set()
        return kb.DispatchResult()

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch)
    kb.run_daemon(
        interval=0,
        max_spawn=7,
        max_heavy_workspaces=2,
        stop_event=stop,
    )
    assert captured["max_spawn"] == 7
    assert captured["max_heavy_workspaces"] == 2
