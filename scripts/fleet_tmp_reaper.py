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
   kept, as is anything whose name carries a non-terminal task id. Fleet
   workspace names carry the id in **both** forms — ``t_7e073169`` and the
   underscore-less ``t7e073169`` (``/tmp/wave-d4-item2-t7e073169``) — so ids are
   normalised to ``t_<hex>`` on both sides before the board lookup.
3. **Live-process exclusion.** Anything referenced by a running process's cwd,
   argv, or open file descriptors is kept. The scan must be **complete**: an
   unreadable ``/proc/<pid>`` is an unknown holder, not an absent one, so it
   fails the whole sweep closed rather than silently reading as "nobody has
   this open". ACP-bridge and ``claude -p`` grandchildren outlive their worker,
   reparent to ``systemd --user`` and keep writing their workspace for days, so
   a directory with no live *worker* can still have a live *writer*.
4. **Unpushed-work exclusion.** A git tree with a tracked modification, a commit
   on no remote, or a stash holds work that exists nowhere else. A clean clone
   is re-clonable; a dirty one is not.

Every gate fails **closed**: an unreadable tree, an unresolvable path, an
incomplete holder scan, a git probe that errors, or an unavailable board makes
the entry a keep, never a delete.

The sweep is **dry-run by default**. Deleting requires an explicit ``--apply``.

Usage::

    python3 scripts/fleet_tmp_reaper.py                  # dry run, human report
    python3 scripts/fleet_tmp_reaper.py --json           # dry run, machine report
    python3 scripts/fleet_tmp_reaper.py --apply          # actually delete

Exit codes: 0 sweep completed, 3 board unavailable (nothing selected),
4 one or more deletions failed under ``--apply``, 5 holder scan incomplete
(nothing selected).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
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
TERMINAL_STATUSES = frozenset({
    "done",
    "completed",
    "archived",
    "cancelled",
    "canceled",
})

