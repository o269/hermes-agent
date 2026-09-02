"""Focused regressions for the Copilot ACP shim safety layer."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.copilot_acp_client import (
    CopilotACPClient,
    _scan_acp_stream_text,
)


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()


class CopilotACPClientSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = CopilotACPClient(acp_cwd="/tmp")



    def test_stream_true_preserves_tool_call_deltas(self) -> None:
        tool_response = (
            "<tool_call>"
            '{"id":"call_read","type":"function",'
            '"function":{"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}'
            "</tool_call>"
        )

        with patch.object(self.client, "_run_prompt", return_value=(tool_response, "")):
            stream = self.client._create_chat_completion(
                model="copilot-acp",
                messages=[{"role": "user", "content": "read README.md"}],
                stream=True,
            )
            # Consume inside the patch context: streaming is lazy — the
            # generator drives _run_prompt on first iteration, so deferring
            # consumption past the with-block would run the REAL subprocess.
            chunks = list(stream)
        delta = chunks[0].choices[0].delta
        self.assertIsNone(delta.content)
        self.assertEqual(chunks[0].choices[0].finish_reason, "tool_calls")
        self.assertEqual(len(delta.tool_calls), 1)
        tool_delta = delta.tool_calls[0]
        self.assertEqual(tool_delta.index, 0)
        self.assertEqual(tool_delta.id, "call_read")
        self.assertEqual(tool_delta.function.name, "read_file")
        self.assertEqual(
            json.loads(tool_delta.function.arguments),
            {"path": "README.md"},
        )
        self.assertEqual(chunks[1].choices, [])


class ACPIncrementalStreamingTests(unittest.TestCase):
    """Real streaming: chunks must arrive DURING the subprocess call.

    The generator drives ``_run_prompt`` on a worker thread and forwards
    each ``session/update`` notification (via the ``on_update`` hook) as
    an OpenAI-style delta the moment it arrives — a regression to
    post-hoc chunking (waiting for the full completion, then slicing it)
    fails the timing assertions here.
    """

    def setUp(self) -> None:
        self.client = CopilotACPClient(acp_cwd="/tmp")

    def _stream(self, run_prompt_fake, **create_kwargs):
        self.client._run_prompt = run_prompt_fake
        return self.client._create_chat_completion(
            model="copilot-acp",
            messages=[{"role": "user", "content": "go"}],
            stream=True,
            **create_kwargs,
        )

    def test_deltas_arrive_before_run_prompt_completes(self):
        timeline: list[float] = []
        t0 = time.monotonic()

        def fake(prompt_text, *, timeout_seconds, on_update=None):
            for text in ("one ", "two ", "three"):
                time.sleep(0.05)
                if on_update:
                    on_update("agent_message_chunk", text)
                timeline.append(time.monotonic() - t0)
            time.sleep(0.3)  # gap AFTER the last notification
            return "one two three", ""

        gen = self._stream(fake)
        first_delta_at = None
        deltas = []
        for chunk in gen:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is not None and delta.content:
                if first_delta_at is None:
                    first_delta_at = time.monotonic() - t0
                deltas.append(delta.content)

        self.assertEqual(len(deltas), 3)
        # The first delta must precede the LAST notification by a clear
        # margin — post-hoc chunking would emit everything only after
        # fake() returned (>= 0.55s here).
        assert first_delta_at is not None
        self.assertLess(first_delta_at, timeline[-1])

    def test_tool_markers_never_stream_as_content(self):
        marker = (
            '<tool_call>{"id":"c1","type":"function",'
            '"function":{"name":"read_file","arguments":"{}"}}</tool_call>'
        )

        def fake(prompt_text, *, timeout_seconds, on_update=None):
            if on_update:
                on_update("agent_message_chunk", "before ")
                on_update("agent_message_chunk", marker)
                on_update("agent_message_chunk", " after")
            return "before " + marker + " after", ""

        gen = self._stream(fake)
        streamed = []
        tool_delta = None
        finish = None
        for chunk in gen:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.delta.content:
                streamed.append(choice.delta.content)
            if choice.delta.tool_calls:
                tool_delta = choice.delta.tool_calls[0]
            if choice.finish_reason:
                finish = choice.finish_reason

        joined = "".join(streamed)
        self.assertNotIn("<tool_call>", joined)
        self.assertNotIn("</tool_call>", joined)
        self.assertIn("before", joined)
        self.assertIn("after", joined)
        self.assertIsNotNone(tool_delta)
        self.assertEqual(tool_delta.function.name, "read_file")
        self.assertEqual(finish, "tool_calls")

    def test_thought_chunks_stream_as_reasoning(self):
        def fake(prompt_text, *, timeout_seconds, on_update=None):
            if on_update:
                on_update("agent_thought_chunk", "[Using tool: Bash]\n")
            return "done", ""

        gen = self._stream(fake)
        reasoning = [
            c.choices[0].delta.reasoning_content
            for c in gen
            if c.choices and c.choices[0].delta.reasoning_content
        ]
        self.assertEqual(reasoning, ["[Using tool: Bash]\n"])

    def test_early_close_aborts_subprocess(self):
        aborted = {"v": False}
        release = threading.Event()

        def fake(prompt_text, *, timeout_seconds, on_update=None):
            for i in range(100):
                if release.wait(timeout=0.05):
                    break
                if on_update:
                    on_update("agent_message_chunk", f"t{i} ")
            return "done", ""

        def fake_close():
            aborted["v"] = True
            release.set()

        self.client._run_prompt = fake
        self.client.close = fake_close
        gen = self._stream(fake)
        seen = 0
        for _chunk in gen:
            seen += 1
            if seen == 2:
                gen.close()
                break
        self.assertTrue(aborted["v"])

    def test_legacy_no_notifications_emits_full_text(self):
        # A bridge/test double that never calls on_update still yields a
        # coherent single-shot stream (previous behavior).
        def fake(prompt_text, *, timeout_seconds, on_update=None):
            return "plain final answer", ""

        gen = self._stream(fake)
        contents = [
            c.choices[0].delta.content
            for c in gen
            if c.choices and c.choices[0].delta.content
        ]
        self.assertEqual(contents, ["plain final answer"])

    def test_error_from_run_prompt_propagates(self):
        def fake(prompt_text, *, timeout_seconds, on_update=None):
            if on_update:
                on_update("agent_message_chunk", "partial ")
            raise RuntimeError("boom")

        gen = self._stream(fake)
        with self.assertRaises(RuntimeError):
            list(gen)


class ACPStreamTextGateTests(unittest.TestCase):
    """``_scan_acp_stream_text``: hold incomplete markers, drop complete."""

    def test_plain_text_passes_through(self):
        emit, carry = _scan_acp_stream_text("", "hello world")
        self.assertEqual((emit, carry), ("hello world", ""))

    def test_complete_marker_dropped(self):
        marker = '<tool_call>{"id":"c","type":"function","function":{}}</tool_call>'
        emit, carry = _scan_acp_stream_text("", "before " + marker + " after")
        self.assertEqual((emit, carry), ("before  after", ""))

    def test_unclosed_marker_held_back(self):
        emit, carry = _scan_acp_stream_text("", "text <tool_call>{\"par")
        self.assertEqual(emit, "text ")
        self.assertEqual(carry, '<tool_call>{"par')

    def test_marker_completing_in_later_chunk(self):
        _, carry = _scan_acp_stream_text("", "text <tool_call>{\"par")
        emit, carry2 = _scan_acp_stream_text(carry, 'tial": 1}</tool_call> tail')
        self.assertEqual(emit, " tail")
        self.assertEqual(carry2, "")

    def test_trailing_partial_opener_held(self):
        emit, carry = _scan_acp_stream_text("", "ending with <tool_c")
        self.assertEqual(emit, "ending with ")
        self.assertEqual(carry, "<tool_c")


class CopilotACPClientServerMessageTests(unittest.TestCase):
    """fs/read|write + permission handling on _handle_server_message."""

    def setUp(self) -> None:
        self.client = CopilotACPClient(acp_cwd="/tmp")

    def _dispatch(self, message: dict, *, cwd: str) -> dict:
        process = _FakeProcess()
        handled = self.client._handle_server_message(
            message,
            process=process,
            cwd=cwd,
            text_parts=[],
            reasoning_parts=[],
        )
        self.assertTrue(handled)
        payload = process.stdin.getvalue().strip()
        self.assertTrue(payload)
        return json.loads(payload)



    def test_read_text_file_redacts_sensitive_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secret_file = root / "config.env"
            secret_file.write_text("OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012")

            # agent.redact snapshots HERMES_REDACT_SECRETS at import time into
            # _REDACT_ENABLED, so patching os.environ is a no-op. Flip the
            # module-level constant directly for the duration of the call.
            with patch("agent.redact._REDACT_ENABLED", True):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "fs/read_text_file",
                        "params": {"path": str(secret_file)},
                    },
                    cwd=str(root),
                )

        content = ((response.get("result") or {}).get("content") or "")
        self.assertNotIn("abc123def456", content)
        self.assertIn("OPENAI_API_KEY=", content)

    def test_fs_read_text_file_decodes_as_utf8_under_non_utf8_locale(self) -> None:
        """Regression for #18637 (bug 2): fs/read_text_file used
        ``path.read_text()`` with no explicit encoding, so on Windows
        GBK/CP932/CP949 locales the Copilot read_file tool crashed on any
        source file with non-ASCII content (e.g. a CJK comment, an em dash,
        or UTF-8 BOM)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "note.md"
            target.write_text("# 中文标题\nem dash — here\n", encoding="utf-8")

            original_read_text = Path.read_text

            def strict_read_text(self, encoding=None, errors=None, **kwargs):
                if self == target and encoding != "utf-8":
                    raise UnicodeDecodeError(
                        "gbk", b"\x94", 0, 1, "illegal multibyte sequence"
                    )
                return original_read_text(
                    self, encoding=encoding, errors=errors, **kwargs
                )

            with patch.object(Path, "read_text", strict_read_text):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "fs/read_text_file",
                        "params": {"path": str(target)},
                    },
                    cwd=str(root),
                )

        self.assertNotIn("error", response)
        content = ((response.get("result") or {}).get("content") or "")
        self.assertIn("中文标题", content)
        self.assertIn("em dash —", content)



    def test_write_text_file_respects_safe_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            safe_root = root / "workspace"
            safe_root.mkdir()
            outside = root / "outside.txt"

            with patch.dict(os.environ, {"HERMES_WRITE_SAFE_ROOT": str(safe_root)}, clear=False):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "fs/write_text_file",
                        "params": {
                            "path": str(outside),
                            "content": "should-not-write",
                        },
                    },
                    cwd=str(root),
                )

        self.assertIn("error", response)
        self.assertIn("HERMES_WRITE_SAFE_ROOT", str(response["error"]))
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()


