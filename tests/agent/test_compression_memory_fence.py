"""Regression matrix for the process-wide compression RSS/resource fence."""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agent.auxiliary_client as auxiliary
import agent.context_compressor as context_compressor_module
import agent.conversation_compression as compression
import agent.codex_runtime as codex_runtime
import hermes_cli.mem_trim as mem_trim
from agent.context_compressor import ContextCompressor
from agent.codex_runtime import CodexStreamLimitError


class _DummyAgent:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.context_compressor = SimpleNamespace(
            _last_compression_telemetry=None,
        )
        self._cached_system_prompt = "cached-system"
        self._last_compression_attempt_recorded = False
        self._last_compression_attempt_in_place = None
        self._compression_skipped_due_to_lock = None
        self._compression_attempt_id = None

    def _build_system_prompt(self, system_message: str) -> str:
        return f"built:{system_message}"


class _ClosableEventStream:
    def __init__(self, events: list[dict]) -> None:
        self._events = events
        self.closed = False

    def __iter__(self):
        return iter(self._events)

    def close(self) -> None:
        self.closed = True


class _FakeResponses:
    def __init__(self, stream: _ClosableEventStream) -> None:
        self.stream = stream
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.stream


class _FakeCodexClient:
    def __init__(self, stream: _ClosableEventStream) -> None:
        self.responses = _FakeResponses(stream)
        self.base_url = "https://chatgpt.com/backend-api/codex"
        self.api_key = "test"
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _cheap_abort_trim(monkeypatch):
    """Keep resource-fence unit tests deterministic and allocator-independent."""
    monkeypatch.setattr(mem_trim, "trim_memory", lambda *a, **kw: True)


def _success_impl(agent, _messages, _system_message, **_kwargs):
    agent._last_compression_attempt_recorded = True
    agent._last_compression_attempt_in_place = False
    return [{"role": "assistant", "content": "compressed"}], "new-system"


def _terminal_event() -> dict:
    return {
        "type": "response.completed",
        "response": {"status": "completed", "usage": {}},
    }


def _compressor() -> ContextCompressor:
    cc = ContextCompressor(
        model="test-model",
        threshold_percent=0.75,
        protect_first_n=5,
        protect_last_n=20,
        quiet_mode=True,
        config_context_length=40960,
        provider="test",
    )
    cc.threshold_tokens = 24_576
    return cc


def _messages(n: int, size: int = 1500) -> list[dict]:
    result = [{"role": "system", "content": "sys"}]
    for index in range(n):
        role = "user" if index % 2 == 0 else "assistant"
        result.append({"role": role, "content": f"m{index} " + "z" * size})
    return result


