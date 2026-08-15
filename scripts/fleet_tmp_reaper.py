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
   kept, as is anything whose name carries a non-terminal task id. Token
   extraction, prefix matching, and status policy come from the shared
   ``board_liveness`` oracle; this reaper does not maintain a second matcher.
3. **Live-process exclusion.** Anything referenced by a running process's cwd,
   argv, or open file descriptors is kept. The scan must be **complete**: an
   unreadable ``/proc/<pid>`` is an unknown holder, not an absent one, so it
   fails the whole sweep closed rather than silently reading as "nobody has
   this open". ACP-bridge and ``claude -p`` grandchildren outlive their worker,
   reparent to ``systemd --user`` and keep writing their workspace for days, so
   a directory with no live *worker* can still have a live *writer*.
4. **Unique-work exclusion.** A git tree with modified or non-ignored untracked
   files, a commit absent from live remote refs, a stash, a nested repository,
   or linked-worktree metadata is held. A clean pushed clone is re-clonable;
   unique work and linked worktree administration are not.
5. **Ownership and apply-time identity.** Every path in a candidate tree must
   belong to the invoking uid. ``--apply`` reloads the board and process scan,
   reruns every predicate, and verifies the original device/inode/uid/mode
   before the fd-anchored delete. A symlink or directory swap becomes an error,
   never a different deletion target.

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
import errno
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import board_liveness

# ── policy constants ────────────────────────────────────────────────────────

DEFAULT_ROOT = Path("/tmp")

#: A tree must be untouched this long before it is even considered.
DEFAULT_MAX_AGE_SECONDS = 12 * 3600

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

#: Compatibility export for callers that report task-shaped names. Matching
#: policy lives in ``board_liveness`` and must not be copied here.
TASK_ID_RE = board_liveness.TASK_TOKEN_RE

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
KEEP_LINKED_GIT_WORKTREE = "linked-git-worktree"
KEEP_GIT_PROBE_FAILED = "git-probe-failed-closed"
KEEP_HOLDER_SCAN_INCOMPLETE = "holder-scan-incomplete-fail-closed"
KEEP_FOREIGN_OWNER = "owner-mismatch-fail-closed"
KEEP_CROSS_DEVICE = "cross-device-entry-fail-closed"
KEEP_MOUNT_BOUNDARY = "mount-boundary-fail-closed"

SELECT_AGED = "aged-and-unreferenced"


# ── data model ──────────────────────────────────────────────────────────────


@dataclass
class BoardTask:
    """One row of the fleet board, reduced to what the sweep needs."""

    task_id: str
    status: str
    workspace_path: str | None = None

    @property
    def is_live(self) -> bool:
        return board_liveness.is_live_status(self.status)


@dataclass(frozen=True)
class EntryIdentity:
    """Filesystem identity frozen when an entry becomes a candidate."""

    device: int
    inode: int
    uid: int
    mode: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "EntryIdentity":
        return cls(value.st_dev, value.st_ino, value.st_uid, value.st_mode)


@dataclass(frozen=True)
class Decision:
    """The verdict for one top-level entry."""

    path: Path
    selected: bool
    reason: str
    age_seconds: float | None = None
    identity: EntryIdentity | None = None

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


def live_task_ids(tasks: Iterable[BoardTask]) -> frozenset[str]:
    """Live task hexes derived by the shared board-liveness oracle."""
    return board_liveness.live_hexes(tasks)


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
    """Canonical ``t_<hex>`` tokens reported by the shared oracle."""
    return frozenset(f"t_{token}" for token in board_liveness.extract_hex_tokens(name))


def _is_permission_error(exc: OSError) -> bool:
    """True for "you may not look", false for "it is not there any more".

    ``ESRCH``/``ENOENT`` mean the process exited between ``iterdir`` and the
    read — a genuinely absent holder, safe to skip. ``EACCES``/``EPERM`` mean a
    process we are not allowed to inspect, whose holdings are unknown.
    """
    return isinstance(exc, PermissionError)


