"""Offline, staged cold archival for Hermes ``state.db`` session history.

This module is intentionally separate from ``SessionDB.prune_sessions``.  The
normal prune path is for interactive/live cleanup and reparents surviving child
sessions to ``NULL``.  Cold archival is the opposite posture: it only mutates an
offline candidate copy after a reviewed Gate-B manifest, a restricted redacted
QMD export, a lossless rollback bundle, and optional fail-closed offsite
readback verification.
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
_ARCHIVE_VERSION = 1
_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")
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
    return paths


def reject_live_state_db(source_path: Path) -> Path:
    """Return a resolved source path or fail if it is the active profile DB.

    The guard deliberately uses ``resolve(strict=True)`` for the candidate and
    ``resolve(strict=False)`` for known live paths so symlinks cannot route a
    destructive pass onto ``~/.hermes/state.db``.
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
    return source


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
    rows = conn.execute(
        """
        SELECT s.*,
               COALESCE((SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id),
                        s.started_at) AS actual_last_active,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS actual_message_count
        FROM sessions s
        ORDER BY s.started_at ASC, s.id ASC
        """
    ).fetchall()
    return {str(row["id"]): dict(row) for row in rows}


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

    if hot_days < 0 or archive_grace_days < 0:
        raise ColdArchiveError("hot_days and archive_grace_days must be non-negative")
    generated_at = time.time() if now is None else float(now)
    cutoff = generated_at - (float(hot_days) + float(archive_grace_days)) * 86400.0
    hot_cutoff = generated_at - float(hot_days) * 86400.0
    source = reject_live_state_db(source_db)
    compiled_title_holds = [re.compile(pattern) for pattern in hold_title_regexes]
    normalized_hold_sources = {str(source_name).lower() for source_name in hold_sources}
    normalized_cwd_holds = [str(prefix) for prefix in hold_cwd_prefixes]

    with _connect_readonly(source) as conn:
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
                if int(row.get("pinned") or 0) != 0:
                    reasons.add("pinned")
                if float(row.get("actual_last_active") or 0.0) >= cutoff:
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
                "cold_cutoff_epoch": cutoff,
                "cold_cutoff_utc": utc_iso(cutoff),
                "hot_cutoff_epoch": hot_cutoff,
                "hot_cutoff_utc": utc_iso(hot_cutoff),
                "must_be_ended": True,
                "must_be_archived": True,
                "must_be_unpinned": True,
                "must_select_whole_parent_child_component": True,
                "actual_messages_rows_not_sessions_message_count": True,
                "default_permanent_hold_sources": sorted(normalized_hold_sources),
                "hold_title_regexes": [
                    pattern.pattern for pattern in compiled_title_holds
                ],
                "hold_cwd_prefixes": normalized_cwd_holds,
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
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        manifest["restricted_ids_sha256"] = manifest["selected_ids_sha256"]
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
    manifest_path = _atomic_write_json(root / "GATE-B-MANIFEST.json", public_manifest)
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
    with _connect_readonly(reject_live_state_db(source_db)) as conn:
        components = _connected_components(_session_rows(conn))
        for component in components:
            if not set(component).issubset(id_set):
                continue
            session = _load_session_export(conn, component)
            redacted = redact_session_data(session)
            filename = safe_session_filename(redacted, fmt="qmd")
            path = stage.qmd_dir / filename
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
    """

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
                "integrity": "rclone-checksum-and-readback-ok",
            })
    return published


def _capture_invariants(
    conn: sqlite3.Connection, *, hot_cutoff: float
) -> dict[str, Any]:
    pinned_ids = (
        sorted(
            str(row[0])
            for row in conn.execute("SELECT id FROM sessions WHERE pinned = 1")
        )
        if _has_column(conn, "sessions", "pinned")
        else []
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
                WHERE COALESCE((SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id),
                               s.started_at) >= ?
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
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Delete only the reviewed selected IDs from an offline candidate copy."""

    ids = sorted(str(sid) for sid in (manifest.get("_restricted_selected_ids") or []))
    if expected_manifest_sha256 and expected_manifest_sha256 != manifest.get(
        "manifest_sha256"
    ):
        raise ColdArchiveError("Gate-B manifest hash mismatch; refusing retention")
    if _ids_digest(ids) != manifest.get("selected_ids_sha256"):
        raise ColdArchiveError("restricted selected IDs do not match manifest digest")
    if not ids:
        return {"applied": False, "reason": "no selected sessions"}

    hot_cutoff = float(manifest["policy"]["hot_cutoff_epoch"])
    with _connect_candidate(candidate_db) as conn:
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
            if _table_exists(conn, "system_prompts"):
                conn.execute(
                    """
                    DELETE FROM system_prompts
                    WHERE NOT EXISTS (
                        SELECT 1 FROM sessions
                        WHERE sessions.system_prompt_hash = system_prompts.hash
                    )
                    """
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

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
        }


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

        if not manifest_only and manifest["counts"]["selected_sessions"]:
            rollback_path, rollback_report = _copy_rollback_bundle(
                source, stage.stage_root / "rollback"
            )
            object.__setattr__(stage, "rollback_bundle_path", rollback_path)
            qmd_report = export_redacted_qmd(source, stage, manifest)
            if (
                qmd_report["message_count"]
                != manifest["counts"]["selected_messages_actual"]
            ):
                raise ColdArchiveError("QMD message count does not match manifest")
            if rclone_remote:
                if rclone_config is None:
                    raise ColdArchiveError(
                        "--rclone-config is required when --rclone-remote is set"
                    )
                if age_recipient_file is None:
                    raise ColdArchiveError(
                        "--age-recipient-file is required before publishing rollback bundle"
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
                publish_list = [
                    stage.manifest_path,
                    stage.restricted_ids_path,
                    stage.restricted_groups_path,
                    *[Path(item["path"]) for item in qmd_report["exported_files"]],
                    encrypted_path,
                ]
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
                retention_report = apply_retention_to_candidate(
                    source,
                    manifest,
                    expected_manifest_sha256=manifest["manifest_sha256"],
                )
            else:
                retention_report = {
                    "applied": False,
                    "reason": "apply-retention-not-set",
                }

        receipt = {
            "operation": "hermes-state-cold-archive-pass",
            "created_at": utc_iso(),
            "source_db": str(source),
            "stage_root": str(stage.stage_root),
            "manifest_path": str(stage.manifest_path),
            "manifest_sha256": sha256_path(stage.manifest_path),
            "gate_b_manifest_sha256": manifest["manifest_sha256"],
            "restricted_ids_path": str(stage.restricted_ids_path),
            "restricted_ids_sha256": sha256_path(stage.restricted_ids_path),
            "qmd_export": qmd_report,
            "rollback_bundle": rollback_report,
            "rollback_encrypted": rollback_encrypted,
            "remote_publish": remote_report,
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
    "ColdArchiveError",
    "DEFAULT_PERMANENT_HOLD_SOURCES",
    "StageArtifacts",
    "apply_retention_to_candidate",
    "build_gate_b_manifest",
    "export_redacted_qmd",
    "publish_paths_with_rclone",
    "reject_live_state_db",
    "run_cold_archive_pass",
    "write_gate_b_manifest",
]