def test_process_permit_rejects_cross_session_overlap_before_expensive_work(
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocking_impl(agent, _messages, _system_message, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return _success_impl(agent, _messages, _system_message)

    monkeypatch.setattr(compression, "_compress_context_impl", blocking_impl)
    first_agent = _DummyAgent("session-a")
    second_agent = _DummyAgent("session-b")
    first_messages = [{"role": "user", "content": "first"}]
    second_messages = [{"role": "user", "content": "second"}]
    result: dict[str, tuple[list, str]] = {}

    thread = threading.Thread(
        target=lambda: result.setdefault(
            "first",
            compression.compress_context(first_agent, first_messages, "system"),
        )
    )
    thread.start()
    assert entered.wait(timeout=1)

    rejected, prompt = compression.compress_context(
        second_agent, second_messages, "system"
    )
    assert rejected is second_messages
    assert prompt == "cached-system"
    assert calls == 1
    assert (
        second_agent.context_compressor._last_compression_telemetry["failure_class"]
        == "process_compression_busy"
    )

    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result["first"][0] is not first_messages


def test_timed_out_worker_keeps_process_permit_until_it_exits(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    worker_returning = threading.Event()
    calls = 0
    trim_calls: list[dict] = []
    monkeypatch.setattr(
        mem_trim,
        "trim_memory",
        lambda *a, **kwargs: trim_calls.append(kwargs) or True,
    )

    def blocking_impl(
        agent, messages, _system_message, *, commit_fence=None, **_kwargs
    ):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        if commit_fence is not None and not commit_fence.begin_commit():
            agent._last_compression_attempt_in_place = None
            worker_returning.set()
            return messages, "cancelled"
        try:
            return _success_impl(agent, messages, _system_message)
        finally:
            if commit_fence is not None:
                commit_fence.finish_commit()
            worker_returning.set()

    monkeypatch.setattr(compression, "_compress_context_impl", blocking_impl)
    first_agent = _DummyAgent("session-timeout")
    original = [{"role": "user", "content": "original"}]

    returned, _ = compression.run_compress_context_with_progress_timeout(
        worker=lambda fence: compression.compress_context(
            first_agent,
            original,
            "system",
            commit_fence=fence,
        ),
        messages=original,
        system_prompt_fallback="fallback",
        idle_timeout_seconds=0.03,
        total_ceiling_seconds=0.08,
    )
    assert entered.is_set()
    assert returned is original
    # The host cannot reclaim the detached worker's still-live snapshot. The
    # sole meaningful trim must wait for that worker's finally path.
    assert trim_calls == []

    second_agent = _DummyAgent("session-while-detached")
    second_messages = [{"role": "user", "content": "second"}]
    rejected, _ = compression.compress_context(second_agent, second_messages, "system")
    assert rejected is second_messages
    assert calls == 1
    assert (
        second_agent.context_compressor._last_compression_telemetry["failure_class"]
        == "process_compression_busy"
    )

    release.set()
    assert worker_returning.wait(timeout=2)
    for _ in range(100):
        third_agent = _DummyAgent("session-after-release")
        third_messages = [{"role": "user", "content": "third"}]
        third, _ = compression.compress_context(third_agent, third_messages, "system")
        if third is not third_messages:
            break
        time.sleep(0.005)
    else:
        pytest.fail("detached worker did not release the process permit")
    assert calls == 2
    assert trim_calls == [
        {"reason": "compression-attempt-finally", "force": True},
    ]


def test_mutating_timed_out_worker_cannot_touch_live_transcript_or_commit(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    commit_count = 0
    split_count = 0

    def mutating_impl(
        agent, messages, _system_message, *, commit_fence=None, **_kwargs
    ):
        nonlocal commit_count, split_count
        messages[0]["content"] = "worker-mutated"
        messages.append({"role": "assistant", "content": "private"})
        entered.set()
        assert release.wait(timeout=2)
        if commit_fence is not None and not commit_fence.begin_commit():
            agent._last_compression_attempt_in_place = None
            finished.set()
            return messages, "cancelled"
        commit_count += 1
        split_count += 1
        agent._last_compression_attempt_in_place = False
        if commit_fence is not None:
            commit_fence.finish_commit()
        finished.set()
        return messages, "committed"

    monkeypatch.setattr(compression, "_compress_context_impl", mutating_impl)
    agent = _DummyAgent("session-mutating")
    original = [{"role": "user", "content": "live-original"}]

    returned, _ = compression.run_compress_context_with_progress_timeout(
        worker=lambda fence: compression.compress_context(
            agent,
            original,
            "system",
            commit_fence=fence,
        ),
        messages=original,
        system_prompt_fallback="fallback",
        idle_timeout_seconds=0.03,
        total_ceiling_seconds=0.08,
    )
    assert entered.is_set()
    assert returned is original
    assert original == [{"role": "user", "content": "live-original"}]

    release.set()
    assert finished.wait(timeout=2)
    assert original == [{"role": "user", "content": "live-original"}]
    assert commit_count == 0
    assert split_count == 0


def test_preflight_rejects_high_rss_before_deepcopy_or_provider_and_logs_no_content(
    monkeypatch, caplog
):
    secret = "TOP-SECRET-COMPRESSION-CONTENT"
    messages = [{"role": "user", "content": secret}]
    agent = _DummyAgent("session-high-rss")
    provider_calls = 0

    monkeypatch.setattr(
        compression,
        "_compression_memory_limits",
        lambda: {
            "rss_ceiling_bytes": 100,
            "message_graph_limit_bytes": 1024 * 1024,
            "snapshot_limit_bytes": 1024 * 1024,
        },
    )
    monkeypatch.setattr(compression, "_current_process_rss_bytes", lambda: 101)
    monkeypatch.setattr(
        compression.copy,
        "deepcopy",
        lambda *_a, **_kw: pytest.fail("deepcopy reached before preflight"),
    )

    def provider_impl(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        pytest.fail("provider/engine reached before preflight")

    monkeypatch.setattr(compression, "_compress_context_impl", provider_impl)
    with caplog.at_level(logging.INFO):
        returned, _ = compression.compress_context(agent, messages, "system")

    assert returned is messages
    assert provider_calls == 0
    telemetry = agent.context_compressor._last_compression_telemetry
    assert telemetry["failure_class"] == "rss_headroom"
    assert isinstance(telemetry["rss_bytes"], int)
    assert isinstance(telemetry["projected_rss_bytes"], int)
    assert secret not in caplog.text
    assert secret not in repr(telemetry)


def test_bounded_snapshot_detaches_replay_sidecar_structure_and_reports_only_counts():
    opaque_blob = bytearray(b"x" * (2 * 1024 * 1024))
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "safe summary input"}],
            "codex_reasoning_items": opaque_blob,
        }
    ]
    stats: dict[str, int] = {}

    snapshot = compression._build_bounded_compression_snapshot(
        messages,
        max_bytes=4 * 1024 * 1024,
        stats=stats,
    )
    estimate = compression._estimate_compression_message_graph(
        messages,
        stop_after=4 * 1024 * 1024,
    )

    assert snapshot is not messages
    assert snapshot[0] is not messages[0]
    assert snapshot[0]["content"] is not messages[0]["content"]
    # PR #79668 review H4: mutable sidecars are STRUCTURALLY DETACHED, not
    # aliased — a mutating engine cannot reach the caller's objects through
    # the worker snapshot. Payload bytes are still never recursively
    # duplicated: immutable leaves stay shared.
    assert snapshot[0]["codex_reasoning_items"] is not opaque_blob
    assert snapshot[0]["codex_reasoning_items"] == opaque_blob
    snapshot[0]["codex_reasoning_items"][0] = 0x7A
    assert opaque_blob[0] == 0x78
    assert stats["opaque_sidecar_fields"] == 1
    assert estimate["opaque_replay_bytes"] >= len(opaque_blob)
    assert all(isinstance(value, int) for value in stats.values())
    assert "safe summary input" not in repr(stats)


def test_codex_sse_caps_close_stream_and_abort_compression_without_partial_commit(
    monkeypatch,
):
    over_limit_cases = [
        (
            "text_bytes",
            1,
            [
                {
                    "type": "response.output_item.added",
                    "item": {"type": "message", "phase": "final"},
                },
                {"type": "response.output_text.delta", "delta": "123456789"},
            ],
        ),
        (
            "commentary_bytes",
            1,
            [
                {
                    "type": "response.output_item.added",
                    "item": {"type": "message", "phase": "commentary"},
                },
                {"type": "response.output_text.delta", "delta": "123456789"},
            ],
        ),
        (
            "reasoning_bytes",
            1,
            [{"type": "response.reasoning_text.delta", "delta": "r" * 17}],
        ),
        (
            "done_item_bytes",
            1,
            [
                {
                    "type": "response.output_item.done",
                    "item": {"type": "message", "content": "d" * 64},
                }
            ],
        ),
        (
            "output_items",
            12_000,
            [
                {
                    "type": "response.output_item.done",
                    "item": {"type": "function_call", "call_id": str(index)},
                }
                for index in range(257)
            ],
        ),
    ]

    for expected_phase, max_tokens, events in over_limit_cases:
        stream = _ClosableEventStream(events + [_terminal_event()])
        client = _FakeCodexClient(stream)
        adapter = auxiliary._CodexCompletionsAdapter(client, "gpt-test")
        transcript = [{"role": "user", "content": "unchanged"}]
        with pytest.raises(CodexStreamLimitError) as raised:
            adapter.create(messages=transcript, max_tokens=max_tokens)
        assert raised.value.phase == expected_phase
        assert stream.closed is True
        assert transcript == [{"role": "user", "content": "unchanged"}]

    cc = _compressor()
    transcript = _messages(14)
    original = [dict(message) for message in transcript]

    def raise_limit(**_kwargs):
        raise CodexStreamLimitError("text_bytes", limit=8, observed=9)

    monkeypatch.setattr(context_compressor_module, "call_llm", raise_limit)
    result = cc.compress(transcript, current_tokens=100_000)
    # ContextCompressor returns a protected shallow snapshot on this abort
    # path; the caller-owned transcript itself must remain byte-for-byte equal.
    assert result == original
    assert transcript == original
    assert cc._last_compress_aborted is True
    assert cc._last_summary_limit_failure is True
    assert cc._last_compression_made_progress is False


def test_non_codex_compression_request_gets_provider_appropriate_output_cap(
    monkeypatch,
):
    monkeypatch.setattr(
        auxiliary,
        "_get_auxiliary_task_config",
        lambda _task: {"max_output_tokens": 4096},
    )
    assert auxiliary.compression_max_output_tokens() == 4096
    kwargs = auxiliary._build_call_kwargs(
        provider="custom",
        model="gpt-4o",
        messages=[{"role": "user", "content": "summarize"}],
        max_tokens=None,
        base_url="https://example.test/v1",
        task="compression",
    )
    output_cap = kwargs.get("max_tokens", kwargs.get("max_completion_tokens"))
    assert output_cap == 4096
    assert len({"max_tokens", "max_completion_tokens"} & set(kwargs)) == 1


def test_codex_uses_local_retention_cap_without_unsupported_wire_parameter():
    stream = _ClosableEventStream([
        {
            "type": "response.output_item.added",
            "item": {"type": "message", "phase": "final"},
        },
        {"type": "response.output_text.delta", "delta": "ok"},
        _terminal_event(),
    ])
    client = _FakeCodexClient(stream)
    adapter = auxiliary._CodexCompletionsAdapter(client, "gpt-test")

    response = adapter.create(
        messages=[{"role": "user", "content": "summarize"}],
        max_completion_tokens=12_000,
    )

    assert response.choices[0].message.content == "ok"
    assert stream.closed is True
    wire = client.responses.calls[0]
    assert "max_tokens" not in wire
    assert "max_completion_tokens" not in wire
    assert "max_output_tokens" not in wire


def test_failure_cleanup_releases_process_and_aux_permits_closes_stream_and_trims(
    monkeypatch,
):
    trim_calls: list[dict] = []
    monkeypatch.setattr(
        mem_trim,
        "trim_memory",
        lambda *a, **kwargs: trim_calls.append(kwargs) or True,
    )

    # The adapter owns and closes the physical stream even when a local cap
    # raises before a terminal event.
    stream = _ClosableEventStream([
        {
            "type": "response.output_item.added",
            "item": {"type": "message", "phase": "final"},
        },
        {"type": "response.output_text.delta", "delta": "123456789"},
    ])
    codex_client = _FakeCodexClient(stream)
    with pytest.raises(CodexStreamLimitError):
        auxiliary._CodexCompletionsAdapter(codex_client, "gpt-test").create(
            messages=[{"role": "user", "content": "x"}],
            max_tokens=1,
        )
    assert stream.closed is True

    auxiliary._aux_sync_semaphores.clear()
    provider = MagicMock()
    provider.base_url = "https://example.test/v1"
    provider.chat.completions.create.side_effect = RuntimeError("boom")
    monkeypatch.setattr(
        auxiliary,
        "_resolve_task_provider_model",
        lambda *_a, **_kw: ("openrouter", "test-model", None, None, None),
    )
    monkeypatch.setattr(
        auxiliary,
        "_get_cached_client",
        lambda *_a, **_kw: (provider, "test-model"),
    )
    monkeypatch.setattr(
        auxiliary,
        "_get_auxiliary_task_config",
        lambda _task: {"max_concurrency": 1, "max_output_tokens": 12_000},
    )
    monkeypatch.setattr(
        auxiliary,
        "_validate_llm_response",
        lambda response, _task, **_kwargs: response,
    )

    def failing_impl(_agent, _messages, _system_message, **_kwargs):
        auxiliary.call_llm(
            task="compression",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=12_000,
        )
        pytest.fail("provider failure should have propagated")

    monkeypatch.setattr(compression, "_compress_context_impl", failing_impl)
    agent = _DummyAgent("cleanup-failure")
    with pytest.raises(RuntimeError, match="boom"):
        compression.compress_context(
            agent,
            [{"role": "user", "content": "live"}],
            "system",
        )

    semaphore = auxiliary._aux_sync_semaphores["compression"][1]
    assert semaphore.acquire(blocking=False) is True
    semaphore.release()
    assert len(trim_calls) == 1
    assert trim_calls[0]["reason"] == "compression-attempt-finally"

    monkeypatch.setattr(compression, "_compress_context_impl", _success_impl)
    next_messages = [{"role": "user", "content": "next"}]
    next_result, _ = compression.compress_context(
        _DummyAgent("cleanup-next"), next_messages, "system"
    )
    assert next_result is not next_messages
    auxiliary._aux_sync_semaphores.clear()


def test_success_cleanup_keeps_existing_post_compression_trim_exactly_once(monkeypatch):
    trim_calls: list[dict] = []
    monkeypatch.setattr(
        mem_trim,
        "trim_memory",
        lambda *a, **kwargs: trim_calls.append(kwargs) or True,
    )

    def successful_impl(agent, messages, system_message, **_kwargs):
        mem_trim.trim_memory(reason="post-compression")
        return _success_impl(agent, messages, system_message)

    monkeypatch.setattr(compression, "_compress_context_impl", successful_impl)
    original = [{"role": "user", "content": "live"}]
    result, _ = compression.compress_context(
        _DummyAgent("cleanup-success"), original, "system"
    )

    assert result is not original
    assert trim_calls == [{"reason": "post-compression"}]


# ---------------------------------------------------------------------------
# PR #79668 exact-head review blockers — contract tests
# ---------------------------------------------------------------------------


def _provider_rejecting_injected_caps(monkeypatch, error_message: str):
    """Fake provider that rejects any call carrying an output-cap key.

    The caller never passes max_tokens: the compression cap is INJECTED by
    auxiliary_client._build_call_kwargs, so its rejection must be detected
    independent of the caller's original argument (review C1).
    """
    provider = MagicMock()
    provider.base_url = "https://example.test/v1"

    def _reject(**kwargs):
        assert ("max_tokens" in kwargs) or ("max_completion_tokens" in kwargs), (
            "compression call carried no injected output cap"
        )
        raise RuntimeError(error_message)

    provider.chat.completions.create.side_effect = _reject
    monkeypatch.setattr(
        auxiliary,
        "_resolve_task_provider_model",
        lambda *_a, **_kw: ("openrouter", "test-model", None, None, None),
    )
    monkeypatch.setattr(
        auxiliary,
        "_get_cached_client",
        lambda *_a, **_kw: (provider, "test-model"),
    )
    monkeypatch.setattr(
        auxiliary,
        "_get_auxiliary_task_config",
        lambda _task: {"max_output_tokens": 12_000},
    )
    monkeypatch.setattr(
        auxiliary,
        "_validate_llm_response",
        lambda response, _task, **_kwargs: response,
    )
    return provider


def test_sync_injected_cap_rejection_without_caller_max_tokens_aborts_unchanged(
    monkeypatch,
):
    """C1 sync: omitted-caller-max_tokens cap rejection must abort fail-closed."""
    _provider_rejecting_injected_caps(
        monkeypatch, "Unsupported parameter: max_tokens is not supported"
    )

    with pytest.raises(auxiliary.AuxiliaryCompressionLimitError):
        auxiliary.call_llm(
            task="compression",
            messages=[{"role": "user", "content": "summarize"}],
        )

    # End-to-end through the real compressor: the same rejection must abort
    # with the transcript unchanged — never commit a static fallback.
    compressor = _compressor()
    transcript = _messages(15)
    original = [dict(message) for message in transcript]
    result = compressor.compress(transcript, current_tokens=100_000)
    assert result == original
    assert transcript == original
    assert compressor._last_compress_aborted is True
    assert compressor._last_summary_fallback_used is False
    assert compressor._last_summary_limit_failure is True


def test_async_injected_cap_rejection_without_caller_max_tokens_aborts(monkeypatch):
    """C1 async: same fail-closed contract on the async call path."""
    import asyncio
    from unittest.mock import AsyncMock

    provider = MagicMock()
    provider.base_url = "https://example.test/v1"

    async def _reject(**kwargs):
        assert ("max_tokens" in kwargs) or ("max_completion_tokens" in kwargs), (
            "compression call carried no injected output cap"
        )
        raise RuntimeError("Unsupported parameter: max_tokens is not supported")

    provider.chat.completions.create = AsyncMock(side_effect=_reject)
    monkeypatch.setattr(
        auxiliary,
        "_resolve_task_provider_model",
        lambda *_a, **_kw: ("openrouter", "test-model", None, None, None),
    )
    monkeypatch.setattr(
        auxiliary,
        "_get_cached_client",
        lambda *_a, **_kw: (provider, "test-model"),
    )
    monkeypatch.setattr(
        auxiliary,
        "_get_auxiliary_task_config",
        lambda _task: {"max_output_tokens": 12_000},
    )
    monkeypatch.setattr(
        auxiliary,
        "_validate_llm_response",
        lambda response, _task, **_kwargs: response,
    )

    with pytest.raises(auxiliary.AuxiliaryCompressionLimitError):
        asyncio.run(
            auxiliary.async_call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )
        )


