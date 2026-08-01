"""Canonical attached-SSH lifecycle tests for fleet VPS2 Kanban workers."""
from __future__ import annotations

import builtins
import io
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import fleet_vps2_worker as vps2


@pytest.fixture
def isolated_board(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb

    kb.init_db()
    return kb


@pytest.fixture
def attached_transport(monkeypatch):
    """Replace SSH with a real local attached process carrying canonical env."""
    config = vps2.Vps2WorkerConfig(enabled=True, start_grace_seconds=0.05)
    processes: list[subprocess.Popen] = []

    monkeypatch.setattr(
        vps2,
        "configured_vps2_worker",
        lambda assignee: config if vps2.is_vps2_assignee(assignee) else None,
    )

    def fake_spawn(**kwargs):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(kwargs["local_env"]),
            start_new_session=True,
        )
        processes.append(proc)
        return proc

    monkeypatch.setattr(vps2, "spawn_vps2_worker_via_ssh", fake_spawn)
    yield config, processes
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _make_task(kb, *, assignee: str = "vps2-eng1"):
    return kb.Task(
        id="t_vps2_spawn",
        title="remote spawn",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="blitz-vps:123",
        claim_expires=None,
        tenant="tenant-a",
        current_run_id=7,
        branch_name="fleet/task",
    )


