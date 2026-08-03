"""Regression tests for state-aware non-spawnable Kanban assignment policy."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
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


@pytest.mark.parametrize(
    "title,body",
    (
        ("[FABLE][APPROVAL] release decision", ""),
        ("Release decision", "Fable approval required."),
        ("Fable approval required", "Author a patch only after approval."),
        ("Operator walkthrough for release", ""),
        ("Destructive production purge", ""),
        ("Live apply database recovery", ""),
        ("Install systemd service", ""),
        ("Restart production dispatcher", ""),
        ("Release app to production", ""),
        ("Production launch tracker", ""),
    ),
)
def test_explicit_and_unbracketed_authority_work_is_nonspawnable(
    title: str,
    body: str,
):
    assert is_nonspawnable_contract(title, body)
    assert assignment_guard_reason(
        title=title,
        body=body,
        assignee="codex1",
        status="ready",
        authority_profiles="fable, operator",
    ) == "nonspawnable_contract_active"


@pytest.mark.parametrize("generic_word", ("PARKED", "DEFERRED"))
def test_generic_parking_words_do_not_admit_unparented_fable_executor(
    guarded_board,
    generic_word: str,
):
    conn, _config_path, _db_path = guarded_board
    body = f"{generic_word} for later work."
    assert assignment_guard_reason(
        title="[AUTHOR] unparented patch",
        body=body,
        assignee="fable",
        status="blocked",
        authority_profiles="fable, operator",
    ) == "authority_executor_missing_parking_contract"
    with pytest.raises(ValueError, match="authority_executor_missing_parking_contract"):
        kb.create_task(
            conn,
            title="[AUTHOR] unparented patch",
            body=body,
            assignee="fable",
            initial_status="blocked",
        )


@pytest.mark.parametrize(
    "exact_task_id",
    ("t_69961c40", "t_1ec8fc59", "t_5f21b5af"),
)
def test_required_exact_ids_classify_without_semantic_markers(exact_task_id: str):
    assert is_nonspawnable_contract(
        "ordinary-looking row",
        "",
        task_id=exact_task_id,
    )
    assert (
        assignment_guard_reason(
            task_id=exact_task_id,
            title="ordinary-looking row",
            body="",
            assignee="fable",
            status="ready",
            authority_profiles=("fable", "operator"),
        )
        == "nonspawnable_contract_active"
    )


def test_exact_id_and_old_state_block_atomic_semantic_erasure(guarded_board):
    conn, _config_path, _db_path = guarded_board
    _raw_insert(
        conn,
        task_id="t_69961c40",
        title="[FABLE][APPLY] exact live recovery",
        body="Fable-only live apply.",
        assignee="fable",
        status="scheduled",
    )
    with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
        conn.execute(
            "UPDATE tasks SET title = 'ordinary work', body = '', "
            "assignee = 'codex1', status = 'ready' WHERE id = 't_69961c40'"
        )
    task = _must_get(conn, "t_69961c40")
    assert (task.title, task.assignee, task.status) == (
        "[FABLE][APPLY] exact live recovery",
        "fable",
        "scheduled",
    )

    _raw_insert(
        conn,
        task_id="t_old_state_gate",
        title="[FABLE][APPLY] generic live recovery",
        body="Operator-only live apply.",
        assignee="fable",
        status="scheduled",
    )
    with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
        conn.execute(
            "UPDATE tasks SET title = 'ordinary work', body = '', "
            "assignee = 'codex1', status = 'ready' WHERE id = 't_old_state_gate'"
        )
    assert _must_get(conn, "t_old_state_gate").status == "scheduled"
    with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
        conn.execute(
            "UPDATE tasks SET title = 'ordinary work', body = '' "
            "WHERE id = 't_old_state_gate'"
        )
    assert _must_get(conn, "t_old_state_gate").title == (
        "[FABLE][APPLY] generic live recovery"
    )

    _raw_insert(
        conn,
        task_id="t_1ec8fc59",
        title="ordinary-looking exact-ID row",
        body="",
        assignee="fable",
        status="blocked",
    )
    promoted, error = kb.promote_task(
        conn,
        "t_1ec8fc59",
        actor="reviewer",
        force=True,
    )
    assert promoted is False
    assert error and "nonspawnable_contract_active" in error
    assert _must_get(conn, "t_1ec8fc59").status == "blocked"

    _raw_insert(
        conn,
        task_id="t_5f21b5af",
        title="another ordinary-looking exact-ID row",
        body="",
        assignee="fable",
        status="scheduled",
    )
    with pytest.raises(ValueError, match="nonspawnable_contract_executor_assignee"):
        kb.assign_task(conn, "t_5f21b5af", "codex1")
    assert _must_get(conn, "t_5f21b5af").assignee == "fable"


def test_typed_create_assign_and_reassign_paths_share_the_fence(guarded_board):
    conn, _config_path, _db_path = guarded_board
    with pytest.raises(ValueError, match="nonspawnable_contract_active"):
        kb.create_task(
            conn,
            title="Production release tracker",
            assignee="codex1",
        )

    parent = kb.create_task(conn, title="upstream", assignee="codex1")
    parked = kb.create_task(
        conn,
        title="[AUTHOR] dependency-held patch",
        assignee="fable",
        parents=[parent],
    )
    assert _must_get(conn, parked).status == "todo"
    with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
        conn.execute(
            "UPDATE tasks SET title = 'ordinary work', status = 'ready' "
            "WHERE id = ?",
            (parked,),
        )
    parked_task = _must_get(conn, parked)
    assert (parked_task.title, parked_task.assignee, parked_task.status) == (
        "[AUTHOR] dependency-held patch",
        "fable",
        "todo",
    )
    with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
        conn.execute(
            "UPDATE tasks SET title = 'ordinary work' WHERE id = ?",
            (parked,),
        )
    assert kb.assign_task(conn, parked, "codex1") is True

    active = kb.create_task(conn, title="[AUTHOR] active patch", assignee="codex1")
    with pytest.raises(ValueError, match="authority_executor_not_parked"):
        kb.assign_task(conn, active, "fable")


@pytest.mark.parametrize(
    "malformed_config",
    ("kanban: [unterminated", "kanban: []\n"),
)
def test_malformed_authority_config_fails_closed_after_trigger_install(
    guarded_board,
    malformed_config: str,
):
    conn, config_path, _db_path = guarded_board
    _raw_insert(
        conn,
        task_id="t_malformed_config",
        title="[TRACKER] production release",
        body="Keep scheduled.",
        assignee="fable",
        status="scheduled",
    )
    config_path.write_text(malformed_config, encoding="utf-8")
    with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
        conn.execute(
            "UPDATE tasks SET assignee = 'codex1' WHERE id = 't_malformed_config'"
        )
    assert _must_get(conn, "t_malformed_config").assignee == "fable"


@pytest.mark.parametrize(
    "malformed_config",
    ("kanban: [unterminated", "kanban: []\n"),
)
def test_first_connect_malformed_config_leaves_raw_sql_fail_closed_trigger(
    tmp_path: Path,
    malformed_config: str,
):
    root = tmp_path / ".hermes"
    root.mkdir()
    config_path = root / "config.yaml"
    config_path.write_text(malformed_config, encoding="utf-8")
    db_path = root / "first-connect.sqlite"

    with pytest.raises(kb.AssignmentPolicyConfigError):
        kb.connect(db_path)

    raw_conn = sqlite3.connect(db_path)
    try:
        trigger_names = {
            row[0]
            for row in raw_conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert {
            "trg_tasks_assignment_policy_insert",
            "trg_tasks_assignment_policy_update",
        } <= trigger_names
        with pytest.raises(
            sqlite3.OperationalError,
            match="no such function: kanban_assignment_guard",
        ):
            _raw_insert(
                raw_conn,
                task_id="t_first_connect_bypass",
                title="ordinary work",
                body="",
                assignee="codex1",
                status="ready",
            )
    finally:
        raw_conn.close()

    config_path.write_text(
        "kanban:\n  authority_profiles: fable, operator\n",
        encoding="utf-8",
    )
    conn = kb.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
            _raw_insert(
                conn,
                task_id="t_first_connect_tracker",
                title="Production launch tracker",
                body="Keep scheduled.",
                assignee="codex1",
                status="ready",
            )
    finally:
        conn.close()
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def test_trigger_replacement_has_no_concurrent_drop_window(
    guarded_board,
    monkeypatch: pytest.MonkeyPatch,
):
    conn, _config_path, db_path = guarded_board
    _raw_insert(
        conn,
        task_id="t_trigger_replace_race",
        title="[TRACKER] production release",
        body="Keep scheduled.",
        assignee="fable",
        status="scheduled",
    )
    conn.close()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    drop_seen = threading.Event()
    release_drop = threading.Event()
    original_sqlite_connect = kb._sqlite_connect

    def traced_sqlite_connect(path: Path) -> sqlite3.Connection:
        traced_conn = original_sqlite_connect(path)

        def trace(sql: str) -> None:
            if "DROP TRIGGER" in sql.upper() and not drop_seen.is_set():
                drop_seen.set()
                assert release_drop.wait(10)

        traced_conn.set_trace_callback(trace)
        return traced_conn

    monkeypatch.setattr(kb, "_sqlite_connect", traced_sqlite_connect)

    def cold_reconnect() -> None:
        replacement_conn = kb.connect(db_path)
        replacement_conn.close()

    attacker = sqlite3.connect(db_path, timeout=0.1, isolation_level=None)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(cold_reconnect)
            assert drop_seen.wait(10)
            try:
                with pytest.raises(
                    sqlite3.OperationalError,
                    match="locked|no such function: kanban_assignment_guard",
                ):
                    attacker.execute(
                        "UPDATE tasks SET assignee = 'codex1', status = 'ready' "
                        "WHERE id = 't_trigger_replace_race'"
                    )
            finally:
                release_drop.set()
            future.result(timeout=10)

        with pytest.raises(
            sqlite3.OperationalError,
            match="no such function: kanban_assignment_guard",
        ):
            attacker.execute(
                "UPDATE tasks SET assignee = 'codex1', status = 'ready' "
                "WHERE id = 't_trigger_replace_race'"
            )
    finally:
        attacker.close()

    readback = kb.connect(db_path)
    try:
        task = _must_get(readback, "t_trigger_replace_race")
        assert (task.assignee, task.status) == ("fable", "scheduled")
    finally:
        readback.close()
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def test_unreadable_authority_config_fails_closed_after_trigger_install(
    guarded_board,
    monkeypatch: pytest.MonkeyPatch,
):
    conn, config_path, _db_path = guarded_board
    _raw_insert(
        conn,
        task_id="t_unreadable_config",
        title="[TRACKER] production release",
        body="Keep scheduled.",
        assignee="fable",
        status="scheduled",
    )
    original_read_text = Path.read_text

    def deny_config_read(path: Path, *args, **kwargs):
        if path == config_path:
            raise PermissionError("test unreadable config")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_config_read)
    with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = 't_unreadable_config'"
        )
    assert _must_get(conn, "t_unreadable_config").status == "scheduled"


def test_custom_db_filename_resolves_root_and_preserves_profiles(tmp_path: Path):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text(
        "kanban:\n  authority_profiles: fable, operator\n",
        encoding="utf-8",
    )
    db_path = root / "fleet-board.sqlite"
    conn = kb.connect(db_path)
    try:
        assert kb._authority_profiles(conn) == ("fable", "operator")
        with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
            _raw_insert(
                conn,
                task_id="t_custom_db_tracker",
                title="Production launch tracker",
                body="Keep scheduled.",
                assignee="codex1",
                status="ready",
            )
    finally:
        conn.close()
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def test_decompose_atomically_creates_dependency_held_fable_sibling(
    guarded_board,
):
    conn, _config_path, _db_path = guarded_board
    root = kb.create_task(
        conn,
        title="orchestrate patch graph",
        assignee="orchestrator",
        triage=True,
    )
    child_ids = kb.decompose_triage_task(
        conn,
        root,
        root_assignee="orchestrator",
        auto_promote=False,
        valid_assignees={"orchestrator", "codex1", "fable"},
        children=[
            {"title": "[AUTHOR] upstream patch", "assignee": "codex1"},
            {
                "title": "[AUTHOR] dependency-held follow-up",
                "assignee": "fable",
                "parents": [0],
            },
        ],
    )
    assert child_ids is not None
    assert len(child_ids) == 2
    assert kb.parent_ids(conn, child_ids[1]) == [child_ids[0]]
    child = _must_get(conn, child_ids[1])
    assert (child.assignee, child.status) == ("fable", "todo")


def test_decompose_unparented_fable_executor_rolls_back_every_child(
    guarded_board,
):
    conn, _config_path, _db_path = guarded_board
    root = kb.create_task(
        conn,
        title="orchestrate invalid graph",
        assignee="orchestrator",
        triage=True,
    )
    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="assignment policy"):
        kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            auto_promote=False,
            valid_assignees={"orchestrator", "codex1", "fable"},
            children=[
                {
                    "title": "[AUTHOR] orphaned follow-up",
                    "body": "PARKED for later.",
                    "assignee": "fable",
                }
            ],
        )
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    assert _must_get(conn, root).status == "triage"
    assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0


def test_decompose_partial_graph_is_invisible_to_concurrent_reader(
    guarded_board,
    monkeypatch: pytest.MonkeyPatch,
):
    conn, _config_path, db_path = guarded_board
    root = kb.create_task(
        conn,
        title="orchestrate concurrent graph",
        assignee="orchestrator",
        triage=True,
    )
    entered = threading.Event()
    release = threading.Event()
    original_append = kb._append_event

    def pausing_append(
        event_conn: sqlite3.Connection,
        task_id: str,
        kind: str,
        payload: dict | None = None,
        *,
        run_id: int | None = None,
    ) -> int:
        event_id = original_append(
            event_conn,
            task_id,
            kind,
            payload,
            run_id=run_id,
        )
        if (
            kind == "created"
            and payload
            and payload.get("from_decompose_of") == root
            and not entered.is_set()
        ):
            entered.set()
            assert release.wait(10)
        return event_id

    monkeypatch.setattr(kb, "_append_event", pausing_append)

    def run_decompose():
        worker_conn = kb.connect(db_path)
        try:
            return kb.decompose_triage_task(
                worker_conn,
                root,
                root_assignee="orchestrator",
                auto_promote=False,
                valid_assignees={"orchestrator", "codex1", "fable"},
                children=[
                    {"title": "[AUTHOR] concurrent upstream", "assignee": "codex1"},
                    {
                        "title": "[AUTHOR] concurrent follow-up",
                        "assignee": "fable",
                        "parents": [0],
                    },
                ],
            )
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_decompose)
        assert entered.wait(10)
        observer = sqlite3.connect(db_path)
        try:
            visible = observer.execute(
                "SELECT COUNT(*) FROM tasks WHERE title LIKE '[AUTHOR] concurrent%'"
            ).fetchone()[0]
            assert visible == 0
        finally:
            observer.close()
            release.set()
        child_ids = future.result(timeout=10)

    assert child_ids is not None
    assert len(child_ids) == 2
    assert _must_get(conn, child_ids[1]).assignee == "fable"
