"""Run-liveness oracle: PID-reuse safety, launch-fault triage, workspace custody.

Three defects motivate this file, all observed on the fleet host on
2026-08-03 (fork rate ~314/s, ``kernel.pid_max`` 4194304, PID wraparound seen
twice inside 20 minutes — the PID space cycles every 3-4 hours):

1. **False ALIVE.** A bare PID check reads ALIVE after the number is recycled
   onto an unrelated process, so a dead run is never reclaimed. The oracle
   must key on ``(pid, start-time, boot-id)``.
2. **Uninformative crashes.** The dispatcher runs as a ``Type=oneshot``
   systemd unit, so a worker is re-parented to init the moment its spawning
   tick exits and can never be ``waitpid``-ed by the next tick. The reap
   registry is therefore empty in production and every crash classifies as
   ``unknown`` → ``pid <N> not alive``, which explains nothing. Six spawns
   were burned on a one-line env gap that the worker's own log named outright.
3. **Workspace destroyed in use.** Deferred parent cleanup fired against a
   parent that was still ``running``, deleting a live worker's directory
   mid-write and losing 777 seconds of work.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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


# --------------------------------------------------------------------------
# 1. (pid, start-time) tuple — PID reuse must read DEAD
# --------------------------------------------------------------------------


@pytest.fixture
def live_process():
    """A real, long-lived child process. Yields ``(pid, create_time)``."""
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Give the kernel a moment to publish /proc/<pid>.
    for _ in range(50):
        _pgid, _sid, started = kb._read_worker_process_identity(proc.pid)
        if started is not None:
            break
        time.sleep(0.05)
    try:
        yield proc.pid, started
    finally:
        proc.kill()
        proc.wait()


def test_recorded_worker_alive_true_for_matching_identity(live_process):
    """A genuinely live worker whose recorded identity matches reads ALIVE."""
    pid, started = live_process
    assert started is not None, "kernel did not publish a process start time"
    assert kb._recorded_worker_alive(
        pid,
        worker_boot_id=kb._read_host_boot_id(),
        worker_started_at=started,
    ) is True


def test_recycled_pid_reads_dead(live_process):
    """Same PID, different start-time → DEAD.

    This is the false-ALIVE direction. The PID is genuinely alive, so a bare
    ``_pid_alive`` check says ALIVE and the stale run is never reclaimed. The
    recorded start-time proves the number was recycled onto a different
    incarnation.
    """
    pid, started = live_process
    assert kb._pid_alive(pid) is True, "precondition: the raw PID is alive"

    # The run recorded an incarnation that started an hour before this one —
    # i.e. the PID has since been recycled.
    recycled_start = float(started) - 3600.0
    assert kb._recorded_worker_alive(
        pid,
        worker_boot_id=kb._read_host_boot_id(),
        worker_started_at=recycled_start,
    ) is False


def test_reboot_invalidates_recorded_identity(live_process):
    """A PID recorded before a reboot must not authorize the same number after."""
    pid, started = live_process
    assert kb._recorded_worker_alive(
        pid,
        worker_boot_id="0000-boot-id-from-a-previous-boot",
        worker_started_at=started,
    ) is False


def test_legacy_row_without_identity_falls_back_to_pid(live_process):
    """Rows predating identity capture keep the old (weaker) behaviour."""
    pid, _started = live_process
    assert kb._recorded_worker_alive(pid) is True
    assert kb._recorded_worker_alive(999_999_999) is False


def test_signal_authority_requires_a_recorded_start_time(live_process):
    """Never signal a PID whose incarnation was never captured."""
    pid, started = live_process
    assert kb._recorded_worker_signal_authorized(pid) is False
    assert kb._recorded_worker_signal_authorized(
        pid, worker_started_at=started,
    ) is True


# --------------------------------------------------------------------------
# 2. Worker log tail + launch-class classification
# --------------------------------------------------------------------------


def test_worker_log_tail_returns_last_meaningful_line(kanban_home):
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "t_demo.log").write_bytes(
        b"starting up\n"
        b"\x1b[31mCould not find the Copilot CLI command 'copilot'.\x1b[0m\n"
        b"Goodbye!\n"
        b"\n   \n"
    )
    line, mtime = kb._worker_log_tail("t_demo")
    assert line == "Goodbye!", "trailing blank lines must be skipped"
    assert mtime is not None


def test_worker_log_tail_strips_ansi(kanban_home):
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "t_ansi.log").write_bytes(
        b"\x1b[1;33mCould not find the Copilot CLI command 'copilot'.\x1b[0m\n"
    )
    line, _mtime = kb._worker_log_tail("t_ansi")
    assert line == "Could not find the Copilot CLI command 'copilot'."


def test_worker_log_tail_missing_log_is_unknown_not_empty(kanban_home):
    """An unreadable log must read as unknown — never as evidence of a fault."""
    assert kb._worker_log_tail("t_absent") == (None, None)


def test_worker_log_tail_reads_only_the_end_of_a_large_log(kanban_home):
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "t_big.log").write_bytes(b"x" * 200_000 + b"\nfinal line\n")
    line, _mtime = kb._worker_log_tail("t_big")
    assert line == "final line"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("unknown", True),      # the only verdict a oneshot dispatcher ever gets
        ("nonzero_exit", True),
        ("signaled", True),
        ("clean_exit", False),  # protocol violation — the work may be done
        ("rate_limited", False),  # quota wall — explicitly not a task failure
    ],
)
def test_launch_class_eligibility_by_exit_kind(kind, expected):
    assert kb._is_launch_class_failure(
        kind, started_at=1000.0, log_mtime=1002.0, window_seconds=10,
    ) is expected


def test_launch_class_window_boundary():
    assert kb._is_launch_class_failure(
        "unknown", started_at=1000.0, log_mtime=1010.0, window_seconds=10,
    ) is True
    assert kb._is_launch_class_failure(
        "unknown", started_at=1000.0, log_mtime=1010.1, window_seconds=10,
    ) is False


def test_launch_class_requires_known_timing():
    """Unproven means ordinary retry, not a block."""
    assert kb._is_launch_class_failure(
        "unknown", started_at=None, log_mtime=1002.0,
    ) is False
    assert kb._is_launch_class_failure(
        "unknown", started_at=1000.0, log_mtime=None,
    ) is False
    # A log written *before* the run started belongs to a previous run.
    assert kb._is_launch_class_failure(
        "unknown", started_at=1000.0, log_mtime=900.0, window_seconds=10,
    ) is False


def test_launch_failure_window_env_override(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_LAUNCH_FAILURE_WINDOW_SECONDS", "45")
    assert kb._resolve_launch_failure_window_seconds() == 45.0
    # Zero disables the classification entirely.
    monkeypatch.setenv("HERMES_KANBAN_LAUNCH_FAILURE_WINDOW_SECONDS", "0")
    assert kb._is_launch_class_failure(
        "unknown", started_at=1000.0, log_mtime=1001.0,
    ) is False
    # Garbage falls back to the default rather than disabling the feature.
    monkeypatch.setenv("HERMES_KANBAN_LAUNCH_FAILURE_WINDOW_SECONDS", "banana")
    assert (
        kb._resolve_launch_failure_window_seconds()
        == float(kb.DEFAULT_LAUNCH_FAILURE_WINDOW_SECONDS)
    )


# --------------------------------------------------------------------------
# 3. detect_crashed_workers surfaces the log line and fast-fails launch faults
# --------------------------------------------------------------------------


def _running_task(conn, *, title, pid, started_at):
    tid = kb.create_task(conn, title=title, assignee="a")
    host = kb._claimer_id().split(":", 1)[0]
    conn.execute(
        "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
        "started_at=? WHERE id=?",
        (pid, f"{host}:w{pid}", int(started_at), tid),
    )
    conn.commit()
    return tid


def _write_log(task_id, text, *, mtime):
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{task_id}.log"
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_crash_error_carries_the_worker_last_log_line(kanban_home, monkeypatch):
    """``pid <N> not alive`` alone is what cost six spawns. Name the cause."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    started = time.time() - 3600  # long past the launch window
    with kb.connect() as conn:
        tid = _running_task(conn, title="log tail", pid=90001, started_at=started)
        _write_log(
            tid,
            "Could not find the Copilot CLI command 'copilot'.\n",
            mtime=started + 1800,
        )
        assert tid in kb.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)

    assert "Could not find the Copilot CLI command" in (task.last_failure_error or "")


