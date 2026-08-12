"""Offline, staged cold archival for Hermes ``state.db`` session history.

This module is intentionally separate from ``SessionDB.prune_sessions``.  The
normal prune path is for interactive/live cleanup and reparents surviving child
sessions to ``NULL``.  Cold archival is the opposite posture: it only mutates an
offline candidate copy after a reviewed Gate-B manifest, a restricted redacted
QMD export, a lossless rollback bundle, and fail-closed offsite
readback verification. Destructive retention requires a distinct external
approver identity plus a positive verified offsite permanence receipt —
self-approve and delete-without-offsite paths fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from hermes_constants import get_hermes_home
from hermes_state import DEFAULT_DB_PATH
from hermes_cli.session_export_md import (
    render_session_markdown,
    safe_session_filename,
    verify_export_file,
    redact_session_data,
)

DEFAULT_HOT_DAYS = 30.0
DEFAULT_ARCHIVE_GRACE_DAYS = 7.0
# Hard floor: cold eligibility cannot be satisfied by lowering hot/grace.
# Tests MUST assert against the literal 37-day / 3_196_800s values, not by
# reading these constants from the module under test.
MIN_COLD_AGE_DAYS = 37.0
MIN_COLD_AGE_SECONDS = 3_196_800  # 37 * 86400
# Rollback / stage bundle retention floor (14 days). Tests MUST assert the
# literal 1_209_600 seconds rather than reading this constant.
BUNDLE_RETENTION_SECONDS = 1_209_600  # 14 * 86400
_ARCHIVE_VERSION = 1
_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")
# Durable sessions columns required before any selection or deletion. Missing
# columns fail closed — never treat absence as an empty invariant set.
_REQUIRED_SESSIONS_COLUMNS = frozenset({"pinned", "last_activity_at"})
# Offsite permanence: only this integrity token counts as a verified receipt.
# Callers may not invent softer statuses; tests assert the literal string.
OFFSITE_INTEGRITY_OK = "rclone-checksum-and-readback-ok"
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
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
    """Raised when the staged archival pass must fail closed."""


@dataclass(frozen=True)
class StageArtifacts:
    stage_root: Path
    manifest_path: Path
    restricted_ids_path: Path
    restricted_groups_path: Path
    qmd_dir: Path
    rollback_bundle_path: Optional[Path]
    rollback_encrypted_path: Optional[Path]
    receipt_path: Path


def utc_iso(value: float | None = None) -> str:
    ts = time.time() if value is None else float(value)
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chmod_private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except FileNotFoundError:
        raise


def _secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise ColdArchiveError(f"could not enforce 0700 on {path}: {exc}") from exc
    return path


def _atomic_write_bytes(path: Path, data: bytes) -> Path:
    _secure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _chmod_private_file(path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Directory fsync is best-effort on platforms without O_DIRECTORY.
            pass
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise
    return path


def _atomic_write_text(path: Path, text: str) -> Path:
    return _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return _atomic_write_text(path, text)


def _resolve_existing_file(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ColdArchiveError(f"source database does not exist: {path}") from exc
    if not resolved.is_file():
        raise ColdArchiveError(f"source is not a regular file: {resolved}")
    return resolved


def _live_state_paths() -> set[Path]:
    paths: set[Path] = set()
    for raw in (DEFAULT_DB_PATH, get_hermes_home() / "state.db"):
        try:
            paths.add(Path(raw).expanduser().resolve(strict=False))
        except OSError:
            continue
    for suffix in _SIDECAR_SUFFIXES:
        if suffix == "":
            continue
        try:
            paths.add((get_hermes_home() / ("state.db" + suffix)).resolve(strict=False))
        except OSError:
            continue
    return paths


def _file_identity(path: Path) -> tuple[int, int] | None:
    """Return (st_dev, st_ino) for an existing path, or None if missing."""
    try:
        st = path.expanduser().resolve(strict=False).stat()
    except OSError:
        return None
    if not stat_is_reg_or_link(st.st_mode):
        # Still compare inode for hardlink detection when path exists.
        pass
    return (int(st.st_dev), int(st.st_ino))


def stat_is_reg_or_link(mode: int) -> bool:
    import stat as stat_mod

    return stat_mod.S_ISREG(mode) or stat_mod.S_ISLNK(mode)


def _live_state_identities() -> set[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    for path in _live_state_paths():
        identity = _file_identity(path)
        if identity is not None:
            identities.add(identity)
    # Also include unresolved live path variants (pre-resolve hardlink targets).
    for raw in (DEFAULT_DB_PATH, get_hermes_home() / "state.db"):
        identity = _file_identity(Path(raw))
        if identity is not None:
            identities.add(identity)
        for suffix in _SIDECAR_SUFFIXES:
            if suffix == "":
                continue
            identity = _file_identity(Path(str(raw) + suffix))
            if identity is not None:
                identities.add(identity)
    return identities


def reject_live_state_db(source_path: Path) -> Path:
    """Return a resolved source path or fail if it is the active profile DB.

    Guards against path equality, symlink resolution, AND hardlink/alias
    identity via ``(st_dev, st_ino)`` comparison. Path-only checks are not
    sufficient: a hardlink of the live DB must also be refused.
    """

    source = _resolve_existing_file(source_path)
    protected = _live_state_paths()
    if source in protected:
        raise ColdArchiveError(
            "refusing to run cold archival against the active Hermes state.db; "
            "use an offline recovered candidate copy"
        )
    for suffix in _SIDECAR_SUFFIXES:
        if source == (get_hermes_home() / ("state.db" + suffix)).resolve(strict=False):
            raise ColdArchiveError(
                "refusing to use an active state.db sidecar as source"
            )
    source_identity = _file_identity(source)
    if source_identity is not None and source_identity in _live_state_identities():
        raise ColdArchiveError(
            "refusing to run cold archival against a hardlink/symlink/alias of "
            "the active Hermes state.db; use a true offline copy (different inode)"
        )
    return source


def require_sessions_schema(conn: sqlite3.Connection) -> set[str]:
    """Fail closed when durable conflict-resolution columns are missing.

    A missing ``pinned`` or ``last_activity_at`` column must never be treated as
    an empty invariant set. Selection and deletion both refuse until the
    schema carries both columns.
    """

    columns = _table_columns(conn, "sessions")
    missing = sorted(_REQUIRED_SESSIONS_COLUMNS - columns)
    if missing:
        raise ColdArchiveError(
            "sessions schema missing required durable columns "
            f"{missing}; refusing cold-archive selection/deletion "
            "(fail closed — do not infer empty pin/activity invariants)"
        )
    return columns


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    source = _resolve_existing_file(db_path)
    uri = source.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=1.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _connect_candidate(db_path: Path) -> sqlite3.Connection:
    source = reject_live_state_db(db_path)
    conn = sqlite3.connect(str(source), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in _table_columns(conn, table)


def _count_where(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    return int(conn.execute(sql, tuple(params)).fetchone()[0])


def _placeholders(values: Sequence[str]) -> str:
    if not values:
        raise ColdArchiveError("internal error: empty placeholder list")
    return ",".join("?" for _ in values)


def _session_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    require_sessions_schema(conn)
    rows = conn.execute(
        """
        SELECT s.*,
               COALESCE(
                   s.last_activity_at,
                   (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id),
                   s.started_at
               ) AS actual_last_active,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS actual_message_count
        FROM sessions s
        ORDER BY s.started_at ASC, s.id ASC
        """
    ).fetchall()
    return {str(row["id"]): dict(row) for row in rows}


def effective_cold_cutoff(
    *,
    generated_at: float,
    hot_days: float,
    archive_grace_days: float,
) -> tuple[float, float, float]:
    """Return (cold_cutoff, hot_cutoff, effective_min_age_days).

    Enforces a hard 37-day floor that cannot be bypassed by setting hot_days or
    archive_grace_days to zero (or any value whose sum is below 37 days).
    """

    if hot_days < 0 or archive_grace_days < 0:
        raise ColdArchiveError("hot_days and archive_grace_days must be non-negative")
    configured_days = float(hot_days) + float(archive_grace_days)
    # Literal floor comparison against 37.0 days — not overridable via knobs.
    effective_days = max(configured_days, float(MIN_COLD_AGE_DAYS))
    if effective_days * 86400.0 < float(MIN_COLD_AGE_SECONDS):
        # Belt-and-suspenders: seconds floor must also hold.
        effective_days = float(MIN_COLD_AGE_SECONDS) / 86400.0
    cold_cutoff = float(generated_at) - effective_days * 86400.0
    hot_cutoff = float(generated_at) - float(hot_days) * 86400.0
    # Hot cutoff itself must never be newer than the hard floor window start.
    floor_hot = float(generated_at) - float(MIN_COLD_AGE_SECONDS)
    if hot_cutoff > floor_hot:
        # hot_ids invariant uses a floor-aware boundary so "hot" always covers
        # anything younger than 37 days even when hot_days is misconfigured low.
        hot_cutoff = floor_hot
    return cold_cutoff, hot_cutoff, effective_days


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
    for sid in ids:
        if (
            conn.execute(
                "SELECT 1 FROM gateway_routing WHERE entry_json LIKE ? LIMIT 1",
                (f"%{sid}%",),
            ).fetchone()
            is not None
        ):
            return True
    return False


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
    rows = conn.execute(
        "SELECT delegation_id, state, delivery_state FROM async_delegations WHERE "
        + " OR ".join(f"({clause})" for clause in checks),
        tuple(params),
    ).fetchall()
    obligations: list[dict[str, Any]] = []
    for row in rows:
        state = str(row["state"] or "").lower()
        delivery = str(row["delivery_state"] or "").lower()
        if (
            state not in _TERMINAL_DELEGATION_STATES
            or delivery not in _TERMINAL_DELIVERY_STATES
        ):
            obligations.append(dict(row))
        else:
            # Gate-B asked for no async references at all.  Terminal rows are
            # still reported as references so the reviewer can decide whether to
            # hold them; by default we skip them as well.
            obligations.append(dict(row))
    return obligations


def _ids_digest(ids: Iterable[str]) -> str:
    payload = "".join(f"{sid}\n" for sid in sorted(ids)).encode("utf-8")
    return _sha256_bytes(payload)


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
    """Build a redacted Gate-B manifest from an offline/read-only snapshot."""

    generated_at = time.time() if now is None else float(now)
    cutoff, hot_cutoff, effective_days = effective_cold_cutoff(
        generated_at=generated_at,
        hot_days=float(hot_days),
        archive_grace_days=float(archive_grace_days),
    )
    source = reject_live_state_db(source_db)
    compiled_title_holds = [re.compile(pattern) for pattern in hold_title_regexes]
    normalized_hold_sources = {str(source_name).lower() for source_name in hold_sources}
    normalized_cwd_holds = [str(prefix) for prefix in hold_cwd_prefixes]

    with _connect_readonly(source) as conn:
        require_sessions_schema(conn)
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
                float(rows[sid].get("actual_last_active") or 0.0) for sid in component
            ]
            for sid in component:
                row = rows[sid]
                if row.get("ended_at") is None:
                    reasons.add("open_session")
                if int(row.get("archived") or 0) != 1:
                    reasons.add("not_archived")
                # pinned is a required durable column (schema-gated above).
                if int(row["pinned"] or 0) != 0:
                    reasons.add("pinned")
                # Exact 37-day boundary is ELIGIBLE (age >= 37d). Skip only when
                # strictly newer than the cold cutoff (last_active > cutoff).
                if float(row.get("actual_last_active") or 0.0) > cutoff:
                    reasons.add("inside_30d_hot_plus_7d_grace")
                for hold_reason in _matches_holds(
                    row,
                    hold_sources=normalized_hold_sources,
                    hold_title_regexes=compiled_title_holds,
                    hold_cwd_prefixes=normalized_cwd_holds,
                ):
                    reasons.add(hold_reason)

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
                continue
            selected_groups.append(summary)
            selected_ids.extend(component)
            selected_message_total += group_message_count

        parent_map = {
            sid: (
                str(row.get("parent_session_id"))
                if row.get("parent_session_id")
                else None
            )
            for sid, row in rows.items()
        }
        manifest = {
            "archive_manifest_version": _ARCHIVE_VERSION,
            "operation": "hermes-state-cold-archive-gate-b",
            "generated_at": utc_iso(generated_at),
            "source_db": str(source),
            "policy": {
                "hot_days": float(hot_days),
                "archive_grace_days": float(archive_grace_days),
                "min_cold_age_days": float(MIN_COLD_AGE_DAYS),
                "min_cold_age_seconds": int(MIN_COLD_AGE_SECONDS),
                "effective_min_age_days": float(effective_days),
                "bundle_retention_seconds": int(BUNDLE_RETENTION_SECONDS),
                "cold_cutoff_epoch": cutoff,
                "cold_cutoff_utc": utc_iso(cutoff),
                "hot_cutoff_epoch": hot_cutoff,
                "hot_cutoff_utc": utc_iso(hot_cutoff),
                "boundary_rule": "last_active > cold_cutoff => skip; exact boundary eligible",
                "must_be_ended": True,
                "must_be_archived": True,
                "must_be_unpinned": True,
                "must_select_whole_parent_child_component": True,
                "actual_messages_rows_not_sessions_message_count": True,
                "required_sessions_columns": sorted(_REQUIRED_SESSIONS_COLUMNS),
                "default_permanent_hold_sources": sorted(normalized_hold_sources),
                "hold_title_regexes": [
                    pattern.pattern for pattern in compiled_title_holds
                ],
                "hold_cwd_prefixes": normalized_cwd_holds,
                "remote_publish_forbids_restricted_ids": True,
                "remote_publish_forbids_parent_map": True,
                "remote_publish_forbids_qmd_plaintext": True,
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
        # restricted_ids_sha256 is public and must be present before digest.
        manifest["restricted_ids_sha256"] = manifest["selected_ids_sha256"]
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        return {
            **manifest,
            "_restricted_selected_ids": sorted(selected_ids),
            "_restricted_parent_map": parent_map,
        }


def _reason_counts(skipped_groups: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for group in skipped_groups:
        for reason in group.get("reasons") or []:
            counts[str(reason)] = counts.get(str(reason), 0) + 1
    return dict(sorted(counts.items()))


def _manifest_digest(manifest: dict[str, Any]) -> str:
    public_manifest = {
        key: value
        for key, value in manifest.items()
        if not key.startswith("_") and key != "manifest_sha256"
    }
    return _sha256_bytes(
        json.dumps(public_manifest, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def write_gate_b_manifest(stage_root: Path, manifest: dict[str, Any]) -> StageArtifacts:
    root = _secure_dir(stage_root.expanduser().resolve(strict=False))
    restricted = _secure_dir(root / "restricted")
    qmd_dir = _secure_dir(root / "cold-qmd")
    rollback_dir = _secure_dir(root / "rollback")
    receipt_path = root / "COLD-ARCHIVE-RECEIPT.json"
    public_manifest = {
        key: value for key, value in manifest.items() if not key.startswith("_")
    }
    manifest_path = root / "GATE-B-MANIFEST.json"
    # Integrity gate: never overwrite an existing Gate-B attestation in place.
    # A second write would replace the very bytes prior reviews may have hashed.
    if manifest_path.exists():
        existing_sha = sha256_path(manifest_path)
        planned_text = (
            json.dumps(public_manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        planned_sha = _sha256_bytes(planned_text.encode("utf-8"))
        if existing_sha != planned_sha:
            raise ColdArchiveError(
                "refusing to overwrite existing GATE-B-MANIFEST.json with different "
                "bytes (integrity gate must not self-overwrite attested content); "
                "use a fresh stage-root"
            )
        # Identical content — leave bytes untouched so attested hash stays stable.
    else:
        _atomic_write_json(manifest_path, public_manifest)
    ids = manifest.get("_restricted_selected_ids") or []
    ids_text = "".join(f"{sid}\n" for sid in ids)
    ids_path = _atomic_write_text(restricted / "selected-session-ids.txt", ids_text)
    groups_path = _atomic_write_json(
        restricted / "lineage-parent-map.json",
        {
            "selected_ids_sha256": public_manifest.get("selected_ids_sha256"),
            "parent_map_sha256": public_manifest.get("parent_map_sha256"),
            "parent_map": manifest.get("_restricted_parent_map") or {},
        },
    )
    return StageArtifacts(
        stage_root=root,
        manifest_path=manifest_path,
        restricted_ids_path=ids_path,
        restricted_groups_path=groups_path,
        qmd_dir=qmd_dir,
        rollback_bundle_path=None,
        rollback_encrypted_path=None,
        receipt_path=receipt_path,
    )


def _copy_rollback_bundle(
    source_db: Path, rollback_dir: Path
) -> tuple[Path, dict[str, Any]]:
    source = reject_live_state_db(source_db)
    bundle_root = _secure_dir(rollback_dir / "source-bundle")
    copied: list[dict[str, Any]] = []
    for suffix in _SIDECAR_SUFFIXES:
        part = source if suffix == "" else source.with_name(source.name + suffix)
        if not part.exists():
            copied.append({"name": part.name, "status": "absent"})
            continue
        destination = bundle_root / part.name
        shutil.copy2(part, destination)
        _chmod_private_file(destination)
        copied.append({
            "name": destination.name,
            "status": "copied",
            "bytes": destination.stat().st_size,
            "sha256": sha256_path(destination),
        })
    manifest_path = _atomic_write_json(
        bundle_root / "ROLLBACK-BUNDLE-MANIFEST.json",
        {"created_at": utc_iso(), "source_db": str(source), "files": copied},
    )
    tar_path = rollback_dir / "rollback-source-bundle.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        for path in sorted(bundle_root.iterdir(), key=lambda p: p.name):
            archive.add(path, arcname=path.name)
    _chmod_private_file(tar_path)
    return tar_path, {
        "path": str(tar_path),
        "sha256": sha256_path(tar_path),
        "bytes": tar_path.stat().st_size,
        "manifest_path": str(manifest_path),
        "files": copied,
    }


def encrypt_file_with_age(
    source: Path,
    output: Path,
    *,
    recipient_file: Path,
    age_exe: str = "age",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if not recipient_file.exists():
        raise ColdArchiveError(f"age recipient file does not exist: {recipient_file}")
    _secure_dir(output.parent)
    tmp = output.with_name(f".{output.name}.{os.getpid()}.partial")
    tmp.unlink(missing_ok=True)
    cmd = [age_exe, "-R", str(recipient_file), "-o", str(tmp), str(source)]
    result = runner(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ColdArchiveError("age encryption failed")
    os.replace(tmp, output)
    _chmod_private_file(output)
    return {
        "path": str(output),
        "sha256": sha256_path(output),
        "bytes": output.stat().st_size,
    }


def _load_session_export(
    conn: sqlite3.Connection, session_ids: Sequence[str]
) -> dict[str, Any]:
    if not session_ids:
        raise ColdArchiveError("cannot export an empty lineage group")
    rows = conn.execute(
        f"SELECT * FROM sessions WHERE id IN ({_placeholders(session_ids)}) ORDER BY started_at ASC, id ASC",
        tuple(session_ids),
    ).fetchall()
    segments: list[dict[str, Any]] = []
    total_messages = 0
    for row in rows:
        segment = dict(row)
        messages = [
            dict(message)
            for message in conn.execute(
                """
                SELECT id, session_id, role, content, tool_call_id, tool_calls,
                       tool_name, timestamp, token_count, finish_reason
                FROM messages
                WHERE session_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (row["id"],),
            ).fetchall()
        ]
        segment["messages"] = messages
        segment["message_count"] = len(messages)
        total_messages += len(messages)
        last_active = conn.execute(
            "SELECT COALESCE(MAX(timestamp), ?) FROM messages WHERE session_id = ?",
            (segment.get("started_at"), row["id"]),
        ).fetchone()[0]
        segment["last_active"] = last_active
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


