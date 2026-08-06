"""Fail-closed, staged cold archival for offline Hermes ``state.db`` copies.

The producer and destructive apply phases are deliberately separate. A producer
creates a fresh, no-clobber stage, exports a rollback bundle and restricted QMD,
encrypts every restricted offsite object, and records exact remote readback
proof. Apply accepts only the externally approved manifest bytes/hash from that
existing stage, re-verifies encrypted remote custody, and performs every
invariant check before committing deletion on the offline candidate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from hermes_constants import get_default_hermes_root, get_hermes_home
from hermes_state import DEFAULT_DB_PATH
from hermes_cli.session_export_md import (
    redact_session_data,
    render_session_markdown,
    verify_export_file,
)

DEFAULT_HOT_DAYS = 30.0
DEFAULT_ARCHIVE_GRACE_DAYS = 7.0
DEFAULT_SOURCE_BUNDLE_RETENTION_SECONDS = 14 * 86_400
_ARCHIVE_VERSION = 2
_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")
_ROLLBACK_MEMBER_NAMES = {
    "": "state.db",
    "-wal": "state.db-wal",
    "-shm": "state.db-shm",
    "-journal": "state.db-journal",
}
_REQUIRED_FTS_OBJECTS = frozenset({
    "messages_fts",
    "messages_fts_insert",
    "messages_fts_delete",
    "messages_fts_update",
    "messages_fts_trigram",
    "messages_fts_trigram_insert",
    "messages_fts_trigram_delete",
    "messages_fts_trigram_update",
})
_REQUIRED_SESSION_COLUMNS = frozenset({
    "id",
    "source",
    "parent_session_id",
    "started_at",
    "ended_at",
    "archived",
    "pinned",
    "last_activity_at",
})
DEFAULT_PERMANENT_HOLD_SOURCES = frozenset({
    "discord",
    "imessage",
    "matrix",
    "photon",
    "signal",
    "slack",
    "sms",
    "telegram",
    "wecom",
    "whatsapp",
})
_TERMINAL_DELEGATION_STATES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "canceled",
})
_TERMINAL_DELIVERY_STATES = frozenset({
    "delivered",
    "failed",
    "cancelled",
    "canceled",
    "none",
    "",
})


class ColdArchiveError(RuntimeError):
    """Raised whenever cold archival cannot prove a required invariant."""


@dataclass(frozen=True)
class StageArtifacts:
    stage_root: Path
    manifest_path: Path
    restricted_ids_path: Path
    restricted_groups_path: Path
    restricted_index_path: Path
    age_recipient_snapshot_path: Path
    qmd_dir: Path
    rollback_dir: Path
    rollback_bundle_path: Path
    rollback_encrypted_path: Path
    restricted_encrypted_path: Path
    source_bundle_policy_path: Path
    producer_receipt_path: Path
    apply_prepared_path: Path
    retention_receipt_path: Path
    cutover_marker_path: Path
    source_bundle_prune_prepared_path: Path
    source_bundle_pruned_path: Path


def utc_iso(value: float | None = None) -> str:
    ts = _finite_epoch(time.time() if value is None else value, "timestamp")
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _finite_epoch(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ColdArchiveError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ColdArchiveError(f"{label} must be a finite number")
    return result


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _reject_symlink_components(
    path: Path, *, leaf_may_be_missing: bool = False
) -> None:
    """Reject symlinks in a security-sensitive path without resolving them away."""

    current = Path(path.anchor)
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if leaf_may_be_missing and index == len(parts) - 1:
                return
            raise ColdArchiveError(f"required path component is unavailable: {current}")
        except OSError as exc:
            raise ColdArchiveError(
                f"could not inspect path component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise ColdArchiveError(f"symlink path component is not allowed: {current}")


def _resolve_existing_file(path: Path) -> Path:
    try:
        raw = _absolute_path(path)
        _reject_symlink_components(raw)
        resolved = raw.resolve(strict=True)
        info = resolved.lstat()
    except (FileNotFoundError, OSError) as exc:
        raise ColdArchiveError(
            f"source database is unavailable: {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ColdArchiveError(f"source is not a regular file: {resolved}")
    return resolved


def _create_private_dir(path: Path) -> Path:
    raw = _absolute_path(path)
    try:
        _reject_symlink_components(raw, leaf_may_be_missing=True)
        parent = raw.parent.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ColdArchiveError(
            f"stage parent is unavailable: {raw.parent}: {exc}"
        ) from exc
    target = parent / raw.name
    try:
        os.mkdir(target, 0o700)
    except FileExistsError as exc:
        raise ColdArchiveError(
            f"refusing existing stage path (new producer stage required): {target}"
        ) from exc
    except OSError as exc:
        raise ColdArchiveError(
            f"could not create private directory {target}: {exc}"
        ) from exc
    if _mode(target) != 0o700:
        raise ColdArchiveError(f"new private directory is not mode 0700: {target}")
    _fsync_directory(parent)
    return target


def _require_private_dir(path: Path) -> Path:
    raw = _absolute_path(path)
    try:
        _reject_symlink_components(raw)
        info = raw.lstat()
    except OSError as exc:
        raise ColdArchiveError(
            f"required stage directory is unavailable: {raw}: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ColdArchiveError(f"stage path is not a non-symlink directory: {raw}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ColdArchiveError(f"stage directory must already be mode 0700: {raw}")
    return raw.resolve(strict=True)


def _require_private_file(path: Path) -> Path:
    raw = _absolute_path(path)
    try:
        _reject_symlink_components(raw)
        info = raw.lstat()
    except OSError as exc:
        raise ColdArchiveError(
            f"required stage file is unavailable: {raw}: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ColdArchiveError(
            f"stage artifact is not a non-symlink regular file: {raw}"
        )
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ColdArchiveError(f"stage artifact must already be mode 0600: {raw}")
    return raw.resolve(strict=True)


def _safe_immediate_child(root: Path, name: str) -> Path:
    if name != Path(name).name or name in {"", ".", ".."}:
        raise ColdArchiveError(f"unsafe archive filename: {name!r}")
    root = _require_private_dir(root)
    target = root / name
    if target.parent != root:
        raise ColdArchiveError(f"archive path escaped stage directory: {target}")
    return target


def _exclusive_write_bytes(path: Path, data: bytes) -> Path:
    parent = _require_private_dir(path.parent)
    target = _safe_immediate_child(parent, path.name)
    partial = _safe_immediate_child(
        parent, f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.partial"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(partial, flags, 0o600)
    except FileExistsError as exc:
        raise ColdArchiveError(f"refusing existing stage partial: {partial}") from exc
    except OSError as exc:
        raise ColdArchiveError(
            f"could not create stage partial {partial}: {exc}"
        ) from exc
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if fd >= 0:
            os.close(fd)
        partial.unlink(missing_ok=True)
        raise
    try:
        os.link(partial, target, follow_symlinks=False)
    except FileExistsError as exc:
        partial.unlink(missing_ok=True)
        raise ColdArchiveError(
            f"refusing to overwrite stage artifact: {target}"
        ) from exc
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ColdArchiveError(
            f"could not publish stage artifact without clobber: {target}: {exc}"
        ) from exc
    partial.unlink()
    _fsync_directory(parent)
    return target


def _exclusive_write_text(path: Path, text: str) -> Path:
    return _exclusive_write_bytes(path, text.encode("utf-8"))


def _exclusive_write_json(path: Path, payload: dict[str, Any]) -> Path:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return _exclusive_write_text(path, text)


def _decode_json_object(data: bytes, path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            data.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ColdArchiveError(f"invalid JSON stage artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ColdArchiveError(f"JSON stage artifact must contain an object: {path}")
    return payload


def _read_private_bytes(path: Path) -> tuple[Path, bytes]:
    """Read one stable private-file snapshot for both hashing and parsing."""

    private = _require_private_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(private, flags)
    except OSError as exc:
        raise ColdArchiveError(
            f"could not open private stage artifact {private}: {exc}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
            raise ColdArchiveError(f"stage artifact changed type or mode: {private}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read()
            after = os.fstat(handle.fileno())
        current = private.lstat()
        if (
            not os.path.samestat(before, after)
            or not os.path.samestat(after, current)
            or len(data) != after.st_size
        ):
            raise ColdArchiveError(f"stage artifact changed during read: {private}")
        return private, data
    finally:
        if fd >= 0:
            os.close(fd)


def _read_stable_regular_bytes(
    path: Path,
    *,
    label: str,
    required_mode: int | None = None,
    require_current_owner: bool = False,
    require_single_link: bool = False,
) -> tuple[Path, bytes, os.stat_result]:
    """Read one external regular file without accepting path or byte drift."""

    raw = _absolute_path(path)
    _reject_symlink_components(raw)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(raw, flags)
    except OSError as exc:
        raise ColdArchiveError(f"{label} is unavailable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ColdArchiveError(f"{label} is not a regular file")
        if required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode:
            raise ColdArchiveError(f"{label} must already be mode {required_mode:04o}")
        if require_current_owner:
            get_effective_uid = getattr(os, "geteuid", None)
            if get_effective_uid is None:
                raise ColdArchiveError(
                    f"{label} ownership cannot be proven on this platform"
                )
            if before.st_uid != get_effective_uid():
                raise ColdArchiveError(
                    f"{label} must be owned by the current effective user"
                )
        if require_single_link and before.st_nlink != 1:
            raise ColdArchiveError(f"{label} must not have hardlink aliases")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read()
            after = os.fstat(handle.fileno())
        current = raw.lstat()
        if (
            not os.path.samestat(before, after)
            or not os.path.samestat(after, current)
            or len(data) != after.st_size
        ):
            raise ColdArchiveError(f"{label} changed during read")
        return raw.resolve(strict=True), data, after
    finally:
        if fd >= 0:
            os.close(fd)


def _read_frozen_approval_bytes(
    stage: StageArtifacts,
    supplied_path: Path,
    stage_artifact_path: Path,
    *,
    label: str,
) -> tuple[Path, bytes]:
    """Require a distinct, read-only, single-link operator approval artifact."""

    supplied, data, _ = _read_stable_regular_bytes(
        supplied_path,
        label=label,
        required_mode=0o400,
        require_current_owner=True,
        require_single_link=True,
    )
    if supplied == stage.stage_root or supplied.is_relative_to(stage.stage_root):
        raise ColdArchiveError(f"{label} must be outside the producer stage")
    try:
        if os.path.samefile(supplied, stage_artifact_path):
            raise ColdArchiveError(f"{label} must be a distinct external file")
    except OSError as exc:
        raise ColdArchiveError(f"could not prove {label} file identity: {exc}") from exc
    return supplied, data


def _load_private_json(path: Path) -> dict[str, Any]:
    private, data = _read_private_bytes(path)
    return _decode_json_object(data, private)


def _stage_paths(root: Path) -> StageArtifacts:
    restricted = root / "restricted"
    rollback = root / "rollback"
    return StageArtifacts(
        stage_root=root,
        manifest_path=root / "GATE-B-MANIFEST.json",
        restricted_ids_path=restricted / "selected-session-ids.json",
        restricted_groups_path=restricted / "lineage-parent-map.json",
        restricted_index_path=restricted / "RESTRICTED-INDEX.json",
        age_recipient_snapshot_path=restricted / "AGE-RECIPIENTS.txt",
        qmd_dir=root / "cold-qmd",
        rollback_dir=rollback,
        rollback_bundle_path=rollback / "rollback-source-bundle.tar.gz",
        rollback_encrypted_path=rollback / "ROLLBACK-SOURCE-BUNDLE.tar.gz.age",
        restricted_encrypted_path=restricted / "RESTRICTED-COLD-QMD.tar.gz.age",
        source_bundle_policy_path=rollback / "SOURCE-BUNDLE-RETENTION-POLICY.json",
        producer_receipt_path=root / "COLD-ARCHIVE-PRODUCER-RECEIPT.json",
        apply_prepared_path=root / "COLD-ARCHIVE-APPLY-PREPARED.json",
        retention_receipt_path=root / "COLD-ARCHIVE-RETENTION-RECEIPT.json",
        cutover_marker_path=rollback / "CANDIDATE-CUTOVER.json",
        source_bundle_prune_prepared_path=rollback
        / "SOURCE-BUNDLE-PRUNE-PREPARED.json",
        source_bundle_pruned_path=rollback / "SOURCE-BUNDLE-PRUNED.json",
    )


def _create_stage(stage_root: Path) -> StageArtifacts:
    root = _create_private_dir(stage_root)
    _create_private_dir(root / "restricted")
    _create_private_dir(root / "cold-qmd")
    _create_private_dir(root / "rollback")
    return _stage_paths(root)


def _load_stage(stage_root: Path) -> StageArtifacts:
    root = _require_private_dir(stage_root)
    _require_private_dir(root / "restricted")
    _require_private_dir(root / "cold-qmd")
    _require_private_dir(root / "rollback")
    return _stage_paths(root)


def _candidate_live_paths() -> list[Path]:
    """Collect every default/current/named-profile DB and sidecar path.

    Enumeration and stat errors fail closed. A live path does not need to be a
    canonical pathname match: ``reject_live_state_db`` also compares device and
    inode so hardlink aliases are fenced.
    """

    try:
        root = get_default_hermes_root().expanduser().resolve(strict=False)
        current = get_hermes_home().expanduser().resolve(strict=False)
    except OSError as exc:
        raise ColdArchiveError(
            f"could not resolve Hermes profile roots: {exc}"
        ) from exc
    homes: list[Path] = [root, current]
    profiles_root = root / "profiles"
    try:
        if profiles_root.exists():
            if not profiles_root.is_dir():
                raise ColdArchiveError(
                    f"profiles root is not a directory: {profiles_root}"
                )
            for entry in sorted(profiles_root.iterdir(), key=lambda item: item.name):
                try:
                    resolved = entry.resolve(strict=True)
                    if resolved.is_dir():
                        homes.append(resolved)
                except OSError as exc:
                    raise ColdArchiveError(
                        f"could not enumerate active profile path {entry}: {exc}"
                    ) from exc
    except OSError as exc:
        raise ColdArchiveError(f"could not enumerate active profiles: {exc}") from exc

    raw_paths = [Path(DEFAULT_DB_PATH).expanduser(), current / "state.db"]
    for home in homes:
        raw_paths.append(home / "state.db")
    paths: list[Path] = []
    seen: set[str] = set()
    for base in raw_paths:
        for suffix in _SIDECAR_SUFFIXES:
            candidate = base if not suffix else base.with_name(base.name + suffix)
            key = os.path.abspath(os.fspath(candidate))
            if key not in seen:
                seen.add(key)
                paths.append(candidate)
    return paths


def reject_live_state_db(source_path: Path) -> Path:
    source = _resolve_existing_file(source_path)
    source_family = [source]
    for suffix in _SIDECAR_SUFFIXES[1:]:
        sidecar = source.with_name(source.name + suffix)
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ColdArchiveError(
                f"could not inspect candidate sidecar identity for {sidecar}: {exc}"
            ) from exc
        source_family.append(_resolve_existing_file(sidecar))

    protected_paths = _candidate_live_paths()
    for candidate_component in source_family:
        try:
            candidate_stat = candidate_component.stat()
        except OSError as exc:
            raise ColdArchiveError(
                f"could not stat candidate database component: {candidate_component}: {exc}"
            ) from exc
        for protected in protected_paths:
            try:
                protected_resolved = protected.expanduser().resolve(strict=False)
            except OSError as exc:
                raise ColdArchiveError(
                    f"could not resolve protected state path: {exc}"
                ) from exc
            if candidate_component == protected_resolved:
                raise ColdArchiveError(
                    "refusing active Hermes state.db or sidecar; use an offline candidate copy"
                )
            try:
                if protected.exists() and os.path.samestat(
                    candidate_stat, protected.stat()
                ):
                    raise ColdArchiveError(
                        "refusing hardlink/inode alias of an active Hermes state.db or sidecar"
                    )
            except ColdArchiveError:
                raise
            except OSError as exc:
                raise ColdArchiveError(
                    f"could not prove protected state identity for {protected}: {exc}"
                ) from exc
    return source


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    source = reject_live_state_db(db_path)
    uri = source.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=1.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _connect_candidate(db_path: Path) -> sqlite3.Connection:
    source = reject_live_state_db(db_path)
    before = source.stat()
    conn = sqlite3.connect(str(source), isolation_level=None, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        after = source.stat()
        if not os.path.samestat(before, after):
            raise ColdArchiveError("candidate database identity changed during open")
        reject_live_state_db(source)
        return conn
    except BaseException:
        conn.close()
        raise


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _typed_value(value: Any) -> list[Any]:
    if value is None:
        return ["null", None]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, float):
        return ["real", value.hex()]
    if isinstance(value, int):
        return ["integer", str(value)]
    raise ColdArchiveError(
        f"unsupported SQLite value type in custody digest: {type(value)!r}"
    )


def _query_rows_digest(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
) -> str:
    """Hash rows with column names, SQLite types, duplicates, and no order ambiguity."""

    cursor = conn.execute(sql, tuple(params))
    columns = [str(item[0]) for item in cursor.description or ()]
    encoded_rows = [
        json.dumps(
            [_typed_value(value) for value in tuple(row)],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        for row in cursor.fetchall()
    ]
    encoded_rows.sort()
    digest = hashlib.sha256()
    header = json.dumps(columns, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    digest.update(len(header).to_bytes(8, "big"))
    digest.update(header)
    for row in encoded_rows:
        digest.update(len(row).to_bytes(8, "big"))
        digest.update(row)
    return digest.hexdigest()


def _logical_schema_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT type, name, tbl_name, COALESCE(sql, '')
        FROM sqlite_master
        ORDER BY type, name, tbl_name, COALESCE(sql, '')
        """
    ).fetchall()


