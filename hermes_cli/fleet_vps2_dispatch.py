#!/usr/bin/env python3
"""CLI entry for blitz-managed VPS2 dumb-executor dispatch (fleet R4).

Called from ``fleet-board-reconciler`` after local dispatch, or standalone::

    python -m hermes_cli.fleet_vps2_dispatch

Environment:
    BOARDD_SOCK          blitz boardd socket (default: ~/.hermes/kanban/boardd-run/boardd.sock)
    VPS2_SSH_HOST        SSH target (default: vps2)
    VPS2_HOST_LOCAL_MAX  concurrent vps2 workers (default: 4)
"""
from __future__ import annotations

import os
import sys

from hermes_cli.fleet_vps2_worker import Vps2WorkerConfig, dispatch_vps2_ready
from hermes_cli.kb_client import Client, DEFAULT_SOCK


def _log(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dry_run = "--dry-run" in argv
    sock = os.environ.get("BOARDD_SOCK", DEFAULT_SOCK)
    if not os.path.exists(sock):
        _log(f"FATAL: boardd socket not found: {sock}")
        return 2

    client = Client(sock_path=sock)
    cfg = Vps2WorkerConfig.from_env()
    if dry_run:
        _log(
            f"DRY-RUN vps2 dispatch host={cfg.dispatch_host} "
            f"ssh={cfg.ssh_user}@{cfg.ssh_host} board={cfg.board}"
        )
        return 0

    result = dispatch_vps2_ready(client, config=cfg, log=_log)
    _log(
        f"PASS vps2 attempted={result.attempted} spawned={result.spawned} "
        f"heartbeated={result.heartbeated} dead_ssh={result.dead_ssh} "
        f"host_local={result.host_local_running}/{cfg.host_local_max} "
        f"global={result.global_running}/{cfg.global_max}"
    )
    print(f"Spawned: {result.spawned}")
    print("Crashed: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
