"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp
from hermes_cli.kanban_assignment_policy import LaneEligibilityPolicy


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch.object(decomp.profiles_mod, "list_profiles", return_value=fake_profiles),
        patch.object(
            decomp.profiles_mod,
            "profile_exists",
            side_effect=lambda x: x in names,
        ),
        patch.object(
            decomp.profiles_mod,
            "get_active_profile_name",
            return_value=names[0] if names else "default",
        ),
    ]


def test_roster_includes_profile_provider_and_model_defaults(kanban_home):
    patches = _patch_list_profiles(["engineer"])
    for item in patches:
        item.start()
    try:
        roster, valid_names = decomp._build_roster()
    finally:
        for item in patches:
            item.stop()

    assert valid_names == {"engineer"}
    assert roster[0]["provider"] == "p"
    assert roster[0]["model"] == "m"
    rendered = decomp._format_roster(roster)
    assert "profile defaults: provider=p, model=m" in rendered


def test_roster_excludes_dead_and_derostered_but_adds_receipted_remote(
    kanban_home,
    tmp_path,
):
    receipts = tmp_path / "lane-health"
    receipts.mkdir()
    now = datetime.now(timezone.utc)
    fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (receipts / "codex1.env").write_text(
        f"LANE_OK=true\nPROBED_AT={fresh}\n", encoding="utf-8"
    )
    (receipts / "vps2-eng1.env").write_text(
        f"status=LANE_OK\nprobed_at={fresh}\n", encoding="utf-8"
    )
    (receipts / "stale1.env").write_text(
        f"LANE_OK=true\nPROBED_AT={stale}\n", encoding="utf-8"
    )
    (receipts / "kimi1.env").write_text(
        f"LANE_OK=true\nPROBED_AT={fresh}\n", encoding="utf-8"
    )
    policy = LaneEligibilityPolicy(
        receipt_dir=receipts,
        de_rostered_prefixes=("kimi",),
    )
    patches = _patch_list_profiles(["codex1", "stale1", "kimi1", "fable"])
    for item in patches:
        item.start()
    try:
        roster, valid_names = decomp._build_roster(
            lane_policy=policy,
            authority_profiles=("fable",),
        )
    finally:
        for item in patches:
            item.stop()

    assert valid_names == {"codex1", "vps2-eng1"}
    assert {entry["name"] for entry in roster} == valid_names


def test_completed_run_for_retired_profile_does_not_grant_authority(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="historical run", assignee="retired-lane")
        assert kb.claim_task(conn, tid) is not None
        kb._set_worker_pid(conn, tid, os.getpid())
        assert kb.complete_task(conn, tid)

    with patch.object(decomp.profiles_mod, "list_profiles", return_value=[]), patch.object(
        decomp,
        "_fleet_named_lanes_only",
        return_value=False,
    ):
        roster, valid_names = decomp._build_roster()

    assert roster == []
    assert valid_names == set()


def test_json_response_wrapper_preserves_exact_values():
    payload = jsonlib.dumps(
        {"fanout": False, "body": "  keep exact whitespace and {braces}  "}
    )
    wrapped = f"```json\n{payload}\n```"

    assert decomp._response_content(_fake_aux_response(wrapped)) == wrapped
    assert decomp._response_content(wrapped) == wrapped
    assert decomp._extract_json_blob(wrapped) == {
        "fanout": False,
        "body": "  keep exact whitespace and {braces}  ",
    }


def test_decompose_with_fanout_creates_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2
    assert outcome.root_status == "todo"
    assert outcome.dependency_edges == 1
    assert outcome.root_dependencies == 2
    assert outcome.leaf_count == 1

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_fanout_preserves_exact_metadata_body_bytes(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="preserve exact body", triage=True)

    exact_body = '  {"metadata":{"artifact":{"bytes":17}}}  '
    llm_payload = jsonlib.dumps(
        {
            "fanout": True,
            "tasks": [
                {
                    "title": "preserve metadata",
                    "body": exact_body,
                    "assignee": "engineer",
                    "parents": [],
                },
                {
                    "title": "verify metadata",
                    "body": "verify",
                    "assignee": "engineer",
                    "parents": [0],
                },
            ],
        }
    )
    patches = _patch_list_profiles(["engineer"])
    for item in patches:
        item.start()
    try:
        with _patch_aux_client(llm_payload):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
    assert child is not None
    assert child.body == exact_body
    assert child.body.encode() == exact_body.encode()