def _logical_table_names(conn: sqlite3.Connection) -> list[str]:
    schema_rows = _logical_schema_rows(conn)
    virtual_roots = {
        str(row[1])
        for row in schema_rows
        if str(row[0]) == "table"
        and str(row[3]).lstrip().upper().startswith("CREATE VIRTUAL TABLE")
    }
    virtual_shadow_names = {
        root + suffix
        for root in virtual_roots
        for suffix in ("_data", "_idx", "_content", "_docsize", "_config")
    }
    table_names = [
        str(row[1])
        for row in schema_rows
        if str(row[0]) == "table"
        and not str(row[1]).startswith("sqlite_")
        and str(row[1]) not in virtual_shadow_names
    ]
    if _table_exists(conn, "sqlite_sequence"):
        table_names.append("sqlite_sequence")
    return sorted(set(table_names))


def _logical_snapshot_sha256(conn: sqlite3.Connection) -> str:
    """Bind approval to the complete logical SQLite snapshot, including FTS content."""

    schema_rows = _logical_schema_rows(conn)
    table_names = _logical_table_names(conn)

    digest = hashlib.sha256()
    metadata = {
        "schema": [tuple(row) for row in schema_rows],
        "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(conn.execute("PRAGMA application_id").fetchone()[0]),
    }
    encoded_metadata = json.dumps(
        metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    digest.update(len(encoded_metadata).to_bytes(8, "big"))
    digest.update(encoded_metadata)
    for table in table_names:
        table_name = table.encode("utf-8")
        table_digest = _query_rows_digest(
            conn, f"SELECT * FROM {_quote_identifier(table)}"
        ).encode("ascii")
        digest.update(len(table_name).to_bytes(8, "big"))
        digest.update(table_name)
        digest.update(table_digest)
    return digest.hexdigest()


def _require_archive_schema(conn: sqlite3.Connection) -> None:
    session_columns = _table_columns(conn, "sessions")
    missing = sorted(_REQUIRED_SESSION_COLUMNS - session_columns)
    if missing:
        raise ColdArchiveError(
            "cold archive schema cannot prove canonical pin/activity state; "
            f"missing sessions columns: {', '.join(missing)}"
        )
    message_columns = _table_columns(conn, "messages")
    required_messages = {"id", "session_id", "timestamp"}
    missing_messages = sorted(required_messages - message_columns)
    if missing_messages:
        raise ColdArchiveError(
            "cold archive schema is missing required messages columns: "
            + ", ".join(missing_messages)
        )
    fts_objects = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'messages_fts%'"
        )
    }
    missing_fts = sorted(_REQUIRED_FTS_OBJECTS - fts_objects)
    if missing_fts:
        raise ColdArchiveError(
            "cold archive schema cannot prove required FTS roots/triggers: "
            + ", ".join(missing_fts)
        )