# ── HOME env propagation tests (from PR #11285) ─────────────────────

from unittest.mock import patch as _patch
import pytest


def _make_home_client(tmp_path):
    return CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command="copilot",
        acp_args=["--acp", "--stdio"],
        acp_cwd=str(tmp_path),
    )


def _fake_popen_capture(captured):
    def _fake(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        raise FileNotFoundError("copilot not found")
    return _fake


def test_run_prompt_preserves_real_home_when_profile_home_available(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    (hermes_home / "home").mkdir(parents=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()

    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # Hermeticity: an ambient HERMES_REAL_HOME (exported by Hermes' own
    # terminal contract on dev boxes) outranks HOME in the candidate ladder,
    # and an ambient TERMINAL_HOME_MODE would change the policy under test.
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    monkeypatch.delenv("TERMINAL_HOME_MODE", raising=False)
    # Hermeticity: get_subprocess_home()'s auto mode prefers the profile home
    # when is_container() is True — on a containerized CI runner that real
    # probe flips the resolution this test asserts. The host/VM branch is the
    # contract under test; pin containment off.
    monkeypatch.setattr("hermes_constants.is_container", lambda: False)

    captured = {}
    client = _make_home_client(tmp_path)

    # Hermeticity: the --acp support probe (PR #87308) calls subprocess.run
    # before Popen; stub it inconclusive so no real CLI on the host box can
    # flip the resolution this test asserts.
    with _patch("agent.copilot_acp_client.subprocess.run", side_effect=FileNotFoundError):
        with _patch("agent.copilot_acp_client.subprocess.Popen", side_effect=_fake_popen_capture(captured)):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)

    assert captured["kwargs"]["env"]["HOME"] == str(real_home)
    assert captured["kwargs"]["env"]["HERMES_REAL_HOME"] == str(real_home)


def test_run_prompt_passes_home_when_parent_env_is_clean(monkeypatch, tmp_path):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    captured = {}
    client = _make_home_client(tmp_path)

    # Hermeticity: the --acp support probe (PR #87308) calls subprocess.run
    # before Popen; stub it inconclusive so no real CLI on the host box can
    # flip the resolution this test asserts.
    with _patch("agent.copilot_acp_client.subprocess.run", side_effect=FileNotFoundError):
        with _patch("agent.copilot_acp_client.subprocess.Popen", side_effect=_fake_popen_capture(captured)):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)

    assert "env" in captured["kwargs"]
    assert captured["kwargs"]["env"]["HOME"]


# ── --acp support probe tests (PR #87308 / issue #87309) ────────────

import subprocess as _subprocess

from agent.copilot_acp_client import _ACP_PROBE_CACHE, _acp_supported


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    _ACP_PROBE_CACHE.clear()
    yield
    _ACP_PROBE_CACHE.clear()


def _completed(returncode=0, stdout=""):
    return _subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_probe_true_when_help_advertises_acp():
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: copilot [--acp] [--stdio]"),
    ):
        assert _acp_supported("copilot", ["--acp", "--stdio"]) is True


