from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "tmp_reaper.py"
)
SPEC = importlib.util.spec_from_file_location("tmp_reaper", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
reaper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reaper
SPEC.loader.exec_module(reaper)


class FakeBoardClient:
    def __init__(
        self,
        workspaces: list[tuple[str, Path]],
        *,
        total_tasks: int = 1,
    ):
        self.workspaces = workspaces
        self.total_tasks = total_tasks
        self.calls: list[str] = []

    def query(
        self,
        sql: str,
        params: list[Any] | None = None,
        max_rows: int | None = None,
    ) -> list[dict[str, Any]]:
        del params, max_rows
        self.calls.append(sql)
        if "COUNT(*)" in sql:
            return [
                {
                    "total_tasks": self.total_tasks,
                    "running_tasks": len(self.workspaces),
                    "live_workspaces": len(self.workspaces),
                }
            ]
        return [
            {"id": task_id, "workspace_path": str(workspace)}
            for task_id, workspace in self.workspaces
        ]


class UnavailableBoardClient:
    def query(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        raise ConnectionError("broker offline: secret detail must not be emitted")


class StaticBoardClient:
    def __init__(self, summary: Any):
        self.summary = summary

    def query(self, sql: str, **kwargs: Any) -> Any:
        del kwargs
        if "COUNT(*)" in sql:
            return self.summary
        raise AssertionError("malformed summaries must stop before row discovery")


class InconsistentBoardClient:
    def query(
        self,
        sql: str,
        params: list[Any] | None = None,
        max_rows: int | None = None,
    ) -> list[dict[str, Any]]:
        del params, max_rows
        if "COUNT(*)" in sql:
            return [{"total_tasks": 1, "running_tasks": 1, "live_workspaces": 1}]
        return []


class SnapshotBoardClient(FakeBoardClient):
    """Return one authoritative snapshot, then a changed recheck snapshot."""

    def __init__(self, snapshots: list[list[tuple[str, Path]]]):
        super().__init__(snapshots[0])
        self.snapshots = snapshots
        self.snapshot_index = 0

    def query(
        self,
        sql: str,
        params: list[Any] | None = None,
        max_rows: int | None = None,
    ) -> list[dict[str, Any]]:
        self.workspaces = self.snapshots[self.snapshot_index]
        rows = super().query(sql, params=params, max_rows=max_rows)
        if "COUNT(*)" not in sql and self.snapshot_index < len(self.snapshots) - 1:
            self.snapshot_index += 1
        return rows


def make_proc_root(tmp_path: Path, cwds: list[tuple[int, Path]]) -> Path:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    for pid, cwd in cwds:
        pid_dir = proc_root / str(pid)
        pid_dir.mkdir()
        (pid_dir / "cwd").symlink_to(cwd, target_is_directory=True)
    return proc_root


def mark_old(path: Path, *, seconds: int = 7200) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old), follow_symlinks=False)


def candidate(receipt: dict[str, Any], name: str) -> dict[str, Any]:
    return next(row for row in receipt["candidates"] if row["name"] == name)


def test_apply_must_fire_keeps_live_and_deletes_dead(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    live = root / "live"
    live_cwd = live / "checkout"
    dead = root / "dead"
    live_cwd.mkdir(parents=True)
    dead.mkdir()
    (dead / "proof.txt").write_text("delete me", encoding="utf-8")
    mark_old(live)
    mark_old(dead)

    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        cwd=live_cwd,
        stdin=subprocess.PIPE,
    )
    try:
        assert process.poll() is None
        assert Path(os.readlink(f"/proc/{process.pid}/cwd")).resolve() == live_cwd.resolve()
        proc_root = make_proc_root(tmp_path, [(process.pid, live_cwd)])
        client = FakeBoardClient([("t_live", live)])

        exit_code, receipt = reaper.run_reaper(
            root,
            apply=True,
            retention_seconds=3600,
            board_client=client,
            proc_root=proc_root,
        )
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=10)

    live_row = candidate(receipt, "live")
    dead_row = candidate(receipt, "dead")
    assert exit_code == 0
    assert receipt["status"] == "ok"
    assert live.is_dir(), "LIVE candidate with two independent references must survive"
    assert not dead.exists(), "DEAD candidate must be physically deleted"
    assert live_row["decision"] == "kept"
    assert "running_workspace_overlap" in live_row["reason_codes"]
    assert "process_cwd_overlap" in live_row["reason_codes"]
    assert dead_row["decision"] == "deleted"
    assert dead_row["reason_codes"] == ["retention_elapsed", "deleted"]
    assert len(client.calls) == 4, "board and proc safety must be rechecked before delete"
    print(
        json.dumps(
            {
                "dead": {
                    "decision": dead_row["decision"],
                    "exists": dead.exists(),
                    "reason_codes": dead_row["reason_codes"],
                },
                "live": {
                    "decision": live_row["decision"],
                    "exists": live.exists(),
                    "reason_codes": live_row["reason_codes"],
                },
            },
            sort_keys=True,
        )
    )