#: Names that are never candidates regardless of age. These are live sockets,
#: OS-managed private mounts, the Claude Code session scratch root
#: (``claude-<uid>``) whose subdirectories belong to sessions that may be idle
#: for hours and then resume, and the exclusions the shipped disk-watchdog
#: (``godmode-bus/bin/disk-watchdog.sh`` lines 98-102, ``tmp-build-gc.sh`` lines
#: 38-42) already encodes and which an age-only gate would otherwise drop.
#:
#: ``hermes-results`` is the one that must never be lost: it is the live Hermes
#: tool-result store (``STORAGE_DIR`` in ``tools/tool_result_storage.py``).
#: Agents read those payloads back **by path** long after the file was written,
#: so its tree age says nothing about whether it is in use — deleting it breaks
#: in-flight agents. ``hermes_sandbox_*`` are live sandbox roots; the remainder
#: (``tsx-``, ``corepack-``, ``bin-``, ``node-v``, ``tmp.``, ``kanban-boardd-run``)
#: are toolchain and broker runtime dirs whose consumers hold them open without
#: ever writing to them.
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
        | hermes-results           # LIVE tool-result store, read back by path
        | hermes_sandbox_          # live sandbox roots
        | kanban-boardd-run        # board broker runtime dir
        | tsx-                     # tsx/esbuild compile caches
        | corepack-
        | bin-
        | node-v                   # extracted node toolchains
        | tmp\.                    # mktemp -d default prefix
        )""",
    re.VERBOSE,
)

#: Task ids look like ``t_ce22cf43``, but fleet workspace names **drop the
#: underscore**: ``/tmp/wave-d4-item2-t7e073169``, ``/tmp/cursor-verify-t19ba5cba``.
#: Requiring ``t_`` made every underscore-less workspace invisible to the board
#: gate — which is precisely how the sweep selected the scratch trees of two
#: live cards. Both forms are matched here and normalised by
#: :func:`normalise_task_id` before the lookup, so they resolve to one card id.
#:
#: The minimum is **four** hex digits, not eight, because names also *truncate*
#: the id: ``/tmp/hermes-tce22-fd-reaper-main`` carries only ``tce22`` for card
#: ``t_ce22cf43``. An eight-digit rule matches nothing in that name, so the
#: board gate never runs on it. Short tokens are resolved by prefix against the
#: live-id set (:func:`resolve_live_ids`) rather than by equality, so a
#: truncated id still finds its card. A four-digit prefix can collide, but a
#: collision only ever produces an extra *keep*.
TASK_ID_RE = re.compile(r"t_?[0-9a-f]{4,}", re.IGNORECASE)

#: The strict spelling, kept for reporting: ids written out in full.
TASK_ID_FULL_RE = re.compile(r"t_?[0-9a-f]{8,}", re.IGNORECASE)

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
KEEP_UNPUSHED_WORK = "unpushed-git-work"
KEEP_HOLDER_SCAN_INCOMPLETE = "holder-scan-incomplete-fail-closed"

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


@dataclass(frozen=True)
class HolderScan:
    """Everything a live process points at, plus whether the scan was complete.

    ``complete=False`` means at least one ``/proc/<pid>`` could not be read, so
    the set of holders is a *lower bound*. Treating that as "no holder" is a
    false negative in the destructive direction, so it fails the sweep closed.
    """

    paths: frozenset[Path] = frozenset()
    unreadable_pids: tuple[int, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unreadable_pids


@dataclass
class SweepResult:
    root: Path
    scanned: int = 0
    decisions: list[Decision] = field(default_factory=list)
    board_available: bool = True
    holder_scan_complete: bool = True
    unreadable_pids: tuple[int, ...] = ()

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


def normalise_task_id(raw: str) -> str:
    """Fold ``t7e073169`` and ``t_7e073169`` onto one canonical ``t_<hex>``.

    The board stores ids as ``t_<hex>``; fleet workspace directory names drop
    the underscore. Without this fold the two spellings are different strings
    and every underscore-less workspace misses the board lookup entirely.
    """
    lowered = raw.strip().lower()
    if lowered.startswith("t_"):
        return lowered
    if lowered.startswith("t"):
        return "t_" + lowered[1:]
    return lowered


def live_task_ids(tasks: Iterable[BoardTask]) -> frozenset[str]:
    """Canonical ids of every non-terminal task."""
    return frozenset(
        normalise_task_id(t.task_id) for t in tasks if t.is_live and t.task_id
    )


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
    """Canonical id tokens appearing in *name*, in either spelling.

    Tokens may be truncated (``t_ce22`` for ``t_ce22cf43``); use
    :func:`resolve_live_ids` to turn them into board ids.
    """
    return frozenset(normalise_task_id(m.group(0)) for m in TASK_ID_RE.finditer(name))


def resolve_live_ids(name: str, live_ids: frozenset[str]) -> frozenset[str]:
    """Live board ids that *name* refers to, exactly or by truncation."""
    tokens = names_task_ids(name)
    return frozenset(
        live
        for live in live_ids
        for token in tokens
        if live == token or live.startswith(token)
    )


def _is_permission_error(exc: OSError) -> bool:
    """True for "you may not look", false for "it is not there any more".

    ``ESRCH``/``ENOENT`` mean the process exited between ``iterdir`` and the
    read — a genuinely absent holder, safe to skip. ``EACCES``/``EPERM`` mean a
    process we are not allowed to inspect, whose holdings are unknown.
    """
    return isinstance(exc, PermissionError)


def process_referenced_paths(proc_root: Path = Path("/proc")) -> HolderScan:
    """Every path a live process points at: cwd, path-shaped argv, and open fds.

    An unreadable ``/proc/<pid>`` is recorded, never silently skipped. Reading
    it as "this process holds nothing" is a false negative that points straight
    at deletion, and the holders it hides are exactly the dangerous ones: ACP
    bridge and ``claude -p`` grandchildren survive their parent worker, reparent
    to ``systemd --user``, and keep writing their workspace for days (orphans
    aged 13 days have been observed on this host).
    """
    out: set[Path] = set()
    unreadable: set[int] = set()
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        # No /proc at all: we know nothing about holders. Report it as such.
        return HolderScan(frozenset(), (-1,))

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        denied = False

        try:
            out.add(Path(os.readlink(entry / "cwd")))
        except OSError as exc:
            denied = denied or _is_permission_error(exc)

        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError as exc:
            denied = denied or _is_permission_error(exc)
            raw = b""
        for arg in raw.split(b"\0"):
            if not arg.startswith(b"/"):
                continue
            try:
                out.add(Path(os.fsdecode(arg)))
            except (ValueError, UnicodeDecodeError):
                continue

        # Open descriptors catch the writer that neither cwd nor argv names —
        # a detached grandchild streaming into a workspace it never chdir'd to.
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError as exc:
            denied = denied or _is_permission_error(exc)
            fds = []
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError as exc:
                denied = denied or _is_permission_error(exc)
                continue
            if target.startswith("/"):
                out.add(Path(target))

        if denied:
            unreadable.add(pid)

    return HolderScan(frozenset(out), tuple(sorted(unreadable)))


def privileged_holder_scan(
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None,
) -> HolderScan | None:
    """Re-run the holder scan as root via ``sudo -n``. ``None`` if unavailable.

    Only the *scan* is escalated, not the whole sweep: the board read has to
    stay as the invoking user or ``kb_client`` reads the wrong broker session.
    """

    def _default(cmd: Sequence[str]) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=300, check=False
        )

    run = runner or _default
    cmd = [
        "sudo",
        "-n",
        sys.executable,
        str(Path(__file__).resolve()),
        "--emit-holder-scan",
    ]
    try:
        proc = run(cmd)
    except Exception:  # noqa: BLE001 — sudo missing, timeout, anything: no escalation
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        payload = json.loads(proc.stdout)
        return HolderScan(
            frozenset(Path(p) for p in payload["paths"]),
            tuple(payload["unreadable_pids"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


def complete_holder_scan(*, escalate: bool = True) -> HolderScan:
    """Scan as the current user; if that is partial and we are not root, sudo."""
    scan = process_referenced_paths()
    if scan.complete or not escalate or os.geteuid() == 0:
        return scan
    elevated = privileged_holder_scan()
    if elevated is None:
        return scan
    # Union: the unprivileged pass may have seen a process that exited before
    # the privileged one started.
    return HolderScan(scan.paths | elevated.paths, elevated.unreadable_pids)


# ── unpushed-work probe ─────────────────────────────────────────────────────


def _run_git(path: Path, args: Sequence[str], timeout: float = 60.0) -> str | None:
    """Run a read-only git command in *path*. ``None`` means "could not tell"."""
    cmd = [
        "git",
        # Root inspecting an odai-owned clone otherwise dies on "dubious
        # ownership" — which would fail every git tree closed and stall the
        # sweep instead of measuring it.
        "-c",
        "safe.directory=*",
        # Never take the index lock or rewrite `.git/index`: this probe must not
        # be the thing that makes a tree look freshly written.
        "--no-optional-locks",
        "-C",
        str(path),
        *args,
    ]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except Exception:  # noqa: BLE001 — git missing, timeout, OSError
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def has_unpushed_work(
    path: Path, runner: Callable[[Path, Sequence[str]], str | None] = _run_git
) -> bool:
    """True when *path* is a git tree holding work that exists nowhere else.

    Any one of these is enough to keep the tree:

    * ``git status --porcelain`` reports a **tracked** change (staged, modified
      or deleted). Untracked files alone are build output, not work.
    * ``git log --branches --not --remotes`` is non-empty — a commit on no
      remote. Deleting the clone deletes the only copy.
    * ``git stash list`` is non-empty.

    A probe that cannot answer returns ``True``. The asymmetry is the point: a
    clean clone is re-clonable from origin and costs a re-clone if we are wrong;
    a dirty one is unrecoverable and costs the work.
    """
    if not (path / ".git").exists():
        return False  # not a git tree; nothing for this gate to protect

    status = runner(path, ["status", "--porcelain", "--untracked-files=no"])
    if status is None:
        return True
    if status.strip():
        return True

    unpushed = runner(path, ["log", "--branches", "--not", "--remotes", "--format=%H"])
    if unpushed is None:
        return True
    if unpushed.strip():
        return True

    stashes = runner(path, ["stash", "list"])
    if stashes is None:
        return True
    return bool(stashes.strip())


# ── age probe ───────────────────────────────────────────────────────────────


def newest_mtime(
    path: Path, prune_dirs: frozenset[str] = AGE_PRUNE_DIRS
) -> float | None:
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
    unpushed_probe: Callable[[Path], bool] = has_unpushed_work,
) -> Decision:
    """Decide a single top-level entry.

    There is deliberately **no name allowlist**. Candidacy is age plus the
    absence of a live reference plus the absence of unpushed work; names only
    ever produce *keeps*.
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

    if resolve_live_ids(entry.name, live_ids):
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

    # Last, because it is the only gate that forks a subprocess: by here the
    # entry is aged, unreferenced and off the board, so this runs on a small set.
    if unpushed_probe(entry):
        return Decision(entry, False, KEEP_UNPUSHED_WORK, age_seconds=age)

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
    unpushed_probe: Callable[[Path], bool] = has_unpushed_work,
    escalate_holder_scan: bool = True,
) -> SweepResult:
    """Evaluate every direct child of *root*.

    Two conditions select **nothing**, because each removes our ability to
    prove a tree is dead rather than merely quiet:

    * ``board_available=False`` — without the board we cannot tell a finished
      workspace from a dispatchable one.
    * an incomplete holder scan — an unreadable ``/proc/<pid>`` is an unknown
      holder, and "unknown" must not read as "none".

    Passing ``process_paths`` explicitly asserts a complete scan (tests do this).
    """
    now = time.time() if now is None else now
    tasks = tasks or []

    if process_paths is None:
        scan = complete_holder_scan(escalate=escalate_holder_scan)
    else:
        scan = HolderScan(process_paths, ())

    result = SweepResult(
        root=root,
        board_available=board_available,
        holder_scan_complete=scan.complete,
        unreadable_pids=scan.unreadable_pids,
    )

    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return result
    result.scanned = len(entries)

    if not board_available:
        result.decisions = [Decision(e, False, KEEP_BOARD_UNAVAILABLE) for e in entries]
        return result
    if not scan.complete:
        result.decisions = [
            Decision(e, False, KEEP_HOLDER_SCAN_INCOMPLETE) for e in entries
        ]
        return result

    workspaces = live_workspace_paths(tasks)
    ids = live_task_ids(tasks)

    result.decisions = [
        evaluate_entry(
            entry,
            now=now,
            max_age_seconds=max_age_seconds,
            live_workspaces=workspaces,
            live_ids=ids,
            process_paths=scan.paths,
            mtime_probe=mtime_probe,
            unpushed_probe=unpushed_probe,
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
    parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable report."
    )
    parser.add_argument(
        "--show-keeps",
        action="store_true",
        help="Also list kept entries with their keep reason.",
    )
    parser.add_argument(
        "--no-escalate-holder-scan",
        action="store_true",
        help=(
            "Do not re-run the /proc holder scan under `sudo -n`. Without root "
            "the scan is usually incomplete, which fails the sweep closed."
        ),
    )
    parser.add_argument(
        "--emit-holder-scan",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: the privileged re-invocation
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

    if args.emit_holder_scan:
        scan = process_referenced_paths()
        print(
            json.dumps({
                "paths": sorted(str(p) for p in scan.paths),
                "unreadable_pids": list(scan.unreadable_pids),
            }),
            file=out,
        )
        return 0

    tasks = tasks_loader()
    result = sweep(
        args.root,
        max_age_seconds=int(args.max_age_hours * 3600),
        tasks=tasks or [],
        board_available=tasks is not None,
        escalate_holder_scan=not args.no_escalate_holder_scan,
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
            "holder_scan_complete": result.holder_scan_complete,
            "unreadable_pids": list(result.unreadable_pids),
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
            f"holder_scan_complete={result.holder_scan_complete} "
            f"scanned={result.scanned} candidates={len(result.candidates)} "
            f"deleted={len(deleted)} failed={len(failed)}",
            file=out,
        )
        if not result.holder_scan_complete:
            print(
                f"  HOLDER SCAN INCOMPLETE — {len(result.unreadable_pids)} unreadable "
                f"/proc entries; nothing selected. Re-run as root.",
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
    if not result.holder_scan_complete:
        return 5
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
