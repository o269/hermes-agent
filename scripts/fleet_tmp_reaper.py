#!/usr/bin/env python3
"""fleet_tmp_reaper — age-based /tmp sweep for the fleet host.

This is the version-controlled, CI-tested replacement for the *candidate gate*
of the unversioned ``~/godmode-bus/bin/tmp-reaper.sh``.

Why this exists
---------------
The shell reaper picked candidates with a hardcoded **name-prefix allowlist**::

    WORKTREE_RE='^(cfr-|omnia|permit-|roof-|a6-|auto-|lead-gen|kimi-|sol-|
                   hermes-w|review|security-|deep-|f530|rev5|califirst|
                   cursor-|tmp\\.)'
    [[ "$base" =~ $WORKTREE_RE ]] || continue      # line 105

An id/prefix-keyed sweep only sees the cases someone remembered to enumerate.
Measured on the fleet host 2026-08-13 (root filesystem 225G, 77% used): of the
299 top-level ``/tmp`` directories with no write in the previous 12 hours —
**46.6 GiB** in total — only **20** matched that regex. The other 279 (93.3%)
were structurally invisible, which is why the reaper logged
``reaped=0 ... skipped=13`` every ten minutes while /tmp grew to 63G. The exact
name list is checked in at ``tests/fixtures/fleet_tmp_reaper/`` and replayed by
the must-fire test.

What replaces it
----------------
1. **Age-based candidate rule.** A top-level entry is a candidate when the
   newest mtime anywhere in its tree (``.git`` bookkeeping pruned, because git
   safety probes touch ``.git/index``) is older than ``--max-age-hours``.
   No name matching of any kind participates in candidacy.
2. **Board-derived live-workspace exclusion.** Anything that *is*, *contains*,
   or *lives inside* the ``workspace_path`` of a non-terminal board task is
   kept, as is anything whose name carries a non-terminal task id.
3. **Live-process exclusion.** Anything referenced by a running process's cwd
   or argv is kept.

Every gate fails **closed**: an unreadable tree, an unresolvable path, or an
unavailable board makes the entry a keep, never a delete.

The sweep is **dry-run by default**. Deleting requires an explicit ``--apply``.

Usage::

    python3 scripts/fleet_tmp_reaper.py                  # dry run, human report
    python3 scripts/fleet_tmp_reaper.py --json           # dry run, machine report
    python3 scripts/fleet_tmp_reaper.py --apply          # actually delete

Exit codes: 0 sweep completed, 3 board unavailable (nothing selected),
4 one or more deletions failed under ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

# ── policy constants ────────────────────────────────────────────────────────

DEFAULT_ROOT = Path("/tmp")

#: A tree must be untouched this long before it is even considered.
DEFAULT_MAX_AGE_SECONDS = 12 * 3600

#: Board statuses that mean "this task will never write here again".
TERMINAL_STATUSES = frozenset({"done", "completed", "archived", "cancelled", "canceled"})

#: Names that are never candidates regardless of age. These are live sockets,
#: OS-managed private mounts, and the Claude Code session scratch root
#: (``claude-<uid>``) whose subdirectories belong to sessions that may be idle
#: for hours and then resume.
PROTECTED_NAME_RE = re.compile(
    r"""^(?:
          \.                       # .X11-unix, .ICE-unix, .font-unix, .XIM-unix
        | claude-\d+$              # live Claude Code session scratch root
        | tmux-\d*$
        | zellij-
        | systemd-private-
        | snap-private-tmp
        | org\.chromium\.
        | pulse-
        | ssh-[A-Za-z0-9]+$
        )""",
    re.VERBOSE,
)

#: Task ids look like ``t_ce22cf43``; they show up inside worktree names such as
#: ``hermes-symptomd-slice2-t_fe15ed02-15658``.
TASK_ID_RE = re.compile(r"t_[0-9a-f]{8,}", re.IGNORECASE)

#: Directory names pruned while computing tree age. Git safety probes and
#: status calls rewrite ``.git/index``, so including it would make every
#: inspected worktree look permanently fresh.
AGE_PRUNE_DIRS = frozenset({".git"})

# Keep reasons (stable strings — logs and tests assert on them).
KEEP_SYMLINK = "symlink"
KEEP_NOT_A_DIRECTORY = "not-a-directory"
KEEP_PROTECTED = "protected-name"
KEEP_UNREADABLE = "unreadable-tree"
KEEP_TOO_NEW = "written-within-max-age"
KEEP_LIVE_WORKSPACE = "live-board-workspace"
KEEP_LIVE_TASK_ID = "live-board-task-id"
KEEP_LIVE_PROCESS = "live-process-reference"
KEEP_BOARD_UNAVAILABLE = "board-unavailable-fail-closed"

SELECT_AGED = "aged-and-unreferenced"


# ── data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BoardTask:
    """One row of the fleet board, reduced to what the sweep needs."""

    task_id: str
    status: str
    workspace_path: str | None = None

    @property
    def is_live(self) -> bool:
        return self.status.strip().lower() not in TERMINAL_STATUSES


@dataclass(frozen=True)
class Decision:
    """The verdict for one top-level entry."""

    path: Path
    selected: bool
    reason: str
    age_seconds: float | None = None

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class SweepResult:
    root: Path
    scanned: int = 0
    decisions: list[Decision] = field(default_factory=list)
    board_available: bool = True

    @property
    def candidates(self) -> list[Decision]:
        return [d for d in self.decisions if d.selected]

    @property
    def keeps(self) -> list[Decision]:
        return [d for d in self.decisions if not d.selected]


# ── live-reference inputs ───────────────────────────────────────────────────


def _resolve(path: str | os.PathLike[str]) -> Path | None:
    try:
        return Path(path).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return None


def live_workspace_paths(tasks: Iterable[BoardTask]) -> frozenset[Path]:
    """Resolved ``workspace_path`` of every non-terminal task."""
    out: set[Path] = set()
    for task in tasks:
        if not task.is_live or not task.workspace_path:
            continue
        resolved = _resolve(task.workspace_path)
        if resolved is not None:
            out.add(resolved)
    return frozenset(out)


def live_task_ids(tasks: Iterable[BoardTask]) -> frozenset[str]:
    """Lowercased ids of every non-terminal task."""
    return frozenset(t.task_id.strip().lower() for t in tasks if t.is_live and t.task_id)


def _paths_overlap(a: Path, b: Path) -> bool:
    """True when *a* and *b* are the same path or one contains the other."""
    return a == b or a in b.parents or b in a.parents


def _contains_or_equals(container: Path, other: Path) -> bool:
    """True when *other* is *container* itself or lives inside it.

    Deliberately one-directional. A process whose cwd is ``/tmp`` does not make
    every directory in ``/tmp`` live; treating an *ancestor* reference as a hit
    would keep the entire sweep permanently empty, which is exactly the class of
    silent no-op this module exists to remove.
    """
    return other == container or container in other.parents


def names_task_ids(name: str) -> frozenset[str]:
    return frozenset(m.group(0).lower() for m in TASK_ID_RE.finditer(name))


def process_referenced_paths(proc_root: Path = Path("/proc")) -> frozenset[Path]:
    """Resolved cwd of every readable process, plus path-shaped argv entries.

    Unreadable processes are skipped rather than treated as "no reference" for
    the whole sweep: the age gate already excludes anything a live process has
    written to recently, and the board gate covers dispatched work.
    """
    out: set[Path] = set()
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return frozenset()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            out.add(Path(os.readlink(entry / "cwd")))
        except OSError:
            pass
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        for arg in raw.split(b"\0"):
            if not arg.startswith(b"/"):
                continue
            try:
                out.add(Path(os.fsdecode(arg)))
            except (ValueError, UnicodeDecodeError):
                continue
    return frozenset(out)


# ── age probe ───────────────────────────────────────────────────────────────


def newest_mtime(path: Path, prune_dirs: frozenset[str] = AGE_PRUNE_DIRS) -> float | None:
    """Newest mtime anywhere under *path*, or ``None`` if it cannot be read.

    ``None`` is a hard keep: we never delete a tree whose age we could not
    establish.
    """
    try:
        newest = path.lstat().st_mtime
    except OSError:
        return None

    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            # A subtree we cannot read could hide arbitrarily fresh writes.
            return None
        for entry in entries:
            # Pruned bookkeeping contributes neither its own mtime nor its
            # contents: `git status`/`ls-remote` safety probes rewrite
            # `.git/index` on every pass, so counting it would make every
            # inspected worktree look permanently fresh.
            if entry.name in prune_dirs and entry.is_dir(follow_symlinks=False):
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                return None
            if stat.st_mtime > newest:
                newest = stat.st_mtime
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
    return newest


# ── the candidate gate ──────────────────────────────────────────────────────


def evaluate_entry(
    entry: Path,
    *,
    now: float,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    live_workspaces: frozenset[Path] = frozenset(),
    live_ids: frozenset[str] = frozenset(),
    process_paths: frozenset[Path] = frozenset(),
    mtime_probe: Callable[[Path], float | None] = newest_mtime,
) -> Decision:
    """Decide a single top-level entry.

    There is deliberately **no name allowlist**. Candidacy is age plus the
    absence of a live reference; names only ever produce *keeps*.
    """
    if entry.is_symlink():
        return Decision(entry, False, KEEP_SYMLINK)
    if not entry.is_dir():
        return Decision(entry, False, KEEP_NOT_A_DIRECTORY)
    if PROTECTED_NAME_RE.match(entry.name):
        return Decision(entry, False, KEEP_PROTECTED)

    resolved = _resolve(entry)
    if resolved is None:
        return Decision(entry, False, KEEP_UNREADABLE)

    for workspace in live_workspaces:
        if _paths_overlap(resolved, workspace):
            return Decision(entry, False, KEEP_LIVE_WORKSPACE)

    if names_task_ids(entry.name) & live_ids:
        return Decision(entry, False, KEEP_LIVE_TASK_ID)

    newest = mtime_probe(entry)
    if newest is None:
        return Decision(entry, False, KEEP_UNREADABLE)
    age = now - newest
    if age < max_age_seconds:
        return Decision(entry, False, KEEP_TOO_NEW, age_seconds=age)

    for referenced in process_paths:
        if _contains_or_equals(resolved, referenced):
            return Decision(entry, False, KEEP_LIVE_PROCESS, age_seconds=age)

    return Decision(entry, True, SELECT_AGED, age_seconds=age)


def sweep(
    root: Path = DEFAULT_ROOT,
    *,
    now: float | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    tasks: Sequence[BoardTask] | None = None,
    board_available: bool = True,
    process_paths: frozenset[Path] | None = None,
    mtime_probe: Callable[[Path], float | None] = newest_mtime,
) -> SweepResult:
    """Evaluate every direct child of *root*.

    ``board_available=False`` (the board could not be read) selects nothing:
    without the board we cannot prove a workspace is dead.
    """
    now = time.time() if now is None else now
    tasks = tasks or []
    result = SweepResult(root=root, board_available=board_available)

    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return result
    result.scanned = len(entries)

    if not board_available:
        result.decisions = [Decision(e, False, KEEP_BOARD_UNAVAILABLE) for e in entries]
        return result

    workspaces = live_workspace_paths(tasks)
    ids = live_task_ids(tasks)
    procs = process_referenced_paths() if process_paths is None else process_paths

    result.decisions = [
        evaluate_entry(
            entry,
            now=now,
            max_age_seconds=max_age_seconds,
            live_workspaces=workspaces,
            live_ids=ids,
            process_paths=procs,
            mtime_probe=mtime_probe,
        )
        for entry in entries
    ]
    return result


# ── board access ────────────────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parent.parent


def ensure_repo_on_path(path_entries: list[str] | None = None) -> list[str]:
    """Put the repo root on ``sys.path``.

    Running this file as a script puts ``scripts/`` on ``sys.path``, not the
    repo root, so ``import hermes_cli`` fails and the board read reports
    "unavailable" — which, because the board gate fails closed, silently turns
    the whole sweep into a no-op. That is the same shape of silent no-op the
    module exists to remove, so the path fix is explicit and tested.
    """
    entries = sys.path if path_entries is None else path_entries
    root = str(REPO_ROOT)
    if root not in entries:
        entries.insert(0, root)
    return entries


def load_board_tasks() -> list[BoardTask] | None:
    """Read the fleet board through the broker. ``None`` means unavailable.

    Read through ``kb_client`` rather than opening the sqlite file: a raw
    read-only open still maps ``-shm`` and joins the WAL-index domain.
    """
    try:
        ensure_repo_on_path()
        from hermes_cli import kb_client  # noqa: PLC0415 — optional at import time

        rows = kb_client.get_client().query(
            "SELECT id, status, workspace_path FROM tasks", [], max_rows=500000
        )
    except Exception:
        return None
    return [
        BoardTask(
            task_id=str(row["id"] or ""),
            status=str(row["status"] or ""),
            workspace_path=row["workspace_path"],
        )
        for row in rows
    ]


# ── CLI ─────────────────────────────────────────────────────────────────────


def _default_deleter(path: Path) -> None:
    shutil.rmtree(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet_tmp_reaper",
        description="Age-based /tmp sweep with a board-derived live-workspace exclusion.",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_SECONDS / 3600,
        help="A tree must be untouched this long to be a candidate (default: 12).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Omitted = dry run, which is the default.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    parser.add_argument(
        "--show-keeps",
        action="store_true",
        help="Also list kept entries with their keep reason.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    tasks_loader: Callable[[], list[BoardTask] | None] = load_board_tasks,
    deleter: Callable[[Path], None] = _default_deleter,
    stdout=None,
) -> int:
    args = build_parser().parse_args(argv)
    out = stdout or sys.stdout

    tasks = tasks_loader()
    result = sweep(
        args.root,
        max_age_seconds=int(args.max_age_hours * 3600),
        tasks=tasks or [],
        board_available=tasks is not None,
    )

    deleted: list[str] = []
    failed: list[str] = []
    if args.apply:
        for decision in result.candidates:
            try:
                deleter(decision.path)
                deleted.append(str(decision.path))
            except Exception as exc:  # noqa: BLE001 — one failure must not abort the sweep
                failed.append(f"{decision.path}: {type(exc).__name__}: {exc}")

    mode = "apply" if args.apply else "dry-run"
    if args.json:
        payload = {
            "mode": mode,
            "root": str(result.root),
            "board_available": result.board_available,
            "scanned": result.scanned,
            "candidates": [
                {"path": str(d.path), "reason": d.reason, "age_seconds": d.age_seconds}
                for d in result.candidates
            ],
            "deleted": deleted,
            "failed": failed,
        }
        if args.show_keeps:
            payload["keeps"] = [
                {"path": str(d.path), "reason": d.reason} for d in result.keeps
            ]
        print(json.dumps(payload, indent=2, sort_keys=True), file=out)
    else:
        print(
            f"mode={mode} root={result.root} board_available={result.board_available} "
            f"scanned={result.scanned} candidates={len(result.candidates)} "
            f"deleted={len(deleted)} failed={len(failed)}",
            file=out,
        )
        for decision in result.candidates:
            age_h = (decision.age_seconds or 0) / 3600
            verb = "DELETED" if str(decision.path) in deleted else "WOULD-DELETE"
            print(f"  {verb} {decision.path} (idle {age_h:.1f}h)", file=out)
        if args.show_keeps:
            for decision in result.keeps:
                print(f"  KEEP {decision.path} ({decision.reason})", file=out)
        for line in failed:
            print(f"  ERROR {line}", file=out)

    if not result.board_available:
        return 3
    if failed:
        return 4
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