def _completed_responses_object(text: str, closed: dict) -> SimpleNamespace:
    completed = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
        usage=None,
    )
    completed.close = lambda: closed.__setitem__("flag", True)
    return completed


def test_codex_completed_object_charged_against_retention_caps_sync():
    """H3 sync: a completed-object reply must respect local retained caps."""
    closed = {"flag": False}
    completed = _completed_responses_object("y" * (1024 * 1024), closed)
    client = _FakeCodexClient(completed)
    adapter = auxiliary._CodexCompletionsAdapter(client, "gpt-test")

    with pytest.raises(CodexStreamLimitError):
        adapter.create(
            messages=[{"role": "user", "content": "summarize"}],
            max_tokens=1,
        )
    assert closed["flag"] is True


def test_codex_completed_object_charged_against_retention_caps_async():
    """H3 async: the async adapter delegates to the sync one — same fence."""
    import asyncio

    closed = {"flag": False}
    completed = _completed_responses_object("y" * (1024 * 1024), closed)
    client = _FakeCodexClient(completed)
    sync_adapter = auxiliary._CodexCompletionsAdapter(client, "gpt-test")
    async_adapter = auxiliary._AsyncCodexCompletionsAdapter(sync_adapter)

    with pytest.raises(CodexStreamLimitError):
        asyncio.run(
            async_adapter.create(
                messages=[{"role": "user", "content": "summarize"}],
                max_tokens=1,
            )
        )
    assert closed["flag"] is True


