from __future__ import annotations

import importlib.util
from itertools import permutations
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = REPO_ROOT / "scripts" / "kanban-authority-lane"


def _load_module(name: str, path: Path):
    sys.path.insert(0, str(ASSET_DIR))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ASSET_DIR))


bridge = _load_module("authority_lane_bridge_test", ASSET_DIR / "kanban_bridge_state.py")
policy = sys.modules["authority_lane_policy"]
installer = _load_module("authority_lane_installer_test", ASSET_DIR / "install.py")

SEPARATED_MIXED_TITLES = (
    "[FIX][FABLE][LAND] implement the reviewed repair",
    "[review][operator-hold] verify implementation",
)
COMBINED_MIXED_TITLES = tuple(
    f"[{'+'.join(parts)}] implement the reviewed repair"
    for parts in permutations(("fable", "land", "fix"))
) + tuple(
    f"[{'+'.join(parts)}] verify implementation"
    for parts in permutations(("operator-hold", "review"))
)
MIXED_TITLES = SEPARATED_MIXED_TITLES + COMBINED_MIXED_TITLES
BROAD_EXECUTOR_AUTHORITY_CASES = (
    pytest.param("[land] implement the migration", None, id="R6-P01"),
    pytest.param("[LAND] implement the migration", None, id="R6-P02"),
    pytest.param("[APPLY] implement the migration", None, id="R6-P03"),
    pytest.param("[FABLE+LAND] implement the migration", None, id="R6-P04"),
    pytest.param("[fable+land] implement the migration", None, id="R6-P05"),
    pytest.param("[LAND+FABLE] implement the migration", None, id="R6-P06"),
    pytest.param("[OPERATOR-GATE] implement the migration", None, id="R6-P07"),
    pytest.param("[ACCEPTANCE] implement the migration", None, id="R6-P08"),
    pytest.param("[MERGE] author the fix", None, id="R6-P09"),
    pytest.param("[LAND] fix the bug", None, id="R6-P10"),
    pytest.param("[LAND] review the PR", None, id="R6-P11"),
    pytest.param("[LAND] build the package", None, id="R6-P12"),
    pytest.param("[LAND] test the suite", None, id="R6-P13"),
    pytest.param("[LAND] verify the result", None, id="R6-P14"),
    pytest.param("[LAND] audit the surface", None, id="R6-P15"),
    pytest.param("[LAND] rework the patch", None, id="R6-P16"),
    pytest.param("[LAND] rebind the branch", None, id="R6-P17"),
    pytest.param("[LAND] migrate the schema", None, id="R6-P18"),
    pytest.param("[LAND] migration plan", None, id="R6-P19"),
    pytest.param("[LAND] authoring notes", None, id="R6-P20"),
    pytest.param("[LAND] source code changes", None, id="R6-P21"),
    pytest.param("[LAND] source-code deliverable", None, id="R6-P22"),
    pytest.param("[LAND] pull request ready", None, id="R6-P23"),
    pytest.param("[LAND] PR #23 follow-up", None, id="R6-P24"),
    pytest.param("[LAND] PR 23 follow-up", None, id="R6-P25"),
    pytest.param(
        "[fable+land] authority hold",
        "Please implement the migration under Fable custody.",
        id="R6-P26-body",
    ),
    pytest.param(
        "[LAND] custody hold",
        "Please author the fix.",
        id="R6-P27-body",
    ),
    pytest.param(
        "[APPLY] custody hold",
        "Please review the diff.",
        id="R6-P28-body",
    ),
    pytest.param(
        "[LAND] custody hold",
        "Open pull request when ready.",
        id="R6-P29-body",
    ),
    pytest.param(
        "[LAND] custody hold",
        "Ship source-code changes.",
        id="R6-P30-body",
    ),
)
CLASSIFICATION_CONTROL_CASES = (
    pytest.param("[LAND] ship after gates", None, False, False, True, id="C-AUTH-01"),
    pytest.param(
        "[FABLE][LAND] exact-head acceptance",
        None,
        False,
        False,
        True,
        id="C-AUTH-02",
    ),
    pytest.param(
        "[FABLE][LAND+INSTALL-GATE] reviewed exact-head acceptance",
        None,
        False,
        False,
        True,
        id="C-AUTH-03",
    ),
    pytest.param(
        "[APPLY] operator cutover",
        "Waiting on operator signal only.",
        False,
        False,
        True,
        id="C-AUTH-04",
    ),
    pytest.param(
        "[LAND+FIX] implement the reviewed repair",
        None,
        True,
        True,
        True,
        id="C-MIX-01",
    ),
    pytest.param(
        "[FIX][FABLE][LAND] implement the reviewed repair",
        None,
        True,
        True,
        True,
        id="C-MIX-02",
    ),
    pytest.param("[FIX] repair the policy", None, True, True, False, id="C-EXEC-01"),
    pytest.param(
        "[REVIEW] exact-head security verdict",
        None,
        True,
        True,
        False,
        id="C-EXEC-02",
    ),
    pytest.param(
        "implement the migration", None, True, False, False, id="C-EXEC-03"
    ),
    pytest.param(
        "Status update", "Waiting on operator.", False, False, False, id="C-NEUT-01"
    ),
    pytest.param(
        "[AUTHOR][LANDING-PAGE] implement the review fixes",
        None,
        True,
        True,
        False,
        id="C-EDGE-01",
    ),
    pytest.param("[LAND] fixed pricing display", None, False, False, True, id="C-EDGE-02"),
    pytest.param("[LAND] prefix the column", None, False, False, True, id="C-EDGE-03"),
    pytest.param(
        "LAND the change after review", None, True, False, False, id="C-EDGE-04"
    ),
    pytest.param(
        "implement the migration",
        "Must [LAND] after review",
        True,
        False,
        False,
        id="C-EDGE-05",
    ),
    pytest.param("[LANDER-PREP] packet", None, False, False, False, id="C-EDGE-06"),
    pytest.param("[LANDING-PAGE] ship copy", None, False, False, False, id="C-EDGE-07"),
)


