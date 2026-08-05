"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decomposition_eligibility_ignores_cross_posted_pr_citations(kanban_home):
    pr_url = "https://github.com/acme/widget/pull/36"
    with kb.connect() as conn:
        referenced_id = _create_triage(
            conn,
            title="citation only",
            assignee="worker",
        )
        declared_id = _create_triage(
            conn,
            title="owns repair PR",
            assignee="worker",
        )
        kb.add_comment(conn, referenced_id, "observer", f"Related repair: {pr_url}")
        kb.add_comment(conn, declared_id, "worker", f"AUTHOR COMPLETE {pr_url}")

        assert kb.decomposition_hold_reason(conn, referenced_id) is None
        assert kb.decomposition_hold_reason(conn, declared_id) is not None
        eligible = kb.list_decomposition_eligible_triage_ids(conn)

    assert referenced_id in eligible
    assert declared_id not in eligible


def test_respawn_guard_ignores_cross_posted_pr_citations_without_event_flood(
    kanban_home,
):
    pr_url = "https://github.com/acme/widget/pull/36"
    with kb.connect() as conn:
        referenced_id = kb.create_task(
            conn,
            title="citation only",
            assignee="worker",
        )
        declared_id = kb.create_task(
            conn,
            title="owns repair PR",
            assignee="worker",
        )
        kb.add_comment(conn, referenced_id, "observer", f"Related repair: {pr_url}")
        kb.add_comment(conn, declared_id, "worker", f"AUTHOR COMPLETE {pr_url}")

        detail = {}
        assert kb.check_respawn_guard(conn, referenced_id, detail_out=detail) is None
        assert detail["ignored_pr_urls"] == [
            {"pr_url": pr_url, "declared_by": declared_id}
        ]

        assert kb.check_respawn_guard(conn, referenced_id) is None
        ignored_events = [
            event
            for event in kb.list_events(conn, referenced_id)
            if event.kind == "respawn_guard_pr_ignored"
        ]
        assert len(ignored_events) == 1
        assert ignored_events[0].payload == {
            "reason": "pr_declared_by_other_task",
            "ignored_pr_urls": [
                {"pr_url": pr_url, "declared_by": declared_id}
            ],
        }

        owner_detail = {}
        assert (
            kb.check_respawn_guard(conn, declared_id, detail_out=owner_detail)
            == "active_pr"
        )
        assert owner_detail["pr_url"] == pr_url
        assert owner_detail["ownership"] == "declared"


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)