def test_dry_run_is_default_and_retention_remains_a_gate(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    old = root / "old"
    fresh = root / "fresh"
    old.mkdir(parents=True)
    fresh.mkdir()
    mark_old(old)
    proc_root = make_proc_root(tmp_path, [(100, tmp_path / "unrelated")])
    client = FakeBoardClient([])

    exit_code, receipt = reaper.run_reaper(
        root,
        retention_seconds=3600,
        board_client=client,
        proc_root=proc_root,
    )

    assert exit_code == 0
    assert old.is_dir() and fresh.is_dir()
    assert candidate(receipt, "old")["decision"] == "would_delete"
    assert candidate(receipt, "fresh")["decision"] == "kept"
    assert candidate(receipt, "fresh")["reason_codes"] == ["retention_active"]


@pytest.mark.parametrize(
    ("client", "error_code"),
    [
        (UnavailableBoardClient(), "board_unavailable"),
        (StaticBoardClient([]), "board_summary_malformed"),
        (
            StaticBoardClient(
                [
                    {
                        "total_tasks": "many",
                        "running_tasks": 0,
                        "live_workspaces": 0,
                    }
                ]
            ),
            "board_summary_malformed",
        ),
        (
            StaticBoardClient(
                [{"total_tasks": 0, "running_tasks": 0, "live_workspaces": 0}]
            ),
            "board_empty",
        ),
        (InconsistentBoardClient(), "board_snapshot_inconsistent"),
    ],
)
def test_bad_authoritative_board_source_deletes_nothing(
    tmp_path: Path,
    client: Any,
    error_code: str,
) -> None:
    root = tmp_path / "workspaces"
    doomed = root / "doomed"
    doomed.mkdir(parents=True)
    mark_old(doomed)
    proc_root = make_proc_root(tmp_path, [(100, tmp_path / "unrelated")])

    exit_code, receipt = reaper.run_reaper(
        root,
        apply=True,
        retention_seconds=3600,
        board_client=client,
        proc_root=proc_root,
    )

    assert exit_code == reaper.EXIT_SAFETY_FAILURE
    assert doomed.is_dir()
    assert receipt["status"] == "safety_failure"
    assert receipt["error"] == {"code": error_code}
    assert "secret detail" not in str(receipt)


def test_board_workspace_bound_deletes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    doomed = root / "doomed"
    doomed.mkdir(parents=True)
    mark_old(doomed)
    proc_root = make_proc_root(tmp_path, [(100, tmp_path / "unrelated")])
    client = FakeBoardClient(
        [("t_one", tmp_path / "one"), ("t_two", tmp_path / "two")],
        total_tasks=2,
    )

    exit_code, receipt = reaper.run_reaper(
        root,
        apply=True,
        retention_seconds=0,
        board_client=client,
        proc_root=proc_root,
        max_live_workspaces=1,
    )

    assert exit_code == reaper.EXIT_SAFETY_FAILURE
    assert receipt["error"] == {"code": "board_workspace_bound_exceeded"}
    assert doomed.is_dir()


def test_symlink_escape_is_listed_but_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "must-survive.txt"
    marker.write_text("safe", encoding="utf-8")
    root.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    proc_root = make_proc_root(tmp_path, [(100, tmp_path / "unrelated")])

    exit_code, receipt = reaper.run_reaper(
        root,
        apply=True,
        retention_seconds=0,
        board_client=FakeBoardClient([]),
        proc_root=proc_root,
    )

    assert exit_code == 0
    assert (root / "escape").is_symlink()
    assert marker.read_text(encoding="utf-8") == "safe"
    assert candidate(receipt, "escape")["reason_codes"] == ["symlink"]


def test_symlink_cleanup_root_fails_closed(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual"
    doomed = actual_root / "doomed"
    doomed.mkdir(parents=True)
    mark_old(doomed)
    root_link = tmp_path / "root-link"
    root_link.symlink_to(actual_root, target_is_directory=True)

    exit_code, receipt = reaper.run_reaper(
        root_link,
        apply=True,
        retention_seconds=0,
        board_client=FakeBoardClient([]),
        proc_root=tmp_path / "unused-proc",
    )

    assert exit_code == reaper.EXIT_SAFETY_FAILURE
    assert receipt["error"] == {"code": "root_is_symlink"}
    assert doomed.is_dir()


def test_unreadable_process_evidence_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    doomed = root / "doomed"
    doomed.mkdir(parents=True)
    mark_old(doomed)
    proc_root = tmp_path / "proc"
    (proc_root / "123").mkdir(parents=True)  # Persistent pid dir, missing cwd.

    exit_code, receipt = reaper.run_reaper(
        root,
        apply=True,
        retention_seconds=0,
        board_client=FakeBoardClient([]),
        proc_root=proc_root,
    )

    assert exit_code == reaper.EXIT_SAFETY_FAILURE
    assert receipt["error"] == {"code": "proc_evidence_unreadable"}
    assert doomed.is_dir()


def test_board_recheck_blocks_toctou_delete(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    candidate_path = root / "candidate"
    candidate_path.mkdir(parents=True)
    mark_old(candidate_path)
    proc_root = make_proc_root(tmp_path, [(100, tmp_path / "unrelated")])
    client = SnapshotBoardClient([[], [("t_became_live", candidate_path)]])

    exit_code, receipt = reaper.run_reaper(
        root,
        apply=True,
        retention_seconds=0,
        board_client=client,
        proc_root=proc_root,
    )

    row = candidate(receipt, "candidate")
    assert exit_code == 0
    assert candidate_path.is_dir()
    assert row["decision"] == "kept"
    assert "running_workspace_overlap" in row["reason_codes"]
    assert "safety_recheck_blocked" in row["reason_codes"]


def test_path_overlap_is_component_safe() -> None:
    assert reaper.paths_overlap(Path("/tmp/job"), Path("/tmp/job/checkout"))
    assert reaper.paths_overlap(Path("/tmp/job/checkout"), Path("/tmp/job"))
    assert not reaper.paths_overlap(Path("/tmp/job"), Path("/tmp/job-other"))