def test_failed_attempt_cannot_mutate_caller_replay_sidecars(monkeypatch):
    """H4: nested sidecar mutation by a failed private engine stays contained."""

    def mutating_impl(agent, messages, _system_message, **_kwargs):
        # Legacy/plugin-style engine mutating its input's nested sidecar.
        messages[0]["codex_reasoning_items"][0]["encrypted_content"] = "MUTATED"
        agent._last_compression_attempt_in_place = None
        return messages, "cancelled"

    monkeypatch.setattr(compression, "_compress_context_impl", mutating_impl)
    agent = _DummyAgent("session-sidecar")
    original = [
        {
            "role": "assistant",
            "content": "live",
            "codex_reasoning_items": [{"encrypted_content": "orig"}],
        }
    ]

    returned, _ = compression.compress_context(agent, original, "system")

    assert returned is original
    assert original[0]["codex_reasoning_items"][0]["encrypted_content"] == "orig"


def test_in_place_commit_boundary_prompt_refresh_failure_returns_compacted(tmp_path):
    """C2: archive_and_compact() is the in-place commit boundary.

    A failed update_system_prompt AFTER the archive commits must not route
    the attempt down the abort path that returns the caller's original
    transcript while the durable active set is already compacted
    (durable/live split-brain). Real SessionDB, fault-injected prompt write.
    """
    import os
    from unittest.mock import patch

    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        parent = "PARENT_INPLACE_PROMPT_FAIL"
        db.create_session(parent, source="cli")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent

            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                platform="cli",
                quiet_mode=True,
                session_db=db,
                session_id=parent,
                skip_context_files=True,
                skip_memory=True,
            )

        compacted = [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail"},
        ]
        compressor = MagicMock()
        compressor.compress.return_value = compacted
        compressor.compression_count = 1
        compressor.last_prompt_tokens = 0
        compressor.last_completion_tokens = 0
        compressor._last_summary_error = None
        compressor._last_compress_aborted = False
        compressor._last_summary_auth_failure = False
        compressor._last_aux_model_failure_model = None
        compressor._last_aux_model_failure_error = None
        agent.context_compressor = compressor
        agent.compression_in_place = True

        original = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        with patch.object(
            db,
            "update_system_prompt",
            side_effect=RuntimeError("prompt write failed"),
        ):
            returned, _prompt = agent._compress_context(
                original, "sys", approx_tokens=120_000
            )

        # The commit boundary already passed: the caller receives the
        # COMPACTED transcript — never the pre-commit original — matching
        # durable state.
        assert returned is not original
        assert [m["content"] for m in returned] == [
            m["content"] for m in compacted
        ]
        assert agent.session_id == parent
        assert agent._last_compression_attempt_in_place is True
        live = db.get_messages_as_conversation(parent)
        assert [m["content"] for m in live] == [m["content"] for m in compacted]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PR #53 exact-head review blockers — adversarial contract tests