def _task_row(conn, task_id: str):
    return conn.execute(
        "SELECT status, claim_lock, claim_expires, worker_pid, current_run_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def test_config_is_disabled_by_default_and_uses_config_mapping_only():
    default = vps2.Vps2WorkerConfig.from_mapping({})
    configured = vps2.Vps2WorkerConfig.from_mapping(
        {
            "enabled": True,
            "host": "fleet-vps2",
            "user": "runner",
            "start_timeout_seconds": 7,
        }
    )

    assert default.enabled is False
    assert configured.enabled is True
    assert configured.ssh_host == "fleet-vps2"
    assert configured.ssh_user == "runner"
    assert configured.start_timeout_seconds == 7


def test_remote_command_is_foreground_supervisor_with_profile_and_env_payload():
    config = vps2.Vps2WorkerConfig(enabled=True)
    command = vps2.build_remote_worker_command(
        task_id="t_payload",
        board="fleet",
        workspace="/local/workspaces/project/.worktrees/t_payload",
        local_workspace_root="/local/workspaces",
        local_env={
            "HERMES_HOME": "/home/local/.hermes/profiles/vps2-eng1",
            "HERMES_KANBAN_DB": "/home/local/kanban.db",
            "HERMES_KANBAN_RUN_ID": "44",
            "HERMES_KANBAN_CLAIM_LOCK": "blitz-vps:991",
            "HERMES_KANBAN_BRANCH": "fleet/t_payload",
            "HERMES_PROFILE": "vps2-eng1",
            "HERMES_TENANT": "tenant-a",
        },
        worker_argv=[
            "-p",
            "vps2-eng1",
            "--cli",
            "--accept-hooks",
            "--skills",
            "github-workflows",
            "chat",
            "-q",
            "work kanban task t_payload",
        ],
        ready_token="START:nonce",
        lease_token="LEASE:nonce",
        config=config,
    )
    payload = vps2._decode_payload(shlex.split(command)[-1])

    assert "test -x /root/.local/bin/hermes" in command
    assert "test -S /run/boardd-blitz.sock" in command
    assert "exec python3 -c " in command
    assert payload["ready_token"] == "START:nonce"
    assert payload["lease_token"] == "LEASE:nonce"
    assert payload["workspace"].endswith("/project/.worktrees/t_payload")
    assert payload["env"]["HERMES_KANBAN_BROKER"] == "1"
    assert payload["env"]["BOARDD_SOCK"] == "/run/boardd-blitz.sock"
    assert payload["env"]["HERMES_KANBAN_RUN_ID"] == "44"
    assert payload["env"]["HERMES_KANBAN_CLAIM_LOCK"] == "blitz-vps:991"
    assert payload["env"]["HERMES_PROFILE"] == "vps2-eng1"
    assert "github-workflows" in payload["argv"]
    assert "nohup" not in command
    assert " & " not in command
    assert "HERMES_HOME" not in payload["env"]
    assert "HERMES_KANBAN_DB" not in payload["env"]


def test_workspace_mapping_preserves_canonical_relative_path_and_fails_outside_root():
    config = vps2.Vps2WorkerConfig(
        enabled=True, remote_workspace_root="/remote/workspaces"
    )
    assert vps2.remote_workspace_for(
        "/local/workspaces/repo/.worktrees/t_x",
        "/local/workspaces",
        config,
    ) == "/remote/workspaces/repo/.worktrees/t_x"
    with pytest.raises(vps2.RemoteStartError, match="outside"):
        vps2.remote_workspace_for("/other/t_x", "/local/workspaces", config)


def test_ssh_argv_disables_tty_and_keeps_connection_detection():
    config = vps2.Vps2WorkerConfig(enabled=True)
    argv = vps2.build_ssh_argv("exec true", config=config)

    assert argv[:2] == ["ssh", "-T"]
    assert "BatchMode=yes" in argv
    assert "ServerAliveInterval=15" in argv
    assert "ServerAliveCountMax=2" in argv
    assert argv[-2] == "root@vps2"
    assert argv[-1] == "exec true"


def test_remote_start_handshake_accepts_only_live_attached_process():
    marker = b"HERMES_VPS2_STARTED:t_handshake:nonce:7654\n"

    class FakeProc:
        pid = 43210
        stdout = io.BytesIO(marker)

        @staticmethod
        def poll():
            return None

    config = vps2.Vps2WorkerConfig(
        enabled=True,
        start_timeout_seconds=0.2,
        start_grace_seconds=0.01,
    )
    proc = vps2.spawn_vps2_worker_via_ssh(
        task_id="t_handshake",
        board="fleet",
        workspace="/local/workspaces/t_handshake",
        local_workspace_root="/local/workspaces",
        local_env={"PATH": os.environ.get("PATH", "")},
        worker_argv=["-p", "vps2-eng1", "chat", "-q", "work"],
        stderr=subprocess.DEVNULL,
        config=config,
        popen=lambda *_args, **_kwargs: FakeProc(),
        token_factory=lambda: "nonce",
    )

    assert proc.pid == 7654


def test_hermes_originated_readiness_signal_uses_internal_fd(monkeypatch):
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("_HERMES_INTERNAL_KANBAN_READY_FD", str(write_fd))
    monkeypatch.setenv("_HERMES_INTERNAL_KANBAN_READY_TOKEN", "READY:hermes")

    vps2.signal_vps2_worker_ready_from_env()

    assert os.read(read_fd, 1024) == b"READY:hermes\n"
    os.close(read_fd)
    assert "_HERMES_INTERNAL_KANBAN_READY_FD" not in os.environ
    assert "_HERMES_INTERNAL_KANBAN_READY_TOKEN" not in os.environ


def test_loss_before_spawn_terminates_unconfirmed_ssh():
    class FakeProc:
        pid = 43211
        stdout = io.BytesIO(b"")

        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    fake = FakeProc()
    config = vps2.Vps2WorkerConfig(
        enabled=True,
        start_timeout_seconds=0.1,
        start_grace_seconds=0.05,
    )

    with pytest.raises(vps2.RemoteStartError, match="closed before"):
        vps2.spawn_vps2_worker_via_ssh(
            task_id="t_before",
            board="fleet",
            workspace="/local/workspaces/t_case",
            local_workspace_root="/local/workspaces",
            local_env={"PATH": os.environ.get("PATH", "")},
            worker_argv=["-p", "vps2-eng1", "chat", "-q", "work"],
            stderr=subprocess.DEVNULL,
            config=config,
            popen=lambda *_args, **_kwargs: fake,
            token_factory=lambda: "nonce",
        )

    assert fake.terminated is True
    assert fake.poll() is not None


def test_exec_failure_after_marker_is_fail_closed():
    marker = b"HERMES_VPS2_STARTED:t_after:nonce\n"

    class FakeProc:
        pid = 43212
        stdout = io.BytesIO(marker)

        @staticmethod
        def poll():
            return 127

    config = vps2.Vps2WorkerConfig(
        enabled=True,
        start_timeout_seconds=0.1,
        start_grace_seconds=0.01,
    )
    with pytest.raises(vps2.RemoteStartError, match="grace period"):
        vps2.spawn_vps2_worker_via_ssh(
            task_id="t_after",
            board="fleet",
            workspace="/local/workspaces/t_case",
            local_workspace_root="/local/workspaces",
            local_env={"PATH": os.environ.get("PATH", "")},
            worker_argv=["-p", "vps2-eng1", "chat", "-q", "work"],
            stderr=subprocess.DEVNULL,
            config=config,
            popen=lambda *_args, **_kwargs: FakeProc(),
            token_factory=lambda: "nonce",
        )


def test_non_vps2_default_spawn_keeps_local_popen_path(
    isolated_board, monkeypatch, tmp_path
):
    kb = isolated_board
    root = Path(os.environ["HERMES_HOME"])
    profile = root / "profiles" / "alpha"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "local-workspace"
    workspace.mkdir()
    captured = {}

    monkeypatch.setattr(vps2, "configured_vps2_worker", lambda _assignee: None)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    pid = kb._default_spawn(_make_task(kb, assignee="alpha"), str(workspace))

    assert pid == 4242
    assert captured["cmd"][:5] == [
        "hermes",
        "-p",
        "alpha",
        "--cli",
        "--accept-hooks",
    ]
    assert captured["cwd"] == str(workspace)
    assert captured["env"]["HERMES_HOME"] == str(profile)


def test_spawn_target_import_failure_preserves_local_degraded_fallback(
    isolated_board, monkeypatch
):
    kb = isolated_board
    real_import = builtins.__import__

    def fail_optional_spawn_imports(name, *args, **kwargs):
        if name in {"hermes_cli.fleet_vps2_worker", "hermes_cli.profiles"}:
            raise ImportError(f"simulated partial install: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_optional_spawn_imports)

    assert kb._assignee_has_spawn_target("local-profile") is True
    assert kb._assignee_has_spawn_target("vps2-eng1") is False


def test_loss_before_spawn_returns_card_through_canonical_failure_path(
    isolated_board, monkeypatch
):
    kb = isolated_board
    config = vps2.Vps2WorkerConfig(enabled=True)
    monkeypatch.setattr(
        vps2,
        "configured_vps2_worker",
        lambda assignee: config if vps2.is_vps2_assignee(assignee) else None,
    )

    def fail_before_start(**_kwargs):
        raise vps2.RemoteStartError("SSH closed before VPS2 remote-start handshake")

    monkeypatch.setattr(vps2, "spawn_vps2_worker_via_ssh", fail_before_start)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="pre-start loss", assignee="vps2-eng1")
        result = kb.dispatch_once(conn, max_spawn=4, failure_limit=2)
        row = _task_row(conn, task_id)
        runs = kb.list_runs(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert result.spawned == []
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    assert row["worker_pid"] is None
    assert runs[-1].status == "spawn_failed"
    assert runs[-1].outcome == "spawn_failed"
    assert "SSH closed before VPS2" in (runs[-1].error or "")
    assert "spawn_failed" in [event.kind for event in events]


def test_transport_disable_race_never_falls_through_to_local_spawn(
    isolated_board, monkeypatch
):
    kb = isolated_board
    config = vps2.Vps2WorkerConfig(enabled=True)
    calls = 0

    def toggled_config(assignee):
        nonlocal calls
        if not vps2.is_vps2_assignee(assignee):
            return None
        calls += 1
        return config if calls == 1 else None

    monkeypatch.setattr(vps2, "configured_vps2_worker", toggled_config)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("remote-only lane used local Popen"),
    )
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="config race", assignee="vps2-eng1")
        result = kb.dispatch_once(conn, max_spawn=1, failure_limit=2)
        row = _task_row(conn, task_id)
        run = kb.list_runs(conn, task_id)[-1]

    assert result.spawned == []
    assert row["status"] == "ready"
    assert row["worker_pid"] is None
    assert run.outcome == "spawn_failed"
    assert "remote-only assignee" in (run.error or "")


