"""Regression tests for propagating CLI subcommand exit codes from main.py.

``hermes_cli.main.main()`` used to call ``args.func(args)`` and discard the
handler's return value. Console-script wrappers only exit nonzero when main
returns the handler's int result, so a failing subcommand (notably
``hermes kanban dispatch``) looked successful to shells and supervisors.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


_WORKTREE = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("handler_result", "expected_main_result"),
    [
        (1, 1),
        (None, None),
        (0, 0),
    ],
)
def test_main_propagates_subcommand_return_value(
    monkeypatch: pytest.MonkeyPatch,
    handler_result: int | None,
    expected_main_result: int | None,
) -> None:
    import hermes_cli.main as main_mod

    seen_args = []

    def fake_cmd_kanban(args):
        seen_args.append(args)
        return handler_result

    monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda args: None)
    monkeypatch.setattr(main_mod, "cmd_kanban", fake_cmd_kanban)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "kanban", "dispatch", "--dry-run"],
    )

    assert main_mod.main() == expected_main_result
    assert len(seen_args) == 1
    assert seen_args[0].command == "kanban"
    assert seen_args[0].kanban_action == "dispatch"


def test_kanban_dispatch_real_cli_exit_status_when_handler_fails(tmp_path: Path) -> None:
    """The real ``python -m hermes_cli.main kanban dispatch`` path must exit 1
    when the kanban handler returns 1 (here via an unknown --board, which
    fails before any dispatcher work or DB writes happen).
    """
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")
    env["PYTHONPATH"] = str(_WORKTREE) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "kanban",
            "--board",
            "ghost",
            "dispatch",
            "--dry-run",
        ],
        cwd=str(_WORKTREE),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1, (
        f"expected exit 1 when the kanban handler returns 1; "
        f"got rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "does not exist" in result.stderr