def test_decompose_fanout_false_assigns_default_when_unassigned(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="just one thing", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "**Goal**\nDo the thing.",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch.object(
            decomp,
            "_load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is False
    assert outcome.new_title == "Tightened title"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    # specify path with no parents -> recompute_ready flips to 'ready'
    assert task.status == "ready"
    assert task.title == "Tightened title"
    assert task.assignee == "fallback"


def test_decompose_fanout_false_preserves_existing_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="already routed",
            assignee="engineer",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Keep existing lane.",
        "assignee": "fallback",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch.object(
            decomp,
            "_load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"
    assert task.status == "triage"
    assert task.title == "Tightened title"
    assert any(event.kind == "decomposed" for event in events)
    assert tid not in decomp.list_triage_ids()


def test_no_fanout_skips_when_custody_changes_while_llm_runs(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="race-sensitive root", triage=True)

    llm_payload = jsonlib.dumps(
        {
            "fanout": False,
            "rationale": "single unit",
            "title": "Tightened title",
            "body": "Keep late custody.",
        }
    )

    def assign_during_llm(*_args, **_kwargs):
        with kb.connect() as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = 'fable' WHERE id = ?",
                (tid,),
            )
        # Match call_llm's response contract while preserving the exact JSON
        # payload, including metadata-bearing body text.
        return _fake_aux_response(llm_payload)

    patches = _patch_list_profiles(["fallback"])
    for item in patches:
        item.start()
    try:
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=assign_during_llm,
        ), _patch_extra_body(), patch.object(
            decomp,
            "_load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert outcome.skipped is True
    assert "assignee changed from None to 'fable'" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.assignee == "fable"
    assert task.status == "triage"
    assert task.title == "race-sensitive root"
    assert task.body is None
    assert not any(event.kind == "decomposed" for event in events)


def test_fanout_skips_all_children_when_custody_changes_while_llm_runs(
    kanban_home,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="race-sensitive root", triage=True)

    llm_payload = jsonlib.dumps(
        {
            "fanout": True,
            "tasks": [
                {"title": "child A", "body": "A", "assignee": "engineer", "parents": []},
                {"title": "child B", "body": "B", "assignee": "engineer", "parents": []},
            ],
        }
    )

    def assign_during_llm(*_args, **_kwargs):
        with kb.connect() as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = 'fable' WHERE id = ?",
                (tid,),
            )
        return _fake_aux_response(llm_payload)

    patches = _patch_list_profiles(["engineer"])
    for item in patches:
        item.start()
    try:
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=assign_during_llm,
        ):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert outcome.skipped is True
    assert outcome.child_ids is None
    assert "assignee changed from None to 'fable'" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        child_count = conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE child_id = ?",
            (tid,),
        ).fetchone()[0]
    assert task is not None
    assert task.assignee == "fable"
    assert task.status == "triage"
    assert child_count == 0
    assert not any(event.kind == "decomposed" for event in events)


