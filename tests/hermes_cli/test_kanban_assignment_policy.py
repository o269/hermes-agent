"""Regression tests for state-aware non-spawnable Kanban assignment policy."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_assignment_policy import (
    assignment_guard_reason,
    is_nonspawnable_contract,
)


# Exact 2026-07-30 live regression frontier. These rows were age-promoted by
# BoardQB and recursively dispatched despite being Fable/operator/live-action
# custody. Keeping the IDs in the fixture makes accidental classifier drift
# visible against the incident receipt rather than only against synthetic names.
LIVE_REGRESSION_CARDS = (
    (
        "t_69961c40",
        "[FABLE][APPLY][BACKUP][XICUT] Exact patch plus shared-volume recovery gate",
        "Fable-only live apply; do not auto-dispatch.",
    ),
    (
        "t_db63bc2b",
        "[FABLE][APPLY][BOARDQB][EXACT PATCH] Process-tree bounds and acceptance criterion",
        "Operator action against the live BoardQB service.",
    ),
    (
        "t_f38d52ac",
        "[CONTROL][FABLE][CAPACITY] Permit declared low-CPU GET-only lanes in intermediate host band",
        "Fable control-plane ruling; keep scheduled.",
    ),
    (
        "t_2317c762",
        "[FABLE][APPLY][BOARDQB FAILOVER R2] Verified stateful negative-control patch",
        "Live apply is reserved to the sole applicator.",
    ),
    (
        "t_b2eaff49",
        "[FABLE][PR684] Apply operator SPLIT/RE-SCOPE ruling and rewrite canonical lineage",
        "Operator-only canonical mutation.",
    ),
    (
        "t_5f2431f2",
        "[PORT][FLEET][DISPATCH] Port ownership-aware active_pr guard to LIVE hermes tree and restart dispatcher",
        "Install/restart of the live service is a nonspawnable action.",
    ),
    (
        "t_75e912b7",
        "[POST-LAUNCH][TOOLING] Evaluate post-launch build-efficiency proposals",
        "Persistent post-launch authority tracker; never dispatch by age.",
    ),
    (
        "t_e34b1a38",
        "[WATCHDOG][RECURRING][TRACKER] Production launch gates and post-launch hygiene",
        "Recurring steward owned by Fable; keep scheduled regardless of age.",
    ),
)


@pytest.fixture
def guarded_board(tmp_path: Path):
    root = tmp_path / ".hermes"
    root.mkdir()
    config_path = root / "config.yaml"
    config_path.write_text("kanban:\n  authority_profiles: fable\n", encoding="utf-8")
    db_path = root / "kanban.db"
    conn = kb.connect(db_path)
    try:
        yield conn, config_path, db_path
    finally:
        conn.close()
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def _raw_insert(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    title: str,
    body: str,
    assignee: str | None,
    status: str,
) -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (task_id, title, body, assignee, status),
    )


def _must_get(conn: sqlite3.Connection, task_id: str):
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def test_exact_live_regression_cards_are_boardqb_gates_and_kernel_parked(
    guarded_board,
):
    conn, _config_path, _db_path = guarded_board

    for task_id, title, body in LIVE_REGRESSION_CARDS:
        assert is_nonspawnable_contract(title, body), task_id
        _raw_insert(
            conn,
            task_id=task_id,
            title=title,
            body=body,
            assignee="fable",
            status="scheduled",
        )

        # Bidirectional fence: neither age promotion nor executor reassignment
        # may make the authority/live-action card spawnable.
        with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?",
                (task_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?",
                (task_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
            conn.execute(
                "UPDATE tasks SET assignee = 'codex1' WHERE id = ?",
                (task_id,),
            )

        row = conn.execute(
            "SELECT assignee, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert (row["assignee"], row["status"]) == ("fable", "scheduled")


def test_dependency_held_fable_author_parks_then_routes_before_promotion(
    guarded_board,
):
    conn, _config_path, _db_path = guarded_board
    parent = kb.create_task(conn, title="upstream implementation", assignee="codex1")
    child = kb.create_task(
        conn,
        title="[AUTHOR] prepare the follow-up patch",
        body="Author a PR after the upstream implementation finishes.",
        assignee="fable",
        parents=[parent],
    )
    assert _must_get(conn, child).status == "todo"

    promoted, reason = kb.promote_task(
        conn,
        child,
        actor="test",
        force=True,
        dry_run=True,
    )
    assert promoted is False
    assert reason == "kanban assignment policy: authority_executor_not_parked"

    # Once the dependency closes, recompute must not auto-promote executor work
    # while it is still in non-spawnable Fable custody.
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    assert kb.recompute_ready(conn) == 0
    assert _must_get(conn, child).status == "todo"

    assert kb.assign_task(conn, child, "codex1") is True
    assert kb.recompute_ready(conn) == 1
    assert _must_get(conn, child).status == "ready"


def test_explicit_contract_allows_safe_fable_review_parking_only(
    guarded_board,
):
    conn, _config_path, _db_path = guarded_board
    review = kb.create_task(
        conn,
        title="[REVIEW] inspect the authored patch",
        body="on-promote: assign to reviewer; keep blocked until then",
        assignee="fable",
        initial_status="blocked",
    )
    assert _must_get(conn, review).status == "blocked"
    assert kb.unblock_task(conn, review) is False

    with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
        conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?",
            (review,),
        )

    assert kb.assign_task(conn, review, "reviewer") is True
    assert kb.unblock_task(conn, review) is True
    assert _must_get(conn, review).status == "ready"


def test_fable_executor_without_parent_or_contract_is_rejected(guarded_board):
    conn, _config_path, _db_path = guarded_board
    with pytest.raises(
        ValueError,
        match="authority_executor_missing_parking_contract",
    ):
        kb.create_task(
            conn,
            title="[AUTHOR] unparented patch",
            body="Write code now.",
            assignee="fable",
            initial_status="blocked",
        )
    assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0


def test_atomic_custom_sql_can_repair_legacy_active_card(guarded_board):
    conn, config_path, _db_path = guarded_board
    config_path.write_text("kanban:\n  authority_profiles: ''\n", encoding="utf-8")
    _raw_insert(
        conn,
        task_id="t_legacy_live_apply",
        title="[FABLE][APPLY] legacy live mutation",
        body="Operator-only live apply.",
        assignee="codex1",
        status="ready",
    )

    config_path.write_text("kanban:\n  authority_profiles: fable\n", encoding="utf-8")
    conn.execute(
        "UPDATE tasks SET status = 'scheduled', assignee = 'fable' WHERE id = ?",
        ("t_legacy_live_apply",),
    )
    row = conn.execute(
        "SELECT assignee, status FROM tasks WHERE id = 't_legacy_live_apply'"
    ).fetchone()
    assert (row["assignee"], row["status"]) == ("fable", "scheduled")


def test_claim_fails_closed_for_legacy_ready_card_after_policy_activation(
    guarded_board,
):
    conn, config_path, _db_path = guarded_board
    config_path.write_text("kanban:\n  authority_profiles: ''\n", encoding="utf-8")
    _raw_insert(
        conn,
        task_id="t_legacy_tracker",
        title="[TRACKER] recurring production hygiene",
        body="Keep scheduled.",
        assignee="codex1",
        status="ready",
    )
    config_path.write_text("kanban:\n  authority_profiles: fable\n", encoding="utf-8")

    assert kb.claim_task(conn, "t_legacy_tracker") is None
    assert _must_get(conn, "t_legacy_tracker").status == "ready"


def test_review_claim_fails_closed_for_legacy_nonspawnable_card(guarded_board):
    conn, config_path, _db_path = guarded_board
    config_path.write_text("kanban:\n  authority_profiles: ''\n", encoding="utf-8")
    _raw_insert(
        conn,
        task_id="t_legacy_review_gate",
        title="[FABLE][APPLY] review-state live mutation",
        body="Operator-only live apply.",
        assignee="fable",
        status="review",
    )
    config_path.write_text("kanban:\n  authority_profiles: fable\n", encoding="utf-8")

    assert kb.claim_review_task(conn, "t_legacy_review_gate") is None
    task = kb.get_task(conn, "t_legacy_review_gate")
    assert task is not None
    assert task.status == "review"


def test_active_policy_fails_closed_for_unregistered_sqlite_writer(guarded_board):
    conn, _config_path, db_path = guarded_board
    _raw_insert(
        conn,
        task_id="t_raw_writer_gate",
        title="[TRACKER] live recurring gate",
        body="Keep scheduled.",
        assignee="fable",
        status="scheduled",
    )

    raw = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="kanban_assignment_guard"):
            raw.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = 't_raw_writer_gate'"
            )
    finally:
        raw.close()


def test_policy_is_opt_in_for_existing_installations(tmp_path: Path):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text(
        "kanban:\n  authority_profiles: ''\n",
        encoding="utf-8",
    )
    db_path = root / "kanban.db"
    conn = kb.connect(db_path)
    try:
        task_id = kb.create_task(
            conn,
            title="[AUTHOR] legacy Fable work",
            assignee="fable",
        )
        assert _must_get(conn, task_id).status == "ready"
        # With the default disabled setting, no persistent trigger is installed;
        # existing unsupported-but-common raw-SQL integrations keep their old
        # behavior until the operator explicitly opts in and restarts the board.
        raw = sqlite3.connect(db_path)
        try:
            raw.execute(
                "UPDATE tasks SET title = 'legacy raw update' WHERE id = ?",
                (task_id,),
            )
            raw.commit()
        finally:
            raw.close()
        assert _must_get(conn, task_id).title == "legacy raw update"
    finally:
        conn.close()
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def test_pure_policy_accepts_comma_or_list_profile_normalization():
    assert (
        assignment_guard_reason(
            title="[TRACKER] recurring gate",
            body="keep scheduled",
            assignee="FABLE",
            status="scheduled",
            authority_profiles=[" fable ", "operator"],
        )
        is None
    )
    assert (
        assignment_guard_reason(
            title="[TRACKER] recurring gate",
            body="keep scheduled",
            assignee="engineer",
            status="scheduled",
            authority_profiles=["fable"],
        )
        == "nonspawnable_contract_executor_assignee"
    )


@pytest.mark.parametrize(
    "title,body",
    (
        ("[INVESTIGATE] apply a patch in a fresh clone", "Ordinary analysis."),
        ("[FABLE][AUTHOR] apply the requested patch", "Route to an engineer."),
        ("[FABLE][REVIEW] inspect and apply review feedback", "Route to a reviewer."),
        ("ordinary maintenance", "on-promote: reviewer; keep scheduled"),
        ("[AUTHOR] fix post-launch workflow", "Implement the quiesce endpoint."),
        (
            "[AUTHOR] produce the patch",
            "Fable is sole lander; do not auto-dispatch until the parent closes.",
        ),
        (
            "Prepare follow-up",
            "Author a patch; Fable is sole lander and must not dispatch it.",
        ),
    ),
)
def test_executor_and_parking_language_are_not_false_nonspawnable_contracts(
    title: str,
    body: str,
):
    assert is_nonspawnable_contract(title, body) is False
