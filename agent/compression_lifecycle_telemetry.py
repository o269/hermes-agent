"""Content-free compression / Codex / gateway memory telemetry.

Emits only sizes, hashes, counts, and process RSS metrics. Never logs message
contents, credentials, replay payloads, raw reasoning, or tool results.

Designed for the seat-survival incident class where a large compression can
amplify parent RSS without any phase-boundary visibility. Fail-open everywhere:
telemetry must never break compression or stream consumption.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

logger = logging.getLogger(__name__)

# Field keys measured for compression preflight. Sizes/hashes only.
_MESSAGE_FIELD_KEYS: tuple[str, ...] = (
    "content",
    "tool_calls",
    "api_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)

# Hard bounds so a pathological session cannot flood logs.
_MAX_LOG_LINE_CHARS = 4_096
_MAX_TOP_SESSIONS = 5
_MAX_PHASE_LOGS_PER_ATTEMPT = 16
_CODEX_EVENT_LOG_EVERY = 100
_CODEX_EVENT_LOG_INTERVAL_S = 5.0
_HASH_HEX_LEN = 12

_phase_log_counts: dict[str, int] = {}
_phase_log_lock = threading.Lock()


def reset_telemetry_state_for_tests() -> None:
    """Clear process-local rate-limit state (tests only)."""
    with _phase_log_lock:
        _phase_log_counts.clear()


def collect_process_memory_snapshot() -> dict[str, int | None]:
    """Return VmRSS / RssAnon / RssFile / PSS / thread_count (Linux best-effort)."""
    snapshot: dict[str, int | None] = {
        "pid": os.getpid(),
        "rss_kib": None,
        "rss_anon_kib": None,
        "rss_file_kib": None,
        "pss_kib": None,
        "thread_count": threading.active_count(),
    }
    if sys.platform != "linux":
        return snapshot
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        status = ""
    if status:
        wanted = {
            "VmRSS": "rss_kib",
            "RssAnon": "rss_anon_kib",
            "RssFile": "rss_file_kib",
        }
        for line in status.splitlines():
            key, sep, raw = line.partition(":")
            if not sep or key not in wanted:
                continue
            parts = raw.strip().split(maxsplit=1)
            if parts and parts[0].isdigit():
                snapshot[wanted[key]] = int(parts[0])
    try:
        rollup = Path("/proc/self/smaps_rollup").read_text(encoding="utf-8")
    except OSError:
        rollup = ""
    if rollup:
        for line in rollup.splitlines():
            if not line.startswith("Pss:"):
                continue
            parts = line.split()
            # "Pss:   1234 kB"
            if len(parts) >= 2 and parts[1].isdigit():
                snapshot["pss_kib"] = int(parts[1])
            break
    return snapshot


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8", errors="replace"))


def serialized_byte_len(value: Any) -> int:
    """Stable UTF-8 byte length for arbitrary message field values."""
    if value is None or value == "" or value == [] or value == {}:
        return 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, str):
        return _utf8_len(value)
    try:
        return _utf8_len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        )
    except (TypeError, ValueError):
        return _utf8_len(str(value))


def content_free_hash(value: Any) -> str | None:
    """Return a short blake2b hex digest of *value* bytes, or None if empty.

    The digest is one-way; callers must never log the preimage.
    """
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    elif isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")
    else:
        try:
            raw = json.dumps(
                value, ensure_ascii=False, sort_keys=True, default=str
            ).encode("utf-8", errors="replace")
        except (TypeError, ValueError):
            raw = str(value).encode("utf-8", errors="replace")
    if not raw:
        return None
    return hashlib.blake2b(raw, digest_size=6).hexdigest()[:_HASH_HEX_LEN]


def measure_message_field_bytes(
    messages: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Aggregate per-field byte totals and combined hashes across *messages*.

    Output shape (all content-free)::

        {
          "message_count": int,
          "fields": {
            "<key>": {"bytes": int, "nonempty": int, "hash": str|None},
            ...
          },
          "total_tracked_bytes": int,
        }
    """
    field_bytes = {key: 0 for key in _MESSAGE_FIELD_KEYS}
    field_nonempty = {key: 0 for key in _MESSAGE_FIELD_KEYS}
    # Rolling digest material: field key + byte length only (not content).
    # Plus a separate content hash stream that never leaves this function as
    # raw bytes — only the final short hex is emitted.
    hashers = {key: hashlib.blake2b(digest_size=6) for key in _MESSAGE_FIELD_KEYS}
    count = 0
    if messages:
        for msg in messages:
            if not isinstance(msg, Mapping):
                continue
            count += 1
            for key in _MESSAGE_FIELD_KEYS:
                if key not in msg:
                    continue
                value = msg.get(key)
                nbytes = serialized_byte_len(value)
                if nbytes <= 0:
                    continue
                field_bytes[key] += nbytes
                field_nonempty[key] += 1
                # Length-keyed rolling input (content-free correlation) PLUS
                # one-way content digest contribution.
                hashers[key].update(f"{key}:{nbytes}\n".encode("ascii"))
                digest = content_free_hash(value)
                if digest:
                    hashers[key].update(digest.encode("ascii"))

    fields: dict[str, dict[str, Any]] = {}
    total = 0
    for key in _MESSAGE_FIELD_KEYS:
        nbytes = field_bytes[key]
        total += nbytes
        fields[key] = {
            "bytes": nbytes,
            "nonempty": field_nonempty[key],
            "hash": hashers[key].hexdigest()[:_HASH_HEX_LEN] if nbytes else None,
        }
    return {
        "message_count": count,
        "fields": fields,
        "total_tracked_bytes": total,
    }


