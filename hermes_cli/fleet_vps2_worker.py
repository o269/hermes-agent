"""Blitz-managed VPS2 dumb executor dispatch (fleet R4).

VPS2 is a dumb executor: blitz is the sole dispatcher. When a ready card is
assigned to a ``vps2-*`` lane, blitz claims it on the broker, SSH-spawns
``hermes chat`` on vps2, and records the *local* SSH session PID for liveness.
The remote worker talks to the board through the existing reverse-tunnel socket;
no remote dispatcher, custody RPC, or cross-host PID comparison is required.
"""
from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

from hermes_cli.kb_client import BoarddError, Client

VPS2_ASSIGNEE_RE = re.compile(r"^vps2-", re.IGNORECASE)
# claim_lock: <dispatch_host>:vps2-ssh:<assignee>:<local_ssh_pid>
SSH_CLAIM_LOCK_RE = re.compile(
    r"^(?P<host>[^:]+):vps2-ssh:(?P<assignee>[^:]+):(?P<pid>\d+)$"
)


@dataclass(frozen=True)
class Vps2WorkerConfig:
    board: str = "fleet"
    dispatch_host: str = socket.gethostname()
    ssh_host: str = "vps2"
    ssh_user: str = "root"
    hermes_bin: str = "/root/.local/bin/hermes"
    remote_boardd_sock: str = "/run/boardd-blitz.sock"
    remote_workspace_root: str = "/mnt/HC_Volume_106418160/fleet-workspaces"
    local_workspace_root: str = "/home/odai/.hermes/kanban/boards/fleet/workspaces"
    claim_ttl_seconds: int = 5400
    host_local_max: int = 4
    global_max: int = 20
    control_assignees: frozenset[str] = frozenset(
        {"security", "fable", "orion-cc", "orion-research"}
    )

    @classmethod
    def from_env(cls) -> "Vps2WorkerConfig":
        control = os.environ.get(
            "VPS2_CONTROL_ASSIGNEES",
            "security,fable,orion-cc,orion-research",
        )
        return cls(
            board=os.environ.get("VPS2_BOARD", os.environ.get("BLITZ_BOARD", "fleet")),
            dispatch_host=os.environ.get(
                "VPS2_DISPATCH_HOST", socket.gethostname()
            ),
            ssh_host=os.environ.get("VPS2_SSH_HOST", "vps2"),
            ssh_user=os.environ.get("VPS2_SSH_USER", "root"),
            hermes_bin=os.environ.get(
                "VPS2_REMOTE_HERMES_BIN", "/root/.local/bin/hermes"
            ),
            remote_boardd_sock=os.environ.get(
                "VPS2_REMOTE_BOARDD_SOCK", "/run/boardd-blitz.sock"
            ),
            remote_workspace_root=os.environ.get(
                "VPS2_REMOTE_WORKSPACE_ROOT",
                "/mnt/HC_Volume_106418160/fleet-workspaces",
            ),
            local_workspace_root=os.environ.get(
                "BLITZ_WORKSPACE_ROOT",
                "/home/odai/.hermes/kanban/boards/fleet/workspaces",
            ),
            claim_ttl_seconds=int(
                os.environ.get("VPS2_CLAIM_TTL_SECONDS", "5400")
            ),
            host_local_max=int(os.environ.get("VPS2_HOST_LOCAL_MAX", "4")),
            global_max=int(os.environ.get("VPS2_GLOBAL_MAX", "20")),
            control_assignees=frozenset(
                a.strip().lower()
                for a in control.split(",")
                if a.strip()
            ),
        )


def is_vps2_assignee(assignee: Optional[str]) -> bool:
    return bool(assignee and VPS2_ASSIGNEE_RE.match(assignee))


def make_ssh_claim_lock(
    dispatch_host: str, assignee: str, local_ssh_pid: int
) -> str:
    return f"{dispatch_host}:vps2-ssh:{assignee}:{local_ssh_pid}"


def parse_ssh_claim_lock(lock: Optional[str]) -> Optional[int]:
    if not lock:
        return None
    match = SSH_CLAIM_LOCK_RE.match(lock)
    if not match:
        return None
    return int(match.group("pid"))


