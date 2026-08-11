#!/usr/bin/env python3
"""Bridge fleet kanban review verdicts to GitHub reviews and a SHA-bound gate.

The command is dry-run by default. ``--apply`` is the only mode that writes to
GitHub. Board access uses hermes_cli.kb_client (boardd); this module never opens
kanban.db. Authentication is read by ``gh`` from ``GH_TOKEN`` at point of use.

A public GitHub review body is deliberately narrower than a private board
comment. PASS emits "No blocking findings". A changes verdict must contain at
least one public-safe file:line finding; otherwise apply mode fails closed rather
than copying a potentially sensitive board comment to GitHub.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

MARKER_VERSION = "fvb:v3"
DEFAULT_CONTEXT = "fleet-review-gate"
PASS_TOKENS = frozenset({
    "APPROVE",
    "APPROVED",
    "GO_LAND",
    "LANDABLE",
    "PASS",
    "PASS_CURRENT",
    "PASS_TO_LAND",
})
CHANGES_TOKENS = frozenset({
    "BLOCK",
    "CHANGES_REQUIRED",
    "FAIL",
    "FIX_REQUIRED",
    "REJECT",
    "REQUEST_CHANGES",
    "REWORK",
})
SENSITIVE_PATHS = (
    "scripts/**",
    "apps/api/src/lib/crm/**",
    "apps/api/src/**/migrations/**",
    "supabase/migrations/**",
    "packages/db/**",
    ".rls-admin-allowlist",
    ".github/workflows/**",
    "**/*rls*",
)

VERDICT_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?"
    r"(?:CANONICAL(?:\s+(?:READ-ONLY|SECURITY)){0,2}\s+)?"
    r"(?:SECURITY\s+)?\*{0,2}VERDICT\s*\*{0,2}\s*[:\-—]\s*"
    r"\*{0,2}([A-Z_]+)\*{0,2}(?=\s|$)",
    re.IGNORECASE | re.MULTILINE,
)
_TOKEN_ALTERNATION = "|".join(
    sorted(PASS_TOKENS | CHANGES_TOKENS, key=len, reverse=True)
)
PLAIN_VERDICT_RE = re.compile(
    rf"^\s*(?:#{{1,6}}\s*)?(?:SECURITY\s+)?\*{{0,2}}"
    rf"({_TOKEN_ALTERNATION})\*{{0,2}}\s*(?=@|—|/|$)",
    re.IGNORECASE | re.MULTILINE,
)
HEAD_RE = re.compile(
    r"(?:exact[-_ ]?head|head|commit|@)\s*[`:=#-]*\s*([0-9a-f]{7,40})\b",
    re.IGNORECASE,
)
URL_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
PR_RE = re.compile(r"\bPR\s*#?\s*(\d{1,8})\b", re.IGNORECASE)
FILE_LINE_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.@-]+/)+[A-Za-z0-9_.@-]+):(?P<line>\d+)(?::\d+)?"
)
SECRET_RE = re.compile(
    r"(?i)(?:gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"(?:authorization|proxy-authorization)\s*:\s*(?:bearer|token)\s+\S+|"
    r"(?:[A-Z0-9_]*(?:API_KEY|ACCESS_KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)"
    r"\s*[:=]\s*\S+)"
)
DATABASE_URL_RE = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|redis|mongodb(?:\+srv)?):\/\/\S+"
)
AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
BEARER_JWT_RE = re.compile(r"(?i)\bbearer\s+eyJ[A-Za-z0-9._-]+")
IPV4_RE = re.compile(
    r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])"
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<![A-Za-z0-9])\+?\d(?:[\s().-]*\d){6,}(?![A-Za-z0-9])")


@dataclass(frozen=True)
class Verdict:
    card: str
    token: str
    classification: str
    head: str | None
    repo: str | None
    pr: int | None
    author: str
    created_at: int
    public_findings: tuple[str, ...]
    title: str = ""


class BridgeError(RuntimeError):
    """A fail-closed input, identity, or transport error."""


class GhClient:
    """Small JSON transport over ``gh api``; never accepts tokens as arguments."""

    def __init__(
        self, token: str | None = None, *, verified_login: str | None = None
    ) -> None:
        self._token = token
        self.verified_login = verified_login

    def api(
        self, method: str, endpoint: str, payload: dict[str, Any] | None = None
    ) -> Any:
        command = ["gh", "api", "-X", method, endpoint]
        if payload is not None:
            command.extend(["--input", "-"])
        env = os.environ.copy()
        env.pop("FVB_APP_JWT", None)
        if self._token is not None:
            env["GH_TOKEN"] = self._token
            env.pop("GITHUB_TOKEN", None)
        try:
            process = subprocess.run(
                command,
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError(
                f"GitHub transport unavailable for {method} {endpoint}: {type(exc).__name__}"
            ) from exc
        if process.returncode:
            detail = _redact(process.stderr.strip() or process.stdout.strip())[:500]
            raise BridgeError(f"GitHub {method} {endpoint} failed: {detail}")
        if not process.stdout.strip():
            return None
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"GitHub returned non-JSON for {endpoint}") from exc

    def pages(self, endpoint: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        separator = "&" if "?" in endpoint else "?"
        while True:
            batch = self.api("GET", f"{endpoint}{separator}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise BridgeError(f"GitHub pagination expected a list for {endpoint}")
            rows.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                return rows
            page += 1


def github_client_from_environment() -> GhClient:
    """Build a client and cryptographically verify App identity when supplied.

    Installation access tokens cannot identify their parent App through a
    read-only endpoint. A secure launcher therefore supplies a short-lived App
    JWT in process environment; this function verifies that JWT with ``/app``
    and mints the installation token itself, preventing a claimed-login-only
    fail-open.
    """
    app_jwt = os.environ.get("FVB_APP_JWT")
    if not app_jwt:
        return GhClient()
    expected_id = os.environ.get("FVB_REVIEWER_APP_ID")
    installation_id = os.environ.get("FVB_REVIEWER_INSTALLATION_ID")
    if not expected_id or not installation_id:
        raise BridgeError(
            "FVB_APP_JWT requires FVB_REVIEWER_APP_ID and FVB_REVIEWER_INSTALLATION_ID"
        )
    app_client = GhClient(token=app_jwt)
    app = app_client.api("GET", "/app")
    if not isinstance(app, dict):
        raise BridgeError("GitHub returned no App identity")
    app_id = str(app.get("id") or "")
    slug = str(app.get("slug") or "")
    if app_id != expected_id or not slug:
        raise BridgeError(
            f"App identity mismatch: expected id {expected_id}, received {app_id or 'none'}"
        )
    minted = app_client.api(
        "POST", f"/app/installations/{installation_id}/access_tokens", {}
    )
    if not isinstance(minted, dict) or not minted.get("token"):
        raise BridgeError("GitHub returned no installation token")
    return GhClient(token=str(minted["token"]), verified_login=f"{slug}[bot]")


class BoardSource:
    """Read verdicts through the boardd broker-backed client."""

    def __init__(self, client: Any | None = None):
        if client is None:
            repo_root = Path(__file__).resolve().parents[2]
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            from hermes_cli.kb_client import Client

            client = Client()
        self.client = client

    def verdicts(self) -> list[Verdict]:
        try:
            rows = self.client.query(
                """
                SELECT tc.task_id, tc.author, tc.created_at,
                       hex(substr(CAST(tc.body AS BLOB), 1, 24000)) AS comment_hex,
                       hex(CAST(COALESCE(t.title, '') AS BLOB)) AS title_hex,
                       hex(substr(CAST(COALESCE(t.body, '') AS BLOB), 1, 16000)) AS task_hex
                  FROM task_comments tc
                  JOIN tasks t ON t.id = tc.task_id
                 WHERE upper(substr(tc.body, 1, 600)) LIKE '%VERDICT%'
                    OR upper(substr(tc.body, 1, 600)) LIKE '%FIX_REQUIRED%'
                    OR upper(substr(tc.body, 1, 600)) LIKE '%REWORK%'
                    OR upper(substr(tc.body, 1, 120)) LIKE '%PASS%'
                    OR upper(substr(tc.body, 1, 120)) LIKE '%APPROVED%'
                 ORDER BY tc.created_at
                """
            )
        except Exception as exc:
            raise BridgeError(
                f"board broker query failed: {type(exc).__name__}"
            ) from exc
        parsed: list[Verdict] = []
        for row in rows:
            verdict = parse_verdict(
                card=str(row["task_id"]),
                author=str(row.get("author") or "unknown"),
                created_at=_coerce_created_at(row["created_at"]),
                comment=_decode_board_hex(row.get("comment_hex")),
                title=_decode_board_hex(row.get("title_hex")),
                task_body=_decode_board_hex(row.get("task_hex")),
            )
            if verdict is not None:
                parsed.append(verdict)
        return parsed


def _decode_board_hex(value: Any) -> str:
    """Decode broker-safe hex while replacing legacy malformed UTF-8 bytes."""
    if value is None:
        return ""
    try:
        return bytes.fromhex(str(value)).decode("utf-8", errors="replace")
    except ValueError as exc:
        raise BridgeError("boardd returned malformed hex text") from exc


def _coerce_created_at(value: Any) -> int:
    """Normalize board timestamps, which exist as both epochs and ISO text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    raw = str(value).strip()
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BridgeError(f"invalid board comment timestamp {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _redact(text: str) -> str:
    text = DATABASE_URL_RE.sub("[REDACTED-DATABASE-URL]", text)
    text = BEARER_JWT_RE.sub("Bearer [REDACTED]", text)
    text = AWS_KEY_RE.sub("[REDACTED-AWS-KEY]", text)
    text = SECRET_RE.sub("[REDACTED]", text)
    text = EMAIL_RE.sub("[REDACTED-EMAIL]", text)
    text = PHONE_RE.sub("[REDACTED-PHONE]", text)
    return IPV4_RE.sub("[REDACTED-IP]", text)


def _repo_from_title(title: str) -> str | None:
    upper = title.upper()
    if "OMNIA-V2" in upper:
        return "o269/omnia-v2"
    if "OMNIA-MOBILE" in upper:
        return "o269/omnia-mobile"
    if "CONTRACTOR-HUB" in upper:
        return "o269/omnia-contractor-hub"
    if "HOME-HUB" in upper:
        return "o269/omnia-home-hub"
    if "OASIS-COMMAND-CENTER" in upper or "[OCC]" in upper:
        return "o269/oasis-command-center"
    if "OASIS-PLATFORM" in upper:
        return "o269/oasis-platform"
    if "OASIS-ADUS" in upper:
        return "o269/oasis-adus"
    if "HERMES-OFFICIAL" in upper or "HERMES-UPSTREAM" in upper:
        return "NousResearch/hermes-agent"
    if "HERMES" in upper:
        return "o269/hermes-agent"
    if re.search(r"(?:^|[^A-Z0-9])OMNIA(?:[^A-Z0-9]|$)", upper):
        return "o269/omnia"
    return None


def extract_public_findings(
    text: str, *, require_marker: bool = False
) -> tuple[str, ...]:
    """Return redacted file:line findings from an explicitly public section."""
    source = text
    if require_marker:
        marker = re.search(r"(?im)^\s*(?:#{1,6}\s*)?PUBLIC[_ ]FINDINGS\s*:\s*$", text)
        if marker is None:
            return ()
        source = text[marker.end() :]
    findings: list[str] = []
    for raw in source.splitlines():
        line = raw.strip().lstrip("-* ")
        if not FILE_LINE_RE.search(line):
            continue
        clean = _redact(line)
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean and clean not in findings:
            findings.append(clean[:500])
        if len(findings) == 10:
            break
    return tuple(findings)


def _target_from_card(
    title: str, task_body: str, comment: str
) -> tuple[str | None, int | None]:
    title_urls = {
        (match.group(1), int(match.group(2))) for match in URL_RE.finditer(title)
    }
    if len(title_urls) == 1:
        return next(iter(title_urls))
    title_pr = PR_RE.search(title)
    body_urls = {
        (match.group(1), int(match.group(2))) for match in URL_RE.finditer(task_body)
    }
    if len(body_urls) == 1:
        return next(iter(body_urls))
    if title_pr:
        number = int(title_pr.group(1))
        numbered_body_urls = {item for item in body_urls if item[1] == number}
        if len(numbered_body_urls) == 1:
            return next(iter(numbered_body_urls))
    if title_pr:
        repo = _repo_from_title(title)
        if repo is not None:
            return repo, int(title_pr.group(1))
    comment_urls = {
        (match.group(1), int(match.group(2))) for match in URL_RE.finditer(comment)
    }
    if not body_urls and len(comment_urls) == 1:
        return next(iter(comment_urls))
    return None, None


def parse_verdict(
    *,
    card: str,
    author: str,
    created_at: int,
    comment: str,
    title: str,
    task_body: str,
) -> Verdict | None:
    match = VERDICT_RE.search(comment) or PLAIN_VERDICT_RE.search(comment)
    if match is None:
        return None
    token = match.group(1).upper()
    if token in PASS_TOKENS:
        classification = "PASS"
    elif token in CHANGES_TOKENS:
        classification = "CHANGES"
    else:
        return None

    repo, pr = _target_from_card(title, task_body, comment)

    head_match = HEAD_RE.search(comment) or HEAD_RE.search(title)
    head = head_match.group(1).lower() if head_match else None
    return Verdict(
        card=card,
        token=token,
        classification=classification,
        head=head,
        repo=repo,
        pr=pr,
        author=author,
        created_at=created_at,
        public_findings=extract_public_findings(comment, require_marker=True),
        title=title,
    )


def latest_by_card(verdicts: Iterable[Verdict]) -> dict[str, Verdict]:
    latest: dict[str, Verdict] = {}
    for verdict in verdicts:
        prior = latest.get(verdict.card)
        if prior is None or verdict.created_at >= prior.created_at:
            latest[verdict.card] = verdict
    return latest


def latest_by_pr(verdicts: Iterable[Verdict]) -> dict[tuple[str, int], Verdict]:
    """Use the fleet's current latest-verdict-wins PR timeline."""
    latest: dict[tuple[str, int], Verdict] = {}
    for verdict in latest_by_card(verdicts).values():
        if verdict.repo is None or verdict.pr is None:
            continue
        key = (verdict.repo.lower(), verdict.pr)
        prior = latest.get(key)
        if prior is None or verdict.created_at >= prior.created_at:
            latest[key] = verdict
    return latest


def latest_ambiguous_by_pr_number(verdicts: Iterable[Verdict]) -> dict[int, Verdict]:
    """Retain legacy verdicts without guessing their repository.

    PR numbers collide across repositories, so these records cannot project a
    native review. Scan mode uses them only to prevent an unsafe auto-success on
    a same-number open PR until the card has an explicit GitHub PR URL.
    """
    latest: dict[int, Verdict] = {}
    for verdict in latest_by_card(verdicts).values():
        if verdict.repo is not None and verdict.pr is not None:
            continue
        title_pr = PR_RE.search(verdict.title)
        if title_pr is None:
            continue
        number = int(title_pr.group(1))
        prior = latest.get(number)
        if prior is None or verdict.created_at >= prior.created_at:
            latest[number] = verdict
    return latest


def resolve_reviewed_commit(
    gh: GhClient, repo: str, pr: int, reference: str | None
) -> tuple[str, bool]:
    if not reference:
        raise BridgeError("verdict is not bound to an exact PR head")
    commits = gh.pages(f"repos/{repo}/pulls/{pr}/commits")
    matches = [
        str(commit.get("sha", ""))
        for commit in commits
        if str(commit.get("sha", "")).startswith(reference.lower())
    ]
    if len(matches) == 1:
        return matches[0], True
    if len(matches) > 1 or len(reference) != 40:
        raise BridgeError(
            f"reviewed head {reference} resolves to {len(matches)} commits on {repo}#{pr}"
        )
    # A force-push can remove the reviewed commit from the PR history. Preserve
    # a blocking verdict by verifying the full object in the repository, then
    # attach REQUEST_CHANGES to the live head while labeling the old reviewed
    # head as superseded. PASS still fails closed below.
    commit = gh.api("GET", f"repos/{repo}/commits/{reference}")
    resolved = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
    if resolved.lower() != reference.lower():
        raise BridgeError(f"reviewed head {reference} is not a repository commit")
    return resolved, False


def resolve_identity(
    gh: GhClient, configured: str | None, *, strict: bool = True
) -> str:
    observed = getattr(gh, "verified_login", None)
    if not observed:
        try:
            user = gh.api("GET", "/user")
            if isinstance(user, dict):
                observed = str(user.get("login") or "") or None
        except BridgeError:
            if strict:
                raise BridgeError(
                    "token identity is unverifiable; App apply mode requires a verified FVB_APP_JWT launcher"
                ) from None
            observed = None
    if observed and configured and observed.lower() != configured.lower():
        if strict:
            raise BridgeError(
                f"configured reviewer {configured} does not match token identity {observed}"
            )
        # Dry-run may use an operator's read-only credential to model an App.
        # No POST can occur in this mode.
        return configured
    identity = observed or (configured if not strict else None)
    if not identity:
        raise BridgeError(
            "reviewer identity is unknown; use a user token or verified App JWT launcher"
        )
    return identity


def _review_marker(verdict: Verdict, head: str) -> str:
    findings_digest = hashlib.sha256(
        "\n".join(verdict.public_findings).encode("utf-8")
    ).hexdigest()[:16]
    return (
        f"<!-- {MARKER_VERSION} card={verdict.card} "
        f"verdict={verdict.token} head={head} findings={findings_digest} -->"
    )


def build_review_body(verdict: Verdict, head: str, stale: bool) -> str:
    if verdict.classification == "PASS":
        findings = "- No blocking findings."
    else:
        findings = "\n".join(f"- {line}" for line in verdict.public_findings)
    stale_line = (
        "\n- **SUPERSEDED HEAD:** the PR moved after this review; the blocking verdict remains unresolved."
        if stale
        else ""
    )
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(verdict.created_at))
    return "\n".join([
        f"## Fleet review verdict: {verdict.token}",
        "",
        f"- Fleet card: `{verdict.card}`",
        f"- Reviewer seat: `{verdict.author}`",
        f"- Verdict recorded: `{stamp}`",
        f"- Reviewed exact head: `{head}`{stale_line}",
        "",
        "### Public-safe findings",
        findings,
        "",
        "This GitHub review is the public enforcement projection of the fleet board verdict.",
        _review_marker(verdict, head),
    ])


