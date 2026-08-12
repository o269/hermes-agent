#!/usr/bin/env python3
"""Fail-closed reaper for old direct-child workspace directories.

The board broker and process cwd scan are both authoritative safety inputs.  Any
failure to read either input prevents deletion.  Dry-run is the default; callers
must pass ``--apply`` to remove an eligible workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

DEFAULT_RETENTION_HOURS = 168.0
DEFAULT_MAX_LIVE_WORKSPACES = 10_000
DEFAULT_MAX_PROCESSES = 100_000
EXIT_SAFETY_FAILURE = 2


class SafetyError(RuntimeError):
    """A safety input or predicate could not be established."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Candidate:
    name: str
    path: Path


@dataclass(frozen=True)
class CleanupRoot:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class SafetySnapshot:
    running_workspaces: tuple[Path, ...]
    process_cwds: tuple[Path, ...]


@dataclass(frozen=True)
class Evaluation:
    decision: str
    reason_codes: tuple[str, ...]
    mtime_ns: int | None = None
    device: int | None = None
    inode: int | None = None


class BoardWorkspaceSource:
    """Read all running workspace paths through the public board broker client."""

    def __init__(self, client: Any | None = None, *, max_workspaces: int):
        if max_workspaces < 1:
            raise ValueError("max_workspaces must be positive")
        self.max_workspaces = max_workspaces
        self._owns_client = client is None
        if client is None:
            repo_root = Path(__file__).resolve().parents[2]
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            from hermes_cli.kb_client import Client

            client = Client()
        self.client = client

    def close(self) -> None:
        if self._owns_client:
            close = getattr(self.client, "close", None)
            if close is not None:
                close()

    def discover(self) -> tuple[Path, ...]:
        try:
            summary_rows = self.client.query(
                """
                SELECT COUNT(*) AS total_tasks,
                       COALESCE(SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END), 0)
                           AS running_tasks,
                       COALESCE(SUM(CASE
                           WHEN status = 'running'
                            AND workspace_path IS NOT NULL
                            AND TRIM(workspace_path) != ''
                           THEN 1 ELSE 0 END), 0) AS live_workspaces
                  FROM tasks
                """,
                max_rows=1,
            )
        except Exception as exc:
            raise SafetyError("board_unavailable") from exc

        if not isinstance(summary_rows, list) or len(summary_rows) != 1:
            raise SafetyError("board_summary_malformed")
        summary = summary_rows[0]
        if not isinstance(summary, dict):
            raise SafetyError("board_summary_malformed")
        total_tasks = _strict_nonnegative_int(summary.get("total_tasks"))
        running_tasks = _strict_nonnegative_int(summary.get("running_tasks"))
        live_count = _strict_nonnegative_int(summary.get("live_workspaces"))
        if total_tasks is None or running_tasks is None or live_count is None:
            raise SafetyError("board_summary_malformed")
        if total_tasks == 0:
            raise SafetyError("board_empty")
        if not (live_count <= running_tasks <= total_tasks):
            raise SafetyError("board_summary_malformed")
        if live_count > self.max_workspaces:
            raise SafetyError("board_workspace_bound_exceeded")

        try:
            rows = self.client.query(
                """
                SELECT id, workspace_path
                  FROM tasks
                 WHERE status = 'running'
                   AND workspace_path IS NOT NULL
                   AND TRIM(workspace_path) != ''
                 ORDER BY id
                 LIMIT ?
                """,
                [self.max_workspaces + 1],
                max_rows=self.max_workspaces + 1,
            )
        except Exception as exc:
            raise SafetyError("board_unavailable") from exc

        if not isinstance(rows, list):
            raise SafetyError("board_rows_malformed")
        if len(rows) > self.max_workspaces:
            raise SafetyError("board_workspace_bound_exceeded")
        if len(rows) != live_count:
            raise SafetyError("board_snapshot_inconsistent")

        task_ids: set[str] = set()
        paths: list[Path] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SafetyError("board_rows_malformed")
            task_id = row.get("id")
            raw_path = row.get("workspace_path")
            if not isinstance(task_id, str) or not task_id.strip():
                raise SafetyError("board_rows_malformed")
            if task_id in task_ids:
                raise SafetyError("board_rows_malformed")
            task_ids.add(task_id)
            paths.append(_canonical_absolute_path(raw_path, "board_rows_malformed"))
        return tuple(paths)