def is_local_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # windows-footgun: ok
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def build_remote_worker_command(
    *,
    task_id: str,
    assignee: str,
    config: Vps2WorkerConfig,
) -> str:
    """Shell command executed on vps2 (backgrounded remotely)."""
    ws = os.path.join(config.remote_workspace_root, task_id)
    log_path = f"/tmp/kanban-vps2-{task_id}.log"
    remote_env = (
        f"HERMES_KANBAN_BROKER=1 "
        f"BOARDD_SOCK={shlex.quote(config.remote_boardd_sock)} "
        f"HERMES_KANBAN_BOARD={shlex.quote(config.board)} "
        f"HERMES_KANBAN_TASK={shlex.quote(task_id)} "
        f"HERMES_KANBAN_WORKSPACE={shlex.quote(ws)} "
        f"HERMES_KANBAN_WORKSPACES_ROOT={shlex.quote(config.remote_workspace_root)}"
    )
    hermes_cmd = (
        f"{shlex.quote(config.hermes_bin)} -p {shlex.quote(assignee)} "
        f"--cli --accept-hooks chat -q {shlex.quote(f'work kanban task {task_id}')}"
    )
    return (
        f"mkdir -p {shlex.quote(ws)} && "
        f"{remote_env} nohup {hermes_cmd} "
        f"</dev/null >>{shlex.quote(log_path)} 2>&1 &"
    )


def build_ssh_argv(
    remote_command: str, *, config: Vps2WorkerConfig
) -> List[str]:
    target = (
        f"{config.ssh_user}@{config.ssh_host}"
        if config.ssh_user
        else config.ssh_host
    )
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        target,
        remote_command,
    ]