def _process_read_is_unknown(exc: OSError) -> bool:
    """True unless *exc* proves the process/path disappeared.

    ``ENOENT`` and ``ESRCH`` are the only negative controls that establish a
    process exited during the scan.  ``EIO``, ``ENOTDIR`` and every other
    failure leave its holders unknown and therefore make the scan incomplete.
    """
    return exc.errno not in {errno.ENOENT, errno.ESRCH}


def _record_process_path(out: set[Path], raw: str | os.PathLike[str]) -> None:
    """Add a canonical absolute process reference when one can be established."""
    value = os.fspath(raw)
    if value.endswith(" (deleted)"):
        value = value[: -len(" (deleted)")]
    if not value.startswith(os.sep):
        return
    resolved = _resolve(value)
    if resolved is not None:
        out.add(resolved)


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
        incomplete = False

        try:
            _record_process_path(out, os.readlink(entry / "cwd"))
        except OSError as exc:
            incomplete = incomplete or _process_read_is_unknown(exc)

        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError as exc:
            incomplete = incomplete or _process_read_is_unknown(exc)
            raw = b""
        for raw_arg in raw.split(b"\0"):
            try:
                arg = os.fsdecode(raw_arg)
            except (ValueError, UnicodeDecodeError):
                continue
            # Accept both a plain absolute argv element and common
            # ``--workspace=/absolute/path`` spellings.
            _record_process_path(out, arg.split("=", 1)[-1])

        # Open descriptors catch the writer that neither cwd nor argv names —
        # a detached grandchild streaming into a workspace it never chdir'd to.
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError as exc:
            incomplete = incomplete or _process_read_is_unknown(exc)
            fds = []
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError as exc:
                incomplete = incomplete or _process_read_is_unknown(exc)
                continue
            _record_process_path(out, target)

        if incomplete:
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
    get_euid = getattr(os, "geteuid", None)
    running_as_root = bool(callable(get_euid) and get_euid() == 0)
    if scan.complete or not escalate or running_as_root:
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
        "-c",
        # Scope the ownership exception to this exact candidate. A wildcard
        # would trust every repository on the host.
        f"safe.directory={path.resolve(strict=False)}",
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


def _discover_git_roots(path: Path) -> tuple[Path, ...] | None:
    """Return every repository rooted inside *path*; ``None`` means unreadable.

    Arbitrary scratch roots can contain nested clones. Checking only
    ``path/.git`` would delete unique work in a nested repository.
    """
    roots: list[Path] = []

    def fail(exc: OSError) -> None:
        raise exc

    try:
        for current, directories, files in os.walk(
            path, topdown=True, followlinks=False, onerror=fail
        ):
            if ".git" not in directories and ".git" not in files:
                continue
            roots.append(Path(current))
            if ".git" in directories:
                directories.remove(".git")
    except OSError:
        return None
    return tuple(roots)


def _single_git_keep_reason(
    path: Path, runner: Callable[[Path, Sequence[str]], str | None] = _run_git
) -> str | None:
    """Return a keep reason for one git worktree, or ``None`` when re-clonable."""
    marker = path / ".git"
    try:
        marker_stat = marker.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return KEEP_GIT_PROBE_FAILED

    # Linked worktrees and submodules use a .git file whose external
    # administrative state raw rmtree cannot clean up safely. Symlinked or
    # special markers are untrusted and also fail closed.
    if stat.S_ISREG(marker_stat.st_mode):
        return KEEP_LINKED_GIT_WORKTREE
    if not stat.S_ISDIR(marker_stat.st_mode):
        return KEEP_GIT_PROBE_FAILED

    status = runner(path, ["status", "--porcelain=v1", "--untracked-files=normal"])
    if status is None:
        return KEEP_GIT_PROBE_FAILED
    if status.strip():
        return KEEP_UNPUSHED_WORK

    unique = runner(
        path,
        ["rev-list", "--max-count=1", "HEAD", "--all", "--not", "--remotes"],
    )
    if unique is None:
        return KEEP_GIT_PROBE_FAILED
    if unique.strip():
        return KEEP_UNPUSHED_WORK

    stashes = runner(path, ["stash", "list"])
    if stashes is None:
        return KEEP_GIT_PROBE_FAILED
    if stashes.strip():
        return KEEP_UNPUSHED_WORK

    head = runner(path, ["rev-parse", "HEAD"])
    remotes = runner(path, ["remote"])
    if head is None or remotes is None:
        return KEEP_GIT_PROBE_FAILED
    head = head.strip()
    remote_names = [name for name in remotes.splitlines() if name.strip()]
    if not head or not remote_names:
        return KEEP_UNPUSHED_WORK

    reachable = False
    for remote in remote_names:
        advertised = runner(path, ["ls-remote", "--heads", "--tags", remote])
        if advertised is None:
            continue
        advertised_heads = {
            fields[0]
            for line in advertised.splitlines()
            if (fields := line.split(maxsplit=1))
        }
        if head in advertised_heads:
            reachable = True
            break
    return None if reachable else KEEP_UNPUSHED_WORK


