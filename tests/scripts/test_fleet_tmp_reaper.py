"""Tests for the age-based /tmp sweep in ``scripts/fleet_tmp_reaper.py``.

The sweep replaces a name-prefix allowlist with an age rule plus a
board-derived live-workspace exclusion. Two properties are load-bearing and
each is tested in both directions:

* **It must fire on the aged bulk.** ``test_must_fire_*`` replays the 299 real
  ``/tmp`` directory names measured on the fleet host on 2026-08-13 (46.6 GiB
  that the shell reaper skipped) and asserts every one is selected. Any
  reintroduced name/prefix allowlist fails this test.
* **It must not fire on live work.** Live board workspaces, live task ids, live
  process references, fresh trees, protected names, and an unavailable board
  all produce keeps.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fleet_tmp_reaper" / "aged_bulk_20260813.txt"

sys.path.insert(0, str(SCRIPTS_DIR))

import fleet_tmp_reaper as reaper  # noqa: E402

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
    """MUST FIRE: all 297 reclaimable measured directories are candidates.

    This is the regression that matters. Reintroducing a name or prefix
    allowlist in the candidate gate drops this to ~20 and fails here.

    Two of the 299 measured names (tmux-1000, org.chromium.Chromium.VJc8dO) are
    protected, so the same run also proves the protection still holds on real
    data rather than only on synthetic names.
    """
    names = load_fixture_names()
    expected_keeps = {n for n in names if reaper.PROTECTED_NAME_RE.match(n)}
    assert expected_keeps == {"tmux-1000", "org.chromium.Chromium.VJc8dO"}
    expected_selected = set(names) - expected_keeps

    root = tmp_path / "tmp"
    root.mkdir()
    for name in names:
        make_tree(root, name, age_seconds=3 * DAY, now=now)

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    selected = {d.name for d in result.candidates}
    missing = sorted(expected_selected - selected)
    assert not missing, f"aged bulk skipped ({len(missing)} dirs), e.g. {missing[:10]}"
    assert len(result.candidates) == 297
    assert {d.name for d in result.keeps} == expected_keeps


def test_must_fire_covers_the_directories_the_legacy_gate_could_not_see(tmp_path, now):
    """The names the prefix allowlist could not match must now all be seen."""
    invisible = [
        n
        for n in load_fixture_names()
        if not LEGACY_WORKTREE_RE.match(n) and not reaper.PROTECTED_NAME_RE.match(n)
    ]
    assert len(invisible) == 277
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


def test_git_bookkeeping_does_not_keep_a_dead_tree_alive(tmp_path, now):
    """``.git`` is pruned: safety probes touch ``.git/index`` on every pass."""
    root = tmp_path / "tmp"
    root.mkdir()
    tree = make_tree(root, "wt-pr901", age_seconds=5 * DAY, now=now)
    git_dir = tree / ".git"
    git_dir.mkdir()
    (git_dir / "index").write_text("touched-by-the-reaper-itself", encoding="utf-8")
    stale = now - 5 * DAY
    os.utime(tree, (stale, stale))  # only .git is fresh, as in a probed worktree

    result = reaper.sweep(root, now=now, tasks=[], process_paths=frozenset())

    assert [d.name for d in result.candidates] == ["wt-pr901"]


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
    make_tree(root, "hermes-symptomd-slice2-t_fe15ed02-15658", age_seconds=6 * DAY, now=now)

    live = [reaper.BoardTask("t_fe15ed02", "review", workspace_path=None)]
    assert reaper.sweep(root, now=now, tasks=live, process_paths=frozenset()).candidates == []

    finished = [reaper.BoardTask("t_fe15ed02", "done", workspace_path=None)]
    assert len(reaper.sweep(root, now=now, tasks=finished, process_paths=frozenset()).candidates) == 1


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
        deleter=deleted.append,
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
        deleter=deleted.append,
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

    def flaky(path: Path) -> None:
        seen.append(path)
        if path.name == "a-aged":
            raise PermissionError("nope")

    code = reaper.main(
        ["--root", str(root), "--apply"],
        tasks_loader=lambda: [],
        deleter=flaky,
        stdout=open(os.devnull, "w", encoding="utf-8"),  # noqa: SIM115
    )

    assert code == 4
    assert [p.name for p in seen] == ["a-aged", "b-aged"]


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
    reaper._default_deleter(tree)
    assert not tree.exists()