# ---------------------------------------------------------------------------


def _fallback_chain(monkeypatch, fb_client, fb_model="fb-model", fb_label="openrouter"):
    """Route call_llm's primary failure onto one recording fallback candidate."""
    monkeypatch.setattr(
        auxiliary,
        "_try_configured_fallback_chain",
        lambda *_a, **_kw: (fb_client, fb_model, fb_label),
    )
    monkeypatch.setattr(
        auxiliary, "_try_main_fallback_chain", lambda *_a, **_kw: (None, None, "")
    )
    payment_fallback_calls = []
    monkeypatch.setattr(
        auxiliary,
        "_try_payment_fallback",
        lambda *_a, **_kw: payment_fallback_calls.append(1) or (None, None, ""),
    )
    return payment_fallback_calls


def _primary_failing_provider():
    primary = MagicMock()
    primary.base_url = "https://primary.test/v1"
    primary.chat.completions.create.side_effect = RuntimeError(
        "402 payment required: insufficient credits"
    )
    return primary


def _patch_aux_resolution(monkeypatch, primary):
    monkeypatch.setattr(
        auxiliary,
        "_resolve_task_provider_model",
        lambda *_a, **_kw: ("openrouter", "test-model", None, None, None),
    )
    monkeypatch.setattr(
        auxiliary,
        "_get_cached_client",
        lambda provider, *_a, **_kw: (primary, "test-model"),
    )
    monkeypatch.setattr(
        auxiliary,
        "_get_auxiliary_task_config",
        lambda _task: {"max_output_tokens": 12_000},
    )
    monkeypatch.setattr(
        auxiliary,
        "_validate_llm_response",
        lambda response, _task, **_kwargs: response,
    )


def test_sync_fallback_carries_clamped_compression_cap(monkeypatch):
    """PR53 P0: primary fails over — the fallback request must carry the cap.

    End-to-end sync path: the caller omits max_tokens, the fallback receives
    the centrally clamped compression cap, and its (oversized-looking)
    response is served through the normal validated route.
    """
    primary = _primary_failing_provider()
    _patch_aux_resolution(monkeypatch, primary)

    fb_client = MagicMock()
    fb_client.base_url = "https://fallback.test/v1"
    fb_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="y" * 1_000_000))]
    )
    fb_client.chat.completions.create.return_value = fb_response
    _fallback_chain(monkeypatch, fb_client)

    result = auxiliary.call_llm(
        task="compression",
        messages=[{"role": "user", "content": "summarize"}],
    )

    assert result is fb_response
    fb_kwargs = fb_client.chat.completions.create.call_args.kwargs
    assert ("max_tokens" in fb_kwargs) or ("max_completion_tokens" in fb_kwargs), (
        "compression fallback request carried no injected output cap"
    )
    cap_value = fb_kwargs.get("max_tokens", fb_kwargs.get("max_completion_tokens"))
    assert cap_value == 12_000  # centrally clamped configured cap


def test_sync_fallback_caller_max_tokens_is_clamped_on_fallback(monkeypatch):
    """The fallback must clamp an explicit caller max_tokens to the cap too."""
    primary = _primary_failing_provider()
    _patch_aux_resolution(monkeypatch, primary)

    fb_client = MagicMock()
    fb_client.base_url = "https://fallback.test/v1"
    fb_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
    )
    _fallback_chain(monkeypatch, fb_client)

    auxiliary.call_llm(
        task="compression",
        messages=[{"role": "user", "content": "summarize"}],
        max_tokens=999_999,
    )

    fb_kwargs = fb_client.chat.completions.create.call_args.kwargs
    cap_value = fb_kwargs.get("max_tokens", fb_kwargs.get("max_completion_tokens"))
    assert cap_value == 12_000


def test_sync_fallback_cap_rejection_fails_closed(monkeypatch):
    """PR53 P0: a fallback rejecting the injected cap aborts compression."""
    primary = _primary_failing_provider()
    _patch_aux_resolution(monkeypatch, primary)

    fb_client = MagicMock()
    fb_client.base_url = "https://fallback.test/v1"
    fb_client.chat.completions.create.side_effect = RuntimeError(
        "Unsupported parameter: max_tokens is not supported"
    )
    payment_fallback_calls = _fallback_chain(monkeypatch, fb_client)

    with pytest.raises(auxiliary.AuxiliaryCompressionLimitError):
        auxiliary.call_llm(
            task="compression",
            messages=[{"role": "user", "content": "summarize"}],
        )
    # Fail-closed: the chain was NOT walked further after the refusal.
    assert payment_fallback_calls == []