def git_tree_keep_reason(
    path: Path, runner: Callable[[Path, Sequence[str]], str | None] = _run_git
) -> str | None:
    """Return why a top-level tree's git state must be kept, if any.

    Any one of these is enough to keep the tree:

    * ``git status --porcelain`` reports a tracked or non-ignored untracked
      change. Ignored build output remains reclaimable.
    * any ref or detached HEAD commit is absent from remote-tracking refs;
    * ``git stash list`` is non-empty.
    * the exact HEAD is not advertised by any reachable live remote.
    * the tree is a linked worktree/submodule or any git probe is unreadable.

    Every nested repository is checked. A probe that cannot answer returns a
    keep reason. The asymmetry is the point: a
    clean clone is re-clonable from origin and costs a re-clone if we are wrong;
    a dirty one is unrecoverable and costs the work.
    """
    roots = _discover_git_roots(path)
    if roots is None:
        return KEEP_GIT_PROBE_FAILED
    for root in roots:
        reason = _single_git_keep_reason(root, runner)
        if reason is not None:
            return reason
    return None


def has_unpushed_work(
    path: Path, runner: Callable[[Path, Sequence[str]], str | None] = _run_git
) -> bool:
    """Compatibility predicate backed by :func:`git_tree_keep_reason`."""
    return git_tree_keep_reason(path, runner) is not None


# ── ownership probe ─────────────────────────────────────────────────────────


def effective_uid() -> int | None:
    """Current effective uid, or ``None`` on platforms without POSIX uids."""
    getter = getattr(os, "geteuid", None)
    return int(getter()) if callable(getter) else None


def tree_owned_by(path: Path, uid: int) -> bool | None:
    """Whether every entry in *path* belongs to *uid*; ``None`` if unreadable."""

    def fail(exc: OSError) -> None:
        raise exc

    try:
        if path.lstat().st_uid != uid:
            return False
        for current, directories, files in os.walk(
            path, topdown=True, followlinks=False, onerror=fail
        ):
            current_path = Path(current)
            for name in (*directories, *files):
                if (current_path / name).lstat().st_uid != uid:
                    return False
    except OSError:
        return None
    return True