def _count_where(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    return int(conn.execute(sql, tuple(params)).fetchone()[0])


def _placeholders(values: Sequence[Any]) -> str:
    if not values:
        raise ColdArchiveError("internal error: empty placeholder list")
    return ",".join("?" for _ in values)


def _session_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    _require_archive_schema(conn)
    rows = conn.execute(
        """
        SELECT s.*,
               (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id)
                   AS actual_message_last_active,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
                   AS actual_message_count
        FROM sessions s
        ORDER BY s.started_at ASC, s.id ASC
        """
    ).fetchall()
    payload: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        session_id = str(row.get("id") or "")
        values = [
            _finite_epoch(row.get("started_at"), f"session {session_id} started_at")
        ]
        message_last_active = row.get("actual_message_last_active")
        if message_last_active is not None:
            values.append(
                _finite_epoch(
                    message_last_active,
                    f"session {session_id} latest message timestamp",
                )
            )
        durable = row.get("last_activity_at")
        if durable is not None:
            values.append(
                _finite_epoch(durable, f"session {session_id} last_activity_at")
            )
        row["actual_last_active"] = max(values)
        payload[str(row["id"])] = row
    return payload


def _parent_edges(rows: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for sid, row in rows.items():
        parent = row.get("parent_session_id")
        if parent and str(parent) in rows:
            edges.append((sid, str(parent)))
    return edges


def _connected_components(rows: dict[str, dict[str, Any]]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = {sid: set() for sid in rows}
    for child, parent in _parent_edges(rows):
        adjacency[child].add(parent)
        adjacency[parent].add(child)
    components: list[list[str]] = []
    seen: set[str] = set()
    for sid in sorted(rows):
        if sid in seen:
            continue
        stack = [sid]
        component: list[str] = []
        seen.add(sid)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(
            sorted(
                component, key=lambda item: (rows[item].get("started_at") or 0, item)
            )
        )
    return components


def _matches_holds(
    row: dict[str, Any],
    *,
    hold_sources: set[str],
    hold_title_regexes: Sequence[re.Pattern[str]],
    hold_cwd_prefixes: Sequence[str],
) -> list[str]:
    reasons: list[str] = []
    source = str(row.get("source") or "").lower()
    if source in hold_sources:
        reasons.append(f"permanent_hold_source:{source}")
    title = str(row.get("title") or "")
    for pattern in hold_title_regexes:
        if pattern.search(title):
            reasons.append(f"permanent_hold_title_regex:{pattern.pattern}")
    cwd = str(row.get("cwd") or "")
    for prefix in hold_cwd_prefixes:
        if cwd == prefix or cwd.startswith(prefix.rstrip("/") + "/"):
            reasons.append(f"permanent_hold_cwd_prefix:{prefix}")
    return reasons


def _table_has_any_reference(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    ids: Sequence[str],
) -> bool:
    if not ids or column not in _table_columns(conn, table):
        return False
    sql = f'SELECT 1 FROM "{table}" WHERE "{column}" IN ({_placeholders(ids)}) LIMIT 1'
    return conn.execute(sql, tuple(ids)).fetchone() is not None


def _gateway_routing_references(conn: sqlite3.Connection, ids: Sequence[str]) -> bool:
    if not ids or "entry_json" not in _table_columns(conn, "gateway_routing"):
        return False
    return any(
        conn.execute(
            "SELECT 1 FROM gateway_routing WHERE entry_json LIKE ? LIMIT 1",
            (f"%{sid}%",),
        ).fetchone()
        is not None
        for sid in ids
    )


def _async_delegation_obligations(
    conn: sqlite3.Connection, ids: Sequence[str]
) -> list[dict[str, Any]]:
    if not ids or not _table_exists(conn, "async_delegations"):
        return []
    columns = _table_columns(conn, "async_delegations")
    checks: list[str] = []
    params: list[str] = []
    for column in ("origin_session", "parent_session_id"):
        if column in columns:
            checks.append(f'"{column}" IN ({_placeholders(ids)})')
            params.extend(ids)
    if not checks:
        return []
    select_columns = [
        column
        for column in ("delegation_id", "state", "delivery_state")
        if column in columns
    ]
    if not select_columns:
        return [{"reference": True}]
    rows = conn.execute(
        "SELECT "
        + ", ".join(f'"{column}"' for column in select_columns)
        + " FROM async_delegations WHERE "
        + " OR ".join(f"({clause})" for clause in checks),
        tuple(params),
    ).fetchall()
    obligations: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        state = str(row.get("state") or "").lower()
        delivery = str(row.get("delivery_state") or "").lower()
        if (
            state not in _TERMINAL_DELEGATION_STATES
            or delivery not in _TERMINAL_DELIVERY_STATES
        ):
            obligations.append(row)
        else:
            # Gate-B is stricter than runtime completion: any durable reference
            # still requires an operator decision before deletion.
            obligations.append(row)
    return obligations


def _ids_digest(ids: Iterable[str]) -> str:
    payload = "".join(f"{sid}\n" for sid in sorted(ids)).encode("utf-8")
    return _sha256_bytes(payload)


def _reason_counts(skipped_groups: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for group in skipped_groups:
        for reason in group.get("reasons") or []:
            key = str(reason)
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _manifest_digest(manifest: dict[str, Any]) -> str:
    public = {
        key: value
        for key, value in manifest.items()
        if not key.startswith("_") and key != "manifest_sha256"
    }
    return _sha256_bytes(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def build_gate_b_manifest(
    source_db: Path,
    *,
    now: float | None = None,
    hot_days: float = DEFAULT_HOT_DAYS,
    archive_grace_days: float = DEFAULT_ARCHIVE_GRACE_DAYS,
    hold_sources: Iterable[str] = DEFAULT_PERMANENT_HOLD_SOURCES,
    hold_title_regexes: Iterable[str] = (),
    hold_cwd_prefixes: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a redacted manifest from an offline, schema-provable snapshot."""

    hot_days = _finite_epoch(hot_days, "hot_days")
    archive_grace_days = _finite_epoch(archive_grace_days, "archive_grace_days")
    if hot_days < 0 or archive_grace_days < 0:
        raise ColdArchiveError("hot_days and archive_grace_days must be non-negative")
    generated_at = _finite_epoch(time.time() if now is None else now, "now")
    cutoff = generated_at - (hot_days + archive_grace_days) * 86_400.0
    hot_cutoff = generated_at - hot_days * 86_400.0
    source = reject_live_state_db(source_db)
    source_info = source.stat()
    compiled_holds = [re.compile(pattern) for pattern in hold_title_regexes]
    normalized_sources = {str(value).lower() for value in hold_sources}
    normalized_cwds = [str(value) for value in hold_cwd_prefixes]

    with _connect_readonly(source) as conn:
        conn.execute("BEGIN")
        rows = _session_rows(conn)
        components = _connected_components(rows)
        selected_groups: list[dict[str, Any]] = []
        skipped_groups: list[dict[str, Any]] = []
        selected_ids: list[str] = []
        selected_message_total = 0
        for index, component in enumerate(components, start=1):
            reasons: set[str] = set()
            group_message_count = sum(
                int(rows[sid].get("actual_message_count") or 0) for sid in component
            )
            last_active_values = [
                float(rows[sid]["actual_last_active"]) for sid in component
            ]
            for sid in component:
                row = rows[sid]
                if row.get("ended_at") is None:
                    reasons.add("open_session")
                if int(row.get("archived") or 0) != 1:
                    reasons.add("not_archived")
                if row.get("pinned") is None:
                    reasons.add("pin_state_unprovable")
                elif int(row.get("pinned") or 0) != 0:
                    reasons.add("pinned")
                if row.get("last_activity_at") is None:
                    reasons.add("canonical_last_activity_unprovable")
                # Exactly on the 37-day default boundary is eligible.
                if float(row["actual_last_active"]) > cutoff:
                    reasons.add("inside_30d_hot_plus_7d_grace")
                reasons.update(
                    _matches_holds(
                        row,
                        hold_sources=normalized_sources,
                        hold_title_regexes=compiled_holds,
                        hold_cwd_prefixes=normalized_cwds,
                    )
                )
            if _async_delegation_obligations(conn, component):
                reasons.add("async_delegation_reference")
            if _gateway_routing_references(conn, component):
                reasons.add("gateway_routing_reference")
            if _table_has_any_reference(
                conn, "compression_locks", "session_id", component
            ):
                reasons.add("compression_lock_reference")

            summary = {
                "group_number": index,
                "group_sha256": _ids_digest(component),
                "session_count": len(component),
                "actual_message_count": group_message_count,
                "oldest_last_active": min(last_active_values)
                if last_active_values
                else None,
                "newest_last_active": max(last_active_values)
                if last_active_values
                else None,
                "sources": sorted({
                    str(rows[sid].get("source") or "") for sid in component
                }),
            }
            if reasons:
                skipped_groups.append({**summary, "reasons": sorted(reasons)})
            else:
                selected_groups.append(summary)
                selected_ids.extend(component)
                selected_message_total += group_message_count

        parent_map = {
            sid: str(row["parent_session_id"]) if row.get("parent_session_id") else None
            for sid, row in rows.items()
        }
        source_after = source.stat()
        if not os.path.samestat(source_info, source_after):
            raise ColdArchiveError(
                "candidate database identity changed during manifest build"
            )
        source_file_sha256 = sha256_path(source)
        source_logical_sha256 = _logical_snapshot_sha256(conn)
        manifest: dict[str, Any] = {
            "archive_manifest_version": _ARCHIVE_VERSION,
            "operation": "hermes-state-cold-archive-gate-b",
            "generated_at": utc_iso(generated_at),
            "generated_at_epoch": generated_at,
            "source_db_name": _ROLLBACK_MEMBER_NAMES[""],
            "source_db_bytes": source_info.st_size,
            "source_db_sha256": source_file_sha256,
            "source_logical_sha256": source_logical_sha256,
            "source_device": source_info.st_dev,
            "source_inode": source_info.st_ino,
            "schema_contract": {
                "required_sessions_columns": sorted(_REQUIRED_SESSION_COLUMNS),
                "canonical_activity": "max(sessions.last_activity_at,messages.timestamp,sessions.started_at)",
                "missing_pin_or_activity_schema": "fail-closed",
            },
            "policy": {
                "hot_days": float(hot_days),
                "archive_grace_days": float(archive_grace_days),
                "cold_cutoff_epoch": cutoff,
                "cold_cutoff_utc": utc_iso(cutoff),
                "hot_cutoff_epoch": hot_cutoff,
                "hot_cutoff_utc": utc_iso(hot_cutoff),
                "cold_boundary_inclusive": True,
                "must_be_ended": True,
                "must_be_archived": True,
                "must_be_unpinned": True,
                "must_have_canonical_last_activity": True,
                "must_select_whole_parent_child_component": True,
                "actual_messages_rows_not_sessions_message_count": True,
                "default_permanent_hold_sources": sorted(normalized_sources),
                "hold_title_regexes": [pattern.pattern for pattern in compiled_holds],
                "hold_cwd_prefixes": normalized_cwds,
            },
            "counts": {
                "sessions_total": len(rows),
                "messages_total_actual": _count_where(
                    conn, "SELECT COUNT(*) FROM messages"
                ),
                "lineage_groups_total": len(components),
                "selected_lineage_groups": len(selected_groups),
                "selected_sessions": len(selected_ids),
                "selected_messages_actual": selected_message_total,
                "skipped_lineage_groups": len(skipped_groups),
            },
            "selected_ids_sha256": _ids_digest(selected_ids),
            "selected_groups": selected_groups,
            "skipped_group_reason_counts": _reason_counts(skipped_groups),
            "parent_map_sha256": _sha256_bytes(
                json.dumps(parent_map, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ),
        }
        manifest["restricted_ids_sha256"] = manifest["selected_ids_sha256"]
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        return {
            **manifest,
            "_restricted_selected_ids": sorted(selected_ids),
            "_restricted_parent_map": parent_map,
        }


def write_gate_b_manifest(stage_root: Path, manifest: dict[str, Any]) -> StageArtifacts:
    """Create a brand-new stage and write Gate-B artifacts exactly once."""

    stage = _create_stage(stage_root)
    public = {key: value for key, value in manifest.items() if not key.startswith("_")}
    _exclusive_write_json(stage.manifest_path, public)
    ids = [str(sid) for sid in manifest.get("_restricted_selected_ids") or []]
    _exclusive_write_json(stage.restricted_ids_path, {"selected_ids": ids})
    _exclusive_write_json(
        stage.restricted_groups_path,
        {
            "selected_ids_sha256": public.get("selected_ids_sha256"),
            "parent_map_sha256": public.get("parent_map_sha256"),
            "parent_map": manifest.get("_restricted_parent_map") or {},
        },
    )
    return stage


def load_approved_gate_b_manifest(
    stage_root: Path,
    *,
    approved_manifest_path: Path,
    approved_manifest_sha256: str,
) -> tuple[StageArtifacts, dict[str, Any]]:
    """Load exact approved bytes from an existing stage without mutating it."""

    if not re.fullmatch(r"[0-9a-f]{64}", approved_manifest_sha256 or ""):
        raise ColdArchiveError(
            "approved manifest sha256 must be 64 lowercase hex characters"
        )
    stage = _load_stage(stage_root)
    expected_path, expected_bytes = _read_private_bytes(stage.manifest_path)
    supplied_path, supplied_bytes = _read_frozen_approval_bytes(
        stage,
        approved_manifest_path,
        expected_path,
        label="approved Gate-B manifest",
    )
    if _sha256_bytes(supplied_bytes) != approved_manifest_sha256:
        raise ColdArchiveError("approved Gate-B manifest exact-byte sha256 mismatch")
    if supplied_bytes != expected_bytes:
        raise ColdArchiveError(
            "externally approved manifest bytes differ from the stage manifest"
        )
    manifest = _decode_json_object(supplied_bytes, supplied_path)
    if manifest.get("archive_manifest_version") != _ARCHIVE_VERSION:
        raise ColdArchiveError("unsupported Gate-B manifest version")
    if _manifest_digest(manifest) != manifest.get("manifest_sha256"):
        raise ColdArchiveError("embedded Gate-B manifest digest mismatch")

    ids_payload = _load_private_json(stage.restricted_ids_path)
    raw_ids = ids_payload.get("selected_ids")
    if not isinstance(raw_ids, list) or any(
        not isinstance(value, str) for value in raw_ids
    ):
        raise ColdArchiveError("restricted selected IDs artifact is invalid")
    if len(raw_ids) != len(set(raw_ids)):
        raise ColdArchiveError("restricted selected IDs contain duplicate rows")
    ids = sorted(raw_ids)
    if _ids_digest(ids) != manifest.get("selected_ids_sha256"):
        raise ColdArchiveError("restricted selected IDs do not match approved manifest")
    if len(ids) != int(manifest.get("counts", {}).get("selected_sessions", -1)):
        raise ColdArchiveError(
            "restricted selected ID count does not match approved manifest"
        )

    groups = _load_private_json(stage.restricted_groups_path)
    parent_map = groups.get("parent_map")
    if not isinstance(parent_map, dict):
        raise ColdArchiveError("restricted parent map is invalid")
    parent_digest = _sha256_bytes(
        json.dumps(parent_map, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if parent_digest != manifest.get("parent_map_sha256"):
        raise ColdArchiveError("restricted parent map does not match approved manifest")
    if groups.get("selected_ids_sha256") != manifest.get("selected_ids_sha256"):
        raise ColdArchiveError("restricted parent map selected-ID digest mismatch")
    return stage, {
        **manifest,
        "_restricted_selected_ids": ids,
        "_restricted_parent_map": parent_map,
    }


def load_approved_producer_receipt(
    stage: StageArtifacts,
    manifest: dict[str, Any],
    approved_receipt_path: Path,
    approved_receipt_sha256: str,
) -> dict[str, Any]:
    """Load the exact externally frozen producer proof from one stable snapshot."""

    if not re.fullmatch(r"[0-9a-f]{64}", approved_receipt_sha256 or ""):
        raise ColdArchiveError(
            "approved producer receipt sha256 must be 64 lowercase hex characters"
        )
    stage_receipt_path, stage_bytes = _read_private_bytes(stage.producer_receipt_path)
    supplied_path, supplied_bytes = _read_frozen_approval_bytes(
        stage,
        approved_receipt_path,
        stage_receipt_path,
        label="approved producer receipt",
    )
    if _sha256_bytes(supplied_bytes) != approved_receipt_sha256:
        raise ColdArchiveError("approved producer receipt exact-byte sha256 mismatch")
    if supplied_bytes != stage_bytes:
        raise ColdArchiveError(
            "externally approved producer receipt differs from the stage receipt"
        )
    receipt = _decode_json_object(supplied_bytes, supplied_path)
    if (
        receipt.get("operation") != "hermes-state-cold-archive-producer"
        or receipt.get("manifest_only") is not False
        or receipt.get("producer_complete") is not True
        or receipt.get("gate_b_manifest_sha256") != manifest.get("manifest_sha256")
        or receipt.get("manifest_file_sha256") != sha256_path(stage.manifest_path)
    ):
        raise ColdArchiveError("approved producer receipt is incomplete or misbound")
    if not re.fullmatch(
        r"[0-9a-f]{64}", str(receipt.get("age_recipient_sha256") or "")
    ):
        raise ColdArchiveError("approved producer receipt lacks recipient fingerprint")
    recipient_snapshot = _require_private_file(stage.age_recipient_snapshot_path)
    if sha256_path(recipient_snapshot) != receipt.get("age_recipient_sha256"):
        raise ColdArchiveError("approved producer recipient snapshot changed")
    expected = (
        ("rollback_encrypted", stage.rollback_encrypted_path),
        ("restricted_encrypted", stage.restricted_encrypted_path),
    )
    local_hashes: dict[str, tuple[str, int]] = {}
    for key, raw_path in expected:
        path = _require_private_file(raw_path)
        report = receipt.get(key)
        if not isinstance(report, dict):
            raise ColdArchiveError(f"approved producer receipt lacks {key}")
        digest = sha256_path(path)
        size = path.stat().st_size
        if report.get("sha256") != digest or report.get("bytes") != size:
            raise ColdArchiveError(f"approved producer receipt {key} bytes changed")
        local_hashes[path.name] = (digest, size)
    reports = receipt.get("remote_publish")
    expected_remote_names = [
        stage.rollback_encrypted_path.name,
        stage.restricted_encrypted_path.name,
        stage.manifest_path.name,
    ]
    if not isinstance(reports, list) or len(reports) != len(expected_remote_names):
        raise ColdArchiveError("approved producer receipt lacks complete remote proof")
    local_hashes[stage.manifest_path.name] = (
        sha256_path(stage.manifest_path),
        stage.manifest_path.stat().st_size,
    )
    for expected_name, report in zip(expected_remote_names, reports):
        if not isinstance(report, dict):
            raise ColdArchiveError("approved producer remote proof is invalid")
        digest, size = local_hashes[expected_name]
        if (
            Path(str(report.get("local_path") or "")).name != expected_name
            or report.get("sha256") != digest
            or report.get("readback_sha256") != digest
            or report.get("bytes") != size
            or report.get("integrity") != "rclone-checksum-and-readback-ok"
        ):
            raise ColdArchiveError("approved producer remote proof is misbound")
    return receipt


def _exclusive_copy(source: Path, destination: Path) -> Path:
    source = _resolve_existing_file(source)
    with source.open("rb") as handle:
        return _exclusive_write_bytes(destination, handle.read())


def _copy_rollback_bundle(
    source_db: Path,
    stage: StageArtifacts,
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = reject_live_state_db(source_db)
    bundle_root = _create_private_dir(stage.rollback_dir / "source-bundle")
    copied: list[dict[str, Any]] = []
    for suffix in _SIDECAR_SUFFIXES:
        part = source if not suffix else source.with_name(source.name + suffix)
        member_name = _ROLLBACK_MEMBER_NAMES[suffix]
        if not part.exists():
            copied.append({"name": member_name, "status": "absent"})
            continue
        try:
            part_info = part.lstat()
        except OSError as exc:
            raise ColdArchiveError(
                f"could not stat rollback source part {part}: {exc}"
            ) from exc
        if stat.S_ISLNK(part_info.st_mode) or not stat.S_ISREG(part_info.st_mode):
            raise ColdArchiveError(
                f"rollback source part is not a regular file: {part}"
            )
        destination = _exclusive_copy(part, bundle_root / member_name)
        copied.append({
            "name": destination.name,
            "status": "copied",
            "bytes": destination.stat().st_size,
            "sha256": sha256_path(destination),
            "mode": "0600",
        })
    created_epoch = time.time() if now is None else float(now)
    bundle_manifest_path = _exclusive_write_json(
        bundle_root / "ROLLBACK-BUNDLE-MANIFEST.json",
        {
            "created_at": utc_iso(created_epoch),
            "created_at_epoch": created_epoch,
            "source_db_name": _ROLLBACK_MEMBER_NAMES[""],
            "files": copied,
        },
    )
    try:
        with tarfile.open(stage.rollback_bundle_path, "x:gz") as archive:
            for path in sorted(bundle_root.iterdir(), key=lambda item: item.name):
                if path.is_symlink() or not path.is_file():
                    raise ColdArchiveError(f"unsafe rollback bundle member: {path}")
                archive.add(path, arcname=path.name, recursive=False)
    except FileExistsError as exc:
        raise ColdArchiveError(
            f"refusing to overwrite rollback bundle: {stage.rollback_bundle_path}"
        ) from exc
    os.chmod(stage.rollback_bundle_path, 0o600)
    report = {
        "path": str(stage.rollback_bundle_path),
        "sha256": sha256_path(stage.rollback_bundle_path),
        "bytes": stage.rollback_bundle_path.stat().st_size,
        "manifest_path": str(bundle_manifest_path),
        "files": copied,
    }
    policy = {
        "operation": "hermes-source-bundle-retention-policy",
        "created_at": utc_iso(created_epoch),
        "created_at_epoch": created_epoch,
        "state": "awaiting-cutover",
        "minimum_retention_seconds_after_cutover": DEFAULT_SOURCE_BUNDLE_RETENTION_SECONDS,
        "rollback_bundle_sha256": report["sha256"],
    }
    return report, policy


def _safe_filename_component(raw: str, *, fallback: str, limit: int) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", raw)
    value = value.strip(" ._-")
    return (value or fallback)[:limit].rstrip(" ._-") or fallback


def _verify_rollback_bundle_snapshot(
    stage: StageArtifacts, manifest: dict[str, Any]
) -> None:
    bundled_db = _require_private_file(
        stage.rollback_dir / "source-bundle" / _ROLLBACK_MEMBER_NAMES[""]
    )
    if sha256_path(bundled_db) != manifest.get("source_db_sha256"):
        raise ColdArchiveError(
            "rollback bundle main database differs from approved bytes"
        )
    with _connect_readonly(bundled_db) as conn:
        if _logical_snapshot_sha256(conn) != manifest.get("source_logical_sha256"):
            raise ColdArchiveError(
                "rollback bundle logical state differs from approved candidate"
            )
        integrity = [
            str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()
        ]
        if integrity != ["ok"]:
            raise ColdArchiveError(
                "rollback bundle integrity check did not return exactly ok"
            )
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ColdArchiveError("rollback bundle has foreign-key violations")


def _safe_qmd_filename(session: dict[str, Any]) -> str:
    raw_id = str(session.get("id") or "")
    digest = _sha256_bytes(raw_id.encode("utf-8"))[:16]
    safe_id = _safe_filename_component(raw_id, fallback="session", limit=48)
    title = _safe_filename_component(
        str(session.get("title") or "untitled"), fallback="untitled", limit=64
    )
    return f"{safe_id}-{digest}-{title}.qmd"


def _load_session_export(
    conn: sqlite3.Connection, session_ids: Sequence[str]
) -> dict[str, Any]:
    if not session_ids:
        raise ColdArchiveError("cannot export an empty lineage group")
    rows = conn.execute(
        f"SELECT * FROM sessions WHERE id IN ({_placeholders(session_ids)}) "
        "ORDER BY started_at ASC, id ASC",
        tuple(session_ids),
    ).fetchall()
    if len(rows) != len(session_ids):
        raise ColdArchiveError("QMD export lineage changed after manifest")
    segments: list[dict[str, Any]] = []
    total_messages = 0
    for raw in rows:
        segment = dict(raw)
        messages = [
            dict(message)
            for message in conn.execute(
                """
                SELECT id, session_id, role, content, tool_call_id, tool_calls,
                       tool_name, timestamp, token_count, finish_reason
                FROM messages WHERE session_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (raw["id"],),
            ).fetchall()
        ]
        segment["messages"] = messages
        segment["message_count"] = len(messages)
        total_messages += len(messages)
        session_id = str(segment.get("id") or "")
        segment["last_active"] = max(
            _finite_epoch(segment["started_at"], f"session {session_id} started_at"),
            _finite_epoch(
                segment["last_activity_at"],
                f"session {session_id} last_activity_at",
            ),
            max(
                (
                    _finite_epoch(
                        item["timestamp"],
                        f"session {session_id} message timestamp",
                    )
                    for item in messages
                ),
                default=_finite_epoch(
                    segment["started_at"], f"session {session_id} started_at"
                ),
            ),
        )
        segments.append(segment)
    base = dict(segments[0])
    base["segments"] = segments
    base["lineage_session_ids"] = [str(segment["id"]) for segment in segments]
    base["messages"] = [
        message for segment in segments for message in segment.get("messages", [])
    ]
    base["message_count"] = total_messages
    base["title"] = base.get("title") or f"cold archive group {base['id']}"
    return base


def export_redacted_qmd(
    source_db: Path,
    stage: StageArtifacts,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    ids = [str(value) for value in manifest.get("_restricted_selected_ids") or []]
    if not ids:
        return {"exported_files": [], "verified": True, "message_count": 0}
    groups = manifest.get("selected_groups") or []
    id_set = set(ids)
    exported: list[dict[str, Any]] = []
    total_messages = 0
    with _connect_readonly(source_db) as conn:
        components = _connected_components(_session_rows(conn))
        for component in components:
            if not set(component).issubset(id_set):
                continue
            session = _load_session_export(conn, component)
            redacted = redact_session_data(session)
            filename = _safe_qmd_filename(redacted)
            path = _safe_immediate_child(stage.qmd_dir, filename)
            text = render_session_markdown(redacted, fmt="qmd")
            _exclusive_write_text(path, text)
            ok, reason = verify_export_file(path, redacted)
            if not ok:
                raise ColdArchiveError(
                    f"QMD export verification failed for {path.name}: {reason}"
                )
            message_count = int(redacted.get("message_count") or 0)
            total_messages += message_count
            body = path.read_text(encoding="utf-8")
            exported.append({
                "path": str(path),
                "sha256": sha256_path(path),
                "bytes": path.stat().st_size,
                "lineage_session_ids_sha256": _ids_digest(component),
                "actual_message_count": message_count,
                "witness": {
                    "session_id_present": str(redacted["id"]) in body,
                    "message_count_marker": f"- Exported messages: `{message_count}`"
                    in body,
                },
            })
    if len(exported) != len(groups):
        raise ColdArchiveError(
            "QMD export count does not match selected lineage groups"
        )
    return {
        "exported_files": exported,
        "verified": True,
        "message_count": total_messages,
    }


def _build_restricted_packet(stage: StageArtifacts, qmd_report: dict[str, Any]) -> Path:
    members = [
        _require_private_file(stage.restricted_ids_path),
        _require_private_file(stage.restricted_groups_path),
    ] + [
        _require_private_file(Path(item["path"]))
        for item in qmd_report.get("exported_files") or []
    ]
    index = {
        "operation": "hermes-cold-archive-restricted-index",
        "files": [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_path(path),
            }
            for path in members
        ],
    }
    _exclusive_write_json(stage.restricted_index_path, index)
    members.append(_require_private_file(stage.restricted_index_path))
    clear_packet = stage.restricted_ids_path.parent / "restricted-cold-qmd.tar.gz"
    try:
        with tarfile.open(clear_packet, "x:gz") as archive:
            for path in members:
                archive.add(path, arcname=path.name, recursive=False)
    except FileExistsError as exc:
        raise ColdArchiveError(
            f"refusing to overwrite restricted packet: {clear_packet}"
        ) from exc
    os.chmod(clear_packet, 0o600)
    return clear_packet


def encrypt_file_with_age(
    source: Path,
    output: Path,
    *,
    recipient_file: Path,
    expected_recipient_sha256: str,
    age_exe: str = "age",
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    source = _require_private_file(source)
    recipient = _resolve_existing_file(recipient_file)
    if sha256_path(recipient) != expected_recipient_sha256:
        raise ColdArchiveError("age recipient snapshot hash mismatch before encryption")
    _require_private_dir(output.parent)
    if output.exists() or output.is_symlink():
        raise ColdArchiveError(f"refusing to overwrite encrypted artifact: {output}")
    partial = _safe_immediate_child(
        output.parent, f".{output.name}.{os.getpid()}.partial"
    )
    if partial.exists() or partial.is_symlink():
        raise ColdArchiveError(f"refusing existing age partial: {partial}")
    command = [age_exe, "-R", str(recipient), "-o", str(partial), str(source)]
    execute = subprocess.run if runner is None else runner
    result = execute(command, text=True, capture_output=True)
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        raise ColdArchiveError("age encryption failed")
    if sha256_path(recipient) != expected_recipient_sha256:
        partial.unlink(missing_ok=True)
        raise ColdArchiveError("age recipient snapshot changed during encryption")
    try:
        info = partial.lstat()
    except OSError as exc:
        raise ColdArchiveError(
            "age reported success without producing ciphertext"
        ) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size <= 0
    ):
        partial.unlink(missing_ok=True)
        raise ColdArchiveError("age produced an invalid ciphertext artifact")
    os.chmod(partial, 0o600)
    try:
        os.link(partial, output, follow_symlinks=False)
        partial.unlink()
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ColdArchiveError(
            f"could not finalize age ciphertext without clobber: {exc}"
        ) from exc
    return {
        "path": str(output),
        "sha256": sha256_path(output),
        "bytes": output.stat().st_size,
    }


def _run_rclone(
    command: list[str],
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> subprocess.CompletedProcess[str]:
    execute = subprocess.run if runner is None else runner
    return execute(command, text=True, capture_output=True)


def _require_secret_config(path: Path) -> tuple[Path, bytes]:
    config, data, _ = _read_stable_regular_bytes(
        path,
        label="rclone config",
        required_mode=0o600,
        require_current_owner=True,
        require_single_link=True,
    )
    return config, data


def _assert_same_file(
    path: Path,
    expected: os.stat_result,
    label: str,
    *,
    expected_sha256: str,
) -> None:
    current_path = _require_private_file(path)
    current = current_path.stat()
    if not os.path.samestat(expected, current):
        raise ColdArchiveError(f"{label} identity changed during operation")
    if sha256_path(current_path) != expected_sha256:
        raise ColdArchiveError(f"{label} bytes changed during operation")


def _validate_remote_namespace(remote_root: str, namespace: str) -> tuple[str, str]:
    remote_match = re.fullmatch(
        r"([A-Za-z0-9._-]+):([A-Za-z0-9._/-]*)", remote_root or ""
    )
    if remote_match is None or any(
        part == ".." for part in remote_match.group(2).split("/")
    ):
        raise ColdArchiveError("invalid rclone remote root")
    if (
        not namespace
        or ".." in namespace
        or namespace.startswith("/")
        or re.search(r"[\r\n\x00]", namespace)
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", namespace)
    ):
        raise ColdArchiveError("invalid remote namespace")
    return remote_root.rstrip("/"), namespace.rstrip("/")


def publish_paths_with_rclone(
    paths: Sequence[Path],
    *,
    remote_root: str,
    rclone_config: Path,
    namespace: str,
    rclone_exe: str = "rclone",
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[dict[str, Any]]:
    """Copy fixed local files immutably and prove checksum plus exact readback."""

    remote_base, namespace = _validate_remote_namespace(remote_root, namespace)
    _, config_bytes = _require_secret_config(rclone_config)
    published: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="hermes-cold-archive-rclone-") as temp_name:
        temp = Path(temp_name)
        config = _exclusive_write_bytes(temp / "rclone.conf", config_bytes)
        config_identity = config.stat()
        config_sha256 = sha256_path(config)
        remote_directory = f"{remote_base}/{namespace}"
        for raw in paths:
            local = _require_private_file(Path(raw))
            if not re.fullmatch(r"[A-Za-z0-9._-]+", local.name):
                raise ColdArchiveError(f"unsafe remote object name: {local.name}")
            remote = f"{remote_directory}/{local.name}"
            local_sha = sha256_path(local)
            _assert_same_file(
                config,
                config_identity,
                "frozen rclone config",
                expected_sha256=config_sha256,
            )
            # rclone copy (directory destination) enforces --immutable. copyto
            # replaces existing objects on supported backends despite that flag.
            upload = _run_rclone(
                [
                    rclone_exe,
                    "copy",
                    str(local),
                    remote_directory,
                    "--immutable",
                    "--config",
                    str(config),
                ],
                runner,
            )
            if upload.returncode != 0:
                raise ColdArchiveError(f"rclone immutable copy failed for {local.name}")
            _assert_same_file(
                config,
                config_identity,
                "frozen rclone config",
                expected_sha256=config_sha256,
            )
            check = _run_rclone(
                [
                    rclone_exe,
                    "check",
                    str(local),
                    remote,
                    "--checksum",
                    "--one-way",
                    "--config",
                    str(config),
                ],
                runner,
            )
            if check.returncode != 0:
                raise ColdArchiveError(
                    f"rclone checksum verification failed for {local.name}"
                )
            readback = temp / local.name
            _assert_same_file(
                config,
                config_identity,
                "frozen rclone config",
                expected_sha256=config_sha256,
            )
            pull = _run_rclone(
                [rclone_exe, "copyto", remote, str(readback), "--config", str(config)],
                runner,
            )
            if pull.returncode != 0:
                raise ColdArchiveError(f"rclone readback failed for {local.name}")
            if not readback.exists() or readback.stat().st_size != local.stat().st_size:
                raise ColdArchiveError(
                    f"rclone readback size mismatch for {local.name}"
                )
            readback_sha = sha256_path(readback)
            if readback_sha != local_sha:
                raise ColdArchiveError(
                    f"rclone readback sha256 mismatch for {local.name}"
                )
            published.append({
                "local_path": str(local),
                "remote": remote,
                "bytes": local.stat().st_size,
                "sha256": local_sha,
                "readback_sha256": readback_sha,
                "integrity": "rclone-checksum-and-readback-ok",
            })
    return published


def _verify_remote_custody(
    stage: StageArtifacts,
    producer_receipt: dict[str, Any],
    *,
    rclone_config: Path,
    rclone_exe: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> list[dict[str, Any]]:
    expected_paths = [
        stage.rollback_encrypted_path,
        stage.restricted_encrypted_path,
        stage.manifest_path,
    ]
    reports = producer_receipt.get("remote_publish")
    if not isinstance(reports, list) or len(reports) != len(expected_paths):
        raise ColdArchiveError(
            "producer receipt lacks complete encrypted offsite custody"
        )
    _, config_bytes = _require_secret_config(rclone_config)
    verified: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="hermes-cold-archive-verify-") as temp_name:
        temp = Path(temp_name)
        config = _exclusive_write_bytes(temp / "rclone.conf", config_bytes)
        config_identity = config.stat()
        config_sha256 = sha256_path(config)
        for local_path, report in zip(expected_paths, reports):
            local = _require_private_file(local_path)
            if not isinstance(report, dict):
                raise ColdArchiveError("producer remote receipt entry is invalid")
            if Path(str(report.get("local_path") or "")).name != local.name:
                raise ColdArchiveError("producer remote receipt local object mismatch")
            local_sha = sha256_path(local)
            if (
                report.get("sha256") != local_sha
                or report.get("readback_sha256") != local_sha
            ):
                raise ColdArchiveError("producer remote receipt hash mismatch")
            if report.get("integrity") != "rclone-checksum-and-readback-ok":
                raise ColdArchiveError("producer remote receipt is not verified")
            remote = str(report.get("remote") or "")
            if re.search(r"[\r\n\x00]", remote) or not remote.endswith(
                "/" + local.name
            ):
                raise ColdArchiveError("producer remote receipt path is invalid")
            _assert_same_file(
                config,
                config_identity,
                "frozen rclone config",
                expected_sha256=config_sha256,
            )
            check = _run_rclone(
                [
                    rclone_exe,
                    "check",
                    str(local),
                    remote,
                    "--checksum",
                    "--one-way",
                    "--config",
                    str(config),
                ],
                runner,
            )
            if check.returncode != 0:
                raise ColdArchiveError(
                    f"remote custody checksum failed for {local.name}"
                )
            readback = temp / local.name
            _assert_same_file(
                config,
                config_identity,
                "frozen rclone config",
                expected_sha256=config_sha256,
            )
            pull = _run_rclone(
                [rclone_exe, "copyto", remote, str(readback), "--config", str(config)],
                runner,
            )
            if pull.returncode != 0:
                raise ColdArchiveError(
                    f"remote custody readback failed for {local.name}"
                )
            if not readback.exists() or readback.stat().st_size != local.stat().st_size:
                raise ColdArchiveError(f"remote custody size mismatch for {local.name}")
            if sha256_path(readback) != local_sha:
                raise ColdArchiveError(
                    f"remote custody sha256 mismatch for {local.name}"
                )
            verified.append({"remote": remote, "sha256": local_sha, "verified": True})
    return verified


def _effective_hot_ids(conn: sqlite3.Connection, hot_cutoff: float) -> list[str]:
    rows = _session_rows(conn)
    return sorted(
        sid
        for sid, row in rows.items()
        if float(row["actual_last_active"]) >= hot_cutoff
    )


def _table_rows_digest(
    conn: sqlite3.Connection,
    table: str,
    *,
    where: str = "",
    params: Sequence[Any] = (),
) -> dict[str, Any]:
    quoted = _quote_identifier(table)
    return {
        "count": _count_where(conn, f"SELECT COUNT(*) FROM {quoted} {where}", params),
        "sha256": _query_rows_digest(conn, f"SELECT * FROM {quoted} {where}", params),
    }


def _capture_payload_survivors(
    conn: sqlite3.Connection,
    *,
    selected_session_ids: Sequence[str],
    prompt_hashes_to_delete: Sequence[str],
) -> dict[str, Any]:
    """Capture every full row outside the documented deletion scope."""

    relation_columns = {
        "sessions": "id",
        "messages": "session_id",
        "compression_locks": "session_id",
        "telegram_dm_topic_bindings": "session_id",
        "session_model_usage": "session_id",
    }
    payload: dict[str, Any] = {}
    for table in _logical_table_names(conn):
        if table in {"messages_fts", "messages_fts_trigram"}:
            continue
        if table == "system_prompts" and "hash" in _table_columns(conn, table):
            if prompt_hashes_to_delete:
                where = f"WHERE hash NOT IN ({_placeholders(prompt_hashes_to_delete)})"
                payload[table] = _table_rows_digest(
                    conn, table, where=where, params=prompt_hashes_to_delete
                )
            else:
                payload[table] = _table_rows_digest(conn, table)
            continue
        relation = relation_columns.get(table)
        if (
            relation
            and relation in _table_columns(conn, table)
            and selected_session_ids
        ):
            where = (
                f"WHERE {_quote_identifier(relation)} NOT IN "
                f"({_placeholders(selected_session_ids)})"
            )
            payload[table] = _table_rows_digest(
                conn, table, where=where, params=selected_session_ids
            )
        else:
            payload[table] = _table_rows_digest(conn, table)
    return payload


def _capture_invariants(
    conn: sqlite3.Connection,
    *,
    hot_cutoff: float,
    selected_session_ids: Sequence[str] = (),
    prompt_hashes_to_delete: Sequence[str] = (),
) -> dict[str, Any]:
    _require_archive_schema(conn)
    return {
        "sessions": _count_where(conn, "SELECT COUNT(*) FROM sessions"),
        "messages": _count_where(conn, "SELECT COUNT(*) FROM messages"),
        "open_ids": sorted(
            str(row[0])
            for row in conn.execute("SELECT id FROM sessions WHERE ended_at IS NULL")
        ),
        "pinned_ids": sorted(
            str(row[0])
            for row in conn.execute("SELECT id FROM sessions WHERE pinned = 1")
        ),
        "hot_ids": _effective_hot_ids(conn, hot_cutoff),
        "parent_map": {
            str(row[0]): str(row[1]) if row[1] is not None else None
            for row in conn.execute(
                "SELECT id, parent_session_id FROM sessions ORDER BY id"
            )
        },
        "payload_survivors": _capture_payload_survivors(
            conn,
            selected_session_ids=selected_session_ids,
            prompt_hashes_to_delete=prompt_hashes_to_delete,
        ),
    }


def _excluded_message_ids(
    conn: sqlite3.Connection, selected_session_ids: Sequence[str]
) -> list[int]:
    if not selected_session_ids:
        return []
    return [
        int(row[0])
        for row in conn.execute(
            f"SELECT id FROM messages WHERE session_id IN ({_placeholders(selected_session_ids)})",
            tuple(selected_session_ids),
        )
    ]


def _survivor_message_digest(
    conn: sqlite3.Connection, selected_session_ids: Sequence[str]
) -> dict[str, Any]:
    if selected_session_ids:
        where = f"WHERE session_id NOT IN ({_placeholders(selected_session_ids)})"
        params: Sequence[Any] = tuple(selected_session_ids)
    else:
        where = ""
        params = ()
    return _table_rows_digest(conn, "messages", where=where, params=params)


def _fts_survivor_content_digest(
    conn: sqlite3.Connection, table: str, excluded_message_ids: Sequence[int]
) -> dict[str, Any] | None:
    if not _table_exists(conn, table):
        return None
    if excluded_message_ids:
        where = f"WHERE rowid NOT IN ({_placeholders(excluded_message_ids)})"
        params: Sequence[Any] = tuple(excluded_message_ids)
    else:
        where = ""
        params = ()
    return {
        "count": _count_where(conn, f'SELECT COUNT(*) FROM "{table}" {where}', params),
        "sha256": _query_rows_digest(
            conn, f'SELECT rowid, * FROM "{table}" {where}', params
        ),
    }


def _capture_search_survivor_invariants(
    conn: sqlite3.Connection, selected_session_ids: Sequence[str]
) -> dict[str, Any]:
    excluded = _excluded_message_ids(conn, selected_session_ids)
    payload: dict[str, Any] = {
        "messages": _survivor_message_digest(conn, selected_session_ids)
    }
    for table in ("messages_fts", "messages_fts_trigram"):
        digest = _fts_survivor_content_digest(conn, table, excluded)
        if digest is not None:
            payload[table] = digest
    return payload


def _verify_fts_counts(
    conn: sqlite3.Connection, *, deleted_message_ids: Sequence[int]
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    message_count = _count_where(conn, "SELECT COUNT(*) FROM messages")
    checks["messages"] = message_count
    for table, expected in (
        ("messages_fts", message_count),
        ("messages_fts_trigram", message_count),
    ):
        if not _table_exists(conn, table):
            continue
        actual = _count_where(conn, f'SELECT COUNT(*) FROM "{table}"')
        checks[table] = actual
        if actual != expected:
            raise ColdArchiveError(f"{table} count does not match its source messages")
        if deleted_message_ids:
            residue = _count_where(
                conn,
                f'SELECT COUNT(*) FROM "{table}" WHERE rowid IN ({_placeholders(deleted_message_ids)})',
                deleted_message_ids,
            )
            if residue:
                raise ColdArchiveError(f"{table} still contains deleted message rowids")
    return checks


def _revalidate_selected_under_lock(
    conn: sqlite3.Connection,
    manifest: dict[str, Any],
    ids: Sequence[str],
) -> int:
    if not conn.in_transaction:
        raise ColdArchiveError(
            "destructive revalidation must run under BEGIN IMMEDIATE"
        )
    if _logical_snapshot_sha256(conn) != manifest.get("source_logical_sha256"):
        raise ColdArchiveError(
            "candidate logical snapshot changed before destructive revalidation"
        )
    rows = _session_rows(conn)
    id_set = set(ids)
    if sorted(id_set & set(rows)) != sorted(ids):
        raise ColdArchiveError(
            "candidate no longer contains every approved selected session"
        )
    cutoff = float(manifest["policy"]["cold_cutoff_epoch"])
    hold_sources = {
        str(value).lower()
        for value in manifest["policy"].get("default_permanent_hold_sources") or []
    }
    hold_title_regexes = [
        re.compile(str(value))
        for value in manifest["policy"].get("hold_title_regexes") or []
    ]
    hold_cwd_prefixes = [
        str(value) for value in manifest["policy"].get("hold_cwd_prefixes") or []
    ]
    for sid in ids:
        row = rows[sid]
        if row.get("ended_at") is None:
            raise ColdArchiveError("approved selected session became open")
        if int(row.get("archived") or 0) != 1:
            raise ColdArchiveError("approved selected session became unarchived")
        if row.get("pinned") is None or int(row.get("pinned") or 0) != 0:
            raise ColdArchiveError("approved selected session became pinned/unprovable")
        if row.get("last_activity_at") is None:
            raise ColdArchiveError(
                "approved selected session lost canonical activity proof"
            )
        if float(row["actual_last_active"]) > cutoff:
            raise ColdArchiveError("approved selected session became recent")
        if _matches_holds(
            row,
            hold_sources=hold_sources,
            hold_title_regexes=hold_title_regexes,
            hold_cwd_prefixes=hold_cwd_prefixes,
        ):
            raise ColdArchiveError("approved selected session gained a permanent hold")
    for component in _connected_components(rows):
        intersection = id_set.intersection(component)
        if intersection and intersection != set(component):
            raise ColdArchiveError(
                "approved selected set no longer covers its whole lineage"
            )
    if _async_delegation_obligations(conn, ids) or _gateway_routing_references(
        conn, ids
    ):
        raise ColdArchiveError("approved selected set gained async/gateway references")
    if _table_has_any_reference(conn, "compression_locks", "session_id", ids):
        raise ColdArchiveError("approved selected set gained a compression lock")
    selected_messages = _count_where(
        conn,
        f"SELECT COUNT(*) FROM messages WHERE session_id IN ({_placeholders(ids)})",
        ids,
    )
    if selected_messages != int(manifest["counts"]["selected_messages_actual"]):
        raise ColdArchiveError("approved selected message count changed")
    return selected_messages


def _selected_prompt_hashes_to_delete(
    conn: sqlite3.Connection, ids: Sequence[str]
) -> list[str]:
    if (
        not _table_exists(conn, "system_prompts")
        or "hash" not in _table_columns(conn, "system_prompts")
        or "system_prompt_hash" not in _table_columns(conn, "sessions")
    ):
        return []
    placeholders = _placeholders(ids)
    return sorted(
        str(row[0])
        for row in conn.execute(
            f"""
            SELECT DISTINCT selected.system_prompt_hash
            FROM sessions selected
            JOIN system_prompts
              ON system_prompts.hash = selected.system_prompt_hash
            WHERE selected.id IN ({placeholders})
              AND selected.system_prompt_hash IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM sessions survivor
                  WHERE survivor.system_prompt_hash = selected.system_prompt_hash
                    AND survivor.id NOT IN ({placeholders})
              )
            """,
            tuple(ids) + tuple(ids),
        )
    )


def _verify_post_delete_invariants(
    conn: sqlite3.Connection,
    *,
    before: dict[str, Any],
    before_search_survivors: dict[str, Any],
    ids: Sequence[str],
    selected_messages: int,
    deleted_message_ids: Sequence[int],
    hot_cutoff: float,
) -> dict[str, Any]:
    if not conn.in_transaction:
        raise ColdArchiveError("post-delete invariants must run before COMMIT")
    after = _capture_invariants(conn, hot_cutoff=hot_cutoff)
    survivors_before = {
        sid: parent
        for sid, parent in before["parent_map"].items()
        if sid not in set(ids)
    }
    if after["parent_map"] != survivors_before:
        raise ColdArchiveError("surviving parent_session_id map changed")
    if before["sessions"] - after["sessions"] != len(ids):
        raise ColdArchiveError("session delta does not equal approved manifest")
    if before["messages"] - after["messages"] != selected_messages:
        raise ColdArchiveError("message delta does not equal approved manifest")
    for key in ("open_ids", "pinned_ids", "hot_ids"):
        if before[key] != after[key]:
            raise ColdArchiveError(f"{key} changed during retention")
    if before["payload_survivors"] != after["payload_survivors"]:
        raise ColdArchiveError("full surviving row payloads changed during retention")
    integrity = [
        str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()
    ]
    if integrity != ["ok"]:
        raise ColdArchiveError("PRAGMA integrity_check did not return exactly ok")
    foreign_keys = [
        list(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
    ]
    if foreign_keys:
        raise ColdArchiveError("PRAGMA foreign_key_check returned violations")
    fts = _verify_fts_counts(conn, deleted_message_ids=deleted_message_ids)
    after_search_survivors = _capture_search_survivor_invariants(conn, [])
    if after_search_survivors != before_search_survivors:
        raise ColdArchiveError("surviving message/search-index invariants changed")
    return {
        "integrity_check": integrity,
        "foreign_key_check_rows": len(foreign_keys),
        "fts_counts": fts,
        "survivor_search_invariants": after_search_survivors,
        "survivor_parent_map_sha256": _sha256_bytes(
            json.dumps(
                after["parent_map"], sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ),
    }


def apply_retention_to_candidate(
    candidate_db: Path,
    manifest: dict[str, Any],
    *,
    expected_manifest_sha256: str,
    approved_manifest_file_sha256: str,
    prepared_receipt_path: Path,
    remote_custody_reverified: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Delete the approved set with revalidation and all checks under one lock."""

    ids = sorted(str(value) for value in manifest.get("_restricted_selected_ids") or [])
    if expected_manifest_sha256 != manifest.get("manifest_sha256"):
        raise ColdArchiveError("Gate-B canonical manifest hash mismatch")
    if _ids_digest(ids) != manifest.get("selected_ids_sha256"):
        raise ColdArchiveError("restricted selected IDs do not match approved manifest")
    if not ids:
        return {"applied": False, "reason": "no selected sessions"}
    hot_cutoff = float(manifest["policy"]["hot_cutoff_epoch"])
    source = reject_live_state_db(candidate_db)
    conn = _connect_candidate(source)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # The identity fence is repeated after lock acquisition.
            reject_live_state_db(source)
            _validate_candidate_under_lock(conn, source, manifest)
            selected_messages = _revalidate_selected_under_lock(conn, manifest, ids)
            deleted_message_ids = _excluded_message_ids(conn, ids)
            prompt_hashes = _selected_prompt_hashes_to_delete(conn, ids)
            before = _capture_invariants(
                conn,
                hot_cutoff=hot_cutoff,
                selected_session_ids=ids,
                prompt_hashes_to_delete=prompt_hashes,
            )
            before_search = _capture_search_survivor_invariants(conn, ids)

            for table in (
                "compression_locks",
                "telegram_dm_topic_bindings",
                "session_model_usage",
                "messages",
            ):
                if "session_id" in _table_columns(conn, table):
                    conn.execute(
                        f'DELETE FROM "{table}" WHERE session_id IN ({_placeholders(ids)})',
                        tuple(ids),
                    )
            conn.execute(
                f"DELETE FROM sessions WHERE id IN ({_placeholders(ids)})", tuple(ids)
            )
            # Delete only prompts that were referenced by the selected sessions
            # and became unreferenced because of this transaction. Pre-existing
            # unrelated orphans are outside the documented retention scope.
            for prompt_hash in prompt_hashes:
                conn.execute(
                    """
                    DELETE FROM system_prompts
                    WHERE hash = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM sessions
                          WHERE sessions.system_prompt_hash = system_prompts.hash
                      )
                    """,
                    (prompt_hash,),
                )
            if prompt_hashes and _count_where(
                conn,
                f"SELECT COUNT(*) FROM system_prompts WHERE hash IN ({_placeholders(prompt_hashes)})",
                prompt_hashes,
            ):
                raise ColdArchiveError(
                    "selected orphan system prompts were not deleted"
                )

            verified = _verify_post_delete_invariants(
                conn,
                before=before,
                before_search_survivors=before_search,
                ids=ids,
                selected_messages=selected_messages,
                deleted_message_ids=deleted_message_ids,
                hot_cutoff=hot_cutoff,
            )
            _validate_candidate_path_identity(source, manifest)
            report = {
                "applied": True,
                "deleted_sessions": len(ids),
                "deleted_messages_actual": selected_messages,
                "deleted_system_prompts": len(prompt_hashes),
                **verified,
                "post_logical_sha256": _logical_snapshot_sha256(conn),
            }
            prepared = {
                "operation": "hermes-state-cold-archive-apply-prepared",
                "approved_manifest_file_sha256": approved_manifest_file_sha256,
                "gate_b_manifest_sha256": manifest["manifest_sha256"],
                "source_pre_logical_sha256": manifest["source_logical_sha256"],
                "post_logical_sha256": report["post_logical_sha256"],
                "remote_custody_reverified": list(remote_custody_reverified),
                "retention": report,
            }
            if prepared_receipt_path.exists():
                if _load_private_json(prepared_receipt_path) != prepared:
                    raise ColdArchiveError(
                        "existing prepared apply receipt does not match this checked transaction"
                    )
            else:
                _exclusive_write_json(prepared_receipt_path, prepared)
            # Final all-profile device/inode fence is intentionally adjacent to
            # COMMIT so a late live-profile alias cannot authorize deletion.
            reject_live_state_db(source)
            _validate_candidate_path_identity(source, manifest)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return report


def _producer_receipt(
    stage: StageArtifacts,
    manifest: dict[str, Any],
    *,
    source: Path,
    manifest_only: bool,
    qmd_report: dict[str, Any],
    rollback_report: dict[str, Any] | None,
    rollback_encrypted: dict[str, Any] | None,
    restricted_encrypted: dict[str, Any] | None,
    remote_report: list[dict[str, Any]],
    age_recipient_sha256: str | None,
) -> dict[str, Any]:
    return {
        "operation": "hermes-state-cold-archive-producer",
        "created_at": utc_iso(),
        "source_db": str(source),
        "stage_root": str(stage.stage_root),
        "manifest_path": str(stage.manifest_path),
        "manifest_file_sha256": sha256_path(stage.manifest_path),
        "gate_b_manifest_sha256": manifest["manifest_sha256"],
        "restricted_ids_sha256": sha256_path(stage.restricted_ids_path),
        "qmd_export": qmd_report,
        "rollback_bundle": rollback_report,
        "rollback_encrypted": rollback_encrypted,
        "restricted_encrypted": restricted_encrypted,
        "age_recipient_sha256": age_recipient_sha256,
        "remote_publish": remote_report,
        "manifest_only": manifest_only,
        "producer_complete": bool(
            manifest_only
            or not manifest["counts"]["selected_sessions"]
            or (rollback_encrypted and restricted_encrypted and len(remote_report) == 3)
        ),
        "retention_applied": False,
        "live_path_mutated": False,
        "vacuum_optimize_checkpoint_invoked": False,
        "auto_prune_enabled": False,
    }


def _validate_candidate_matches_manifest(
    source: Path, manifest: dict[str, Any]
) -> None:
    info = source.stat()
    if info.st_size != int(manifest.get("source_db_bytes", -1)):
        raise ColdArchiveError("candidate bytes differ from approved manifest snapshot")
    if info.st_dev != int(manifest.get("source_device", -1)) or info.st_ino != int(
        manifest.get("source_inode", -1)
    ):
        raise ColdArchiveError("candidate device/inode differs from approved manifest")
    if sha256_path(source) != manifest.get("source_db_sha256"):
        raise ColdArchiveError(
            "candidate sha256 differs from approved manifest snapshot"
        )
    with _connect_readonly(source) as conn:
        logical = _logical_snapshot_sha256(conn)
    if logical != manifest.get("source_logical_sha256"):
        raise ColdArchiveError(
            "candidate logical snapshot differs from approved manifest"
        )


def _validate_candidate_under_lock(
    conn: sqlite3.Connection, source: Path, manifest: dict[str, Any]
) -> None:
    if not conn.in_transaction:
        raise ColdArchiveError(
            "candidate approval revalidation requires BEGIN IMMEDIATE"
        )
    info = source.stat()
    if (
        info.st_size != int(manifest.get("source_db_bytes", -1))
        or info.st_dev != int(manifest.get("source_device", -1))
        or info.st_ino != int(manifest.get("source_inode", -1))
        or sha256_path(source) != manifest.get("source_db_sha256")
    ):
        raise ColdArchiveError(
            "candidate path bytes/identity changed before destructive lock"
        )
    if _logical_snapshot_sha256(conn) != manifest.get("source_logical_sha256"):
        raise ColdArchiveError(
            "candidate logical snapshot changed before destructive lock"
        )


def _validate_candidate_path_identity(source: Path, manifest: dict[str, Any]) -> None:
    info = source.stat()
    if info.st_dev != int(manifest.get("source_device", -1)) or info.st_ino != int(
        manifest.get("source_inode", -1)
    ):
        raise ColdArchiveError(
            "candidate path identity changed during destructive transaction"
        )


def _retention_receipt_payload(
    stage: StageArtifacts,
    manifest: dict[str, Any],
    *,
    approved_manifest_file_sha256: str,
    approved_producer_receipt_sha256: str,
    custody: Sequence[dict[str, Any]],
    report: dict[str, Any],
    recovered_from_prepared: bool = False,
) -> dict[str, Any]:
    return {
        "operation": "hermes-state-cold-archive-retention",
        "created_at": utc_iso(),
        "approved_manifest_path": str(stage.manifest_path),
        "approved_manifest_file_sha256": approved_manifest_file_sha256,
        "approved_producer_receipt_sha256": approved_producer_receipt_sha256,
        "gate_b_manifest_sha256": manifest["manifest_sha256"],
        "remote_custody_reverified": list(custody),
        "retention": report,
        "checks_completed_before_commit": True,
        "prepared_receipt_path": str(stage.apply_prepared_path),
        "prepared_receipt_sha256": (
            sha256_path(stage.apply_prepared_path)
            if stage.apply_prepared_path.exists()
            else None
        ),
        "receipt_written_after_commit": True,
        "recovered_from_prepared": recovered_from_prepared,
    }


def _recover_committed_prepared_apply(
    source: Path,
    stage: StageArtifacts,
    manifest: dict[str, Any],
    *,
    approved_manifest_file_sha256: str,
    approved_producer_receipt_sha256: str,
    custody: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    if not stage.apply_prepared_path.exists():
        return None
    prepared = _load_private_json(stage.apply_prepared_path)
    if (
        prepared.get("operation") != "hermes-state-cold-archive-apply-prepared"
        or prepared.get("approved_manifest_file_sha256")
        != approved_manifest_file_sha256
        or prepared.get("gate_b_manifest_sha256") != manifest.get("manifest_sha256")
        or prepared.get("source_pre_logical_sha256")
        != manifest.get("source_logical_sha256")
    ):
        raise ColdArchiveError("prepared apply receipt does not bind this approval")
    report = prepared.get("retention")
    if not isinstance(report, dict) or not report.get("applied"):
        raise ColdArchiveError(
            "prepared apply receipt has no committed post-state claim"
        )
    if prepared.get("post_logical_sha256") != report.get("post_logical_sha256"):
        raise ColdArchiveError("prepared apply receipt post-state hash is inconsistent")
    _validate_candidate_path_identity(source, manifest)
    with _connect_readonly(source) as conn:
        current_logical = _logical_snapshot_sha256(conn)
        if current_logical != prepared.get("post_logical_sha256"):
            return None
        ids = sorted(
            str(value) for value in manifest.get("_restricted_selected_ids") or []
        )
        if ids and _count_where(
            conn,
            f"SELECT COUNT(*) FROM sessions WHERE id IN ({_placeholders(ids)})",
            ids,
        ):
            raise ColdArchiveError(
                "prepared post-state still contains selected sessions"
            )
        integrity = [
            str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()
        ]
        if integrity != ["ok"] or conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ColdArchiveError(
                "prepared post-state failed integrity recovery checks"
            )
        _verify_fts_counts(conn, deleted_message_ids=[])
    receipt = _retention_receipt_payload(
        stage,
        manifest,
        approved_manifest_file_sha256=approved_manifest_file_sha256,
        approved_producer_receipt_sha256=approved_producer_receipt_sha256,
        custody=custody,
        report=report,
        recovered_from_prepared=True,
    )
    _exclusive_write_json(stage.retention_receipt_path, receipt)
    return {
        **receipt,
        "receipt_path": str(stage.retention_receipt_path),
        "receipt_sha256": sha256_path(stage.retention_receipt_path),
    }


def _validate_final_retention_receipt(
    source: Path,
    stage: StageArtifacts,
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    *,
    approved_manifest_file_sha256: str,
    approved_producer_receipt_sha256: str | None,
) -> None:
    if (
        receipt.get("operation") != "hermes-state-cold-archive-retention"
        or receipt.get("approved_manifest_file_sha256") != approved_manifest_file_sha256
        or receipt.get("gate_b_manifest_sha256") != manifest.get("manifest_sha256")
    ):
        raise ColdArchiveError("final retention receipt does not bind this approval")
    ids = sorted(str(value) for value in manifest.get("_restricted_selected_ids") or [])
    report = receipt.get("retention")
    if not isinstance(report, dict):
        raise ColdArchiveError("final retention receipt has no retention report")
    if not ids:
        if report.get("applied") is not False:
            raise ColdArchiveError(
                "empty selection receipt incorrectly claims deletion"
            )
        _validate_candidate_matches_manifest(source, manifest)
        return
    if (
        approved_producer_receipt_sha256 is None
        or receipt.get("approved_producer_receipt_sha256")
        != approved_producer_receipt_sha256
        or report.get("applied") is not True
        or not stage.apply_prepared_path.exists()
        or receipt.get("prepared_receipt_sha256")
        != sha256_path(stage.apply_prepared_path)
    ):
        raise ColdArchiveError("final retention receipt lacks approved committed proof")
    _validate_candidate_path_identity(source, manifest)
    with _connect_readonly(source) as conn:
        if _logical_snapshot_sha256(conn) != report.get("post_logical_sha256"):
            raise ColdArchiveError(
                "final retention receipt post-state no longer matches"
            )
        if _count_where(
            conn,
            f"SELECT COUNT(*) FROM sessions WHERE id IN ({_placeholders(ids)})",
            ids,
        ):
            raise ColdArchiveError(
                "final retention receipt replay found selected sessions"
            )
        integrity = [
            str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()
        ]
        if integrity != ["ok"] or conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ColdArchiveError(
                "final retention receipt replay failed integrity checks"
            )
        _verify_fts_counts(conn, deleted_message_ids=[])


def run_cold_archive_pass(
    *,
    source_db: Path,
    stage_root: Path,
    hot_days: float = DEFAULT_HOT_DAYS,
    archive_grace_days: float = DEFAULT_ARCHIVE_GRACE_DAYS,
    now: float | None = None,
    hold_sources: Iterable[str] = DEFAULT_PERMANENT_HOLD_SOURCES,
    hold_title_regexes: Iterable[str] = (),
    hold_cwd_prefixes: Iterable[str] = (),
    manifest_only: bool = False,
    apply_retention: bool = False,
    approved_manifest_path: Path | None = None,
    approved_manifest_sha256: str | None = None,
    approved_producer_receipt_path: Path | None = None,
    approved_producer_receipt_sha256: str | None = None,
    rclone_remote: str | None = None,
    rclone_config: Path | None = None,
    remote_namespace: str | None = None,
    age_recipient_file: Path | None = None,
    age_exe: str = "age",
    rclone_exe: str = "rclone",
    age_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    rclone_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Run either a no-clobber producer or an approved destructive apply."""

    if manifest_only and apply_retention:
        raise ColdArchiveError(
            "--manifest-only and --apply-retention are mutually exclusive"
        )
    source = reject_live_state_db(source_db)
    old_umask = os.umask(0o077)
    try:
        if apply_retention:
            if approved_manifest_path is None or approved_manifest_sha256 is None:
                raise ColdArchiveError(
                    "apply requires external --approved-manifest and exact-byte sha256"
                )
            if rclone_config is None:
                raise ColdArchiveError(
                    "apply requires --rclone-config for fresh remote readback"
                )
            stage, manifest = load_approved_gate_b_manifest(
                stage_root,
                approved_manifest_path=approved_manifest_path,
                approved_manifest_sha256=approved_manifest_sha256,
            )
            ids = manifest.get("_restricted_selected_ids") or []
            if not ids:
                if stage.retention_receipt_path.exists():
                    existing = _load_private_json(stage.retention_receipt_path)
                    _validate_final_retention_receipt(
                        source,
                        stage,
                        manifest,
                        existing,
                        approved_manifest_file_sha256=approved_manifest_sha256,
                        approved_producer_receipt_sha256=None,
                    )
                    return {**existing, "replayed": True}
                _validate_candidate_matches_manifest(source, manifest)
                report = {"applied": False, "reason": "no selected sessions"}
                receipt = {
                    "operation": "hermes-state-cold-archive-retention",
                    "created_at": utc_iso(),
                    "approved_manifest_file_sha256": approved_manifest_sha256,
                    "gate_b_manifest_sha256": manifest["manifest_sha256"],
                    "remote_custody_reverified": [],
                    "retention": report,
                }
                _exclusive_write_json(stage.retention_receipt_path, receipt)
                return {
                    **receipt,
                    "receipt_path": str(stage.retention_receipt_path),
                    "receipt_sha256": sha256_path(stage.retention_receipt_path),
                }
            if (
                approved_producer_receipt_path is None
                or approved_producer_receipt_sha256 is None
            ):
                raise ColdArchiveError(
                    "apply requires external --approved-producer-receipt and exact-byte sha256"
                )
            producer = load_approved_producer_receipt(
                stage,
                manifest,
                approved_producer_receipt_path,
                approved_producer_receipt_sha256,
            )
            custody = _verify_remote_custody(
                stage,
                producer,
                rclone_config=rclone_config,
                rclone_exe=rclone_exe,
                runner=rclone_runner,
            )
            if stage.retention_receipt_path.exists():
                existing = _load_private_json(stage.retention_receipt_path)
                _validate_final_retention_receipt(
                    source,
                    stage,
                    manifest,
                    existing,
                    approved_manifest_file_sha256=approved_manifest_sha256,
                    approved_producer_receipt_sha256=approved_producer_receipt_sha256,
                )
                return {**existing, "replayed": True}
            recovered = _recover_committed_prepared_apply(
                source,
                stage,
                manifest,
                approved_manifest_file_sha256=approved_manifest_sha256,
                approved_producer_receipt_sha256=approved_producer_receipt_sha256,
                custody=custody,
            )
            if recovered is not None:
                return recovered
            _validate_candidate_matches_manifest(source, manifest)
            report = apply_retention_to_candidate(
                source,
                manifest,
                expected_manifest_sha256=manifest["manifest_sha256"],
                approved_manifest_file_sha256=approved_manifest_sha256,
                prepared_receipt_path=stage.apply_prepared_path,
                remote_custody_reverified=custody,
            )
            receipt = _retention_receipt_payload(
                stage,
                manifest,
                approved_manifest_file_sha256=approved_manifest_sha256,
                approved_producer_receipt_sha256=approved_producer_receipt_sha256,
                custody=custody,
                report=report,
            )
            _exclusive_write_json(stage.retention_receipt_path, receipt)
            return {
                **receipt,
                "receipt_path": str(stage.retention_receipt_path),
                "receipt_sha256": sha256_path(stage.retention_receipt_path),
            }

        age_recipient_bytes: bytes | None = None
        if not manifest_only:
            if not rclone_remote or rclone_config is None or age_recipient_file is None:
                raise ColdArchiveError(
                    "full producer requires --age-recipient-file, --rclone-config, "
                    "and --rclone-remote before stage creation"
                )
            _require_secret_config(rclone_config)
            _, age_recipient_bytes, _ = _read_stable_regular_bytes(
                age_recipient_file,
                label="age recipient file",
            )
            _validate_remote_namespace(
                rclone_remote, remote_namespace or "hermes-state/preflight"
            )
            if rclone_runner is None and shutil.which(rclone_exe) is None:
                raise ColdArchiveError(
                    f"rclone executable is unavailable: {rclone_exe}"
                )
            if age_runner is None and shutil.which(age_exe) is None:
                raise ColdArchiveError(f"age executable is unavailable: {age_exe}")

        manifest = build_gate_b_manifest(
            source,
            now=now,
            hot_days=hot_days,
            archive_grace_days=archive_grace_days,
            hold_sources=hold_sources,
            hold_title_regexes=hold_title_regexes,
            hold_cwd_prefixes=hold_cwd_prefixes,
        )
        stage = write_gate_b_manifest(stage_root, manifest)
        qmd_report: dict[str, Any] = {
            "exported_files": [],
            "verified": True,
            "message_count": 0,
        }
        rollback_report: dict[str, Any] | None = None
        rollback_encrypted: dict[str, Any] | None = None
        restricted_encrypted: dict[str, Any] | None = None
        remote_report: list[dict[str, Any]] = []
        age_recipient_sha256: str | None = None
        if not manifest_only and manifest["counts"]["selected_sessions"]:
            if not rclone_remote or rclone_config is None or age_recipient_file is None:
                raise ColdArchiveError(
                    "full producer requires --age-recipient-file, --rclone-config, "
                    "and --rclone-remote before any retention can be authorized"
                )
            if age_recipient_bytes is None:
                raise ColdArchiveError(
                    "age recipient bytes were not frozen at preflight"
                )
            recipient_snapshot = _exclusive_write_bytes(
                stage.age_recipient_snapshot_path, age_recipient_bytes
            )
            age_recipient_sha256 = sha256_path(recipient_snapshot)
            _validate_candidate_matches_manifest(source, manifest)
            rollback_report, retention_policy = _copy_rollback_bundle(
                source, stage, now=now
            )
            _verify_rollback_bundle_snapshot(stage, manifest)
            retention_policy["gate_b_manifest_sha256"] = manifest["manifest_sha256"]
            _exclusive_write_json(stage.source_bundle_policy_path, retention_policy)
            qmd_report = export_redacted_qmd(source, stage, manifest)
            if (
                qmd_report["message_count"]
                != manifest["counts"]["selected_messages_actual"]
            ):
                raise ColdArchiveError("QMD message count does not match manifest")
            restricted_packet = _build_restricted_packet(stage, qmd_report)
            rollback_encrypted = encrypt_file_with_age(
                stage.rollback_bundle_path,
                stage.rollback_encrypted_path,
                recipient_file=recipient_snapshot,
                expected_recipient_sha256=age_recipient_sha256,
                age_exe=age_exe,
                runner=age_runner,
            )
            try:
                restricted_encrypted = encrypt_file_with_age(
                    restricted_packet,
                    stage.restricted_encrypted_path,
                    recipient_file=recipient_snapshot,
                    expected_recipient_sha256=age_recipient_sha256,
                    age_exe=age_exe,
                    runner=age_runner,
                )
            finally:
                restricted_packet.unlink(missing_ok=True)
            namespace = (
                remote_namespace or f"hermes-state/{manifest['manifest_sha256']}"
            )
            # Restricted payloads are opaque and uploaded first; the sole clear
            # remote object is the redacted manifest, published last as marker.
            remote_report = publish_paths_with_rclone(
                [
                    stage.rollback_encrypted_path,
                    stage.restricted_encrypted_path,
                    stage.manifest_path,
                ],
                remote_root=rclone_remote,
                rclone_config=rclone_config,
                namespace=namespace,
                rclone_exe=rclone_exe,
                runner=rclone_runner,
            )
            _validate_candidate_matches_manifest(source, manifest)
        receipt = _producer_receipt(
            stage,
            manifest,
            source=source,
            manifest_only=manifest_only,
            qmd_report=qmd_report,
            rollback_report=rollback_report,
            rollback_encrypted=rollback_encrypted,
            restricted_encrypted=restricted_encrypted,
            remote_report=remote_report,
            age_recipient_sha256=age_recipient_sha256,
        )
        _exclusive_write_json(stage.producer_receipt_path, receipt)
        return {
            **receipt,
            "receipt_path": str(stage.producer_receipt_path),
            "receipt_sha256": sha256_path(stage.producer_receipt_path),
        }
    finally:
        os.umask(old_umask)


def _producer_has_verified_rollback_remote(
    stage: StageArtifacts, producer: dict[str, Any]
) -> bool:
    if (
        producer.get("operation") != "hermes-state-cold-archive-producer"
        or producer.get("producer_complete") is not True
        or producer.get("manifest_only") is not False
    ):
        return False
    encrypted_sha = sha256_path(_require_private_file(stage.rollback_encrypted_path))
    for report in producer.get("remote_publish") or []:
        if (
            isinstance(report, dict)
            and Path(str(report.get("local_path") or "")).name
            == stage.rollback_encrypted_path.name
            and report.get("sha256") == encrypted_sha
            and report.get("readback_sha256") == encrypted_sha
            and report.get("integrity") == "rclone-checksum-and-readback-ok"
        ):
            return True
    return False


def record_candidate_cutover(
    stage_root: Path,
    *,
    candidate_health_confirmed: bool,
    rclone_config: Path,
    rclone_exe: str = "rclone",
    rclone_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Exclusively record the real cutover clock after a verified apply."""

    if not candidate_health_confirmed:
        raise ColdArchiveError("candidate health confirmation is required at cutover")
    stage = _load_stage(stage_root)
    existing_marker: dict[str, Any] | None = None
    if stage.cutover_marker_path.exists():
        existing_marker = _load_private_json(stage.cutover_marker_path)
    policy = _load_private_json(stage.source_bundle_policy_path)
    producer = _load_private_json(stage.producer_receipt_path)
    retention_receipt = _load_private_json(stage.retention_receipt_path)
    prepared = _load_private_json(stage.apply_prepared_path)
    producer_sha = sha256_path(stage.producer_receipt_path)
    retention = retention_receipt.get("retention")
    if (
        policy.get("operation") != "hermes-source-bundle-retention-policy"
        or policy.get("state") != "awaiting-cutover"
        or retention_receipt.get("operation") != "hermes-state-cold-archive-retention"
        or not isinstance(retention, dict)
        or retention.get("applied") is not True
        or retention_receipt.get("approved_producer_receipt_sha256") != producer_sha
        or retention_receipt.get("prepared_receipt_sha256")
        != sha256_path(stage.apply_prepared_path)
        or retention_receipt.get("checks_completed_before_commit") is not True
        or retention_receipt.get("receipt_written_after_commit") is not True
        or prepared.get("operation") != "hermes-state-cold-archive-apply-prepared"
        or prepared.get("post_logical_sha256") != retention.get("post_logical_sha256")
        or prepared.get("gate_b_manifest_sha256")
        != retention_receipt.get("gate_b_manifest_sha256")
        or policy.get("gate_b_manifest_sha256")
        != producer.get("gate_b_manifest_sha256")
    ):
        raise ColdArchiveError(
            "verified applied retention receipt is required at cutover"
        )
    bundle = _require_private_file(stage.rollback_bundle_path)
    bundle_sha = sha256_path(bundle)
    if policy.get("rollback_bundle_sha256") != bundle_sha:
        raise ColdArchiveError("source-bundle policy does not match rollback bundle")
    if not _producer_has_verified_rollback_remote(stage, producer):
        raise ColdArchiveError("verified encrypted rollback remote receipt is required")
    _verify_remote_custody(
        stage,
        producer,
        rclone_config=rclone_config,
        rclone_exe=rclone_exe,
        runner=rclone_runner,
    )
    if existing_marker is not None:
        existing_cutover = _finite_epoch(
            existing_marker.get("cutover_epoch"), "recorded cutover time"
        )
        if (
            existing_marker.get("operation") != "hermes-candidate-cutover"
            or existing_marker.get("candidate_health_confirmed") is not True
            or existing_marker.get("cutover_at") != utc_iso(existing_cutover)
            or existing_marker.get("rollback_bundle_sha256") != bundle_sha
            or existing_marker.get("producer_receipt_sha256") != producer_sha
            or existing_marker.get("retention_receipt_sha256")
            != sha256_path(stage.retention_receipt_path)
        ):
            raise ColdArchiveError("existing cutover marker is invalid")
        return {**existing_marker, "replayed": True}
    cutover_epoch = _finite_epoch(time.time() if now is None else now, "cutover time")
    marker = {
        "operation": "hermes-candidate-cutover",
        "cutover_at": utc_iso(cutover_epoch),
        "cutover_epoch": cutover_epoch,
        "rollback_bundle_sha256": bundle_sha,
        "producer_receipt_sha256": producer_sha,
        "retention_receipt_sha256": sha256_path(stage.retention_receipt_path),
        "candidate_health_confirmed": True,
    }
    _exclusive_write_json(stage.cutover_marker_path, marker)
    return marker


def _prepare_source_bundle_prune(
    stage: StageArtifacts,
    *,
    rollback_bundle_sha256: str,
    cutover_marker_sha256: str,
) -> dict[str, Any]:
    if stage.source_bundle_prune_prepared_path.exists():
        prepared = _load_private_json(stage.source_bundle_prune_prepared_path)
        if (
            prepared.get("operation") != "hermes-source-bundle-prune-prepared"
            or prepared.get("rollback_bundle_sha256") != rollback_bundle_sha256
            or prepared.get("cutover_marker_sha256") != cutover_marker_sha256
        ):
            raise ColdArchiveError("prepared source-bundle prune intent is misbound")
        return prepared
    bundle_root = _require_private_dir(stage.rollback_dir / "source-bundle")
    members: list[dict[str, Any]] = []
    for child in sorted(bundle_root.iterdir(), key=lambda item: item.name):
        private = _require_private_file(child)
        members.append({
            "name": private.name,
            "bytes": private.stat().st_size,
            "sha256": sha256_path(private),
        })
    clear_tar = _require_private_file(stage.rollback_bundle_path)
    if sha256_path(clear_tar) != rollback_bundle_sha256:
        raise ColdArchiveError("rollback bundle changed before prune intent")
    prepared = {
        "operation": "hermes-source-bundle-prune-prepared",
        "rollback_bundle_sha256": rollback_bundle_sha256,
        "cutover_marker_sha256": cutover_marker_sha256,
        "source_bundle_members": members,
        "rollback_bundle_name": clear_tar.name,
    }
    _exclusive_write_json(stage.source_bundle_prune_prepared_path, prepared)
    return prepared


def _delete_private_source_bundle(
    stage: StageArtifacts, prepared: dict[str, Any]
) -> list[str]:
    deleted: list[str] = []
    bundle_root = stage.rollback_dir / "source-bundle"
    raw_members = prepared.get("source_bundle_members")
    if not isinstance(raw_members, list):
        raise ColdArchiveError("prepared source-bundle member list is invalid")
    expected: dict[str, dict[str, Any]] = {}
    for report in raw_members:
        if not isinstance(report, dict) or not re.fullmatch(
            r"[A-Za-z0-9._-]+", str(report.get("name") or "")
        ):
            raise ColdArchiveError("prepared source-bundle member is invalid")
        expected[str(report["name"])] = report
    if bundle_root.exists():
        private_root = _require_private_dir(bundle_root)
        current_names = {child.name for child in private_root.iterdir()}
        if not current_names.issubset(expected):
            raise ColdArchiveError(
                "unexpected member appeared during source-bundle prune"
            )
        for name, report in expected.items():
            child = private_root / name
            if not child.exists():
                continue
            private = _require_private_file(child)
            if private.stat().st_size != report.get("bytes") or sha256_path(
                private
            ) != report.get("sha256"):
                raise ColdArchiveError(
                    "source-bundle member changed after prune intent"
                )
            private.unlink()
            deleted.append(str(private))
        private_root.rmdir()
        deleted.append(str(private_root))
    clear_tar = stage.rollback_bundle_path
    if clear_tar.exists():
        private_tar = _require_private_file(clear_tar)
        if sha256_path(private_tar) != prepared.get("rollback_bundle_sha256"):
            raise ColdArchiveError("rollback bundle changed after prune intent")
        private_tar.unlink()
        deleted.append(str(private_tar))
    _fsync_directory(stage.rollback_dir)
    return deleted


def prune_source_bundle_after_retention(
    stage_root: Path,
    *,
    candidate_health_confirmed: bool,
    approved_cutover_marker_sha256: str,
    rclone_config: Path,
    rclone_exe: str = "rclone",
    rclone_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Prune only plaintext source copies at/after cutover + exactly 14 days."""

    if not candidate_health_confirmed:
        raise ColdArchiveError("candidate health confirmation is required before prune")
    if not re.fullmatch(r"[0-9a-f]{64}", approved_cutover_marker_sha256 or ""):
        raise ColdArchiveError("approved cutover marker sha256 must be lowercase hex")
    stage = _load_stage(stage_root)
    existing_pruned: dict[str, Any] | None = None
    if stage.source_bundle_pruned_path.exists():
        existing_pruned = _load_private_json(stage.source_bundle_pruned_path)
        if (
            existing_pruned.get("operation") != "hermes-source-bundle-pruned"
            or existing_pruned.get("pruned") is not True
            or existing_pruned.get("approved_cutover_marker_sha256")
            != approved_cutover_marker_sha256
        ):
            raise ColdArchiveError("existing source-bundle prune receipt is invalid")
    policy = _load_private_json(stage.source_bundle_policy_path)
    if not stage.cutover_marker_path.exists():
        if existing_pruned is not None:
            raise ColdArchiveError("source-bundle prune receipt exists before cutover")
        return {"pruned": False, "reason": "awaiting-cutover"}
    marker_path, marker_bytes = _read_private_bytes(stage.cutover_marker_path)
    if _sha256_bytes(marker_bytes) != approved_cutover_marker_sha256:
        raise ColdArchiveError("approved cutover marker exact-byte sha256 mismatch")
    marker = _decode_json_object(marker_bytes, marker_path)
    producer = _load_private_json(stage.producer_receipt_path)
    retention_receipt = _load_private_json(stage.retention_receipt_path)
    retention_state = retention_receipt.get("retention")
    if (
        policy.get("operation") != "hermes-source-bundle-retention-policy"
        or policy.get("state") != "awaiting-cutover"
        or policy.get("gate_b_manifest_sha256")
        != producer.get("gate_b_manifest_sha256")
        or retention_receipt.get("operation") != "hermes-state-cold-archive-retention"
        or not isinstance(retention_state, dict)
        or retention_state.get("applied") is not True
        or retention_receipt.get("approved_producer_receipt_sha256")
        != sha256_path(stage.producer_receipt_path)
        or retention_receipt.get("prepared_receipt_sha256")
        != sha256_path(stage.apply_prepared_path)
    ):
        raise ColdArchiveError("source-bundle lifecycle receipts are invalid")
    if not _producer_has_verified_rollback_remote(stage, producer):
        raise ColdArchiveError("verified encrypted rollback remote receipt is required")
    _verify_remote_custody(
        stage,
        producer,
        rclone_config=rclone_config,
        rclone_exe=rclone_exe,
        runner=rclone_runner,
    )
    bundle_sha = str(policy.get("rollback_bundle_sha256") or "")
    cutover = _finite_epoch(marker.get("cutover_epoch"), "recorded cutover time")
    if (
        marker.get("operation") != "hermes-candidate-cutover"
        or marker.get("candidate_health_confirmed") is not True
        or marker.get("cutover_at") != utc_iso(cutover)
        or marker.get("rollback_bundle_sha256") != bundle_sha
    ):
        raise ColdArchiveError("cutover marker fields are invalid or inconsistent")
    if marker.get("producer_receipt_sha256") != sha256_path(
        stage.producer_receipt_path
    ):
        raise ColdArchiveError("cutover marker producer receipt hash mismatch")
    if (
        marker.get("retention_receipt_sha256")
        != sha256_path(stage.retention_receipt_path)
        or retention_receipt.get("operation") != "hermes-state-cold-archive-retention"
    ):
        raise ColdArchiveError("cutover marker retention receipt hash mismatch")
    retention = int(policy.get("minimum_retention_seconds_after_cutover", -1))
    if retention != DEFAULT_SOURCE_BUNDLE_RETENTION_SECONDS:
        raise ColdArchiveError("source-bundle retention policy is invalid")
    current = _finite_epoch(time.time() if now is None else now, "prune time")
    if current < cutover:
        raise ColdArchiveError("clock is earlier than recorded candidate cutover")
    deadline = cutover + retention
    if current < deadline:
        if existing_pruned is not None:
            raise ColdArchiveError(
                "source-bundle prune receipt predates retention boundary"
            )
        return {
            "pruned": False,
            "reason": "minimum-retention-active",
            "cutover_epoch": cutover,
            "eligible_at_epoch": deadline,
        }
    if existing_pruned is not None:
        prepared = _load_private_json(stage.source_bundle_prune_prepared_path)
        pruned_epoch = _finite_epoch(
            existing_pruned.get("pruned_at_epoch"), "recorded source-bundle prune time"
        )
        if (
            pruned_epoch < deadline
            or existing_pruned.get("pruned_at") != utc_iso(pruned_epoch)
            or existing_pruned.get("cutover_epoch") != cutover
            or existing_pruned.get("minimum_retention_seconds") != retention
            or existing_pruned.get("rollback_bundle_sha256") != bundle_sha
            or existing_pruned.get("prepared_prune_sha256")
            != sha256_path(stage.source_bundle_prune_prepared_path)
            or prepared.get("operation") != "hermes-source-bundle-prune-prepared"
            or prepared.get("rollback_bundle_sha256") != bundle_sha
            or prepared.get("cutover_marker_sha256") != approved_cutover_marker_sha256
        ):
            raise ColdArchiveError("existing source-bundle prune receipt is misbound")
        if (
            stage.rollback_dir / "source-bundle"
        ).exists() or stage.rollback_bundle_path.exists():
            raise ColdArchiveError("plaintext source bundle reappeared after prune")
        _require_private_file(stage.rollback_encrypted_path)
        return {**existing_pruned, "replayed": True}
    prepared = _prepare_source_bundle_prune(
        stage,
        rollback_bundle_sha256=bundle_sha,
        cutover_marker_sha256=approved_cutover_marker_sha256,
    )
    deleted = _delete_private_source_bundle(stage, prepared)
    receipt = {
        "operation": "hermes-source-bundle-pruned",
        "pruned": True,
        "pruned_at": utc_iso(current),
        "pruned_at_epoch": current,
        "cutover_epoch": cutover,
        "minimum_retention_seconds": retention,
        "rollback_bundle_sha256": bundle_sha,
        "approved_cutover_marker_sha256": approved_cutover_marker_sha256,
        "prepared_prune_sha256": sha256_path(stage.source_bundle_prune_prepared_path),
        "deleted_local_plaintext_paths": deleted,
        "remote_objects_deleted": False,
        "encrypted_rollback_retained": stage.rollback_encrypted_path.exists(),
    }
    _exclusive_write_json(stage.source_bundle_pruned_path, receipt)
    return receipt


__all__ = [
    "ColdArchiveError",
    "DEFAULT_PERMANENT_HOLD_SOURCES",
    "DEFAULT_SOURCE_BUNDLE_RETENTION_SECONDS",
    "StageArtifacts",
    "apply_retention_to_candidate",
    "build_gate_b_manifest",
    "export_redacted_qmd",
    "load_approved_gate_b_manifest",
    "prune_source_bundle_after_retention",
    "publish_paths_with_rclone",
    "record_candidate_cutover",
    "reject_live_state_db",
    "run_cold_archive_pass",
    "write_gate_b_manifest",
]
