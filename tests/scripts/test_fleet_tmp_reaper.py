"""Tests for the age-based /tmp sweep in ``scripts/fleet_tmp_reaper.py``.

The sweep replaces a name-prefix allowlist with an age rule plus a
board-derived live-workspace exclusion. Two properties are load-bearing and
each is tested in both directions:

* **It must fire on the aged bulk.** ``test_must_fire_*`` replays the 299 real
  ``/tmp`` directory names measured on the fleet host on 2026-08-13 (46.6 GiB
  that the shell reaper skipped) and asserts every non-protected one is
  selected. Any reintroduced name/prefix allowlist fails this test.
* **It must not fire on live work.** Live board workspaces, live task ids in
  *either* spelling, live process references, fresh trees, protected names,
  unpushed git work, an incomplete holder scan, and an unavailable board all
  produce keeps.

Over-correction is a failure mode of equal weight here: a reaper that keeps
everything is as broken as one that deletes live work, so every keep gate has a
paired test proving a genuinely-safe tree is still selected.
"""

import json
import errno
import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "fleet_tmp_reaper" / "aged_bulk_20260813.txt"
)

sys.path.insert(0, str(SCRIPTS_DIR))

import fleet_tmp_reaper as reaper  # noqa: E402
import board_liveness  # noqa: E402

HOUR = 3600
DAY = 24 * HOUR

#: Copied verbatim from godmode-bus/bin/tmp-reaper.sh line 24 (WORKTREE_RE), the
#: prefix allowlist this module replaces. Present only so the tests can show what
#: it did and did not see; the sweep itself never consults it.
LEGACY_WORKTREE_RE = re.compile(
    r"^(cfr-|omnia|permit-|roof-|a6-|auto-|lead-gen|kimi-|sol-|hermes-w|review|"
    r"security-|deep-|f530|rev5|califirst|cursor-|tmp\.)"
)