def _decode_mountinfo_path(value: str) -> str:
    """Decode the octal escapes used for mountinfo path fields."""
    return (
        value
        .replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _mount_identities(
    mountinfo: Path = Path("/proc/self/mountinfo"),
    *,
    scope: Path | None = None,
) -> frozenset[tuple[int, int]] | None:
    """Return ``(st_dev, st_ino)`` for mount roots at or below *scope*.

    Device equality alone does not detect a same-filesystem bind mount.  The
    mounted root identity does, so every destructive traversal checks both.
    An unreadable relevant mount is an unknown boundary and fails closed.
    Unrelated mounts are deliberately not statted: this host has privileged
    debug mounts that the fleet user cannot inspect, but they say nothing about
    whether a candidate under ``/tmp`` crosses a mount boundary.
    """
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    resolved_scope = _resolve(scope) if scope is not None else None
    if scope is not None and resolved_scope is None:
        return None
    identities: set[tuple[int, int]] = set()
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            return None
        mount_path = Path(_decode_mountinfo_path(fields[4]))
        if resolved_scope is not None and not (
            mount_path == resolved_scope or resolved_scope in mount_path.parents
        ):
            continue
        try:
            observed = mount_path.stat()
        except OSError:
            # Mounts may disappear concurrently. Unknown is safer than treating
            # the vanished row as proof no boundary exists.
            return None
        identities.add((observed.st_dev, observed.st_ino))
    return frozenset(identities)


def tree_boundary_keep_reason(
    path: Path,
    expected_device: int,
    *,
    mount_identities: frozenset[tuple[int, int]] | None = None,
) -> str | None:
    """Return a fail-closed filesystem-boundary reason for *path*.

    The traversal never follows symlinks.  Every real entry must remain on the
    candidate filesystem, no directory may be a mount root (including bind
    mounts on the same device).
    """
    mounts = (
        _mount_identities(scope=path) if mount_identities is None else mount_identities
    )
    if mounts is None:
        return KEEP_MOUNT_BOUNDARY

    def inspect(observed: os.stat_result) -> str | None:
        if observed.st_dev != expected_device:
            return KEEP_CROSS_DEVICE
        if (observed.st_dev, observed.st_ino) in mounts:
            return KEEP_MOUNT_BOUNDARY
        return None

    def fail(exc: OSError) -> None:
        raise exc

    try:
        reason = inspect(path.lstat())
        if reason is not None:
            return reason
        for current, directories, files in os.walk(
            path, topdown=True, followlinks=False, onerror=fail
        ):
            current_path = Path(current)
            for name in (*directories, *files):
                reason = inspect((current_path / name).lstat())
                if reason is not None:
                    return reason
    except OSError:
        return KEEP_UNREADABLE
    return None


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
    reference_entry: Path | None = None,
    now: float,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    live_workspaces: frozenset[Path] = frozenset(),
    live_ids: frozenset[str] = frozenset(),
    process_paths: frozenset[Path] = frozenset(),
    mtime_probe: Callable[[Path], float | None] = newest_mtime,
    git_probe: Callable[[Path], str | None] = git_tree_keep_reason,
    owner_uid: int | None = None,
    ownership_probe: Callable[[Path, int], bool | None] = tree_owned_by,
    boundary_probe: Callable[[Path, int], str | None] = tree_boundary_keep_reason,
) -> Decision:
    """Decide a single top-level entry.

    There is deliberately **no name allowlist**. Candidacy is age plus the
    absence of a live reference plus the absence of unpushed work; names only
    ever produce *keeps*.
    """
    logical_entry = reference_entry or entry
    try:
        entry_stat = entry.lstat()
    except OSError:
        return Decision(entry, False, KEEP_UNREADABLE)
    if stat.S_ISLNK(entry_stat.st_mode):
        return Decision(entry, False, KEEP_SYMLINK)
    if not stat.S_ISDIR(entry_stat.st_mode):
        return Decision(entry, False, KEEP_NOT_A_DIRECTORY)
    if PROTECTED_NAME_RE.match(logical_entry.name):
        return Decision(entry, False, KEEP_PROTECTED)

    owner_uid = effective_uid() if owner_uid is None else owner_uid
    if owner_uid is not None:
        owned = ownership_probe(entry, owner_uid)
        if owned is None:
            return Decision(entry, False, KEEP_UNREADABLE)
        if not owned:
            return Decision(entry, False, KEEP_FOREIGN_OWNER)

    boundary_reason = boundary_probe(entry, entry_stat.st_dev)
    if boundary_reason is not None:
        return Decision(entry, False, boundary_reason)

    resolved = _resolve(entry)
    logical_resolved = _resolve(logical_entry)
    if resolved is None or logical_resolved is None:
        return Decision(entry, False, KEEP_UNREADABLE)

    for workspace in live_workspaces:
        if _paths_overlap(logical_resolved, workspace):
            return Decision(entry, False, KEEP_LIVE_WORKSPACE)

    if board_liveness.name_is_held(logical_entry.name, live_ids, board_ok=True):
        return Decision(entry, False, KEEP_LIVE_TASK_ID)

    newest = mtime_probe(entry)
    if newest is None:
        return Decision(entry, False, KEEP_UNREADABLE)
    age = now - newest
    if age < max_age_seconds:
        return Decision(entry, False, KEEP_TOO_NEW, age_seconds=age)

    for referenced in process_paths:
        if _contains_or_equals(resolved, referenced) or _contains_or_equals(
            logical_resolved, referenced
        ):
            return Decision(entry, False, KEEP_LIVE_PROCESS, age_seconds=age)

    # Last, because it is the only gate that forks a subprocess: by here the
    # entry is aged, unreferenced and off the board, so this runs on a small set.
    git_reason = git_probe(entry)
    if git_reason is not None:
        return Decision(entry, False, git_reason, age_seconds=age)

    return Decision(
        entry,
        True,
        SELECT_AGED,
        age_seconds=age,
        identity=EntryIdentity.from_stat(entry_stat),
    )