def test_launch_class_failure_stops_short_of_the_full_retry_budget(
    kanban_home, monkeypatch,
):
    """A sub-second config fault must block on the first occurrence."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    started = time.time() - 600
    with kb.connect() as conn:
        tid = _running_task(conn, title="launch fault", pid=90002, started_at=started)
        # Worker stopped writing 2s after start → launch-class.
        _write_log(
            tid,
            "Could not find the Copilot CLI command 'copilot'.\nGoodbye!\n",
            mtime=started + 2,
        )
        assert tid in kb.detect_crashed_workers(conn)
        first = kb.get_task(conn, tid)
        assert first.status == "ready", (
            f"first fast death must still requeue (got {first.status})"
        )
        assert "launch failure" in (first.last_failure_error or "")
        assert "Goodbye!" in (first.last_failure_error or "")

        # Same fault, second spawn.
        host = kb._claimer_id().split(":", 1)[0]
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (90012, f"{host}:w90012", int(started), tid),
        )
        conn.commit()
        assert tid in kb.detect_crashed_workers(conn)
        second = kb.get_task(conn, tid)

    assert second.status == "blocked", (
        "a repeated launch-class failure must block well short of the full "
        f"retry budget (got {second.status})"
    )


def test_launch_class_event_marks_the_run(kanban_home, monkeypatch):
    """The crash event carries ``launch_failure`` so triage can filter on it."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    started = time.time() - 600
    with kb.connect() as conn:
        tid = _running_task(conn, title="marked", pid=90004, started_at=started)
        _write_log(tid, "boom at launch\n", mtime=started + 1)
        assert tid in kb.detect_crashed_workers(conn)
        rows = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'crashed'",
            (tid,),
        ).fetchall()

    payloads = [json.loads(r["payload"]) for r in rows]
    assert any(p.get("launch_failure") for p in payloads)
    assert any(p.get("last_log_line") == "boom at launch" for p in payloads)


