"""Deterministic tests for content-free compression lifecycle telemetry."""

from __future__ import annotations

import json
import logging
import re

import pytest

import agent.compression_lifecycle_telemetry as tel


@pytest.fixture(autouse=True)
def _reset_state():
    tel.reset_telemetry_state_for_tests()
    yield
    tel.reset_telemetry_state_for_tests()


def test_serialized_byte_len_and_hash_are_stable():
    payload = {"a": 1, "b": ["x", "y"]}
    assert tel.serialized_byte_len(payload) == tel.serialized_byte_len(
        {"b": ["x", "y"], "a": 1}
    )
    h1 = tel.content_free_hash(payload)
    h2 = tel.content_free_hash({"b": ["x", "y"], "a": 1})
    assert h1 == h2
    assert h1 is not None
    assert len(h1) == 12
    assert tel.content_free_hash("") is None
    assert tel.content_free_hash(None) is None


def test_measure_message_field_bytes_counts_only_tracked_fields():
    messages = [
        {
            "role": "user",
            "content": "hello world",
            "api_content": [{"type": "input_text", "text": "hello world"}],
            "secret_should_be_ignored": "PASSWORD=hunter2",
        },
        {
            "role": "assistant",
            "content": "ok",
            "codex_reasoning_items": [{"encrypted_content": "AAAA" * 20}],
            "tool_calls": [{"id": "1", "function": {"name": "x", "arguments": "{}"}}],
        },
    ]
    stats = tel.measure_message_field_bytes(messages)
    assert stats["message_count"] == 2
    assert stats["fields"]["content"]["bytes"] == len(b"hello world") + len(b"ok")
    assert stats["fields"]["content"]["nonempty"] == 2
    assert stats["fields"]["api_content"]["bytes"] > 0
    assert stats["fields"]["codex_reasoning_items"]["bytes"] > 0
    assert stats["fields"]["tool_calls"]["bytes"] > 0
    assert stats["fields"]["reasoning_details"]["bytes"] == 0
    assert stats["total_tracked_bytes"] == sum(
        stats["fields"][k]["bytes"] for k in stats["fields"]
    )
    # Hashes present only for nonempty fields
    assert stats["fields"]["content"]["hash"]
    assert stats["fields"]["reasoning_details"]["hash"] is None


def test_measure_never_embeds_raw_content_in_structure():
    secret = "SUPER_SECRET_TOKEN_do_not_log"
    stats = tel.measure_message_field_bytes(
        [{"role": "user", "content": secret, "api_content": secret}]
    )
    blob = json.dumps(stats)
    assert secret not in blob
    assert "SUPER_SECRET" not in blob


def test_log_compression_phase_emits_json_without_content(caplog, monkeypatch):
    monkeypatch.setattr(
        tel,
        "collect_process_memory_snapshot",
        lambda: {
            "pid": 1,
            "rss_kib": 100,
            "rss_anon_kib": 80,
            "rss_file_kib": 20,
            "pss_kib": 90,
            "thread_count": 4,
        },
    )
    with caplog.at_level(logging.INFO, logger=tel.logger.name):
        payload = tel.log_compression_phase(
            "pre_deepcopy",
            attempt_id="att-1",
            session_id="sess-1",
            extra=tel.measure_message_field_bytes(
                [{"role": "user", "content": "VISIBLE_CONTENT_XYZ"}]
            ),
        )
    assert payload["phase"] == "pre_deepcopy"
    assert payload["rss_kib"] == 100
    assert payload["rss_anon_kib"] == 80
    assert payload["pss_kib"] == 90
    assert "VISIBLE_CONTENT_XYZ" not in caplog.text
    # Log line is parseable JSON after the prefix
    assert "compression lifecycle telemetry:" in caplog.text
    raw = caplog.records[-1].getMessage().split("telemetry: ", 1)[1]
    parsed = json.loads(raw)
    assert parsed["event"] == "compression_lifecycle"
    assert parsed["attempt_id"] == "att-1"
    assert "VISIBLE_CONTENT_XYZ" not in raw


def test_phase_log_volume_is_bounded_per_attempt(caplog, monkeypatch):
    monkeypatch.setattr(
        tel,
        "collect_process_memory_snapshot",
        lambda: {
            "pid": 1,
            "rss_kib": 1,
            "rss_anon_kib": 1,
            "rss_file_kib": 0,
            "pss_kib": 1,
            "thread_count": 1,
        },
    )
    with caplog.at_level(logging.INFO, logger=tel.logger.name):
        for i in range(tel._MAX_PHASE_LOGS_PER_ATTEMPT + 10):
            tel.log_compression_phase(f"phase_{i}", attempt_id="bound-me")
    emitted = [
        r for r in caplog.records if "compression lifecycle telemetry:" in r.getMessage()
    ]
    assert len(emitted) == tel._MAX_PHASE_LOGS_PER_ATTEMPT


def test_log_line_length_bound(caplog, monkeypatch):
    monkeypatch.setattr(
        tel,
        "collect_process_memory_snapshot",
        lambda: {
            "pid": 1,
            "rss_kib": 1,
            "rss_anon_kib": 1,
            "rss_file_kib": 0,
            "pss_kib": 1,
            "thread_count": 1,
        },
    )
    # Oversized free-form strings are rejected by the sanitizer; force a large
    # but legal nested structure of ints to approach the clip bound.
    huge = {f"k{i}": i for i in range(2000)}
    with caplog.at_level(logging.INFO, logger=tel.logger.name):
        tel.log_compression_phase("bulk", attempt_id="clip", extra={"bulk": huge})
    raw = caplog.records[-1].getMessage()
    # Prefix + payload; payload itself is clipped.
    payload_part = raw.split("telemetry: ", 1)[1]
    assert len(payload_part) <= tel._MAX_LOG_LINE_CHARS


