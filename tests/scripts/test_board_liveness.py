"""Must-fire / must-NOT-fire fixtures for ``scripts/board_liveness.py``.

These two tests are the permanence gate for the 2026-08-13 disk-watchdog
incident. If someone "simplifies" the helper by deleting the board match,
``test_must_fire_board_guard_every_nonterminal_status`` fails. If someone
makes the helper keep everything, ``test_must_not_fire_on_junk_or_terminal``
fails.

A GC that keeps everything is as broken as one that deletes live work.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import board_liveness as bl  # noqa: E402

# Exact victim directory names from the 2026-08-13 21:41:28 UTC pass.
VICTIM_READY = "wave-d4-item2-t7e073169"
VICTIM_BLOCKED = "cursor-verify-t19ba5cba"
VICTIM_DONE_TRUNCATED = "hermes-tce22-fd-reaper-main"

# One live id per status the fleet board actually used on 2026-08-13.
# Do not shrink this list without enumerating the live DB again.
STATUS_MATRIX = (
    ("ready", "t_7e073169", VICTIM_READY),
    ("blocked", "t_19ba5cba", VICTIM_BLOCKED),
    ("todo", "t_b75a1e71", "wdguard-todo-tb75a1e71"),
    ("review", "t_1de4e419", "wdguard-review-t1de4e419"),
    ("triage", "t_9dc55573", "wdguard-triage-t9dc55573"),
    ("scheduled", "t_89fb9854", "wdguard-sched-t89fb9854"),
    ("running", "t_142868f0", "wdguard-running-t142868f0"),
)


def test_observed_nonterminal_set_is_the_live_board_set():
    """If this fails, the live board grew a status the incident fixture missed."""
    assert bl.OBSERVED_NON_TERMINAL_STATUSES == (
        "blocked",
        "ready",
        "todo",
        "review",
        "triage",
        "scheduled",
        "running",
    )


def test_must_fire_board_guard_every_nonterminal_status():
    """MUST FIRE: every non-terminal status holds both name spellings.

    Removing the board match (or restricting it to ``running`` / a live
    process) makes this fail. That is the incident.
    """
    for status, task_id, dirname in STATUS_MATRIX:
        live = bl.live_hexes([SimpleNamespace(task_id=task_id, status=status)])
        assert live, f"{status} produced no live hex — terminal-set regression"
        assert bl.name_is_held(dirname, live, board_ok=True), (
            f"{status} did not hold {dirname!r} (underscoreless / truncated form)"
        )
        assert bl.name_is_held(task_id, live, board_ok=True), (
            f"{status} did not hold canonical {task_id!r}"
        )


def test_must_fire_on_the_two_destroyed_nonterminal_victims():
    live = bl.live_hexes([
        SimpleNamespace(task_id="t_7e073169", status="ready"),
        SimpleNamespace(task_id="t_19ba5cba", status="blocked"),
        SimpleNamespace(task_id="t_ce22cf43", status="done"),
    ])
    assert bl.name_is_held(VICTIM_READY, live, board_ok=True)
    assert bl.name_is_held(VICTIM_BLOCKED, live, board_ok=True)
    # done card: the board must NOT save it. Unpushed work is a different oracle.
    assert not bl.name_is_held(VICTIM_DONE_TRUNCATED, live, board_ok=True)


def test_must_not_fire_on_junk_or_terminal():
    """MUST NOT FIRE: junk and terminal cards stay collectable.

    A helper that returns True for every name (or for every token) fails here.
    """
    live = bl.live_hexes([
        SimpleNamespace(task_id="t_7e073169", status="ready"),
        SimpleNamespace(task_id="t_492ce754", status="done"),
        SimpleNamespace(task_id="t_ef02b763", status="archived"),
    ])
    assert not bl.name_is_held("wdguard-junk-plain-no-tid", live, board_ok=True)
    assert not bl.name_is_held("wdtest-nocard-plainjunk", live, board_ok=True)
    assert not bl.name_is_held("tsx-0", live, board_ok=True)
    assert not bl.name_is_held("omnia-smoke.log", live, board_ok=True)
    assert not bl.name_is_held("wdtest-t_492ce754", live, board_ok=True)
    assert not bl.name_is_held("wdguard-archived-tef02b763", live, board_ok=True)
    assert not bl.name_is_held(VICTIM_DONE_TRUNCATED, live, board_ok=True)


def test_must_fire_fail_closed_holds_shaped_names_only():
    """Unreadable board: task-id-shaped KEEP, junk still DELETE."""
    assert bl.name_is_held(VICTIM_READY, (), board_ok=False)
    assert bl.name_is_held(VICTIM_BLOCKED, (), board_ok=False)
    assert bl.name_is_held(VICTIM_DONE_TRUNCATED, (), board_ok=False)
    assert bl.name_is_held("wdtest-t_492ce754", (), board_ok=False)
    assert not bl.name_is_held("wdguard-junk-plain-no-tid", (), board_ok=False)
    assert not bl.name_is_held("plain-junk-no-shape", (), board_ok=False)
    assert not bl.name_is_held("tsx-0", (), board_ok=False)


def test_is_task_id_shaped_is_the_alarm_predicate():
    assert bl.is_task_id_shaped(VICTIM_READY)
    assert bl.is_task_id_shaped(VICTIM_BLOCKED)
    assert bl.is_task_id_shaped(VICTIM_DONE_TRUNCATED)
    assert bl.is_task_id_shaped("t_492ce754")
    assert not bl.is_task_id_shaped("wdguard-junk-plain-no-tid")
    assert not bl.is_task_id_shaped("tsx-0")


def test_running_only_oracle_is_rejected_by_must_fire():
    """Document the incident oracle: 'running' is not 'live'."""
    # A ready card with no process. The broken oracle would drop this.
    live = bl.live_hexes([SimpleNamespace(task_id="t_7e073169", status="ready")])
    assert "7e073169" in live
    assert bl.name_is_held(VICTIM_READY, live, board_ok=True)


def test_shell_helper_matches_python_on_incident_names():
    """The sourceable shell helper is the same contract, not a second guess."""
    script = r"""
    source "$1"
    BOARD_LIVENESS_OK=1
    BOARD_LIVENESS_HEX=$'7e073169\n19ba5cba\n'
    held() { board_liveness_holds "$1" && echo KEEP || echo DELETE; }
    held 'wave-d4-item2-t7e073169'
    held 'cursor-verify-t19ba5cba'
    held 'hermes-tce22-fd-reaper-main'
    held 'wdguard-junk-plain-no-tid'
    BOARD_LIVENESS_OK=0
    BOARD_LIVENESS_HEX=''
    held 'wave-d4-item2-t7e073169'
    held 'wdguard-junk-plain-no-tid'
    """
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(SCRIPTS_DIR / "board_liveness.sh")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == [
        "KEEP",
        "KEEP",
        "DELETE",
        "DELETE",
        "KEEP",
        "DELETE",
    ]


@pytest.mark.parametrize(
    ("name", "token"),
    [
        ("wave-d4-item2-t7e073169", "7e073169"),
        ("cursor-verify-t19ba5cba", "19ba5cba"),
        ("hermes-tce22-fd-reaper-main", "ce22"),
        ("t_7e073169", "7e073169"),
    ],
)
def test_extract_hex_tokens_covers_both_spellings(name, token):
    assert token in bl.extract_hex_tokens(name)