def test_probe_false_when_help_lacks_acp_and_run_prompt_fast_fails(tmp_path):
    client = _make_home_client(tmp_path)
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: claude [--print] [--model]"),
    ):
        with pytest.raises(RuntimeError, match="ACP transport not supported"):
            client._run_prompt("hello", timeout_seconds=1)


def test_probe_inconclusive_falls_through_to_spawn_error(tmp_path):
    """Missing binary: probe must NOT mask the established spawn error."""
    client = _make_home_client(tmp_path)
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        side_effect=FileNotFoundError("copilot not found"),
    ):
        with _patch(
            "agent.copilot_acp_client.subprocess.Popen",
            side_effect=FileNotFoundError("copilot not found"),
        ):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)


def test_probe_result_cached_per_binary_path():
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: copilot [--acp]"),
    ) as run_mock:
        assert _acp_supported("copilot", ["--acp"]) is True
        assert _acp_supported("copilot", ["--acp"]) is True
    assert run_mock.call_count == 1


def test_probe_inconclusive_not_cached():
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        side_effect=FileNotFoundError,
    ) as run_mock:
        assert _acp_supported("copilot", ["--acp"]) is None
        assert _acp_supported("copilot", ["--acp"]) is None
    assert run_mock.call_count == 2  # inconclusive verdicts retry


def test_probe_skipped_for_custom_args_without_acp():
    with _patch("agent.copilot_acp_client.subprocess.run") as run_mock:
        assert _acp_supported("mycli", ["--custom-transport"]) is True
    run_mock.assert_not_called()
