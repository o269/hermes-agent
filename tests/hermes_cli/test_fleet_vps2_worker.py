"""Tests for blitz-managed VPS2 dumb-executor dispatch (fleet R4)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hermes_cli.fleet_vps2_worker import (
    Vps2WorkerConfig,
    build_remote_worker_command,
    build_ssh_argv,
    dispatch_vps2_ready,
    is_local_pid_alive,
    is_vps2_assignee,
    make_ssh_claim_lock,
    parse_ssh_claim_lock,
    spawn_vps2_worker_via_ssh,
)


def test_is_vps2_assignee():
    assert is_vps2_assignee("vps2-eng1")
    assert is_vps2_assignee("VPS2-gemini1")
    assert not is_vps2_assignee("cursor1")
    assert not is_vps2_assignee(None)
    assert not is_vps2_assignee("")


def test_ssh_claim_lock_roundtrip():
    lock = make_ssh_claim_lock("blitz-vps", "vps2-eng1", 4242)
    assert lock == "blitz-vps:vps2-ssh:vps2-eng1:4242"
    assert parse_ssh_claim_lock(lock) == 4242
    assert parse_ssh_claim_lock("legacy:lock:123") is None
    assert parse_ssh_claim_lock(None) is None


def test_build_remote_worker_command_contains_task_and_profile():
    cfg = Vps2WorkerConfig(
        board="fleet",
        remote_workspace_root="/mnt/ws",
        hermes_bin="/root/.local/bin/hermes",
        remote_boardd_sock="/run/boardd-blitz.sock",
    )
    cmd = build_remote_worker_command(
        task_id="t_82210f15", assignee="vps2-eng1", config=cfg
    )
    assert "t_82210f15" in cmd
    assert "vps2-eng1" in cmd
    assert "BOARDD_SOCK=/run/boardd-blitz.sock" in cmd
    assert "/mnt/ws/t_82210f15" in cmd
    assert "nohup" in cmd


def test_build_ssh_argv():
    cfg = Vps2WorkerConfig(ssh_host="vps2", ssh_user="root")
    argv = build_ssh_argv("echo hi", config=cfg)
    assert argv[:3] == ["ssh", "-o", "BatchMode=yes"]
    assert argv[-2] == "root@vps2"
    assert argv[-1] == "echo hi"


def test_spawn_vps2_worker_via_ssh_returns_local_pid():
    cfg = Vps2WorkerConfig()
    proc = MagicMock()
    proc.pid = 55555
    pid = spawn_vps2_worker_via_ssh(
        task_id="t_deadbeef",
        assignee="vps2-eng2",
        config=cfg,
        popen=MagicMock(return_value=proc),
    )
    assert pid == 55555


def test_spawn_vps2_worker_via_ssh_failure_returns_none():
    cfg = Vps2WorkerConfig()

    def boom(*_a, **_k):
        raise OSError("ssh failed")

    assert (
        spawn_vps2_worker_via_ssh(
            task_id="t_deadbeef",
            assignee="vps2-eng2",
            config=cfg,
            popen=boom,
        )
        is None
    )


def test_is_local_pid_alive_current_process():
    import os

    assert is_local_pid_alive(os.getpid()) is True
    assert is_local_pid_alive(2**30) is False


def test_dispatch_vps2_ready_spawns_and_relocks(monkeypatch, tmp_path):
    client = MagicMock()
    client.query.side_effect = [
        [],  # maintain_ssh_sessions running rows
        [{"n": 0}],  # host_local count
        [{"n": 0}],  # global count
        [
            {
                "id": "t_abc12345",
                "assignee": "vps2-eng1",
                "title": "test",
                "priority": 0,
                "created_at": 1,
            }
        ],
    ]
    client.claim.return_value = {"won": True, "run_id": 99}

    cfg = Vps2WorkerConfig(
        dispatch_host="blitz-test",
        local_workspace_root=str(tmp_path),
        host_local_max=4,
        global_max=20,
    )

    proc = MagicMock()
    proc.pid = 1234
    result = dispatch_vps2_ready(
        client,
        config=cfg,
        popen=MagicMock(return_value=proc),
        log=lambda _m: None,
    )

    assert result.attempted == 1
    assert result.spawned == 1
    assert client.claim.call_count >= 2
    final_claim = client.claim.call_args_list[-1]
    assert final_claim.kwargs["claimer"] == "blitz-test:vps2-ssh:vps2-eng1:1234"
    client.set_workspace_path.assert_called_once()


def test_dispatch_vps2_ready_respects_host_cap():
    client = MagicMock()
    client.query.side_effect = [
        [],
        [{"n": 4}],
        [{"n": 4}],
    ]
    cfg = Vps2WorkerConfig(host_local_max=4, global_max=20)
    result = dispatch_vps2_ready(client, config=cfg, log=lambda _m: None)
    assert result.spawned == 0
    assert result.attempted == 0
    client.claim.assert_not_called()


def test_maintain_ssh_sessions_heartbeats_alive(monkeypatch):
    import os

    from hermes_cli.fleet_vps2_worker import maintain_ssh_sessions

    client = MagicMock()
    lock = make_ssh_claim_lock("blitz", "vps2-eng1", os.getpid())
    client.query.return_value = [
        {"id": "t_alive123", "assignee": "vps2-eng1", "claim_lock": lock}
    ]
    cfg = Vps2WorkerConfig()
    heartbeated, dead = maintain_ssh_sessions(client, config=cfg)
    assert heartbeated == 1
    assert dead == 0
    client.heartbeat.assert_called_once()
