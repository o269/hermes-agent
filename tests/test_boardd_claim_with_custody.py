from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db
from hermes_cli.kb_client import Client


REPO_ROOT = Path(__file__).resolve().parents[1]
BOARDD = REPO_ROOT / "scripts" / "fleet" / "boardd.py"
BLITZ_DISPATCHER = REPO_ROOT / "scripts" / "fleet" / "fleet-board-reconciler"
VPS2_DISPATCHER = REPO_ROOT / "scripts" / "fleet" / "fleet-board-reconciler-vps2"

CLAIM_PROGRAM = """
import json
import sys
from hermes_cli.kb_client import Client
client = Client(sock_path=sys.argv[1], worker_id=sys.argv[4])
result = client.claim_with_custody(
    assignee=sys.argv[2],
    task_id=sys.argv[3] or None,
    host_identity=sys.argv[5] or None,
    ttl_seconds=int(sys.argv[6]),
)
print(json.dumps(result), flush=True)
client.close()
"""

LIVE_CLAIM_PROGRAM = CLAIM_PROGRAM + "\nimport time\ntime.sleep(60)\n"


def _python_env(**updates: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["KB_CLIENT_RETRY_DEADLINE_S"] = "2"
    env["KB_CLIENT_CONNECT_TIMEOUT_S"] = "0.5"
    env.update(updates)
    return env


def _raw_ping(sock_path: Path) -> bool:
    request = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    request.settimeout(0.2)
    try:
        request.connect(str(sock_path))
        request.sendall(b'{"op":"ping"}\n')
        response = b""
        while not response.endswith(b"\n"):
            chunk = request.recv(4096)
            if not chunk:
                return False
            response += chunk
        return bool(json.loads(response).get("ok"))
    except (OSError, ValueError):
        return False
    finally:
        request.close()


@pytest.fixture
def broker(tmp_path: Path):
    db_path = tmp_path / "kanban.db"
    sock_path = tmp_path / "boardd.sock"
    schema_path = tmp_path / "schema.sql"
    schema_path.write_text(kanban_db.SCHEMA_SQL, encoding="utf-8")
    env = _python_env(
        BOARDD_DISKGUARD_MIN_FREE_BYTES="0",
        BOARDD_CHECKPOINT_INTERVAL_S="3600",
        BOARDD_BACKUP_INTERVAL_S="3600",
        BOARDD_HOST_IDENTITIES="blitz,blitz-vps-2",
        BOARDD_HOST_LIMITS="blitz:12,blitz-vps-2:8",
        BOARDD_GLOBAL_MAX="20",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(BOARDD),
            "--db",
            str(db_path),
            "--sock",
            str(sock_path),
            "--schema-sql-file",
            str(schema_path),
            "--log-level",
            "WARNING",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"boardd exited early ({process.returncode}): {stdout}\n{stderr}")
        if sock_path.exists() and _raw_ping(sock_path):
            break
        time.sleep(0.02)
    else:
        process.kill()
        stdout, stderr = process.communicate()
        pytest.fail(f"boardd did not become ready: {stdout}\n{stderr}")

    client = Client(sock_path=str(sock_path), worker_id="pytest-control")
    try:
        yield {"client": client, "sock": sock_path, "process": process, "tmp": tmp_path}
    finally:
        client.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _create_task(
    client: Client,
    task_id: str,
    assignee: str,
    *,
    status: str = "ready",
    priority: int = 0,
) -> None:
    client.create_task(
        id=task_id,
        title=f"Task {task_id}",
        assignee=assignee,
        status=status,
        priority=priority,
    )


def _claim(
    sock_path: Path,
    *,
    identity: str,
    assignee: str,
    task_id: str,
    ttl: int = 7200,
    spoofed_identity: str = "",
    worker_id: str = "claim-test",
) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            CLAIM_PROGRAM,
            str(sock_path),
            assignee,
            task_id,
            worker_id,
            spoofed_identity,
            str(ttl),
        ],
        cwd=REPO_ROOT,
        env=_python_env(FLEET_HOST_IDENTITY=identity),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(completed.stdout)