def _safe_qmd_filename(session: dict[str, Any]) -> str:
    """Build a path-safe QMD filename that cannot escape the export directory.

    Session IDs from the DB are untrusted path components: strip any directory
    separators and ``..`` segments before composing the filename.
    """

    raw_name = safe_session_filename(session, fmt="qmd")
    # Collapse any path segments — session_id may contain ../ or absolute paths.
    leaf = Path(raw_name).name
    leaf = leaf.replace("\x00", "")
    if leaf in {"", ".", ".."} or "/" in leaf or "\\" in leaf:
        raise ColdArchiveError(f"unsafe QMD export filename after sanitization: {raw_name!r}")
    # Only allow a conservative charset in the final leaf.
    if not re.match(r"^[A-Za-z0-9._-]+$", leaf):
        digest = _sha256_bytes(str(session.get("id") or raw_name).encode("utf-8"))[:16]
        leaf = f"session-{digest}.qmd"
    if not leaf.endswith(".qmd"):
        leaf = f"{leaf}.qmd"
    return leaf


def _contained_path(root: Path, relative_name: str) -> Path:
    """Resolve ``root / relative_name`` and refuse path escape."""

    root_resolved = root.expanduser().resolve(strict=False)
    candidate = (root_resolved / relative_name).resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ColdArchiveError(
            f"refusing path escape outside stage directory: {candidate}"
        ) from exc
    return candidate


