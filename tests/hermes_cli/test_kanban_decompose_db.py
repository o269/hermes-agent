"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

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
            valid_assignees=VALID_ASSIGNEES,
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


def test_decompose_preserves_assigned_root_custody_and_triage_status(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(
            conn,
            title="PR17 cross-agent preflight gate",
            assignee="fable",
        )
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="default",
            children=[
                {
                    "title": "Run preflight",
                    "assignee": "engineer",
                    "parents": [],
                }
            ],
            valid_assignees={"fable", "engineer"},
            author="auto-decomposer",
        )

        assert child_ids and len(child_ids) == 1
        root = kb.get_task(conn, tid)
        assert root is not None
        assert root.assignee == "fable"
        assert root.status == "triage"
        assert root.assignee != "default"

        # Assigned roots intentionally stay in triage; the decomposed event is
        # the durable replay guard that prevents a second fan-out next tick.
        second = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="fable",
            children=[{"title": "Duplicate", "assignee": "engineer"}],
            valid_assignees={"fable", "engineer"},
            author="auto-decomposer",
        )
        assert second is None


def test_decompose_rejects_unroutable_assignee_atomically(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="no resolvable profile"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orchestrator",
                children=[{"title": "bad route", "assignee": "ghost"}],
                valid_assignees={"orchestrator"},
                author="auto-decomposer",
            )
        root = kb.get_task(conn, tid)
        assert root is not None
        assert root.status == "triage"
        assert root.assignee is None
        assert not any(
            event.kind == "decomposed" for event in kb.list_events(conn, tid)
        )


def test_specify_rejects_unroutable_assignee_before_write(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="no resolvable profile"):
            kb.specify_triage_task(
                conn,
                tid,
                title="specified",
                assignee="ghost",
                valid_assignees={"engineer"},
                author="auto-decomposer",
            )
        root = kb.get_task(conn, tid)
        assert root is not None
        assert root.title == "rough idea"
        assert root.status == "triage"
        assert root.assignee is None


def test_decompose_returns_none_for_control_plane_gate(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="PR17 [QUIESCE-GATE]")
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[{"title": "must not exist", "assignee": "engineer"}],
            valid_assignees={"orchestrator", "engineer"},
            author="auto-decomposer",
        )
        assert result is None
        root = kb.get_task(conn, tid)
        assert root is not None
        assert root.status == "triage"
        assert root.assignee is None


def test_free_form_operator_prose_is_not_a_permanent_hold(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(
            conn,
            title="PR19 infrastructure preflight",
            body="The prior operator authority finding is closed; proceed normally.",
        )
        kb.add_comment(
            conn,
            tid,
            "operator",
            "The prior operator authority finding is closed; proceed normally.",
        )
        assert kb.decomposition_hold_reason(conn, tid) is None


def test_free_form_comment_history_is_not_scanned_for_holds(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="PR19 infrastructure preflight")
        kb.add_comment(
            conn,
            tid,
            "operator",
            "Normalized unassigned/non-dispatchable pending operator authority.",
        )
        for index in range(55):
            kb.add_comment(conn, tid, "worker", f"benign follow-up {index}")

        assert kb.decomposition_hold_reason(conn, tid) is None


def test_ordinary_comment_wording_is_not_an_operator_ruling(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="Improve documentation parser")
        kb.add_comment(
            conn,
            tid,
            "engineer",
            "Document how the phrase non-dispatchable is tokenized in examples.",
        )

        assert kb.decomposition_hold_reason(conn, tid) is None


def test_active_pr_custody_blocks_decomposition_with_repair_guidance(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(
            conn,
            title="repair existing change",
            assignee="worker",
        )
        kb.add_comment(
            conn,
            tid,
            "worker",
            "Opened https://github.com/acme/widgets/pull/42 for this card.",
        )

        reason = kb.decomposition_hold_reason(conn, tid)
        assert reason is not None
        assert "active PR custody detected" in reason
        assert "https://github.com/acme/widgets/pull/42" in reason
        assert f"/kanban continuation review {tid}" in reason
        assert f"/kanban continuation authorize {tid}" in reason

        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="worker",
            children=[{"title": "replacement work", "assignee": "engineer"}],
            valid_assignees=VALID_ASSIGNEES,
            author="auto-decomposer",
        )
        assert result is None
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "triage"
        assert task.assignee == "worker"


def test_append_task_gate_preserves_status_and_assignee(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="PR17 cross-agent preflight", assignee="fable")
        assert kb.append_task_gate(
            conn,
            tid,
            "OPERATOR-GATE",
            field="title",
            author="engineer",
        )
        # The mutation is idempotent and content-only.
        assert kb.append_task_gate(
            conn,
            tid,
            "OPERATOR-GATE",
            field="title",
            author="engineer",
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.title.count("[OPERATOR-GATE]") == 1
        assert task.status == "triage"
        assert task.assignee == "fable"
        assert kb.decomposition_hold_reason(conn, tid) is not None


def test_append_task_gate_rejects_done_card(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="finished", assignee="worker")
        assert kb.complete_task(conn, tid)
        with pytest.raises(ValueError, match="cannot mark done task"):
            kb.append_task_gate(conn, tid, "FREEZE-GATE")


def test_batched_triage_eligibility_uses_one_bounded_read(kanban_home):
    with kb.connect() as conn:
        eligible_id = _create_triage(conn, title="ordinary executable work")
        gated_id = _create_triage(conn, title="release [OPERATOR-GATE]")
        pr_id = _create_triage(conn, title="continue existing change", assignee="worker")
        kb.add_comment(
            conn,
            pr_id,
            "worker",
            "Opened https://github.com/acme/widgets/pull/42 for this card.",
        )
        for index in range(25):
            other_id = _create_triage(conn, title=f"ordinary {index}")
            kb.add_comment(conn, other_id, "worker", f"benign comment {index}")

        class CountingConnection:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.execute_calls = 0

            def execute(self, *args, **kwargs):
                self.execute_calls += 1
                return self.wrapped.execute(*args, **kwargs)

        counted = CountingConnection(conn)
        ids = kb.list_decomposition_eligible_triage_ids(counted)

        assert counted.execute_calls == 1
        assert eligible_id in ids
        assert gated_id not in ids
        assert pr_id not in ids


def test_decompose_returns_none_when_task_missing(kanban_home):
    with kb.connect() as conn:
        result = kb.decompose_triage_task(
            conn,
            "nonexistent",
            root_assignee="orch",
            children=[{"title": "x"}],
            valid_assignees=VALID_ASSIGNEES,
            author="me",
        )
    assert result is None


def test_decompose_returns_none_when_task_not_in_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already a real task")  # not triage
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "x"}],
            valid_assignees=VALID_ASSIGNEES,
            author="me",
        )
    assert result is None