def test_mid_task_crash_still_takes_the_normal_retry_path(kanban_home, monkeypatch):
    """Negative control: a long-lived worker's crash must NOT be fast-blocked.

    Without this, the fast-fail would swallow every genuine crash and the
    check would be worse than useless.
    """
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    started = time.time() - 3600
    with kb.connect() as conn:
        tid = _running_task(conn, title="real crash", pid=90003, started_at=started)
        # Worker wrote for 30 minutes before dying — a real mid-task crash.
        _write_log(tid, "...doing work...\nboom\n", mtime=started + 1800)
        assert tid in kb.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)

    assert task.status == "ready", (
        f"an isolated mid-task crash keeps its retry budget (got {task.status})"
    )
    assert "launch failure" not in (task.last_failure_error or "")


# --------------------------------------------------------------------------
# 4. Deferred parent-workspace cleanup must not delete a live worker's dir
# --------------------------------------------------------------------------


def _scratch_parent_with_child(conn, *, parent_status, workspaces_root):
    parent = kb.create_task(conn, title="parent", assignee="a")
    child = kb.create_task(conn, title="child", assignee="b", parents=[parent])
    wp = Path(workspaces_root) / parent
    wp.mkdir(parents=True, exist_ok=True)
    (wp / "deliverable.md").write_text("777 seconds of work", encoding="utf-8")
    conn.execute(
        "UPDATE tasks SET workspace_kind='scratch', workspace_path=?, status=? "
        "WHERE id=?",
        (str(wp), parent_status, parent),
    )
    conn.commit()
    return parent, child, wp


def test_running_parent_workspace_survives_child_completion(kanban_home, monkeypatch):
    """Regression: the 777-second data loss.

    A decomposition parent that is still ``running`` must keep its scratch
    workspace when its last child goes terminal.
    """
    monkeypatch.setattr(kb, "_is_managed_scratch_path", lambda _p: True)
    with kb.connect() as conn:
        parent, child, wp = _scratch_parent_with_child(
            conn, parent_status="running", workspaces_root=kanban_home / "ws",
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
        conn.commit()

        kb._try_cleanup_parent_workspaces(conn, child)

    assert wp.is_dir(), "a running parent's workspace must not be reaped"
    assert (wp / "deliverable.md").exists()


def test_done_parent_workspace_is_reaped_after_children_finish(
    kanban_home, monkeypatch,
):
    """Positive control: the deferred cleanup this guard must not break."""
    monkeypatch.setattr(kb, "_is_managed_scratch_path", lambda _p: True)
    with kb.connect() as conn:
        parent, child, wp = _scratch_parent_with_child(
            conn, parent_status="done", workspaces_root=kanban_home / "ws",
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
        conn.commit()

        kb._try_cleanup_parent_workspaces(conn, child)

    assert not wp.exists(), "a finished parent's workspace should still be reclaimed"


def test_done_parent_with_live_worker_is_deferred(kanban_home, monkeypatch, live_process):
    """Terminal status can still race a worker mid-teardown — hold the dir."""
    monkeypatch.setattr(kb, "_is_managed_scratch_path", lambda _p: True)
    pid, started = live_process
    with kb.connect() as conn:
        parent, child, wp = _scratch_parent_with_child(
            conn, parent_status="done", workspaces_root=kanban_home / "ws",
        )
        run_id = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, worker_pid, "
            "worker_boot_id, worker_started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                parent, "running", int(time.time()), pid,
                kb._read_host_boot_id(), started,
            ),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET worker_pid=?, current_run_id=? WHERE id=?",
            (pid, run_id, parent),
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
        conn.commit()

        kb._try_cleanup_parent_workspaces(conn, child)

    assert wp.is_dir(), "cleanup must defer while the recorded worker is alive"