def sweep(
    root: Path = DEFAULT_ROOT,
    *,
    now: float | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    tasks: Sequence[BoardTask] | None = None,
    board_available: bool = True,
    process_paths: frozenset[Path] | None = None,
    holder_scan: HolderScan | None = None,
    mtime_probe: Callable[[Path], float | None] = newest_mtime,
    git_probe: Callable[[Path], str | None] = git_tree_keep_reason,
    owner_uid: int | None = None,
    ownership_probe: Callable[[Path, int], bool | None] = tree_owned_by,
    boundary_probe: Callable[[Path, int], str | None] = tree_boundary_keep_reason,
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

    if holder_scan is not None:
        scan = holder_scan
    elif process_paths is None:
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

    # Mount enumeration is a point-in-time safety input, not static policy.
    # Load it once per sweep so a 300-entry candidate set does not restat the
    # entire mount table 300 times. A later apply revalidation loads it again.
    effective_boundary_probe = boundary_probe
    if boundary_probe is tree_boundary_keep_reason:
        mount_identities = _mount_identities(scope=root)
        if mount_identities is None:
            result.decisions = [
                Decision(e, False, KEEP_MOUNT_BOUNDARY) for e in entries
            ]
            return result

        def effective_boundary_probe(path: Path, device: int) -> str | None:
            return tree_boundary_keep_reason(
                path, device, mount_identities=mount_identities
            )

    result.decisions = [
        evaluate_entry(
            entry,
            now=now,
            max_age_seconds=max_age_seconds,
            live_workspaces=workspaces,
            live_ids=ids,
            process_paths=scan.paths,
            mtime_probe=mtime_probe,
            git_probe=git_probe,
            owner_uid=owner_uid,
            ownership_probe=ownership_probe,
            boundary_probe=effective_boundary_probe,
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
            "SELECT id, status, workspace_path, COUNT(*) OVER() AS _total FROM tasks",
            [],
            max_rows=500000,
        )
    except Exception:
        return None
    # A destructive consumer must not interpret a zero-row response, a broker
    # pointed at the wrong board, or max_rows truncation as "there is no live
    # work". The window count is a positive control from the same query/object.
    if not rows:
        return None
    try:
        totals = {int(row["_total"]) for row in rows}
    except (KeyError, TypeError, ValueError):
        return None
    if totals != {len(rows)}:
        return None

    tasks: list[BoardTask] = []
    for row in rows:
        try:
            task_id = str(row["id"] or "").strip()
            status_value = str(row["status"] or "").strip()
            workspace_value = row["workspace_path"]
        except (KeyError, TypeError):
            return None
        body = board_liveness.hex_of_task_id(task_id)
        if (
            len(body) < 4
            or any(char not in "0123456789abcdef" for char in body.lower())
            or not status_value
            or workspace_value is not None
            and not isinstance(workspace_value, str)
        ):
            return None
        tasks.append(
            BoardTask(
                task_id=task_id,
                status=status_value,
                workspace_path=workspace_value or None,
            )
        )
    return tasks


# ── CLI ─────────────────────────────────────────────────────────────────────


def _identity_matches(expected: EntryIdentity, observed: os.stat_result) -> bool:
    return expected == EntryIdentity.from_stat(observed)


def _restore_quarantined_name(
    root_fd: int,
    quarantined_name: str,
    original_name: str,
) -> str:
    """Restore a quarantined entry without overwriting a new public name."""
    try:
        os.stat(original_name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        destination = original_name
    else:
        destination = f"{original_name}.fleet-reaper-restored-{secrets.token_hex(16)}"
    os.rename(
        quarantined_name,
        destination,
        src_dir_fd=root_fd,
        dst_dir_fd=root_fd,
    )
    return destination


def _find_identity_name(root_fd: int, expected: EntryIdentity) -> str | None:
    """Find the direct child still naming *expected* under an open root."""
    for name in os.listdir(root_fd):
        try:
            observed = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if _identity_matches(expected, observed):
            return name
    return None


def _validate_delete_stat(
    observed: os.stat_result,
    *,
    expected_device: int,
    expected_uid: int,
    mount_identities: frozenset[tuple[int, int]],
) -> None:
    if observed.st_uid != expected_uid:
        raise RuntimeError(KEEP_FOREIGN_OWNER)
    if observed.st_dev != expected_device:
        raise RuntimeError(KEEP_CROSS_DEVICE)
    if (observed.st_dev, observed.st_ino) in mount_identities:
        raise RuntimeError(KEEP_MOUNT_BOUNDARY)


def _delete_tree_contents_fd(
    directory_fd: int,
    *,
    expected_device: int,
    expected_uid: int,
    mount_identities: frozenset[tuple[int, int]],
    mutated: list[bool],
) -> None:
    """Recursively delete through already-open directory descriptors.

    No recursive step resolves a pathname outside its verified parent fd. A
    substituted directory name is detected before ``rmdir`` and is never
    traversed or recursively deleted.
    """
    for name in sorted(os.listdir(directory_fd)):
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_delete_stat(
            observed,
            expected_device=expected_device,
            expected_uid=expected_uid,
            mount_identities=mount_identities,
        )
        if stat.S_ISDIR(observed.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            child_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if not os.path.samestat(observed, opened):
                    raise RuntimeError("directory identity changed during delete")
                _validate_delete_stat(
                    opened,
                    expected_device=expected_device,
                    expected_uid=expected_uid,
                    mount_identities=mount_identities,
                )
                _delete_tree_contents_fd(
                    child_fd,
                    expected_device=expected_device,
                    expected_uid=expected_uid,
                    mount_identities=mount_identities,
                    mutated=mutated,
                )
            finally:
                os.close(child_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not os.path.samestat(observed, current):
                raise RuntimeError("directory name changed during delete")
            os.rmdir(name, dir_fd=directory_fd)
            mutated[0] = True
            continue

        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not os.path.samestat(observed, current):
            raise RuntimeError("entry name changed during delete")
        os.unlink(name, dir_fd=directory_fd)
        mutated[0] = True


def delete_verified(
    decision: Decision,
    root: Path,
    *,
    before_namespace_move: Callable[[], None] | None = None,
    after_namespace_move: Callable[[Path], None] | None = None,
    post_quarantine_check: Callable[[Path, int], None] | None = None,
    before_fd_delete: Callable[[Path], None] | None = None,
) -> None:
    """Delete only the selection-time directory identity, anchored to *root*.

    Open and verify the candidate first, then move it to an unpredictable name
    under the already-open parent. The moved name must still identify the open
    inode before deletion. A same-name replacement in the open-to-rename gap is
    restored and kept, while a new public-name entry created after quarantine
    is outside the deletion namespace.
    """
    if not decision.selected or decision.identity is None:
        raise ValueError("refusing to delete an unselected or unidentified entry")
    if decision.path.parent.resolve(strict=True) != root.resolve(strict=True):
        raise ValueError("candidate is not a direct child of the configured root")
    if os.sep in decision.path.name or decision.path.name in {"", ".", ".."}:
        raise ValueError("invalid candidate basename")
    open_flags = os.O_RDONLY
    open_flags |= getattr(os, "O_DIRECTORY", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, open_flags)
    candidate_fd = -1
    quarantine_name = f".fleet-reaper-{decision.identity.inode}-{secrets.token_hex(16)}"
    quarantined = False
    mutated = [False]
    try:
        try:
            candidate_fd = os.open(decision.path.name, open_flags, dir_fd=root_fd)
        except OSError as exc:
            raise RuntimeError("candidate is no longer a directory") from exc
        observed = os.fstat(candidate_fd)
        if not stat.S_ISDIR(observed.st_mode):
            raise RuntimeError("candidate is no longer a directory")
        if not _identity_matches(decision.identity, observed):
            raise RuntimeError("candidate identity changed after selection")
        mounts = _mount_identities(scope=decision.path)
        if mounts is None:
            raise RuntimeError(KEEP_MOUNT_BOUNDARY)
        if before_namespace_move is not None:
            before_namespace_move()

        os.rename(
            decision.path.name,
            quarantine_name,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        moved = os.stat(quarantine_name, dir_fd=root_fd, follow_symlinks=False)
        if not _identity_matches(decision.identity, moved):
            restored = _restore_quarantined_name(
                root_fd, quarantine_name, decision.path.name
            )
            raise RuntimeError(
                "candidate identity changed before quarantine; "
                f"moved entry restored to {restored}"
            )
        quarantined = True
        quarantine_path = root / quarantine_name
        try:
            if after_namespace_move is not None:
                after_namespace_move(quarantine_path)
            if post_quarantine_check is not None:
                post_quarantine_check(quarantine_path, candidate_fd)
            if before_fd_delete is not None:
                before_fd_delete(quarantine_path)

            opened = os.fstat(candidate_fd)
            _validate_delete_stat(
                opened,
                expected_device=decision.identity.device,
                expected_uid=decision.identity.uid,
                mount_identities=mounts,
            )
            _delete_tree_contents_fd(
                candidate_fd,
                expected_device=decision.identity.device,
                expected_uid=decision.identity.uid,
                mount_identities=mounts,
                mutated=mutated,
            )

            # The selected inode may have been renamed again after quarantine.
            # Remove only the direct child still naming that exact, now-empty
            # inode. A replacement at the secret quarantine name is untouched.
            selected_name = _find_identity_name(root_fd, decision.identity)
            if selected_name is not None:
                current = os.stat(selected_name, dir_fd=root_fd, follow_symlinks=False)
                if not _identity_matches(decision.identity, current):
                    raise RuntimeError("selected directory name changed at removal")
                os.rmdir(selected_name, dir_fd=root_fd)
                mutated[0] = True
            quarantined = False
        except Exception as exc:
            if quarantined and not mutated[0]:
                selected_name = _find_identity_name(root_fd, decision.identity)
                if selected_name is not None:
                    restored = _restore_quarantined_name(
                        root_fd, selected_name, decision.path.name
                    )
                    raise RuntimeError(
                        f"{exc}; selected entry restored to {restored}"
                    ) from exc
            raise
    finally:
        if candidate_fd >= 0:
            os.close(candidate_fd)
        os.close(root_fd)


def _default_deleter(decision: Decision) -> None:
    delete_verified(decision, decision.path.parent)


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
    deleter: Callable[[Decision], None] | None = None,
    holder_scanner: Callable[[], HolderScan] | None = None,
    mtime_probe: Callable[[Path], float | None] = newest_mtime,
    git_probe: Callable[[Path], str | None] = git_tree_keep_reason,
    owner_uid: int | None = None,
    ownership_probe: Callable[[Path, int], bool | None] = tree_owned_by,
    boundary_probe: Callable[[Path, int], str | None] = tree_boundary_keep_reason,
    after_quarantine: Callable[[Path], None] | None = None,
    before_fd_delete: Callable[[Path], None] | None = None,
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

    scan_holders = holder_scanner or (
        lambda: complete_holder_scan(escalate=not args.no_escalate_holder_scan)
    )
    tasks = tasks_loader()
    initial_scan = scan_holders()
    result = sweep(
        args.root,
        max_age_seconds=int(args.max_age_hours * 3600),
        tasks=tasks or [],
        board_available=tasks is not None,
        holder_scan=initial_scan,
        mtime_probe=mtime_probe,
        git_probe=git_probe,
        owner_uid=owner_uid,
        ownership_probe=ownership_probe,
        boundary_probe=boundary_probe,
    )

    deleted: list[str] = []
    failed: list[str] = []
    if args.apply:
        for decision in result.candidates:
            try:
                if deleter is None:
                    expected_identity = decision.identity
                    if expected_identity is None:
                        raise RuntimeError("selected candidate has no identity")

                    def post_quarantine_check(
                        quarantine_path: Path, candidate_fd: int
                    ) -> None:
                        fresh_tasks = tasks_loader()
                        if fresh_tasks is None:
                            raise RuntimeError(KEEP_BOARD_UNAVAILABLE)
                        fresh_scan = scan_holders()
                        if not fresh_scan.complete:
                            raise RuntimeError(KEEP_HOLDER_SCAN_INCOMPLETE)
                        fresh = evaluate_entry(
                            quarantine_path,
                            reference_entry=decision.path,
                            now=time.time(),
                            max_age_seconds=int(args.max_age_hours * 3600),
                            live_workspaces=live_workspace_paths(fresh_tasks),
                            live_ids=live_task_ids(fresh_tasks),
                            process_paths=fresh_scan.paths,
                            mtime_probe=mtime_probe,
                            git_probe=git_probe,
                            owner_uid=owner_uid,
                            ownership_probe=ownership_probe,
                            boundary_probe=boundary_probe,
                        )
                        if not fresh.selected:
                            raise RuntimeError(
                                f"apply revalidation kept entry: {fresh.reason}"
                            )
                        if fresh.identity != expected_identity or not _identity_matches(
                            expected_identity, os.fstat(candidate_fd)
                        ):
                            raise RuntimeError(
                                "candidate identity changed after selection"
                            )

                    delete_verified(
                        decision,
                        args.root,
                        after_namespace_move=after_quarantine,
                        post_quarantine_check=post_quarantine_check,
                        before_fd_delete=before_fd_delete,
                    )
                else:
                    # Injected deleters are a unit-test seam. Keep the old
                    # pre-call revalidation contract for those non-destructive
                    # callers; the production deleter always uses the stronger
                    # quarantine-first path above.
                    fresh_tasks = tasks_loader()
                    if fresh_tasks is None:
                        raise RuntimeError(KEEP_BOARD_UNAVAILABLE)
                    fresh_scan = scan_holders()
                    if not fresh_scan.complete:
                        raise RuntimeError(KEEP_HOLDER_SCAN_INCOMPLETE)
                    fresh = evaluate_entry(
                        decision.path,
                        now=time.time(),
                        max_age_seconds=int(args.max_age_hours * 3600),
                        live_workspaces=live_workspace_paths(fresh_tasks),
                        live_ids=live_task_ids(fresh_tasks),
                        process_paths=fresh_scan.paths,
                        mtime_probe=mtime_probe,
                        git_probe=git_probe,
                        owner_uid=owner_uid,
                        ownership_probe=ownership_probe,
                        boundary_probe=boundary_probe,
                    )
                    if not fresh.selected:
                        raise RuntimeError(
                            f"apply revalidation kept entry: {fresh.reason}"
                        )
                    if fresh.identity != decision.identity:
                        raise RuntimeError("candidate identity changed after selection")
                    deleter(fresh)
                deleted.append(str(decision.path))
            except Exception as exc:  # noqa: BLE001 — one failure must not abort the sweep
                failed.append(f"{decision.path}: {type(exc).__name__}: {exc}")

    mode = "apply" if args.apply else "dry-run"
    if args.json:
        payload: dict[str, object] = {
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