def test_decompose_empty_children_returns_none(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[],
            valid_assignees=VALID_ASSIGNEES,
            author="me",
        )
    assert result is None


def test_decompose_rejects_self_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cannot list itself"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [0]}],
                valid_assignees=VALID_ASSIGNEES,
                author="me",
            )


def test_decompose_rejects_out_of_range_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="not a valid index"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [5]}],
                valid_assignees=VALID_ASSIGNEES,
                author="me",
            )


def test_decompose_rejects_cyclic_parents(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cyclic dependency"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[
                    {"title": "A", "parents": [1]},
                    {"title": "B", "parents": [0]},
                ],
                valid_assignees=VALID_ASSIGNEES,
                author="me",
            )


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            valid_assignees=VALID_ASSIGNEES,
            author="alice",
        )
    assert child_ids is not None

    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)


def test_decompose_children_inherit_dir_workspace(kanban_home):
    """Fan-out children inherit the root's dir workspace, not scratch."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="codegen root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "part A"}, {"title": "part B", "parents": [0]}],
            valid_assignees=VALID_ASSIGNEES,
            author="decomposer",
        )
    assert child_ids and len(child_ids) == 2
    with kb.connect() as conn:
        for cid in child_ids:
            t = kb.get_task(conn, cid)
            assert t.workspace_kind == "dir"
            assert t.workspace_path == proj


def test_decompose_children_stay_scratch_when_root_scratch(kanban_home):
    """No regression: a scratch root still fans out into scratch children."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="scratch root", assignee="worker",
            workspace_kind="scratch", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "s1"}],
            valid_assignees=VALID_ASSIGNEES,
            author="decomposer",
        )
    with kb.connect() as conn:
        t = kb.get_task(conn, child_ids[0])
    assert t.workspace_kind == "scratch"
    assert t.workspace_path is None


def test_decompose_per_child_workspace_override(kanban_home):
    """An explicit per-child workspace beats inheritance."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[
                {"title": "override", "workspace_kind": "dir",
                 "workspace_path": "/other/repo"},
                {"title": "inherit"},
            ],
            valid_assignees=VALID_ASSIGNEES,
            author="decomposer",
        )
    with kb.connect() as conn:
        over = kb.get_task(conn, child_ids[0])
        inh = kb.get_task(conn, child_ids[1])
    assert over.workspace_path == "/other/repo"
    assert inh.workspace_path == proj