def _matching_review(
    reviews: Sequence[dict[str, Any]], identity: str, marker: str
) -> dict[str, Any] | None:
    for review in reviews:
        login = str((review.get("user") or {}).get("login") or "")
        if login.lower() != identity.lower():
            continue
        if marker in str(review.get("body") or ""):
            return review
    return None


def _changed_paths(gh: GhClient, repo: str, pr: int) -> list[str]:
    files = gh.pages(f"repos/{repo}/pulls/{pr}/files")
    paths: list[str] = []
    for file_info in files:
        for key in ("filename", "previous_filename"):
            value = file_info.get(key)
            if value and value not in paths:
                paths.append(str(value))
    return paths


def _head_scope_failure(
    gh: GhClient, repo: str, head: str, base_ref: str
) -> str | None:
    associated = gh.pages(f"repos/{repo}/commits/{head}/pulls")
    open_prs = sorted({
        int(item["number"])
        for item in associated
        if item.get("state") == "open" and item.get("number") is not None
    })
    if len(open_prs) > 1:
        joined = ",".join(f"#{number}" for number in open_prs)
        return f"ambiguous:head-shared-by-open-prs {joined}"
    if base_ref != "main":
        return f"base-not-main:{base_ref or 'unknown'}"
    return None


def sensitive_matches(
    paths: Iterable[str], patterns: Sequence[str] = SENSITIVE_PATHS
) -> list[str]:
    return sorted({
        path
        for path in paths
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
    })