def test_vps2_card_obeys_canonical_active_pr_continuation_guard(
    isolated_board, attached_transport, monkeypatch
):
    kb = isolated_board
    _config, processes = attached_transport
    pr_url = "https://github.com/o269/hermes-agent/pull/15"
    monkeypatch.setattr(
        kb,
        "_default_github_pr_verifier",
        lambda _pr: kb.GitHubPRState(
            canonical_url=pr_url,
            state="OPEN",
            is_draft=True,
            head_sha="a" * 40,
        ),
    )
    monkeypatch.setattr(
        kb,
        "_default_profile_provider_resolver",
        lambda _profile: "openai-codex",
    )
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="continue PR", assignee="vps2-eng1")
        kb.add_comment(conn, task_id, "vps2-eng1", f"Opened {pr_url}")
        result = kb.dispatch_once(conn, max_spawn=4)
        row = _task_row(conn, task_id)

    assert result.spawned == []
    assert (task_id, "active_pr") in result.respawn_guarded
    assert row["status"] == "ready"
    assert row["current_run_id"] is None
    assert processes == []


def test_canonical_dispatch_records_local_ssh_pid_and_one_per_lane(
    isolated_board, attached_transport
):
    kb = isolated_board
    _config, processes = attached_transport
    with kb.connect_closing() as conn:
        first = kb.create_task(
            conn, title="first", assignee="vps2-eng1", priority=100
        )
        same_lane = kb.create_task(
            conn, title="same lane", assignee="vps2-eng1", priority=90
        )
        other_lane = kb.create_task(
            conn, title="other lane", assignee="vps2-eng2", priority=80
        )
        result = kb.dispatch_once(conn, max_spawn=2)
        first_row = _task_row(conn, first)
        same_row = _task_row(conn, same_lane)
        other_row = _task_row(conn, other_lane)
        run_row = conn.execute(
            "SELECT worker_pid, status FROM task_runs WHERE id = ?",
            (first_row["current_run_id"],),
        ).fetchone()
        spawned_event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'spawned'",
            (first,),
        ).fetchone()

    assert [item[0] for item in result.spawned] == [first, other_lane]
    assert result.skipped_per_profile_capped == [(same_lane, "vps2-eng1", 1)]
    assert first_row["status"] == "running"
    assert other_row["status"] == "running"
    assert same_row["status"] == "ready"
    assert first_row["worker_pid"] == processes[0].pid
    assert run_row["worker_pid"] == processes[0].pid
    assert run_row["status"] == "running"
    assert json.loads(spawned_event["payload"])["pid"] == processes[0].pid