def _clip_log_line(line: str) -> str:
    if len(line) <= _MAX_LOG_LINE_CHARS:
        return line
    return line[: _MAX_LOG_LINE_CHARS - 18] + "...[truncated]"


def _allow_phase_log(attempt_id: str) -> bool:
    if not attempt_id:
        attempt_id = "_"
    with _phase_log_lock:
        used = _phase_log_counts.get(attempt_id, 0)
        if used >= _MAX_PHASE_LOGS_PER_ATTEMPT:
            return False
        _phase_log_counts[attempt_id] = used + 1
        # Opportunistic prune when the map grows (long-lived gateway).
        if len(_phase_log_counts) > 256:
            # Drop oldest-ish half by insertion order (CPython 3.7+).
            for stale in list(_phase_log_counts.keys())[:128]:
                _phase_log_counts.pop(stale, None)
        return True


def log_compression_phase(
    phase: str,
    *,
    attempt_id: str = "",
    session_id: str = "",
    extra: Mapping[str, Any] | None = None,
    started_at: float | None = None,
    log: logging.Logger | None = None,
) -> dict[str, Any]:
    """Emit one content-free JSON phase line with process memory snapshot.

    Returns the payload (also useful for tests). Never raises.
    """
    payload: dict[str, Any] = {
        "event": "compression_lifecycle",
        "phase": str(phase or "unknown"),
        "attempt_id": str(attempt_id or ""),
        "session_id": str(session_id or ""),
    }
    try:
        mem = collect_process_memory_snapshot()
        payload.update(mem)
        if started_at is not None:
            payload["elapsed_ms"] = int(max(0.0, (time.monotonic() - started_at) * 1000))
        if extra:
            # Shallow copy only known-safe scalar/dict structures. Never accept
            # caller-supplied free-form strings that could embed content.
            for key, value in extra.items():
                if key in payload:
                    continue
                if value is None or isinstance(value, (bool, int, float)):
                    payload[key] = value
                elif isinstance(value, str) and len(value) <= 128:
                    payload[key] = value
                elif isinstance(value, Mapping):
                    # Nested dicts: only keep scalar leaves / short strings.
                    cleaned: dict[str, Any] = {}
                    for nk, nv in value.items():
                        sk = str(nk)
                        if isinstance(nv, (bool, int, float)) or nv is None:
                            cleaned[sk] = nv
                        elif isinstance(nv, str) and len(nv) <= 128:
                            cleaned[sk] = nv
                        elif isinstance(nv, Mapping):
                            nested: dict[str, Any] = {}
                            for nnk, nnv in nv.items():
                                if isinstance(nnv, (bool, int, float)) or nnv is None:
                                    nested[str(nnk)] = nnv
                                elif isinstance(nnv, str) and len(nnv) <= 128:
                                    nested[str(nnk)] = nnv
                            cleaned[sk] = nested
                    payload[str(key)] = cleaned
                elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                    items: list[Any] = []
                    for item in list(value)[:_MAX_TOP_SESSIONS]:
                        if isinstance(item, Mapping):
                            items.append(
                                {
                                    str(ik): iv
                                    for ik, iv in item.items()
                                    if isinstance(iv, (bool, int, float))
                                    or iv is None
                                    or (isinstance(iv, str) and len(iv) <= 128)
                                }
                            )
                        elif isinstance(item, (bool, int, float)) or item is None:
                            items.append(item)
                        elif isinstance(item, str) and len(item) <= 128:
                            items.append(item)
                    payload[str(key)] = items
        if not _allow_phase_log(str(attempt_id or "")):
            payload["suppressed"] = True
            return payload
        line = _clip_log_line(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        )
        (log or logger).info("compression lifecycle telemetry: %s", line)
    except Exception as exc:  # pragma: no cover - fail-open
        try:
            (log or logger).debug(
                "compression lifecycle telemetry failed: %s: %s",
                type(exc).__name__,
                exc,
            )
        except Exception:
            pass
    return payload