def _start_live_claim(
    sock_path: Path,
    *,
    identity: str,
    assignee: str,
    task_id: str,
    ttl: int,
) -> tuple[subprocess.Popen, dict]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            LIVE_CLAIM_PROGRAM,
            str(sock_path),
            assignee,
            task_id,
            "live-claim-test",
            "",
            str(ttl),
        ],
        cwd=REPO_ROOT,
        env=_python_env(FLEET_HOST_IDENTITY=identity),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        _, stderr = process.communicate(timeout=5)
        pytest.fail(f"live claim subprocess exited without a result: {stderr}")
    return process, json.loads(line)


def test_parent_dag_violation_rejects_child_claim(broker) -> None:
    client = broker["client"]
    _create_task(client, "t_00000001", "data", status="todo")
    _create_task(client, "t_00000002", "engineer", status="ready")
    client.exec_write(
        "INSERT INTO task_links(parent_id, child_id) VALUES(?, ?)",
        ["t_00000001", "t_00000002"],
    )

    result = _claim(
        broker["sock"],
        identity="blitz",
        assignee="engineer",
        task_id="t_00000002",
    )

    assert result == {"won": False, "reason": "parent_not_done"}
    assert client.get_task("t_00000002")["status"] == "ready"


def test_double_dispatch_and_pid_record_are_cas_fenced(broker) -> None:
    client = broker["client"]
    _create_task(client, "t_00000003", "engineer")

    first = _claim(
        broker["sock"],
        identity="blitz",
        assignee="engineer",
        task_id="t_00000003",
    )
    second = _claim(
        broker["sock"],
        identity="blitz",
        assignee="engineer",
        task_id="t_00000003",
    )

    assert first["won"] is True
    assert second == {"won": False, "reason": "already_claimed"}
    recorded = client.record_worker_pid(
        first["task_id"], first["run_id"], os.getpid(), first["claim_lock"]
    )
    assert recorded == {"ok": True}
    task = client.get_task("t_00000003")
    assert task["worker_pid"] == os.getpid()
    run = client.query("SELECT worker_pid FROM task_runs WHERE id=?", [first["run_id"]])
    assert run == [{"worker_pid": os.getpid()}]
    assert client.query(
        "SELECT COUNT(*) AS n FROM task_runs WHERE task_id=?", ["t_00000003"]
    )[0]["n"] == 1
    assert client.record_worker_pid(
        first["task_id"], first["run_id"], os.getpid(), "stale-lock"
    ) == {"ok": False, "reason": "stale_claim"}


def test_lane_claim_selects_highest_priority_and_allows_only_one_running(broker) -> None:
    client = broker["client"]
    _create_task(client, "t_00000015", "low", priority=1)
    _create_task(client, "t_00000016", "low", priority=99)

    selected = _claim(
        broker["sock"],
        identity="blitz",
        assignee="low",
        task_id="",
    )
    duplicate_lane = _claim(
        broker["sock"],
        identity="blitz",
        assignee="low",
        task_id="",
    )

    assert selected["won"] is True
    assert selected["task_id"] == "t_00000016"
    assert duplicate_lane == {"won": False, "reason": "lane_in_use"}
    assert client.get_task("t_00000015")["status"] == "ready"