def test_healthy_remote_over_ttl_extends_claim_without_duplicate(
    isolated_board, attached_transport
):
    kb = isolated_board
    _config, processes = attached_transport
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="long", assignee="vps2-eng1")
        first = kb.dispatch_once(conn, max_spawn=4)
        assert [item[0] for item in first.spawned] == [task_id]
        original_run = _task_row(conn, task_id)["current_run_id"]
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (int(time.time()) - 1, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                (int(time.time()) - 1, original_run),
            )

        assert kb.release_stale_claims(conn) == 0
        extended = _task_row(conn, task_id)
        second = kb.dispatch_once(conn, max_spawn=4)
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()

    assert extended["status"] == "running"
    assert extended["current_run_id"] == original_run
    assert extended["claim_expires"] > int(time.time())
    assert second.spawned == []
    assert len(processes) == 1
    assert "claim_extended" in [row["kind"] for row in events]


def test_live_remote_owner_suppresses_reconnect_retry_duplicate(
    isolated_board, attached_transport
):
    kb = isolated_board
    _config, processes = attached_transport
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="retry", assignee="vps2-eng1")
        kb.dispatch_once(conn, max_spawn=4)
        run_id = _task_row(conn, task_id)["current_run_id"]
        # Simulate a dispatcher reconnect observing a prematurely restored queue
        # row while the canonical attached SSH process still owns the run.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
                (task_id,),
            )
        result = kb.dispatch_once(conn, max_spawn=4)
        row = _task_row(conn, task_id)

    assert result.spawned == []
    assert (task_id, "live_worker_process") in result.respawn_guarded
    assert row["status"] == "ready"
    assert row["current_run_id"] == run_id
    assert len(processes) == 1


