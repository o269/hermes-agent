from __future__ import annotations

import sys
from pathlib import Path as _Path

_HELPER_DIR = _Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "merge-truth-reconciler"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

import pytest

import merge_truth_reconciler as reconciler
from merge_truth_reconciler import (
    AdmissionResult,
    BoardAdapter,
    BoardFailure,
    CapabilityBlocked,
    CardSnapshot,
    Config,
    ConfigError,
    FeedFailure,
    FeedResult,
    GitHubFeed,
    GuardResult,
    HttpResponse,
    InventoryPayload,
    MergeRecord,
    ReconcilerError,
    ReconcilerState,
    execute_tick,
    extract_inventory_pr_urls,
    probe_host_admission,
    summary_line,
)

PR_URL = "https://github.com/acme/widget/pull/42"
UNKNOWN_URL = "https://github.com/acme/widget/pull/999"
MERGED_AT = "2026-08-05T10:00:00Z"
NOW = datetime(2026, 8, 5, 11, 0, tzinfo=timezone.utc)


def merge_record() -> MergeRecord:
    return MergeRecord(
        canonical_url=PR_URL,
        repository="acme/widget",
        number=42,
        merge_sha="abc123def456",
        merged_at=MERGED_AT,
    )


def make_config(tmp_path: Path, *, enabled: bool = True) -> Config:
    artifacts = tmp_path / "source-artifacts"
    artifacts.mkdir(parents=True)
    state_dir = tmp_path / "state"
    report_dir = tmp_path / "reports"
    ticker = tmp_path / "bus" / "STATUS-TICKER.md"
    alert_dir = tmp_path / "bus" / "to-claude"
    for directory in (state_dir, report_dir, ticker.parent, alert_dir):
        directory.mkdir(parents=True, exist_ok=True)
    ticker.touch(mode=0o600)
    return Config(
        enabled=enabled,
        artifacts_dir=artifacts,
        state_dir=state_dir,
        report_dir=report_dir,
        status_ticker=ticker,
        alert_dir=alert_dir,
        sdb_mount=tmp_path,
        load1_max=1_000_000,
        sdb_min_free_kib=1,
        inventory_ttl_seconds=86_400,
        bootstrap_days=30,
        overlap_seconds=600,
        request_timeout_seconds=2,
        helper_timeout_seconds=2,
        max_pages_per_repo=3,
    )


def card_snapshot(
    task_id: str,
    title: str,
    body: str,
    comments: tuple[str, ...] = (),
    status: str = "blocked",
    assignee: str = "",
    *,
    terminal: bool = False,
    protected_custody: bool = False,
    active_run: bool = False,
    skills: tuple[str, ...] = (),
) -> CardSnapshot:
    return CardSnapshot(
        task_id=task_id,
        title=title,
        body=body,
        comments=comments,
        status=status,
        terminal=terminal,
        protected_custody=protected_custody,
        active_run=active_run,
        assignee=assignee,
        skills=skills,
    )


class FixedFeed:
    def __init__(self, merges: Sequence[MergeRecord] = ()) -> None:
        self.merges = tuple(merges)
        self.calls = 0
        self.cutoffs: list[datetime] = []

    def fetch(
        self,
        repositories: Sequence[str],
        cutoff: datetime,
        state: ReconcilerState,
    ) -> FeedResult:
        self.calls += 1
        self.cutoffs.append(cutoff)
        assert repositories == ("acme/widget",)
        return FeedResult(self.merges, api_calls=1, not_modified=0)


class FailingFeed(FixedFeed):
    def fetch(
        self,
        repositories: Sequence[str],
        cutoff: datetime,
        state: ReconcilerState,
    ) -> FeedResult:
        self.calls += 1
        raise FeedFailure("complete feed unavailable")


class RecordingRepositoriesFeed:
    def __init__(self) -> None:
        self.repositories: list[tuple[str, ...]] = []

    def fetch(
        self,
        repositories: Sequence[str],
        cutoff: datetime,
        state: ReconcilerState,
    ) -> FeedResult:
        self.repositories.append(tuple(repositories))
        return FeedResult((), api_calls=0, not_modified=0)


