"""Integration coverage for profile-local MCP discovery in slash workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import textwrap
import threading
import time

import pytest
import yaml

pytest.importorskip("mcp.server.fastmcp")


# MCP process startup is CPU-heavy enough to exceed the production 1.5-second
# discovery budget when the suite is running 16 files at once. This integration
# test needs discovery to finish before it can assert the first /tools snapshot,
# so give discovery a deliberately loose test-only budget. The response wait is
# larger still to leave room for HermesCLI initialization on a loaded runner.
_MCP_DISCOVERY_TIMEOUT_S = 30.0
_SLASH_RESPONSE_TIMEOUT_S = 60.0


def _wait_for_stdout_line(proc: subprocess.Popen[str], output: queue.Queue[str]) -> str:
    """Wait portably for one response while failing fast if the worker exits."""
    deadline = time.monotonic() + _SLASH_RESPONSE_TIMEOUT_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(
                "slash worker produced no /tools response within "
                f"{_SLASH_RESPONSE_TIMEOUT_S:g} seconds "
                f"(exit_code={proc.poll()!r})"
            )

        try:
            line = output.get(timeout=min(0.1, remaining))
        except queue.Empty:
            returncode = proc.poll()
            if returncode is None:
                continue
            pytest.fail(
                f"slash worker exited with code {returncode} before /tools responded"
            )

        if line:
            return line
        pytest.fail(
            "slash worker closed stdout before /tools responded "
            f"(exit_code={proc.poll()!r})"
        )


def test_profile_local_mcp_tool_is_visible_in_slash_worker(tmp_path):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    marker = "profile-local-61922"
    server = tmp_path / "fastmcp_probe.py"
    server.write_text(
        textwrap.dedent(
            f"""
            from mcp.server.fastmcp import FastMCP

            mcp = FastMCP("profileprobe")

            @mcp.tool()
            def hermes_61922_profile_probe() -> str:
                return {marker!r}

            if __name__ == "__main__":
                mcp.run(transport="stdio")
            """
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump({
            "mcp_discovery_timeout": _MCP_DISCOVERY_TIMEOUT_S,
            "mcp_servers": {
                "profileprobe": {
                    "enabled": True,
                    "command": sys.executable,
                    "args": [str(server)],
                }
            },
        }),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN"):
            env.pop(key)
    env["HERMES_HOME"] = str(profile_home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["HERMES_SLASH_WATCHDOG_GRACE_S"] = "0"
    env["HERMES_SLASH_WATCHDOG_POLL_S"] = "0.05"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "tui_gateway.slash_worker",
            "--session-key",
            "agent:main:tui:dm:mcp-profile-test",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    output: queue.Queue[str] = queue.Queue()
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        stdout = proc.stdout
        threading.Thread(
            target=lambda: output.put(stdout.readline()),
            daemon=True,
        ).start()
        proc.stdin.write(json.dumps({"id": 1, "command": "/tools"}) + "\n")
        proc.stdin.flush()
        line = _wait_for_stdout_line(proc, output)
        response = json.loads(line)
        assert response["ok"] is True
        assert "mcp__profileprobe__hermes_61922_profile_probe" in response["output"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
