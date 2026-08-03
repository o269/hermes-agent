"""Attached SSH transport for canonical VPS2 Kanban worker spawning.

This module deliberately does not scan, claim, or dispatch cards. The canonical
``hermes_cli.kanban_db.dispatch_once`` policy path owns all of those decisions;
``_default_spawn`` calls this transport only after it has claimed a ``vps2-*``
card and built the normal worker argv/environment.
"""
from __future__ import annotations

import base64
import json
import os
import posixpath
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Callable, Mapping, Optional, Protocol, Sequence, cast


class _AttachedProcess(Protocol):
    """Small Popen surface used by the transport and its deterministic tests."""

    pid: int
    stdout: Optional[BinaryIO]

    def poll(self) -> Optional[int]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: Optional[float] = None) -> int: ...


class _AttachedTransportProxy:
    """Expose the real OpenSSH PID while supervising it through a local parent."""

    def __init__(self, supervisor: _AttachedProcess, ssh_pid: int) -> None:
        self._supervisor = supervisor
        self.pid = ssh_pid
        self.stdout = supervisor.stdout

    def poll(self) -> Optional[int]:
        return self._supervisor.poll()

    def terminate(self) -> None:
        self._supervisor.terminate()

    def kill(self) -> None:
        self._supervisor.kill()

    def wait(self, timeout: Optional[float] = None) -> int:
        return self._supervisor.wait(timeout=timeout)


VPS2_ASSIGNEE_RE = re.compile(r"^vps2-", re.IGNORECASE)
_SAFE_SSH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_READY_FD_ENV = "_HERMES_INTERNAL_KANBAN_READY_FD"
_READY_TOKEN_ENV = "_HERMES_INTERNAL_KANBAN_READY_TOKEN"
_REMOTE_ENV_KEYS = frozenset(
    {
        "HERMES_KANBAN_BRANCH",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_GOAL_MAX_TURNS",
        "HERMES_KANBAN_GOAL_MODE",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_TASK",
        "HERMES_PROFILE",
        "HERMES_TENANT",
        "TERMINAL_MAX_FOREGROUND_TIMEOUT",
        "TERMINAL_TIMEOUT",
    }
)


class RemoteStartError(RuntimeError):
    """The SSH process failed the fail-closed remote-start contract."""


