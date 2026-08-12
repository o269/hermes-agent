"""Ready-snapshot disposition invariants for the Kanban dispatcher."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

_REAL_GITHUB_PULL_STATE = getattr(kb, "_github_pull_state")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty Kanban database."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _ready_ids(conn):
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM tasks WHERE status = 'ready' "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()
    ]


def _outcomes(result):
    return {
        entry.task_id: (entry.outcome, entry.reason, dict(entry.detail))
        for entry in result.dispositions
    }


def test_ready_snapshot_has_one_priority_ordered_disposition_per_card(
    kanban_home, monkeypatch
):
    """Mixed guards/skips/spawns cannot omit or duplicate a Ready row."""
    monkeypatch.setattr(
        kb,
        "_assignee_has_spawn_target",
        lambda assignee: assignee != "terminal",
    )
    with kb.connect() as conn:
        spawn_a = kb.create_task(conn, title="spawn a", assignee="alpha", priority=100)
        cap_a = kb.create_task(conn, title="cap a", assignee="alpha", priority=90)
        unassigned = kb.create_task(conn, title="unassigned", priority=80)
        terminal = kb.create_task(
            conn, title="terminal", assignee="terminal", priority=70
        )
        active_pr = kb.create_task(
            conn, title="active pr", assignee="beta", priority=60
        )
        pr_url = "https://github.com/o269/hermes-agent/pull/99999"
        kb.add_comment(conn, active_pr, "beta", f"AUTHOR COMPLETE: {pr_url}")
        spawn_c = kb.create_task(conn, title="spawn c", assignee="charlie", priority=50)
        budget_d = kb.create_task(conn, title="budget d", assignee="delta", priority=40)
        ready_ids = _ready_ids(conn)

        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=2,
            max_in_progress_per_profile=1,
        )

    assert ready_ids == [
        spawn_a,
        cap_a,
        unassigned,
        terminal,
        active_pr,
        spawn_c,
        budget_d,
    ]
    disposition_ids = [entry.task_id for entry in result.dispositions]
    assert disposition_ids == ready_ids
    assert len(disposition_ids) == len(set(disposition_ids))
    outcomes = _outcomes(result)
    assert outcomes[spawn_a][:2] == ("spawned", None)
    assert outcomes[cap_a][:2] == ("held", "per_profile_cap")
    assert outcomes[unassigned][:2] == ("skipped", "unassigned")
    assert outcomes[terminal][:2] == ("skipped", "nonspawnable_assignee")
    assert outcomes[active_pr][:2] == ("held", "active_pr")
    assert outcomes[active_pr][2]["pr_url"] == pr_url
    assert outcomes[spawn_c][:2] == ("spawned", None)
    assert outcomes[budget_d][:2] == ("held", "max_spawn")


def test_global_cap_holds_every_ready_card_and_persists_events(
    kanban_home, all_assignees_spawnable
):
    """The old max_in_progress early return now accounts for every row."""
    with kb.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="runner")
        assert kb.claim_task(conn, running) is not None
        kb._set_worker_pid(conn, running, os.getpid())
        first = kb.create_task(conn, title="first", assignee="alpha", priority=20)
        second = kb.create_task(conn, title="second", assignee="beta", priority=10)
        ready_ids = _ready_ids(conn)

        result = kb.dispatch_once(
            conn,
            max_in_progress=1,
        )

        persisted = {
            task_id: [
                event
                for event in kb.list_events(conn, task_id)
                if event.kind == "dispatch_disposition"
            ]
            for task_id in ready_ids
        }

    assert ready_ids == [first, second]
    assert [entry.task_id for entry in result.dispositions] == ready_ids
    assert all(
        (entry.outcome, entry.reason) == ("held", "max_in_progress")
        for entry in result.dispositions
    )
    assert all(len(events) == 1 for events in persisted.values())
    assert all(
        events[0].payload
        == {
            "outcome": "held",
            "reason": "max_in_progress",
            "detail": {"current": 1, "limit": 1},
        }
        for events in persisted.values()
    )


@pytest.mark.parametrize(
    ("failure_stage", "expected_reason"),
    [("workspace", "workspace_failure"), ("spawn", "spawn_failure")],
)
def test_ready_failure_paths_emit_compact_secret_free_dispositions(
    kanban_home,
    all_assignees_spawnable,
    monkeypatch,
    failure_stage,
    expected_reason,
):
    """Workspace and spawn exceptions are terminal for this tick, not silent."""
    secret = "TOP-SECRET-CREDENTIAL"
    if failure_stage == "workspace":

        def fail_workspace(*_args, **_kwargs):
            raise RuntimeError(secret)

        monkeypatch.setattr(kb, "resolve_workspace", fail_workspace)

    def spawn(_task, _workspace):
        if failure_stage == "spawn":
            raise RuntimeError(secret)
        raise AssertionError("spawn must not run after workspace resolution fails")

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=failure_stage, assignee="alpha")
        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn,
            failure_limit=99,
        )
        events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "dispatch_disposition"
        ]

    assert [entry.task_id for entry in result.dispositions] == [task_id]
    entry = result.dispositions[0]
    assert (entry.outcome, entry.reason) == ("skipped", expected_reason)
    assert entry.detail["error_type"] == "RuntimeError"
    assert len(events) == 1
    assert secret not in json.dumps(events[0].payload)


def test_claim_rejection_race_has_explicit_skipped_disposition(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    monkeypatch.setattr(kb, "claim_task", lambda *_args, **_kwargs: None)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="claim race", assignee="alpha")
        result = kb.dispatch_once(conn)

    assert [entry.task_id for entry in result.dispositions] == [task_id]
    assert (result.dispositions[0].outcome, result.dispositions[0].reason) == (
        "skipped",
        "claim_rejected",
    )


def test_active_pr_claim_race_keeps_local_custody_detail(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A PR appearing between guard evaluation and claim is still diagnosable."""
    pr_url = "https://github.com/o269/hermes-agent/pull/99998"

    def race_claim(conn, task_id, **_kwargs):
        kb.add_comment(conn, task_id, "alpha", f"PR opened: {pr_url}")
        decision = kb.evaluate_respawn_guard(conn, task_id, process_snapshot=[])
        assert decision.reason == "active_pr"
        kb.record_respawn_guard_decision(conn, task_id, decision, phase="claim")
        return None

    monkeypatch.setattr(kb, "claim_task", race_claim)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="claim custody race", assignee="alpha")
        result = kb.dispatch_once(conn)

    assert [entry.task_id for entry in result.dispositions] == [task_id]
    entry = result.dispositions[0]
    assert (entry.outcome, entry.reason) == ("held", "active_pr")
    assert entry.detail["pr_url"] == pr_url
    assert (task_id, "active_pr") in result.respawn_guarded


