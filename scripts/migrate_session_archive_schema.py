#!/usr/bin/env python3
"""Safely reconcile cold-archive session policy columns across state.db files.

Dry-run is the default. ``--apply`` requires an explicit backup suffix, a
pre-mutation manifest path, a receipt path, and two clean ``lsof`` observations.
The caller is responsible for stopping Hermes processes before invoking apply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


MIN_FREE_SPACE_BYTES = 64 * 1024 * 1024
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class MigrationError(RuntimeError):
    """Fail-closed migration error."""


class MigrationApplyError(MigrationError):
    """Apply failed after backups were created and rollback was attempted."""

    def __init__(self, message: str, receipt: dict[str, Any]):
        super().__init__(message)
        self.receipt = receipt


@dataclass(frozen=True)
class TargetGroup:
    canonical_path: Path
    aliases: tuple[Path, ...]
    device: int
    inode: int
    size_bytes: int

    @property
    def identity(self) -> str:
        return f"{self.device}:{self.inode}"


@dataclass(frozen=True)
class BackupRecord:
    target: TargetGroup
    backup_path: Path
    source_sha256: str
    backup_sha256: str
    size_bytes: int
    device: int
    inode: int


Failpoint = Callable[[str, TargetGroup, sqlite3.Connection], None]
Runner = Callable[..., subprocess.CompletedProcess[str]]


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def read_paths_file(path: Path) -> list[Path]:
    paths: list[Path] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            paths.append(Path(line))
    return paths


def deduplicate_targets(paths: Iterable[Path]) -> list[TargetGroup]:
    """Group path aliases by the underlying device+inode identity."""
    grouped: dict[tuple[int, int], list[Path]] = {}
    metadata: dict[tuple[int, int], os.stat_result] = {}
    normalized: list[Path] = sorted(
        {_absolute(path) for path in paths}, key=lambda value: str(value)
    )
    if not normalized:
        raise MigrationError("no state.db paths were supplied")

    for path in normalized:
        try:
            stat_result = path.stat()
        except OSError as exc:
            raise MigrationError(f"cannot stat target {path}: {exc}") from exc
        if not path.is_file():
            raise MigrationError(f"target is not a regular file: {path}")
        identity = (int(stat_result.st_dev), int(stat_result.st_ino))
        grouped.setdefault(identity, []).append(path)
        metadata[identity] = stat_result

    result: list[TargetGroup] = []
    for identity, aliases in sorted(grouped.items(), key=lambda item: str(item[1][0])):
        # Never use a supplied symlink as the path we open for backup or
        # mutation. Prefer a regular hardlink; if every supplied path is a
        # symlink, resolve the lexicographically first alias once and then pin
        # its device+inode at every later gate.
        regular_aliases = [path for path in aliases if not path.is_symlink()]
        canonical = (
            regular_aliases[0]
            if regular_aliases
            else _absolute(Path(os.path.realpath(str(aliases[0]))))
        )
        try:
            stat_result = canonical.stat()
        except OSError as exc:
            raise MigrationError(
                f"cannot stat canonical target {canonical}: {exc}"
            ) from exc
        canonical_identity = (int(stat_result.st_dev), int(stat_result.st_ino))
        if canonical_identity != identity or not stat.S_ISREG(stat_result.st_mode):
            raise MigrationError(
                f"canonical target changed during discovery: {canonical}"
            )
        result.append(
            TargetGroup(
                canonical_path=canonical,
                aliases=tuple(aliases),
                device=identity[0],
                inode=identity[1],
                size_bytes=int(stat_result.st_size),
            )
        )
    return result


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    mode = "ro" if read_only else "rw"
    uri = f"{path.as_uri()}?mode={mode}"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    escaped = table.replace('"', '""')
    rows = conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()
    return {str(row["name"]): row for row in rows}


def _normalize_default(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text.lower()


def _validate_schema(conn: sqlite3.Connection) -> tuple[bool, bool]:
    sessions = _table_columns(conn, "sessions")
    messages = _table_columns(conn, "messages")
    if not sessions or not messages:
        raise MigrationError("required sessions/messages tables are missing")
    for required in ("id", "started_at"):
        if required not in sessions:
            raise MigrationError(f"sessions.{required} is missing")
    for required in ("session_id", "timestamp"):
        if required not in messages:
            raise MigrationError(f"messages.{required} is missing")

    pinned_present = "pinned" in sessions
    activity_present = "last_activity_at" in sessions
    if pinned_present:
        pinned = sessions["pinned"]
        if str(pinned["type"] or "").upper() not in {"BOOLEAN", "INTEGER"}:
            raise MigrationError("existing sessions.pinned has an unsupported type")
        if (
            int(pinned["notnull"] or 0) != 1
            or _normalize_default(pinned["dflt_value"]) != "0"
        ):
            raise MigrationError(
                "existing sessions.pinned must be NOT NULL DEFAULT 0; refusing to rewrite it"
            )
    if activity_present:
        activity = sessions["last_activity_at"]
        activity_type = str(activity["type"] or "").upper()
        if activity_type not in {"REAL", "FLOAT", "DOUBLE"}:
            raise MigrationError(
                "existing sessions.last_activity_at has an unsupported type"
            )
        if int(activity["notnull"] or 0) != 0 or activity["dflt_value"] is not None:
            raise MigrationError(
                "existing sessions.last_activity_at must be nullable with no default; "
                "refusing unsafe synthetic activity semantics"
            )
    return pinned_present, activity_present


def _finite_epoch(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"{label} is not numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise MigrationError(f"{label} is non-finite: {value!r}")
    return result


def _session_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    pinned_present, activity_present = _validate_schema(conn)
    projection = ["id"]
    projection.append("pinned" if pinned_present else "0 AS pinned")
    projection.append(
        "last_activity_at" if activity_present else "NULL AS last_activity_at"
    )
    rows = conn.execute(
        f"SELECT {', '.join(projection)} FROM sessions ORDER BY id"
    ).fetchall()

    pins: dict[str, int] = {}
    activity: dict[str, float | None] = {}
    for row in rows:
        session_id = str(row["id"])
        pin = int(row["pinned"] or 0)
        if pin not in (0, 1):
            raise MigrationError(f"session {session_id} has invalid pinned={pin}")
        pins[session_id] = pin
        raw_activity = row["last_activity_at"]
        activity[session_id] = (
            None
            if raw_activity is None
            else _finite_epoch(raw_activity, f"session {session_id} last_activity_at")
        )

    message_activity: dict[str, float] = {}
    for row in conn.execute(
        "SELECT session_id, MAX(timestamp) AS max_timestamp "
        "FROM messages GROUP BY session_id ORDER BY session_id"
    ).fetchall():
        session_id = str(row["session_id"])
        message_activity[session_id] = _finite_epoch(
            row["max_timestamp"], f"session {session_id} MAX(messages.timestamp)"
        )

    message_count = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    return {
        "pinned_present": pinned_present,
        "last_activity_at_present": activity_present,
        "session_count": len(rows),
        "message_count": message_count,
        "pins": pins,
        "activity": activity,
        "message_activity": message_activity,
    }


def _digest_mapping(mapping: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for key in sorted(mapping):
        digest.update(key.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(repr(mapping[key]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _expected_activity(snapshot: dict[str, Any]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for session_id, existing in snapshot["activity"].items():
        message_max = snapshot["message_activity"].get(session_id)
        if message_max is None:
            result[session_id] = existing
        elif existing is None:
            result[session_id] = message_max
        else:
            result[session_id] = max(existing, message_max)
    return result


def _summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    expected = _expected_activity(snapshot)
    rows_backfilled = sum(
        1
        for session_id, value in snapshot["activity"].items()
        if value is None and expected[session_id] is not None
    )
    rows_advanced = sum(
        1
        for session_id, value in snapshot["activity"].items()
        if value is not None
        and expected[session_id] is not None
        and expected[session_id] > value
    )
    no_message_null_rows = sum(
        1
        for session_id, value in snapshot["activity"].items()
        if value is None and session_id not in snapshot["message_activity"]
    )
    return {
        "pinned_present": snapshot["pinned_present"],
        "last_activity_at_present": snapshot["last_activity_at_present"],
        "session_rows": snapshot["session_count"],
        "message_rows": snapshot["message_count"],
        "message_sessions": len(snapshot["message_activity"]),
        "pinned_true_rows": sum(snapshot["pins"].values()),
        "pinned_digest": _digest_mapping(snapshot["pins"]),
        "activity_digest": _digest_mapping(snapshot["activity"]),
        "rows_backfill_needed": rows_backfilled,
        "rows_advance_needed": rows_advanced,
        "no_message_null_rows": no_message_null_rows,
        "needs_apply": (
            not snapshot["pinned_present"]
            or not snapshot["last_activity_at_present"]
            or rows_backfilled > 0
            or rows_advanced > 0
        ),
    }


def inspect_target(target: TargetGroup) -> dict[str, Any]:
    try:
        conn = _connect(target.canonical_path, read_only=True)
    except sqlite3.Error as exc:
        raise MigrationError(
            f"cannot open {target.canonical_path} read-only: {exc}"
        ) from exc
    try:
        snapshot = _session_snapshot(conn)
        result = _summary(snapshot)
        result.update({
            "canonical_path": str(target.canonical_path),
            "aliases": [str(path) for path in target.aliases],
            "device": target.device,
            "inode": target.inode,
            "size_bytes": target.size_bytes,
        })
        return result
    finally:
        conn.close()


def build_plan(targets: Sequence[TargetGroup]) -> dict[str, Any]:
    per_db = [inspect_target(target) for target in targets]
    return {
        "format": "hermes-session-archive-schema-plan-v1",
        "generated_at": time.time(),
        "input_paths": sum(len(target.aliases) for target in targets),
        "unique_databases": len(targets),
        "databases_needing_apply": sum(1 for item in per_db if item["needs_apply"]),
        "databases": per_db,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _backup_path(target: TargetGroup, suffix: str) -> Path:
    if not suffix.startswith(".bak-") or "/" in suffix or "\\" in suffix:
        raise MigrationError(
            "backup suffix must start with .bak- and contain no path separator"
        )
    return target.canonical_path.with_name(target.canonical_path.name + suffix)


def assert_sidecars_quiet(targets: Sequence[TargetGroup]) -> None:
    """Require a fully quiesced main file with no SQLite sidecar artifacts."""
    for target in targets:
        for alias in target.aliases:
            for suffix in _SIDECAR_SUFFIXES:
                sidecar = Path(str(alias) + suffix)
                if sidecar.exists():
                    raise MigrationError(
                        "SQLite sidecar must be absent before apply; stop holders and "
                        f"checkpoint/clean it under operator custody: {sidecar}"
                    )


def assert_target_identities(targets: Sequence[TargetGroup]) -> None:
    """Reject path swaps or alias drift between planning and mutation."""
    for target in targets:
        for alias in target.aliases:
            try:
                stat_result = alias.stat()
            except OSError as exc:
                raise MigrationError(f"cannot restat target {alias}: {exc}") from exc
            identity = (int(stat_result.st_dev), int(stat_result.st_ino))
            if identity != (target.device, target.inode):
                raise MigrationError(
                    f"target identity changed after planning: {alias} "
                    f"was {target.identity}, now {identity[0]}:{identity[1]}"
                )


def _lsof_paths(targets: Sequence[TargetGroup]) -> list[Path]:
    paths: set[Path] = set()
    for target in targets:
        for alias in target.aliases:
            paths.add(alias)
            for suffix in _SIDECAR_SUFFIXES:
                sidecar = Path(str(alias) + suffix)
                if sidecar.exists():
                    paths.add(sidecar)
    ordered: list[Path] = sorted(paths, key=lambda value: str(value))
    return ordered


def assert_no_lsof_holders(
    targets: Sequence[TargetGroup],
    *,
    runner: Runner = subprocess.run,
) -> None:
    paths = _lsof_paths(targets)
    command = ["lsof", "-F", "pfn", "--", *(str(path) for path in paths)]
    try:
        result = runner(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise MigrationError("lsof is required for --apply") from exc
    output = (result.stdout or "").strip()
    error_output = (result.stderr or "").strip()
    if result.returncode == 0 or output:
        raise MigrationError(
            "live holder detected; stop every writer before apply: "
            + (output or result.stderr or "lsof returned success")
        )
    if result.returncode != 1 or error_output:
        raise MigrationError(
            f"lsof failed with exit {result.returncode}: {result.stderr or result.stdout}"
        )


def enforce_free_space(targets: Sequence[TargetGroup]) -> dict[str, Any]:
    by_device: dict[int, dict[str, int]] = {}
    for target in targets:
        source_stat = target.canonical_path.stat()
        stat_result = target.canonical_path.parent.stat()
        entry = by_device.setdefault(
            int(stat_result.st_dev), {"source_bytes": 0, "free_bytes": 0}
        )
        entry["source_bytes"] += int(source_stat.st_size)
        entry["free_bytes"] = shutil.disk_usage(target.canonical_path.parent).free

    receipt: dict[str, Any] = {}
    for device, values in by_device.items():
        required = values["source_bytes"] * 2 + MIN_FREE_SPACE_BYTES
        if values["free_bytes"] < required:
            raise MigrationError(
                f"free-space gate failed on device {device}: "
                f"need {required}, have {values['free_bytes']}"
            )
        receipt[str(device)] = {**values, "required_bytes": required}
    return receipt


def create_backups(targets: Sequence[TargetGroup], suffix: str) -> list[BackupRecord]:
    backups: list[BackupRecord] = []
    for target in targets:
        destination = _backup_path(target, suffix)
        source_hash = sha256_file(target.canonical_path)
        try:
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise MigrationError(
                f"refusing to overwrite existing backup: {destination}"
            ) from exc
        except OSError as exc:
            raise MigrationError(f"cannot create backup {destination}: {exc}") from exc

        try:
            with os.fdopen(destination_fd, "wb") as output:
                with target.canonical_path.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            shutil.copystat(target.canonical_path, destination, follow_symlinks=False)
            os.chmod(destination, 0o600)
            backup_hash = sha256_file(destination)
            if backup_hash != source_hash:
                raise MigrationError(
                    f"backup hash mismatch for {target.canonical_path}"
                )
            if destination.stat().st_size != target.canonical_path.stat().st_size:
                raise MigrationError(
                    f"backup size mismatch for {target.canonical_path}"
                )
            _fsync_directory(destination.parent)
            backup_stat = destination.stat()
            backups.append(
                BackupRecord(
                    target=target,
                    backup_path=destination,
                    source_sha256=source_hash,
                    backup_sha256=backup_hash,
                    size_bytes=int(backup_stat.st_size),
                    device=int(backup_stat.st_dev),
                    inode=int(backup_stat.st_ino),
                )
            )
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    return backups


def verify_backups_still_current(backups: Sequence[BackupRecord]) -> None:
    for backup in backups:
        backup_stat = backup.backup_path.stat()
        if (int(backup_stat.st_dev), int(backup_stat.st_ino)) != (
            backup.device,
            backup.inode,
        ):
            raise MigrationError(f"backup path identity changed: {backup.backup_path}")
        source_hash = sha256_file(backup.target.canonical_path)
        backup_hash = sha256_file(backup.backup_path)
        if source_hash != backup.source_sha256 or backup_hash != backup.backup_sha256:
            raise MigrationError(
                f"target changed after backup or backup drifted: {backup.target.canonical_path}"
            )


def _run_integrity_checks(conn: sqlite3.Connection) -> dict[str, Any]:
    quick = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    integrity = [
        str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()
    ]
    foreign_keys = [
        tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
    ]
    if quick != ["ok"]:
        raise MigrationError(f"quick_check failed: {quick}")
    if integrity != ["ok"]:
        raise MigrationError(f"integrity_check failed: {integrity}")
    if foreign_keys:
        raise MigrationError(f"foreign_key_check failed: {foreign_keys[:5]}")
    return {
        "quick_check": "ok",
        "integrity_check": "ok",
        "foreign_key_check_rows": 0,
    }


def _assert_snapshot_transition(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[int, int]:
    if before["session_count"] != after["session_count"]:
        raise MigrationError("session row count changed during migration")
    if before["message_count"] != after["message_count"]:
        raise MigrationError("message row count changed during migration")
    if before["pins"] != after["pins"]:
        raise MigrationError("pinned values changed during migration")
    if before["message_activity"] != after["message_activity"]:
        raise MigrationError("message timestamps changed during migration")

    expected = _expected_activity(before)
    if expected != after["activity"]:
        raise MigrationError("last_activity_at does not match durable/message maxima")
    rows_backfilled = sum(
        1
        for session_id, value in before["activity"].items()
        if value is None and expected[session_id] is not None
    )
    rows_advanced = sum(
        1
        for session_id, value in before["activity"].items()
        if value is not None
        and expected[session_id] is not None
        and expected[session_id] > value
    )
    return rows_backfilled, rows_advanced


def migrate_one(
    target: TargetGroup,
    *,
    failpoint: Failpoint | None = None,
) -> dict[str, Any]:
    conn = _connect(target.canonical_path, read_only=False)
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = _session_snapshot(conn)
        schema_changes = 0
        if not before["pinned_present"]:
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT 0"
            )
            schema_changes += 1
        if not before["last_activity_at_present"]:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_activity_at REAL")
            schema_changes += 1

        update_cursor = conn.execute(
            """UPDATE sessions
               SET last_activity_at = (
                   SELECT MAX(m.timestamp)
                   FROM messages m
                   WHERE m.session_id = sessions.id
               )
               WHERE EXISTS (
                   SELECT 1 FROM messages m
                   WHERE m.session_id = sessions.id
               )
                 AND (
                   last_activity_at IS NULL
                   OR last_activity_at < (
                       SELECT MAX(m.timestamp)
                       FROM messages m
                       WHERE m.session_id = sessions.id
                   )
                 )"""
        )
        if failpoint is not None:
            failpoint("after_backfill", target, conn)

        after = _session_snapshot(conn)
        rows_backfilled, rows_advanced = _assert_snapshot_transition(before, after)
        expected_changes = rows_backfilled + rows_advanced
        if update_cursor.rowcount not in (-1, expected_changes):
            raise MigrationError(
                f"backfill rowcount mismatch: SQL={update_cursor.rowcount}, expected={expected_changes}"
            )
        checks = _run_integrity_checks(conn)
        conn.execute("COMMIT")
        committed = True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    if not committed:
        raise MigrationError(f"migration did not commit: {target.canonical_path}")
    post = inspect_target(target)
    if post["needs_apply"]:
        raise MigrationError(
            f"post-commit inspection still needs apply: {target.canonical_path}"
        )
    return {
        "canonical_path": str(target.canonical_path),
        "aliases": [str(path) for path in target.aliases],
        "device": target.device,
        "inode": target.inode,
        "before": _summary(before),
        "after": post,
        "schema_changes": schema_changes,
        "rows_backfilled": rows_backfilled,
        "rows_advanced": rows_advanced,
        **checks,
    }


def restore_backups(
    backups: Sequence[BackupRecord],
    *,
    lsof_runner: Runner = subprocess.run,
) -> tuple[list[dict[str, Any]], list[str]]:
    validation_errors: list[str] = []
    for backup in backups:
        try:
            backup_stat = backup.backup_path.stat()
            backup_hash = sha256_file(backup.backup_path)
            backup_size = backup_stat.st_size
        except OSError as exc:
            validation_errors.append(f"cannot read {backup.backup_path}: {exc}")
            continue
        if (int(backup_stat.st_dev), int(backup_stat.st_ino)) != (
            backup.device,
            backup.inode,
        ):
            validation_errors.append(
                f"backup path identity changed: {backup.backup_path}"
            )
            continue
        if backup_hash != backup.backup_sha256 or backup_size != backup.size_bytes:
            validation_errors.append(
                f"backup drifted before rollback: {backup.backup_path}"
            )
    # Never overwrite even one target unless every rollback source validates.
    if validation_errors:
        return [], validation_errors

    # A sidecar that appears after the apply preflight may belong to a writer
    # that raced the operator's quiesce.  Never delete it: destroying an
    # unproven WAL/SHM/journal can discard writes.  Rollback is destructive too,
    # so re-prove identities, zero holders, and an absent-sidecar state before
    # overwriting any main file.  If this gate fails, preserve every byte for
    # operator-led recovery from the already-verified backups.
    try:
        rollback_targets = [backup.target for backup in backups]
        assert_target_identities(rollback_targets)
        assert_no_lsof_holders(rollback_targets, runner=lsof_runner)
        assert_sidecars_quiet(rollback_targets)
    except Exception as exc:
        return [], [f"rollback preflight refused: {type(exc).__name__}: {exc}"]

    restored: list[dict[str, Any]] = []
    restore_errors: list[str] = []
    for backup in backups:
        try:
            source_fd = os.open(
                backup.backup_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                source_stat = os.fstat(source_fd)
                if (int(source_stat.st_dev), int(source_stat.st_ino)) != (
                    backup.device,
                    backup.inode,
                ):
                    raise MigrationError(
                        f"backup path identity changed: {backup.backup_path}"
                    )
                destination_fd = os.open(
                    backup.target.canonical_path,
                    os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    destination_stat = os.fstat(destination_fd)
                    if (int(destination_stat.st_dev), int(destination_stat.st_ino)) != (
                        backup.target.device,
                        backup.target.inode,
                    ):
                        raise MigrationError(
                            "target identity changed before rollback overwrite: "
                            f"{backup.target.canonical_path}"
                        )
                    os.ftruncate(destination_fd, 0)
                    with (
                        os.fdopen(source_fd, "rb") as source,
                        os.fdopen(destination_fd, "wb") as destination,
                    ):
                        source_fd = -1
                        destination_fd = -1
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                        destination.flush()
                        os.fsync(destination.fileno())
                finally:
                    if destination_fd >= 0:
                        os.close(destination_fd)
            finally:
                if source_fd >= 0:
                    os.close(source_fd)
            _fsync_directory(backup.target.canonical_path.parent)
            restored_hash = sha256_file(backup.target.canonical_path)
            if restored_hash != backup.source_sha256:
                raise MigrationError(
                    f"rollback hash mismatch for {backup.target.canonical_path}"
                )
            restored.append({
                "canonical_path": str(backup.target.canonical_path),
                "restored_sha256": restored_hash,
                "matches_backup": True,
            })
        except Exception as exc:  # keep trying every independently backed-up DB
            restore_errors.append(f"{backup.target.canonical_path}: {exc}")
    return restored, restore_errors


def _backup_receipt(backup: BackupRecord) -> dict[str, Any]:
    return {
        "canonical_path": str(backup.target.canonical_path),
        "backup_path": str(backup.backup_path),
        "source_sha256": backup.source_sha256,
        "backup_sha256": backup.backup_sha256,
        "size_bytes": backup.size_bytes,
        "device": backup.device,
        "inode": backup.inode,
        "byte_exact": backup.source_sha256 == backup.backup_sha256,
    }


def _no_op_database_receipt(
    target: TargetGroup, inspection: dict[str, Any]
) -> dict[str, Any]:
    conn = _connect(target.canonical_path, read_only=True)
    try:
        checks = _run_integrity_checks(conn)
    finally:
        conn.close()
    return {
        "canonical_path": str(target.canonical_path),
        "aliases": [str(path) for path in target.aliases],
        "device": target.device,
        "inode": target.inode,
        "status": "unchanged",
        "before": inspection,
        "after": inspection,
        "schema_changes": 0,
        "rows_backfilled": 0,
        "rows_advanced": 0,
        "backup": None,
        **checks,
    }


def _pending_database_receipt(
    target: TargetGroup, inspection: dict[str, Any]
) -> dict[str, Any]:
    return {
        "canonical_path": str(target.canonical_path),
        "aliases": [str(path) for path in target.aliases],
        "device": target.device,
        "inode": target.inode,
        "status": "pending",
        "before": inspection,
        "after": None,
        "schema_changes": None,
        "rows_backfilled": None,
        "rows_advanced": None,
        "backup": None,
        "quick_check": None,
        "integrity_check": None,
        "foreign_key_check_rows": None,
    }


def apply_plan(
    targets: Sequence[TargetGroup],
    plan: dict[str, Any],
    *,
    backup_suffix: str,
    lsof_runner: Runner = subprocess.run,
    failpoint: Failpoint | None = None,
) -> dict[str, Any]:
    del plan  # Always refresh after the first clean lsof observation.
    assert_target_identities(targets)
    assert_sidecars_quiet(targets)
    assert_no_lsof_holders(targets, runner=lsof_runner)
    fresh_plan = build_plan(targets)
    source_hashes = {
        target.identity: sha256_file(target.canonical_path) for target in targets
    }
    by_identity = {target.identity: target for target in targets}
    mutation_targets = [
        by_identity[f"{item['device']}:{item['inode']}"]
        for item in fresh_plan["databases"]
        if item["needs_apply"]
    ]
    no_op_items = [item for item in fresh_plan["databases"] if not item["needs_apply"]]
    receipt: dict[str, Any] = {
        "format": "hermes-session-archive-schema-receipt-v1",
        "started_at": time.time(),
        "status": "noop" if not mutation_targets else "applying",
        "input_paths": fresh_plan["input_paths"],
        "unique_databases": fresh_plan["unique_databases"],
        "databases_needing_apply": len(mutation_targets),
        "free_space": {},
        "lsof_zero_checks": 1,
        "backups": [],
        "databases": [
            *[
                _no_op_database_receipt(
                    by_identity[f"{item['device']}:{item['inode']}"], item
                )
                for item in no_op_items
            ],
            *[
                _pending_database_receipt(
                    by_identity[f"{item['device']}:{item['inode']}"], item
                )
                for item in fresh_plan["databases"]
                if item["needs_apply"]
            ],
        ],
    }
    # Every unique target receives a fresh byte-exact backup, including a DB
    # whose refreshed plan is already a no-op.  This keeps the operator's
    # target manifest and backup set one-to-one and makes a replay independently
    # recoverable without relying on an earlier run's artifacts.
    receipt["free_space"] = enforce_free_space(targets)

    # Observation 1 precedes planning/backups. Observation 2 follows every
    # fresh copy, immediately before the first DB mutation. Main-file hashes
    # bridge the interval so a short-lived writer cannot leave stale evidence.
    backups = create_backups(targets, backup_suffix)
    receipt["backups"] = [_backup_receipt(backup) for backup in backups]
    backups_by_identity = {backup.target.identity: backup for backup in backups}
    receipt_by_identity = {
        f"{item['device']}:{item['inode']}": item for item in receipt["databases"]
    }
    for item in receipt["databases"]:
        identity = f"{item['device']}:{item['inode']}"
        item["backup"] = _backup_receipt(backups_by_identity[identity])
    assert_no_lsof_holders(targets, runner=lsof_runner)
    receipt["lsof_zero_checks"] += 1
    assert_target_identities(targets)
    assert_sidecars_quiet(targets)
    for target in targets:
        if sha256_file(target.canonical_path) != source_hashes[target.identity]:
            raise MigrationError(
                f"target changed between lsof gates: {target.canonical_path}"
            )
    verify_backups_still_current(backups)

    if not mutation_targets:
        receipt["finished_at"] = time.time()
        receipt["databases"].sort(key=lambda item: item["canonical_path"])
        return receipt

    try:
        for target in mutation_targets:
            per_db = migrate_one(target, failpoint=failpoint)
            backup = backups_by_identity[target.identity]
            per_db["backup"] = _backup_receipt(backup)
            per_db["status"] = "applied"
            receipt_by_identity[target.identity].clear()
            receipt_by_identity[target.identity].update(per_db)
    except Exception as exc:
        mutation_backups = [
            backups_by_identity[target.identity] for target in mutation_targets
        ]
        restored, rollback_errors = restore_backups(
            mutation_backups,
            lsof_runner=lsof_runner,
        )
        restored_paths = {item["canonical_path"] for item in restored}
        plan_by_identity = {
            f"{item['device']}:{item['inode']}": item
            for item in fresh_plan["databases"]
        }
        for target in mutation_targets:
            item = receipt_by_identity[target.identity]
            if str(target.canonical_path) in restored_paths:
                restored_item = _no_op_database_receipt(
                    target,
                    plan_by_identity[target.identity],
                )
                restored_item["status"] = "restored"
                restored_item["backup"] = _backup_receipt(
                    backups_by_identity[target.identity]
                )
                item.clear()
                item.update(restored_item)
            else:
                item["status"] = "rollback_failed"
                item["error"] = f"{type(exc).__name__}: {exc}"
        rollback_error = "; ".join(rollback_errors) if rollback_errors else None
        receipt.update({
            "status": "rolled_back" if rollback_error is None else "rollback_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "restored": restored,
            "rollback_error": rollback_error,
            "finished_at": time.time(),
        })
        receipt["databases"].sort(key=lambda item: item["canonical_path"])
        raise MigrationApplyError(str(exc), receipt) from exc

    receipt["status"] = "applied"
    receipt["finished_at"] = time.time()
    receipt["databases"].sort(key=lambda item: item["canonical_path"])
    return receipt


def write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    destination = _absolute(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise MigrationError(f"refusing to overwrite receipt: {destination}") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(destination, 0o400)
    _fsync_directory(destination.parent)


def reserve_json_output(path: Path) -> tuple[Path, int]:
    """Reserve a receipt inode before mutation so final evidence cannot collide."""
    destination = _absolute(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise MigrationError(f"refusing to overwrite receipt: {destination}") from exc
    marker = b'{"status":"reserved-before-mutation"}\n'
    os.write(fd, marker)
    os.fsync(fd)
    _fsync_directory(destination.parent)
    return destination, fd


def finalize_reserved_json(
    reservation: tuple[Path, int], payload: dict[str, Any]
) -> None:
    destination, fd = reservation
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(destination, 0o400)
    _fsync_directory(destination.parent)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", default=[], type=Path)
    parser.add_argument("--paths-file", action="append", default=[], type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-suffix")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--receipt", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    supplied_paths = list(args.db)
    for paths_file in args.paths_file:
        supplied_paths.extend(read_paths_file(paths_file))

    targets = deduplicate_targets(supplied_paths)
    plan = build_plan(targets)
    if not args.apply:
        if args.manifest is not None:
            write_json_exclusive(args.manifest, plan)
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    if not args.backup_suffix:
        raise MigrationError(
            "--apply requires --backup-suffix (for example .bak-20260806)"
        )
    if args.manifest is None:
        raise MigrationError("--apply requires --manifest")
    if args.receipt is None:
        raise MigrationError("--apply requires --receipt")

    # Validate every evidence destination before any DB mutation. The receipt
    # inode is reserved up front, eliminating a late O_EXCL collision after a
    # successful migration.
    backup_paths = {
        _absolute(_backup_path(target, args.backup_suffix)) for target in targets
    }
    database_paths = {
        _absolute(alias) for target in targets for alias in target.aliases
    }
    sidecar_paths = {
        _absolute(Path(str(path) + suffix))
        for target in targets
        for path in {target.canonical_path, *target.aliases}
        for suffix in _SIDECAR_SUFFIXES
    }
    manifest_path = _absolute(args.manifest)
    receipt_path = _absolute(args.receipt)
    protected = backup_paths | database_paths | sidecar_paths
    if (
        manifest_path == receipt_path
        or manifest_path in protected
        or receipt_path in protected
    ):
        raise MigrationError(
            "manifest/receipt paths must be distinct from DB, sidecar, and backup paths"
        )
    write_json_exclusive(manifest_path, plan)
    reservation = reserve_json_output(receipt_path)

    try:
        receipt = apply_plan(
            targets,
            plan,
            backup_suffix=args.backup_suffix,
        )
    except MigrationApplyError as exc:
        finalize_reserved_json(reservation, exc.receipt)
        raise
    except Exception as exc:
        refused = {
            "format": "hermes-session-archive-schema-receipt-v1",
            "status": "refused",
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at": time.time(),
        }
        finalize_reserved_json(reservation, refused)
        raise
    finalize_reserved_json(reservation, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as exc:
        print(f"session archive schema migration refused: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