class MemoryBoard(BoardAdapter):
    def __init__(
        self,
        cards: Sequence[CardSnapshot] = (),
        *,
        promotion_count: int = 0,
    ) -> None:
        self.cards = {card.task_id: card for card in cards}
        self.statuses = {card.task_id: card.status for card in cards}
        self.promotion_count = promotion_count
        self.validations = 0
        self.inventory_calls = 0
        self.guard_calls = 0
        self.guard_operations: set[str] = set()
        self.complete_operations: set[str] = set()
        self.evidence_operations: set[str] = set()
        self.complete_receipts: dict[str, str] = {}
        self.evidence_receipts: dict[str, str] = {}
        self.promotion_calls = 0

    def validate_capabilities(self) -> None:
        self.validations += 1

    def inventory_payload(self) -> InventoryPayload:
        self.inventory_calls += 1
        return InventoryPayload(canonical_urls=(PR_URL,))

    def converge_ownership(self, merge: MergeRecord, operation_id: str) -> GuardResult:
        self.guard_calls += 1
        if operation_id in self.guard_operations:
            return GuardResult(0, (), True)
        self.guard_operations.add(operation_id)
        return GuardResult(2, ("t_owner",), True)

    def list_cards_citing(self, canonical_urls: Sequence[str]) -> Sequence[CardSnapshot]:
        return tuple(
            replace(card, status=self.statuses[card.task_id]) for card in self.cards.values()
        )

    def complete_gate_card(self, task_id: str, receipt: str, operation_id: str) -> bool:
        if operation_id in self.complete_operations:
            return False
        self.complete_operations.add(operation_id)
        self.statuses[task_id] = "done"
        self.complete_receipts[task_id] = receipt
        return True

    def add_evidence_comment(self, task_id: str, receipt: str, operation_id: str) -> bool:
        if operation_id in self.evidence_operations:
            return False
        self.evidence_operations.add(operation_id)
        self.evidence_receipts[task_id] = receipt
        return True

    def recompute_ready(self, operation_id: str) -> int:
        self.promotion_calls += 1
        return self.promotion_count


class ExplodingBoard(MemoryBoard):
    def validate_capabilities(self) -> None:
        raise AssertionError("disabled ticks must not access the board")


class BoardReadReached(MemoryBoard):
    def validate_capabilities(self) -> None:
        self.validations += 1
        raise BoardFailure("board-read-phase-reached")


class ExplodingFeed(FixedFeed):
    def fetch(
        self,
        repositories: Sequence[str],
        cutoff: datetime,
        state: ReconcilerState,
    ) -> FeedResult:
        raise AssertionError("disabled ticks must not access GitHub")


class MalformedCardBoard(MemoryBoard):
    def list_cards_citing(self, canonical_urls: Sequence[str]) -> Sequence[CardSnapshot]:
        raise BoardFailure("invalid hex card row")


