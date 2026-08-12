"""Must-fire wiring: brief-path-guard.sh is invoked from dispatch.

Criterion 13 (t_3d64f74b): a remote-host brief that cites a blitz-only
godmode-bus path must be refused; a clean brief must still pass.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

FIXTURE_GUARD = (
    Path(__file__).resolve().parent / "fixtures" / "brief-path-guard-stub.sh"
)
REAL_GUARD = Path.home() / "godmode-bus" / "bin" / "brief-path-guard.sh"
CANARY_REL = "to-orchestrator/t_3d64f74b-must-fire-canary.md"
CANARY_BLITZ = Path.home() / "godmode-bus" / CANARY_REL


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def fixture_guard(monkeypatch):
    assert FIXTURE_GUARD.is_file(), f"missing known-positive stub {FIXTURE_GUARD}"
    mode = FIXTURE_GUARD.stat().st_mode
    if not (mode & stat.S_IXUSR):
        FIXTURE_GUARD.chmod(mode | stat.S_IXUSR)
    monkeypatch.setenv(kb.BRIEF_PATH_GUARD_ENV, str(FIXTURE_GUARD))
    return FIXTURE_GUARD


def _spawn_recorder():
    calls = []

    def _spawn(task, workspace, board=None):
        calls.append((task.id, task.assignee, workspace, board))
        return 4242

    return calls, _spawn


def test_fixture_guard_known_positive_fires(fixture_guard):
    """A zero is a claim about the tool: the stub must refuse the canary."""
    bad = subprocess.run(
        [str(fixture_guard), "--stdin", "--host", "vps2"],
        input="read /home/odai/godmode-bus/to-orchestrator/MUST_FIRE_BLITZ_ONLY.md",
        text=True,
        capture_output=True,
    )
    good = subprocess.run(
        [str(fixture_guard), "--stdin", "--host", "vps2"],
        input="Write the receipt to docs/receipts/ok.md",
        text=True,
        capture_output=True,
    )
    assert bad.returncode == 4, bad.stdout + bad.stderr
    assert "VIOLATION" in bad.stdout
    assert good.returncode == 0, good.stdout + good.stderr
    assert "PASS" in good.stdout


def test_dispatch_refuses_blitz_only_brief(
    kanban_home, fixture_guard, all_assignees_spawnable
):
    calls, spawn = _spawn_recorder()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="P29 must-fire bad",
            body="Read /home/odai/godmode-bus/to-orchestrator/MUST_FIRE_BLITZ_ONLY.md",
            assignee="vps2-eng1",
        )
        result = kb.dispatch_once(conn, spawn_fn=spawn)

        task = kb.get_task(conn, tid)
        comments = kb.list_comments(conn, tid)
        events = [e for e in kb.list_events(conn, tid) if e.kind == "brief_path_guard"]

    assert calls == []
    assert result.spawned == []
    assert tid in result.brief_path_blocked
    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert any("VIOLATION" in (c.body or "") for c in comments)
    assert events and events[-1].payload.get("rc") == 4
    outcomes = {e.task_id: (e.outcome, e.reason) for e in result.dispositions}
    assert outcomes[tid] == ("skipped", "brief_path_guard")


def test_dispatch_allows_clean_remote_brief(
    kanban_home, fixture_guard, all_assignees_spawnable
):
    calls, spawn = _spawn_recorder()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="P29 must-fire good",
            body="Write the receipt to docs/receipts/ok.md — no bus path.",
            assignee="vps2-eng1",
        )
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, tid)

    assert len(calls) == 1
    assert calls[0][0] == tid
    assert result.brief_path_blocked == []
    assert task is not None
    assert task.status == "running"
    assert any(item[0] == tid for item in result.spawned)


def test_local_assignee_does_not_invoke_guard(
    kanban_home, fixture_guard, all_assignees_spawnable, monkeypatch
):
    invoked = []

    def _boom(**kwargs):
        invoked.append(kwargs)
        raise AssertionError("local cards must not exec the remote-host guard")

    monkeypatch.setattr(kb, "run_brief_path_guard", _boom)
    calls, spawn = _spawn_recorder()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="local",
            body="Read /home/odai/godmode-bus/to-orchestrator/MUST_FIRE_BLITZ_ONLY.md",
            assignee="alpha",
        )
        result = kb.dispatch_once(conn, spawn_fn=spawn)

    assert invoked == []
    assert result.brief_path_blocked == []
    assert any(item[0] == tid for item in result.spawned)


def test_unverifiable_is_nonfatal(
    kanban_home, fixture_guard, all_assignees_spawnable
):
    calls, spawn = _spawn_recorder()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="unverifiable",
            body="See UNVERIFIABLE_BARE_NAME_XYZ before starting.",
            assignee="vps2-eng2",
        )
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, tid)
        comments = kb.list_comments(conn, tid)

    assert any(item[0] == tid for item in result.spawned)
    assert result.brief_path_blocked == []
    assert task is not None and task.status == "running"
    assert any("UNVERIFIABLE" in (c.body or "") for c in comments)
    assert calls


def test_guard_outage_fail_open(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    monkeypatch.setattr(
        kb,
        "run_brief_path_guard",
        lambda **kwargs: (2, "killed by signal 9"),
    )
    calls, spawn = _spawn_recorder()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="outage",
            body="any brief",
            assignee="vps2-eng1",
        )
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, tid)
        comments = kb.list_comments(conn, tid)

    assert any(item[0] == tid for item in result.spawned)
    assert result.brief_path_blocked == []
    assert task is not None and task.status == "running"
    assert any("fail-open" in (c.body or "") or "rc=2" in (c.body or "") for c in comments)


def test_second_tick_does_not_reinvoke_or_recomment(
    kanban_home, fixture_guard, all_assignees_spawnable, monkeypatch
):
    calls, spawn = _spawn_recorder()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="P29 must-fire bad",
            body="cite MUST_FIRE_BLITZ_ONLY.md",
            assignee="vps2-eng1",
        )
        kb.dispatch_once(conn, spawn_fn=spawn)
        comments_after_first = list(kb.list_comments(conn, tid))

    invoked = []
    real = kb.run_brief_path_guard

    def _wrap(**kwargs):
        invoked.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(kb, "run_brief_path_guard", _wrap)
    with kb.connect() as conn:
        # Unblock without fixing the brief — guard must re-block, not re-exec.
        kb.unblock_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, tid)
        comments_after_second = list(kb.list_comments(conn, tid))

    assert invoked == []
    assert task is not None
    # Re-block after unblock increments the same-cause counter; the
    # loop breaker may escalate to triage. Either way it must not spawn.
    assert task.status in {"blocked", "triage"}
    assert task.status not in {"ready", "running"}
    assert len(comments_after_second) == len(comments_after_first)


def test_dry_run_violation_does_not_mutate(
    kanban_home, fixture_guard, all_assignees_spawnable
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="dry",
            body="MUST_FIRE_BLITZ_ONLY.md",
            assignee="vps2-eng1",
        )
        result = kb.dispatch_once(conn, dry_run=True)
        task = kb.get_task(conn, tid)
        comments = kb.list_comments(conn, tid)

    assert tid in result.brief_path_blocked
    assert task is not None and task.status == "ready"
    assert comments == []


def _vps2_canary_absent() -> bool:
    probe = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "vps2",
            f"test ! -e /root/godmode-bus/{CANARY_REL} && "
            f"test ! -e /home/odai/godmode-bus/{CANARY_REL}",
        ],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not REAL_GUARD.is_file(), reason="live brief-path-guard.sh not installed")
def test_live_guard_script_must_fire_and_pass():
    """Known-positive against the real checker (SSH to vps2)."""
    if not _vps2_canary_absent():
        pytest.skip("canary already present on vps2; refuse to poison the proof")
    CANARY_BLITZ.parent.mkdir(parents=True, exist_ok=True)
    CANARY_BLITZ.write_text(
        "t_3d64f74b must-fire canary — blitz-only, do not copy to vps2\n",
        encoding="utf-8",
    )
    try:
        bad = subprocess.run(
            [str(REAL_GUARD), "--stdin", "--host", "vps2"],
            input=f"Read {CANARY_BLITZ}\n",
            text=True,
            capture_output=True,
            timeout=30,
        )
        good = subprocess.run(
            [str(REAL_GUARD), "--stdin", "--host", "vps2"],
            input="Write docs/receipts/ok.md — repo-relative, travels with the clone.\n",
            text=True,
            capture_output=True,
            timeout=30,
        )
    finally:
        try:
            CANARY_BLITZ.unlink()
        except OSError:
            pass
    assert bad.returncode == 4, bad.stdout + bad.stderr
    assert "VIOLATION" in bad.stdout
    assert CANARY_REL in bad.stdout
    assert good.returncode == 0, good.stdout + good.stderr
    assert "PASS" in good.stdout