class MemoryBroker:
    """SQLite-backed stand-in for boardd's exact transaction/readback surface."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                assignee TEXT,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                created_by TEXT,
                workspace_kind TEXT,
                workspace_path TEXT,
                branch_name TEXT,
                started_at INTEGER,
                completed_at INTEGER,
                claim_lock TEXT,
                claim_expires INTEGER,
                worker_pid INTEGER,
                last_heartbeat_at INTEGER,
                current_run_id INTEGER,
                block_kind TEXT,
                last_failure_error TEXT,
                result TEXT
            );
            CREATE TABLE task_events (
                task_id TEXT,
                run_id INTEGER,
                kind TEXT,
                payload TEXT,
                created_at INTEGER
            );
            CREATE TABLE task_comments (
                task_id TEXT,
                author TEXT,
                body TEXT,
                created_at INTEGER
            );
            """
        )
        self._next = 0
        self._open = False
        self.readbacks = 0
        self.tamper_readback = False

    def seed(
        self,
        *,
        task_id: str,
        title: str,
        assignee: str,
        status: str = "running",
        body: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO tasks(id, title, body, assignee, status) VALUES (?, ?, ?, ?, ?)",
            (task_id, title, body, assignee, status),
        )
        self.conn.commit()

    def txn_begin(self) -> str:
        assert not self._open
        self.conn.execute("BEGIN")
        self._open = True
        return "txn"

    def txn_exec(self, txn: str, sql: str, params=()):
        assert txn == "txn" and self._open
        cursor = self.conn.execute(sql, tuple(params or ()))
        if sql.lstrip().upper().startswith("SELECT"):
            return {"rows": [dict(row) for row in cursor.fetchall()]}
        return {"rowcount": cursor.rowcount, "lastrowid": cursor.lastrowid}

    def txn_commit(self, txn: str):
        assert txn == "txn" and self._open
        self.conn.commit()
        self._open = False
        return {"committed": True}

    def txn_rollback(self, txn: str):
        assert txn == "txn"
        if self._open:
            self.conn.rollback()
            self._open = False
        return {"rolled_back": True}

    def get_task(self, task_id: str) -> dict[str, Any]:
        self.readbacks += 1
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert row is not None
        result = dict(row)
        if self.tamper_readback:
            result["assignee"] = "tampered"
        return result

    def create_task(
        self,
        *,
        title: str,
        assignee: str | None,
        status: str,
        body: str | None,
        created_by: str,
        priority: int,
        workspace_kind: str,
        id: str | None,
    ) -> dict[str, str]:
        self._next += 1
        task_id = id or f"t_created{self._next}"
        self.conn.execute(
            """INSERT INTO tasks(
                   id, title, body, assignee, status, priority,
                   created_by, workspace_kind
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                title,
                body,
                assignee,
                status,
                priority,
                created_by,
                workspace_kind,
            ),
        )
        self.conn.commit()
        return {"id": task_id}


def _transition(broker: MemoryBroker, **overrides):
    values = {
        "task_id": "t_executor",
        "state": "review",
        "board": "fleet",
        "bridge": "test-executor",
        "worktree": "/tmp/work",
        "branch": "fix/test",
        "pid": 123,
        "now": 1000,
    }
    values.update(overrides)
    return bridge.transition_task(broker, **values)


@pytest.mark.parametrize("title", MIXED_TITLES)
def test_classifier_retains_mixed_dimensions_for_executable_precedence(title: str):
    classification = policy.classify_card(title)

    assert classification.executor
    assert classification.executor_marker
    assert classification.authority
    assert classification.mixed
    assert not classification.pure_authority


@pytest.mark.parametrize("title", MIXED_TITLES)
def test_public_authority_helper_rejects_mixed_title(title: str):
    assert not policy.is_explicit_authority_card(title)


@pytest.mark.parametrize(
    ("title", "expected"),
    (
        ("[FABLE][LAND] exact-head acceptance", True),
        ("[review] exact-head security verdict", False),
        ("[AUTHOR][LANDING-PAGE] implement the review fixes", False),
    ),
)
def test_public_authority_helper_preserves_pure_semantics(
    title: str, expected: bool
):
    assert policy.is_explicit_authority_card(title) is expected


@pytest.mark.parametrize(("title", "body"), BROAD_EXECUTOR_AUTHORITY_CASES)
def test_broad_executor_prose_is_mixed_not_pure_authority(
    title: str, body: str | None
):
    classification = policy.classify_card(title, body)

    assert classification.executor
    assert not classification.executor_marker
    assert classification.authority
    assert classification.mixed
    assert not classification.pure_authority


@pytest.mark.parametrize(("title", "body"), BROAD_EXECUTOR_AUTHORITY_CASES)
def test_public_authority_helper_rejects_broad_executor_prose(
    title: str, body: str | None
):
    assert not policy.is_explicit_authority_card(title, body)


@pytest.mark.parametrize(
    ("title", "body", "executor", "executor_marker", "authority"),
    CLASSIFICATION_CONTROL_CASES,
)
def test_control_matrix_preserves_classification_and_lifecycle_semantics(
    title: str,
    body: str | None,
    executor: bool,
    executor_marker: bool,
    authority: bool,
):
    classification = policy.classify_card(title, body)
    expected_mixed = executor and authority
    expected_pure_authority = authority and not executor

    assert classification.executor is executor
    assert classification.executor_marker is executor_marker
    assert classification.authority is authority
    assert classification.mixed is expected_mixed
    assert classification.pure_authority is expected_pure_authority
    assert policy.is_explicit_authority_card(title, body) is expected_pure_authority

    create_broker = MemoryBroker()
    if expected_pure_authority:
        result = bridge.create_task(
            create_broker,
            title=title,
            body=body,
            assignee="fable",
            status="blocked",
            created_by="orchestrator",
            priority=0,
            workspace_kind="scratch",
        )
        assert result["assignee"] == "fable"
    else:
        with pytest.raises(policy.AuthorityLaneError):
            bridge.create_task(
                create_broker,
                title=title,
                body=body,
                assignee="fable",
                status="blocked",
                created_by="orchestrator",
                priority=0,
                workspace_kind="scratch",
            )
        assert create_broker.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    explicit_broker = MemoryBroker()
    explicit_broker.seed(
        task_id="t_executor",
        title=title,
        body=body,
        assignee="engineer",
    )
    if expected_pure_authority:
        result = _transition(explicit_broker, assignee="fable")
        assert result["assignee"] == "fable"
    else:
        with pytest.raises(policy.AuthorityLaneError):
            _transition(explicit_broker, assignee="fable")
        row = explicit_broker.get_task("t_executor")
        assert row["status"] == "running"
        assert row["assignee"] == "engineer"

    implicit_broker = MemoryBroker()
    implicit_broker.seed(
        task_id="t_executor",
        title=title,
        body=body,
        assignee="engineer",
    )
    if expected_mixed:
        with pytest.raises(policy.AuthorityLaneError, match="implicit assignee"):
            _transition(implicit_broker)
        row = implicit_broker.get_task("t_executor")
        assert row["status"] == "running"
        assert row["assignee"] == "engineer"
    else:
        result = _transition(implicit_broker)
        assert result["assignee"] == "engineer"
        assert result["to"] == "review"


def test_omitted_review_assignee_preserves_current_executor_with_broker_readback():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[REVIEW][DEFERRED] exact-head security verdict",
        assignee="security",
    )

    result = _transition(broker)

    assert result["to"] == "review"
    assert result["assignee"] == "security"
    assert result["readback"] == {"status": "review", "assignee": "security"}
    assert broker.readbacks == 1


def test_explicit_non_fable_review_route_is_honored():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[REVIEW] implementer-distinct verdict",
        assignee="engineer",
    )

    result = _transition(broker, assignee="security")

    assert result["assignee"] == "security"
    assert broker.get_task("t_executor")["assignee"] == "security"


def test_explicit_non_fable_review_route_wins_for_mixed_title():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[FIX][FABLE][LAND] implement the reviewed repair",
        assignee="engineer",
    )

    result = _transition(broker, assignee="security")

    assert result["assignee"] == "security"
    assert broker.get_task("t_executor")["status"] == "review"


def test_explicit_fable_authority_transition_remains_allowed():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[FABLE][LAND+INSTALL-GATE] reviewed exact-head acceptance",
        assignee="lander-prep",
    )

    result = _transition(broker, assignee="fable")

    assert result["assignee"] == "fable"
    assert broker.get_task("t_executor")["status"] == "review"


def test_executor_transition_to_fable_fails_closed_without_mutation():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[FIX][HERMES] implement review defects",
        assignee="engineer",
    )

    with pytest.raises(policy.AuthorityLaneError, match="executor-shaped work"):
        _transition(broker, assignee="fable")

    row = broker.get_task("t_executor")
    assert row["status"] == "running"
    assert row["assignee"] == "engineer"


@pytest.mark.parametrize("title", MIXED_TITLES)
def test_mixed_transition_to_fable_fails_closed_without_mutation(title: str):
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title=title,
        assignee="engineer",
    )

    with pytest.raises(policy.AuthorityLaneError, match="executor-shaped work"):
        _transition(broker, assignee="fable")

    row = broker.get_task("t_executor")
    assert row["status"] == "running"
    assert row["assignee"] == "engineer"


@pytest.mark.parametrize(("title", "body"), BROAD_EXECUTOR_AUTHORITY_CASES)
def test_broad_executor_transition_to_fable_fails_closed_without_mutation(
    title: str, body: str | None
):
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title=title,
        body=body,
        assignee="engineer",
    )

    with pytest.raises(policy.AuthorityLaneError, match="executor-shaped work"):
        _transition(broker, assignee="fable")

    row = broker.get_task("t_executor")
    assert row["status"] == "running"
    assert row["assignee"] == "engineer"


@pytest.mark.parametrize("title", MIXED_TITLES)
def test_implicit_review_of_mixed_title_fails_closed_without_mutation(title: str):
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title=title,
        assignee="engineer",
    )

    with pytest.raises(policy.AuthorityLaneError, match="implicit assignee"):
        _transition(broker)

    row = broker.get_task("t_executor")
    assert row["status"] == "running"
    assert row["assignee"] == "engineer"


@pytest.mark.parametrize(("title", "body"), BROAD_EXECUTOR_AUTHORITY_CASES)
def test_implicit_review_of_broad_executor_prose_fails_closed_without_mutation(
    title: str, body: str | None
):
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title=title,
        body=body,
        assignee="engineer",
    )

    with pytest.raises(policy.AuthorityLaneError, match="implicit assignee"):
        _transition(broker)

    row = broker.get_task("t_executor")
    assert row["status"] == "running"
    assert row["assignee"] == "engineer"


def test_implicit_review_of_pure_authority_title_preserves_current_assignee():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[FABLE][LAND] exact-head acceptance gate",
        assignee="lander-prep",
    )

    result = _transition(broker)

    assert result["assignee"] == "lander-prep"
    assert broker.get_task("t_executor")["status"] == "review"


def test_bare_gate_marker_does_not_grant_fable_authority():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[REVIEW][GATE] verify the implementation",
        assignee="engineer",
    )

    with pytest.raises(policy.AuthorityLaneError, match="executor-shaped work"):
        _transition(broker, assignee="fable")


def test_authority_action_must_be_a_complete_tag_token():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[AUTHOR][LANDING-PAGE] implement the review fixes",
        assignee="engineer",
    )

    with pytest.raises(policy.AuthorityLaneError, match="executor-shaped work"):
        _transition(broker, assignee="fable")


def test_neutral_title_cannot_bypass_explicit_fable_authority_contract():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="Coordinate the next step",
        assignee="orchestrator",
    )

    with pytest.raises(policy.AuthorityLaneError, match="explicit authority action"):
        _transition(broker, assignee="fable")


def test_bridge_rejects_non_fleet_board_before_transaction():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[REVIEW] exact-head verdict",
        assignee="security",
    )

    with pytest.raises(ValueError, match="fleet-only"):
        _transition(broker, board="default")

    assert not broker._open
    assert broker.get_task("t_executor")["status"] == "running"


def test_create_time_executor_assignment_to_fable_is_rejected():
    broker = MemoryBroker()

    with pytest.raises(policy.AuthorityLaneError, match="task creation rejected"):
        bridge.create_task(
            broker,
            title="[AUTHOR] implement the migration",
            body="Open and verify a pull request.",
            assignee="fable",
            status="blocked",
            created_by="orchestrator",
            priority=0,
            workspace_kind="scratch",
        )

    count = broker.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert count == 0


@pytest.mark.parametrize("title", MIXED_TITLES)
def test_create_time_mixed_assignment_to_fable_is_rejected(title: str):
    broker = MemoryBroker()

    with pytest.raises(policy.AuthorityLaneError, match="executor-shaped work"):
        bridge.create_task(
            broker,
            title=title,
            body="Open and verify a pull request.",
            assignee="fable",
            status="blocked",
            created_by="orchestrator",
            priority=0,
            workspace_kind="scratch",
        )

    assert broker.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize(("title", "body"), BROAD_EXECUTOR_AUTHORITY_CASES)
def test_create_time_broad_executor_assignment_to_fable_is_rejected(
    title: str, body: str | None
):
    broker = MemoryBroker()

    with pytest.raises(policy.AuthorityLaneError, match="executor-shaped work"):
        bridge.create_task(
            broker,
            title=title,
            body=body,
            assignee="fable",
            status="blocked",
            created_by="orchestrator",
            priority=0,
            workspace_kind="scratch",
        )

    assert broker.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_create_cli_returns_guard_exit_without_broker_mutation(capsys):
    broker = MemoryBroker()

    rc = bridge.main(
        [
            "create",
            "--title",
            "[REVIEW] implement the fix",
            "--body",
            "Open a pull request.",
            "--assignee",
            "fable",
        ],
        client=broker,
    )

    assert rc == 3
    assert "authority-lane guard" in capsys.readouterr().err
    assert broker.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_review_cli_returns_guard_exit_for_implicit_mixed_title(capsys):
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[REVIEW][OPERATOR-HOLD] verify implementation",
        assignee="security",
    )

    rc = bridge.main(
        [
            "t_executor",
            "review",
            "--bridge",
            "test-executor",
        ],
        client=broker,
    )

    assert rc == 3
    assert "implicit assignee" in capsys.readouterr().err
    row = broker.get_task("t_executor")
    assert row["status"] == "running"
    assert row["assignee"] == "security"


def test_create_time_explicit_fable_authority_card_is_allowed():
    broker = MemoryBroker()

    result = bridge.create_task(
        broker,
        title="[OPERATOR-GATE][FABLE][APPLY] reviewed exact-head acceptance",
        body="Apply only after exact-head acceptance.",
        assignee="fable",
        status="blocked",
        created_by="orchestrator",
        priority=10,
        workspace_kind="scratch",
    )

    assert result["assignee"] == "fable"
    assert result["readback"] == {"status": "blocked", "assignee": "fable"}


def test_transition_reports_broker_readback_mismatch():
    broker = MemoryBroker()
    broker.seed(
        task_id="t_executor",
        title="[REVIEW] exact-head verdict",
        assignee="security",
    )
    broker.tamper_readback = True

    with pytest.raises(RuntimeError, match="broker readback mismatch"):
        _transition(broker)


WRAPPERS = (
    "kanban_codex_service.sh",
    "kanban_subscription_acp_service.sh",
    "run_kanban_codex_service.sh",
    "run_ef2_post_cursor_verify.sh",
    "run_pr534_node22_verify.sh",
)
HISTORICAL_ONE_OFF_WRAPPERS = (
    "run_ef2_post_cursor_verify.sh",
    "run_pr534_node22_verify.sh",
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_toolchain(tmp_path: Path, branch_name: str) -> tuple[Path, Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_log = tmp_path / "state.jsonl"
    git_count = tmp_path / "git-count"
    _write_executable(
        bin_dir / "state",
        """#!/usr/bin/env python3
import json, os, sys
with open(os.environ['STATE_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
if len(sys.argv) > 2 and os.environ.get('STATE_FAIL_STATE') == sys.argv[2]:
    raise SystemExit(9)
""",
    )
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
if 'rev-parse' in args and args[-1] == 'HEAD':
    counter = pathlib.Path(os.environ['GIT_COUNT'])
    count = int(counter.read_text() or '0') if counter.exists() else 0
    counter.write_text(str(count + 1))
    print('newhead' if os.environ.get('GIT_ALWAYS_NEW') == '1' or count else 'oldhead')
elif 'rev-parse' in args and args[-1] == 'FETCH_HEAD':
    print('newhead')
elif 'branch' in args and '--show-current' in args:
    print(os.environ['FAKE_BRANCH'])
elif 'ls-remote' in args:
    print('newhead\\trefs/heads/' + os.environ['FAKE_BRANCH'])
elif 'rev-list' in args:
    print('1')
""",
    )
    _write_executable(
        bin_dir / "gh",
        """#!/usr/bin/env sh
printf '%s\\n' 'https://github.com/o269/example/pull/1'
""",
    )
    _write_executable(
        bin_dir / "timeout",
        """#!/usr/bin/env sh
shift
exec "$@"
""",
    )
    _write_executable(
        bin_dir / "bridge",
        """#!/usr/bin/env sh
if [ -n "${BRIDGE_MARKER:-}" ]; then : > "$BRIDGE_MARKER"; fi
printf '%s\\n' '{"ok":true}'
""",
    )
    for name in ("acp-verify", "npm", "npx", "corepack", "node"):
        _write_executable(
            bin_dir / name,
            "#!/usr/bin/env sh\nprintf '%s\\n' '{\"ok\":true}'\n",
        )
    return bin_dir, state_log, git_count, bin_dir / "bridge"


def _wrapper_command(
    wrapper: str,
    *,
    tmp_path: Path,
    worktree: Path,
    request: Path,
    output: Path,
    error: Path,
    bridge_bin: Path,
) -> list[str]:
    path = str(ASSET_DIR / wrapper)
    common = [
        "--task", "t_executor",
        "--worktree", str(worktree),
        "--branch", "fix/test",
        "--request", str(request),
        "--output", str(output),
        "--error", str(error),
        "--repo", "o269/example",
        "--model-timeout", "1",
    ]
    if wrapper == "kanban_codex_service.sh":
        return [path, *common]
    if wrapper == "kanban_subscription_acp_service.sh":
        return [
            path,
            *common,
            "--bridge-label", "kimi-cli-acp-bridge",
            "--bridge-bin", str(bridge_bin),
            "--timeout-env", "KIMI_TIMEOUT",
        ]
    if wrapper == "run_kanban_codex_service.sh":
        return [
            path,
            "t_executor",
            str(worktree),
            "fix/test",
            str(request),
            str(output),
            str(error),
            "1",
            "5",
            "true",
        ]
    return [path]


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_every_generic_wrapper_preserves_executor_on_review(
    tmp_path: Path,
    wrapper: str,
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    request = tmp_path / "request.jsonl"
    request.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "output.jsonl"
    error = tmp_path / "error.log"
    acp_output = tmp_path / "acp-output.jsonl"
    acp_output.write_text("{}\n", encoding="utf-8")
    bin_dir, state_log, git_count, bridge_bin = _fake_toolchain(
        tmp_path, "fix/test"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "KANBAN_BRIDGE_STATE": str(bin_dir / "state"),
            "KANBAN_BRIDGE_BIN": str(bridge_bin),
            "KANBAN_ACP_VERIFY": str(bin_dir / "acp-verify"),
            "KANBAN_TASK": "t_executor",
            "KANBAN_WORKTREE": str(worktree),
            "KANBAN_BRANCH": "fix/test",
            "KANBAN_VERIFY_LOG": str(tmp_path / "verify.log"),
            "KANBAN_ACP_OUTPUT": str(acp_output),
            "KANBAN_PR_JSON": str(tmp_path / "pr.json"),
            "KANBAN_PR_CHECKS": str(tmp_path / "checks.txt"),
            "KANBAN_VERIFY_PATH": f"{bin_dir}:/usr/bin:/bin",
            "STATE_LOG": str(state_log),
            "GIT_COUNT": str(git_count),
            "FAKE_BRANCH": "fix/test",
        }
    )
    if wrapper in {"run_ef2_post_cursor_verify.sh", "run_pr534_node22_verify.sh"}:
        env["GIT_ALWAYS_NEW"] = "1"

    completed = subprocess.run(
        _wrapper_command(
            wrapper,
            tmp_path=tmp_path,
            worktree=worktree,
            request=request,
            output=output,
            error=error,
            bridge_bin=bridge_bin,
        ),
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, (
        f"{wrapper} failed\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    calls = [json.loads(line) for line in state_log.read_text().splitlines()]
    review_calls = [call for call in calls if len(call) > 1 and call[1] == "review"]
    assert len(review_calls) == 1
    assert all("--assignee" not in call for call in calls)
    assert all("fable" not in call for call in calls)


def test_wrapper_stops_before_executor_when_running_transition_is_rejected(
    tmp_path: Path,
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    request = tmp_path / "request.jsonl"
    request.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "output.jsonl"
    error = tmp_path / "error.log"
    marker = tmp_path / "bridge-ran"
    bin_dir, state_log, git_count, bridge_bin = _fake_toolchain(
        tmp_path, "fix/test"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "KANBAN_BRIDGE_STATE": str(bin_dir / "state"),
            "KANBAN_BRIDGE_BIN": str(bridge_bin),
            "KANBAN_ACP_VERIFY": str(bin_dir / "acp-verify"),
            "STATE_LOG": str(state_log),
            "STATE_FAIL_STATE": "running",
            "BRIDGE_MARKER": str(marker),
            "GIT_COUNT": str(git_count),
            "FAKE_BRANCH": "fix/test",
        }
    )

    completed = subprocess.run(
        _wrapper_command(
            "kanban_codex_service.sh",
            tmp_path=tmp_path,
            worktree=worktree,
            request=request,
            output=output,
            error=error,
            bridge_bin=bridge_bin,
        ),
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 9
    assert not marker.exists()
    calls = [json.loads(line) for line in state_log.read_text().splitlines()]
    assert [call[1] for call in calls] == ["running"]


@pytest.mark.parametrize("wrapper", HISTORICAL_ONE_OFF_WRAPPERS)
def test_historical_wrapper_requires_task_before_state_bridge(
    tmp_path: Path, wrapper: str
):
    state_marker = tmp_path / "state-invoked"
    state = tmp_path / "state"
    _write_executable(
        state,
        "#!/usr/bin/env sh\n: > \"$STATE_MARKER\"\nexit 99\n",
    )
    env = os.environ.copy()
    env.pop("KANBAN_TASK", None)
    env.update(
        {
            "KANBAN_BRIDGE_STATE": str(state),
            "STATE_MARKER": str(state_marker),
        }
    )

    completed = subprocess.run(
        [str(ASSET_DIR / wrapper)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 64
    assert "missing required KANBAN_TASK" in completed.stderr
    assert not state_marker.exists()


def test_installer_excludes_historical_one_off_wrappers():
    installed_sources = {
        source.name for source, _target, _mode in installer.INSTALL_MANIFEST
    }

    assert installed_sources.isdisjoint(HISTORICAL_ONE_OFF_WRAPPERS)


def test_installer_dry_run_install_and_check_round_trip(tmp_path: Path):
    hermes_home = tmp_path / "hermes-home"

    dry_receipts, dry_current = installer.install(
        hermes_home, dry_run=True, check=False
    )
    assert not dry_current
    assert all(receipt["action"] == "would-install" for receipt in dry_receipts)
    assert not hermes_home.exists()

    installed, was_current = installer.install(
        hermes_home, dry_run=False, check=False
    )
    assert not was_current
    assert all(receipt["action"] == "created" for receipt in installed)

    checked, all_current = installer.install(
        hermes_home, dry_run=False, check=True
    )
    assert all_current
    assert all(receipt["action"] == "current" for receipt in checked)
    executable = hermes_home / "scripts" / "kanban_bridge_state.py"
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755