def _status_payload(
    verdict: Verdict,
    sensitive: Sequence[str],
    context: str,
) -> dict[str, str]:
    # An explicit blocking review is authoritative regardless of path. Path
    # scoping decides whether a PR with *no* verdict may auto-skip; it must
    # never turn a known FIX_REQUIRED into success.
    if verdict.classification == "CHANGES":
        state = "failure"
        description = f"{verdict.token} from card {verdict.card}"
    elif not sensitive:
        state = "success"
        description = f"skip:no-sensitive-paths; card {verdict.card}"
    else:
        state = "success"
        description = f"PASS from card {verdict.card}"
    return {
        "state": state,
        "context": context,
        "description": description[:140],
    }


def _same_status(
    check_runs: Sequence[dict[str, Any]], identity: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the latest App-authored check run only when it is identical."""
    expected_slug = identity.removesuffix("[bot]").lower()
    for check_run in check_runs:
        if check_run.get("name") != payload["name"]:
            continue
        slug = str((check_run.get("app") or {}).get("slug") or "").lower()
        if slug != expected_slug:
            continue
        return (
            check_run
            if all(
                check_run.get(key) == payload[key]
                for key in ("conclusion", "external_id")
            )
            else None
        )
    return None


def _check_run_payload(head: str, status: dict[str, str]) -> dict[str, Any]:
    fingerprint = hashlib.sha256(
        "\n".join((
            status["context"],
            status["state"],
            status["description"],
            head,
        )).encode("utf-8")
    ).hexdigest()
    conclusion = "success" if status["state"] == "success" else "failure"
    return {
        "name": status["context"],
        "head_sha": head,
        "status": "completed",
        "conclusion": conclusion,
        "external_id": f"{MARKER_VERSION}:{fingerprint}",
        "output": {
            "title": f"Fleet review gate: {conclusion}",
            "summary": status["description"],
        },
    }


def _check_runs(
    gh: GhClient, repo: str, head: str, context: str
) -> list[dict[str, Any]]:
    encoded_name = urllib.parse.quote(context, safe="")
    response = gh.api(
        "GET",
        f"repos/{repo}/commits/{head}/check-runs?check_name={encoded_name}&per_page=100",
    )
    if not isinstance(response, dict) or not isinstance(
        response.get("check_runs"), list
    ):
        raise BridgeError(f"GitHub returned no check-run list for {repo}@{head[:12]}")
    return [item for item in response["check_runs"] if isinstance(item, dict)]


def project_verdict(
    gh: GhClient,
    verdict: Verdict,
    *,
    apply: bool,
    reviewer_login: str | None,
    context: str = DEFAULT_CONTEXT,
    sensitive_patterns: Sequence[str] = SENSITIVE_PATHS,
    review_only: bool = False,
) -> list[str]:
    if verdict.repo is None or verdict.pr is None:
        raise BridgeError(
            "no GitHub repository/PR could be resolved from the review card"
        )
    repo, pr = verdict.repo, verdict.pr
    pr_data = gh.api("GET", f"repos/{repo}/pulls/{pr}")
    if not isinstance(pr_data, dict):
        raise BridgeError(f"GitHub returned no PR data for {repo}#{pr}")
    live_head = str((pr_data.get("head") or {}).get("sha") or "")
    author = str((pr_data.get("user") or {}).get("login") or "")
    reviewed_head, reviewed_in_pr = resolve_reviewed_commit(gh, repo, pr, verdict.head)
    stale = reviewed_head != live_head
    if verdict.classification == "PASS" and stale:
        raise BridgeError(
            f"stale-head approval refused: reviewed {reviewed_head[:12]}, live {live_head[:12]}"
        )
    if verdict.classification == "CHANGES" and not verdict.public_findings:
        raise BridgeError(
            "changes verdict has no public-safe file:line finding; refusing to copy private board prose"
        )

    identity = resolve_identity(gh, reviewer_login, strict=apply)
    if identity.lower() == author.lower():
        raise BridgeError(
            f"reviewer identity equals PR author ({identity}); GitHub rejects self-review"
        )

    reviews = gh.pages(f"repos/{repo}/pulls/{pr}/reviews")
    event = "APPROVE" if verdict.classification == "PASS" else "REQUEST_CHANGES"
    body = build_review_body(verdict, reviewed_head, stale)
    marker = _review_marker(verdict, reviewed_head)
    review_commit = reviewed_head if reviewed_in_pr else live_head
    messages: list[str] = []
    duplicate = _matching_review(reviews, identity, marker)
    if duplicate is not None:
        messages.append(
            f"review no-op: identical marker already exists (id={duplicate.get('id')})"
        )
    elif apply:
        response = gh.api(
            "POST",
            f"repos/{repo}/pulls/{pr}/reviews",
            {"commit_id": review_commit, "body": body, "event": event},
        )
        review_id = response.get("id") if isinstance(response, dict) else None
        messages.append(f"review posted: event={event} id={review_id}")
    else:
        messages.append(
            f"dry-run review: event={event} commit={reviewed_head} card={verdict.card}"
        )

    if review_only:
        messages.append("check run skipped: explicit review-only mode")
        return messages

    changed_paths = _changed_paths(gh, repo, pr)
    sensitive = sensitive_matches(changed_paths, sensitive_patterns)
    base_ref = str((pr_data.get("base") or {}).get("ref") or "")
    scope_failure = _head_scope_failure(gh, repo, live_head, base_ref)
    status_payload = (
        {
            "state": "failure",
            "context": context,
            "description": scope_failure[:140],
        }
        if scope_failure
        else _status_payload(verdict, sensitive, context)
    )
    check_payload = _check_run_payload(live_head, status_payload)
    check_runs = _check_runs(gh, repo, live_head, context)
    duplicate_status = _same_status(check_runs, identity, check_payload)
    if duplicate_status is not None:
        messages.append(
            f"check run no-op: {context}={status_payload['state']} already exists "
            f"(id={duplicate_status.get('id')})"
        )
    elif apply:
        response = gh.api("POST", f"repos/{repo}/check-runs", check_payload)
        status_id = response.get("id") if isinstance(response, dict) else None
        messages.append(
            f"check run posted: {context}={status_payload['state']} id={status_id}"
        )
    else:
        messages.append(
            f"dry-run check run: {context}={status_payload['state']} "
            f"sensitive_paths={len(sensitive)} head={live_head}"
        )
    return messages


def project_unreviewed_pr(
    gh: GhClient,
    *,
    repo: str,
    pr_data: dict[str, Any],
    apply: bool,
    reviewer_login: str | None,
    board_blocker: str | None = None,
    context: str = DEFAULT_CONTEXT,
    sensitive_patterns: Sequence[str] = SENSITIVE_PATHS,
) -> list[str]:
    """Set the repo-wide required check run when no board verdict exists yet."""
    pr = int(pr_data["number"])
    live_head = str((pr_data.get("head") or {}).get("sha") or "")
    if not live_head:
        raise BridgeError(f"GitHub returned no head for {repo}#{pr}")
    identity = resolve_identity(gh, reviewer_login, strict=apply)
    sensitive = sensitive_matches(_changed_paths(gh, repo, pr), sensitive_patterns)
    base_ref = str((pr_data.get("base") or {}).get("ref") or "")
    scope_failure = _head_scope_failure(gh, repo, live_head, base_ref)
    payload = {
        "state": (
            "failure" if board_blocker or sensitive or scope_failure else "success"
        ),
        "context": context,
        "description": (
            scope_failure
            or board_blocker
            or (
                "review-required:no-board-verdict"
                if sensitive
                else "skip:no-sensitive-paths; no board verdict"
            )
        )[:140],
    }
    check_payload = _check_run_payload(live_head, payload)
    check_runs = _check_runs(gh, repo, live_head, context)
    duplicate = _same_status(check_runs, identity, check_payload)
    if duplicate is not None:
        return [
            f"check run no-op: {context}={payload['state']} already exists "
            f"(id={duplicate.get('id')})"
        ]
    if apply:
        response = gh.api("POST", f"repos/{repo}/check-runs", check_payload)
        status_id = response.get("id") if isinstance(response, dict) else None
        return [f"check run posted: {context}={payload['state']} id={status_id}"]
    return [
        f"dry-run check run: {context}={payload['state']} "
        f"sensitive_paths={len(sensitive)} head={live_head}"
    ]


def _atomic_write_cursor(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(f"{value}\n")
        temporary = Path(handle.name)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _manual_verdict(args: argparse.Namespace) -> Verdict:
    token = args.verdict.upper()
    classification = "PASS" if token in PASS_TOKENS else "CHANGES"
    findings_text = (
        Path(args.body_file).read_text(encoding="utf-8") if args.body_file else ""
    )
    return Verdict(
        card=args.card_ref or "manual",
        token=token,
        classification=classification,
        head=args.head,
        repo=args.repo,
        pr=args.pr,
        author=args.author or "manual",
        created_at=int(time.time()),
        public_findings=extract_public_findings(findings_text),
        title="",
    )


def cmd_post(args: argparse.Namespace, gh: GhClient, board: BoardSource | None) -> int:
    if args.card:
        if board is None:  # defensive; main constructs it for card mode
            raise BridgeError("board source unavailable for --card")
        verdicts = board.verdicts()
        card_verdict = latest_by_card(verdicts).get(args.card)
        if card_verdict is None:
            raise BridgeError(f"no structured verdict found on card {args.card}")
        if card_verdict.repo is None or card_verdict.pr is None:
            raise BridgeError(f"card {args.card} does not identify a GitHub PR")
        current = latest_by_pr(verdicts).get((
            card_verdict.repo.lower(),
            card_verdict.pr,
        ))
        if current != card_verdict:
            raise BridgeError(
                f"card {args.card} is superseded by latest PR verdict on {current.card if current else 'unknown'}"
            )
        verdict = card_verdict
    else:
        verdict = _manual_verdict(args)
    for message in project_verdict(
        gh,
        verdict,
        apply=args.apply,
        reviewer_login=args.reviewer_login,
        context=args.context,
        sensitive_patterns=tuple(args.sensitive_path or SENSITIVE_PATHS),
        review_only=args.review_only,
    ):
        print(message)
    return 0


def cmd_scan(args: argparse.Namespace, gh: GhClient, board: BoardSource) -> int:
    since = args.since
    cursor_path = Path(args.cursor_file).expanduser() if args.cursor_file else None
    if since is None and cursor_path and cursor_path.exists():
        try:
            since = int(cursor_path.read_text(encoding="utf-8").strip() or "0")
        except ValueError as exc:
            raise BridgeError(f"invalid cursor file {cursor_path}") from exc
    since = since or 0
    verdicts = board.verdicts()
    current = latest_by_pr(verdicts)
    ambiguous = latest_ambiguous_by_pr_number(verdicts)
    newest = max(
        since, max((verdict.created_at for verdict in verdicts), default=since)
    )
    repositories = args.repo or ["o269/omnia"]
    patterns = tuple(args.sensitive_path or SENSITIVE_PATHS)
    had_errors = False
    for repo in repositories:
        try:
            open_prs = gh.pages(
                f"repos/{repo}/pulls?state=open&sort=updated&direction=desc"
            )
        except BridgeError as exc:
            had_errors = True
            print(f"{repo} REFUSED: {exc}", file=sys.stderr)
            continue
        for pr_data in open_prs:
            pr = int(pr_data["number"])
            verdict = current.get((repo.lower(), pr))
            ambiguous_verdict = ambiguous.get(pr)
            board_blocker = None
            if ambiguous_verdict is not None and (
                verdict is None or ambiguous_verdict.created_at >= verdict.created_at
            ):
                board_blocker = (
                    "review-required:ambiguous-board-target "
                    f"card={ambiguous_verdict.card} verdict={ambiguous_verdict.token}"
                )
                verdict = None
            try:
                if verdict is None:
                    suffix = f" blocker={board_blocker}" if board_blocker else ""
                    print(f"{repo}#{pr} verdict=NONE{suffix}")
                    messages = project_unreviewed_pr(
                        gh,
                        repo=repo,
                        pr_data=pr_data,
                        apply=args.apply,
                        reviewer_login=args.reviewer_login,
                        board_blocker=board_blocker,
                        context=args.context,
                        sensitive_patterns=patterns,
                    )
                else:
                    print(f"{repo}#{pr} card={verdict.card} verdict={verdict.token}")
                    messages = project_verdict(
                        gh,
                        verdict,
                        apply=args.apply,
                        reviewer_login=args.reviewer_login,
                        context=args.context,
                        sensitive_patterns=patterns,
                    )
            except BridgeError as exc:
                had_errors = True
                print(f"  REFUSED: {exc}", file=sys.stderr)
                continue
            for message in messages:
                print(f"  {message}")
    if had_errors:
        print(f"cursor not advanced; reconciliation had errors (candidate {newest})")
        return 2
    if args.apply and cursor_path:
        _atomic_write_cursor(cursor_path, newest)
        print(f"cursor advanced: {cursor_path} -> {newest}")
    else:
        print(f"cursor would advance to {newest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--apply", action="store_true", help="perform GitHub writes")
    common.add_argument(
        "--reviewer-login",
        default=os.environ.get("FVB_REVIEWER_LOGIN"),
        help="expected reviewer bot login; required for GitHub App installation tokens",
    )
    common.add_argument("--context", default=DEFAULT_CONTEXT)
    common.add_argument(
        "--sensitive-path",
        action="append",
        help="override path-aware gate glob; repeat for multiple globs",
    )

    post = subparsers.add_parser("post", parents=[common])
    post.add_argument("--card")
    post.add_argument("--card-ref")
    post.add_argument("--repo")
    post.add_argument("--pr", type=int)
    post.add_argument("--verdict", choices=sorted(PASS_TOKENS | CHANGES_TOKENS))
    post.add_argument("--head")
    post.add_argument("--body-file")
    post.add_argument("--author")
    post.add_argument(
        "--review-only",
        action="store_true",
        help="post/prove the native review without projecting the Check Run",
    )

    scan = subparsers.add_parser("scan", parents=[common])
    scan.add_argument("--since", type=int)
    scan.add_argument(
        "--repo",
        action="append",
        help="repository to reconcile; repeat for multiple repos (default: o269/omnia)",
    )
    scan.add_argument(
        "--cursor-file",
        default="~/.hermes/state/fleet-verdict-bridge.cursor",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "post" and not args.card:
        if not (args.repo and args.pr and args.verdict and args.head):
            parser.error("post requires --card or --repo/--pr/--verdict/--head")
    try:
        gh = github_client_from_environment()
        board = BoardSource() if args.command == "scan" or args.card else None
        if args.command == "post":
            return cmd_post(args, gh, board)
        if board is None:  # defensive; scan always constructs it
            raise BridgeError("board source unavailable for scan")
        return cmd_scan(args, gh, board)
    except BridgeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