def test_loss_after_spawn_requeues_then_uses_canonical_respawn(
    isolated_board, attached_transport
):
    kb = isolated_board
    _config, processes = attached_transport
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="disconnect", assignee="vps2-eng1")
        kb.dispatch_once(conn, max_spawn=4)
        first_run = _task_row(conn, task_id)["current_run_id"]
        processes[0].terminate()
        processes[0].wait(timeout=5)

        crashed = kb.detect_crashed_workers(conn)
        after_loss = _task_row(conn, task_id)
        respawn = kb.dispatch_once(conn, max_spawn=4)
        after_respawn = _task_row(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert task_id in crashed
    assert after_loss["status"] == "ready"
    assert [item[0] for item in respawn.spawned] == [task_id]
    assert after_respawn["status"] == "running"
    assert after_respawn["current_run_id"] != first_run
    assert len(processes) == 2
    assert [run.outcome for run in runs[:1]] == ["crashed"]
    assert runs[-1].status == "running"


def test_disabled_transport_is_clean_rollback_to_nonspawnable(
    isolated_board, monkeypatch
):
    kb = isolated_board
    monkeypatch.setattr(vps2, "configured_vps2_worker", lambda _assignee: None)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="rollback", assignee="vps2-eng1")
        result = kb.dispatch_once(conn, max_spawn=4)
        row = _task_row(conn, task_id)

    assert result.spawned == []
    assert result.skipped_nonspawnable == [task_id]
    assert tuple(row) == ("ready", None, None, None, None)