def load_fixture_names() -> list[str]:
    names = [
        line.strip()
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert names, "fixture is empty — the must-fire test would pass vacuously"
    return names


def make_tree(parent: Path, name: str, *, age_seconds: float, now: float) -> Path:
    """Create ``parent/name`` with a file inside, aged ``age_seconds``."""
    directory = parent / name
    directory.mkdir(parents=True, exist_ok=True)
    payload = directory / "payload.txt"
    payload.write_text("x", encoding="utf-8")
    stamp = now - age_seconds
    os.utime(payload, (stamp, stamp))
    os.utime(directory, (stamp, stamp))
    return directory


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def age_tree(target: Path, *, age_seconds: float, now: float) -> Path:
    """Backdate every path in *target* except ``.git`` (which the sweep prunes).

    Must be called *after* the last mutation: creating a file inside a directory
    refreshes that directory's own mtime, which would otherwise leave the tree
    looking freshly written and mask the gate under test.
    """
    stamp = now - age_seconds
    for path in (*target.rglob("*"), target):
        if ".git" in path.relative_to(target.parent).parts[1:]:
            continue
        os.utime(path, (stamp, stamp), follow_symlinks=False)
    return target


def make_clone(
    parent: Path, name: str, origin: Path, *, age_seconds: float, now: float
) -> Path:
    """A real clone of *origin*, fully pushed and clean, aged ``age_seconds``."""
    target = parent / name
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    return age_tree(target, age_seconds=age_seconds, now=now)


@pytest.fixture
def origin_repo(tmp_path_factory) -> Path:
    """An upstream repo with one commit, used as the clone source."""
    origin = tmp_path_factory.mktemp("origin")
    _git(origin, "init", "--quiet", "--initial-branch=main", ".")
    (origin / "README.md").write_text("upstream\n", encoding="utf-8")
    (origin / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _git(origin, "add", "README.md", ".gitignore")
    _git(origin, "commit", "--quiet", "-m", "initial")
    return origin


@pytest.fixture
def now() -> float:
    return time.time()


# ── must fire: the aged bulk the shell reaper could not see ─────────────────


def test_fixture_documents_the_defect_it_exists_to_catch():
    """The fixture is only meaningful if the legacy gate really missed the bulk."""
    names = load_fixture_names()
    assert len(names) == 299
    matched = [n for n in names if LEGACY_WORKTREE_RE.match(n)]
    assert len(matched) == 20
    # 279/299 = 93.3% invisible to the prefix allowlist.
    assert len(names) - len(matched) == 279


def test_must_fire_selects_every_aged_bulk_directory(tmp_path, now):
    """MUST FIRE: all 280 reclaimable measured directories are candidates.

    This is the regression that matters. Reintroducing a name or prefix
    allowlist in the candidate gate drops this to ~20 and fails here.

    19 of the 299 measured names are protected, so the same run also proves the
    protection holds on real data rather than only on synthetic names. 17 of
    those 19 are the disk-watchdog exclusions this sweep had dropped — most
    importantly ``hermes-results``, the live tool-result store, which the
    unprotected version selected for deletion.
    """
    names = load_fixture_names()
    expected_keeps = {n for n in names if reaper.PROTECTED_NAME_RE.match(n)}
    assert len(expected_keeps) == 19
    assert {"tmux-1000", "org.chromium.Chromium.VJc8dO"} <= expected_keeps
    # The regression that mattered most: the live Hermes tool-result store.
    assert "hermes-results" in expected_keeps
    assert sum(1 for n in expected_keeps if n.startswith("hermes_sandbox_")) == 6
    expected_selected = set(names) - expected_keeps

    root = tmp_path / "tmp"
    root.mkdir()
    for name in names:
        make_tree(root, name, age_seconds=3 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    selected = {d.name for d in result.candidates}
    missing = sorted(expected_selected - selected)
    assert not missing, f"aged bulk skipped ({len(missing)} dirs), e.g. {missing[:10]}"
    assert len(result.candidates) == 280
    assert {d.name for d in result.keeps} == expected_keeps


def test_must_fire_covers_the_directories_the_legacy_gate_could_not_see(tmp_path, now):
    """The names the prefix allowlist could not match must now all be seen."""
    invisible = [
        n
        for n in load_fixture_names()
        if not LEGACY_WORKTREE_RE.match(n) and not reaper.PROTECTED_NAME_RE.match(n)
    ]
    assert len(invisible) == 268
    root = tmp_path / "tmp"
    root.mkdir()
    for name in invisible:
        make_tree(root, name, age_seconds=3 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert {d.name for d in result.candidates} == set(invisible)


def test_candidate_gate_has_no_name_allowlist(tmp_path, now):
    """An arbitrary never-before-seen name is a candidate purely on age."""
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "a-name-nobody-enumerated-9f3c1a", age_seconds=2 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert [d.name for d in result.candidates] == ["a-name-nobody-enumerated-9f3c1a"]
    assert result.candidates[0].reason == reaper.SELECT_AGED


# ── must NOT fire: legitimate live work ─────────────────────────────────────


def test_does_not_fire_on_freshly_written_tree(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "omnia-worktree-busy", age_seconds=30 * 60, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_TOO_NEW


def test_does_not_fire_when_only_a_nested_file_is_fresh(tmp_path, now):
    """Age is the newest mtime anywhere in the tree, not the top directory's."""
    root = tmp_path / "tmp"
    root.mkdir()
    tree = make_tree(root, "deep-worktree", age_seconds=5 * DAY, now=now)
    nested = tree / "src" / "pkg"
    nested.mkdir(parents=True)
    (nested / "live.py").write_text("print(1)\n", encoding="utf-8")

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_TOO_NEW


def test_git_bookkeeping_does_not_keep_a_dead_tree_alive(tmp_path, now, origin_repo):
    """``.git`` is pruned: safety probes touch ``.git/index`` on every pass.

    Uses a real clean clone, so it also proves the unpushed-work guard does not
    over-fire: a clone that is fully pushed is still selected.
    """
    root = tmp_path / "tmp"
    root.mkdir()
    tree = make_clone(root, "wt-pr901", origin_repo, age_seconds=5 * DAY, now=now)
    # Only .git is fresh, exactly as in a worktree a safety probe just touched.
    (tree / ".git" / "index").touch()
    assert tree.stat().st_mtime < now - 4 * DAY

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert [d.name for d in result.candidates] == ["wt-pr901"]
    assert result.candidates[0].reason == reaper.SELECT_AGED


def test_does_not_fire_on_live_board_workspace(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    workspace = make_tree(root, "hermes-run-t_ce22cf43", age_seconds=5 * DAY, now=now)
    dead = make_tree(root, "hermes-run-t_00000001", age_seconds=5 * DAY, now=now)

    tasks = [
        reaper.BoardTask("t_ce22cf43", "running", str(workspace)),
        reaper.BoardTask("t_00000001", "done", str(dead)),
    ]
    result = reaper.sweep(root, now=now, tasks=tasks, process_paths=frozenset())

    assert [d.name for d in result.candidates] == ["hermes-run-t_00000001"]
    kept = {d.name: d.reason for d in result.keeps}
    assert kept["hermes-run-t_ce22cf43"] == reaper.KEEP_LIVE_WORKSPACE


def test_does_not_fire_when_live_workspace_is_nested_inside_the_entry(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    parent = make_tree(root, "seat-scratch", age_seconds=9 * DAY, now=now)
    nested = parent / "workspaces" / "t_abcd1234"
    nested.mkdir(parents=True)

    tasks = [reaper.BoardTask("t_abcd1234", "in_progress", str(nested))]
    result = reaper.sweep(root, now=now, tasks=tasks, process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_LIVE_WORKSPACE


def test_does_not_fire_on_name_carrying_a_live_task_id(tmp_path, now):
    """Real measured name — the board says the task is still live, so keep it."""
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(
        root, "hermes-symptomd-slice2-t_fe15ed02-15658", age_seconds=6 * DAY, now=now
    )

    live = [reaper.BoardTask("t_fe15ed02", "review", workspace_path=None)]
    assert (
        reaper.sweep(root, now=now, tasks=live, process_paths=frozenset()).candidates
        == []
    )

    finished = [reaper.BoardTask("t_fe15ed02", "done", workspace_path=None)]
    assert (
        len(
            reaper.sweep(
                root, now=now, tasks=finished, process_paths=frozenset()
            ).candidates
        )
        == 1
    )


def test_does_not_fire_on_process_referenced_path(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    busy = make_tree(root, "p63-omnia", age_seconds=4 * DAY, now=now)
    idle = make_tree(root, "p64-omnia", age_seconds=4 * DAY, now=now)

    result = reaper.sweep(
        root,
        now=now,
        tasks=[],
        process_paths=frozenset({(busy / "node_modules" / ".bin").resolve()}),
    )

    assert [d.name for d in result.candidates] == ["p64-omnia"]
    assert result.keeps[0].reason == reaper.KEEP_LIVE_PROCESS


def test_process_reference_overlap_is_directional():
    candidate = Path("/tmp/root/job")

    assert reaper._contains_or_equals(candidate, candidate)
    assert reaper._contains_or_equals(candidate, candidate / "checkout")

    # A process outside the candidate is not endangered by deleting it. In
    # particular, common cwd values such as / and /tmp must not pin every
    # candidate beneath them.
    for ancestor in (Path("/"), Path("/tmp"), candidate.parent):
        assert not reaper._contains_or_equals(candidate, ancestor)
    assert not reaper._contains_or_equals(candidate, Path("/tmp/root/job-other"))


def test_ancestor_process_reference_does_not_pin_candidate(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    candidate = make_tree(root, "old-workspace", age_seconds=4 * DAY, now=now)

    result = reaper.sweep(
        root,
        now=now,
        tasks=[],
        process_paths=frozenset({Path("/"), root.resolve()}),
    )

    assert [decision.path for decision in result.candidates] == [candidate]
    assert result.keeps == []


@pytest.mark.parametrize(
    "name",
    [
        ".X11-unix",
        ".ICE-unix",
        ".font-unix",
        "claude-1000",
        "systemd-private-deab4e2f-systemd-logind.service-4qseAV",
        "snap-private-tmp",
        "tmux-1000",
    ],
)
def test_does_not_fire_on_protected_names(tmp_path, now, name):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, name, age_seconds=30 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_PROTECTED


def test_does_not_fire_on_symlinks_or_files(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    target = make_tree(tmp_path / "elsewhere", "real", age_seconds=9 * DAY, now=now)
    link = root / "aged-link"
    link.symlink_to(target)
    stray = root / "aged-file.log"
    stray.write_text("x", encoding="utf-8")
    os.utime(stray, (now - 9 * DAY, now - 9 * DAY))

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert result.candidates == []
    assert {d.reason for d in result.keeps} == {
        reaper.KEEP_SYMLINK,
        reaper.KEEP_NOT_A_DIRECTORY,
    }


def test_unreadable_subtree_is_kept_not_deleted(tmp_path, now, monkeypatch):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "opaque", age_seconds=9 * DAY, now=now)

    result = reaper.sweep(
        root, now=now, tasks=[], process_paths=frozenset(), mtime_probe=lambda _p: None
    )

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_UNREADABLE


def test_owner_mismatch_fails_closed_and_owner_match_selects(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "old-owned-tree", age_seconds=9 * DAY, now=now)

    foreign = reaper.sweep(
        root,
        now=now,
        tasks=[],
        process_paths=frozenset(),
        owner_uid=123,
        ownership_probe=lambda _path, _uid: False,
    )
    owned = reaper.sweep(
        root,
        now=now,
        tasks=[],
        process_paths=frozenset(),
        owner_uid=123,
        ownership_probe=lambda _path, _uid: True,
    )

    assert foreign.keeps[0].reason == reaper.KEEP_FOREIGN_OWNER
    assert [item.name for item in owned.candidates] == ["old-owned-tree"]


def test_board_unavailable_selects_nothing(tmp_path, now):
    """Fail closed: without the board we cannot prove a workspace is dead."""
    root = tmp_path / "tmp"
    root.mkdir()
    for name in ("hermes-mp13-work", "t138-p51-site"):
        make_tree(root, name, age_seconds=30 * DAY, now=now)

    result = reaper.sweep(
        root, now=now, tasks=[], board_available=False, process_paths=frozenset()
    )

    assert result.candidates == []
    assert {d.reason for d in result.keeps} == {reaper.KEEP_BOARD_UNAVAILABLE}


# ── fix 1: underscore-less task ids ─────────────────────────────────────────

#: The three real workspaces the unfixed sweep selected on blitz-vps on
#: 2026-08-13. The first two belong to non-terminal cards; the third was
#: declared off limits by the operator. Reproduced here by name because the
#: originals were destroyed before this fix landed.
LIVE_WORKSPACE_NAMES = {
    "wave-d4-item2-t7e073169": "t_7e073169",
    "cursor-verify-t19ba5cba": "t_19ba5cba",
    "hermes-tce22-fd-reaper-main": "t_ce22cf43",
}


@pytest.mark.parametrize(("name", "task_id"), sorted(LIVE_WORKSPACE_NAMES.items()))
def test_underscore_less_workspace_name_resolves_to_its_card(name, task_id):
    """``t7e073169`` in a directory name is card ``t_7e073169``.

    Requiring the underscore made these names carry *no* id at all, so the
    board gate never ran on them and they fell through to selection.
    ``hermes-tce22-fd-reaper-main`` additionally *truncates* the id to
    ``tce22``, so resolution is by prefix, not equality.
    """
    assert board_liveness.name_is_held(name, frozenset({task_id}), board_ok=True)


def test_truncated_id_needs_prefix_resolution_not_equality():
    """The 8-hex rule alone cannot see the truncated third workspace."""
    name = "hermes-tce22-fd-reaper-main"
    assert reaper.names_task_ids(name) == {"t_ce22"}
    assert board_liveness.name_is_held(name, frozenset({"t_ce22cf43"}), board_ok=True)
    # ...and it must not match an unrelated card that merely shares no prefix.
    assert not board_liveness.name_is_held(
        name, frozenset({"t_ffffffff"}), board_ok=True
    )


def test_normalise_task_id_folds_both_spellings():
    assert board_liveness.hex_of_task_id("t7e073169") == "7e073169"
    assert board_liveness.hex_of_task_id("t_7e073169") == "7e073169"
    assert board_liveness.hex_of_task_id("T_7E073169") == "7e073169"


def test_task_marker_policy_is_the_shared_board_liveness_oracle(
    monkeypatch, tmp_path, now
):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "scratch-t_deadbeef", age_seconds=3 * DAY, now=now)
    calls = []

    def marker(name, live_hexes, *, board_ok):
        calls.append((name, live_hexes, board_ok))
        return True

    monkeypatch.setattr(board_liveness, "name_is_held", marker)
    result = reaper.sweep(
        root,
        now=now,
        tasks=[reaper.BoardTask("t_deadbeef", "ready")],
        process_paths=frozenset(),
    )

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_LIVE_TASK_ID
    assert calls == [("scratch-t_deadbeef", frozenset({"deadbeef"}), True)]


@pytest.mark.parametrize(("name", "task_id"), sorted(LIVE_WORKSPACE_NAMES.items()))
def test_does_not_fire_on_underscore_less_live_card_workspace(
    tmp_path, now, name, task_id
):
    """ACCEPTANCE: each of the three real workspaces is a keep while its card lives."""
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, name, age_seconds=30 * HOUR, now=now)

    live = [reaper.BoardTask(task_id, "ready", workspace_path=None)]
    result = reaper.sweep(root, now=now, tasks=live, process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_LIVE_TASK_ID


def test_underscore_less_id_still_selects_when_the_card_is_terminal(tmp_path, now):
    """Paired direction: the fold must not keep everything with a t-prefix."""
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "wave-d4-item2-t7e073169", age_seconds=30 * HOUR, now=now)

    finished = [reaper.BoardTask("t_7e073169", "done", workspace_path=None)]
    result = reaper.sweep(root, now=now, tasks=finished, process_paths=frozenset())

    assert [d.name for d in result.candidates] == ["wave-d4-item2-t7e073169"]


# ── fix 2: restored disk-watchdog exclusions ────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "hermes-results",
        "hermes_sandbox_0t1iovbo",
        "kanban-boardd-run",
        "tsx-0",
        "corepack-bin",
        "bin-node",
        "node-v22.14.0-linux-x64",
        "tmp.w53s1-omnia",
    ],
)
def test_restored_watchdog_exclusions_are_protected(tmp_path, now, name):
    """These are the exclusions the shipped disk-watchdog already encoded.

    ``hermes-results`` is the load-bearing one: agents read those payloads back
    by path long after the last write, so age says nothing about whether it is
    in use.
    """
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, name, age_seconds=30 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_PROTECTED


def test_protected_prefixes_do_not_swallow_unrelated_names(tmp_path, now):
    """Paired direction: near-miss names are still selected."""
    root = tmp_path / "tmp"
    root.mkdir()
    for name in (
        "binary-analysis",
        "nodemon-cache",
        "tmpfiles-report",
        "hermes-result-viewer",
    ):
        make_tree(root, name, age_seconds=3 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert len(result.candidates) == 4


# ── fix 3: unpushed-work guard ──────────────────────────────────────────────


def test_keeps_a_clone_with_a_tracked_modification(tmp_path, now, origin_repo):
    root = tmp_path / "tmp"
    root.mkdir()
    tree = make_clone(root, "dirty-clone", origin_repo, age_seconds=5 * DAY, now=now)
    (tree / "README.md").write_text("edited, never committed\n", encoding="utf-8")
    age_tree(tree, age_seconds=5 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_UNPUSHED_WORK


def test_keeps_a_clone_with_a_commit_on_no_remote(tmp_path, now, origin_repo):
    root = tmp_path / "tmp"
    root.mkdir()
    tree = make_clone(root, "unpushed-clone", origin_repo, age_seconds=5 * DAY, now=now)
    (tree / "new.py").write_text(
        "print('work that exists nowhere else')\n", encoding="utf-8"
    )
    _git(tree, "add", "new.py")
    _git(tree, "commit", "--quiet", "-m", "local only")
    age_tree(tree, age_seconds=5 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_UNPUSHED_WORK


def test_keeps_a_clone_with_a_stash(tmp_path, now, origin_repo):
    root = tmp_path / "tmp"
    root.mkdir()
    tree = make_clone(root, "stashed-clone", origin_repo, age_seconds=5 * DAY, now=now)
    (tree / "README.md").write_text("stash me\n", encoding="utf-8")
    _git(tree, "stash", "push", "--quiet", "-m", "wip")
    age_tree(tree, age_seconds=5 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_UNPUSHED_WORK


def test_untracked_build_output_alone_does_not_keep_a_clone(tmp_path, now, origin_repo):
    """Paired direction: node_modules is not work. Over-keeping is a failure."""
    root = tmp_path / "tmp"
    root.mkdir()
    tree = make_clone(root, "built-clone", origin_repo, age_seconds=5 * DAY, now=now)
    build = tree / "node_modules" / "pkg"
    build.mkdir(parents=True)
    (build / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    age_tree(tree, age_seconds=5 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert [d.name for d in result.candidates] == ["built-clone"]


def test_nonignored_untracked_source_is_unique_work(tmp_path, now, origin_repo):
    root = tmp_path / "tmp"
    root.mkdir()
    tree = make_clone(
        root, "untracked-source", origin_repo, age_seconds=5 * DAY, now=now
    )
    (tree / "new-source.py").write_text("print('unique')\n", encoding="utf-8")
    age_tree(tree, age_seconds=5 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_UNPUSHED_WORK


def test_git_probe_failure_keeps_while_non_git_tree_selects(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "git-unknown", age_seconds=5 * DAY, now=now)

    failed = reaper.sweep(
        root,
        now=now,
        tasks=[],
        process_paths=frozenset(),
        git_probe=lambda _path: reaper.KEEP_GIT_PROBE_FAILED,
    )
    clean = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert failed.keeps[0].reason == reaper.KEEP_GIT_PROBE_FAILED
    assert [item.name for item in clean.candidates] == ["git-unknown"]


def test_nested_dirty_repository_keeps_parent(tmp_path, now, origin_repo):
    root = tmp_path / "tmp"
    root.mkdir()
    parent = make_tree(root, "container", age_seconds=5 * DAY, now=now)
    nested = make_clone(parent, "nested", origin_repo, age_seconds=5 * DAY, now=now)
    (nested / "README.md").write_text("unique\n", encoding="utf-8")
    age_tree(parent, age_seconds=5 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_UNPUSHED_WORK


def test_linked_worktree_metadata_is_never_raw_deleted(tmp_path, now, origin_repo):
    root = tmp_path / "tmp"
    root.mkdir()
    linked = root / "linked-worktree"
    _git(origin_repo, "worktree", "add", "--detach", str(linked))
    age_tree(linked, age_seconds=5 * DAY, now=now)

    result = reaper.sweep(
        root,
        now=now,
        tasks=[],
        process_paths=frozenset(),
        mtime_probe=lambda _path: now - 5 * DAY,
    )

    assert result.candidates == []
    assert result.keeps[0].reason == reaper.KEEP_LINKED_GIT_WORKTREE


def test_non_git_tree_is_not_gated_by_the_git_probe(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "plain-scratch", age_seconds=5 * DAY, now=now)

    assert reaper.has_unpushed_work(root / "plain-scratch") is False


def test_unreadable_git_tree_fails_closed():
    """A probe that cannot answer keeps the tree."""
    assert reaper.has_unpushed_work(Path("/nonexistent/x"), runner=lambda *_: None)

    def broken(path, args):
        return None

    tree = Path(__file__).resolve().parents[2]  # a real repo, so `.git` exists
    assert reaper.has_unpushed_work(tree, runner=broken) is True


# ── fix 4: the holder scan must be complete ─────────────────────────────────


def test_unreadable_proc_entry_fails_the_sweep_closed(tmp_path, now):
    """An unreadable /proc/<pid> is an unknown holder, never an absent one."""
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "hermes-mp13-work", age_seconds=9 * DAY, now=now)

    import unittest.mock as mock

    incomplete = reaper.HolderScan(frozenset(), (4242,))
    with mock.patch.object(reaper, "complete_holder_scan", return_value=incomplete):
        result = reaper.sweep(root, now=now, tasks=[], process_paths=None)

    assert result.holder_scan_complete is False
    assert result.unreadable_pids == (4242,)
    assert result.candidates == []
    assert {d.reason for d in result.keeps} == {reaper.KEEP_HOLDER_SCAN_INCOMPLETE}


def test_complete_scan_still_selects(tmp_path, now):
    """Paired direction: a complete scan with no holders selects normally."""
    import unittest.mock as mock

    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "hermes-mp13-work", age_seconds=9 * DAY, now=now)

    with mock.patch.object(
        reaper, "complete_holder_scan", return_value=reaper.HolderScan(frozenset(), ())
    ):
        result = reaper.sweep(root, now=now, tasks=[], process_paths=None)

    assert [d.name for d in result.candidates] == ["hermes-mp13-work"]


def test_permission_error_is_unreadable_but_exit_is_not(tmp_path):
    """ESRCH/ENOENT (process exited) must not be mistaken for "may not look"."""
    assert reaper._is_permission_error(PermissionError(13, "denied")) is True
    assert reaper._is_permission_error(FileNotFoundError(2, "gone")) is False
    assert (
        reaper._is_permission_error(ProcessLookupError(3, "no such process")) is False
    )


def test_marker_non_disappearance_proc_error_must_make_scan_incomplete(
    tmp_path, monkeypatch
):
    """MUST FIRE: EIO is unknown custody; ENOENT is the disappearance ablation."""
    proc_root = tmp_path / "proc"
    pid = proc_root / "4242"
    (pid / "fd").mkdir(parents=True)
    original_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == pid / "cmdline":
            raise OSError(errno.EIO, "marker: unreadable live process")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert reaper.process_referenced_paths(proc_root).unreadable_pids == (4242,)

    # Ablation: the same missing proc channels are proof of process exit, not
    # an unknown holder, so the otherwise identical scan remains complete.
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: (_ for _ in ()).throw(FileNotFoundError(errno.ENOENT, "gone")),
    )
    assert reaper.process_referenced_paths(proc_root).complete


def test_holder_scan_reads_cwd_argv_and_open_descriptors(tmp_path):
    """Synthetic /proc proves every holder channel, including an actual fd."""
    held = tmp_path / "held"
    held.mkdir()
    cwd_target = held / "cwd"
    cwd_target.mkdir()
    argv_target = held / "argv"
    argv_target.mkdir()
    fd_target = held / "output.log"
    fd_target.write_text("live\n", encoding="utf-8")
    pid = tmp_path / "proc" / "4242"
    (pid / "fd").mkdir(parents=True)
    (pid / "cwd").symlink_to(cwd_target)
    (pid / "cmdline").write_bytes(
        b"worker\0--workspace=" + os.fsencode(argv_target) + b"\0"
    )
    (pid / "fd" / "7").symlink_to(fd_target)

    scan = reaper.process_referenced_paths(tmp_path / "proc")

    assert scan.complete
    assert {
        cwd_target.resolve(),
        argv_target.resolve(),
        fd_target.resolve(),
    } <= scan.paths


def test_privileged_scan_returns_none_when_sudo_is_unavailable():
    def failing(cmd):
        raise FileNotFoundError("sudo")

    assert reaper.privileged_holder_scan(runner=failing) is None


def test_privileged_scan_parses_the_emitted_payload():
    class Done:
        returncode = 0
        stdout = json.dumps({"paths": ["/tmp/held"], "unreadable_pids": []})

    scan = reaper.privileged_holder_scan(runner=lambda _cmd: Done())

    assert scan is not None
    assert scan.paths == frozenset({Path("/tmp/held")})
    assert scan.complete is True


def test_missing_geteuid_keeps_incomplete_holder_sweep_closed(
    tmp_path, now, monkeypatch
):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "old-workspace", age_seconds=9 * DAY, now=now)
    incomplete = reaper.HolderScan(frozenset(), (4242,))

    monkeypatch.delattr(reaper.os, "geteuid", raising=False)
    monkeypatch.setattr(reaper, "process_referenced_paths", lambda: incomplete)
    monkeypatch.setattr(reaper, "privileged_holder_scan", lambda: None)

    assert reaper.effective_uid() is None
    result = reaper.sweep(root, now=now, tasks=[], process_paths=None)

    assert result.candidates == []
    assert result.holder_scan_complete is False
    assert result.keeps[0].reason == reaper.KEEP_HOLDER_SCAN_INCOMPLETE


def test_cli_exits_5_when_holder_scan_incomplete(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "wt-pr901", age_seconds=9 * DAY, now=now)
    deleted: list[Path] = []

    code = reaper.main(
        ["--root", str(root), "--apply"],
        tasks_loader=lambda: [],
        holder_scanner=lambda: reaper.HolderScan(frozenset(), (7,)),
        deleter=lambda decision: deleted.append(decision.path),
        stdout=open(os.devnull, "w", encoding="utf-8"),  # noqa: SIM115
    )

    assert code == 5
    assert deleted == []


# ── age probe ───────────────────────────────────────────────────────────────


def test_newest_mtime_reports_newest_entry_in_tree(tmp_path, now):
    tree = make_tree(tmp_path, "t", age_seconds=10 * DAY, now=now)
    fresh = tree / "a" / "b.txt"
    fresh.parent.mkdir()
    fresh.write_text("x", encoding="utf-8")
    for target in (fresh, fresh.parent, tree):
        os.utime(target, (now - 10 * DAY, now - 10 * DAY))
    os.utime(fresh, (now - HOUR, now - HOUR))

    assert reaper.newest_mtime(tree) == pytest.approx(now - HOUR, abs=2)


def test_newest_mtime_returns_none_for_missing_path(tmp_path):
    assert reaper.newest_mtime(tmp_path / "nope") is None


# ── CLI: dry run is the default ─────────────────────────────────────────────


def _cli(tmp_path, argv, tasks, deleted):
    return reaper.main(
        [*argv],
        tasks_loader=lambda: tasks,
        holder_scanner=lambda: reaper.HolderScan(frozenset(), ()),
        deleter=lambda decision: deleted.append(decision.path),
        stdout=open(os.devnull, "w", encoding="utf-8"),  # noqa: SIM115
    )


def test_cli_defaults_to_dry_run_and_deletes_nothing(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "hermes-mp13-work", age_seconds=9 * DAY, now=now)
    deleted: list[Path] = []

    code = _cli(tmp_path, ["--root", str(root)], [], deleted)

    assert code == 0
    assert deleted == []
    assert (root / "hermes-mp13-work").is_dir()


def test_cli_apply_deletes_exactly_the_candidates(tmp_path, now, capsys):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "hermes-mp13-work", age_seconds=9 * DAY, now=now)
    make_tree(root, "fresh-build", age_seconds=5 * 60, now=now)
    deleted: list[Path] = []

    code = _cli(tmp_path, ["--root", str(root), "--apply"], [], deleted)

    assert code == 0
    assert [p.name for p in deleted] == ["hermes-mp13-work"]


def test_cli_json_report_lists_candidates_without_deleting(tmp_path, now, capsys):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "wt-pr901", age_seconds=9 * DAY, now=now)
    deleted: list[Path] = []

    code = reaper.main(
        ["--root", str(root), "--json"],
        tasks_loader=lambda: [],
        holder_scanner=lambda: reaper.HolderScan(frozenset(), ()),
        deleter=lambda decision: deleted.append(decision.path),
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["mode"] == "dry-run"
    assert payload["board_available"] is True
    assert [Path(c["path"]).name for c in payload["candidates"]] == ["wt-pr901"]
    assert payload["deleted"] == []
    assert deleted == []


def test_cli_exits_3_when_board_unavailable(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    make_tree(root, "wt-pr901", age_seconds=9 * DAY, now=now)
    deleted: list[Path] = []

    code = _cli(tmp_path, ["--root", str(root), "--apply"], None, deleted)

    assert code == 3
    assert deleted == []


def test_cli_exits_4_and_continues_when_a_delete_fails(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    for name in ("a-aged", "b-aged"):
        make_tree(root, name, age_seconds=9 * DAY, now=now)
    seen: list[Path] = []

    def flaky(decision: reaper.Decision) -> None:
        seen.append(decision.path)
        if decision.path.name == "a-aged":
            raise PermissionError("nope")

    code = reaper.main(
        ["--root", str(root), "--apply"],
        tasks_loader=lambda: [],
        holder_scanner=lambda: reaper.HolderScan(frozenset(), ()),
        deleter=flaky,
        stdout=open(os.devnull, "w", encoding="utf-8"),  # noqa: SIM115
    )

    assert code == 4
    assert [p.name for p in seen] == ["a-aged", "b-aged"]


def test_apply_reloads_board_and_refuses_newly_live_task_marker(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    target = make_tree(root, "scratch-t_deadbeef", age_seconds=9 * DAY, now=now)
    loads = iter([[], [reaper.BoardTask("t_deadbeef", "ready")]])
    deleted = []

    code = reaper.main(
        ["--root", str(root), "--apply"],
        tasks_loader=lambda: next(loads),
        holder_scanner=lambda: reaper.HolderScan(frozenset(), ()),
        deleter=lambda decision: deleted.append(decision.path),
        stdout=open(os.devnull, "w", encoding="utf-8"),  # noqa: SIM115
    )

    assert code == 4
    assert deleted == []
    assert target.is_dir()


def test_apply_rescans_processes_and_refuses_new_holder(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    target = make_tree(root, "scratch", age_seconds=9 * DAY, now=now)
    scans = iter([
        reaper.HolderScan(frozenset(), ()),
        reaper.HolderScan(frozenset({target.resolve()}), ()),
    ])
    deleted = []

    code = reaper.main(
        ["--root", str(root), "--apply"],
        tasks_loader=lambda: [],
        holder_scanner=lambda: next(scans),
        deleter=lambda decision: deleted.append(decision.path),
        stdout=open(os.devnull, "w", encoding="utf-8"),  # noqa: SIM115
    )

    assert code == 4
    assert deleted == []
    assert target.is_dir()


def test_apply_rechecks_git_and_refuses_new_unique_work(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    target = make_tree(root, "scratch", age_seconds=9 * DAY, now=now)
    probes = iter([None, reaper.KEEP_UNPUSHED_WORK])
    deleted = []

    code = reaper.main(
        ["--root", str(root), "--apply"],
        tasks_loader=lambda: [],
        holder_scanner=lambda: reaper.HolderScan(frozenset(), ()),
        git_probe=lambda _path: next(probes),
        deleter=lambda decision: deleted.append(decision.path),
        stdout=open(os.devnull, "w", encoding="utf-8"),  # noqa: SIM115
    )

    assert code == 4
    assert deleted == []
    assert target.is_dir()


def test_marker_board_probe_rejects_zero_malformed_and_truncated_rows(monkeypatch):
    """MUST FIRE: the same-object count control rejects false-empty board reads."""
    from hermes_cli import kb_client

    class FakeClient:
        def __init__(self, rows):
            self.rows = rows
            self.sql = ""
            self.max_rows = 0

        def query(self, sql, _params, *, max_rows):
            self.sql = sql
            self.max_rows = max_rows
            return self.rows

    invalid_rows = [
        [],
        [{"id": "", "status": "ready", "workspace_path": None, "_total": 1}],
        [{"id": "t_deadbeef", "status": "", "workspace_path": None, "_total": 1}],
        [
            {
                "id": "t_deadbeef",
                "status": "ready",
                "workspace_path": 42,
                "_total": 1,
            }
        ],
        [
            {
                "id": "t_deadbeef",
                "status": "ready",
                "workspace_path": None,
                "_total": 2,
            }
        ],
    ]
    for rows in invalid_rows:
        client = FakeClient(rows)
        monkeypatch.setattr(kb_client, "get_client", lambda client=client: client)
        assert reaper.load_board_tasks() is None
        assert "COUNT(*) OVER()" in client.sql
        assert client.max_rows >= 500_000

    # Ablation/positive control: one well-formed row whose window count agrees
    # is accepted, proving the marker does not collapse all reads to unknown.
    valid = FakeClient([
        {
            "id": "t_deadbeef",
            "status": "ready",
            "workspace_path": None,
            "_total": 1,
        }
    ])
    monkeypatch.setattr(kb_client, "get_client", lambda: valid)
    assert reaper.load_board_tasks() == [reaper.BoardTask("t_deadbeef", "ready", None)]


def test_marker_filesystem_boundaries_must_fail_closed(tmp_path, monkeypatch):
    """MUST FIRE: cross-device and same-device bind-mount markers both keep."""
    tree = tmp_path / "candidate"
    tree.mkdir()
    nested = tree / "nested"
    nested.mkdir()
    payload = tree / "payload"
    payload.write_text("marker", encoding="utf-8")
    observed_tree = tree.lstat()
    observed_nested = nested.lstat()

    assert (
        reaper.tree_boundary_keep_reason(
            tree,
            observed_tree.st_dev,
            mount_identities=frozenset({
                (observed_nested.st_dev, observed_nested.st_ino)
            }),
        )
        == reaper.KEEP_MOUNT_BOUNDARY
    )

    original_lstat = Path.lstat

    def cross_device_lstat(path: Path):
        value = original_lstat(path)
        if path == nested:
            return SimpleNamespace(
                st_dev=value.st_dev + 1,
                st_ino=value.st_ino,
                st_mode=value.st_mode,
                st_nlink=value.st_nlink,
            )
        return value

    monkeypatch.setattr(Path, "lstat", cross_device_lstat)
    assert (
        reaper.tree_boundary_keep_reason(
            tree, observed_tree.st_dev, mount_identities=frozenset()
        )
        == reaper.KEEP_CROSS_DEVICE
    )
    monkeypatch.setattr(Path, "lstat", original_lstat)

    # Ablation: removing both markers makes the same tree traversable.
    assert (
        reaper.tree_boundary_keep_reason(
            tree, observed_tree.st_dev, mount_identities=frozenset()
        )
        is None
    )


def test_marker_board_liveness_is_rechecked_after_quarantine(tmp_path, now):
    """MUST FIRE: a card becoming live after namespace move restores the tree."""
    root = tmp_path / "tmp"
    root.mkdir()
    target = make_tree(root, "scratch-t_deadbeef", age_seconds=9 * DAY, now=now)
    tasks: list[reaper.BoardTask] = []

    def make_live(_quarantined: Path) -> None:
        tasks.append(reaper.BoardTask("t_deadbeef", "ready"))

    code = reaper.main(
        ["--root", str(root), "--apply"],
        tasks_loader=lambda: list(tasks),
        holder_scanner=lambda: reaper.HolderScan(frozenset(), ()),
        git_probe=lambda _path: None,
        after_quarantine=make_live,
        stdout=io.StringIO(),
    )

    assert code == 4
    assert (target / "payload.txt").is_file()
    assert list(root.glob(".fleet-reaper-*")) == []


def test_marker_process_custody_is_rechecked_after_quarantine(tmp_path, now):
    """MUST FIRE: a holder appearing after namespace move restores the tree."""
    root = tmp_path / "tmp"
    root.mkdir()
    target = make_tree(root, "scratch", age_seconds=9 * DAY, now=now)
    holder = {"path": None}

    def acquire(quarantined: Path) -> None:
        holder["path"] = quarantined.resolve()

    def scan() -> reaper.HolderScan:
        paths = frozenset() if holder["path"] is None else frozenset({holder["path"]})
        return reaper.HolderScan(paths, ())

    code = reaper.main(
        ["--root", str(root), "--apply"],
        tasks_loader=lambda: [],
        holder_scanner=scan,
        git_probe=lambda _path: None,
        after_quarantine=acquire,
        stdout=io.StringIO(),
    )

    assert code == 4
    assert (target / "payload.txt").is_file()
    assert list(root.glob(".fleet-reaper-*")) == []


def test_marker_quarantine_revalidation_ablation_deletes_still_dead_tree(tmp_path, now):
    """Ablation: unchanged board and custody inputs still reach fd deletion."""
    root = tmp_path / "tmp"
    root.mkdir()
    target = make_tree(root, "scratch", age_seconds=9 * DAY, now=now)

    code = reaper.main(
        ["--root", str(root), "--apply"],
        tasks_loader=lambda: [],
        holder_scanner=lambda: reaper.HolderScan(frozenset(), ()),
        git_probe=lambda _path: None,
        stdout=io.StringIO(),
    )

    assert code == 0
    assert not target.exists()
    assert list(root.glob(".fleet-reaper-*")) == []


def test_verified_delete_rejects_symlink_swap_and_preserves_target(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    candidate = make_tree(root, "candidate", age_seconds=9 * DAY, now=now)
    outside = make_tree(tmp_path, "outside", age_seconds=9 * DAY, now=now)
    decision = reaper.evaluate_entry(
        candidate, now=now, process_paths=frozenset(), owner_uid=reaper.effective_uid()
    )
    candidate.rename(root / "original")
    candidate.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="no longer a directory|identity changed"):
        reaper.delete_verified(decision, root)

    assert outside.is_dir()
    assert (outside / "payload.txt").is_file()
    assert (root / "original").is_dir()


def test_verified_delete_rejects_directory_inode_swap(tmp_path, now):
    root = tmp_path / "tmp"
    root.mkdir()
    candidate = make_tree(root, "candidate", age_seconds=9 * DAY, now=now)
    decision = reaper.evaluate_entry(
        candidate, now=now, process_paths=frozenset(), owner_uid=reaper.effective_uid()
    )
    candidate.rename(root / "original")
    replacement = make_tree(root, "candidate", age_seconds=9 * DAY, now=now)

    with pytest.raises(RuntimeError, match="identity changed"):
        reaper.delete_verified(decision, root)

    assert replacement.is_dir()
    assert (root / "original").is_dir()


def test_verified_delete_rejects_exact_open_to_namespace_move_swap(tmp_path, now):
    """MUST FIRE: replacing the public name after verified open deletes nothing."""
    root = tmp_path / "tmp"
    root.mkdir()
    candidate = make_tree(root, "candidate", age_seconds=9 * DAY, now=now)
    decision = reaper.evaluate_entry(
        candidate, now=now, process_paths=frozenset(), owner_uid=reaper.effective_uid()
    )

    def swap_after_open() -> None:
        candidate.rename(root / "original-preserved")
        make_tree(root, "candidate", age_seconds=9 * DAY, now=now)

    with pytest.raises(RuntimeError, match="identity changed before quarantine"):
        reaper.delete_verified(decision, root, before_namespace_move=swap_after_open)

    assert (root / "original-preserved" / "payload.txt").is_file()
    assert (root / "candidate" / "payload.txt").is_file()
    assert list(root.glob(".fleet-reaper-*")) == []


def test_marker_post_check_directory_substitution_never_deletes_replacement(
    tmp_path, now
):
    """MUST FIRE: replacing the quarantine name cannot redirect recursion."""
    root = tmp_path / "tmp"
    root.mkdir()
    candidate = make_tree(root, "candidate", age_seconds=9 * DAY, now=now)
    decision = reaper.evaluate_entry(
        candidate, now=now, process_paths=frozenset(), owner_uid=reaper.effective_uid()
    )
    replacement: dict[str, Path] = {}

    def substitute(quarantined: Path) -> None:
        quarantined.rename(root / "selected-moved")
        replacement["path"] = make_tree(
            root, quarantined.name, age_seconds=9 * DAY, now=now
        )
        (replacement["path"] / "replacement.marker").write_text(
            "must survive", encoding="utf-8"
        )

    reaper.delete_verified(decision, root, before_fd_delete=substitute)

    assert not (root / "selected-moved").exists()
    assert (replacement["path"] / "replacement.marker").read_text(
        encoding="utf-8"
    ) == "must survive"


def test_marker_namespace_restore_uses_distinct_name_when_public_name_reappears(
    tmp_path, now
):
    """MUST FIRE: failed revalidation restores custody without overwriting."""
    root = tmp_path / "tmp"
    root.mkdir()
    candidate = make_tree(root, "candidate", age_seconds=9 * DAY, now=now)
    decision = reaper.evaluate_entry(
        candidate, now=now, process_paths=frozenset(), owner_uid=reaper.effective_uid()
    )

    def occupy_public_name(_quarantined: Path) -> None:
        replacement = root / "candidate"
        replacement.mkdir()
        (replacement / "replacement.marker").write_text("new", encoding="utf-8")

    def marker_failure(_quarantined: Path, _candidate_fd: int) -> None:
        raise RuntimeError("marker: final custody unknown")

    with pytest.raises(RuntimeError, match="selected entry restored to"):
        reaper.delete_verified(
            decision,
            root,
            after_namespace_move=occupy_public_name,
            post_quarantine_check=marker_failure,
        )

    assert (root / "candidate" / "replacement.marker").read_text(
        encoding="utf-8"
    ) == "new"
    restored = list(root.glob("candidate.fleet-reaper-restored-*"))
    assert len(restored) == 1
    assert (restored[0] / "payload.txt").is_file()
    assert list(root.glob(".fleet-reaper-*")) == []

    # Ablation: without a public-name collision, restoration returns to the
    # original name and still leaves no hidden quarantine namespace.
    restored[0].rename(root / "candidate-ablation")
    ablation = root / "ablation"
    ablation.mkdir()
    (ablation / "payload.txt").write_text("x", encoding="utf-8")
    ablation_decision = reaper.evaluate_entry(
        ablation,
        now=now,
        process_paths=frozenset(),
        mtime_probe=lambda _path: now - 9 * DAY,
        owner_uid=reaper.effective_uid(),
    )
    with pytest.raises(RuntimeError, match="restored to ablation"):
        reaper.delete_verified(
            ablation_decision, root, post_quarantine_check=marker_failure
        )
    assert (root / "ablation" / "payload.txt").is_file()


def test_repo_root_is_put_on_sys_path_for_the_board_import():
    """Without this the board import fails and the sweep silently no-ops.

    ``python3 scripts/fleet_tmp_reaper.py`` puts ``scripts/`` on sys.path, not
    the repo root. ``hermes_cli`` then fails to import, the board reads as
    unavailable, the fail-closed gate keeps everything, and the sweep reports
    zero candidates for a reason that has nothing to do with disk state.
    """
    entries = ["/some/other/place"]
    reaper.ensure_repo_on_path(entries)
    assert entries[0] == str(REPO_ROOT)
    assert (REPO_ROOT / "hermes_cli" / "kb_client.py").is_file()

    # idempotent
    reaper.ensure_repo_on_path(entries)
    assert entries.count(str(REPO_ROOT)) == 1


def test_default_deleter_removes_the_tree(tmp_path, now):
    """The real deleter is wired up — proven on a pytest-owned tempdir only."""
    tree = make_tree(tmp_path, "disposable", age_seconds=9 * DAY, now=now)
    decision = reaper.evaluate_entry(
        tree, now=now, process_paths=frozenset(), owner_uid=reaper.effective_uid()
    )
    reaper._default_deleter(decision)
    assert not tree.exists()