def export_redacted_qmd(
    source_db: Path,
    stage: StageArtifacts,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    ids = list(manifest.get("_restricted_selected_ids") or [])
    if not ids:
        return {"exported_files": [], "verified": True, "message_count": 0}
    groups = manifest.get("selected_groups") or []
    id_set = set(ids)
    exported: list[dict[str, Any]] = []
    total_messages = 0
    qmd_root = stage.qmd_dir.expanduser().resolve(strict=False)
    with _connect_readonly(reject_live_state_db(source_db)) as conn:
        require_sessions_schema(conn)
        components = _connected_components(_session_rows(conn))
        for component in components:
            if not set(component).issubset(id_set):
                continue
            session = _load_session_export(conn, component)
            redacted = redact_session_data(session)
            filename = _safe_qmd_filename(redacted)
            path = _contained_path(qmd_root, filename)
            # Refuse to clobber Gate-B / restricted / receipt / other stage roots.
            if path.exists() and path.resolve() != path:
                pass
            forbidden_names = {
                "GATE-B-MANIFEST.json",
                "COLD-ARCHIVE-RECEIPT.json",
                "selected-session-ids.txt",
                "lineage-parent-map.json",
            }
            if path.name in forbidden_names:
                raise ColdArchiveError(
                    f"QMD export filename collides with stage integrity artifact: {path.name}"
                )
            text = render_session_markdown(redacted, fmt="qmd")
            _atomic_write_text(path, text)
            ok, reason = verify_export_file(path, redacted)
            if not ok:
                raise ColdArchiveError(
                    f"QMD export verification failed for {path}: {reason}"
                )
            message_count = int(redacted.get("message_count") or 0)
            total_messages += message_count
            exported.append({
                "path": str(path),
                "sha256": sha256_path(path),
                "bytes": path.stat().st_size,
                "lineage_session_ids_sha256": _ids_digest(component),
                "actual_message_count": message_count,
                "witness": {
                    "session_id_present": str(redacted["id"])
                    in path.read_text(encoding="utf-8"),
                    "message_count_marker": f"- Exported messages: `{message_count}`"
                    in path.read_text(encoding="utf-8"),
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


def _run_rclone(
    cmd: list[str],
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    return runner(cmd, text=True, capture_output=True)


def publish_paths_with_rclone(
    paths: Sequence[Path],
    *,
    remote_root: str,
    rclone_config: Path,
    namespace: str,
    rclone_exe: str = "rclone",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, Any]]:
    """Copy files to a bounded rclone remote and verify exact readback.

    This intentionally uses copy/check/readback only.  It never invokes rclone
    sync, delete, purge, dedupe, move, cleanup, or retention verbs.

    Restricted IDs, parent maps, and QMD plaintext are never eligible for
    remote publication — ``assert_publish_paths_are_remote_safe`` enforces that
    before any network call.
    """

    assert_publish_paths_are_remote_safe(paths)
    if not remote_root or re.search(r"[\r\n\x00]", remote_root):
        raise ColdArchiveError("invalid rclone remote root")
    if (
        ".." in namespace
        or namespace.startswith("/")
        or re.search(r"[\r\n\x00]", namespace)
    ):
        raise ColdArchiveError("invalid remote namespace")
    if not rclone_config.exists():
        raise ColdArchiveError(f"rclone config does not exist: {rclone_config}")
    published: list[dict[str, Any]] = []
    remote_base = remote_root.rstrip("/")
    with tempfile.TemporaryDirectory(
        prefix="hermes-cold-archive-rclone-"
    ) as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        for local in paths:
            local = Path(local)
            if not local.is_file():
                raise ColdArchiveError(f"cannot publish missing file: {local}")
            remote_name = f"{namespace.rstrip('/')}/{local.name}"
            if not re.match(r"^[A-Za-z0-9._/-]+$", remote_name):
                raise ColdArchiveError(f"unsafe remote object name: {remote_name}")
            remote = f"{remote_base}/{remote_name}"
            local_sha = sha256_path(local)
            upload = _run_rclone(
                [
                    rclone_exe,
                    "copyto",
                    str(local),
                    remote,
                    "--config",
                    str(rclone_config),
                ],
                runner,
            )
            if upload.returncode != 0:
                raise ColdArchiveError(f"rclone copyto failed for {local.name}")
            check = _run_rclone(
                [
                    rclone_exe,
                    "check",
                    str(local),
                    remote,
                    "--checksum",
                    "--one-way",
                    "--config",
                    str(rclone_config),
                ],
                runner,
            )
            if check.returncode != 0:
                raise ColdArchiveError(
                    f"rclone checksum verification failed for {local.name}"
                )
            readback = tmp_dir / local.name
            pull = _run_rclone(
                [
                    rclone_exe,
                    "copyto",
                    remote,
                    str(readback),
                    "--config",
                    str(rclone_config),
                ],
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
                "integrity": OFFSITE_INTEGRITY_OK,
            })
    return published


def _normalize_actor_identity(value: str | None, *, field: str) -> str:
    if value is None:
        raise ColdArchiveError(f"{field} is required for destructive retention")
    normalized = " ".join(str(value).strip().split())
    if not normalized:
        raise ColdArchiveError(f"{field} must be a non-empty identity")
    if len(normalized) > 256:
        raise ColdArchiveError(f"{field} exceeds maximum length")
    if re.search(r"[\r\n\x00]", normalized):
        raise ColdArchiveError(f"{field} contains illegal control characters")
    return normalized


def require_distinct_gate_b_approval(
    *,
    approved_gate_b_manifest_sha256: str | None,
    requestor_identity: str | None,
    approver_identity: str | None,
    computed_manifest_sha256: str,
    on_disk_manifest_sha256: str | None = None,
) -> dict[str, str]:
    """Refuse self-approve and unattested Gate-B hashes for destructive retention.

    The approved hash must be supplied by an external actor (CLI/operator), not
    silently taken from the in-memory build of this same invocation. The
    approver identity must be distinct from the requestor identity.
    """

    if not approved_gate_b_manifest_sha256:
        raise ColdArchiveError(
            "refusing retention without externally supplied "
            "--approved-gate-b-sha256 (Gate-B must not self-approve)"
        )
    approved = str(approved_gate_b_manifest_sha256).strip().lower()
    if not _SHA256_HEX_RE.match(approved):
        raise ColdArchiveError(
            "approved Gate-B manifest sha256 must be 64 lowercase hex characters"
        )
    requestor = _normalize_actor_identity(
        requestor_identity, field="requestor_identity"
    )
    approver = _normalize_actor_identity(approver_identity, field="approver_identity")
    if approver.casefold() == requestor.casefold():
        raise ColdArchiveError(
            "refusing Gate-B self-approve: approver_identity must be distinct "
            f"from requestor_identity (both are {requestor!r})"
        )
    computed = str(computed_manifest_sha256).strip().lower()
    if computed != approved:
        raise ColdArchiveError(
            "Gate-B manifest hash does not match externally approved "
            "gate_b_manifest_sha256; refusing retention"
        )
    if on_disk_manifest_sha256 is not None:
        disk = str(on_disk_manifest_sha256).strip().lower()
        if disk != approved:
            raise ColdArchiveError(
                "on-disk GATE-B-MANIFEST.json sha256 does not match externally "
                "approved hash; refusing retention"
            )
    return {
        "approved_gate_b_manifest_sha256": approved,
        "requestor_identity": requestor,
        "approver_identity": approver,
    }


def verify_offsite_permanence_receipt(
    receipt: Sequence[dict[str, Any]] | None,
    *,
    required_local_artifacts: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Fail closed unless receipt proves encrypted offsite readback permanence.

    Deletion must not proceed on assumed prior-step success. A valid receipt
    requires at least:

    - public ``GATE-B-MANIFEST.json`` with integrity
      ``rclone-checksum-and-readback-ok`` and matching sha256/readback_sha256
    - one age-encrypted object (name ends with ``.age``) with the same integrity

    When ``required_local_artifacts`` is supplied, each local path must appear
    in the receipt with a matching sha256 (existence + integrity confirmed).
    """

    if not receipt:
        raise ColdArchiveError(
            "refusing deletion without verified offsite permanence receipt "
            "(encrypted publish + readback required before --apply-retention)"
        )
    if not isinstance(receipt, (list, tuple)):
        raise ColdArchiveError("offsite permanence receipt must be a list")

    by_name: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(receipt):
        if not isinstance(entry, dict):
            raise ColdArchiveError(f"offsite receipt entry {index} is not an object")
        local_path = Path(str(entry.get("local_path") or ""))
        name = local_path.name
        if not name:
            raise ColdArchiveError(f"offsite receipt entry {index} missing local_path")
        remote = str(entry.get("remote") or "").strip()
        if not remote:
            raise ColdArchiveError(
                f"offsite receipt entry for {name} missing remote location"
            )
        integrity = str(entry.get("integrity") or "")
        if integrity != OFFSITE_INTEGRITY_OK:
            raise ColdArchiveError(
                f"offsite receipt for {name} lacks verified integrity "
                f"{OFFSITE_INTEGRITY_OK!r} (got {integrity!r})"
            )
        sha = str(entry.get("sha256") or "").strip().lower()
        readback = str(entry.get("readback_sha256") or "").strip().lower()
        if not _SHA256_HEX_RE.match(sha) or not _SHA256_HEX_RE.match(readback):
            raise ColdArchiveError(
                f"offsite receipt for {name} missing valid sha256/readback_sha256"
            )
        if sha != readback:
            raise ColdArchiveError(
                f"offsite receipt for {name} sha256 != readback_sha256"
            )
        try:
            size = int(entry.get("bytes"))
        except (TypeError, ValueError) as exc:
            raise ColdArchiveError(
                f"offsite receipt for {name} missing valid byte size"
            ) from exc
        if size < 0:
            raise ColdArchiveError(f"offsite receipt for {name} has negative bytes")
        by_name[name] = entry

    if "GATE-B-MANIFEST.json" not in by_name:
        raise ColdArchiveError(
            "offsite permanence receipt missing verified GATE-B-MANIFEST.json"
        )
    age_names = [name for name in by_name if name.endswith(".age")]
    if not age_names:
        raise ColdArchiveError(
            "offsite permanence receipt missing verified age-encrypted rollback "
            "bundle (.age)"
        )

    if required_local_artifacts is not None:
        if not required_local_artifacts:
            raise ColdArchiveError(
                "required_local_artifacts must not be empty when supplied"
            )
        for artifact in required_local_artifacts:
            path = Path(artifact)
            if not path.is_file():
                raise ColdArchiveError(
                    f"required offsite artifact missing on disk: {path}"
                )
            name = path.name
            if name not in by_name:
                raise ColdArchiveError(
                    f"offsite receipt does not cover required artifact: {name}"
                )
            local_sha = sha256_path(path)
            entry_sha = str(by_name[name]["sha256"]).strip().lower()
            if local_sha != entry_sha:
                raise ColdArchiveError(
                    f"offsite receipt sha256 does not match local artifact {name}"
                )
            local_size = path.stat().st_size
            if int(by_name[name]["bytes"]) != local_size:
                raise ColdArchiveError(
                    f"offsite receipt size does not match local artifact {name}"
                )

    return {
        "verified": True,
        "integrity": OFFSITE_INTEGRITY_OK,
        "objects": sorted(by_name),
        "gate_b_sha256": str(by_name["GATE-B-MANIFEST.json"]["sha256"]).lower(),
        "encrypted_bundle_names": sorted(age_names),
    }


def _capture_invariants(
    conn: sqlite3.Connection, *, hot_cutoff: float
) -> dict[str, Any]:
    # Fail closed: pinned / last_activity_at must exist. Never invent an empty
    # pinned_ids set from a missing column.
    require_sessions_schema(conn)
    pinned_ids = sorted(
        str(row[0])
        for row in conn.execute("SELECT id FROM sessions WHERE pinned = 1")
    )
    invariants: dict[str, Any] = {
        "sessions": _count_where(conn, "SELECT COUNT(*) FROM sessions"),
        "messages": _count_where(conn, "SELECT COUNT(*) FROM messages"),
        "open_ids": sorted(
            str(row[0])
            for row in conn.execute("SELECT id FROM sessions WHERE ended_at IS NULL")
        ),
        "pinned_ids": pinned_ids,
        "hot_ids": sorted(
            str(row[0])
            for row in conn.execute(
                """
                SELECT s.id FROM sessions s
                WHERE COALESCE(
                    s.last_activity_at,
                    (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id),
                    s.started_at
                ) > ?
                """,
                (hot_cutoff,),
            )
        ),
        "parent_map": {
            str(row[0]): (str(row[1]) if row[1] is not None else None)
            for row in conn.execute(
                "SELECT id, parent_session_id FROM sessions ORDER BY id"
            )
        },
    }
    return invariants


def _row_payload_sha256(rows: Sequence[Sequence[Any]]) -> str:
    return _sha256_bytes(
        json.dumps(
            list(rows), ensure_ascii=False, sort_keys=False, separators=(",", ":")
        ).encode("utf-8")
    )


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
        ).fetchall()
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
    rows = [
        [
            int(row[0]),
            str(row[1]),
            str(row[2]),
            row[3] if row[3] is not None else "",
            row[4] if row[4] is not None else "",
            row[5] if row[5] is not None else "",
            float(row[6]),
        ]
        for row in conn.execute(
            f"""
            SELECT id, session_id, role, content, tool_name, tool_call_id, timestamp
            FROM messages
            {where}
            ORDER BY id ASC
            """,
            tuple(params),
        ).fetchall()
    ]
    return {"count": len(rows), "sha256": _row_payload_sha256(rows)}


def _fts_survivor_rowids_digest(
    conn: sqlite3.Connection, table: str, excluded_message_ids: Sequence[int]
) -> dict[str, Any] | None:
    if not _table_exists(conn, table):
        return None
    if excluded_message_ids:
        placeholders = _placeholders([str(i) for i in excluded_message_ids])
        where = f"WHERE rowid NOT IN ({placeholders})"
        params: Sequence[Any] = tuple(excluded_message_ids)
    else:
        where = ""
        params = ()
    rowids = [
        int(row[0])
        for row in conn.execute(
            f'SELECT rowid FROM "{table}" {where} ORDER BY rowid ASC',
            tuple(params),
        ).fetchall()
    ]
    return {
        "count": len(rowids),
        "sha256": _row_payload_sha256([[rowid] for rowid in rowids]),
    }


def _capture_search_survivor_invariants(
    conn: sqlite3.Connection, selected_session_ids: Sequence[str]
) -> dict[str, Any]:
    excluded_ids = _excluded_message_ids(conn, selected_session_ids)
    payload: dict[str, Any] = {
        "messages": _survivor_message_digest(conn, selected_session_ids),
    }
    for table in ("messages_fts", "messages_fts_trigram"):
        digest = _fts_survivor_rowids_digest(conn, table, excluded_ids)
        if digest is not None:
            payload[table] = digest
    return payload


def _verify_fts_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    message_count = _count_where(conn, "SELECT COUNT(*) FROM messages")
    non_tool_count = _count_where(
        conn, "SELECT COUNT(*) FROM messages WHERE role <> 'tool'"
    )
    checks["messages"] = message_count
    checks["non_tool_messages"] = non_tool_count
    if _table_exists(conn, "messages_fts"):
        fts_count = _count_where(conn, "SELECT COUNT(*) FROM messages_fts")
        checks["messages_fts"] = fts_count
        if fts_count != message_count:
            raise ColdArchiveError("messages_fts count does not equal messages count")
    if _table_exists(conn, "messages_fts_trigram"):
        trigram_count = _count_where(conn, "SELECT COUNT(*) FROM messages_fts_trigram")
        checks["messages_fts_trigram"] = trigram_count
        if trigram_count != non_tool_count:
            raise ColdArchiveError(
                "messages_fts_trigram count does not equal non-tool messages count"
            )
    return checks


def apply_retention_to_candidate(
    candidate_db: Path,
    manifest: dict[str, Any],
    *,
    expected_manifest_sha256: str,
    offsite_permanence_receipt: Sequence[dict[str, Any]],
    requestor_identity: str | None = None,
    approver_identity: str | None = None,
    gate_b_approval: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Delete only the reviewed selected IDs from an offline candidate copy.

    Deletion and invariant checks share one transaction: any post-delete
    invariant failure rolls the deletion back. ``system_prompts`` rows are
    never deleted (out of declared cold-archive scope).

    Custody gates (fail closed):

    - ``expected_manifest_sha256`` is required and must match the manifest
      (externally approved Gate-B hash — not optional self-hash).
    - When ``requestor_identity`` / ``approver_identity`` are supplied (or a
      precomputed ``gate_b_approval``), self-approve is refused.
    - ``offsite_permanence_receipt`` must positively verify encrypted offsite
      readback of Gate-B + age bundle before any DELETE.
    """

    if not expected_manifest_sha256:
        raise ColdArchiveError(
            "expected_manifest_sha256 is required; refusing unattested retention"
        )
    expected = str(expected_manifest_sha256).strip().lower()
    if not _SHA256_HEX_RE.match(expected):
        raise ColdArchiveError(
            "expected_manifest_sha256 must be 64 lowercase hex characters"
        )
    if expected != str(manifest.get("manifest_sha256") or "").strip().lower():
        raise ColdArchiveError("Gate-B manifest hash mismatch; refusing retention")

    approval = gate_b_approval
    if approval is None and (
        requestor_identity is not None or approver_identity is not None
    ):
        approval = require_distinct_gate_b_approval(
            approved_gate_b_manifest_sha256=expected,
            requestor_identity=requestor_identity,
            approver_identity=approver_identity,
            computed_manifest_sha256=str(manifest.get("manifest_sha256") or ""),
        )
    elif approval is None:
        # Direct library callers must still prove distinct approval when deleting.
        raise ColdArchiveError(
            "refusing retention without distinct Gate-B approval identities "
            "(requestor_identity and approver_identity required)"
        )
    else:
        # Re-validate precomputed approval against this call's expected hash.
        require_distinct_gate_b_approval(
            approved_gate_b_manifest_sha256=approval.get(
                "approved_gate_b_manifest_sha256"
            ),
            requestor_identity=approval.get("requestor_identity"),
            approver_identity=approval.get("approver_identity"),
            computed_manifest_sha256=str(manifest.get("manifest_sha256") or ""),
        )

    offsite_verified = verify_offsite_permanence_receipt(offsite_permanence_receipt)

    ids = sorted(str(sid) for sid in (manifest.get("_restricted_selected_ids") or []))
    if _ids_digest(ids) != manifest.get("selected_ids_sha256"):
        raise ColdArchiveError("restricted selected IDs do not match manifest digest")
    if not ids:
        return {
            "applied": False,
            "reason": "no selected sessions",
            "gate_b_approval": approval,
            "offsite_permanence": offsite_verified,
        }

    hot_cutoff = float(manifest["policy"]["hot_cutoff_epoch"])
    with _connect_candidate(candidate_db) as conn:
        require_sessions_schema(conn)
        before = _capture_invariants(conn, hot_cutoff=hot_cutoff)
        before_search_survivors = _capture_search_survivor_invariants(conn, ids)
        existing = sorted(
            str(row[0])
            for row in conn.execute(
                f"SELECT id FROM sessions WHERE id IN ({_placeholders(ids)})",
                tuple(ids),
            )
        )
        if existing != ids:
            raise ColdArchiveError(
                "candidate database no longer matches manifest selected IDs"
            )
        external_children = [
            str(row[0])
            for row in conn.execute(
                f"""
                SELECT id FROM sessions
                WHERE parent_session_id IN ({_placeholders(ids)})
                  AND id NOT IN ({_placeholders(ids)})
                """,
                tuple(ids) + tuple(ids),
            )
        ]
        if external_children:
            raise ColdArchiveError("selected set would orphan surviving child sessions")
        if _async_delegation_obligations(conn, ids) or _gateway_routing_references(
            conn, ids
        ):
            raise ColdArchiveError(
                "selected set gained async/gateway references after manifest"
            )

        selected_messages = _count_where(
            conn,
            f"SELECT COUNT(*) FROM messages WHERE session_id IN ({_placeholders(ids)})",
            ids,
        )
        if selected_messages != int(manifest["counts"]["selected_messages_actual"]):
            raise ColdArchiveError("selected message count changed after manifest")

        conn.execute("BEGIN IMMEDIATE")
        try:
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
                f"DELETE FROM sessions WHERE id IN ({_placeholders(ids)})",
                tuple(ids),
            )
            # NOTE: deliberately do NOT touch system_prompts. Orphan prompt rows
            # are outside declared cold-archive scope.

            # Invariant checks run INSIDE the same transaction so failure rolls
            # the deletion back rather than retaining a post-COMMIT failure.
            after = _capture_invariants(conn, hot_cutoff=hot_cutoff)
            survivor_parent_before = {
                sid: parent
                for sid, parent in before["parent_map"].items()
                if sid not in set(ids)
            }
            if after["parent_map"] != survivor_parent_before:
                raise ColdArchiveError("surviving parent_session_id map changed")
            if before["sessions"] - after["sessions"] != len(ids):
                raise ColdArchiveError(
                    "session delta does not equal manifest selected count"
                )
            if before["messages"] - after["messages"] != selected_messages:
                raise ColdArchiveError(
                    "message delta does not equal manifest selected message count"
                )
            for invariant_key in ("open_ids", "pinned_ids", "hot_ids"):
                if before[invariant_key] != after[invariant_key]:
                    raise ColdArchiveError(f"{invariant_key} changed during retention")

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
            fts = _verify_fts_counts(conn)
            after_search_survivors = _capture_search_survivor_invariants(conn, [])
            if after_search_survivors != before_search_survivors:
                raise ColdArchiveError("surviving message/search-index invariants changed")

            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

        return {
            "applied": True,
            "deleted_sessions": len(ids),
            "deleted_messages_actual": selected_messages,
            "integrity_check": integrity,
            "foreign_key_check_rows": len(foreign_keys),
            "fts_counts": fts,
            "survivor_search_invariants": after_search_survivors,
            "survivor_parent_map_sha256": _sha256_bytes(
                json.dumps(
                    after["parent_map"], sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ),
            "system_prompts_deleted": 0,
            "gate_b_approval": approval,
            "offsite_permanence": offsite_verified,
        }


def classify_bundle_retention(
    *,
    bundle_mtime_epoch: float,
    now_epoch: float,
    retention_seconds: int = BUNDLE_RETENTION_SECONDS,
) -> str:
    """Return ``retain`` or ``eligible_for_purge`` for a rollback bundle.

    Retention is a hard floor of ``retention_seconds`` (default 14 days =
    1_209_600s). Bundles younger than the floor must never be purged by this
    module. Callers that implement purge must use this classifier.
    """

    if retention_seconds < 0:
        raise ColdArchiveError("bundle retention_seconds must be non-negative")
    age = float(now_epoch) - float(bundle_mtime_epoch)
    if age < float(retention_seconds):
        return "retain"
    return "eligible_for_purge"


def assert_publish_paths_are_remote_safe(paths: Sequence[Path]) -> None:
    """Refuse remote publication of restricted IDs, parent maps, or QMD plaintext."""

    forbidden_suffixes = (".qmd",)
    forbidden_names = {
        "selected-session-ids.txt",
        "lineage-parent-map.json",
    }
    for path in paths:
        name = Path(path).name
        if name in forbidden_names:
            raise ColdArchiveError(
                f"refusing remote publication of restricted artifact: {name}"
            )
        if name.endswith(forbidden_suffixes):
            raise ColdArchiveError(
                f"refusing remote publication of QMD plaintext: {name}"
            )
        # Also catch paths under restricted/ regardless of leaf name.
        parts = {p.lower() for p in Path(path).parts}
        if "restricted" in parts:
            raise ColdArchiveError(
                f"refusing remote publication of path under restricted/: {path}"
            )
        if "cold-qmd" in parts:
            raise ColdArchiveError(
                f"refusing remote publication of path under cold-qmd/: {path}"
            )


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
    rclone_remote: str | None = None,
    rclone_config: Path | None = None,
    remote_namespace: str | None = None,
    age_recipient_file: Path | None = None,
    age_exe: str = "age",
    rclone_exe: str = "rclone",
    approved_gate_b_manifest_sha256: str | None = None,
    requestor_identity: str | None = None,
    approver_identity: str | None = None,
    verified_offsite_receipt: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source = reject_live_state_db(source_db)
    old_umask = os.umask(0o077)
    try:
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
        rollback_report: dict[str, Any] | None = None
        rollback_encrypted: dict[str, Any] | None = None
        qmd_report: dict[str, Any] = {
            "exported_files": [],
            "verified": True,
            "message_count": 0,
        }
        remote_report: list[dict[str, Any]] = []
        retention_report: dict[str, Any] = {"applied": False, "reason": "manifest-only"}
        gate_b_approval: dict[str, str] | None = None
        offsite_verified: dict[str, Any] | None = None

        if not manifest_only and manifest["counts"]["selected_sessions"]:
            rollback_path, rollback_report = _copy_rollback_bundle(
                source, stage.stage_root / "rollback"
            )
            object.__setattr__(stage, "rollback_bundle_path", rollback_path)
            rollback_report["bundle_retention_seconds"] = int(BUNDLE_RETENTION_SECONDS)
            rollback_report["bundle_retention_policy"] = (
                "retain bundles younger than bundle_retention_seconds; "
                "purge eligibility is classify_bundle_retention only"
            )
            qmd_report = export_redacted_qmd(source, stage, manifest)
            if (
                qmd_report["message_count"]
                != manifest["counts"]["selected_messages_actual"]
            ):
                raise ColdArchiveError("QMD message count does not match manifest")
            if apply_retention or rclone_remote:
                # Destructive retention always requires encrypted offsite publish
                # + readback in this pass (or a pre-verified receipt covering the
                # same local artifacts). Publish path is no longer optional when
                # deleting.
                if apply_retention and not rclone_remote and not verified_offsite_receipt:
                    raise ColdArchiveError(
                        "refusing --apply-retention without offsite permanence: "
                        "set --rclone-remote (and config/age recipient) or supply "
                        "a verified offsite receipt covering Gate-B + .age bundle"
                    )
                if rclone_remote:
                    if rclone_config is None:
                        raise ColdArchiveError(
                            "--rclone-config is required when --rclone-remote is set"
                        )
                    if age_recipient_file is None:
                        raise ColdArchiveError(
                            "--age-recipient-file is required before publishing "
                            "rollback bundle"
                        )
                    encrypted_path = rollback_path.with_suffix(
                        rollback_path.suffix + ".age"
                    )
                    rollback_encrypted = encrypt_file_with_age(
                        rollback_path,
                        encrypted_path,
                        recipient_file=age_recipient_file,
                        age_exe=age_exe,
                    )
                    object.__setattr__(stage, "rollback_encrypted_path", encrypted_path)
                    # NEVER publish restricted IDs, parent maps, or QMD plaintext.
                    # Allowed remote objects: public Gate-B manifest + age-encrypted
                    # rollback bundle only.
                    publish_list = [
                        stage.manifest_path,
                        encrypted_path,
                    ]
                    assert_publish_paths_are_remote_safe(publish_list)
                    namespace = (
                        remote_namespace or f"hermes-state/{manifest['manifest_sha256']}"
                    )
                    remote_report = publish_paths_with_rclone(
                        publish_list,
                        remote_root=rclone_remote,
                        rclone_config=rclone_config,
                        namespace=namespace,
                        rclone_exe=rclone_exe,
                    )
            if apply_retention:
                on_disk_payload = json.loads(
                    stage.manifest_path.read_text(encoding="utf-8")
                )
                on_disk_logical_sha = _manifest_digest(on_disk_payload)
                gate_b_approval = require_distinct_gate_b_approval(
                    approved_gate_b_manifest_sha256=approved_gate_b_manifest_sha256,
                    requestor_identity=requestor_identity,
                    approver_identity=approver_identity,
                    computed_manifest_sha256=str(manifest["manifest_sha256"]),
                    on_disk_manifest_sha256=on_disk_logical_sha,
                )
                permanence_source = (
                    list(verified_offsite_receipt)
                    if verified_offsite_receipt is not None
                    else list(remote_report)
                )
                required_artifacts: list[Path] = [stage.manifest_path]
                enc_path = stage.rollback_encrypted_path
                if enc_path is not None and Path(enc_path).is_file():
                    required_artifacts.append(Path(enc_path))
                elif verified_offsite_receipt is None:
                    raise ColdArchiveError(
                        "encrypted rollback bundle missing; cannot verify offsite "
                        "permanence before deletion"
                    )
                offsite_verified = verify_offsite_permanence_receipt(
                    permanence_source,
                    required_local_artifacts=required_artifacts,
                )
                retention_report = apply_retention_to_candidate(
                    source,
                    manifest,
                    expected_manifest_sha256=gate_b_approval[
                        "approved_gate_b_manifest_sha256"
                    ],
                    offsite_permanence_receipt=permanence_source,
                    gate_b_approval=gate_b_approval,
                )
            else:
                retention_report = {
                    "applied": False,
                    "reason": "apply-retention-not-set",
                }

        elif apply_retention and not manifest["counts"]["selected_sessions"]:
            retention_report = {
                "applied": False,
                "reason": "no selected sessions",
            }
        elif apply_retention and manifest_only:
            raise ColdArchiveError(
                "refusing --apply-retention with --manifest-only "
                "(export + verified offsite permanence required before delete)"
            )

        receipt = {
            "operation": "hermes-state-cold-archive-pass",
            "created_at": utc_iso(),
            "source_db": str(source),
            "stage_root": str(stage.stage_root),
            "manifest_path": str(stage.manifest_path),
            "manifest_sha256": sha256_path(stage.manifest_path),
            "gate_b_manifest_sha256": manifest["manifest_sha256"],
            "gate_b_approval": gate_b_approval,
            "restricted_ids_path": str(stage.restricted_ids_path),
            "restricted_ids_sha256": sha256_path(stage.restricted_ids_path),
            "qmd_export": qmd_report,
            "rollback_bundle": rollback_report,
            "rollback_encrypted": rollback_encrypted,
            "remote_publish": remote_report,
            "offsite_permanence": offsite_verified,
            "remote_publish_policy": {
                "publishes_restricted_ids": False,
                "publishes_parent_map": False,
                "publishes_qmd_plaintext": False,
                "allowed_objects": [
                    "GATE-B-MANIFEST.json",
                    "rollback-source-bundle.tar.gz.age",
                ],
                "deletion_requires_verified_offsite": True,
                "deletion_requires_distinct_approver": True,
            },
            "bundle_retention_seconds": int(BUNDLE_RETENTION_SECONDS),
            "retention": retention_report,
            "live_path_mutated": False,
            "vacuum_optimize_checkpoint_invoked": False,
            "auto_prune_enabled": False,
        }
        _atomic_write_json(stage.receipt_path, receipt)
        receipt["receipt_path"] = str(stage.receipt_path)
        receipt["receipt_sha256"] = sha256_path(stage.receipt_path)
        return receipt
    finally:
        os.umask(old_umask)


__all__ = [
    "BUNDLE_RETENTION_SECONDS",
    "ColdArchiveError",
    "DEFAULT_PERMANENT_HOLD_SOURCES",
    "MIN_COLD_AGE_DAYS",
    "MIN_COLD_AGE_SECONDS",
    "OFFSITE_INTEGRITY_OK",
    "StageArtifacts",
    "apply_retention_to_candidate",
    "assert_publish_paths_are_remote_safe",
    "build_gate_b_manifest",
    "classify_bundle_retention",
    "effective_cold_cutoff",
    "export_redacted_qmd",
    "publish_paths_with_rclone",
    "reject_live_state_db",
    "require_distinct_gate_b_approval",
    "require_sessions_schema",
    "run_cold_archive_pass",
    "verify_offsite_permanence_receipt",
    "write_gate_b_manifest",
]
