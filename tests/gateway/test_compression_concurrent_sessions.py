"""Behavioral tests for concurrent compression across distinct and shared sessions.

Complements ``test_compression_concurrent_fork.py`` (which tests the
agent-level lock against a real ``SessionDB``) by focusing on gateway-level
isolation guarantees:

1. Five distinct sessions compressing in parallel must not alias each other's
   session_ids (no cross-session contamination). Under the process-wide
   compression permit (the RSS memory fence) exactly one attempt is admitted;
   the rest fail closed with their original transcripts and keep their parent
   session_ids. The aliasing invariant is what this test pins.
2. Two agents sharing the same session_id must serialize: exactly one rotates,
   the other returns its input unchanged (the permit busy / lock-loser
   contract).

The stub-compressor pattern mirrors ``test_compression_concurrent_fork.py``:
the compressor returns deterministic output and sleeps briefly so threads
actually overlap at the OS level, making the absence of aliasing a genuine
stress test rather than a timing accident.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agent.conversation_compression as conversation_compression
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_agent_with_db(db: SessionDB, session_id: str):
    """Construct an AIAgent wired to *db* and pinned to *session_id*.

    Mirrors the helper in test_compression_concurrent_fork.py exactly so the
    two test modules can be read side-by-side without cognitive overhead.
    """
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )

    # Stub the compressor: deterministic output, brief sleep to force thread overlap.
    compressor = MagicMock()

    def _compress_with_overlap(*_a, **_kw):
        time.sleep(0.25)  # match fork test sleep so threads reliably overlap
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail"},
        ]

    compressor.compress.side_effect = _compress_with_overlap
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    # ROTATION fallback path — pin in_place=False so these keep covering the
    # concurrent-rotation lock contract regardless of the global default
    # (flipped to True in #38763).
    agent.compression_in_place = False
    return agent


class _BarrieredPermit:
    """Test facade over the real process compression permit.

    The permit is a C-level ``threading.BoundedSemaphore`` whose ``acquire``
    cannot be wrapped per-instance, so the module global is swapped for this
    facade during a test. Every caller rendezvous at a barrier *before* the
    real non-blocking acquire, which forces genuine simultaneous contention —
    exactly the condition these tests mean to assert, with zero timing
    dependency (a two-party barrier in front of an atomic acquire decides a
    single winner even under CI CPU starvation).
    """

    def __init__(self, real: threading.BoundedSemaphore, parties: int) -> None:
        self._real = real
        self._barrier = threading.Barrier(parties, timeout=15)

    def acquire(self, blocking: bool = False) -> bool:  # noqa: FBT001,FBT002 - mirrors semaphore API
        try:
            self._barrier.wait()
        except threading.BrokenBarrierError:
            # A test-side timeout must never masquerade as permit-logic
            # failure: fall through to the real (atomic) acquire.
            pass
        return self._real.acquire(blocking=blocking)

    def release(self) -> None:
        self._real.release()


_MESSAGES = [{"role": "user", "content": f"m{i}"} for i in range(20)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_concurrent_compressions_do_not_alias_sessions(tmp_path: Path) -> None:
    """Five distinct sessions compressing in parallel stay isolated.

    The process-wide compression permit (RSS memory fence) admits exactly one
    attempt; the other four fail closed with their original transcripts and
    keep their parent session_ids. The invariant this test pins is isolation:
    the admitted attempt rotates to a fresh unique id, no two agents ever
    share a post-compression id, and no rejected attempt mutates its session.
    """
    db = SessionDB(db_path=tmp_path / "state.db")

    n = 5
    parent_ids = [f"DISTINCT_PARENT_{i:02d}" for i in range(n)]
    for sid in parent_ids:
        db.create_session(sid, source="discord")

    agents = [_build_agent_with_db(db, sid) for sid in parent_ids]
    errors: list[Exception] = []

    def run(agent):
        try:
            agent._compress_context(_MESSAGES, "sys", approx_tokens=120_000)
        except Exception as exc:
            errors.append(exc)

    real_permit = conversation_compression._process_compression_permit
    conversation_compression._process_compression_permit = _BarrieredPermit(
        real_permit, parties=n
    )
    try:
        threads = [threading.Thread(target=run, args=(a,), name=f"session-{i}") for i, a in enumerate(agents)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
    finally:
        conversation_compression._process_compression_permit = real_permit

    assert not errors, f"Compression raised exceptions: {errors}"

    # Exactly one agent wins the permit and rotates to a fresh session_id;
    # the rest are busy-rejected and must keep their parent id untouched.
    new_ids = [a.session_id for a in agents]
    rotated = [sid for sid in new_ids if sid not in parent_ids]
    kept = [sid for sid in new_ids if sid in parent_ids]
    assert len(rotated) == 1, (
        f"Expected exactly one permit winner to rotate, got {len(rotated)}. "
        f"parent_ids={parent_ids}  new_ids={new_ids}"
    )
    assert len(kept) == n - 1, (
        f"Busy-rejected agents must keep their parent session_id: {new_ids}"
    )
    assert len(set(new_ids)) == n, (
        f"Post-compression session_ids are not unique: {new_ids}. "
        "Two agents aliased to the same id — cross-session contamination."
    )


def test_concurrent_compressions_same_session_serialize(tmp_path: Path) -> None:
    """Two agents sharing a session_id must not both rotate it.

    The process-wide compression permit (RSS memory fence) is the first
    serialization gate: one attempt is admitted and the other is busy-rejected
    before any expensive work, returning its messages unchanged. (The
    per-session DB lock added in #34351 remains as the second gate for the
    admitted attempt.) Exactly one agent must rotate; the loser detects
    ``len(returned) == len(input)`` and backs off.

    This is the gateway analogue of the fork test in
    ``test_compression_concurrent_fork.py`` but scoped to the two-agent /
    same-session shape most likely to occur in practice: the main-turn agent
    and its background-review fork both hitting the compression threshold.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    shared_sid = "SHARED_SESSION_CONCURRENT"
    db.create_session(shared_sid, source="discord")

    agent_a = _build_agent_with_db(db, shared_sid)
    agent_b = _build_agent_with_db(db, shared_sid)

    results: dict[str, list | None] = {"a": None, "b": None}
    errors: list[Exception] = []

    def run(key, agent):
        try:
            compressed, _sp = agent._compress_context(_MESSAGES, "sys", approx_tokens=120_000)
            results[key] = compressed
        except Exception as exc:
            errors.append(exc)

    # Force genuine simultaneous contention at the permit gate instead of
    # relying on the compressor stub's ``time.sleep`` to overlap threads:
    # both callers rendezvous, then the real atomic acquire picks the winner.
    real_permit = conversation_compression._process_compression_permit
    conversation_compression._process_compression_permit = _BarrieredPermit(
        real_permit, parties=2
    )
    try:
        t_a = threading.Thread(target=run, args=("a", agent_a), name="main_turn")
        t_b = threading.Thread(target=run, args=("b", agent_b), name="review_fork")
        t_a.start()
        t_b.start()
        t_a.join(timeout=15)
        t_b.join(timeout=15)
    finally:
        conversation_compression._process_compression_permit = real_permit

    assert not errors, f"Compression raised exceptions: {errors}"

    # Count which agents actually compressed (returned fewer messages than input)
    compressed_count = sum(
        1 for msgs in results.values()
        if msgs is not None and len(msgs) < len(_MESSAGES)
    )
    unchanged_count = sum(
        1 for msgs in results.values()
        if msgs is not None and len(msgs) == len(_MESSAGES)
    )

    assert compressed_count == 1, (
        f"Expected exactly one agent to compress, got {compressed_count}. "
        "If both compressed, the permit failed to serialize. "
        "If neither compressed, both were rejected (check permit logic)."
    )
    assert unchanged_count == 1, (
        f"Expected exactly one agent to return messages unchanged (permit "
        f"busy-rejection), got {unchanged_count}."
    )

    # Exactly one session_id rotation must have occurred.
    rotated = sum(
        1 for a in (agent_a, agent_b) if a.session_id != shared_sid
    )
    assert rotated == 1, (
        f"Expected exactly one agent to rotate session_id, got {rotated}. "
        "Both agents rotating produces a session fork (Damien's incident shape)."
    )

    # The session lock must be released so future compression on the NEW
    # session_id works.
    assert db.get_compression_lock_holder(shared_sid) is None, (
        "Compression lock leaked: still held on the parent session_id after both "
        "threads joined. Future compression on the child session would deadlock."
    )