def test_watermark_replay_is_idempotent_and_feed_failure_does_not_advance(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    board = MemoryBoard()
    first = execute_tick(config, board, FixedFeed((merge_record(),)), NOW)
    assert first.status == "ok"
    assert first.ownership_rows_cleared == 2
    assert first.watermark_after == "2026-08-05T11:00:00Z"

    failed = execute_tick(
        config,
        board,
        FailingFeed(),
        NOW + timedelta(minutes=2),
    )
    assert failed.status == "error"
    assert board.guard_calls == 1
    state_after_failure = ReconcilerState.load(config.state_file)
    assert state_after_failure.watermark == "2026-08-05T11:00:00Z"

    replay = execute_tick(
        config,
        board,
        FixedFeed((merge_record(),)),
        NOW + timedelta(minutes=4),
    )
    assert replay.status == "ok"
    assert replay.ownership_rows_cleared == 0
    assert len(board.guard_operations) == 1


class SQLiteGuardBoard(MemoryBoard):
    def __init__(self, path: Path, allowed_root: Path) -> None:
        super().__init__()
        assert path.resolve().is_relative_to(allowed_root.resolve())
        self.path = path
        self.semantic_failure = False
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                CREATE TABLE ownership (
                    id INTEGER PRIMARY KEY,
                    canonical_url TEXT NOT NULL,
                    declared INTEGER NOT NULL,
                    expired INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE operations (operation_id TEXT PRIMARY KEY);
                """
            )
            connection.executemany(
                "INSERT INTO ownership(canonical_url, declared) VALUES (?, ?)",
                [(PR_URL, 1), (PR_URL, 0)],
            )

    def converge_ownership(self, merge: MergeRecord, operation_id: str) -> GuardResult:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if existing:
                connection.rollback()
                return GuardResult(0, (), True)
            cursor = connection.execute(
                "UPDATE ownership SET expired = 1 WHERE canonical_url = ? AND expired = 0",
                (merge.canonical_url,),
            )
            active = connection.execute(
                "SELECT COUNT(*) FROM ownership WHERE canonical_url = ? AND expired = 0",
                (merge.canonical_url,),
            ).fetchone()[0]
            if self.semantic_failure or active:
                raise CapabilityBlocked("semantic verification failed")
            connection.execute(
                "INSERT INTO operations(operation_id) VALUES (?)", (operation_id,)
            )
            connection.commit()
            return GuardResult(cursor.rowcount, ("t_owner",), True)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def active_rows(self) -> list[tuple[int, int]]:
        with sqlite3.connect(self.path) as connection:
            return connection.execute(
                "SELECT declared, expired FROM ownership ORDER BY declared DESC"
            ).fetchall()


def test_guard_adapter_expires_declared_and_referenced_rows_and_rolls_back(
    tmp_path: Path,
) -> None:
    board = SQLiteGuardBoard(tmp_path / "guard-test.db", tmp_path)
    operation_id = "test-op-id"
    board.semantic_failure = True
    with pytest.raises(CapabilityBlocked):
        board.converge_ownership(merge_record(), operation_id)
    assert board.active_rows() == [(1, 0), (0, 0)]

    board.semantic_failure = False
    result = board.converge_ownership(merge_record(), operation_id)
    assert result.cleared_rows == 2
    assert result.semantic_released is True
    assert board.active_rows() == [(1, 1), (0, 1)]
    assert board.converge_ownership(merge_record(), operation_id).cleared_rows == 0


def test_exact_tier_a_gate_completes_with_receipt_and_promotes_actual_count(
    tmp_path: Path,
) -> None:
    card = card_snapshot(
        task_id="t_exact",
        title="Await dependency",
        body=f"Context is allowed.\n  GATE: PR-MERGE {PR_URL}  \nMore context.",
        comments=(),
        status="blocked",
    )
    board = MemoryBoard((card,), promotion_count=3)
    report = execute_tick(make_config(tmp_path), board, FixedFeed((merge_record(),)), NOW)
    assert report.cards_closed == ["t_exact"]
    assert board.statuses["t_exact"] == "done"
    assert "MERGE-RECEIPT" in board.complete_receipts["t_exact"]
    assert f"URL={PR_URL}" in board.complete_receipts["t_exact"]
    assert "SHA=abc123def456" in board.complete_receipts["t_exact"]
    assert "author=merge-truth-reconciler" in board.complete_receipts["t_exact"]
    assert report.children_promoted == 3
    assert board.promotion_calls == 1


def test_legacy_tier_b_never_completes_and_evidence_is_idempotent(
    tmp_path: Path,
) -> None:
    card = card_snapshot(
        task_id="t_legacy",
        title="Await land",
        body=f"Blocked awaiting merge of {PR_URL}",
        comments=(),
        status="blocked",
    )
    board = MemoryBoard((card,))
    config = make_config(tmp_path)
    first = execute_tick(config, board, FixedFeed((merge_record(),)), NOW)
    second = execute_tick(
        config, board, FixedFeed((merge_record(),)), NOW + timedelta(minutes=2)
    )
    assert first.cards_closed == []
    assert second.cards_closed == []
    assert board.statuses["t_legacy"] == "blocked"
    assert len(board.evidence_operations) == 1
    assert len(first.evidence_comments) == 1
    assert second.evidence_comments == []
    assert first.stale_gate_items[0]["task_id"] == "t_legacy"


def test_security_fable_operator_and_running_cards_receive_evidence_only(
    tmp_path: Path,
) -> None:
    gate = f"GATE: PR-MERGE {PR_URL}"
    cards = (
        card_snapshot("t_security", "Security review", gate, assignee="security"),
        card_snapshot(
            "t_fable", "Terminal custody", gate, assignee="fable", protected_custody=True
        ),
        card_snapshot("t_operator", "OPERATOR HOLD", gate, assignee="data"),
        card_snapshot(
            "t_running",
            "Active worker",
            gate,
            status="running",
            assignee="data",
            active_run=True,
        ),
    )
    board = MemoryBoard(cards)
    report = execute_tick(make_config(tmp_path), board, FixedFeed((merge_record(),)), NOW)
    assert report.cards_closed == []
    assert set(board.evidence_receipts) == {card.task_id for card in cards}
    assert {item["reason"] for item in report.exclusions} == {
        "security-scope",
        "protected-custody",
        "operator-hold",
        "active-run",
    }
    assert all(board.statuses[card.task_id] == card.status for card in cards)


def test_canonical_failure_custody_and_active_run_authority_never_promotes(
    tmp_path: Path,
) -> None:
    gate = f"GATE: PR-MERGE {PR_URL}"
    cards = (
        card_snapshot("t_failed", "Failed card", gate, status="failed", terminal=True),
        card_snapshot(
            "t_lander",
            "Lander lane",
            gate,
            assignee="lander-prep",
            protected_custody=True,
        ),
        card_snapshot(
            "t_active_drift",
            "Drifted active run",
            gate,
            status="blocked",
            assignee="data",
            active_run=True,
        ),
    )
    board = MemoryBoard(cards, promotion_count=9)
    report = execute_tick(make_config(tmp_path), board, FixedFeed((merge_record(),)), NOW)
    assert report.cards_closed == []
    assert report.children_promoted == 0
    assert board.promotion_calls == 0
    assert set(board.evidence_receipts) == {card.task_id for card in cards}
    assert {item["reason"] for item in report.exclusions} == {
        "terminal-status",
        "protected-custody",
        "active-run",
    }
    assert all(board.statuses[card.task_id] == card.status for card in cards)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-terminal",
        "string-terminal",
        "integer-protected",
        "missing-active",
        "failed-not-terminal",
        "running-not-active",
        "terminal-and-active",
    ),
)
def test_helper_card_authority_contract_fails_closed(mutation: str) -> None:
    payload: dict[str, object] = {
        "task_id": "t_authority",
        "title": "Authority",
        "body": f"GATE: PR-MERGE {PR_URL}",
        "comments": [],
        "status": "blocked",
        "terminal": False,
        "protected_custody": False,
        "active_run": False,
    }
    if mutation == "missing-terminal":
        del payload["terminal"]
    elif mutation == "string-terminal":
        payload["terminal"] = "false"
    elif mutation == "integer-protected":
        payload["protected_custody"] = 0
    elif mutation == "missing-active":
        del payload["active_run"]
    elif mutation == "failed-not-terminal":
        payload["status"] = "failed"
    elif mutation == "running-not-active":
        payload["status"] = "running"
    elif mutation == "terminal-and-active":
        payload["terminal"] = True
        payload["active_run"] = True
    with pytest.raises(BoardFailure, match="authority|contradicted|terminal and active"):
        CardSnapshot.from_dict(payload)


def test_invalid_helper_authority_precedes_board_mutation(tmp_path: Path) -> None:
    class InvalidAuthorityBoard(MemoryBoard):
        def list_cards_citing(
            self, canonical_urls: Sequence[str]
        ) -> Sequence[CardSnapshot]:
            CardSnapshot.from_dict(
                {
                    "task_id": "t_invalid",
                    "status": "blocked",
                    "body": f"GATE: PR-MERGE {PR_URL}",
                    "terminal": False,
                    "protected_custody": False,
                }
            )
            raise AssertionError("unreachable")

    board = InvalidAuthorityBoard()
    report = execute_tick(make_config(tmp_path), board, FixedFeed((merge_record(),)), NOW)
    assert report.status == "error"
    assert board.guard_calls == 0
    assert board.complete_operations == set()
    assert report.watermark_after is None


@pytest.mark.parametrize(
    "body",
    [
        f"GATE:  PR-MERGE {PR_URL}",
        f"GATE: PR-MERGE {PR_URL}\nGATE: MANUAL operator",
        f"GATE: PR-MERGE {PR_URL}\nGATE: PR-MERGE {UNKNOWN_URL}",
        f"GATE: PR-MERGE {UNKNOWN_URL}",
    ],
)
def test_malformed_mixed_unknown_and_unmerged_gates_do_not_complete(
    tmp_path: Path, body: str
) -> None:
    card = card_snapshot("t_mixed", "Dependency", body)
    board = MemoryBoard((card,))
    report = execute_tick(make_config(tmp_path), board, FixedFeed((merge_record(),)), NOW)
    assert report.cards_closed == []
    assert board.statuses["t_mixed"] == "blocked"


def test_kill_switch_prevents_github_board_and_state_access(tmp_path: Path) -> None:
    config = make_config(tmp_path, enabled=False)
    report = execute_tick(config, ExplodingBoard(), ExplodingFeed(), NOW)
    assert report.status == "disabled"
    assert not config.state_file.exists()
    assert list(config.report_dir.iterdir()) == []
    assert config.status_ticker.read_text() == ""


def test_inventory_supports_full_urls_and_split_repo_pr_receipts() -> None:
    text = "\n".join(
        (
            "landed: https://github.com/Acme/Widget/pull/42",
            "repo: Other/Service",
            "pr: #17",
            "repository: Third/Tool",
            "PR: 9",
        )
    )
    assert extract_inventory_pr_urls(text) == {
        PR_URL,
        "https://github.com/other/service/pull/17",
        "https://github.com/third/tool/pull/9",
    }

    snapshot = CardSnapshot.from_dict(
        {
            "task_id": "t_legacy_bytes",
            "title": "Legacy row",
            "body_hex": (f"Blocked by {PR_URL}".encode() + b"\xff").hex(),
            "comments_hex": [("await merge " + PR_URL).encode().hex()],
            "status": "blocked",
            "terminal": False,
            "protected_custody": False,
            "active_run": False,
        }
    )
    assert PR_URL in snapshot.body
    assert "\ufffd" in snapshot.body
    assert PR_URL in snapshot.comments[0]


def test_live_board_inventory_is_queried_and_additive_before_artifact_cache_expiry(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    board = MemoryBoard()
    feed = RecordingRepositoriesFeed()
    first = execute_tick(config, board, feed, NOW)
    assert first.status == "ok"
    assert feed.repositories == [("acme/widget",)]

    def expanded_inventory() -> InventoryPayload:
        board.inventory_calls += 1
        return InventoryPayload(
            canonical_urls=(PR_URL, "https://github.com/new/repo/pull/7")
        )

    board.inventory_payload = expanded_inventory  # type: ignore[method-assign]
    second = execute_tick(config, board, feed, NOW + timedelta(minutes=2))
    assert second.status == "ok"
    assert second.artifact_cache_hit is True
    assert feed.repositories[-1] == ("acme/widget", "new/repo")
    assert board.inventory_calls == 2


def test_artifact_inventory_cap_fails_closed_after_live_board_query(
    tmp_path: Path,
) -> None:
    config = replace(make_config(tmp_path), artifact_max_candidates=1)
    (config.artifacts_dir / "merge-receipt-one.md").write_text(PR_URL)
    (config.artifacts_dir / "merge-receipt-two.md").write_text(PR_URL)
    board = MemoryBoard()
    feed = RecordingRepositoriesFeed()
    report = execute_tick(config, board, feed, NOW)
    assert report.status == "error"
    assert report.watermark_after is None
    assert board.inventory_calls == 1
    assert feed.repositories == []
    assert board.guard_calls == 0
    assert "candidate-file limit" in report.errors[0]


def test_report_retention_is_bounded_and_preserves_latest(tmp_path: Path) -> None:
    config = replace(
        make_config(tmp_path), report_retention_count=2, report_retention_days=30
    )
    for stamp in (
        "20200101T000000Z",
        "20260801T000000Z",
        "20260802T000000Z",
        "20260803T000000Z",
    ):
        (config.report_dir / f"{stamp}.json").write_text("{}\n")
        (config.report_dir / f"{stamp}.md").write_text("old\n")
    (config.report_dir / "latest.json").write_text('{"sentinel":true}\n')

    report = execute_tick(config, MemoryBoard(), FixedFeed(), NOW)
    assert report.status == "ok"
    assert report.reports_pruned == 6
    timestamped = [
        path
        for path in config.report_dir.iterdir()
        if reconciler.TIMESTAMPED_REPORT_RE.fullmatch(path.name)
    ]
    assert len(timestamped) == 4
    assert (config.report_dir / "latest.json").is_file()
    assert json.loads((config.report_dir / "latest.json").read_text())["status"] == "ok"


def test_enabled_tick_fails_before_board_io_when_bootstrap_path_is_missing(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    config.status_ticker.unlink()
    board = ExplodingBoard()
    report = execute_tick(config, board, ExplodingFeed(), NOW)
    assert report.status == "error"
    assert "ticker file is missing" in report.errors[0]
    assert board.validations == 0
    assert not config.state_file.exists()


def test_read_only_source_artifacts_reach_board_read_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    config.artifacts_dir.chmod(0o500)
    real_access = reconciler.os.access

    def access(path: Path, mode: int) -> bool:
        if Path(path) == config.artifacts_dir and mode & reconciler.os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(reconciler.os, "access", access)
    board = BoardReadReached()
    report = execute_tick(config, board, ExplodingFeed(), NOW)
    assert report.status == "error"
    assert report.errors == ["board-read-phase-reached"]
    assert board.validations == 1


@pytest.mark.parametrize(
    "admission",
    [
        AdmissionResult(False, "load1-above-max", 12.01, 20_000_000),
        AdmissionResult(False, "sdb-free-below-min", 1.0, 15_728_639),
        AdmissionResult(False, "load1-unavailable,sdb-free-unavailable", None, None),
    ],
)
def test_admission_skip_is_fail_closed_before_board_github_and_writes(
    tmp_path: Path, admission: AdmissionResult
) -> None:
    config = make_config(tmp_path)
    report = execute_tick(
        config,
        ExplodingBoard(),
        ExplodingFeed(),
        NOW,
        admission_probe=lambda _config: admission,
    )
    assert report.status == "admission-skip"
    assert report.admission["reason"] == admission.reason
    assert not config.state_file.exists()
    assert list(config.report_dir.iterdir()) == []


def test_environment_cannot_weaken_load_or_sdb_safety_thresholds(tmp_path: Path) -> None:
    base = {"HOME": str(tmp_path), "MERGE_TRUTH_RECONCILER_ENABLED": "1"}
    with pytest.raises(ReconcilerError, match="safety ceiling"):
        Config.from_env({**base, "MERGE_TRUTH_RECONCILER_LOAD1_MAX": "12.01"})
    with pytest.raises(ReconcilerError, match="safety floor"):
        Config.from_env(
            {**base, "MERGE_TRUTH_RECONCILER_SDB_MIN_FREE_KIB": "15728639"}
        )
    for key, value in (
        ("MERGE_TRUTH_RECONCILER_ARTIFACT_MAX_CANDIDATES", "501"),
        ("MERGE_TRUTH_RECONCILER_ARTIFACT_MAX_FILE_BYTES", "1000001"),
        ("MERGE_TRUTH_RECONCILER_ARTIFACT_MAX_TOTAL_BYTES", "20000001"),
        ("MERGE_TRUTH_RECONCILER_ARTIFACT_MAX_ELAPSED_MS", "5001"),
        ("MERGE_TRUTH_RECONCILER_REPORT_RETENTION_COUNT", "101"),
        ("MERGE_TRUTH_RECONCILER_REPORT_RETENTION_DAYS", "31"),
    ):
        with pytest.raises(ReconcilerError, match="safety ceiling"):
            Config.from_env({**base, key: value})


@pytest.mark.parametrize(
    ("key", "attribute", "maximum", "tightened"),
    (
        (
            "MERGE_TRUTH_RECONCILER_INVENTORY_TTL_SECONDS",
            "inventory_ttl_seconds",
            86_400,
            3_600,
        ),
        ("MERGE_TRUTH_RECONCILER_BOOTSTRAP_DAYS", "bootstrap_days", 30, 7),
        ("MERGE_TRUTH_RECONCILER_OVERLAP_SECONDS", "overlap_seconds", 600, 120),
        (
            "MERGE_TRUTH_RECONCILER_REQUEST_TIMEOUT_SECONDS",
            "request_timeout_seconds",
            20,
            10,
        ),
        (
            "MERGE_TRUTH_RECONCILER_HELPER_TIMEOUT_SECONDS",
            "helper_timeout_seconds",
            30,
            15,
        ),
        ("MERGE_TRUTH_RECONCILER_MAX_PAGES_PER_REPO", "max_pages_per_repo", 20, 5),
    ),
)
def test_environment_growth_controls_have_hard_maxima(
    tmp_path: Path, key: str, attribute: str, maximum: int, tightened: int
) -> None:
    base = {"HOME": str(tmp_path), "MERGE_TRUTH_RECONCILER_ENABLED": "1"}
    assert getattr(Config.from_env(base), attribute) == maximum
    assert getattr(Config.from_env({**base, key: str(tightened)}), attribute) == tightened
    for invalid in ("0", "-1", str(maximum + 1)):
        with pytest.raises(ConfigError, match=key):
            Config.from_env({**base, key: invalid})


def test_host_admission_exact_boundaries_and_fail_closed_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        make_config(tmp_path),
        load1_max=12.0,
        sdb_min_free_kib=15_728_640,
    )

    class Stat:
        f_bavail = 15_728_640
        f_frsize = 1024

    monkeypatch.setattr("merge_truth_reconciler.os.getloadavg", lambda: (12.0, 1.0, 1.0))
    monkeypatch.setattr("merge_truth_reconciler.os.statvfs", lambda _path: Stat())
    assert probe_host_admission(config).allowed is True

    monkeypatch.setattr("merge_truth_reconciler.os.getloadavg", lambda: (12.01, 1.0, 1.0))
    assert probe_host_admission(config).reason == "load1-above-max"

    def unavailable() -> tuple[float, float, float]:
        raise OSError("unavailable")

    monkeypatch.setattr("merge_truth_reconciler.os.getloadavg", unavailable)
    monkeypatch.setattr(
        "merge_truth_reconciler.os.statvfs",
        lambda _path: (_ for _ in ()).throw(OSError("unavailable")),
    )
    failed = probe_host_admission(config)
    assert failed.allowed is False
    assert failed.reason == "load1-unavailable,sdb-free-unavailable"


def test_card_parse_ambiguity_precedes_all_board_mutation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    board = MalformedCardBoard()
    report = execute_tick(config, board, FixedFeed((merge_record(),)), NOW)
    assert report.status == "error"
    assert report.watermark_after is None
    assert board.guard_calls == 0


class RecordingTransport:
    def __init__(self, responses: Sequence[HttpResponse]) -> None:
        self.responses = list(responses)
        self.headers: list[dict[str, str]] = []

    def request(
        self, url: str, headers: Mapping[str, str], timeout: int
    ) -> HttpResponse:
        self.headers.append(dict(headers))
        return self.responses.pop(0)


def test_etag_304_reuses_cached_page_and_watermark_uses_overlap(tmp_path: Path) -> None:
    row = {
        "number": 42,
        "html_url": PR_URL,
        "updated_at": "2026-08-05T10:30:00Z",
        "merged_at": MERGED_AT,
        "merge_commit_sha": "abc123def456",
    }
    transport = RecordingTransport(
        (
            HttpResponse(200, {"ETag": '"page-v1"'}, json.dumps([row]).encode()),
            HttpResponse(304, {}, b""),
        )
    )
    feed = GitHubFeed(
        "https://api.github.test",
        timeout_seconds=2,
        max_pages_per_repo=2,
        token_provider=lambda: "in-memory-test-token",
        transport=transport,
    )
    state = ReconcilerState()
    cutoff = NOW - timedelta(days=1)
    first = feed.fetch(("acme/widget",), cutoff, state)
    second = feed.fetch(("acme/widget",), cutoff, state)
    assert first.merges == second.merges == (merge_record(),)
    assert second.not_modified == 1
    assert "If-None-Match" not in transport.headers[0]
    assert transport.headers[1]["If-None-Match"] == '"page-v1"'
    assert "in-memory-test-token" not in json.dumps(state.etag_cache)

    config = make_config(tmp_path)
    board = MemoryBoard()
    execute_tick(config, board, FixedFeed((merge_record(),)), NOW)
    recording_feed = FixedFeed()
    execute_tick(config, board, recording_feed, NOW + timedelta(minutes=2))
    assert recording_feed.cutoffs == [NOW - timedelta(seconds=600)]


def test_state_save_failure_returns_error_without_false_success_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)

    def fail_save(_state: ReconcilerState, _path: Path) -> None:
        raise OSError("fault injected state save")

    monkeypatch.setattr(ReconcilerState, "save", fail_save)
    report = execute_tick(config, MemoryBoard(), FixedFeed((merge_record(),)), NOW)
    assert report.status == "error"
    assert report.watermark_after is None
    assert not config.state_file.exists()
    latest = json.loads((config.report_dir / "latest.json").read_text())
    assert latest["status"] == "error"
    assert latest["watermark_after"] is None


def test_report_failure_keeps_prior_latest_after_durable_state_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    first = execute_tick(config, MemoryBoard(), FixedFeed(), NOW)
    assert first.status == "ok"
    prior_latest = (config.report_dir / "latest.json").read_text()
    original_atomic_write = reconciler.atomic_write_text
    failed_name = "20260805T110200Z.md"

    def fail_timestamped_markdown(path: Path, content: str, mode: int) -> None:
        if path.name == failed_name:
            raise OSError("fault injected report write")
        original_atomic_write(path, content, mode)

    monkeypatch.setattr(reconciler, "atomic_write_text", fail_timestamped_markdown)
    report = execute_tick(
        config, MemoryBoard(), FixedFeed(), NOW + timedelta(minutes=2)
    )
    assert report.status == "error"
    assert ReconcilerState.load(config.state_file).watermark == "2026-08-05T11:02:00Z"
    assert (config.report_dir / "latest.json").read_text() == prior_latest


def test_alert_file_failure_retries_from_durable_outbox_without_ticker_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    card = card_snapshot("t_alert_file", "Await merge", f"Blocked by {PR_URL}")
    board = MemoryBoard((card,))
    execute_tick(config, board, FixedFeed((merge_record(),)), NOW)
    original_atomic_write = reconciler.atomic_write_text

    def fail_alert_file(path: Path, content: str, mode: int) -> None:
        if path.parent == config.alert_dir:
            raise OSError("fault injected alert file write")
        original_atomic_write(path, content, mode)

    monkeypatch.setattr(reconciler, "atomic_write_text", fail_alert_file)
    failed = execute_tick(
        config, board, FixedFeed((merge_record(),)), NOW + timedelta(minutes=2)
    )
    assert failed.status == "error"
    assert ReconcilerState.load(config.state_file).alert_outbox
    assert len(config.status_ticker.read_text().splitlines()) == 1
    assert list(config.alert_dir.glob("*.md")) == []

    monkeypatch.setattr(reconciler, "atomic_write_text", original_atomic_write)
    recovered = execute_tick(
        config, board, FixedFeed((merge_record(),)), NOW + timedelta(minutes=4)
    )
    assert recovered.status == "ok"
    ticker_lines = config.status_ticker.read_text().splitlines()
    assert len(ticker_lines) == 1
    assert "RECEIPT=merge-truth-reconciler:slo-alert:" in ticker_lines[0]
    assert len(list(config.alert_dir.glob("merge-truth-reconciler-slo-*.md"))) == 1
    recovered_state = ReconcilerState.load(config.state_file)
    assert recovered_state.alert_outbox == {}
    assert len(recovered_state.alert_receipts) == 1


def test_ticker_failure_retries_from_durable_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    card = card_snapshot("t_ticker", "Await merge", f"Blocked by {PR_URL}")
    board = MemoryBoard((card,))
    execute_tick(config, board, FixedFeed((merge_record(),)), NOW)
    original_append = reconciler.append_line_safely

    def fail_ticker(_path: Path, _line: str, _receipt_key: str) -> None:
        raise OSError("fault injected ticker write")

    monkeypatch.setattr(reconciler, "append_line_safely", fail_ticker)
    failed = execute_tick(
        config, board, FixedFeed((merge_record(),)), NOW + timedelta(minutes=2)
    )
    assert failed.status == "error"
    assert ReconcilerState.load(config.state_file).alert_outbox
    assert config.status_ticker.read_text() == ""

    monkeypatch.setattr(reconciler, "append_line_safely", original_append)
    recovered = execute_tick(
        config, board, FixedFeed((merge_record(),)), NOW + timedelta(minutes=4)
    )
    assert recovered.status == "ok"
    assert len(config.status_ticker.read_text().splitlines()) == 1
    assert len(list(config.alert_dir.glob("*.md"))) == 1


def test_post_alert_state_checkpoint_failure_replays_outputs_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    card = card_snapshot("t_checkpoint", "Await merge", f"Blocked by {PR_URL}")
    board = MemoryBoard((card,))
    execute_tick(config, board, FixedFeed((merge_record(),)), NOW)
    original_save = ReconcilerState.save
    save_calls = 0

    def fail_second_save(state: ReconcilerState, path: Path) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("fault injected post-alert checkpoint")
        original_save(state, path)

    monkeypatch.setattr(ReconcilerState, "save", fail_second_save)
    failed = execute_tick(
        config, board, FixedFeed((merge_record(),)), NOW + timedelta(minutes=2)
    )
    assert failed.status == "error"
    assert ReconcilerState.load(config.state_file).alert_outbox
    assert len(config.status_ticker.read_text().splitlines()) == 1
    assert len(list(config.alert_dir.glob("*.md"))) == 1

    monkeypatch.setattr(ReconcilerState, "save", original_save)
    recovered = execute_tick(
        config, board, FixedFeed((merge_record(),)), NOW + timedelta(minutes=4)
    )
    assert recovered.status == "ok"
    assert len(config.status_ticker.read_text().splitlines()) == 1
    assert len(list(config.alert_dir.glob("*.md"))) == 1
    assert ReconcilerState.load(config.state_file).alert_outbox == {}


def test_slo_breach_deduplicates_alerts_and_summary_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    card = card_snapshot(
        "t_slo",
        "Await merge",
        f"Blocked by {PR_URL}",
        (),
        "blocked",
    )
    config = make_config(tmp_path)
    board = MemoryBoard((card,))
    first = execute_tick(config, board, FixedFeed((merge_record(),)), NOW)
    second = execute_tick(
        config, board, FixedFeed((merge_record(),)), NOW + timedelta(minutes=2)
    )
    third = execute_tick(
        config, board, FixedFeed((merge_record(),)), NOW + timedelta(minutes=4)
    )
    assert first.slo_breaches == []
    assert len(second.slo_breaches) == 1
    assert len(third.slo_breaches) == 1
    ticker_lines = config.status_ticker.read_text().splitlines()
    assert len(ticker_lines) == 1
    assert ticker_lines[0].startswith("SLO-BREACH ")
    assert len(list(config.alert_dir.glob("*.md"))) == 1

    quiet_root = tmp_path / "quiet"
    quiet_report = execute_tick(
        make_config(quiet_root), MemoryBoard(), FixedFeed(), NOW
    )
    print(summary_line(quiet_report))
    captured = capsys.readouterr().out
    assert captured.count("\n") == 1
    assert captured.startswith("merge-truth-reconciler status=ok merges=0")