def test_async_fallback_carries_cap_and_cap_rejection_fails_closed(monkeypatch):
    """PR53 P0 async mirror: cap on the wire + fail-closed on refusal."""
    import asyncio
    from unittest.mock import AsyncMock

    primary = _primary_failing_provider()
    _patch_aux_resolution(monkeypatch, primary)
    # async_call_llm() accepts the sync fallback-chain result and converts it
    # before dispatch. Keep these recording clients intact so the test probes
    # the async helper's kwargs/exception contract instead of constructing a
    # real AsyncOpenAI client from MagicMock credentials.
    monkeypatch.setattr(
        auxiliary,
        "_to_async_client",
        lambda client, model, **_kwargs: (client, model),
    )

    # Case 1: fallback answers — request must carry the clamped cap.
    fb_client = MagicMock()
    fb_client.base_url = "https://fallback.test/v1"
    fb_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="y" * 1_000_000))]
    )
    fb_client.chat.completions.create = AsyncMock(return_value=fb_response)
    _fallback_chain(monkeypatch, fb_client)

    result = asyncio.run(
        auxiliary.async_call_llm(
            task="compression",
            messages=[{"role": "user", "content": "summarize"}],
        )
    )
    assert result is fb_response
    fb_kwargs = fb_client.chat.completions.create.call_args.kwargs
    cap_value = fb_kwargs.get("max_tokens", fb_kwargs.get("max_completion_tokens"))
    assert cap_value == 12_000

    # Case 2: fallback rejects the cap — must fail closed, not continue.
    fb_rejecting = MagicMock()
    fb_rejecting.base_url = "https://fallback.test/v1"
    fb_rejecting.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("Unsupported parameter: max_tokens is not supported")
    )
    payment_fallback_calls = _fallback_chain(monkeypatch, fb_rejecting)

    with pytest.raises(auxiliary.AuxiliaryCompressionLimitError):
        asyncio.run(
            auxiliary.async_call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )
        )
    assert payment_fallback_calls == []


def test_fallback_auth_refresh_retry_carries_cap_and_fails_closed(monkeypatch):
    """PR53 P0: the auth-refresh retry inside a fallback is fenced too.

    First fallback attempt 401s; the refreshed-credential retry must still
    carry the clamped compression cap and must fail closed when the retry
    rejects it.
    """
    primary = _primary_failing_provider()

    retry_client = MagicMock()
    retry_client.base_url = "https://refresh.test/v1"
    retry_client.chat.completions.create.side_effect = RuntimeError(
        "Unsupported parameter: max_completion_tokens is not supported"
    )

    def _client_for(provider, *_a, **_kw):
        if provider == "anthropic":
            return retry_client, "retry-model"
        return primary, "test-model"

    monkeypatch.setattr(
        auxiliary,
        "_resolve_task_provider_model",
        lambda *_a, **_kw: ("openrouter", "test-model", None, None, None),
    )
    monkeypatch.setattr(auxiliary, "_get_cached_client", _client_for)
    monkeypatch.setattr(
        auxiliary,
        "_get_auxiliary_task_config",
        lambda _task: {"max_output_tokens": 12_000},
    )
    monkeypatch.setattr(
        auxiliary,
        "_validate_llm_response",
        lambda response, _task, **_kwargs: response,
    )

    fb_client = MagicMock()
    fb_client.base_url = "https://fallback.test/v1"
    fb_client.chat.completions.create.side_effect = RuntimeError("Error code: 401")
    _fallback_chain(monkeypatch, fb_client, fb_label="anthropic")
    monkeypatch.setattr(
        auxiliary, "_auth_refresh_provider_for_route", lambda *_a, **_kw: "anthropic"
    )
    monkeypatch.setattr(
        auxiliary, "_refresh_provider_credentials", lambda *_a, **_kw: True
    )

    with pytest.raises(auxiliary.AuxiliaryCompressionLimitError):
        auxiliary.call_llm(
            task="compression",
            messages=[{"role": "user", "content": "summarize"}],
        )

    retry_kwargs = retry_client.chat.completions.create.call_args.kwargs
    cap_value = retry_kwargs.get(
        "max_tokens", retry_kwargs.get("max_completion_tokens")
    )
    assert cap_value == 12_000


def _bushy_container_graph(fanout: int = 64) -> list:
    """Broad+shallow container graph: 1 + fanout + fanout² containers."""
    return [[[] for _ in range(fanout)] for _ in range(fanout)]


def test_snapshot_container_allocations_are_charged_against_cap():
    """PR53 P0: container allocations count against the snapshot byte cap."""
    messages = [
        {"role": "user", "content": "x", "graph": _bushy_container_graph()}
    ]
    # 4,161 empty lists ≈ 266 KB of pure container allocation: a 512-byte cap
    # must reject, where payload-only accounting admitted it wholesale.
    with pytest.raises(compression.CompressionSnapshotTooLargeError):
        compression._build_bounded_compression_snapshot(messages, max_bytes=512)

    stats: dict[str, int] = {}
    snapshot = compression._build_bounded_compression_snapshot(
        messages, max_bytes=64 * 1024 * 1024, stats=stats
    )
    assert snapshot is not messages
    container_count = 1 + 64 + 64 * 64
    # Every allocated container is charged with the conservative bound...
    assert stats["snapshot_retained_bytes"] >= (
        compression._SNAPSHOT_LIST_CONTAINER_BYTES * container_count
    )
    # ...and counted as a node.
    assert stats["snapshot_nodes"] >= container_count
    # Measured upper bound: the charged estimate stays conservative against
    # the real allocation size of the cloned graph.
    import sys as _sys

    def _actual_bytes(value, seen=None):
        seen = seen or set()
        if id(value) in seen:
            return 0
        seen.add(id(value))
        total = _sys.getsizeof(value)
        if isinstance(value, (list, tuple, set, frozenset)):
            total += sum(_actual_bytes(item, seen) for item in value)
        elif isinstance(value, dict):
            total += sum(
                _actual_bytes(k, seen) + _actual_bytes(v, seen)
                for k, v in value.items()
            )
        return total

    assert stats["snapshot_retained_bytes"] >= _actual_bytes(snapshot)