@pytest.mark.skipif(os.name == "nt", reason="fleet SSH transport is POSIX-only")
def test_real_fake_ssh_stays_attached_and_disconnect_leaves_no_orphan(
    tmp_path, isolated_board, monkeypatch
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_ssh = bin_dir / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys, threading\n"
        "remote_env = {'PATH': os.environ.get('PATH', '')}\n"
        "remote = subprocess.Popen(\n"
        "    ['/bin/sh', '-c', sys.argv[-1]], stdin=subprocess.PIPE,\n"
        "    stdout=subprocess.PIPE, stderr=None, start_new_session=True, env=remote_env)\n"
        "write_fd = remote.stdin.fileno()\n"
        "gate_r, gate_w = os.pipe()\n"
        "subprocess.Popen(\n"
        "    [sys.executable, '-c',\n"
        "     'import os,sys,time; os.read(int(sys.argv[1]),1); time.sleep(2)',\n"
        "     str(gate_r)],\n"
        "    pass_fds=(write_fd, gate_r), stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=remote_env)\n"
        "os.close(gate_r)\n"
        "def forward():\n"
        "    while True:\n"
        "        data = os.read(0, 4096)\n"
        "        if not data:\n"
        "            return\n"
        "        os.write(write_fd, data)\n"
        "threading.Thread(target=forward, daemon=True).start()\n"
        "while True:\n"
        "    line = remote.stdout.readline()\n"
        "    if not line:\n"
        "        os._exit(remote.wait())\n"
        "    sys.stdout.buffer.write(line)\n"
        "    sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    record_path = tmp_path / "worker.json"
    fake_hermes = bin_dir / "fake-hermes"
    fake_hermes.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        f"record = {str(record_path)!r}\n"
        "payload = {\n"
        "  'pid': os.getpid(),\n"
        "  'argv': sys.argv[1:],\n"
        "  'env': {key: os.environ.get(key) for key in [\n"
        "    'HERMES_HOME', 'HERMES_KANBAN_DB', 'HERMES_KANBAN_BROKER',\n"
        "    'BOARDD_SOCK', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_TASK',\n"
        "    'HERMES_KANBAN_RUN_ID', 'HERMES_KANBAN_CLAIM_LOCK',\n"
        "    'HERMES_KANBAN_WORKSPACE', 'HERMES_PROFILE', 'HERMES_TENANT']}\n"
        "}\n"
        "with open(record, 'w', encoding='utf-8') as handle:\n"
        "    json.dump(payload, handle)\n"
        "ready_fd = int(os.environ.pop('_HERMES_INTERNAL_KANBAN_READY_FD'))\n"
        "ready_token = os.environ.pop('_HERMES_INTERNAL_KANBAN_READY_TOKEN')\n"
        "os.write(ready_fd, (ready_token + '\\n').encode())\n"
        "os.close(ready_fd)\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)

    boardd_path = tmp_path / "boardd.sock"
    boardd_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    boardd_listener.bind(str(boardd_path))
    boardd_listener.listen(1)
    config = vps2.Vps2WorkerConfig(
        enabled=True,
        ssh_host="fake-vps2",
        ssh_user="",
        hermes_bin=str(fake_hermes),
        remote_boardd_sock=str(boardd_path),
        remote_workspace_root=str(tmp_path / "workspaces"),
        remote_log_root=str(tmp_path / "logs"),
        remote_path=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        # Process creation can be slow when the canonical runner executes this
        # integration file beside CPU-heavy Kanban suites. Keep the assertion
        # event-driven and leave enough headroom for a contended CI host.
        start_timeout_seconds=10.0,
        start_grace_seconds=0.05,
        lease_interval_seconds=0.2,
        lease_timeout_seconds=1.0,
    )
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv(
        "PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    monkeypatch.setattr(
        vps2,
        "configured_vps2_worker",
        lambda assignee: config if vps2.is_vps2_assignee(assignee) else None,
    )
    real_spawn = vps2.spawn_vps2_worker_via_ssh
    transports = []

    def capture_spawn(**kwargs):
        kwargs["token_factory"] = lambda: "integration"
        transport = real_spawn(**kwargs)
        transports.append(transport)
        return transport

    monkeypatch.setattr(vps2, "spawn_vps2_worker_via_ssh", capture_spawn)
    kb = isolated_board
    spawned_at = time.monotonic()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="split-host lease", assignee="vps2-eng1", tenant="tenant-a"
        )
        result = kb.dispatch_once(conn, max_spawn=1)
        initial_row = _task_row(conn, task_id)
    assert [item[0] for item in result.spawned] == [task_id]
    proc = transports[0]
    assert initial_row["worker_pid"] == proc.pid
    try:
        deadline = time.monotonic() + 2
        while not record_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        payload = json.loads(record_path.read_text(encoding="utf-8"))

        assert proc.poll() is None
        remote_worker_pid = payload["pid"]
        assert remote_worker_pid != proc.pid
        assert payload["argv"][:2] == ["-p", "vps2-eng1"]
        assert payload["env"]["HERMES_KANBAN_BROKER"] == "1"
        assert payload["env"]["HERMES_KANBAN_RUN_ID"] == str(
            initial_row["current_run_id"]
        )
        assert payload["env"]["HERMES_KANBAN_CLAIM_LOCK"] == initial_row["claim_lock"]
        assert payload["env"]["HERMES_PROFILE"] == "vps2-eng1"
        assert payload["env"]["HERMES_TENANT"] == "tenant-a"
        assert payload["env"]["HERMES_HOME"] is None
        assert payload["env"]["HERMES_KANBAN_DB"] is None
        assert payload["env"]["HERMES_KANBAN_WORKSPACE"].endswith(
            f"/workspaces/{task_id}"
        )

        # Fake SSH exits across an isolated process-session boundary while a
        # holder keeps the remote stdin pipe open, modeling a one-way partition:
        # no EOF/HUP reaches the remote supervisor. The canonical local PID must
        # remain alive through lease expiry, and Hermes must be dead first.
        os.kill(proc.pid, signal.SIGTERM)  # windows-footgun: ok — POSIX-only test
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(proc.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("fake OpenSSH PID did not exit")
        assert proc.poll() is None
        with kb.connect_closing() as conn:
            assert kb.detect_crashed_workers(conn) == []
            rebound_row = _task_row(conn, task_id)
        assert rebound_row["status"] == "running"
        assert rebound_row["worker_pid"] not in (None, proc.pid)

        proc.wait(timeout=10)
        assert proc.poll() is not None
        assert time.monotonic() - spawned_at >= config.lease_timeout_seconds
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(remote_worker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("remote worker survived local SSH lease expiry")
        crashed = []
        final_row = rebound_row
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with kb.connect_closing() as conn:
                crashed.extend(kb.detect_crashed_workers(conn))
                final_row = _task_row(conn, task_id)
            if final_row["status"] == "ready":
                break
            time.sleep(0.05)
        assert task_id in crashed
        assert final_row["status"] == "ready"
        assert final_row["claim_lock"] is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        boardd_listener.close()