def gateway_registry_aggregate(
    sessions: Mapping[str, Mapping[str, Any]] | None,
    *,
    transport_is_dead: Any | None = None,
) -> dict[str, Any]:
    """Content-free aggregate over the in-memory gateway session registry.

    ``transport_is_dead`` is an optional callable(session_transport) -> bool
    used to classify detached vs live transports without importing the
    gateway module (avoids cycles).
    """
    live = 0
    running = 0
    detached = 0
    total_history_msgs = 0
    ranked: list[tuple[int, str]] = []
    if sessions:
        for sid, session in sessions.items():
            if not isinstance(session, Mapping):
                continue
            live += 1
            if session.get("running"):
                running += 1
            transport = session.get("transport")
            is_detached = False
            if transport_is_dead is not None:
                try:
                    is_detached = bool(transport_is_dead(transport))
                except Exception:
                    is_detached = False
            if is_detached:
                detached += 1
            history = session.get("history")
            if isinstance(history, list):
                msg_count = len(history)
                # Prefer measured tracked bytes when history is message dicts;
                # fall back to message count * 0 so we still rank by count.
                try:
                    measured = measure_message_field_bytes(history)
                    size = int(measured.get("total_tracked_bytes") or 0)
                    if size <= 0:
                        size = msg_count
                except Exception:
                    size = msg_count
                    measured = None
                total_history_msgs += msg_count
                ranked.append((size, str(sid)))
            else:
                ranked.append((0, str(sid)))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    top = [
        {"session_id": sid, "history_bytes": size}
        for size, sid in ranked[:_MAX_TOP_SESSIONS]
    ]
    return {
        "live_session_count": live,
        "running_session_count": running,
        "detached_session_count": detached,
        "aggregate_history_messages": total_history_msgs,
        "top_histories": top,
    }


def log_gateway_registry(
    sessions: Mapping[str, Mapping[str, Any]] | None,
    *,
    transport_is_dead: Any | None = None,
    log: logging.Logger | None = None,
) -> dict[str, Any]:
    """Emit one reaper-pass registry aggregate line. Never raises."""
    try:
        agg = gateway_registry_aggregate(
            sessions, transport_is_dead=transport_is_dead
        )
        return log_compression_phase(
            "gateway_reaper_registry",
            attempt_id="gateway-reaper",
            extra=agg,
            log=log,
        )
    except Exception as exc:  # pragma: no cover
        try:
            (log or logger).debug(
                "gateway registry telemetry failed: %s: %s",
                type(exc).__name__,
                exc,
            )
        except Exception:
            pass
        return {}