def test_preflight_estimator_charges_containers_and_keeps_sidecar_mass():
    """PR53 P0: preflight projection matches the clone it admits."""
    messages = [
        {
            "role": "assistant",
            "content": "live",
            "graph": _bushy_container_graph(),
            "codex_reasoning_items": [{"encrypted_content": "z" * 2048}],
        }
    ]
    estimate = compression._estimate_compression_message_graph(
        messages, stop_after=64 * 1024 * 1024
    )
    container_count = 1 + 64 + 64 * 64
    assert estimate["message_graph_bytes"] >= (
        compression._SNAPSHOT_LIST_CONTAINER_BYTES * container_count
    )
    # Sidecar mass is counted for admission but no longer SUBTRACTED from the
    # projected clone charge: mutable sidecar containers are structurally
    # cloned into the worker snapshot (and again into the rollback snapshot).
    assert estimate["opaque_replay_bytes"] >= 2048
    assert estimate["snapshot_clone_bytes"] == estimate["message_graph_bytes"]


def test_compress_context_rejects_oversized_container_graph_preflight(monkeypatch):
    """The whole boundary rejects a container-heavy transcript before work."""
    monkeypatch.setattr(
        compression,
        "_compression_memory_limits",
        lambda: {
            "rss_ceiling_bytes": 64 * 1024 * 1024 * 1024,
            "message_graph_limit_bytes": 64 * 1024 * 1024,
            "snapshot_limit_bytes": 4096,
        },
    )
    impl_calls = []
    monkeypatch.setattr(
        compression,
        "_compress_context_impl",
        lambda *a, **kw: impl_calls.append(1) or _success_impl(*a, **kw),
    )
    agent = _DummyAgent("session-bushy")
    original = [{"role": "user", "content": "x", "graph": _bushy_container_graph()}]

    returned, _ = compression.compress_context(agent, original, "system")

    assert returned is original
    assert impl_calls == []
    telemetry = agent.context_compressor._last_compression_telemetry
    assert telemetry["failure_class"] == "snapshot_too_large"


def test_bounded_snapshot_detaches_sets_in_messages_and_sidecars():
    """PR53 P1: set/frozenset containers are detached, not aliased."""
    caller_flags = {"alpha"}
    caller_tags = {"original"}
    messages = [
        {
            "role": "assistant",
            "content": "live",
            "plugin_flags": caller_flags,
            "codex_reasoning_items": {"tags": caller_tags},
        }
    ]
    stats: dict[str, int] = {}
    snapshot = compression._build_bounded_compression_snapshot(
        messages, max_bytes=4 * 1024 * 1024, stats=stats
    )

    snap_flags = snapshot[0]["plugin_flags"]
    snap_tags = snapshot[0]["codex_reasoning_items"]["tags"]
    assert snap_flags == {"alpha"} and snap_flags is not caller_flags
    assert snap_tags == {"original"} and snap_tags is not caller_tags
    snap_flags.add("mutated")
    snap_tags.add("mutated")
    assert caller_flags == {"alpha"}
    assert caller_tags == {"original"}
    assert all(isinstance(value, int) for value in stats.values())


def test_failed_attempt_cannot_mutate_caller_set_sidecars(monkeypatch):
    """PR53 P1: a mutating engine cannot reach caller sets through aliases."""
    def mutating_impl(agent, messages, _system_message, **_kwargs):
        messages[0]["codex_reasoning_items"]["tags"].add("MUTATED")
        messages[0]["plugin_flags"].add("MUTATED")
        agent._last_compression_attempt_in_place = None
        return messages, "cancelled"

    monkeypatch.setattr(compression, "_compress_context_impl", mutating_impl)
    agent = _DummyAgent("session-set-sidecar")
    original = [
        {
            "role": "assistant",
            "content": "live",
            "plugin_flags": {"original"},
            "codex_reasoning_items": {"tags": {"original"}},
        }
    ]

    returned, _ = compression.compress_context(agent, original, "system")

    assert returned is original
    assert original[0]["plugin_flags"] == {"original"}
    assert original[0]["codex_reasoning_items"]["tags"] == {"original"}


class _OpaqueSDKObject:
    """Stand-in for an unknown mutable SDK/plugin object the cloner cannot detach."""

    def __init__(self, payload: str = "opaque") -> None:
        self.payload = payload


def test_mutable_extension_engine_fails_closed_on_opaque_snapshot_objects(
    monkeypatch,
):
    """PR53 P1: opaque mutable aliases + mutable engine → reject before invoke."""
    impl_calls = []
    monkeypatch.setattr(
        compression,
        "_compress_context_impl",
        lambda *a, **kw: impl_calls.append(1) or _success_impl(*a, **kw),
    )
    agent = _DummyAgent("session-opaque")
    # _DummyAgent's SimpleNamespace compressor is NOT the built-in — a
    # mutable extension point by the fail-closed predicate.
    private_payload = "SENSITIVE_OPAQUE_SIDECAR_PAYLOAD"
    original = [
        {
            "role": "assistant",
            "content": "live",
            "sdk_obj": _OpaqueSDKObject(private_payload),
        }
    ]

    returned, _ = compression.compress_context(agent, original, "system")

    assert returned is original
    assert impl_calls == []  # engine never invoked
    telemetry = agent.context_compressor._last_compression_telemetry
    assert telemetry["failure_class"] == "snapshot_opaque_mutable"
    assert private_payload not in repr(telemetry)


def test_engine_mutability_gate_pins_builtin_vs_extension():
    """The fail-closed predicate: exact built-in type only is non-mutating."""
    assert compression._engine_is_mutable_extension_point(_compressor()) is False
    assert compression._engine_is_mutable_extension_point(SimpleNamespace()) is True

    class _CustomCompressor(ContextCompressor):
        pass

    assert (
        compression._engine_is_mutable_extension_point(
            object.__new__(_CustomCompressor)
        )
        is True
    )
    assert compression._engine_is_mutable_extension_point(None) is True


class _SlottedDoneItem:
    """Compatibility-host done item carrying a large payload in __slots__."""

    __slots__ = ("type", "arguments")

    def __init__(self, payload: str) -> None:
        self.type = "function_call"
        self.arguments = payload