def _strict_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _canonical_absolute_path(value: Any, error_code: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SafetyError(error_code)
    try:
        path = Path(value)
        if not path.is_absolute():
            raise SafetyError(error_code)
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SafetyError(error_code) from exc


def paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either canonical path contains the other by component."""

    return left == right or left in right.parents or right in left.parents


def discover_process_cwds(proc_root: Path, *, max_processes: int) -> tuple[Path, ...]:
    if max_processes < 1:
        raise ValueError("max_processes must be positive")
    try:
        if not proc_root.is_absolute() or proc_root.is_symlink() or not proc_root.is_dir():
            raise SafetyError("proc_root_invalid")
        with os.scandir(proc_root) as entries:
            pid_names = sorted(entry.name for entry in entries if entry.name.isdecimal())
    except SafetyError:
        raise
    except OSError as exc:
        raise SafetyError("proc_unavailable") from exc

    if not pid_names:
        raise SafetyError("proc_empty")
    if len(pid_names) > max_processes:
        raise SafetyError("proc_bound_exceeded")

    cwds: list[Path] = []
    for pid_name in pid_names:
        pid_dir = proc_root / pid_name
        cwd_link = pid_dir / "cwd"
        try:
            target = os.readlink(cwd_link)
        except FileNotFoundError as exc:
            # A process that disappeared during enumeration is harmless.  If the
            # pid directory remains, its cwd evidence is unreadable and deletion
            # must fail closed.
            if not pid_dir.exists():
                continue
            raise SafetyError("proc_evidence_unreadable") from exc
        except OSError as exc:
            raise SafetyError("proc_evidence_unreadable") from exc
        if target.endswith(" (deleted)"):
            target = target[: -len(" (deleted)")]
        cwds.append(_canonical_absolute_path(target, "proc_evidence_malformed"))
    return tuple(cwds)


def _validate_root(root: Path) -> CleanupRoot:
    if not root.is_absolute():
        raise SafetyError("root_not_absolute")
    try:
        root_lstat = root.lstat()
    except OSError as exc:
        raise SafetyError("root_unavailable") from exc
    if stat.S_ISLNK(root_lstat.st_mode):
        raise SafetyError("root_is_symlink")
    if not stat.S_ISDIR(root_lstat.st_mode):
        raise SafetyError("root_not_directory")
    try:
        canonical = root.resolve(strict=True)
        canonical_stat = canonical.lstat()
    except (OSError, RuntimeError) as exc:
        raise SafetyError("root_unavailable") from exc
    if not stat.S_ISDIR(canonical_stat.st_mode):
        raise SafetyError("root_not_directory")
    if (root_lstat.st_dev, root_lstat.st_ino) != (
        canonical_stat.st_dev,
        canonical_stat.st_ino,
    ):
        raise SafetyError("root_changed")
    return CleanupRoot(canonical, canonical_stat.st_dev, canonical_stat.st_ino)


def _root_identity_matches(root: CleanupRoot, root_fd: int) -> bool:
    try:
        path_stat = root.path.lstat()
        fd_stat = os.fstat(root_fd)
    except OSError:
        return False
    expected = (root.device, root.inode)
    return (
        stat.S_ISDIR(path_stat.st_mode)
        and stat.S_ISDIR(fd_stat.st_mode)
        and (path_stat.st_dev, path_stat.st_ino) == expected
        and (fd_stat.st_dev, fd_stat.st_ino) == expected
    )


def enumerate_candidates(root: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    is_symlink = entry.is_symlink()
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError as exc:
                    raise SafetyError("candidate_enumeration_failed") from exc
                if is_symlink or is_directory:
                    candidates.append(Candidate(name=entry.name, path=root / entry.name))
    except SafetyError:
        raise
    except OSError as exc:
        raise SafetyError("candidate_enumeration_failed") from exc
    return sorted(candidates, key=lambda candidate: candidate.name)


def evaluate_candidate(
    candidate: Candidate,
    *,
    root: Path,
    snapshot: SafetySnapshot,
    retention_ns: int,
    now_ns: int,
) -> Evaluation:
    if Path(candidate.name).name != candidate.name or os.sep in candidate.name:
        return Evaluation("kept", ("not_direct_child",))
    expected_path = root / candidate.name
    if candidate.path != expected_path:
        return Evaluation("kept", ("not_direct_child",))
    try:
        candidate_stat = candidate.path.lstat()
    except FileNotFoundError:
        return Evaluation("kept", ("candidate_missing",))
    except OSError:
        return Evaluation("kept", ("candidate_unreadable",))
    if stat.S_ISLNK(candidate_stat.st_mode):
        return Evaluation(
            "kept",
            ("symlink",),
            candidate_stat.st_mtime_ns,
            candidate_stat.st_dev,
            candidate_stat.st_ino,
        )
    if not stat.S_ISDIR(candidate_stat.st_mode):
        return Evaluation(
            "kept",
            ("not_directory",),
            candidate_stat.st_mtime_ns,
            candidate_stat.st_dev,
            candidate_stat.st_ino,
        )
    try:
        canonical = candidate.path.resolve(strict=True)
    except (OSError, RuntimeError):
        return Evaluation("kept", ("candidate_unresolvable",), candidate_stat.st_mtime_ns)
    if canonical.parent != root or canonical == root:
        return Evaluation("kept", ("root_escape",), candidate_stat.st_mtime_ns)

    reason_codes: list[str] = []
    if now_ns - candidate_stat.st_mtime_ns < retention_ns:
        reason_codes.append("retention_active")
    else:
        reason_codes.append("retention_elapsed")
    if any(paths_overlap(canonical, workspace) for workspace in snapshot.running_workspaces):
        reason_codes.append("running_workspace_overlap")
    if any(paths_overlap(canonical, cwd) for cwd in snapshot.process_cwds):
        reason_codes.append("process_cwd_overlap")

    blocked = any(code != "retention_elapsed" for code in reason_codes)
    return Evaluation(
        "kept" if blocked else "eligible",
        tuple(reason_codes),
        candidate_stat.st_mtime_ns,
        candidate_stat.st_dev,
        candidate_stat.st_ino,
    )


def _snapshot(
    board_source: BoardWorkspaceSource,
    proc_root: Path,
    *,
    max_processes: int,
) -> SafetySnapshot:
    running_workspaces = board_source.discover()
    process_cwds = discover_process_cwds(proc_root, max_processes=max_processes)
    return SafetySnapshot(running_workspaces, process_cwds)


def _candidate_receipt(candidate: Candidate, evaluation: Evaluation) -> dict[str, Any]:
    return {
        "decision": evaluation.decision,
        "mtime_ns": evaluation.mtime_ns,
        "name": candidate.name,
        "path": str(candidate.path),
        "reason_codes": list(evaluation.reason_codes),
    }


def _keep_pending(receipt_rows: Sequence[dict[str, Any]], reason_code: str) -> None:
    for row in receipt_rows:
        if row["decision"] == "eligible":
            row["decision"] = "kept"
            row["reason_codes"].append(reason_code)


def _error_receipt(
    *,
    apply: bool,
    root: Path,
    retention_seconds: float,
    code: str,
    candidates: Sequence[Candidate] = (),
) -> dict[str, Any]:
    return {
        "apply": apply,
        "candidates": [
            {
                "decision": "kept",
                "mtime_ns": None,
                "name": candidate.name,
                "path": str(candidate.path),
                "reason_codes": ["safety_discovery_failed"],
            }
            for candidate in candidates
        ],
        "error": {"code": code},
        "retention_seconds": retention_seconds,
        "root": str(root),
        "status": "safety_failure",
    }


def run_reaper(
    root: Path,
    *,
    apply: bool = False,
    retention_seconds: float = DEFAULT_RETENTION_HOURS * 3600,
    board_client: Any | None = None,
    proc_root: Path = Path("/proc"),
    max_live_workspaces: int = DEFAULT_MAX_LIVE_WORKSPACES,
    max_processes: int = DEFAULT_MAX_PROCESSES,
) -> tuple[int, dict[str, Any]]:
    """Evaluate and optionally delete old direct-child workspaces."""

    if retention_seconds < 0:
        raise ValueError("retention_seconds must be nonnegative")
    board_source = BoardWorkspaceSource(
        board_client,
        max_workspaces=max_live_workspaces,
    )
    candidates: list[Candidate] = []
    try:
        try:
            validated_root = _validate_root(root)
            candidates = enumerate_candidates(validated_root.path)
            initial_snapshot = _snapshot(
                board_source,
                proc_root,
                max_processes=max_processes,
            )
        except SafetyError as exc:
            return EXIT_SAFETY_FAILURE, _error_receipt(
                apply=apply,
                root=root,
                retention_seconds=retention_seconds,
                code=exc.code,
                candidates=candidates,
            )

        retention_ns = int(retention_seconds * 1_000_000_000)
        evaluations = [
            evaluate_candidate(
                candidate,
                root=validated_root.path,
                snapshot=initial_snapshot,
                retention_ns=retention_ns,
                now_ns=time.time_ns(),
            )
            for candidate in candidates
        ]
        receipt_rows = [
            _candidate_receipt(candidate, evaluation)
            for candidate, evaluation in zip(candidates, evaluations, strict=True)
        ]
        receipt: dict[str, Any] = {
            "apply": apply,
            "candidates": receipt_rows,
            "error": None,
            "retention_seconds": retention_seconds,
            "root": str(validated_root.path),
            "status": "ok",
        }
        if not apply:
            for row in receipt_rows:
                if row["decision"] == "eligible":
                    row["decision"] = "would_delete"
            return 0, receipt

        if not hasattr(os, "O_NOFOLLOW") or not shutil.rmtree.avoids_symlink_attacks:
            receipt["status"] = "safety_failure"
            receipt["error"] = {"code": "unsafe_rmtree_platform"}
            _keep_pending(receipt_rows, "unsafe_rmtree_platform")
            return EXIT_SAFETY_FAILURE, receipt
        root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
        try:
            root_fd = os.open(validated_root.path, root_flags)
        except OSError:
            receipt["status"] = "safety_failure"
            receipt["error"] = {"code": "root_fd_unavailable"}
            _keep_pending(receipt_rows, "root_fd_unavailable")
            return EXIT_SAFETY_FAILURE, receipt
        try:
            if not _root_identity_matches(validated_root, root_fd):
                receipt["status"] = "safety_failure"
                receipt["error"] = {"code": "root_changed"}
                _keep_pending(receipt_rows, "root_changed")
                return EXIT_SAFETY_FAILURE, receipt
            for index, (candidate, evaluation) in enumerate(
                zip(candidates, evaluations, strict=True)
            ):
                if evaluation.decision != "eligible":
                    continue
                try:
                    recheck_snapshot = _snapshot(
                        board_source,
                        proc_root,
                        max_processes=max_processes,
                    )
                except SafetyError as exc:
                    receipt["status"] = "safety_failure"
                    receipt["error"] = {"code": exc.code}
                    _keep_pending(receipt_rows, "safety_recheck_failed")
                    return EXIT_SAFETY_FAILURE, receipt

                recheck = evaluate_candidate(
                    candidate,
                    root=validated_root.path,
                    snapshot=recheck_snapshot,
                    retention_ns=retention_ns,
                    now_ns=time.time_ns(),
                )
                receipt_rows[index] = _candidate_receipt(candidate, recheck)
                if recheck.decision != "eligible":
                    receipt_rows[index]["reason_codes"].append("safety_recheck_blocked")
                    continue
                try:
                    if not _root_identity_matches(validated_root, root_fd):
                        raise SafetyError("root_changed")
                    fd_stat = os.stat(candidate.name, dir_fd=root_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(fd_stat.st_mode):
                        raise SafetyError("candidate_type_changed")
                    if (
                        recheck.device,
                        recheck.inode,
                        recheck.mtime_ns,
                    ) != (fd_stat.st_dev, fd_stat.st_ino, fd_stat.st_mtime_ns):
                        raise SafetyError("candidate_changed_before_delete")
                    shutil.rmtree(candidate.name, dir_fd=root_fd)
                except SafetyError as exc:
                    receipt["status"] = "safety_failure"
                    receipt["error"] = {"code": exc.code}
                    _keep_pending(receipt_rows, "apply_aborted")
                    receipt_rows[index]["reason_codes"].append(exc.code)
                    return EXIT_SAFETY_FAILURE, receipt
                except OSError:
                    receipt["status"] = "safety_failure"
                    receipt["error"] = {"code": "delete_failed"}
                    _keep_pending(receipt_rows, "apply_aborted")
                    receipt_rows[index]["reason_codes"].append("delete_failed")
                    return EXIT_SAFETY_FAILURE, receipt
                if os.path.lexists(candidate.path):
                    receipt["status"] = "safety_failure"
                    receipt["error"] = {"code": "candidate_reappeared"}
                    _keep_pending(receipt_rows, "apply_aborted")
                    receipt_rows[index]["reason_codes"].append("candidate_reappeared")
                    return EXIT_SAFETY_FAILURE, receipt
                receipt_rows[index]["decision"] = "deleted"
                receipt_rows[index]["reason_codes"].append("deleted")
        finally:
            os.close(root_fd)
        return 0, receipt
    finally:
        board_source.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed reaper for old direct-child workspace directories"
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="explicit absolute cleanup root; only direct child directories are considered",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete eligible workspaces (default is dry-run)",
    )
    parser.add_argument(
        "--retention-hours",
        type=float,
        default=DEFAULT_RETENTION_HOURS,
        help=f"minimum age in hours (default: {DEFAULT_RETENTION_HOURS:g})",
    )
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"), help=argparse.SUPPRESS)
    parser.add_argument(
        "--max-live-workspaces",
        type=int,
        default=DEFAULT_MAX_LIVE_WORKSPACES,
    )
    parser.add_argument("--max-processes", type=int, default=DEFAULT_MAX_PROCESSES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.retention_hours < 0:
        print(json.dumps({"error": {"code": "invalid_retention"}}, sort_keys=True))
        return EXIT_SAFETY_FAILURE
    try:
        exit_code, receipt = run_reaper(
            args.root,
            apply=args.apply,
            retention_seconds=args.retention_hours * 3600,
            proc_root=args.proc_root,
            max_live_workspaces=args.max_live_workspaces,
            max_processes=args.max_processes,
        )
    except ValueError:
        print(json.dumps({"error": {"code": "invalid_configuration"}}, sort_keys=True))
        return EXIT_SAFETY_FAILURE
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