def test_active_pr_guard_rechecks_github_state_without_releasing_open_control(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A closed PR releases its card; an actually-open sibling remains held."""
    closed_url = "https://github.com/o269/hermes-agent/pull/67"
    open_url = "https://github.com/o269/hermes-agent/pull/70"
    states = {closed_url: "CLOSED", open_url: "OPEN"}
    calls: list[str] = []

    def github_state(pr_url):
        calls.append(pr_url)
        return states[pr_url]

    monkeypatch.setattr(kb, "_github_pull_state", github_state, raising=False)
    with kb.connect() as conn:
        closed_card = kb.create_task(
            conn, title="ordinary closed PR card", assignee="alpha", priority=20
        )
        kb.add_comment(conn, closed_card, "alpha", f"AUTHOR COMPLETE: {closed_url}")
        open_card = kb.create_task(
            conn, title="ordinary open PR card", assignee="beta", priority=10
        )
        kb.add_comment(conn, open_card, "beta", f"AUTHOR COMPLETE: {open_url}")

        result = kb.dispatch_once(conn, dry_run=True)

    outcomes = _outcomes(result)
    assert outcomes[closed_card][:2] == ("spawned", None)
    assert outcomes[open_card][:2] == ("held", "active_pr")
    assert calls == [closed_url, open_url]


def test_closed_pr_releases_claim_time_guard(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """The atomic claim path uses the same live-state predicate as dispatch."""
    closed_url = "https://github.com/o269/hermes-agent/pull/67"
    monkeypatch.setattr(kb, "_github_pull_state", lambda _url: "CLOSED")
    spawned: list[str] = []
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="closed PR card", assignee="alpha")
        kb.add_comment(conn, task_id, "alpha", f"AUTHOR COMPLETE: {closed_url}")

        result = kb.dispatch_once(
            conn, spawn_fn=lambda task, _workspace: spawned.append(task.id)
        )

    assert _outcomes(result)[task_id][:2] == ("spawned", None)
    assert spawned == [task_id]


def test_closed_pr_does_not_consume_resume_marker(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    closed_url = "https://github.com/o269/hermes-agent/pull/67"
    monkeypatch.setattr(kb, "_github_pull_state", lambda _url: "CLOSED")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="closed PR with marker", assignee="alpha")
        kb.add_comment(conn, task_id, "alpha", f"AUTHOR COMPLETE: {closed_url}")
        getattr(kb, "_append_event")(
            conn,
            task_id,
            "resume_marker",
            {
                "actor": "fable",
                "authorized_by": "operator-test",
                "authorized_profile": "alpha",
                "assignment_generation": getattr(
                    kb, "_resume_assignment_generation"
                )(conn, task_id),
                "reason": "closed PR no longer needs custody bypass",
            },
        )

        assert kb.claim_task(conn, task_id) is not None
        consumed = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "resume_marker_consumed"
        ]

    assert consumed == []


@pytest.mark.parametrize(
    ("title", "matching_numbers"),
    [
        ("REWORK omnia#896+#897 - refresh stack", {896, 897}),
        ("FIX omnia#896, #897 - refresh stack", {896, 897}),
        ("Audit omnia#896+#897", set()),
        ("FIX omnia#896 - not the sibling", {896}),
    ],
)
def test_rework_title_matches_only_explicit_prs(title, matching_numbers):
    for number in (896, 897, 898):
        pr_url = f"https://github.com/o269/omnia/pull/{number}"
        assert getattr(kb, "_title_marks_matching_pr_rework")(title, pr_url) is (
            number in matching_numbers
        )


@pytest.mark.parametrize("marker", ["REWORK", "FIX"])
def test_open_pr_rework_card_spawns_but_non_rework_sibling_stays_held(
    kanban_home, all_assignees_spawnable, monkeypatch, marker
):
    """REWORK/FIX exempts only the matching PR, including the claim-time guard."""
    pr_url = "https://github.com/o269/hermes-agent/pull/70"
    other_pr_url = "https://github.com/o269/hermes-agent/pull/71"
    calls: list[str] = []

    def github_state(url):
        calls.append(url)
        return "OPEN"

    monkeypatch.setattr(kb, "_github_pull_state", github_state, raising=False)
    spawned: list[str] = []
    with kb.connect() as conn:
        rework = kb.create_task(
            conn,
            title=f"{marker} hermes-agent#70 - repair the live fence",
            assignee="alpha",
            priority=40,
        )
        kb.add_comment(conn, rework, "alpha", f"Continue existing {pr_url}")
        sibling = kb.create_task(
            conn,
            title="Audit hermes-agent#70 before landing",
            assignee="beta",
            priority=20,
        )
        kb.add_comment(conn, sibling, "beta", f"AUTHOR COMPLETE: {pr_url}")
        wrong_pr = kb.create_task(
            conn,
            title="FIX hermes-agent#71 - unrelated repair",
            assignee="charlie",
            priority=20,
        )
        kb.add_comment(conn, wrong_pr, "charlie", f"Related: {pr_url}")
        mixed_prs = kb.create_task(
            conn,
            title=f"{marker} hermes-agent#70 - with another open dependency",
            assignee="delta",
            priority=10,
        )
        kb.add_comment(
            conn,
            mixed_prs,
            "delta",
            f"Repair {pr_url}; dependency remains {other_pr_url}",
        )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )

    outcomes = _outcomes(result)
    assert outcomes[rework][:2] == ("spawned", None)
    assert outcomes[sibling][:2] == ("held", "active_pr")
    assert outcomes[wrong_pr][:2] == ("held", "active_pr")
    assert outcomes[mixed_prs][:2] == ("held", "active_pr")
    assert outcomes[mixed_prs][2]["pr_url"] == other_pr_url
    assert spawned == [rework]
    assert calls == [pr_url, pr_url, other_pr_url]


def test_active_pr_state_lookup_failure_keeps_fail_closed_hold(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    pr_url = "https://github.com/o269/hermes-agent/pull/70"
    monkeypatch.setattr(kb, "_github_pull_state", lambda _url: None, raising=False)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="ordinary work", assignee="alpha")
        kb.add_comment(conn, task_id, "alpha", f"AUTHOR COMPLETE: {pr_url}")
        result = kb.dispatch_once(conn, dry_run=True)

    assert _outcomes(result)[task_id][:2] == ("held", "active_pr")


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (0, "OPEN\n", "OPEN"),
        (0, "CLOSED\n", "CLOSED"),
        (0, "MERGED\n", "MERGED"),
        (1, "", None),
        (0, "UNKNOWN\n", None),
    ],
)
def test_github_pull_state_maps_live_cli_result_fail_closed(
    monkeypatch, returncode, stdout, expected
):
    result = kb.subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout
    )
    monkeypatch.setattr(kb.subprocess, "run", lambda *_args, **_kwargs: result)

    assert (
        _REAL_GITHUB_PULL_STATE("https://github.com/o269/hermes-agent/pull/70")
        == expected
    )