def test_codex_completed_slotted_object_charged_against_done_item_cap():
    """PR53 P1: a slotted completed object can no longer bypass the cap."""
    closed = {"flag": False}
    completed = SimpleNamespace(
        output=[_SlottedDoneItem("y" * (1024 * 1024))],
        usage=None,
    )
    completed.close = lambda: closed.__setitem__("flag", True)
    client = _FakeCodexClient(completed)
    adapter = auxiliary._CodexCompletionsAdapter(client, "gpt-test")

    with pytest.raises(CodexStreamLimitError):
        adapter.create(
            messages=[{"role": "user", "content": "summarize"}],
            max_tokens=1,
        )
    assert closed["flag"] is True


def test_codex_completed_unknown_opaque_object_rejected_fail_closed():
    """PR53 P1: unknown non-scalar objects are rejected, not charged 64B."""
    completed = SimpleNamespace(
        output=[{"type": "function_call", "opaque": object()}],
        usage=None,
    )
    with pytest.raises(CodexStreamLimitError) as exc_info:
        codex_runtime._enforce_codex_completed_object_limits(
            completed,
            retention_limits={"max_done_item_bytes": 4096},
        )
    assert exc_info.value.phase == "done_item_bytes"


def test_codex_walk_bounded_charges_slotted_graph():
    """The shared walker charges slotted payloads near their real size."""
    charged = codex_runtime._bounded_codex_value_bytes(
        _SlottedDoneItem("y" * (1024 * 1024)),
        stop_after=8 * 1024 * 1024,
    )
    assert charged >= 1024 * 1024


def test_codex_completed_output_item_cap_checks_without_materializing():
    """PR53 P1: output_items enforcement consumes at most max+1 entries."""
    consumed = {"count": 0}

    def _output_stream():
        for _ in range(100):
            consumed["count"] += 1
            yield {"type": "message", "content": []}

    completed = SimpleNamespace(output=_output_stream(), usage=None)
    with pytest.raises(CodexStreamLimitError) as exc_info:
        codex_runtime._enforce_codex_completed_object_limits(
            completed,
            retention_limits={"max_output_items": 12},
        )
    assert exc_info.value.phase == "output_items"
    assert exc_info.value.observed == 13
    # The full 100-entry container was never materialized/consumed.
    assert consumed["count"] == 13


def _rotation_agent_and_compressor(db, parent, monkeypatch):
    """Real SessionDB + real AIAgent wired for rotation-mode compression."""
    import os
    from unittest.mock import patch

    db.create_session(parent, source="cli")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            platform="cli",
            quiet_mode=True,
            session_db=db,
            session_id=parent,
            skip_context_files=True,
            skip_memory=True,
        )

    compacted = [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
        {"role": "user", "content": "tail"},
    ]
    compressor = MagicMock()
    compressor.compress.return_value = compacted
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_summary_auth_failure = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    agent.compression_in_place = False  # rotation mode

    created_children: list[str] = []
    real_create = db.create_session

    def _tracking_create(session_id, source, **kwargs):
        if session_id != parent:
            created_children.append(session_id)
        return real_create(session_id, source, **kwargs)

    monkeypatch.setattr(db, "create_session", _tracking_create)

    goal_migrations: list[tuple] = []
    monkeypatch.setattr(
        "hermes_cli.goals.migrate_goal_to_session",
        lambda src, dst, reason=None: goal_migrations.append((src, dst, reason)),
    )
    return agent, compacted, created_children, goal_migrations


def _assert_rotation_fully_compensated(
    db, agent, parent, original, returned, created_children, goal_migrations
):
    """The post-child publication failure must leave NO durable split-brain."""
    # Caller-visible abort: the original transcript content is returned.
    assert [m["content"] for m in returned] == [m["content"] for m in original]
    # The agent is restored onto the still-indexed parent session.
    assert agent.session_id == parent
    # Read-back: the parent is live (not ended) with its original transcript.
    parent_row = db.get_session(parent)
    assert parent_row is not None
    assert parent_row.get("ended_at") is None
    live = db.get_messages_as_conversation(parent)
    assert [m["content"] for m in live] == [m["content"] for m in original]
    # The failed empty child was retired — no orphan row remains.
    assert len(created_children) == 1
    assert db.get_session(created_children[0]) is None
    # The goal migration was reversed onto the parent.
    assert (created_children[0], parent, "compression_rollback") in goal_migrations
    # The boundary never committed: no compaction marker, no engine boundary
    # notification.
    assert agent._last_compression_attempt_in_place in (None, False)
    agent.context_compressor.on_session_start.assert_not_called()


def test_rotation_child_prompt_write_failure_compensates_parent_and_child(
    tmp_path, monkeypatch
):
    """PR53 P0: child update_system_prompt failure fully rolls the rotation back."""
    from unittest.mock import patch

    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        parent = "PARENT_ROTATION_PROMPT_FAIL"
        agent, _compacted, created_children, goal_migrations = (
            _rotation_agent_and_compressor(db, parent, monkeypatch)
        )
        original = [{"role": "user", "content": f"m{i}"} for i in range(20)]

        with patch.object(
            db,
            "update_system_prompt",
            side_effect=RuntimeError("child prompt write failed"),
        ):
            returned, _prompt = agent._compress_context(
                original, "sys", approx_tokens=120_000
            )

        _assert_rotation_fully_compensated(
            db, agent, parent, original, returned, created_children, goal_migrations
        )
    finally:
        db.close()


def test_rotation_child_replace_messages_failure_compensates_parent_and_child(
    tmp_path, monkeypatch
):
    """PR53 P0: child replace_messages failure fully rolls the rotation back."""
    from unittest.mock import patch

    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        parent = "PARENT_ROTATION_REPLACE_FAIL"
        agent, _compacted, created_children, goal_migrations = (
            _rotation_agent_and_compressor(db, parent, monkeypatch)
        )
        original = [{"role": "user", "content": f"m{i}"} for i in range(20)]

        with patch.object(
            db,
            "replace_messages",
            side_effect=RuntimeError("child handoff write failed"),
        ):
            returned, _prompt = agent._compress_context(
                original, "sys", approx_tokens=120_000
            )

        _assert_rotation_fully_compensated(
            db, agent, parent, original, returned, created_children, goal_migrations
        )
    finally:
        db.close()
