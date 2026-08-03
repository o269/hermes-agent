#!/usr/bin/env python3
"""Fail-closed freshness and integrity monitor for finalized boardd backups.

This process is deliberately independent of boardd and never opens the live board.
It is stateless: every invocation discovers and verifies the newest finalized copy,
so a stale success cache cannot hide a replaced or newly corrupted backup.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
from typing import Callable, Sequence

DEFAULT_BACKUP_DIR = "/var/lib/boardd/fleet/boardd-backups"
DEFAULT_BACKUP_INTERVAL_SECONDS = 900.0
DEFAULT_MAX_AGE_SECONDS = 2700.0
FINALIZED_BACKUP_RE = re.compile(r"^kanban\.(\d{8}-\d{6})\.db$")

SQLiteConnector = Callable[..., sqlite3.Connection]


@dataclass(frozen=True)
class BackupCandidate:
    path: Path
    name_timestamp_ns: int
    device: int
    inode: int
    size: int
    mtime_ns: int

    @property
    def identity(self) -> tuple[int, int, int, int]:
        return (self.device, self.inode, self.size, self.mtime_ns)


@dataclass(frozen=True)
class CheckResult:
    backup: Path
    age_seconds: float
    size: int


class CheckFailure(RuntimeError):
    def __init__(self, reason: str, detail: str, *, exit_code: int = 1) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.exit_code = exit_code


def _field(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=True, separators=(",", ":"))


def _filename_timestamp_ns(name: str) -> int | None:
    match = FINALIZED_BACKUP_RE.fullmatch(name)
    if match is None:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S")
        # boardd names backups with time.strftime(), which uses the host's local
        # timezone. time.mktime() mirrors that contract instead of assuming UTC.
        return int(time.mktime(parsed.timetuple()) * 1_000_000_000)
    except (ValueError, OverflowError, OSError):
        return None


def discover_finalized_backups(directory: Path) -> list[BackupCandidate]:
    if not directory.is_absolute():
        raise CheckFailure(
            "invalid_config",
            f"backup_dir={_field(directory)} must be absolute",
            exit_code=2,
        )

    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CheckFailure(
            "scan_failed",
            f"backup_dir={_field(directory)} error={_field(exc)} "
            'action="fix monitor read access to the finalized backup directory"',
        ) from exc

    candidates: list[BackupCandidate] = []
    for entry in entries:
        name_timestamp_ns = _filename_timestamp_ns(entry.name)
        if name_timestamp_ns is None:
            # Includes boardd's .kanban.*.partial files and every malformed name.
            continue
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise CheckFailure(
                "scan_failed",
                f"backup={_field(entry.name)} error={_field(exc)} "
                'action="fix monitor read access and retry"',
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            continue
        candidates.append(
            BackupCandidate(
                path=Path(entry.path),
                name_timestamp_ns=name_timestamp_ns,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                size=metadata.st_size,
                mtime_ns=metadata.st_mtime_ns,
            )
        )
    return candidates


def _current_identity(path: Path) -> tuple[int, int, int, int]:
    metadata = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise CheckFailure(
            "backup_changed",
            f"backup={_field(path.name)} is no longer a regular file "
            'action="retry after boardd backup rotation settles"',
        )
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def verify_integrity(
    candidate: BackupCandidate,
    *,
    connector: SQLiteConnector = sqlite3.connect,
) -> None:
    if candidate.size <= 0:
        raise CheckFailure(
            "sqlite_open_or_query_failed",
            f"backup={_field(candidate.path.name)} is empty "
            'action="inspect boardd backup errors and disk health"',
        )

    try:
        if _current_identity(candidate.path) != candidate.identity:
            raise CheckFailure(
                "backup_changed",
                f"backup={_field(candidate.path.name)} changed before verification "
                'action="retry after boardd backup rotation settles"',
            )
        uri = f"{candidate.path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
        connection = connector(uri, uri=True, timeout=5.0)
        try:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
        if _current_identity(candidate.path) != candidate.identity:
            raise CheckFailure(
                "backup_changed",
                f"backup={_field(candidate.path.name)} changed during verification "
                'action="retry after boardd backup rotation settles"',
            )
    except CheckFailure:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise CheckFailure(
            "sqlite_open_or_query_failed",
            f"backup={_field(candidate.path.name)} error={_field(exc)} "
            'action="inspect the newest backup and boardd backup logs"',
        ) from exc

    if rows != [("ok",)]:
        summary = repr(rows[:3])[:200]
        raise CheckFailure(
            "integrity_not_ok",
            f"backup={_field(candidate.path.name)} result={_field(summary)} "
            'action="preserve the corrupt copy and investigate boardd before restore"',
        )


def run_check(
    backup_dir: Path,
    *,
    max_age_seconds: float,
    now_ns: int | None = None,
    connector: SQLiteConnector = sqlite3.connect,
) -> CheckResult:
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise CheckFailure(
            "invalid_config",
            f"max_age_seconds={_field(max_age_seconds)} must be finite and positive",
            exit_code=2,
        )
    candidates = discover_finalized_backups(backup_dir)
    # Capture the real clock after scanning so a backup finalized during the
    # scan is not misclassified as future-dated by a few microseconds.
    if now_ns is None:
        now_ns = time.time_ns()
    if not candidates:
        raise CheckFailure(
            "no_finalized_backup",
            f"backup_dir={_field(backup_dir)} "
            'action="check boardd.service, disk health, and BOARDD_BACKUP_DIR"',
        )

    for candidate in candidates:
        if candidate.name_timestamp_ns > now_ns or candidate.mtime_ns > now_ns:
            raise CheckFailure(
                "clock_skew",
                f"backup={_field(candidate.path.name)} has a future timestamp "
                'action="check the host clock and backup file metadata"',
            )

    # mtime is the final-copy completion signal, while filename time records the
    # start of boardd's VACUUM operation. A single-threaded boardd produces both
    # in the same order. Refuse inconsistent ordering instead of letting either
    # timestamp hide a corrupt newer copy behind an older clean one.
    newest_by_mtime = max(
        candidates,
        key=lambda item: (item.mtime_ns, item.name_timestamp_ns, item.path.name),
    )
    newest_by_name = max(
        candidates,
        key=lambda item: (item.name_timestamp_ns, item.mtime_ns, item.path.name),
    )
    if newest_by_mtime.path != newest_by_name.path:
        raise CheckFailure(
            "ambiguous_backup_order",
            f"mtime_newest={_field(newest_by_mtime.path.name)} "
            f"name_newest={_field(newest_by_name.path.name)} "
            'action="check the host clock and backup file metadata"',
        )
    newest = newest_by_mtime
    age_ns = max(now_ns - newest.mtime_ns, now_ns - newest.name_timestamp_ns)
    age_seconds = age_ns / 1_000_000_000
    if age_seconds > max_age_seconds:
        raise CheckFailure(
            "stale_backup",
            f"backup={_field(newest.path.name)} age_seconds={age_seconds:.0f} "
            f"max_age_seconds={max_age_seconds:.0f} "
            'action="check boardd.service, backup errors, and disk guard state"',
        )

    verify_integrity(newest, connector=connector)
    return CheckResult(backup=newest.path, age_seconds=age_seconds, size=newest.size)


def _positive_finite(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise CheckFailure(
            "invalid_config",
            f"{name}={_field(raw)} must be a number",
            exit_code=2,
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise CheckFailure(
            "invalid_config",
            f"{name}={_field(raw)} must be finite and positive",
            exit_code=2,
        )
    return value


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only freshness and integrity check for boardd backups",
    )
    parser.add_argument(
        "--backup-dir",
        default=os.environ.get("BOARDD_BACKUP_DIR", DEFAULT_BACKUP_DIR),
        help="directory containing finalized kanban.YYYYMMDD-HHMMSS.db copies",
    )
    parser.add_argument(
        "--backup-interval-seconds",
        default=os.environ.get(
            "BOARDD_BACKUP_INTERVAL_S",
            str(DEFAULT_BACKUP_INTERVAL_SECONDS),
        ),
        help="expected boardd backup cadence; max age must be at least twice this",
    )
    parser.add_argument(
        "--max-age-seconds",
        default=os.environ.get(
            "BOARDD_BACKUP_MONITOR_MAX_AGE_SECONDS",
            str(DEFAULT_MAX_AGE_SECONDS),
        ),
        help="freshness threshold (default: 2700, three times the 900s cadence)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        interval = _positive_finite(
            str(args.backup_interval_seconds),
            "backup_interval_seconds",
        )
        max_age = _positive_finite(str(args.max_age_seconds), "max_age_seconds")
        if max_age < interval * 2:
            raise CheckFailure(
                "unsafe_threshold",
                f"max_age_seconds={max_age:g} must be at least twice "
                f"backup_interval_seconds={interval:g}",
                exit_code=2,
            )
        result = run_check(
            Path(args.backup_dir),
            max_age_seconds=max_age,
        )
    except CheckFailure as exc:
        print(
            f"CRITICAL boardd-backup-monitor reason={exc.reason} {exc.detail}",
            file=sys.stderr,
        )
        return exc.exit_code
    except Exception as exc:  # fail closed without a noisy timer traceback
        print(
            "CRITICAL boardd-backup-monitor reason=unexpected_error "
            f'error={_field(exc)} action="inspect the monitor unit and retry"',
            file=sys.stderr,
        )
        return 1

    print(
        "OK boardd-backup-monitor "
        f"backup={_field(result.backup.name)} "
        f"age_seconds={result.age_seconds:.0f} size={result.size} integrity=ok"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