def test_codex_stream_telemetry_emits_on_interval_and_close(caplog, monkeypatch):
    monkeypatch.setattr(
        tel,
        "collect_process_memory_snapshot",
        lambda: {
            "pid": 1,
            "rss_kib": 50,
            "rss_anon_kib": 40,
            "rss_file_kib": 10,
            "pss_kib": 45,
            "thread_count": 2,
        },
    )
    # Force event-count threshold only (no time wait).
    monkeypatch.setattr(tel, "_CODEX_EVENT_LOG_EVERY", 5)
    monkeypatch.setattr(tel, "_CODEX_EVENT_LOG_INTERVAL_S", 10_000.0)

    stream = tel.CodexStreamTelemetry(attempt_id="s1", log=tel.logger)
    with caplog.at_level(logging.INFO, logger=tel.logger.name):
        for i in range(12):
            stream.note_event()
            if i % 3 == 0:
                stream.note_text_delta("x" * 10)
            if i % 4 == 0:
                stream.note_output_item_done({"type": "message", "id": str(i)})
        stream.close()
    msgs = [r.getMessage() for r in caplog.records]
    progress = [m for m in msgs if "codex_stream_progress" in m]
    closes = [m for m in msgs if "codex_stream_close" in m]
    assert len(progress) >= 2  # at events 5 and 10
    assert len(closes) == 1
    for m in msgs:
        assert "encrypted" not in m.lower() or "encrypted_content" not in m
        # Ensure no raw delta text leaked (we only sent "x"*10)
        assert "xxxxxxxxxx" not in m


def test_gateway_registry_aggregate_top_histories_content_free():
    sessions = {
        "big": {
            "running": True,
            "transport": object(),
            "history": [
                {"role": "user", "content": "A" * 100},
                {"role": "assistant", "content": "B" * 50},
            ],
        },
        "small": {
            "running": False,
            "transport": object(),
            "history": [{"role": "user", "content": "hi"}],
        },
        "empty": {"running": False, "transport": object(), "history": []},
    }

    def _dead(t):
        return False

    agg = tel.gateway_registry_aggregate(sessions, transport_is_dead=_dead)
    assert agg["live_session_count"] == 3
    assert agg["running_session_count"] == 1
    assert agg["aggregate_history_messages"] == 3
    assert agg["top_histories"][0]["session_id"] == "big"
    assert agg["top_histories"][0]["history_bytes"] >= 150
    blob = json.dumps(agg)
    assert "A" * 20 not in blob
    assert '"content"' not in blob
    assert "msg-body" not in blob


def test_collect_process_memory_snapshot_parses_linux(monkeypatch):
    monkeypatch.setattr(tel.sys, "platform", "linux")

    def _fake_read_text(self, encoding="utf-8"):
        path = str(self)
        if path.endswith("status"):
            return "Name:\tpython\nVmRSS:\t2048 kB\nRssAnon:\t1800 kB\nRssFile:\t248 kB\n"
        if path.endswith("smaps_rollup"):
            return "Pss:               1900 kB\n"
        raise OSError("unexpected path")

    monkeypatch.setattr(tel.Path, "read_text", _fake_read_text)
    monkeypatch.setattr(tel.threading, "active_count", lambda: 7)
    snap = tel.collect_process_memory_snapshot()
    assert snap["rss_kib"] == 2048
    assert snap["rss_anon_kib"] == 1800
    assert snap["rss_file_kib"] == 248
    assert snap["pss_kib"] == 1900
    assert snap["thread_count"] == 7
    assert snap["pid"] == tel.os.getpid()


def test_import_wiring_modules():
    """Smoke: patched modules still import cleanly with the new dependency."""
    import agent.codex_runtime as cr
    import agent.context_compressor as cc
    import agent.conversation_compression as ccomp

    assert hasattr(cr, "_consume_codex_event_stream")
    assert hasattr(cc.ContextCompressor, "_generate_summary")
    assert hasattr(ccomp, "compress_context")


def test_benchmark_field_measure_is_linear_ish():
    """Cheap bound: measuring 500 msgs stays well under 250ms on CI hosts."""
    import time

    messages = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"msg-{i}-" + ("body" * 20),
            "api_content": [{"type": "input_text", "text": f"msg-{i}"}],
        }
        for i in range(500)
    ]
    started = time.perf_counter()
    stats = tel.measure_message_field_bytes(messages)
    elapsed = time.perf_counter() - started
    assert stats["message_count"] == 500
    assert elapsed < 0.25


_SECRETISH = re.compile(
    r"(PASSWORD|SECRET|TOKEN|Bearer\s+[A-Za-z0-9._\-]+|sk-[A-Za-z0-9]+)",
    re.I,
)


def test_no_secret_patterns_in_module_source():
    src = tel.Path(tel.__file__).read_text(encoding="utf-8")
    # The module itself must not hardcode example secrets that look real.
    assert "sk-proj-" not in src
    assert "BEGIN PRIVATE KEY" not in src