def spawn_vps2_worker_via_ssh(
    *,
    task_id: str,
    assignee: str,
    config: Vps2WorkerConfig,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> Optional[int]:
    """SSH-spawn a vps2 worker; return the local SSH session PID."""
    remote_cmd = build_remote_worker_command(
        task_id=task_id, assignee=assignee, config=config
    )
    argv = build_ssh_argv(remote_cmd, config=config)
    try:
        proc = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc.pid
    except (OSError, subprocess.SubprocessError):
        return None


def _count_running(
    client: Client,
    *,
    assignee_allowlist: Optional[Sequence[str]] = None,
    exclude_control: bool = True,
    config: Vps2WorkerConfig,
) -> int:
    conditions = ["status='running'"]
    params: list = []
    if exclude_control and config.control_assignees:
        placeholders = ",".join("?" * len(config.control_assignees))
        conditions.append(
            f"(assignee IS NULL OR lower(assignee) NOT IN ({placeholders}))"
        )
        params.extend(config.control_assignees)
    if assignee_allowlist:
        placeholders = ",".join("?" * len(assignee_allowlist))
        conditions.append(f"assignee IN ({placeholders})")
        params.extend(assignee_allowlist)
    sql = "SELECT COUNT(*) AS n FROM tasks WHERE " + " AND ".join(conditions)
    rows = client.query(sql, params)
    return int(rows[0]["n"]) if rows else 0


def _find_ready_vps2_candidates(client: Client) -> List[dict]:
    rows = client.query(
        "SELECT id, assignee, title, priority, created_at FROM tasks "
        "WHERE status='ready' AND claim_lock IS NULL "
        "AND assignee LIKE 'vps2-%' "
        "ORDER BY priority DESC, created_at ASC"
    )
    return [r for r in rows if is_vps2_assignee(r.get("assignee"))]


@dataclass
class Vps2DispatchResult:
    attempted: int = 0
    spawned: int = 0
    heartbeated: int = 0
    dead_ssh: int = 0
    host_local_running: int = 0
    global_running: int = 0


def maintain_ssh_sessions(
    client: Client,
    *,
    config: Vps2WorkerConfig,
    log: Optional[Callable[[str], None]] = None,
) -> tuple[int, int]:
    """Heartbeat running vps2 cards whose local SSH session is still alive."""
    _log = log or (lambda _msg: None)
    heartbeated = 0
    dead = 0
    rows = client.query(
        "SELECT id, assignee, claim_lock FROM tasks "
        "WHERE status='running' AND assignee LIKE 'vps2-%'"
    )
    for row in rows:
        task_id = row["id"]
        ssh_pid = parse_ssh_claim_lock(row.get("claim_lock"))
        if ssh_pid is None:
            continue
        if is_local_pid_alive(ssh_pid):
            try:
                client.heartbeat(
                    task_id,
                    note=f"vps2-ssh alive local_pid={ssh_pid}",
                )
                heartbeated += 1
            except BoarddError as exc:
                _log(f"HEARTBEAT-FAIL {task_id} {exc}")
        else:
            _log(f"VPS2-SSH-DEAD {task_id} local_ssh_pid={ssh_pid}")
            dead += 1
    return heartbeated, dead


def dispatch_vps2_ready(
    client: Client,
    *,
    config: Optional[Vps2WorkerConfig] = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    log: Optional[Callable[[str], None]] = None,
) -> Vps2DispatchResult:
    """Claim and SSH-spawn ready vps2-* cards from blitz."""
    cfg = config or Vps2WorkerConfig.from_env()
    _log = log or (lambda _msg: None)
    result = Vps2DispatchResult()

    heartbeated, dead = maintain_ssh_sessions(client, config=cfg, log=_log)
    result.heartbeated = heartbeated
    result.dead_ssh = dead

    rows = client.query(
        "SELECT COUNT(*) AS n FROM tasks WHERE status='running' "
        "AND assignee LIKE 'vps2-%'"
    )
    result.host_local_running = int(rows[0]["n"]) if rows else 0
    result.global_running = _count_running(
        client, exclude_control=True, config=cfg
    )

    if result.host_local_running >= cfg.host_local_max:
        _log("VPS2-HOST-LOCAL-CAP-REACHED")
        return result
    if result.global_running >= cfg.global_max:
        _log("VPS2-GLOBAL-CAP-REACHED")
        return result

    candidates = _find_ready_vps2_candidates(client)
    if not candidates:
        _log("VPS2-NO-READY-CANDIDATES")
        return result

    host_remaining = cfg.host_local_max - result.host_local_running
    global_remaining = cfg.global_max - result.global_running
    budget = min(host_remaining, global_remaining, len(candidates))
    if budget <= 0:
        _log("VPS2-CLAIM-BUDGET-ZERO")
        return result

    for row in candidates[:budget]:
        task_id = row["id"]
        assignee = row["assignee"]
        result.attempted += 1
        provisional_lock = make_ssh_claim_lock(
            cfg.dispatch_host, assignee, os.getpid()
        )
        try:
            claim = client.claim(
                task_id, claimer=provisional_lock, ttl_seconds=cfg.claim_ttl_seconds
            )
        except BoarddError as exc:
            _log(f"VPS2-CLAIM-FAIL {task_id} {exc}")
            continue
        if not claim.get("won"):
            _log(f"VPS2-CLAIM-LOST {task_id}")
            continue

        local_ws = os.path.join(cfg.local_workspace_root, task_id)
        try:
            os.makedirs(local_ws, exist_ok=True)
            client.set_workspace_path(task_id, local_ws)
        except (OSError, BoarddError) as exc:
            _log(f"VPS2-WORKSPACE-FAIL {task_id} {exc}")

        ssh_pid = spawn_vps2_worker_via_ssh(
            task_id=task_id,
            assignee=assignee,
            config=cfg,
            popen=popen,
        )
        if ssh_pid is None:
            _log(f"VPS2-SPAWN-FAIL {task_id}")
            try:
                client.claim(
                    task_id, claimer=provisional_lock, ttl_seconds=60
                )
            except BoarddError:
                pass
            continue

        real_lock = make_ssh_claim_lock(cfg.dispatch_host, assignee, ssh_pid)
        try:
            client.claim(
                task_id, claimer=real_lock, ttl_seconds=cfg.claim_ttl_seconds
            )
            _log(f"VPS2-SPAWN {task_id} -> {assignee} local_ssh_pid={ssh_pid}")
            result.spawned += 1
        except BoarddError as exc:
            _log(f"VPS2-RELOCK-FAIL {task_id} {exc}")

        result.host_local_running += 1
        result.global_running += 1
        if (
            result.host_local_running >= cfg.host_local_max
            or result.global_running >= cfg.global_max
        ):
            break

    return result