def test_no_fanout_skips_assignment_aba_from_public_transitions(kanban_home):
    original_body = ' \t\n{"metadata":{"blank":" ","bytes":17}}\n\t '
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="ABA original title",
            body=original_body,
            triage=True,
        )

    llm_payload = jsonlib.dumps(
        {
            "fanout": False,
            "title": "ABA stale title",
            "body": "ABA stale body",
            "assignee": "engineer",
        }
    )

    def assign_and_restore(*_args, **_kwargs):
        with kb.connect() as conn:
            assert kb.assign_task(conn, tid, "fable")
        with kb.connect() as conn:
            assert kb.assign_task(conn, tid, None)
        return _fake_aux_response(llm_payload)

    patches = _patch_list_profiles(["engineer"])
    for item in patches:
        item.start()
    try:
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=assign_and_restore,
        ):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert outcome.skipped is True
    assert "assignment generation changed from 0 to 2" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert task is not None
    assert task.title == "ABA original title"
    assert task.body is not None
    assert task.body.encode("utf-8") == original_body.encode("utf-8")
    assert task.assignee is None
    assert task.status == "triage"
    assert task.assignment_generation == 2
    assert task_count == 1
    assert sum(event.kind == "assigned" for event in events) == 2
    assert not any(event.kind in {"specified", "decomposed"} for event in events)


def test_fanout_skips_assignment_aba_from_public_transitions(kanban_home):
    original_body = "fanout root bytes\n\n  preserved  \n"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="ABA fanout root",
            body=original_body,
            triage=True,
        )

    llm_payload = jsonlib.dumps(
        {
            "fanout": True,
            "tasks": [
                {
                    "title": "child A",
                    "body": "A",
                    "assignee": "engineer",
                    "parents": [],
                },
                {
                    "title": "child B",
                    "body": "B",
                    "assignee": "engineer",
                    "parents": [],
                },
            ],
        }
    )

    def assign_and_restore(*_args, **_kwargs):
        with kb.connect() as conn:
            assert kb.assign_task(conn, tid, "fable")
        with kb.connect() as conn:
            assert kb.assign_task(conn, tid, None)
        return _fake_aux_response(llm_payload)

    patches = _patch_list_profiles(["engineer"])
    for item in patches:
        item.start()
    try:
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=assign_and_restore,
        ):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert outcome.skipped is True
    assert outcome.child_ids is None
    assert "assignment generation changed from 0 to 2" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        links = conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0]
    assert task is not None
    assert task.title == "ABA fanout root"
    assert task.body is not None
    assert task.body.encode("utf-8") == original_body.encode("utf-8")
    assert task.assignee is None
    assert task.status == "triage"
    assert task.assignment_generation == 2
    assert task_count == 1
    assert links == 0
    assert sum(event.kind == "assigned" for event in events) == 2
    assert not any(event.kind == "decomposed" for event in events)


def test_fanout_rejects_profile_removed_during_inference(kanban_home):
    from types import SimpleNamespace

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="live roster root",
            body="must remain byte exact\n",
            triage=True,
        )

    live_names = ["engineer"]

    def list_live_profiles():
        return [
            SimpleNamespace(
                name=name,
                description=f"{name} capability",
                provider="p",
                model="m",
            )
            for name in live_names
        ]

    llm_payload = jsonlib.dumps(
        {
            "fanout": True,
            "tasks": [
                {
                    "title": "child A",
                    "body": "A",
                    "assignee": "engineer",
                    "parents": [],
                },
                {
                    "title": "child B",
                    "body": "B",
                    "assignee": "engineer",
                    "parents": [],
                },
            ],
        }
    )

    def retire_then_answer(*_args, **_kwargs):
        live_names.clear()
        return _fake_aux_response(llm_payload)

    with patch.object(
        decomp.profiles_mod,
        "list_profiles",
        side_effect=list_live_profiles,
    ), patch.object(
        decomp.profiles_mod,
        "get_active_profile_name",
        return_value="engineer",
    ), patch.object(
        decomp,
        "_fleet_named_lanes_only",
        return_value=False,
    ), patch(
        "agent.auxiliary_client.call_llm",
        side_effect=retire_then_answer,
    ):
        outcome = decomp.decompose_task(tid, author="auto-decomposer")

    assert outcome.ok is False
    assert "has no resolvable profile" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        links = conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0]
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.title == "live roster root"
    assert task.body == "must remain byte exact\n"
    assert task.assignee is None
    assert task.status == "triage"
    assert task.assignment_generation == 0
    assert task_count == 1
    assert links == 0
    assert not any(event.kind == "decomposed" for event in events)