def test_stale_dead_pid_reclaims_but_live_pid_does_not(broker) -> None:
    client = broker["client"]
    _create_task(client, "t_00000004", "data")
    expired = _claim(
        broker["sock"],
        identity="blitz",
        assignee="data",
        task_id="t_00000004",
        ttl=-1,
        worker_id="expired-claim",
    )
    assert expired["won"] is True

    reclaimed = _claim(
        broker["sock"],
        identity="blitz",
        assignee="data",
        task_id="t_00000004",
        worker_id="replacement-claim",
    )
    assert reclaimed["won"] is True
    assert reclaimed["run_id"] != expired["run_id"]
    old_run = client.query(
        "SELECT status, outcome FROM task_runs WHERE id=?", [expired["run_id"]]
    )[0]
    assert old_run == {"status": "reclaimed", "outcome": "reclaimed"}

    _create_task(client, "t_00000014", "frontend")
    worker_owned = _claim(
        broker["sock"],
        identity="blitz",
        assignee="frontend",
        task_id="t_00000014",
        ttl=-1,
        worker_id="expired-claimer-live-worker",
    )
    assert worker_owned["won"] is True
    assert client.record_worker_pid(
        worker_owned["task_id"],
        worker_owned["run_id"],
        os.getpid(),
        worker_owned["claim_lock"],
    ) == {"ok": True}
    assert _claim(
        broker["sock"],
        identity="blitz",
        assignee="frontend",
        task_id="t_00000014",
    ) == {"won": False, "reason": "already_claimed"}

    _create_task(client, "t_00000005", "security")
    live_process, live_claim = _start_live_claim(
        broker["sock"],
        identity="blitz",
        assignee="security",
        task_id="t_00000005",
        ttl=-1,
    )
    try:
        assert live_claim["won"] is True
        duplicate = _claim(
            broker["sock"],
            identity="blitz",
            assignee="security",
            task_id="t_00000005",
        )
        assert duplicate == {"won": False, "reason": "already_claimed"}
    finally:
        live_process.terminate()
        live_process.wait(timeout=5)


def test_peer_host_identity_is_recorded_and_spoofing_is_ignored(broker) -> None:
    client = broker["client"]
    _create_task(client, "t_00000006", "engineer")
    _create_task(client, "t_00000007", "vps2-eng1")
    _create_task(client, "t_00000008", "low")

    blitz = _claim(
        broker["sock"],
        identity="blitz",
        assignee="engineer",
        task_id="t_00000006",
        spoofed_identity="blitz-vps-2",
    )
    vps2 = _claim(
        broker["sock"],
        identity="blitz-vps-2",
        assignee="vps2-eng1",
        task_id="t_00000007",
    )
    outsider = _claim(
        broker["sock"],
        identity="not-allowed",
        assignee="low",
        task_id="t_00000008",
    )

    assert blitz["won"] is True
    assert vps2["won"] is True
    assert outsider == {"won": False, "reason": "bad_host_identity"}
    identities = client.query(
        "SELECT id, host_identity, claim_pid, claimed_at FROM tasks "
        "WHERE id IN (?, ?) ORDER BY id",
        ["t_00000006", "t_00000007"],
    )
    assert [row["host_identity"] for row in identities] == ["blitz", "blitz-vps-2"]
    assert all(row["claim_pid"] > 0 for row in identities)
    assert all(row["claimed_at"] > 0 for row in identities)


def test_active_pr_and_invalid_continuation_fail_closed(broker) -> None:
    client = broker["client"]
    _create_task(client, "t_00000009", "data")
    now = int(time.time())
    client.exec_write(
        "INSERT INTO continuation_authorizations("
        "task_id, pr_tuples, reason, authorized_profile, authorized_provider, "
        "authorized_by, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
        ["t_00000009", "not-json", "repair", "data", "openai-codex", "fable", now, now + 600],
    )
    denied = _claim(
        broker["sock"], identity="blitz", assignee="data", task_id="t_00000009"
    )
    assert denied == {"won": False, "reason": "continuation_denied"}

    client.exec_write(
        "UPDATE continuation_authorizations SET revoked_at=? WHERE task_id=?",
        [now, "t_00000009"],
    )
    client.exec_write(
        "INSERT INTO task_pr_ownership("
        "task_id, canonical_url, first_seen_at, last_seen_at, declared) "
        "VALUES(?,?,?,?,1)",
        ["t_00000009", "https://github.com/o269/hermes-agent/pull/1", now, now],
    )
    guarded = _claim(
        broker["sock"], identity="blitz", assignee="data", task_id="t_00000009"
    )
    assert guarded == {"won": False, "reason": "active_pr"}


