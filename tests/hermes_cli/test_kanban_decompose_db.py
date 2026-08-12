"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


VALID_ASSIGNEES = {"orch", "orchestrator", "researcher", "engineer", "worker"}


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


def _decompose(conn, task_id: str, **kwargs):
    kwargs.setdefault("idempotency_key", f"test-decompose:{task_id}")
    return kb.decompose_triage_task(conn, task_id, **kwargs)


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
        child_ids = _decompose(
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
        child_ids = _decompose(
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


def test_live_parent_chain_and_fanout_cap_are_atomic_recurrence_fences(
    kanban_home,
    caplog,
):
    """Must-fire fixture for MP-4.3 ITEM 2.

    Unfixed main decomposes a triage card under a live ancestor and accepts a
    16-child burst. The fence must reject both without minting even one row.
    """
    assert "idempotency_key" in inspect.signature(
        kb.decompose_triage_task
    ).parameters, "stable decomposition attempt keys must be mandatory"
    assert hasattr(
        kb, "MAX_DECOMPOSITION_CHILDREN"
    ), "decomposition must publish a hard child cap"
    assert kb.MAX_DECOMPOSITION_CHILDREN == 15

    with kb.connect() as conn:
        ancestor = kb.create_task(conn, title="live ancestor")
        parent = kb.create_task(conn, title="live parent", parents=[ancestor])
        guarded_root = kb.create_task(
            conn,
            title="must stay whole",
            parents=[parent],
            triage=True,
        )
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        blocked = _decompose(
            conn,
            guarded_root,
            root_assignee="orchestrator",
            children=[{"title": "must not exist", "assignee": "worker"}],
        )
        assert blocked is None
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
        assert guarded_root not in kb.list_decomposition_eligible_triage_ids(conn)
        assert "live parent chain detected" in caplog.text

        burst_root = _create_triage(conn, title="oversized graph")
        children = [
            {"title": f"child {index}", "assignee": "worker"}
            for index in range(kb.MAX_DECOMPOSITION_CHILDREN + 1)
        ]
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        with pytest.raises(ValueError, match="exceeds hard cap 15"):
            _decompose(
                conn,
                burst_root,
                root_assignee="orchestrator",
                children=children,
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
        assert "reason=fanout_cap" in caplog.text
        assert "requested_children=16" in caplog.text


def test_decomposition_retry_key_returns_original_children_without_remint(
    kanban_home,
):
    key = "decomposition:stable-retry"
    with kb.connect() as conn:
        root = _create_triage(conn, title="retry-safe graph")
        children = [
            {"title": "first", "assignee": "worker"},
            {"title": "second", "assignee": "worker"},
        ]
        first = _decompose(
            conn,
            root,
            root_assignee="orchestrator",
            children=children,
            idempotency_key=key,
        )
        assert first is not None
        count_after_first = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        second = _decompose(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "replacement one", "assignee": "worker"},
                {"title": "replacement two", "assignee": "worker"},
            ],
            idempotency_key=key,
        )
        persisted = conn.execute(
            "SELECT idempotency_key FROM tasks WHERE id IN (?, ?) ORDER BY id",
            tuple(first),
        ).fetchall()

    assert second == first
    assert count_after_first == 3
    assert {row["idempotency_key"] for row in persisted} == {
        f"{key}:{root}:0",
        f"{key}:{root}:1",
    }