def test_no_fanout_rechecks_gate_added_while_llm_runs(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="race-sensitive root", triage=True)

    llm_payload = jsonlib.dumps(
        {
            "fanout": False,
            "rationale": "single unit",
            "title": "Must not replace gate",
            "body": "Must not promote.",
        }
    )

    def gate_during_llm(*_args, **_kwargs):
        with kb.connect() as conn:
            assert kb.append_task_gate(conn, tid, "OPERATOR-GATE")
        return _fake_aux_response(llm_payload)

    patches = _patch_list_profiles(["fallback"])
    for item in patches:
        item.start()
    try:
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=gate_during_llm,
        ), _patch_extra_body(), patch.object(
            decomp,
            "_load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert outcome.skipped is True
    assert "control-plane hold appeared" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.title == "race-sensitive root [OPERATOR-GATE]"
    assert task.body is None
    assert task.assignee is None
    assert task.status == "triage"
    assert not any(event.kind == "decomposed" for event in events)


def test_decompose_fanout_false_uses_valid_llm_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to specialist.",
        "assignee": "engineer",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch.object(
            decomp,
            "_load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch.object(
            decomp,
            "_load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_decompose_unknown_assignee_falls_back_to_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    # Roster only has 'orchestrator' and 'fallback'; LLM picks 'made_up'.
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test",
        "tasks": [
            {"title": "do X", "body": "", "assignee": "made_up", "parents": []},
            {"title": "do Y", "body": "", "assignee": "made_up", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch.dict(
            "os.environ", {}, clear=False,
        ), _patch_aux_client(llm_payload), _patch_extra_body(), \
            patch.object(
                decomp,
                "_load_config",
                return_value={
                    "kanban": {
                        "orchestrator_profile": "orchestrator",
                        "default_assignee": "fallback",
                    }
                },
            ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 2
    with kb.connect() as conn:
        children = [kb.get_task(conn, child_id) for child_id in outcome.child_ids]
    # 'made_up' wasn't in roster, so assignee rewritten to 'fallback'
    assert all(child is not None and child.assignee == "fallback" for child in children)


def test_decompose_fanout_preserves_assigned_root_custody(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="PR17 cross-agent preflight gate",
            assignee="fable",
            triage=True,
        )

    payload = jsonlib.dumps(
        {
            "fanout": True,
            "rationale": "parallel preflight",
            "tasks": [
                {
                    "title": "Run code preflight",
                    "body": "Verify the candidate.",
                    "assignee": "engineer",
                    "parents": [],
                },
                {
                    "title": "Run release preflight",
                    "body": "Verify release custody.",
                    "assignee": "engineer",
                    "parents": [],
                },
            ],
        }
    )
    patches = _patch_list_profiles(["fable", "engineer"])
    for item in patches:
        item.start()
    try:
        with _patch_aux_client(payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
    assert root is not None
    assert root.assignee == "fable"
    assert root.status == "triage"


def test_control_plane_hold_skips_llm_and_auto_triage_list(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="PR19 infrastructure [DECISION REQUIRED]",
            triage=True,
        )

    llm = MagicMock()
    with patch("agent.auxiliary_client.call_llm", llm):
        outcome = decomp.decompose_task(tid, author="auto-decomposer")

    assert outcome.ok is False
    assert "control-plane hold" in outcome.reason
    llm.assert_not_called()
    assert tid not in decomp.list_triage_ids()
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "triage"
    assert task.assignee is None


def test_no_resolvable_profile_fails_loud_without_default_lane(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route this safely", triage=True)

    patches = _patch_list_profiles([])
    for item in patches:
        item.start()
    try:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert "no resolvable orchestrator profile" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "triage"
    assert task.assignee is None


def test_fleet_implicit_default_is_not_a_routable_lane(kanban_home):
    """The built-in root profile must not become a fleet assignee by accident."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route this safely", triage=True)

    patches = _patch_list_profiles(["default"])
    for item in patches:
        item.start()
    try:
        with patch.object(decomp, "_fleet_named_lanes_only", return_value=True):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert outcome.reason == "no resolvable orchestrator profile; task left in triage"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "triage"
    assert task.assignee is None


def test_fleet_detection_follows_resolved_db_path(kanban_home, monkeypatch):
    monkeypatch.setenv(
        "HERMES_KANBAN_DB",
        str(kb.board_dir("fleet") / "kanban.db"),
    )
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")

    assert decomp._fleet_named_lanes_only() is True


def test_decompose_handles_malformed_llm_json(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client("not json at all, sorry"), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "malformed JSON" in outcome.reason


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert outcome.skipped is True
    assert outcome.root_status == "ready"
    assert "not in triage" in outcome.reason


def test_decompose_reports_status_race_as_skip(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="race me", triage=True)

    llm_payload = jsonlib.dumps(
        {"fanout": False, "title": "must not land", "body": "must not land"}
    )

    def move_status_during_llm(*_args, **_kwargs):
        with kb.connect() as conn, kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        return _fake_aux_response(llm_payload)

    patches = _patch_list_profiles(["engineer"])
    for item in patches:
        item.start()
    try:
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=move_status_during_llm,
        ):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert outcome.skipped is True
    assert outcome.root_status == "ready"
    assert "status changed to 'ready'" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.title == "race me"
    assert task.body is None


@pytest.mark.parametrize("task_count", [1, 7])
def test_decompose_rejects_fanout_outside_two_through_six(
    kanban_home,
    task_count,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bounded graph", triage=True)

    llm_payload = jsonlib.dumps(
        {
            "fanout": True,
            "tasks": [
                {
                    "title": f"child {index}",
                    "body": f"body {index}",
                    "assignee": "engineer",
                    "parents": [],
                }
                for index in range(task_count)
            ],
        }
    )
    patches = _patch_list_profiles(["engineer"])
    for item in patches:
        item.start()
    try:
        with _patch_aux_client(llm_payload):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert f"{task_count} tasks; expected 2-6" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        task_count_after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert task is not None
    assert task.status == "triage"
    assert task.assignee is None
    assert task_count_after == 1


@pytest.mark.parametrize(
    ("parents", "expected"),
    [
        ([3], "outside 0..1"),
        ([0], "cannot depend on itself"),
        (["0"], "must be an integer index"),
        ([False], "must be an integer index"),
    ],
)
def test_decompose_rejects_invalid_dependency_with_diagnostic(
    kanban_home, parents, expected
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bad graph", triage=True)

    llm_payload = jsonlib.dumps(
        {
            "fanout": True,
            "tasks": [
                {
                    "title": "child",
                    "body": "must not be created",
                    "assignee": "engineer",
                    "parents": parents,
                },
                {
                    "title": "valid sibling",
                    "body": "must not be created",
                    "assignee": "engineer",
                    "parents": [],
                },
            ],
        }
    )
    patches = _patch_list_profiles(["engineer"])
    for item in patches:
        item.start()
    try:
        with _patch_aux_client(llm_payload):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert expected in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "triage"
        child_count = conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE child_id = ?", (tid,)
        ).fetchone()[0]
        assert child_count == 0


def test_decompose_reports_cyclic_dependency_path():
    with pytest.raises(ValueError, match=r"0 -> 1 -> 0"):
        decomp._dependency_diagnostics(
            [
                {"parents": [1]},
                {"parents": [0]},
            ]
        )


def test_decompose_no_aux_client_configured(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        # call_llm raises RuntimeError when no provider is configured; the
        # decomposer must convert that into a failed outcome, not a crash.
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("No LLM provider configured"),
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    # call_llm's no-provider RuntimeError surfaces via the LLM-error branch.
    assert "LLM error" in outcome.reason
