#!/usr/bin/env python3
"""Installed entrypoint for the Hermes-Agent PR destination guard."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def _install_source_import_path() -> None:
    hermes_home = Path(
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    ).expanduser()
    candidates = (
        Path(__file__).resolve().parents[2],
        hermes_home / "hermes-agent",
        Path.home() / ".hermes" / "hermes-agent",
    )
    for candidate in candidates:
        if (candidate / "tools" / "github_pr_destination_guard.py").is_file():
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)
            return
    raise RuntimeError(
        "could not locate an installed Hermes checkout containing "
        "tools/github_pr_destination_guard.py"
    )


def main() -> int:
    _install_source_import_path()
    from tools.github_pr_destination_guard import main as guard_main

    return guard_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
