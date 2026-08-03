#!/usr/bin/env python3
"""Mirror an external executor run into Kanban through the boardd broker.

The bridge preserves the task's current assignee whenever ``--assignee`` is
omitted.  Before create or transition writes it applies the authority-lane
policy from :mod:`authority_lane_policy`, then performs a broker readback after
commit so a stale/no-op write cannot be reported as success.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Optional, Sequence

from authority_lane_policy import (
    AuthorityLaneError,
    resolve_transition_assignee,
    validate_assignment,
)

VISIBLE_STATES = {"running", "review", "ready", "blocked", "done", "heartbeat"}


def _unwrap_task(value: Any) -> dict[str, Any]:
    """Normalize boardd task readbacks across legacy/current response shapes."""
    if isinstance(value, dict) and isinstance(value.get("task"), dict):
        value = value["task"]
    if not isinstance(value, dict):
        raise RuntimeError(f"broker returned a non-object task readback: {value!r}")
    return value


def _readback_task(client: Any, task_id: str) -> dict[str, Any]:
    row = _unwrap_task(client.get_task(task_id))
    if row.get("id") != task_id:
        raise RuntimeError(
            f"broker readback id mismatch: expected {task_id!r}, got {row.get('id')!r}"
        )
    return row


def _assert_readback(
    row: dict[str, Any],
    *,
    status: str,
    assignee: Optional[str],
) -> None:
    if row.get("status") != status or row.get("assignee") != assignee:
        raise RuntimeError(
            "broker readback mismatch: "
            f"expected status={status!r} assignee={assignee!r}, "
            f"got status={row.get('status')!r} assignee={row.get('assignee')!r}"
        )


def transition_task(
    client: Any,
    *,
    task_id: str,
    state: str,
    board: str,
    bridge: str,
    worktree: Optional[str] = None,
    branch: Optional[str] = None,
    pid: Optional[int] = None,
    assignee: Optional[str] = None,
    ttl: int = 7200,
    comment: Optional[str] = None,
    result: Optional[str] = None,
    author: str = "orchestrator",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Apply one bridge transition atomically, then verify it by broker readback."""
    if board != "fleet":
        raise ValueError(
            f"authority-lane bridge is fleet-only; refusing board {board!r}"
        )
    if state not in VISIBLE_STATES:
        raise ValueError(f"unsupported state: {state}")
    ts = int(time.time()) if now is None else int(now)
    txn = client.txn_begin()
    committed = False
    try:
        rows = client.txn_exec(
            txn, "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )["rows"]
        if not rows:
            raise ValueError(f"task not found: {task_id}")
        row = rows[0]
        before = row["status"]

        if state == "heartbeat":
            resolved_assignee = row.get("assignee")
            client.txn_exec(
                txn,
                """UPDATE tasks
                   SET last_heartbeat_at = ?, claim_expires = ?,
                       worker_pid = COALESCE(?, worker_pid)
                   WHERE id = ? AND status = 'running'""",
                (ts, ts + ttl, pid, task_id),
            )
            event_kind = "bridge_heartbeat"
            after = before
        else:
            resolved_assignee = resolve_transition_assignee(
                row.get("assignee"),
                assignee,
                title=row.get("title") or "",
                body=row.get("body"),
                operation=f"{state} transition for {task_id}",
                reject_implicit_mixed=state == "review",
            )
            resolved_assignee = validate_assignment(
                title=row.get("title") or "",
                body=row.get("body"),
                assignee=resolved_assignee,
                operation=f"{state} transition for {task_id}",
            )
            if state == "running":
                client.txn_exec(
                    txn,
                    """UPDATE tasks
                       SET status = 'running', assignee = ?,
                           started_at = COALESCE(started_at, ?), completed_at = NULL,
                           workspace_path = COALESCE(?, workspace_path),
                           branch_name = COALESCE(?, branch_name),
                           claim_lock = ?, claim_expires = ?, worker_pid = ?,
                           last_heartbeat_at = ?, current_run_id = NULL,
                           block_kind = NULL, last_failure_error = NULL
                       WHERE id = ?""",
                    (
                        resolved_assignee,
                        ts,
                        worktree,
                        branch,
                        f"acp:{bridge}:{pid or 'external'}",
                        ts + ttl,
                        pid,
                        ts,
                        task_id,
                    ),
                )
                event_kind = "bridge_dispatched"
                after = "running"
            else:
                completed_at = ts if state == "done" else None
                failure = result if state == "blocked" else None
                client.txn_exec(
                    txn,
                    """UPDATE tasks
                       SET status = ?, assignee = ?, completed_at = ?,
                           workspace_path = COALESCE(?, workspace_path),
                           branch_name = COALESCE(?, branch_name),
                           claim_lock = NULL, claim_expires = NULL, worker_pid = NULL,
                           last_heartbeat_at = ?, current_run_id = NULL,
                           block_kind = CASE WHEN ? = 'blocked' THEN 'needs_input' ELSE NULL END,
                           last_failure_error = COALESCE(?, last_failure_error),
                           result = COALESCE(?, result)
                       WHERE id = ?""",
                    (
                        state,
                        resolved_assignee,
                        completed_at,
                        worktree,
                        branch,
                        ts,
                        state,
                        failure,
                        result if state == "done" else None,
                        task_id,
                    ),
                )
                event_kind = {
                    "review": "bridge_completed_review",
                    "ready": "bridge_requeued",
                    "blocked": "bridge_blocked",
                    "done": "bridge_completed",
                }[state]
                after = state

        payload = {
            "from": before,
            "to": after,
            "bridge": bridge,
            "worktree": worktree,
            "branch": branch,
            "pid": pid,
            "result": result,
            "requested_assignee": assignee,
            "resolved_assignee": resolved_assignee,
        }
        client.txn_exec(
            txn,
            "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, ?, ?, ?)",
            (task_id, event_kind, json.dumps(payload, sort_keys=True), ts),
        )
        if comment:
            client.txn_exec(
                txn,
                "INSERT INTO task_comments(task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, author, comment, ts),
            )
            client.txn_exec(
                txn,
                "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'commented', ?, ?)",
                (
                    task_id,
                    json.dumps({"author": author, "len": len(comment)}),
                    ts,
                ),
            )
        client.txn_commit(txn)
        committed = True
    except BaseException:
        if not committed:
            try:
                client.txn_rollback(txn)
            except Exception:
                pass
        raise

    readback = _readback_task(client, task_id)
    _assert_readback(readback, status=after, assignee=resolved_assignee)
    return {
        "task_id": task_id,
        "from": before,
        "to": after,
        "bridge": bridge,
        "pid": pid,
        "assignee": resolved_assignee,
        "readback": {
            "status": readback["status"],
            "assignee": readback.get("assignee"),
        },
    }


def create_task(
    client: Any,
    *,
    title: str,
    body: Optional[str],
    assignee: Optional[str],
    status: str,
    created_by: str,
    priority: int,
    workspace_kind: str,
    task_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create through boardd only after the assignment policy accepts the row."""
    resolved_assignee = validate_assignment(
        title=title,
        body=body,
        assignee=assignee,
        operation="task creation",
    )
    result = client.create_task(
        title=title,
        body=body,
        assignee=resolved_assignee,
        status=status,
        created_by=created_by,
        priority=priority,
        workspace_kind=workspace_kind,
        id=task_id,
    )
    if isinstance(result, dict):
        new_id = result.get("id") or result.get("task_id")
    else:
        new_id = result
    if not isinstance(new_id, str) or not new_id:
        raise RuntimeError(f"broker create returned no task id: {result!r}")
    readback = _readback_task(client, new_id)
    _assert_readback(readback, status=status, assignee=resolved_assignee)
    return {
        "task_id": new_id,
        "status": status,
        "assignee": resolved_assignee,
        "readback": {
            "status": readback["status"],
            "assignee": readback.get("assignee"),
        },
    }


def _install_source_import_path() -> None:
    """Find the installed Hermes checkout without hardcoding one user's home."""
    hermes_home = Path(
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    ).expanduser()
    candidates = (
        hermes_home / "hermes-agent",
        Path.home() / ".hermes" / "hermes-agent",
    )
    for candidate in candidates:
        if (candidate / "hermes_cli" / "kb_client.py").is_file():
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)
            return
    raise RuntimeError(
        "could not locate an installed Hermes checkout containing "
        "hermes_cli/kb_client.py"
    )


def _transition_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transition an external executor task through the boardd broker"
    )
    parser.add_argument("task_id")
    parser.add_argument("state", choices=sorted(VISIBLE_STATES))
    parser.add_argument("--board", default="fleet")
    parser.add_argument("--bridge", required=True)
    parser.add_argument("--worktree")
    parser.add_argument("--branch")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--assignee")
    parser.add_argument("--ttl", type=int, default=7200)
    parser.add_argument("--comment")
    parser.add_argument("--result")
    parser.add_argument("--author", default="orchestrator")
    return parser


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a policy-guarded task through the boardd broker"
    )
    parser.add_argument("create", nargs="?")
    parser.add_argument("--board", default="fleet")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body")
    parser.add_argument("--assignee", required=True)
    parser.add_argument("--status", default="blocked")
    parser.add_argument("--created-by", default="orchestrator")
    parser.add_argument("--priority", type=int, default=0)
    parser.add_argument("--workspace-kind", default="scratch")
    parser.add_argument("--task-id")
    return parser


def main(argv: Optional[Sequence[str]] = None, *, client: Any = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    create_mode = bool(raw and raw[0] == "create")
    args = (_create_parser() if create_mode else _transition_parser()).parse_args(raw)
    if client is None:
        _install_source_import_path()
        from hermes_cli import kb_client

        client = kb_client.get_client()
    try:
        if create_mode:
            if args.board != "fleet":
                raise ValueError(
                    f"authority-lane bridge is fleet-only; refusing board {args.board!r}"
                )
            output = create_task(
                client,
                title=args.title,
                body=args.body,
                assignee=args.assignee,
                status=args.status,
                created_by=args.created_by,
                priority=args.priority,
                workspace_kind=args.workspace_kind,
                task_id=args.task_id,
            )
        else:
            output = transition_task(
                client,
                task_id=args.task_id,
                state=args.state,
                board=args.board,
                bridge=args.bridge,
                worktree=args.worktree,
                branch=args.branch,
                pid=args.pid,
                assignee=args.assignee,
                ttl=args.ttl,
                comment=args.comment,
                result=args.result,
                author=args.author,
            )
    except AuthorityLaneError as exc:
        print(f"authority-lane guard: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