def test_vps2_outage_is_fatal(tmp_path: Path) -> None:
    bogus_socket = tmp_path / "missing-boardd.sock"
    completed = subprocess.run(
        [sys.executable, str(VPS2_DISPATCHER)],
        cwd=REPO_ROOT,
        env=_python_env(
            BOARDD_SOCK=str(bogus_socket),
            FLEET_DISPATCH_LOG=str(tmp_path / "dispatcher.log"),
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode != 0
    assert "FATAL: vps2 dispatcher cannot reach boardd" in completed.stderr


def test_blitz_dispatcher_spawns_and_records_worker_pid(broker) -> None:
    client = broker["client"]
    _create_task(client, "t_00000017", "engineer")
    fake_hermes = broker["tmp"] / "fake-hermes"
    fake_hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_hermes.chmod(0o755)

    completed = subprocess.run(
        [sys.executable, str(BLITZ_DISPATCHER)],
        cwd=REPO_ROOT,
        env=_python_env(
            BOARDD_SOCK=str(broker["sock"]),
            FLEET_HOST_IDENTITY="blitz",
            HERMES_BIN=str(fake_hermes),
            FLEET_WORKSPACE_ROOT=str(broker["tmp"] / "workspaces"),
            FLEET_DISPATCH_LOG=str(broker["tmp"] / "blitz-dispatch.log"),
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Spawned: 1" in completed.stdout
    task = client.get_task("t_00000017")
    worker_pid = int(task["worker_pid"])
    assert worker_pid > 0
    run = client.query(
        "SELECT worker_pid FROM task_runs WHERE id=?", [task["current_run_id"]]
    )
    assert run == [{"worker_pid": worker_pid}]


def test_split_brain_guard_rejects_live_foreign_worker(broker) -> None:
    client = broker["client"]
    _create_task(client, "t_00000018", "vps2-eng1")
    foreign = _claim(
        broker["sock"],
        identity="blitz-vps-2",
        assignee="vps2-eng1",
        task_id="t_00000018",
    )
    assert foreign["won"] is True
    assert client.record_worker_pid(
        foreign["task_id"], foreign["run_id"], os.getpid(), foreign["claim_lock"]
    ) == {"ok": True}

    completed = subprocess.run(
        [sys.executable, str(BLITZ_DISPATCHER)],
        cwd=REPO_ROOT,
        env=_python_env(
            BOARDD_SOCK=str(broker["sock"]),
            FLEET_HOST_IDENTITY="blitz",
            FLEET_DISPATCH_LOG=str(broker["tmp"] / "split-brain.log"),
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 1
    assert "FATAL: split-brain live foreign worker(s)" in completed.stderr


def test_disabling_vps2_prevents_new_remote_claims_without_rewriting_old_state(
    broker,
) -> None:
    client = broker["client"]
    _create_task(client, "t_00000010", "engineer")
    _create_task(client, "t_00000011", "vps2-eng1")
    assert _claim(
        broker["sock"], identity="blitz", assignee="engineer", task_id="t_00000010"
    )["won"]
    assert _claim(
        broker["sock"],
        identity="blitz-vps-2",
        assignee="vps2-eng1",
        task_id="t_00000011",
    )["won"]

    _create_task(client, "t_00000012", "vps2-eng2")
    disabled = subprocess.run(
        [sys.executable, str(VPS2_DISPATCHER)],
        cwd=REPO_ROOT,
        env=_python_env(
            BOARDD_SOCK=str(broker["sock"]),
            FLEET_VPS2_DISABLED="1",
            FLEET_DISPATCH_LOG=str(broker["tmp"] / "vps2-disabled.log"),
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert disabled.returncode == 0
    assert client.get_task("t_00000011")["status"] == "running"
    assert client.get_task("t_00000012")["status"] == "ready"

    _create_task(client, "t_00000013", "low")
    local_after_disable = _claim(
        broker["sock"], identity="blitz", assignee="low", task_id="t_00000013"
    )
    assert local_after_disable["won"] is True
    newly_running = client.query(
        "SELECT id, host_identity FROM tasks WHERE id IN (?, ?) AND status='running'",
        ["t_00000012", "t_00000013"],
    )
    assert newly_running == [{"id": "t_00000013", "host_identity": "blitz"}]