class CodexStreamTelemetry:
    """Bounded counters for ``_consume_codex_event_stream`` retention.

    Logs every ``_CODEX_EVENT_LOG_EVERY`` events or every
    ``_CODEX_EVENT_LOG_INTERVAL_S`` seconds, whichever comes first, plus a
    final ``stream_close`` emission.
    """

    __slots__ = (
        "attempt_id",
        "session_id",
        "started_at",
        "event_count",
        "output_item_done_count",
        "output_item_done_bytes",
        "text_delta_count",
        "text_delta_bytes",
        "commentary_delta_count",
        "commentary_delta_bytes",
        "reasoning_delta_count",
        "reasoning_delta_bytes",
        "_last_emit_at",
        "_last_emit_events",
        "_closed",
        "_log",
    )

    def __init__(
        self,
        *,
        attempt_id: str = "",
        session_id: str = "",
        log: logging.Logger | None = None,
    ) -> None:
        self.attempt_id = str(attempt_id or "")
        self.session_id = str(session_id or "")
        self.started_at = time.monotonic()
        self.event_count = 0
        self.output_item_done_count = 0
        self.output_item_done_bytes = 0
        self.text_delta_count = 0
        self.text_delta_bytes = 0
        self.commentary_delta_count = 0
        self.commentary_delta_bytes = 0
        self.reasoning_delta_count = 0
        self.reasoning_delta_bytes = 0
        self._last_emit_at = self.started_at
        self._last_emit_events = 0
        self._closed = False
        self._log = log

    def note_event(self) -> None:
        self.event_count += 1
        self._maybe_emit("codex_stream_progress")

    def note_output_item_done(self, item: Any) -> None:
        self.output_item_done_count += 1
        self.output_item_done_bytes += serialized_byte_len(item)

    def note_text_delta(self, text: Any) -> None:
        if not text:
            return
        self.text_delta_count += 1
        self.text_delta_bytes += serialized_byte_len(text)

    def note_commentary_delta(self, text: Any) -> None:
        if not text:
            return
        self.commentary_delta_count += 1
        self.commentary_delta_bytes += serialized_byte_len(text)

    def note_reasoning_delta(self, text: Any) -> None:
        if not text:
            return
        self.reasoning_delta_count += 1
        self.reasoning_delta_bytes += serialized_byte_len(text)

    def _counters(self) -> dict[str, int]:
        return {
            "event_count": self.event_count,
            "output_item_done_count": self.output_item_done_count,
            "output_item_done_bytes": self.output_item_done_bytes,
            "text_delta_count": self.text_delta_count,
            "text_delta_bytes": self.text_delta_bytes,
            "commentary_delta_count": self.commentary_delta_count,
            "commentary_delta_bytes": self.commentary_delta_bytes,
            "reasoning_delta_count": self.reasoning_delta_count,
            "reasoning_delta_bytes": self.reasoning_delta_bytes,
        }

    def _maybe_emit(self, phase: str) -> None:
        if self._closed:
            return
        now = time.monotonic()
        events_since = self.event_count - self._last_emit_events
        due_events = events_since >= _CODEX_EVENT_LOG_EVERY
        due_time = (now - self._last_emit_at) >= _CODEX_EVENT_LOG_INTERVAL_S
        if not (due_events or due_time):
            return
        self._emit(phase)
        self._last_emit_at = now
        self._last_emit_events = self.event_count

    def _emit(self, phase: str) -> None:
        log_compression_phase(
            phase,
            attempt_id=self.attempt_id or "codex-stream",
            session_id=self.session_id,
            started_at=self.started_at,
            extra=self._counters(),
            log=self._log,
        )

    def close(self, phase: str = "codex_stream_close") -> None:
        if self._closed:
            return
        self._closed = True
        self._emit(phase)


def new_attempt_id() -> str:
    """Short unique id for correlating phase lines of one compression."""
    # time_ns + pid keeps it unique without importing uuid (cheaper hot path).
    return f"{time.time_ns():x}-{os.getpid():x}"


__all__ = [
    "CodexStreamTelemetry",
    "collect_process_memory_snapshot",
    "content_free_hash",
    "gateway_registry_aggregate",
    "log_compression_phase",
    "log_gateway_registry",
    "measure_message_field_bytes",
    "new_attempt_id",
    "reset_telemetry_state_for_tests",
    "serialized_byte_len",
]
