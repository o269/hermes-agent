#!/usr/bin/env python3
"""Install the source-controlled authority-lane fence into one Hermes home.

Only the sole lander/installer should run this command without ``--dry-run`` or
``--check``.  Existing differing files are backed up before atomic replacement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = Path(__file__).resolve().parent

# (source, destination relative to HERMES_HOME, mode)
INSTALL_MANIFEST = (
    (SOURCE_DIR / "authority_lane_policy.py", Path("scripts/authority_lane_policy.py"), 0o644),
    (SOURCE_DIR / "kanban_bridge_state.py", Path("scripts/kanban_bridge_state.py"), 0o755),
    (SOURCE_DIR / "kanban_codex_service.sh", Path("scripts/kanban_codex_service.sh"), 0o755),
    (
        SOURCE_DIR / "kanban_subscription_acp_service.sh",
        Path("scripts/kanban_subscription_acp_service.sh"),
        0o755,
    ),
    (
        SOURCE_DIR / "run_kanban_codex_service.sh",
        Path("scripts/run_kanban_codex_service.sh"),
        0o755,
    ),
    (
        REPO_ROOT
        / "skills/devops/kanban-workflows/references/"
        "canonical-pr-rework-and-respawn-guards.md",
        Path(
            "skills/devops/kanban-workflows/references/"
            "canonical-pr-rework-and-respawn-guards.md"
        ),
        0o644,
    ),
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _assert_safe_destination(home: Path, target: Path) -> None:
    home = home.resolve()
    try:
        target.relative_to(home)
    except ValueError as exc:
        raise ValueError(f"destination escapes Hermes home: {target}") from exc
    cursor = target.parent
    while cursor != home.parent:
        if cursor.is_symlink():
            raise ValueError(f"refusing symlinked install directory: {cursor}")
        if cursor == home:
            break
        cursor = cursor.parent


def _atomic_install(source: Path, target: Path, mode: int, stamp: int) -> str:
    data = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise ValueError(f"refusing to replace symlink destination: {target}")
    action = "created"
    if target.exists():
        if target.read_bytes() == data and stat.S_IMODE(target.stat().st_mode) == mode:
            return "unchanged"
        backup = target.with_name(f"{target.name}.bak.{stamp}")
        shutil.copy2(target, backup, follow_symlinks=False)
        action = f"updated (backup {backup.name})"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return action


def install(home: Path, *, dry_run: bool, check: bool) -> tuple[list[dict[str, object]], bool]:
    """Install/check the manifest and return ``(receipts, all_current)``."""
    home = home.expanduser().resolve()
    stamp = int(time.time())
    receipts: list[dict[str, object]] = []
    all_current = True
    for source, relative, mode in INSTALL_MANIFEST:
        if not source.is_file():
            raise FileNotFoundError(f"missing source-controlled asset: {source}")
        target = home / relative
        _assert_safe_destination(home, target)
        source_data = source.read_bytes()
        current = (
            target.is_file()
            and not target.is_symlink()
            and target.read_bytes() == source_data
            and stat.S_IMODE(target.stat().st_mode) == mode
        )
        all_current = all_current and current
        if check:
            action = "current" if current else "drift"
        elif dry_run:
            action = "unchanged" if current else "would-install"
        else:
            action = _atomic_install(source, target, mode, stamp)
        receipts.append(
            {
                "source": str(source.relative_to(REPO_ROOT)),
                "target": str(target),
                "mode": oct(mode),
                "sha256": _sha256(source_data),
                "action": action,
            }
        )
    return receipts, all_current


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install/check the Kanban authority-lane recurrence fence"
    )
    default_home = Path(
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    )
    parser.add_argument("--hermes-home", type=Path, default=default_home)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipts, all_current = install(
        args.hermes_home,
        dry_run=bool(args.dry_run),
        check=bool(args.check),
    )
    print(
        json.dumps(
            {
                "hermes_home": str(args.hermes_home.expanduser().resolve()),
                "mode": "check" if args.check else "dry-run" if args.dry_run else "install",
                "all_current": all_current,
                "files": receipts,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if args.check and not all_current else 0


if __name__ == "__main__":
    raise SystemExit(main())