@dataclass(frozen=True)
class Vps2WorkerConfig:
    """Narrow fleet transport contract loaded from ``kanban.vps2_ssh``."""

    enabled: bool = False
    ssh_host: str = "vps2"
    ssh_user: str = "root"
    hermes_bin: str = "/root/.local/bin/hermes"
    remote_boardd_sock: str = "/run/boardd-blitz.sock"
    remote_workspace_root: str = "/mnt/HC_Volume_106418160/fleet-workspaces"
    remote_log_root: str = "/tmp"
    remote_path: str = (
        "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    connect_timeout_seconds: int = 15
    server_alive_interval_seconds: int = 15
    server_alive_count_max: int = 2
    start_timeout_seconds: float = 20.0
    start_grace_seconds: float = 0.1
    lease_interval_seconds: float = 1.0
    lease_timeout_seconds: float = 4.0

    @classmethod
    def from_mapping(cls, value: object) -> "Vps2WorkerConfig":
        raw = value if isinstance(value, Mapping) else {}
        return cls(
            enabled=raw.get("enabled") is True,
            ssh_host=str(raw.get("host", cls.ssh_host)).strip(),
            ssh_user=str(raw.get("user", cls.ssh_user)).strip(),
            hermes_bin=str(raw.get("hermes_bin", cls.hermes_bin)).strip(),
            remote_boardd_sock=str(
                raw.get("boardd_sock", cls.remote_boardd_sock)
            ).strip(),
            remote_workspace_root=str(
                raw.get("workspace_root", cls.remote_workspace_root)
            ).strip(),
            remote_log_root=str(raw.get("log_root", cls.remote_log_root)).strip(),
            remote_path=str(raw.get("path", cls.remote_path)).strip(),
            connect_timeout_seconds=_bounded_int(
                raw.get("connect_timeout_seconds"),
                cls.connect_timeout_seconds,
                minimum=1,
                maximum=300,
            ),
            server_alive_interval_seconds=_bounded_int(
                raw.get("server_alive_interval_seconds"),
                cls.server_alive_interval_seconds,
                minimum=1,
                maximum=300,
            ),
            server_alive_count_max=_bounded_int(
                raw.get("server_alive_count_max"),
                cls.server_alive_count_max,
                minimum=1,
                maximum=10,
            ),
            start_timeout_seconds=_bounded_float(
                raw.get("start_timeout_seconds"),
                cls.start_timeout_seconds,
                minimum=0.1,
                maximum=300.0,
            ),
            start_grace_seconds=_bounded_float(
                raw.get("start_grace_seconds"),
                cls.start_grace_seconds,
                minimum=0.05,
                maximum=5.0,
            ),
            lease_interval_seconds=_bounded_float(
                raw.get("lease_interval_seconds"),
                cls.lease_interval_seconds,
                minimum=0.1,
                maximum=2.0,
            ),
            # Canonical process-tree termination waits five seconds. The local
            # supervisor must outlive the remote lease but exit before that
            # deadline, so the configured lease is deliberately capped at four.
            lease_timeout_seconds=_bounded_float(
                raw.get("lease_timeout_seconds"),
                cls.lease_timeout_seconds,
                minimum=1.0,
                maximum=4.0,
            ),
        )

    def validate(self) -> None:
        if not self.enabled:
            raise RemoteStartError("kanban.vps2_ssh is disabled")
        if not self.ssh_host or not _SAFE_SSH_COMPONENT_RE.fullmatch(self.ssh_host):
            raise RemoteStartError("kanban.vps2_ssh.host is invalid")
        if self.ssh_host.startswith("-"):
            raise RemoteStartError("kanban.vps2_ssh.host must not start with '-'")
        if self.ssh_user and (
            not _SAFE_SSH_COMPONENT_RE.fullmatch(self.ssh_user)
            or self.ssh_user.startswith("-")
            or ":" in self.ssh_user
        ):
            raise RemoteStartError("kanban.vps2_ssh.user is invalid")
        if not self.remote_path or "\x00" in self.remote_path:
            raise RemoteStartError("kanban.vps2_ssh.path must be non-empty")
        for key, path in (
            ("hermes_bin", self.hermes_bin),
            ("boardd_sock", self.remote_boardd_sock),
            ("workspace_root", self.remote_workspace_root),
            ("log_root", self.remote_log_root),
        ):
            if not path or not PurePosixPath(path).is_absolute() or "\x00" in path:
                raise RemoteStartError(
                    f"kanban.vps2_ssh.{key} must be an absolute POSIX path"
                )
        if self.start_timeout_seconds <= 0 or self.start_grace_seconds <= 0:
            raise RemoteStartError("kanban.vps2_ssh start timing must be positive")
        if self.lease_interval_seconds <= 0 or not (
            self.lease_interval_seconds * 2 <= self.lease_timeout_seconds <= 4.0
        ):
            raise RemoteStartError(
                "kanban.vps2_ssh lease timeout must be at least twice the interval and at most 4 seconds"
            )


def _bounded_int(
    value: object,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(str(value)) if value is not None else int(default)
    except (TypeError, ValueError):
        return int(default)
    return min(maximum, max(minimum, parsed))


def _bounded_float(
    value: object,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(str(value)) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)
    return min(maximum, max(minimum, parsed))


def load_vps2_worker_config() -> Vps2WorkerConfig:
    """Load the fleet SSH contract from the dispatcher's ``config.yaml``."""
    try:
        from hermes_cli.config import load_config

        kanban_config = load_config().get("kanban") or {}
    except Exception:
        kanban_config = {}
    raw = (
        kanban_config.get("vps2_ssh", {})
        if isinstance(kanban_config, Mapping)
        else {}
    )
    return Vps2WorkerConfig.from_mapping(raw)


def is_vps2_assignee(assignee: Optional[str]) -> bool:
    return bool(assignee and VPS2_ASSIGNEE_RE.match(assignee))


def configured_vps2_worker(
    assignee: Optional[str],
) -> Optional[Vps2WorkerConfig]:
    """Return enabled VPS2 transport config for ``assignee``, if any."""
    if not is_vps2_assignee(assignee):
        return None
    config = load_vps2_worker_config()
    return config if config.enabled else None


def remote_workspace_for(
    workspace: str,
    local_workspace_root: str,
    config: Vps2WorkerConfig,
) -> str:
    """Map the canonical resolved workspace into the configured remote root.

    The relative path is preserved exactly. Cards whose resolved workspace is
    outside the canonical local root fail closed instead of silently running in
    a different/empty directory on VPS2.
    """
    local_root = posixpath.normpath(str(local_workspace_root))
    local_path = posixpath.normpath(str(workspace))
    if not PurePosixPath(local_root).is_absolute() or not PurePosixPath(
        local_path
    ).is_absolute():
        raise RemoteStartError("canonical local workspace paths must be absolute")
    try:
        common = posixpath.commonpath((local_root, local_path))
    except ValueError as exc:
        raise RemoteStartError("canonical workspace cannot be mapped to VPS2") from exc
    if common != local_root:
        raise RemoteStartError(
            f"canonical workspace is outside HERMES_KANBAN_WORKSPACES_ROOT: {workspace}"
        )
    relative = posixpath.relpath(local_path, local_root)
    if relative in ("", "."):
        raise RemoteStartError("canonical workspace must be below its root")
    return posixpath.normpath(
        posixpath.join(config.remote_workspace_root.rstrip("/"), relative)
    )


def build_remote_worker_env(
    *,
    task_id: str,
    board: str,
    workspace: str,
    local_workspace_root: str,
    local_env: Mapping[str, str],
    config: Vps2WorkerConfig,
) -> dict[str, str]:
    """Return the minimal board/profile payload safe for the remote process."""
    if not _SAFE_TASK_ID_RE.fullmatch(task_id):
        raise RemoteStartError(f"unsafe Kanban task id for remote spawn: {task_id!r}")
    remote_workspace = remote_workspace_for(
        workspace, local_workspace_root, config
    )
    remote_env = {
        key: str(value)
        for key, value in local_env.items()
        if key in _REMOTE_ENV_KEYS and value is not None
    }
    remote_env.update(
        {
            "HERMES_KANBAN_BROKER": "1",
            "BOARDD_SOCK": config.remote_boardd_sock,
            "HERMES_KANBAN_BOARD": board,
            "HERMES_KANBAN_TASK": task_id,
            "HERMES_KANBAN_WORKSPACE": remote_workspace,
            "HERMES_KANBAN_WORKSPACES_ROOT": config.remote_workspace_root,
            "PATH": config.remote_path,
            "TERMINAL_CWD": remote_workspace,
        }
    )
    # The remote profile owns its own HERMES_HOME. A local DB path or profile
    # home is never forwarded across hosts; the broker socket is the board path.
    remote_env.pop("HERMES_HOME", None)
    remote_env.pop("HERMES_KANBAN_DB", None)
    return remote_env


_REMOTE_SUPERVISOR_SCRIPT = r"""
import base64, json, os, select, signal, subprocess, sys, time
p = json.loads(base64.urlsafe_b64decode(sys.argv[1].encode()).decode())
stop = False
def on_signal(_sig, _frame):
    global stop
    stop = True
for name in ("SIGTERM", "SIGINT", "SIGHUP"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), on_signal)
ready_r, ready_w = os.pipe()
env = dict(p["env"])
env[p["ready_fd_env"]] = str(ready_w)
env[p["ready_token_env"]] = p["ready_token"]
log = open(p["log_path"], "ab", buffering=0)
child = subprocess.Popen(
    p["argv"], cwd=p["workspace"], stdin=subprocess.DEVNULL,
    stdout=log, stderr=subprocess.STDOUT, env=env, pass_fds=(ready_w,)
)
os.close(ready_w)
os.set_blocking(ready_r, False)
stdin_fd = sys.stdin.fileno()
os.set_blocking(stdin_fd, False)
ready_buf = b""
lease_buf = b""
ready = False
started = time.monotonic()
lease_deadline = started + float(p["lease_timeout"])
reason = ""
try:
    while True:
        now = time.monotonic()
        rc = child.poll()
        if rc is not None:
            if not ready:
                reason = "Hermes exited before readiness (rc=%s)" % rc
            break
        if stop:
            reason = "remote supervisor received termination signal"
            break
        if not ready and now - started >= float(p["start_timeout"]):
            reason = "timed out waiting for Hermes readiness"
            break
        if now >= lease_deadline:
            reason = "local SSH lease expired"
            break
        readers = [stdin_fd]
        if not ready:
            readers.append(ready_r)
        readable, _, _ = select.select(readers, [], [], 0.1)
        if stdin_fd in readable:
            chunk = os.read(stdin_fd, 4096)
            if not chunk:
                reason = "local SSH lease stream closed"
                break
            lease_buf += chunk
            while b"\n" in lease_buf:
                line, lease_buf = lease_buf.split(b"\n", 1)
                if line.rstrip(b"\r") == p["lease_token"].encode():
                    lease_deadline = time.monotonic() + float(p["lease_timeout"])
        if ready_r in readable:
            chunk = os.read(ready_r, 4096)
            if not chunk:
                reason = "Hermes closed readiness pipe before signaling"
                break
            ready_buf += chunk
            while b"\n" in ready_buf:
                line, ready_buf = ready_buf.split(b"\n", 1)
                if line.rstrip(b"\r") == p["ready_token"].encode():
                    ready = True
                    os.close(ready_r)
                    sys.stdout.write(p["ready_token"] + "\n")
                    sys.stdout.flush()
                    break
finally:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    if reason:
        sys.stderr.write("vps2 remote supervisor: " + reason + "\n")
        sys.stderr.flush()
    try:
        os.close(ready_r)
    except OSError:
        pass
    log.close()
sys.exit(child.returncode if child.returncode is not None else 1)
""".strip()


def _encode_payload(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_payload(value: str) -> dict[str, Any]:
    """Decode an internal supervisor payload (also used by deterministic tests)."""
    return cast(
        dict[str, Any],
        json.loads(base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")),
    )


def build_remote_worker_command(
    *,
    task_id: str,
    board: str,
    workspace: str,
    local_workspace_root: str,
    local_env: Mapping[str, str],
    worker_argv: Sequence[str],
    ready_token: str,
    lease_token: str,
    config: Vps2WorkerConfig,
) -> str:
    """Build a foreground remote supervisor with readiness and lease semantics."""
    config.validate()
    remote_workspace = remote_workspace_for(
        workspace, local_workspace_root, config
    )
    log_path = posixpath.join(
        config.remote_log_root.rstrip("/"), f"kanban-{task_id}.log"
    )
    remote_env = build_remote_worker_env(
        task_id=task_id,
        board=board,
        workspace=workspace,
        local_workspace_root=local_workspace_root,
        local_env=local_env,
        config=config,
    )
    payload = _encode_payload(
        {
            "argv": [config.hermes_bin, *worker_argv],
            "env": remote_env,
            "lease_timeout": config.lease_timeout_seconds,
            "lease_token": lease_token,
            "log_path": log_path,
            "ready_fd_env": _READY_FD_ENV,
            "ready_token": ready_token,
            "ready_token_env": _READY_TOKEN_ENV,
            "start_timeout": config.start_timeout_seconds,
            "workspace": remote_workspace,
        }
    )
    # The shell only performs fail-closed prerequisites, then foreground-execs a
    # supervisor. The supervisor owns Hermes as an attached child, requires a
    # Hermes-originated readiness signal, and kills it if lease pulses stop.
    return " && ".join(
        [
            "umask 077",
            "mkdir -p "
            + " ".join(
                (shlex.quote(remote_workspace), shlex.quote(config.remote_log_root))
            ),
            f"test -x {shlex.quote(config.hermes_bin)}",
            f"test -S {shlex.quote(config.remote_boardd_sock)}",
            "command -v python3 >/dev/null 2>&1",
            "exec python3 -c "
            + shlex.quote(_REMOTE_SUPERVISOR_SCRIPT)
            + " "
            + shlex.quote(payload),
        ]
    )


def build_ssh_argv(
    remote_command: str,
    *,
    config: Vps2WorkerConfig,
) -> list[str]:
    config.validate()
    target = (
        f"{config.ssh_user}@{config.ssh_host}"
        if config.ssh_user
        else config.ssh_host
    )
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={config.connect_timeout_seconds}",
        "-o",
        f"ServerAliveInterval={config.server_alive_interval_seconds}",
        "-o",
        f"ServerAliveCountMax={config.server_alive_count_max}",
        target,
        remote_command,
    ]


def _terminate_failed_start(
    proc: _AttachedProcess,
    *,
    wait_seconds: float = 5.0,
) -> None:
    """Close transport and wait through the remote non-survival lease."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=wait_seconds)
        return
    except (OSError, subprocess.SubprocessError, TimeoutError):
        pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        pass


def _await_remote_start(
    proc: _AttachedProcess,
    *,
    handshake_token: str,
    timeout_seconds: float,
    grace_seconds: float,
    allow_pid_suffix: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Require the exact Hermes-originated marker and a live grace window."""
    if proc.stdout is None:
        raise RemoteStartError("SSH start handshake has no stdout pipe")

    lines: queue.Queue[Optional[bytes]] = queue.Queue()

    def _read_lines(stream: BinaryIO) -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(
        target=_read_lines,
        args=(proc.stdout,),
        name=f"vps2-ssh-start-{proc.pid}",
        daemon=True,
    ).start()

    expected = handshake_token.encode("utf-8")
    deadline = monotonic() + timeout_seconds
    observed_bytes = 0
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RemoteStartError("timed out waiting for VPS2 remote-start handshake")
        try:
            line = lines.get(timeout=min(0.1, remaining))
        except queue.Empty:
            returncode = proc.poll()
            if returncode is not None:
                raise RemoteStartError(
                    f"SSH exited before VPS2 remote-start handshake (rc={returncode})"
                )
            continue
        if line is None:
            returncode = proc.poll()
            raise RemoteStartError(
                f"SSH closed before VPS2 remote-start handshake (rc={returncode})"
            )
        observed_bytes += len(line)
        if observed_bytes > 64 * 1024:
            raise RemoteStartError("SSH pre-handshake output exceeded 64 KiB")
        observed = line.rstrip(b"\r\n")
        if observed == expected or (
            allow_pid_suffix and observed.startswith(expected + b":")
        ):
            matched = observed.decode("utf-8", errors="strict")
            break

    grace_deadline = monotonic() + grace_seconds
    while monotonic() < grace_deadline:
        returncode = proc.poll()
        if returncode is not None:
            raise RemoteStartError(
                f"SSH/remote worker exited during start grace period (rc={returncode})"
            )
        sleep(min(0.02, max(0.0, grace_deadline - monotonic())))
    if proc.poll() is not None:
        raise RemoteStartError("SSH/remote worker did not survive start grace period")
    return matched


def signal_vps2_worker_ready_from_env() -> None:
    """Emit the internal ready token after Hermes has initialized its agent.

    No-op outside the internal VPS2 supervisor contract. A configured signal
    must succeed; otherwise startup fails closed and the remote supervisor kills
    the worker instead of accepting a process that never became usable.
    """
    raw_fd = os.environ.pop(_READY_FD_ENV, "")
    token = os.environ.pop(_READY_TOKEN_ENV, "")
    if not raw_fd and not token:
        return
    if not raw_fd or not token:
        raise RemoteStartError("incomplete internal VPS2 readiness contract")
    try:
        fd = int(raw_fd)
    except ValueError as exc:
        raise RemoteStartError("invalid internal VPS2 readiness descriptor") from exc
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    except OSError as exc:
        raise RemoteStartError("could not signal VPS2 worker readiness") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _run_local_supervisor(encoded_payload: str) -> int:
    """Own one SSH child and keep its local lifecycle token through lease expiry."""
    payload = _decode_payload(encoded_payload)
    ready_token = str(payload["ready_token"])
    lease_token = str(payload["lease_token"])
    lease_interval = float(payload["lease_interval"])
    lease_timeout = float(payload["lease_timeout"])
    stop = threading.Event()

    def _on_signal(_signum, _frame) -> None:
        stop.set()

    for sig_name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, _on_signal)

    ssh = subprocess.Popen(
        [str(part) for part in payload["ssh_argv"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        start_new_session=False,
    )

    def _pulse() -> None:
        line = (lease_token + "\n").encode("utf-8")
        while not stop.is_set() and ssh.poll() is None:
            try:
                assert ssh.stdin is not None
                ssh.stdin.write(line)
                ssh.stdin.flush()
            except (BrokenPipeError, OSError):
                break
            stop.wait(lease_interval)

    pulse_thread = threading.Thread(
        target=_pulse,
        name=f"vps2-lease-{ssh.pid}",
        daemon=True,
    )
    pulse_thread.start()
    exit_code = 1
    try:
        _await_remote_start(
            cast(_AttachedProcess, ssh),
            handshake_token=ready_token,
            timeout_seconds=float(payload["start_timeout"]),
            grace_seconds=0.05,
        )
        sys.stdout.write(f"{ready_token}:{ssh.pid}\n")
        sys.stdout.flush()
        while not stop.is_set() and ssh.poll() is None:
            time.sleep(0.1)
        if ssh.poll() is None:
            ssh.terminate()
        try:
            exit_code = ssh.wait(timeout=1)
        except subprocess.TimeoutExpired:
            ssh.kill()
            exit_code = ssh.wait()
    except Exception as exc:
        sys.stderr.write(f"vps2 local supervisor: {exc}\n")
        sys.stderr.flush()
        if ssh.poll() is None:
            ssh.terminate()
            try:
                ssh.wait(timeout=1)
            except subprocess.TimeoutExpired:
                ssh.kill()
                ssh.wait()
    finally:
        stop.set()
        if ssh.stdin is not None:
            try:
                ssh.stdin.close()
            except OSError:
                pass
        # A one-way partition can make local SSH exit before sshd observes the
        # channel loss. Hold the canonical local PID past the remote lease so
        # dispatch cannot requeue until the remote supervisor has killed Hermes.
        deadline = time.monotonic() + lease_timeout + 0.5
        while time.monotonic() < deadline:
            time.sleep(min(0.1, deadline - time.monotonic()))
    return int(exit_code)


def spawn_vps2_worker_via_ssh(
    *,
    task_id: str,
    board: str,
    workspace: str,
    local_workspace_root: str,
    local_env: Mapping[str, str],
    worker_argv: Sequence[str],
    stderr: object,
    config: Vps2WorkerConfig,
    popen: Callable[..., Any] = subprocess.Popen,
    token_factory: Callable[[], str] = lambda: os.urandom(16).hex(),
) -> _AttachedProcess:
    """Start and verify one local supervisor as the canonical worker identity."""
    nonce = token_factory()
    ready_token = f"HERMES_VPS2_STARTED:{task_id}:{nonce}"
    lease_token = f"HERMES_VPS2_LEASE:{task_id}:{nonce}"
    remote_command = build_remote_worker_command(
        task_id=task_id,
        board=board,
        workspace=workspace,
        local_workspace_root=local_workspace_root,
        local_env=local_env,
        worker_argv=worker_argv,
        ready_token=ready_token,
        lease_token=lease_token,
        config=config,
    )
    ssh_argv = build_ssh_argv(remote_command, config=config)
    supervisor_payload = _encode_payload(
        {
            "lease_interval": config.lease_interval_seconds,
            "lease_timeout": config.lease_timeout_seconds,
            "lease_token": lease_token,
            "ready_token": ready_token,
            "ssh_argv": ssh_argv,
            "start_timeout": config.start_timeout_seconds,
        }
    )
    argv = [
        sys.executable,
        "-m",
        "hermes_cli.fleet_vps2_worker",
        "--local-supervisor",
        supervisor_payload,
    ]
    try:
        proc = cast(
            _AttachedProcess,
            popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
                env=dict(local_env),
                start_new_session=True,
            ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RemoteStartError(f"could not exec VPS2 local supervisor: {exc}") from exc

    try:
        matched = _await_remote_start(
            proc,
            handshake_token=ready_token,
            timeout_seconds=config.start_timeout_seconds + 1.0,
            grace_seconds=config.start_grace_seconds,
            allow_pid_suffix=True,
        )
        try:
            ssh_pid = int(matched.rsplit(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise RemoteStartError(
                "VPS2 local supervisor returned an invalid OpenSSH PID"
            ) from exc
        if ssh_pid <= 0:
            raise RemoteStartError(
                "VPS2 local supervisor returned an invalid OpenSSH PID"
            )
    except Exception:
        _terminate_failed_start(
            proc,
            wait_seconds=config.lease_timeout_seconds + 1.0,
        )
        raise
    return _AttachedTransportProxy(proc, ssh_pid)


def _main(argv: Sequence[str]) -> int:
    if len(argv) == 2 and argv[0] == "--local-supervisor":
        return _run_local_supervisor(argv[1])
    raise SystemExit("fleet_vps2_worker is an internal transport module")


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
