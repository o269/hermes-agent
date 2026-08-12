from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "fleet"
    / "fleet_verdict_bridge.py"
)
SPEC = importlib.util.spec_from_file_location("fleet_verdict_bridge", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
fvb = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fvb
SPEC.loader.exec_module(fvb)

HEAD_OLD = "a" * 40
HEAD_LIVE = "b" * 40


class FakeGh:
    def __init__(
        self,
        *,
        live_head: str = HEAD_LIVE,
        author: str = "author",
        identity: str | None = "review-bot[bot]",
        files: list[str] | None = None,
        reviews: list[dict[str, Any]] | None = None,
        statuses: list[dict[str, Any]] | None = None,
        commits: list[str] | None = None,
        associated_prs: list[dict[str, Any]] | None = None,
        base_ref: str = "main",
        open_prs: list[dict[str, Any]] | None = None,
    ):
        self.live_head = live_head
        self.author = author
        self.identity = identity
        self.verified_login: str | None = None
        self.files = files if files is not None else ["scripts/import_customers.py"]
        self.reviews = list(reviews or [])
        self.check_runs = list(statuses or [])
        self.commits = commits
        self.associated_prs = (
            associated_prs
            if associated_prs is not None
            else [{"number": 12, "state": "open"}]
        )
        self.base_ref = base_ref
        self.open_prs = open_prs
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def api(
        self, method: str, endpoint: str, payload: dict[str, Any] | None = None
    ) -> Any:
        if method == "GET" and endpoint == "/user":
            if self.identity is None:
                raise fvb.BridgeError("installation token has no user")
            return {"login": self.identity}
        if method == "GET" and endpoint.endswith("/pulls/12"):
            return {
                "head": {"sha": self.live_head},
                "user": {"login": self.author},
                "base": {"ref": self.base_ref},
            }
        if method == "GET" and "/check-runs?" in endpoint:
            return {"check_runs": self.check_runs}
        if method == "GET" and "/commits/" in endpoint:
            return {"sha": endpoint.rsplit("/", 1)[-1]}
        if method == "POST":
            assert payload is not None
            self.posts.append((endpoint, payload))
            if endpoint.endswith("/reviews"):
                return {"id": 41, "state": payload["event"]}
            if endpoint.endswith("/check-runs"):
                return {"id": 42, **payload}
        raise AssertionError(f"unexpected api call: {method} {endpoint}")

    def pages(self, endpoint: str) -> list[dict[str, Any]]:
        if "/pulls?state=open" in endpoint:
            if self.open_prs is not None:
                return self.open_prs
            return [
                {
                    "number": 12,
                    "head": {"sha": self.live_head},
                    "base": {"ref": self.base_ref},
                }
            ]
        if endpoint.endswith("/pulls/12/commits"):
            commit_shas = self.commits
            if commit_shas is None:
                commit_shas = [HEAD_OLD]
                if self.live_head != HEAD_OLD:
                    commit_shas.append(self.live_head)
            return [{"sha": item} for item in commit_shas]
        if endpoint.endswith("/pulls/12/reviews"):
            return self.reviews
        if endpoint.endswith("/pulls/12/files"):
            return [{"filename": name} for name in self.files]
        if endpoint.endswith(f"/commits/{self.live_head}/pulls"):
            return self.associated_prs
        raise AssertionError(f"unexpected pages call: {endpoint}")


class FakeBoardClient:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.sql = ""

    def query(self, sql: str) -> list[dict[str, Any]]:
        self.sql = sql
        return self.rows


def verdict(
    classification: str,
    *,
    token: str | None = None,
    head: str = HEAD_LIVE,
    findings: tuple[str, ...] | None = None,
) -> Any:
    is_pass = classification == "PASS"
    return fvb.Verdict(
        card="t_review",
        token=token or ("PASS" if is_pass else "FIX_REQUIRED"),
        classification=classification,
        head=head,
        repo="o269/omnia",
        pr=12,
        author="security",
        created_at=1_700_000_000,
        public_findings=(
            tuple()
            if is_pass
            else (
                ("scripts/import_customers.py:42 writes unsafe output",)
                if findings is None
                else findings
            )
        ),
        title="review PR12",
    )


def test_parse_verdict_prefers_url_and_redacts_public_findings():
    parsed = fvb.parse_verdict(
        card="t_x",
        author="security",
        created_at=12,
        comment=(
            "VERDICT: FIX_REQUIRED\n"
            f"Exact head `{HEAD_LIVE[:12]}`\n"
            "PUBLIC_FINDINGS:\n"
            "scripts/import.py:77 leaked +1 (415) 555-1212 and x@example.com"
        ),
        title="[REVIEW][PR12]",
        task_body="https://github.com/o269/omnia/pull/12",
    )
    assert parsed is not None
    assert parsed.classification == "CHANGES"
    assert parsed.repo == "o269/omnia"
    assert parsed.pr == 12
    assert parsed.head == HEAD_LIVE[:12]
    assert "555" not in parsed.public_findings[0]
    assert "example.com" not in parsed.public_findings[0]


def test_plain_pass_is_terminal_and_comment_url_cannot_override_title_target():
    parsed = fvb.parse_verdict(
        card="t_x",
        author="security",
        created_at=12,
        comment=(
            f"PASS — exact head `{HEAD_LIVE}`\n"
            "Evidence https://github.com/o269/other-repo/pull/99"
        ),
        title="[REVIEW][OMNIA][PR12]",
        task_body="",
    )
    assert parsed is not None
    assert parsed.classification == "PASS"
    assert (parsed.repo, parsed.pr) == ("o269/omnia", 12)


def test_unqualified_title_uses_explicit_task_body_url():
    parsed = fvb.parse_verdict(
        card="t_x",
        author="security",
        created_at=12,
        comment=f"VERDICT: PASS\nExact head `{HEAD_LIVE}`",
        title="[REVIEW][PR12]",
        task_body="https://github.com/o269/hermes-agent/pull/12",
    )
    assert parsed is not None
    assert (parsed.repo, parsed.pr) == ("o269/hermes-agent", 12)


def test_ambiguous_unqualified_title_does_not_default_to_omnia():
    parsed = fvb.parse_verdict(
        card="t_x",
        author="security",
        created_at=12,
        comment=f"VERDICT: PASS\nExact head `{HEAD_LIVE}`",
        title="[REVIEW][PR12]",
        task_body="",
    )
    assert parsed is not None
    assert (parsed.repo, parsed.pr) == (None, None)


@pytest.mark.parametrize(
    ("title", "repo"),
    [
        ("[OCC][PR12]", "o269/oasis-command-center"),
        ("[OASIS-PLATFORM][PR12]", "o269/oasis-platform"),
        ("[OASIS-ADUS][PR12]", "o269/oasis-adus"),
    ],
)
def test_explicit_fleet_repo_title_markers_are_mapped(title, repo):
    parsed = fvb.parse_verdict(
        card="t_x",
        author="security",
        created_at=12,
        comment=f"FIX_REQUIRED — do not land\nHead `{HEAD_LIVE}`",
        title=title,
        task_body="",
    )
    assert parsed is not None
    assert parsed.repo == repo
    assert parsed.pr == 12


def test_relayed_verdict_prose_is_not_treated_as_a_terminal_verdict():
    parsed = fvb.parse_verdict(
        card="t_x",
        author="security",
        created_at=12,
        comment="The earlier VERDICT: FIX_REQUIRED was addressed by this rework.",
        title="[REVIEW][OMNIA][PR12]",
        task_body="",
    )
    assert parsed is None


@pytest.mark.parametrize(
    "line",
    [
        "SECURITY VERDICT: FIX_REQUIRED / DO NOT LAND",
        "CANONICAL READ-ONLY VERDICT — FIX_REQUIRED / REBIND",
        "CANONICAL SECURITY VERDICT — FIX_REQUIRED / DO NOT LAND",
    ],
)
def test_security_and_canonical_prefixed_structured_verdicts_are_terminal(line):
    parsed = fvb.parse_verdict(
        card="t_x",
        author="security",
        created_at=12,
        comment=f"{line}\nHead `{HEAD_LIVE}`",
        title="[REVIEW][OMNIA][PR12]",
        task_body="",
    )
    assert parsed is not None
    assert parsed.classification == "CHANGES"


@pytest.mark.parametrize("line", ["REWORK STARTED", "Rework completed:"])
def test_rework_progress_labels_are_not_terminal_verdicts(line):
    parsed = fvb.parse_verdict(
        card="t_x",
        author="security",
        created_at=12,
        comment=line,
        title="[REVIEW][OMNIA][PR12]",
        task_body="",
    )
    assert parsed is None


def test_board_source_hex_decodes_malformed_legacy_utf8_without_crashing():
    comment = (
        b"VERDICT: FIX_REQUIRED\n"
        + f"Exact head `{HEAD_LIVE}`\n".encode()
        + b"PUBLIC_FINDINGS:\nscripts/a.py:1 bad byte \xff"
    )
    client = FakeBoardClient([
        {
            "task_id": "t_bad_utf8",
            "author": "security",
            "created_at": 12,
            "comment_hex": comment.hex(),
            "title_hex": "[REVIEW][OMNIA][PR12]".encode().hex(),
            "task_hex": b"".hex(),
        }
    ])
    parsed = fvb.BoardSource(client, required_authors=["security"]).verdicts()
    assert len(parsed) == 1
    assert parsed[0].public_findings == ("scripts/a.py:1 bad byte �",)
    assert "hex(substr(CAST(tc.body AS BLOB)" in client.sql


def test_board_source_requires_explicit_author_allowlist():
    client = FakeBoardClient([])
    with pytest.raises(fvb.BridgeError, match="require-authors"):
        fvb.BoardSource(client).verdicts()
    assert client.sql == ""


def test_board_source_rejects_spoofed_author_outside_required_reviewers():
    client = FakeBoardClient([
        {
            "task_id": "t_spoofed",
            # Board callers can set this label to "fable"; it is not provenance.
            "author": "fable",
            "created_at": 12,
            "comment_hex": (f"VERDICT: PASS\nExact head `{HEAD_LIVE}`".encode().hex()),
            "title_hex": "R2 REVIEW PR #12".encode().hex(),
            "task_hex": "https://github.com/o269/omnia/pull/12".encode().hex(),
        }
    ])
    board = fvb.BoardSource(client, required_authors=["security"])
    assert board.verdicts() == []
    assert len(board.rejected_verdicts) == 1
    assert board.rejected_verdicts[0].card == "t_spoofed"
    assert board.warnings == [
        "card t_spoofed ignored: comment author 'fable' is not in --require-authors"
    ]


def test_board_author_allowlist_is_defense_in_depth_not_provenance():
    client = FakeBoardClient([
        {
            "task_id": "t_caller_label",
            # A board caller can spoof this allowlisted label. Root-gated apply
            # custody, not this string, is the actual authorization boundary.
            "author": "fable",
            "created_at": 12,
            "comment_hex": (f"VERDICT: PASS\nExact head `{HEAD_LIVE}`".encode().hex()),
            "title_hex": "R2 REVIEW PR #12".encode().hex(),
            "task_hex": "https://github.com/o269/omnia/pull/12".encode().hex(),
        }
    ])
    board = fvb.BoardSource(client, required_authors=["fable"])
    parsed = board.verdicts()
    assert [item.card for item in parsed] == ["t_caller_label"]
    assert board.warnings == []


def test_board_source_rejects_non_review_card_with_warning():
    client = FakeBoardClient([
        {
            "task_id": "t_ordinary",
            "author": "security",
            "created_at": 12,
            "comment_hex": (f"VERDICT: PASS\nExact head `{HEAD_LIVE}`".encode().hex()),
            "title_hex": "AUTHOR importer cleanup".encode().hex(),
            "task_hex": "https://github.com/o269/omnia/pull/12".encode().hex(),
        }
    ])
    board = fvb.BoardSource(client, required_authors=["security"])
    assert board.verdicts() == []
    assert len(board.rejected_verdicts) == 1
    assert board.rejected_verdicts[0].card == "t_ordinary"
    assert board.warnings == ["card t_ordinary ignored: title is not review-shaped"]


def test_card_post_never_projects_rejected_non_review_card(capsys):
    client = FakeBoardClient([
        {
            "task_id": "t_ordinary",
            "author": "security",
            "created_at": 12,
            "comment_hex": (f"VERDICT: PASS\nExact head `{HEAD_LIVE}`".encode().hex()),
            "title_hex": "AUTHOR importer cleanup".encode().hex(),
            "task_hex": "https://github.com/o269/omnia/pull/12".encode().hex(),
        }
    ])
    board = fvb.BoardSource(client, required_authors=["security"])
    args = SimpleNamespace(card="t_ordinary")
    gh = FakeGh()
    with pytest.raises(fvb.BridgeError, match="no structured verdict"):
        fvb.cmd_post(args, gh, board)
    assert gh.posts == []
    assert capsys.readouterr().err == (
        "BOARD FILTER WARNING: card t_ordinary ignored: title is not review-shaped\n"
    )


def test_card_post_emits_only_target_card_warning(capsys):
    board = SimpleNamespace(
        verdicts=lambda: [],
        warnings=[
            "card t_target ignored: title is not review-shaped",
            "card t_unrelated ignored: comment author 'worker' is not in --require-authors",
        ],
    )
    with pytest.raises(fvb.BridgeError, match="no structured verdict"):
        fvb.cmd_post(SimpleNamespace(card="t_target"), FakeGh(), board)
    assert capsys.readouterr().err == (
        "BOARD FILTER WARNING: card t_target ignored: title is not review-shaped\n"
    )


@pytest.mark.parametrize(
    "title",
    [
        "[REVIEW][OMNIA][PR12]",
        "R2 REVIEW PR #12 — exact-head verdict",
        "p207 — R1 SECURITY REVIEW PR #709",
        "p201 — [GOVERNANCE ROOT CAUSE] R2 REVIEW PR #709",
        "[OMNIA][PR12] independent re-review",
    ],
)
def test_review_card_title_shapes_are_accepted(title):
    assert fvb.is_review_card_title(title)


def test_author_card_that_merely_mentions_review_is_not_review_shaped():
    assert not fvb.is_review_card_title(
        "p201 — [GOVERNANCE ROOT CAUSE] Bridge review verdicts to GitHub"
    )


def test_public_findings_redact_common_secret_and_customer_markers():
    text = "\n".join([
        "PUBLIC_FINDINGS:",
        "scripts/a.py:1 github_pat_" + "x" * 30,
        "scripts/a.py:2 Authorization: Bearer eyJabc.def.ghi",
        "scripts/a.py:3 AWS_ACCESS_KEY_ID=AKIA1234567890ABCDEF",
        "scripts/a.py:4 DATABASE_URL=postgres://u:pass@10.2.3.4/db",
    ])
    rendered = "\n".join(fvb.extract_public_findings(text, require_marker=True))
    for forbidden in (
        "github_pat_",
        "eyJabc",
        "AKIA1234567890ABCDEF",
        "postgres://",
        "10.2.3.4",
    ):
        assert forbidden not in rendered
    assert "REDACTED" in rendered


def test_pass_posts_approval_and_success_status_on_sensitive_path():
    gh = FakeGh()
    messages = fvb.project_verdict(
        gh,
        verdict("PASS"),
        apply=True,
        reviewer_login="review-bot[bot]",
    )
    assert "event=APPROVE" in messages[0]
    assert "fleet-review-gate=success" in messages[1]
    review_payload = gh.posts[0][1]
    status_payload = gh.posts[1][1]
    assert review_payload["event"] == "APPROVE"
    assert review_payload["commit_id"] == HEAD_LIVE
    assert "No blocking findings" in review_payload["body"]
    assert status_payload["conclusion"] == "success"
    assert status_payload["name"] == "fleet-review-gate"


def test_changes_posts_request_and_failure_status():
    gh = FakeGh()
    messages = fvb.project_verdict(
        gh,
        verdict("CHANGES"),
        apply=True,
        reviewer_login="review-bot[bot]",
    )
    assert "event=REQUEST_CHANGES" in messages[0]
    assert "fleet-review-gate=failure" in messages[1]
    assert gh.posts[0][1]["event"] == "REQUEST_CHANGES"
    assert gh.posts[1][1]["conclusion"] == "failure"


def test_identical_review_and_status_are_idempotent():
    current = verdict("CHANGES")
    marker = fvb._review_marker(current, HEAD_LIVE)
    reviews = [
        {
            "id": 91,
            "user": {"login": "review-bot[bot]"},
            "body": marker,
        }
    ]
    check_payload = fvb._check_run_payload(
        HEAD_LIVE,
        {
            "context": "fleet-review-gate",
            "state": "failure",
            "description": "FIX_REQUIRED from card t_review",
        },
    )
    statuses = [{"id": 92, "app": {"slug": "review-bot"}, **check_payload}]
    gh = FakeGh(reviews=reviews, statuses=statuses)
    messages = fvb.project_verdict(
        gh,
        current,
        apply=True,
        reviewer_login="review-bot[bot]",
    )
    assert messages[0].startswith("review no-op")
    assert messages[1].startswith("check run no-op")
    assert gh.posts == []


def test_changed_findings_on_same_card_verdict_and_head_supersede():
    prior = verdict("CHANGES")
    current = verdict(
        "CHANGES", findings=("scripts/import_customers.py:43 newly narrowed finding",)
    )
    reviews = [
        {
            "id": 91,
            "user": {"login": "review-bot[bot]"},
            "body": fvb.build_review_body(prior, HEAD_LIVE, stale=False),
        }
    ]
    gh = FakeGh(reviews=reviews)
    messages = fvb.project_verdict(
        gh,
        current,
        apply=True,
        reviewer_login="review-bot[bot]",
        review_only=True,
    )
    assert messages[0].startswith("review posted")
    assert gh.posts[0][1]["event"] == "REQUEST_CHANGES"


def test_new_pass_supersedes_prior_changes_review_and_status():
    old = verdict("CHANGES", head=HEAD_OLD)
    reviews = [
        {
            "id": 51,
            "user": {"login": "review-bot[bot]"},
            "body": fvb.build_review_body(old, HEAD_OLD, stale=True),
        }
    ]
    statuses = [
        {
            "id": 52,
            "app": {"slug": "review-bot"},
            **fvb._check_run_payload(
                HEAD_LIVE,
                {
                    "context": "fleet-review-gate",
                    "state": "failure",
                    "description": "FIX_REQUIRED from card t_review",
                },
            ),
        }
    ]
    gh = FakeGh(reviews=reviews, statuses=statuses)
    fvb.project_verdict(
        gh,
        verdict("PASS"),
        apply=True,
        reviewer_login="review-bot[bot]",
    )
    assert gh.posts[0][1]["event"] == "APPROVE"
    assert gh.posts[1][1]["conclusion"] == "success"


def test_review_only_posts_no_status():
    gh = FakeGh(files=["supabase/migrations/20260811_guard.sql"])
    messages = fvb.project_verdict(
        gh,
        verdict("FIX_REQUIRED"),
        apply=True,
        reviewer_login="review-bot[bot]",
        review_only=True,
    )
    assert len(gh.posts) == 1
    assert gh.posts[0][0].endswith("/reviews")
    assert messages[-1] == "check run skipped: explicit review-only mode"


def test_stale_pass_fails_closed_without_writes():
    gh = FakeGh()
    with pytest.raises(fvb.BridgeError, match="stale-head approval refused"):
        fvb.project_verdict(
            gh,
            verdict("PASS", head=HEAD_OLD),
            apply=True,
            reviewer_login="review-bot[bot]",
        )
    assert gh.posts == []


def test_stale_changes_review_binds_old_head_and_blocks_live_head():
    gh = FakeGh()
    fvb.project_verdict(
        gh,
        verdict("CHANGES", head=HEAD_OLD),
        apply=True,
        reviewer_login="review-bot[bot]",
    )
    review_endpoint, review_payload = gh.posts[0]
    status_endpoint, status_payload = gh.posts[1]
    assert review_endpoint.endswith("/pulls/12/reviews")
    assert review_payload["commit_id"] == HEAD_OLD
    assert "SUPERSEDED HEAD" in review_payload["body"]
    assert status_endpoint.endswith("/check-runs")
    assert status_payload["head_sha"] == HEAD_LIVE
    assert status_payload["conclusion"] == "failure"


def test_force_pushed_changes_review_attaches_to_live_head_but_names_old_head():
    gh = FakeGh(commits=[HEAD_LIVE])
    fvb.project_verdict(
        gh,
        verdict("CHANGES", head=HEAD_OLD),
        apply=True,
        reviewer_login="review-bot[bot]",
    )
    review_payload = gh.posts[0][1]
    assert review_payload["commit_id"] == HEAD_LIVE
    assert HEAD_OLD in review_payload["body"]
    assert "SUPERSEDED HEAD" in review_payload["body"]


def test_shared_head_forces_failure_even_for_pass():
    gh = FakeGh(
        associated_prs=[
            {"number": 12, "state": "open"},
            {"number": 13, "state": "open"},
        ]
    )
    fvb.project_verdict(
        gh,
        verdict("PASS"),
        apply=True,
        reviewer_login="review-bot[bot]",
    )
    status_payload = gh.posts[1][1]
    assert status_payload["conclusion"] == "failure"
    assert (
        status_payload["output"]["summary"]
        == "ambiguous:head-shared-by-open-prs #12,#13"
    )


def test_non_main_base_cannot_mint_success_before_retarget():
    gh = FakeGh(base_ref="release")
    fvb.project_verdict(
        gh,
        verdict("PASS"),
        apply=True,
        reviewer_login="review-bot[bot]",
    )
    assert gh.posts[1][1]["conclusion"] == "failure"
    assert gh.posts[1][1]["output"]["summary"] == "base-not-main:release"


def test_explicit_changes_verdict_blocks_even_on_non_sensitive_path():
    gh = FakeGh(files=["README.md"])
    fvb.project_verdict(
        gh,
        verdict("CHANGES"),
        apply=True,
        reviewer_login="review-bot[bot]",
    )
    status_payload = gh.posts[1][1]
    assert status_payload["conclusion"] == "failure"
    assert status_payload["output"]["summary"].startswith("FIX_REQUIRED")


@pytest.mark.parametrize(
    ("files", "expected_state", "description"),
    [
        (["README.md"], "success", "skip:no-sensitive-paths"),
        (
            ["supabase/migrations/20260811_guard.sql"],
            "failure",
            "review-required:no-board-verdict",
        ),
    ],
)
def test_unreviewed_pr_status_is_path_aware(files, expected_state, description):
    gh = FakeGh(files=files)
    messages = fvb.project_unreviewed_pr(
        gh,
        repo="o269/omnia",
        pr_data={
            "number": 12,
            "head": {"sha": HEAD_LIVE},
            "base": {"ref": "main"},
        },
        apply=True,
        reviewer_login="review-bot[bot]",
    )
    assert expected_state in messages[0]
    assert gh.posts[0][1]["conclusion"] == expected_state
    assert gh.posts[0][1]["output"]["summary"].startswith(description)


def test_ambiguous_legacy_verdict_prevents_non_sensitive_auto_success():
    gh = FakeGh(files=["README.md"])
    messages = fvb.project_unreviewed_pr(
        gh,
        repo="o269/omnia",
        pr_data={
            "number": 12,
            "head": {"sha": HEAD_LIVE},
            "base": {"ref": "main"},
        },
        apply=True,
        reviewer_login="review-bot[bot]",
        board_blocker=(
            "review-required:ambiguous-board-target card=t_legacy verdict=FIX_REQUIRED"
        ),
    )
    assert "failure" in messages[0]
    assert gh.posts[0][1]["conclusion"] == "failure"
    assert "ambiguous-board-target" in gh.posts[0][1]["output"]["summary"]


def test_changes_without_public_file_line_finding_refuses():
    gh = FakeGh()
    with pytest.raises(fvb.BridgeError, match="no public-safe file:line"):
        fvb.project_verdict(
            gh,
            verdict("CHANGES", findings=tuple()),
            apply=True,
            reviewer_login="review-bot[bot]",
        )
    assert gh.posts == []


def test_self_review_refuses_before_any_write():
    gh = FakeGh(author="review-bot[bot]")
    with pytest.raises(fvb.BridgeError, match="equals PR author"):
        fvb.project_verdict(
            gh,
            verdict("PASS"),
            apply=True,
            reviewer_login="review-bot[bot]",
        )
    assert gh.posts == []


def test_unverifiable_installation_token_fails_closed_before_write():
    gh = FakeGh(identity=None)
    with pytest.raises(fvb.BridgeError, match="identity is unverifiable"):
        fvb.project_verdict(
            gh,
            verdict("PASS"),
            apply=True,
            reviewer_login="review-bot[bot]",
        )
    assert gh.posts == []


def test_verified_app_login_allows_installation_token():
    gh = FakeGh(identity=None)
    gh.verified_login = "review-bot[bot]"
    fvb.project_verdict(
        gh,
        verdict("PASS"),
        apply=True,
        reviewer_login="review-bot[bot]",
        review_only=True,
    )
    assert gh.posts[0][1]["event"] == "APPROVE"


def test_apply_launcher_rejects_a_valid_but_unpinned_app_jwt(monkeypatch):
    calls = []

    def fake_api(self, method, endpoint, payload=None):
        calls.append((method, endpoint))
        if endpoint == "/app":
            return {"id": 9999999, "slug": "attacker"}
        pytest.fail("must not mint an installation token for an unpinned App")

    monkeypatch.setenv("FVB_APP_JWT", "signed-by-another-app")
    monkeypatch.setenv("FVB_REVIEWER_INSTALLATION_ID", "123")
    monkeypatch.delenv("FVB_REVIEWER_APP_ID", raising=False)
    monkeypatch.setattr(fvb.GhClient, "api", fake_api)
    with pytest.raises(fvb.BridgeError, match="expected omnia-lander"):
        fvb.github_client_from_environment()
    assert calls == [("GET", "/app")]


def test_apply_launcher_rejects_override_of_pinned_app_id(monkeypatch):
    monkeypatch.setenv("FVB_APP_JWT", "signed-by-another-app")
    monkeypatch.setenv("FVB_REVIEWER_APP_ID", "9999999")
    monkeypatch.setenv("FVB_REVIEWER_INSTALLATION_ID", "123")
    monkeypatch.setattr(
        fvb.GhClient,
        "api",
        lambda *args, **kwargs: pytest.fail("must fail before a network request"),
    )
    with pytest.raises(fvb.BridgeError, match="pinned omnia-lander App"):
        fvb.github_client_from_environment()


def test_apply_launcher_mints_only_after_pinned_app_identity(monkeypatch):
    calls = []

    def fake_api(self, method, endpoint, payload=None):
        calls.append((self._token, method, endpoint))
        if endpoint == "/app":
            return {
                "id": int(fvb.TRUSTED_REVIEWER_APP_ID),
                "slug": fvb.TRUSTED_REVIEWER_APP_SLUG,
            }
        if endpoint == "/app/installations/123/access_tokens":
            return {"token": "installation-token"}
        pytest.fail(f"unexpected endpoint {endpoint}")

    monkeypatch.setenv("FVB_APP_JWT", "trusted-app-jwt")
    monkeypatch.setenv("FVB_REVIEWER_APP_ID", fvb.TRUSTED_REVIEWER_APP_ID)
    monkeypatch.setenv("FVB_REVIEWER_INSTALLATION_ID", "123")
    monkeypatch.setattr(fvb.GhClient, "api", fake_api)
    client = fvb.github_client_from_environment()
    assert client._token == "installation-token"
    assert client.verified_login == "omnia-lander[bot]"
    assert calls == [
        ("trusted-app-jwt", "GET", "/app"),
        (
            "trusted-app-jwt",
            "POST",
            "/app/installations/123/access_tokens",
        ),
    ]


def test_latest_pr_verdict_supersedes_older_card_verdict():
    older = verdict("CHANGES")
    newer = fvb.Verdict(**{
        **older.__dict__,
        "card": "t_rereview",
        "token": "PASS",
        "classification": "PASS",
        "created_at": older.created_at + 1,
        "public_findings": tuple(),
    })
    assert fvb.latest_by_pr([older, newer])[("o269/omnia", 12)] == newer


def test_newer_unqualified_verdict_is_retained_as_ambiguous_blocker():
    mapped = verdict("PASS")
    ambiguous = fvb.Verdict(**{
        **mapped.__dict__,
        "card": "t_legacy",
        "token": "FIX_REQUIRED",
        "classification": "CHANGES",
        "repo": None,
        "pr": None,
        "created_at": mapped.created_at + 1,
        "title": "[REVIEW][PR12] legacy card",
    })
    assert fvb.latest_ambiguous_by_pr_number([mapped, ambiguous])[12] == ambiguous


def test_scan_newer_ambiguous_verdict_overrides_mapped_pass(capsys):
    mapped = verdict("PASS")
    ambiguous = fvb.Verdict(**{
        **mapped.__dict__,
        "card": "t_legacy",
        "token": "FIX_REQUIRED",
        "classification": "CHANGES",
        "repo": None,
        "pr": None,
        "created_at": mapped.created_at + 1,
        "title": "[REVIEW][PR12] legacy card",
    })
    board = SimpleNamespace(verdicts=lambda: [mapped, ambiguous])
    args = SimpleNamespace(
        since=0,
        cursor_file=None,
        repo=["o269/omnia"],
        sensitive_path=[],
        apply=False,
        reviewer_login="review-bot[bot]",
        context="fleet-review-gate",
    )
    assert fvb.cmd_scan(args, FakeGh(files=["README.md"]), board) == 0
    output = capsys.readouterr().out
    assert "ambiguous-board-target" in output
    assert "fleet-review-gate=failure" in output


def test_scan_rejected_spoofed_author_is_warned_and_never_projected(capsys):
    spoofed = verdict("PASS")
    board = SimpleNamespace(
        verdicts=lambda: [],
        rejected_verdicts=[spoofed],
        warnings=[
            "card t_review ignored: comment author 'fable' is not in --require-authors"
        ],
    )
    args = SimpleNamespace(
        since=0,
        cursor_file=None,
        repo=["o269/omnia"],
        sensitive_path=[],
        apply=False,
        reviewer_login="review-bot[bot]",
        context="fleet-review-gate",
    )
    gh = FakeGh(files=["README.md"])
    assert fvb.cmd_scan(args, gh, board) == 0
    output = capsys.readouterr()
    assert "BOARD FILTER WARNING" in output.err
    assert "rejected-board-verdict" in output.out
    assert "fleet-review-gate=failure" in output.out
    assert gh.posts == []


def test_manual_apply_without_verified_app_launcher_refuses(monkeypatch, capsys):
    monkeypatch.delenv("FVB_APP_JWT", raising=False)
    monkeypatch.setattr(
        fvb,
        "github_client_from_environment",
        lambda: pytest.fail("GitHub client must not be constructed"),
    )
    assert (
        fvb.main([
            "post",
            "--repo",
            "o269/omnia",
            "--pr",
            "12",
            "--verdict",
            "PASS",
            "--head",
            HEAD_LIVE,
            "--apply",
        ])
        == 2
    )
    assert "sudo-gated FVB_APP_JWT launcher" in capsys.readouterr().err


def test_scan_does_not_advance_cursor_after_stale_pass_error(tmp_path):
    cursor = tmp_path / "cursor"
    cursor.write_text("123\n", encoding="utf-8")
    stale = verdict("PASS", head=HEAD_OLD)
    board = SimpleNamespace(verdicts=lambda: [stale])
    args = SimpleNamespace(
        since=None,
        cursor_file=str(cursor),
        repo=["o269/omnia"],
        sensitive_path=[],
        apply=True,
        reviewer_login="review-bot[bot]",
        context="fleet-review-gate",
    )
    gh = FakeGh()
    assert fvb.cmd_scan(args, gh, board) == 2
    assert cursor.read_text(encoding="utf-8") == "123\n"
    assert gh.posts == []
