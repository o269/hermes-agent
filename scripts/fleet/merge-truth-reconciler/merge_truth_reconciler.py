#!/usr/bin/env python3
"""Fail-closed reconciliation of GitHub merge truth into board gate state.

Production board mutations are deliberately delegated to a broker-side helper whose
capability handshake proves the post-custody contracts.  This module never opens a
board database and has no SQL production fallback.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

IDENTITY = "merge-truth-reconciler"
STATE_VERSION = 2
PR_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)",
    re.IGNORECASE,
)
SPLIT_REPOSITORY_RE = re.compile(
    r"^\s*repo(?:sitory)?\s*:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*$",
    re.IGNORECASE,
)
SPLIT_PR_RE = re.compile(r"^\s*pr\s*:\s*#?([1-9][0-9]*)\s*$", re.IGNORECASE)
EXACT_GATE_RE = re.compile(
    r"GATE: PR-MERGE (https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*)"
)
REQUIRED_CAPABILITIES = frozenset(
    {
        "inventory_hex_v1",
        "card_citations_hex_v2",
        "card_authority_v1",
        "ownership_converge_v1",
        "semantic_url_release_v1",
        "gate_complete_opid_v1",
        "evidence_comment_opid_v1",
        "recompute_ready_receipts_v1",
    }
)
TERMINAL_STATUSES = frozenset(
    {
        "done",
        "archived",
        "cancelled",
        "canceled",
        "failed",
        "error",
        "errored",
        "timed_out",
        "timed-out",
        "timedout",
        "timeout",
        "rejected",
        "aborted",
        "abandoned",
    }
)
ACTIVE_STATUSES = frozenset({"running", "in_progress", "in-progress"})
ARTIFACT_NAME_RE = re.compile(
    r"(?:receipt|land|merge|pr[-_#]|handoff|result|complete|done)", re.IGNORECASE
)
ARTIFACT_SUFFIXES = frozenset({".json", ".md", ".txt"})
TIMESTAMPED_REPORT_RE = re.compile(r"^(\d{8}T\d{6}Z)\.(json|md)$")
BLOCKER_MARKERS = re.compile(
    r"\b(block(?:ed|er|ing)?|gate|dependency|await(?:ing)?[- ](?:land|merge))\b",
    re.IGNORECASE,
)


class ReconcilerError(RuntimeError):
    """Base error that is safe to reduce to a single journal summary."""


class ConfigError(ReconcilerError):
    """Environment configuration is invalid or weakens a hard safety bound."""


class CapabilityBlocked(ReconcilerError):
    """The installed broker surface cannot prove the required semantics."""


class FeedFailure(ReconcilerError):
    """The complete GitHub feed could not be established."""


class BoardFailure(ReconcilerError):
    """A broker-mediated board action failed or returned ambiguous evidence."""


class InventoryFailure(ReconcilerError):
    """Artifact inventory was incomplete or exceeded a strict operational bound."""


@dataclass(frozen=True)
class Config:
    enabled: bool
    artifacts_dir: Path
    state_dir: Path
    report_dir: Path
    status_ticker: Path
    alert_dir: Path
    sdb_mount: Path = Path("/mnt/HC_Volume_106418160")
    load1_max: float = 12.0
    sdb_min_free_kib: int = 15 * 1024 * 1024
    board_helper: Path | None = None
    inventory_ttl_seconds: int = 86_400
    bootstrap_days: int = 30
    overlap_seconds: int = 600
    request_timeout_seconds: int = 20
    helper_timeout_seconds: int = 30
    max_pages_per_repo: int = 20
    artifact_max_candidates: int = 500
    artifact_max_file_bytes: int = 1_000_000
    artifact_max_total_bytes: int = 20_000_000
    artifact_max_elapsed_ms: int = 5_000
    report_retention_count: int = 100
    report_retention_days: int = 30
    github_api_base: str = "https://api.github.com"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "tick.lock"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        values = os.environ if env is None else env
        home = Path(values.get("HOME", str(Path.home()))).expanduser()
        enabled = values.get("MERGE_TRUTH_RECONCILER_ENABLED", "0").strip() == "1"
        helper_text = values.get("MERGE_TRUTH_RECONCILER_BOARD_HELPER", "").strip()
        return cls(
            enabled=enabled,
            artifacts_dir=_configured_path(
                values,
                "MERGE_TRUTH_RECONCILER_SOURCE_ARTIFACTS",
                home / "godmode-bus" / "artifacts",
                home,
            ),
            state_dir=_configured_path(
                values,
                "MERGE_TRUTH_RECONCILER_STATE_DIR",
                home / ".local" / "state" / IDENTITY,
                home,
            ),
            report_dir=_configured_path(
                values,
                "MERGE_TRUTH_RECONCILER_REPORT_DIR",
                home / "godmode-bus" / "artifacts" / IDENTITY,
                home,
            ),
            status_ticker=_configured_path(
                values,
                "MERGE_TRUTH_RECONCILER_STATUS_TICKER",
                home / "godmode-bus" / "STATUS-TICKER.md",
                home,
            ),
            alert_dir=_configured_path(
                values,
                "MERGE_TRUTH_RECONCILER_ALERT_DIR",
                home / "godmode-bus" / "to-claude",
                home,
            ),
            sdb_mount=_configured_path(
                values,
                "MERGE_TRUTH_RECONCILER_SDB_MOUNT",
                Path("/mnt/HC_Volume_106418160"),
                home,
            ),
            load1_max=_maximum_float(
                values, "MERGE_TRUTH_RECONCILER_LOAD1_MAX", 12.0, 12.0
            ),
            sdb_min_free_kib=_minimum_int(
                values,
                "MERGE_TRUTH_RECONCILER_SDB_MIN_FREE_KIB",
                15 * 1024 * 1024,
                15 * 1024 * 1024,
            ),
            board_helper=(
                Path(helper_text.replace("%h", str(home))).expanduser()
                if helper_text
                else None
            ),
            inventory_ttl_seconds=_maximum_int(
                values,
                "MERGE_TRUTH_RECONCILER_INVENTORY_TTL_SECONDS",
                86_400,
                86_400,
            ),
            bootstrap_days=_maximum_int(
                values, "MERGE_TRUTH_RECONCILER_BOOTSTRAP_DAYS", 30, 30
            ),
            overlap_seconds=_maximum_int(
                values, "MERGE_TRUTH_RECONCILER_OVERLAP_SECONDS", 600, 600
            ),
            request_timeout_seconds=_maximum_int(
                values, "MERGE_TRUTH_RECONCILER_REQUEST_TIMEOUT_SECONDS", 20, 20
            ),
            helper_timeout_seconds=_maximum_int(
                values, "MERGE_TRUTH_RECONCILER_HELPER_TIMEOUT_SECONDS", 30, 30
            ),
            max_pages_per_repo=_maximum_int(
                values, "MERGE_TRUTH_RECONCILER_MAX_PAGES_PER_REPO", 20, 20
            ),
            artifact_max_candidates=_maximum_int(
                values, "MERGE_TRUTH_RECONCILER_ARTIFACT_MAX_CANDIDATES", 500, 500
            ),
            artifact_max_file_bytes=_maximum_int(
                values,
                "MERGE_TRUTH_RECONCILER_ARTIFACT_MAX_FILE_BYTES",
                1_000_000,
                1_000_000,
            ),
            artifact_max_total_bytes=_maximum_int(
                values,
                "MERGE_TRUTH_RECONCILER_ARTIFACT_MAX_TOTAL_BYTES",
                20_000_000,
                20_000_000,
            ),
            artifact_max_elapsed_ms=_maximum_int(
                values, "MERGE_TRUTH_RECONCILER_ARTIFACT_MAX_ELAPSED_MS", 5_000, 5_000
            ),
            report_retention_count=_maximum_int(
                values, "MERGE_TRUTH_RECONCILER_REPORT_RETENTION_COUNT", 100, 100
            ),
            report_retention_days=_maximum_int(
                values, "MERGE_TRUTH_RECONCILER_REPORT_RETENTION_DAYS", 30, 30
            ),
            github_api_base=values.get(
                "MERGE_TRUTH_RECONCILER_GITHUB_API_BASE", "https://api.github.com"
            ).rstrip("/"),
        )


def _positive_int(values: Mapping[str, str], key: str, default: int) -> int:
    raw = values.get(key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid integer configuration: {key}") from exc
    if value <= 0:
        raise ConfigError(f"configuration must be positive: {key}")
    return value


def _maximum_int(
    values: Mapping[str, str], key: str, default: int, maximum: int
) -> int:
    value = _positive_int(values, key, default)
    if value > maximum:
        raise ConfigError(f"configuration may not weaken safety ceiling: {key}")
    return value


def _configured_path(
    values: Mapping[str, str], key: str, default: Path, home: Path
) -> Path:
    return Path(values.get(key, str(default)).replace("%h", str(home))).expanduser()


def _minimum_int(
    values: Mapping[str, str], key: str, default: int, minimum: int
) -> int:
    value = _positive_int(values, key, default)
    if value < minimum:
        raise ConfigError(f"configuration may not weaken safety floor: {key}")
    return value


def _maximum_float(
    values: Mapping[str, str], key: str, default: float, maximum: float
) -> float:
    raw = values.get(key, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid numeric configuration: {key}") from exc
    if value <= 0 or value > maximum:
        raise ConfigError(f"configuration may not weaken safety ceiling: {key}")
    return value


@dataclass(frozen=True)
class MergeRecord:
    canonical_url: str
    repository: str
    number: int
    merge_sha: str
    merged_at: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MergeRecord":
        return cls(
            canonical_url=canonical_pr_url(str(value["canonical_url"])),
            repository=normalize_repository(str(value["repository"])),
            number=int(value["number"]),
            merge_sha=str(value["merge_sha"]),
            merged_at=format_time(parse_time(str(value["merged_at"]))),
        )


@dataclass(frozen=True)
class InventoryPayload:
    canonical_urls: tuple[str, ...] = ()
    hex_texts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactInventory:
    canonical_urls: tuple[str, ...]
    candidates: int
    scanned: int
    skipped: int
    scanned_bytes: int


@dataclass(frozen=True)
class GuardResult:
    cleared_rows: int
    affected_task_ids: tuple[str, ...]
    semantic_released: bool


@dataclass(frozen=True)
class CardSnapshot:
    task_id: str
    title: str
    body: str
    comments: tuple[str, ...]
    status: str
    terminal: bool
    protected_custody: bool
    active_run: bool
    assignee: str = ""
    skills: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CardSnapshot":
        status_value = value.get("status")
        if not isinstance(status_value, str) or not status_value.strip():
            raise BoardFailure("broker card row lacked a valid status")
        status = status_value.strip().lower()
        authorities: dict[str, bool] = {}
        for field_name in ("terminal", "protected_custody", "active_run"):
            field_value = value.get(field_name)
            if not isinstance(field_value, bool):
                raise BoardFailure(
                    f"broker card row lacked boolean authority: {field_name}"
                )
            authorities[field_name] = field_value
        if status in TERMINAL_STATUSES and not authorities["terminal"]:
            raise BoardFailure("broker terminal authority contradicted canonical status")
        if status in ACTIVE_STATUSES and not authorities["active_run"]:
            raise BoardFailure("broker active-run authority contradicted canonical status")
        if authorities["terminal"] and authorities["active_run"]:
            raise BoardFailure("broker card row claimed terminal and active-run authority")
        body = str(value.get("body", ""))
        if "body_hex" in value:
            body = decode_hex_text(str(value["body_hex"]))
        comments = tuple(str(item) for item in value.get("comments", []))
        if "comments_hex" in value:
            comments = tuple(decode_hex_text(str(item)) for item in value["comments_hex"])
        return cls(
            task_id=str(value["task_id"]),
            title=str(value.get("title", "")),
            body=body,
            comments=comments,
            status=status,
            terminal=authorities["terminal"],
            protected_custody=authorities["protected_custody"],
            active_run=authorities["active_run"],
            assignee=str(value.get("assignee", "")),
            skills=tuple(str(item) for item in value.get("skills", [])),
        )


@dataclass
class ReconcilerState:
    version: int = STATE_VERSION
    watermark: str | None = None
    repository_cache: dict[str, Any] = field(default_factory=dict)
    etag_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    processed_merges: dict[str, dict[str, Any]] = field(default_factory=dict)
    first_observed: dict[str, dict[str, Any]] = field(default_factory=dict)
    alert_receipts: dict[str, str] = field(default_factory=dict)
    alert_outbox: dict[str, dict[str, str]] = field(default_factory=dict)
    tick_counter: int = 0

    @classmethod
    def load(cls, path: Path) -> "ReconcilerState":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReconcilerError("state file is unreadable or invalid") from exc
        if not isinstance(raw, Mapping):
            raise ReconcilerError("state file is unreadable or invalid")
        if raw.get("version") != STATE_VERSION:
            raise ReconcilerError("unsupported state version")
        return cls(
            version=STATE_VERSION,
            watermark=raw.get("watermark"),
            repository_cache=dict(raw.get("repository_cache", {})),
            etag_cache=dict(raw.get("etag_cache", {})),
            processed_merges=dict(raw.get("processed_merges", {})),
            first_observed=dict(raw.get("first_observed", {})),
            alert_receipts=dict(raw.get("alert_receipts", {})),
            alert_outbox=dict(raw.get("alert_outbox", {})),
            tick_counter=int(raw.get("tick_counter", 0)),
        )

    def save(self, path: Path) -> None:
        atomic_write_text(
            path,
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n",
            0o600,
        )


@dataclass
class TickReport:
    status: str
    observed_at: str
    merges_seen: int = 0
    ownership_rows_cleared: int = 0
    guard_urls_semantically_released: int = 0
    cards_closed: list[str] = field(default_factory=list)
    children_promoted: int = 0
    evidence_comments: list[dict[str, str]] = field(default_factory=list)
    stale_gate_items: list[dict[str, str]] = field(default_factory=list)
    exclusions: list[dict[str, str]] = field(default_factory=list)
    slo_breaches: list[dict[str, str]] = field(default_factory=list)
    api_calls: int = 0
    api_not_modified: int = 0
    artifact_cache_hit: bool = False
    artifact_candidates: int = 0
    artifact_files_scanned: int = 0
    artifact_files_skipped: int = 0
    artifact_bytes_scanned: int = 0
    reports_pruned: int = 0
    watermark_before: str | None = None
    watermark_after: str | None = None
    admission: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["identity"] = IDENTITY
        result["stale_queue_size"] = len(self.stale_gate_items)
        return result


@dataclass(frozen=True)
class AdmissionResult:
    allowed: bool
    reason: str
    load1: float | None
    sdb_free_kib: int | None

    def report_fields(self, config: Config) -> dict[str, str]:
        return {
            "reason": self.reason,
            "load1": "unavailable" if self.load1 is None else f"{self.load1:.2f}",
            "load1_max": f"{config.load1_max:.2f}",
            "sdb_free_kib": (
                "unavailable" if self.sdb_free_kib is None else str(self.sdb_free_kib)
            ),
            "sdb_min_free_kib": str(config.sdb_min_free_kib),
        }


class BoardAdapter(Protocol):
    def validate_capabilities(self) -> None: ...

    def inventory_payload(self) -> InventoryPayload: ...

    def converge_ownership(self, merge: MergeRecord, operation_id: str) -> GuardResult: ...

    def list_cards_citing(self, canonical_urls: Sequence[str]) -> Sequence[CardSnapshot]: ...

    def complete_gate_card(
        self, task_id: str, receipt: str, operation_id: str
    ) -> bool: ...

    def add_evidence_comment(
        self, task_id: str, receipt: str, operation_id: str
    ) -> bool: ...

    def recompute_ready(self, operation_id: str) -> int: ...


@dataclass(frozen=True)
class FeedResult:
    merges: tuple[MergeRecord, ...]
    api_calls: int
    not_modified: int


class MergeFeed(Protocol):
    def fetch(
        self,
        repositories: Sequence[str],
        cutoff: datetime,
        state: ReconcilerState,
    ) -> FeedResult: ...


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(self, url: str, headers: Mapping[str, str], timeout: int) -> HttpResponse: ...


class UrllibTransport:
    def request(self, url: str, headers: Mapping[str, str], timeout: int) -> HttpResponse:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return HttpResponse(status=304, headers=dict(exc.headers.items()), body=b"")
            raise FeedFailure(f"GitHub HTTP failure: status={exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FeedFailure("GitHub transport failure") from exc


class GitHubFeed:
    def __init__(
        self,
        api_base: str,
        timeout_seconds: int,
        max_pages_per_repo: int,
        token_provider: Callable[[], str] | None = None,
        transport: HttpTransport | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_pages_per_repo = max_pages_per_repo
        self.token_provider = token_provider or gh_auth_token
        self.transport = transport or UrllibTransport()

    def fetch(
        self,
        repositories: Sequence[str],
        cutoff: datetime,
        state: ReconcilerState,
    ) -> FeedResult:
        token = self.token_provider().strip()
        if not token:
            raise FeedFailure("GitHub authentication token unavailable")
        headers_base = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": IDENTITY,
        }
        found: dict[str, MergeRecord] = {}
        api_calls = 0
        not_modified = 0
        for repository in sorted(set(repositories)):
            owner, repo = normalize_repository(repository).split("/", 1)
            horizon_reached = False
            for page in range(1, self.max_pages_per_repo + 1):
                url = (
                    f"{self.api_base}/repos/{urllib.parse.quote(owner)}/"
                    f"{urllib.parse.quote(repo)}/pulls?state=closed&sort=updated&"
                    f"direction=desc&per_page=100&page={page}"
                )
                cached = state.etag_cache.get(url, {})
                headers = dict(headers_base)
                if cached.get("etag"):
                    headers["If-None-Match"] = str(cached["etag"])
                response = self.transport.request(url, headers, self.timeout_seconds)
                api_calls += 1
                if response.status == 304:
                    not_modified += 1
                    if "items" not in cached:
                        raise FeedFailure("GitHub returned 304 without a cached page")
                    rows = cached["items"]
                elif response.status == 200:
                    try:
                        decoded = json.loads(response.body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise FeedFailure("GitHub returned invalid JSON") from exc
                    if not isinstance(decoded, list):
                        raise FeedFailure("GitHub pulls response was not a list")
                    rows = [_minimal_pull_row(row) for row in decoded]
                    etag = _header(response.headers, "etag")
                    state.etag_cache[url] = {"etag": etag, "items": rows}
                else:
                    raise FeedFailure(f"GitHub HTTP failure: status={response.status}")

                if not isinstance(rows, list):
                    raise FeedFailure("cached GitHub page was invalid")
                page_old_enough = False
                for raw in rows:
                    if not isinstance(raw, Mapping):
                        raise FeedFailure("GitHub pull row was invalid")
                    try:
                        updated_at = parse_time(str(raw["updated_at"]))
                    except (KeyError, ValueError) as exc:
                        raise FeedFailure("GitHub pull row lacked a valid updated_at") from exc
                    if updated_at < cutoff:
                        page_old_enough = True
                    merged_text = raw.get("merged_at")
                    if not merged_text:
                        continue
                    merged_at = parse_time(str(merged_text))
                    if merged_at < cutoff:
                        continue
                    number = int(raw["number"])
                    url_value = raw.get("html_url") or (
                        f"https://github.com/{owner}/{repo}/pull/{number}"
                    )
                    canonical = canonical_pr_url(str(url_value))
                    sha = str(raw.get("merge_commit_sha") or "").strip()
                    if not sha:
                        raise FeedFailure(f"merged PR lacked merge SHA: {canonical}")
                    found[canonical] = MergeRecord(
                        canonical_url=canonical,
                        repository=f"{owner}/{repo}",
                        number=number,
                        merge_sha=sha,
                        merged_at=format_time(merged_at),
                    )
                if len(rows) < 100 or page_old_enough:
                    horizon_reached = True
                    break
            if not horizon_reached:
                raise FeedFailure(
                    f"GitHub pagination limit reached before cutoff: {repository}"
                )
        return FeedResult(tuple(sorted(found.values(), key=lambda item: item.canonical_url)), api_calls, not_modified)


def _minimal_pull_row(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise FeedFailure("GitHub pull row was invalid")
    return {
        "number": raw.get("number"),
        "html_url": raw.get("html_url"),
        "updated_at": raw.get("updated_at"),
        "merged_at": raw.get("merged_at"),
        "merge_commit_sha": raw.get("merge_commit_sha"),
    }


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def gh_auth_token() -> str:
    try:
        completed = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FeedFailure("gh auth token invocation failed") from exc
    if completed.returncode != 0:
        raise FeedFailure("gh auth token invocation failed")
    return completed.stdout.strip()


class JsonHelperBoardAdapter:
    """Broker-only adapter using a capability-versioned JSON/stdin helper.

    Production uses scripts/fleet/merge-truth-reconciler/merge_truth_board_helper.py
    over boardd (post-PR36/PR38/PR41).  No request data is placed in argv.
    """

    def __init__(self, helper: Path, timeout_seconds: int) -> None:
        self.helper = helper
        self.timeout_seconds = timeout_seconds

    def _call(self, action: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.helper.is_file() or not os.access(self.helper, os.X_OK):
            raise CapabilityBlocked("configured board helper is not executable")
        request = json.dumps({"action": action, "author": IDENTITY, "payload": payload})
        try:
            completed = subprocess.run(
                [str(self.helper)],
                input=request,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BoardFailure(f"board helper failed: action={action}") from exc
        if completed.returncode != 0:
            raise BoardFailure(f"board helper failed: action={action}")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise BoardFailure(f"board helper returned invalid JSON: action={action}") from exc
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            raise BoardFailure(f"board helper rejected action={action}")
        result = response.get("result", {})
        if not isinstance(result, Mapping):
            raise BoardFailure(f"board helper returned invalid result: action={action}")
        return result

    def validate_capabilities(self) -> None:
        result = self._call("capabilities", {})
        actual = {str(item) for item in result.get("capabilities", [])}
        missing = sorted(REQUIRED_CAPABILITIES - actual)
        if missing:
            raise CapabilityBlocked(
                "board helper missing required capabilities: " + ",".join(missing)
            )

    def inventory_payload(self) -> InventoryPayload:
        result = self._call("inventory", {"body_encoding": "hex"})
        canonical_urls = result.get("canonical_urls", [])
        hex_texts = result.get("hex_texts", [])
        if not isinstance(canonical_urls, list) or not all(
            isinstance(item, str) for item in canonical_urls
        ):
            raise BoardFailure("board helper returned invalid canonical URL inventory")
        if not isinstance(hex_texts, list) or not all(
            isinstance(item, str) for item in hex_texts
        ):
            raise BoardFailure("board helper returned invalid hex inventory")
        return InventoryPayload(
            canonical_urls=tuple(canonical_urls),
            hex_texts=tuple(hex_texts),
        )

    def converge_ownership(self, merge: MergeRecord, operation_id: str) -> GuardResult:
        result = self._call(
            "converge_ownership",
            {"merge": asdict(merge), "operation_id": operation_id, "atomic": True},
        )
        semantic = result.get("semantic_released") is True
        if not semantic:
            raise CapabilityBlocked(
                f"broker could not prove semantic URL release: {merge.canonical_url}"
            )
        return GuardResult(
            cleared_rows=int(result.get("cleared_rows", 0)),
            affected_task_ids=tuple(str(item) for item in result.get("affected_task_ids", [])),
            semantic_released=True,
        )

    def list_cards_citing(self, canonical_urls: Sequence[str]) -> Sequence[CardSnapshot]:
        result = self._call(
            "list_cards_citing",
            {"canonical_urls": list(canonical_urls), "body_encoding": "hex"},
        )
        cards = result.get("cards", [])
        if not isinstance(cards, list) or not all(isinstance(item, Mapping) for item in cards):
            raise BoardFailure("board helper returned invalid card list")
        return tuple(CardSnapshot.from_dict(item) for item in cards)

    def complete_gate_card(self, task_id: str, receipt: str, operation_id: str) -> bool:
        result = self._call(
            "complete_gate_card",
            {"task_id": task_id, "receipt": receipt, "operation_id": operation_id},
        )
        return result.get("changed") is True

    def add_evidence_comment(self, task_id: str, receipt: str, operation_id: str) -> bool:
        result = self._call(
            "add_evidence_comment",
            {"task_id": task_id, "receipt": receipt, "operation_id": operation_id},
        )
        return result.get("changed") is True

    def recompute_ready(self, operation_id: str) -> int:
        result = self._call("recompute_ready", {"operation_id": operation_id})
        if "actual_promoted_count" not in result:
            raise CapabilityBlocked("promotion helper did not return receipt-backed actual count")
        return int(result["actual_promoted_count"])


def canonical_pr_url(value: str) -> str:
    match = PR_URL_RE.search(value.strip())
    if not match:
        raise ValueError("not a canonical GitHub pull-request URL")
    owner, repo, number = match.groups()
    return f"https://github.com/{owner.lower()}/{repo.lower()}/pull/{int(number)}"


def normalize_repository(value: str) -> str:
    parts = value.strip().strip("/").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise ValueError("invalid GitHub repository")
    return f"{parts[0].lower()}/{parts[1].lower()}"


def extract_pr_urls(text: str) -> set[str]:
    urls: set[str] = set()
    for match in PR_URL_RE.finditer(text):
        urls.add(canonical_pr_url(match.group(0)))
    return urls


def extract_inventory_pr_urls(text: str) -> set[str]:
    """Extract full URLs and line-oriented split ``repo`` / ``pr`` receipts."""
    urls = extract_pr_urls(text)
    current_repository: str | None = None
    for line in text.splitlines():
        repository_match = SPLIT_REPOSITORY_RE.fullmatch(line)
        if repository_match:
            try:
                current_repository = normalize_repository(repository_match.group(1))
            except ValueError:
                current_repository = None
            continue
        pr_match = SPLIT_PR_RE.fullmatch(line)
        if pr_match and current_repository is not None:
            urls.add(
                canonical_pr_url(
                    f"https://github.com/{current_repository}/pull/{int(pr_match.group(1))}"
                )
            )
    return urls


def repository_from_url(url: str) -> str:
    match = PR_URL_RE.fullmatch(canonical_pr_url(url))
    if not match:
        raise ValueError("invalid pull-request URL")
    return normalize_repository(f"{match.group(1)}/{match.group(2)}")


def decode_hex_citations(values: Iterable[str]) -> set[str]:
    urls: set[str] = set()
    for value in values:
        try:
            decoded = bytes.fromhex(value).decode("utf-8", errors="replace")
        except (ValueError, TypeError) as exc:
            raise BoardFailure("broker returned invalid hex inventory") from exc
        urls.update(extract_inventory_pr_urls(decoded))
    return urls


def decode_hex_text(value: str) -> str:
    try:
        return bytes.fromhex(value).decode("utf-8", errors="replace")
    except (ValueError, TypeError) as exc:
        raise BoardFailure("broker returned invalid hex text") from exc


def artifact_pr_urls(
    config: Config, now: datetime, lookback_days: int = 30
) -> ArtifactInventory:
    directory = config.artifacts_dir
    cutoff = now.timestamp() - lookback_days * 86_400
    urls: set[str] = set()
    if not directory.is_dir():
        raise InventoryFailure("artifact inventory directory is missing")
    started = time.monotonic()
    candidates = 0
    scanned = 0
    skipped = 0
    scanned_bytes = 0
    for path in directory.rglob("*"):
        elapsed_ms = int((time.monotonic() - started) * 1_000)
        if elapsed_ms > config.artifact_max_elapsed_ms:
            raise InventoryFailure("artifact inventory elapsed-time limit exceeded")
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix.lower() not in ARTIFACT_SUFFIXES or not ARTIFACT_NAME_RE.search(
                path.name
            ):
                skipped += 1
                continue
            candidates += 1
            if candidates > config.artifact_max_candidates:
                raise InventoryFailure("artifact inventory candidate-file limit exceeded")
            stat = path.stat()
            if stat.st_mtime < cutoff:
                skipped += 1
                continue
            if stat.st_size > config.artifact_max_file_bytes:
                raise InventoryFailure(
                    f"artifact inventory per-file byte limit exceeded: {path.name}"
                )
            if scanned_bytes + stat.st_size > config.artifact_max_total_bytes:
                raise InventoryFailure("artifact inventory total-byte limit exceeded")
            content = path.read_bytes()
            if int((time.monotonic() - started) * 1_000) > config.artifact_max_elapsed_ms:
                raise InventoryFailure("artifact inventory elapsed-time limit exceeded")
            if len(content) > config.artifact_max_file_bytes:
                raise InventoryFailure(
                    f"artifact inventory file grew beyond byte limit: {path.name}"
                )
            if scanned_bytes + len(content) > config.artifact_max_total_bytes:
                raise InventoryFailure("artifact inventory total-byte limit exceeded")
            scanned += 1
            scanned_bytes += len(content)
            urls.update(
                extract_inventory_pr_urls(
                    content.decode("utf-8", errors="replace")
                )
            )
        except InventoryFailure:
            raise
        except OSError as exc:
            raise InventoryFailure(f"artifact inventory read failed: {path.name}") from exc
    if int((time.monotonic() - started) * 1_000) > config.artifact_max_elapsed_ms:
        raise InventoryFailure("artifact inventory elapsed-time limit exceeded")
    return ArtifactInventory(
        canonical_urls=tuple(sorted(urls)),
        candidates=candidates,
        scanned=scanned,
        skipped=skipped,
        scanned_bytes=scanned_bytes,
    )


def derive_repositories(
    config: Config,
    state: ReconcilerState,
    board: BoardAdapter,
    now: datetime,
    report: TickReport,
) -> tuple[str, ...]:
    inventory = board.inventory_payload()
    board_urls: set[str] = set()
    for raw in inventory.canonical_urls:
        try:
            board_urls.add(canonical_pr_url(raw))
        except ValueError as exc:
            raise BoardFailure("broker returned invalid canonical URL inventory") from exc
    board_urls.update(decode_hex_citations(inventory.hex_texts))

    cache = state.repository_cache
    try:
        expires_at = parse_time(str(cache.get("expires_at", "")))
    except ValueError:
        expires_at = datetime.min.replace(tzinfo=timezone.utc)
    cached_repositories = cache.get("artifact_repositories")
    if expires_at > now and isinstance(cached_repositories, list):
        report.artifact_cache_hit = True
        artifact_repositories = {
            normalize_repository(str(item)) for item in cached_repositories
        }
    else:
        artifact_inventory = artifact_pr_urls(config, now)
        report.artifact_candidates = artifact_inventory.candidates
        report.artifact_files_scanned = artifact_inventory.scanned
        report.artifact_files_skipped = artifact_inventory.skipped
        report.artifact_bytes_scanned = artifact_inventory.scanned_bytes
        artifact_repositories = {
            repository_from_url(url) for url in artifact_inventory.canonical_urls
        }
        state.repository_cache = {
            "expires_at": format_time(now + timedelta(seconds=config.inventory_ttl_seconds)),
            "artifact_repositories": sorted(artifact_repositories),
        }

    repositories = tuple(
        sorted(
            artifact_repositories
            | {repository_from_url(url) for url in board_urls}
        )
    )
    if not repositories:
        raise CapabilityBlocked("repository inventory is empty")
    return repositories


def parse_time(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def deterministic_operation_id(action: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join((IDENTITY, action, *parts)).encode("utf-8")).hexdigest()
    return f"{IDENTITY}:{action}:{digest[:32]}"


def exact_gate_urls(body: str) -> tuple[tuple[str, ...], bool]:
    gates: list[str] = []
    malformed = False
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("GATE:"):
            continue
        match = EXACT_GATE_RE.fullmatch(stripped)
        if not match:
            malformed = True
            continue
        try:
            gates.append(canonical_pr_url(match.group(1)))
        except ValueError:
            malformed = True
    return tuple(gates), malformed


def exclusion_reason(card: CardSnapshot) -> str | None:
    joined = f"{card.title}\n{card.body}".lower()
    assignee = card.assignee.lower()
    skills = " ".join(card.skills).lower()
    status = card.status.lower()
    if "security" in assignee or "security" in skills or "security" in joined:
        return "security-scope"
    if "operator" in joined:
        return "operator-hold"
    if card.terminal or status in TERMINAL_STATUSES:
        return "terminal-status"
    if card.protected_custody or assignee in {"fable", "terminal", "terminal-lane"} or re.search(
        r"\b(fable|terminal)[- ]custody\b", joined
    ):
        return "protected-custody"
    if card.active_run or status in ACTIVE_STATUSES:
        return "active-run"
    return None


def merge_receipt(prefix: str, merges: Sequence[MergeRecord]) -> str:
    lines = []
    for merge in sorted(merges, key=lambda item: item.canonical_url):
        lines.append(
            f"{prefix} URL={merge.canonical_url} SHA={merge.merge_sha} "
            f"MERGED_AT={merge.merged_at} author={IDENTITY}"
        )
    return "\n".join(lines)


def _known_merges(state: ReconcilerState) -> dict[str, MergeRecord]:
    known: dict[str, MergeRecord] = {}
    for url, value in state.processed_merges.items():
        try:
            merge = MergeRecord.from_dict(value)
        except (KeyError, TypeError, ValueError):
            continue
        known[url] = merge
    return known


def _cutoff(config: Config, state: ReconcilerState, now: datetime) -> datetime:
    if state.watermark:
        return parse_time(state.watermark) - timedelta(seconds=config.overlap_seconds)
    return now - timedelta(days=config.bootstrap_days)


def probe_host_admission(config: Config) -> AdmissionResult:
    load1: float | None
    sdb_free_kib: int | None
    reasons: list[str] = []
    try:
        load1 = float(os.getloadavg()[0])
    except (OSError, AttributeError, ValueError):
        load1 = None
        reasons.append("load1-unavailable")
    else:
        if load1 > config.load1_max:
            reasons.append("load1-above-max")

    try:
        stat = os.statvfs(config.sdb_mount)
        sdb_free_kib = int(stat.f_bavail * stat.f_frsize // 1024)
    except (OSError, ValueError, ZeroDivisionError):
        sdb_free_kib = None
        reasons.append("sdb-free-unavailable")
    else:
        if sdb_free_kib < config.sdb_min_free_kib:
            reasons.append("sdb-free-below-min")
    return AdmissionResult(
        allowed=not reasons,
        reason="admitted" if not reasons else ",".join(reasons),
        load1=load1,
        sdb_free_kib=sdb_free_kib,
    )


def validate_runtime_paths(config: Config) -> None:
    if not config.artifacts_dir.is_dir():
        raise ReconcilerError(
            f"required source artifacts directory is missing: {config.artifacts_dir}"
        )
    if not os.access(config.artifacts_dir, os.R_OK | os.X_OK):
        raise ReconcilerError(
            "required source artifacts directory is not readable/searchable: "
            f"{config.artifacts_dir}"
        )

    required_writable_directories = (
        config.state_dir,
        config.report_dir,
        config.alert_dir,
    )
    for directory in required_writable_directories:
        if not directory.is_dir():
            raise ReconcilerError(f"required writable directory is missing: {directory}")
        if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
            raise ReconcilerError(
                f"required writable directory is not accessible: {directory}"
            )
    if not config.status_ticker.is_file():
        raise ReconcilerError(
            f"required writable ticker file is missing: {config.status_ticker}"
        )
    if not os.access(config.status_ticker, os.R_OK | os.W_OK):
        raise ReconcilerError(f"required ticker file is not writable: {config.status_ticker}")


def execute_tick(
    config: Config,
    board: BoardAdapter | None = None,
    feed: MergeFeed | None = None,
    now: datetime | None = None,
    admission_probe: Callable[[Config], AdmissionResult] | None = None,
) -> TickReport:
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not config.enabled:
        return TickReport(status="disabled", observed_at=format_time(observed))

    admission = (admission_probe or probe_host_admission)(config)
    if not admission.allowed:
        return TickReport(
            status="admission-skip",
            observed_at=format_time(observed),
            admission=admission.report_fields(config),
        )

    try:
        validate_runtime_paths(config)
    except ReconcilerError as exc:
        return TickReport(
            status="error",
            observed_at=format_time(observed),
            admission=admission.report_fields(config),
            errors=[str(exc)],
        )
    lock_handle = config.lock_file.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return TickReport(
                status="overlap-skip",
                observed_at=format_time(observed),
                admission=admission.report_fields(config),
            )
        return _execute_locked(config, board, feed, observed, admission)
    finally:
        lock_handle.close()


def _copy_state(state: ReconcilerState, *, tick_increment: int = 0) -> ReconcilerState:
    return ReconcilerState(
        version=state.version,
        watermark=state.watermark,
        repository_cache=json.loads(json.dumps(state.repository_cache)),
        etag_cache=json.loads(json.dumps(state.etag_cache)),
        processed_merges=json.loads(json.dumps(state.processed_merges)),
        first_observed=json.loads(json.dumps(state.first_observed)),
        alert_receipts=json.loads(json.dumps(state.alert_receipts)),
        alert_outbox=json.loads(json.dumps(state.alert_outbox)),
        tick_counter=state.tick_counter + tick_increment,
    )


def _execute_locked(
    config: Config,
    board: BoardAdapter | None,
    feed: MergeFeed | None,
    now: datetime,
    admission: AdmissionResult,
) -> TickReport:
    try:
        state = ReconcilerState.load(config.state_file)
    except ReconcilerError as exc:
        report = TickReport(
            status="error",
            observed_at=format_time(now),
            admission=admission.report_fields(config),
            errors=[str(exc)],
        )
        return _publish_report_safely(config, report, now)

    report = TickReport(
        status="ok",
        observed_at=format_time(now),
        watermark_before=state.watermark,
        watermark_after=state.watermark,
        admission=admission.report_fields(config),
    )

    if state.alert_outbox:
        recovered = _copy_state(state)
        try:
            _flush_alert_outbox(config, recovered)
            recovered.save(config.state_file)
        except (OSError, ReconcilerError) as exc:
            report.status = "error"
            report.errors.append(f"pending alert outbox recovery failed: {exc}")
            return _publish_report_safely(config, report, now)
        state = recovered

    active_board = board
    if active_board is None:
        if config.board_helper is None:
            report.status = "capability-blocked"
            report.errors.append("board helper not configured; broker helper path required (post-PR36/PR38/PR41)")
            return _publish_report_safely(config, report, now)
        active_board = JsonHelperBoardAdapter(config.board_helper, config.helper_timeout_seconds)
    active_feed = feed or GitHubFeed(
        config.github_api_base,
        config.request_timeout_seconds,
        config.max_pages_per_repo,
    )

    working = _copy_state(state, tick_increment=1)
    try:
        active_board.validate_capabilities()
        repositories = derive_repositories(config, working, active_board, now, report)
        fetched = active_feed.fetch(repositories, _cutoff(config, state, now), working)
        report.merges_seen = len(fetched.merges)
        report.api_calls = fetched.api_calls
        report.api_not_modified = fetched.not_modified
    except ReconcilerError as exc:
        report.status = (
            "capability-blocked" if isinstance(exc, CapabilityBlocked) else "error"
        )
        report.errors.append(str(exc))
        return _publish_report_safely(config, report, now)
    except Exception:
        report.status = "error"
        report.errors.append("unexpected read-phase failure")
        return _publish_report_safely(config, report, now)

    for merge in fetched.merges:
        working.processed_merges[merge.canonical_url] = asdict(merge)
    known = _known_merges(working)

    try:
        # Parse every candidate before the first mutation. A malformed broker row
        # therefore cannot leave a partially-actioned tick.
        cards = active_board.list_cards_citing(tuple(sorted(known))) if known else ()
        for merge in sorted(known.values(), key=lambda item: item.canonical_url):
            operation_id = deterministic_operation_id(
                "ownership", merge.canonical_url, merge.merge_sha
            )
            result = active_board.converge_ownership(merge, operation_id)
            if not result.semantic_released:
                raise CapabilityBlocked(
                    f"semantic URL release was not proven: {merge.canonical_url}"
                )
            report.ownership_rows_cleared += result.cleared_rows
            report.guard_urls_semantically_released += 1

        open_observation_keys: set[str] = set()
        completion_operations: set[str] = set()
        for card in cards:
            completion_operation, observations = _reconcile_card(
                active_board, card, known, working, report, now
            )
            if completion_operation is not None:
                completion_operations.add(completion_operation)
            open_observation_keys.update(observations)

        for key in list(working.first_observed):
            if key not in open_observation_keys:
                del working.first_observed[key]

        for completion_operation in sorted(completion_operations):
            report.children_promoted += active_board.recompute_ready(
                deterministic_operation_id("recompute-ready", completion_operation)
            )

        _queue_new_slo_alerts(working, report, now)
    except ReconcilerError as exc:
        report.status = (
            "capability-blocked" if isinstance(exc, CapabilityBlocked) else "error"
        )
        report.errors.append(str(exc))
        return _publish_report_safely(config, report, now)
    except Exception:
        report.status = "error"
        report.errors.append("unexpected action-phase failure")
        return _publish_report_safely(config, report, now)

    report.watermark_after = format_time(now)
    working.watermark = report.watermark_after
    _prune_state(working, now)
    try:
        # The durable watermark and pending outbox always precede any ok report or
        # external alert publication. Board operations above are deterministic.
        working.save(config.state_file)
    except (OSError, ReconcilerError) as exc:
        report.status = "error"
        report.watermark_after = state.watermark
        report.errors.append(f"durable state save failed: {exc}")
        return _publish_report_safely(config, report, now)

    if working.alert_outbox:
        try:
            _flush_alert_outbox(config, working)
            # Clearing the durable outbox is a second idempotent checkpoint. If
            # this save fails, the prior state safely retries deterministic outputs.
            working.save(config.state_file)
        except (OSError, ReconcilerError) as exc:
            report.status = "error"
            report.errors.append(f"alert outbox publication failed: {exc}")
            return _publish_report_safely(config, report, now, durable_tick=True)

    return _publish_report_safely(config, report, now, durable_tick=True)


def _reconcile_card(
    board: BoardAdapter,
    card: CardSnapshot,
    known: Mapping[str, MergeRecord],
    state: ReconcilerState,
    report: TickReport,
    now: datetime,
) -> tuple[str | None, set[str]]:
    gates, malformed = exact_gate_urls(card.body)
    all_gate_merges = [known[url] for url in gates if url in known]
    tier_a = bool(gates) and not malformed and len(all_gate_merges) == len(gates)
    combined = "\n".join((card.title, card.body, *card.comments))
    cited_urls = extract_pr_urls(combined)
    merged_urls = sorted(cited_urls.intersection(known))
    relevant = [known[url] for url in merged_urls]
    excluded = exclusion_reason(card)

    if tier_a and excluded is None:
        receipt = merge_receipt("MERGE-RECEIPT", all_gate_merges)
        operation_id = deterministic_operation_id(
            "complete", card.task_id, *(merge.merge_sha for merge in all_gate_merges)
        )
        changed = board.complete_gate_card(card.task_id, receipt, operation_id)
        if changed:
            report.cards_closed.append(card.task_id)
        # Keep the deterministic completion key coupled to the canonical
        # recompute receipt, including an idempotent unchanged retry.
        return operation_id, set()

    should_evidence = bool(relevant) and (
        malformed or bool(gates) or bool(BLOCKER_MARKERS.search(combined)) or excluded is not None
    )
    observation_keys: set[str] = set()
    if should_evidence:
        for merge in relevant:
            if card.status.lower() in TERMINAL_STATUSES:
                continue
            key = f"{card.task_id}|{merge.canonical_url}|{merge.merge_sha}"
            observation_keys.add(key)
            first = state.first_observed.get(key)
            if first is None:
                first = {
                    "first_tick": state.tick_counter,
                    "observations": 0,
                    "observed_at": format_time(now),
                    "task_id": card.task_id,
                    "canonical_url": merge.canonical_url,
                    "merge_sha": merge.merge_sha,
                }
                state.first_observed[key] = first
            first["observations"] = int(first.get("observations", 0)) + 1
            age_seconds = int((now - parse_time(str(first["observed_at"]))).total_seconds())
            if age_seconds >= 240 or int(first["observations"]) >= 2:
                report.slo_breaches.append(
                    {
                        "key": key,
                        "task_id": card.task_id,
                        "canonical_url": merge.canonical_url,
                        "merge_sha": merge.merge_sha,
                        "age_seconds": str(max(age_seconds, 0)),
                        "observations": str(first["observations"]),
                    }
                )
        receipt = merge_receipt("MERGE-EVIDENCE", relevant)
        operation_id = deterministic_operation_id(
            "evidence", card.task_id, *(merge.merge_sha for merge in relevant)
        )
        if board.add_evidence_comment(card.task_id, receipt, operation_id):
            report.evidence_comments.append(
                {"task_id": card.task_id, "urls": ",".join(merged_urls)}
            )
        if not tier_a:
            report.stale_gate_items.append(
                {
                    "task_id": card.task_id,
                    "urls": ",".join(merged_urls),
                    "reason": "malformed-or-legacy-gate",
                }
            )
    if excluded is not None and relevant:
        report.exclusions.append(
            {
                "task_id": card.task_id,
                "reason": excluded,
                "urls": ",".join(merged_urls),
            }
        )
    return None, observation_keys


def _queue_new_slo_alerts(
    state: ReconcilerState, report: TickReport, now: datetime
) -> None:
    for breach in report.slo_breaches:
        receipt_key = deterministic_operation_id("slo-alert", breach["key"])
        if receipt_key in state.alert_receipts or receipt_key in state.alert_outbox:
            continue
        line = (
            f"SLO-BREACH task={breach['task_id']} PR={breach['canonical_url']} "
            f"SHA={breach['merge_sha']} age_seconds={breach['age_seconds']} "
            f"observations={breach['observations']} RECEIPT={receipt_key} author={IDENTITY}"
        )
        state.alert_outbox[receipt_key] = {
            "line": line,
            "observed_at": format_time(now),
        }


def _flush_alert_outbox(config: Config, state: ReconcilerState) -> None:
    for receipt_key in sorted(state.alert_outbox):
        payload = state.alert_outbox[receipt_key]
        line = payload.get("line", "")
        observed_at = payload.get("observed_at", "")
        if not line or f"RECEIPT={receipt_key}" not in line:
            raise ReconcilerError("durable alert outbox entry is invalid")
        try:
            parse_time(observed_at)
        except ValueError as exc:
            raise ReconcilerError("durable alert outbox timestamp is invalid") from exc
        append_line_safely(config.status_ticker, line, receipt_key)
        name_hash = hashlib.sha256(receipt_key.encode("utf-8")).hexdigest()[:16]
        alert_path = config.alert_dir / f"{IDENTITY}-slo-{name_hash}.md"
        atomic_write_text(alert_path, line + "\n", 0o600)
        state.alert_receipts[receipt_key] = observed_at
        del state.alert_outbox[receipt_key]


def _prune_state(state: ReconcilerState, now: datetime) -> None:
    cutoff = now - timedelta(days=180)
    for url, raw in list(state.processed_merges.items()):
        try:
            merged_at = parse_time(str(raw["merged_at"]))
        except (KeyError, ValueError):
            del state.processed_merges[url]
            continue
        if merged_at < cutoff:
            del state.processed_merges[url]
    referenced = {
        deterministic_operation_id("slo-alert", key) for key in state.first_observed
    }
    for key in list(state.alert_receipts):
        if key not in referenced:
            del state.alert_receipts[key]
    if len(state.etag_cache) > 2_000:
        state.etag_cache = dict(list(state.etag_cache.items())[-2_000:])


def atomic_write_text(path: Path, content: str, mode: int) -> None:
    if not path.parent.is_dir():
        raise OSError(f"destination directory is missing: {path.parent}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_line_safely(path: Path, line: str, receipt_key: str) -> None:
    if not path.parent.is_dir():
        raise OSError(f"ticker directory is missing: {path.parent}")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        marker = f"RECEIPT={receipt_key}"
        if any(marker in existing for existing in handle):
            return
        handle.seek(0, os.SEEK_END)
        handle.write(line.rstrip("\n") + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def prune_timestamped_reports(config: Config, now: datetime) -> int:
    groups: dict[str, list[Path]] = {}
    for path in config.report_dir.iterdir():
        match = TIMESTAMPED_REPORT_RE.fullmatch(path.name)
        if match and path.is_file():
            groups.setdefault(match.group(1), []).append(path)

    cutoff = now - timedelta(days=config.report_retention_days)
    delete_stamps: set[str] = set()
    for stamp in groups:
        try:
            report_time = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError as exc:
            raise ReconcilerError("timestamped report filename is invalid") from exc
        if report_time < cutoff:
            delete_stamps.add(stamp)

    remaining = sorted((stamp for stamp in groups if stamp not in delete_stamps), reverse=True)
    keep_existing = max(config.report_retention_count - 1, 0)
    delete_stamps.update(remaining[keep_existing:])
    pruned = 0
    for stamp in sorted(delete_stamps):
        for path in groups[stamp]:
            path.unlink()
            pruned += 1
    return pruned


def _publish_report_safely(
    config: Config,
    report: TickReport,
    now: datetime,
    *,
    durable_tick: bool = False,
) -> TickReport:
    try:
        if durable_tick:
            report.reports_pruned += prune_timestamped_reports(config, now)
            write_reports(config, report, now)
        else:
            payload = json.dumps(report.as_dict(), sort_keys=True, indent=2) + "\n"
            atomic_write_text(config.report_dir / "latest.json", payload, 0o600)
    except (OSError, ReconcilerError) as exc:
        report.status = "error"
        report.errors.append(f"report publication failed: {exc}")
    return report


def write_reports(config: Config, report: TickReport, now: datetime) -> None:
    payload = json.dumps(report.as_dict(), sort_keys=True, indent=2) + "\n"
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    atomic_write_text(config.report_dir / f"{timestamp}.json", payload, 0o600)
    markdown = [
        f"# {IDENTITY} tick {format_time(now)}",
        "",
        f"- status: {report.status}",
        f"- merges seen: {report.merges_seen}",
        f"- ownership rows cleared: {report.ownership_rows_cleared}",
        f"- guard URLs released: {report.guard_urls_semantically_released}",
        f"- cards closed: {len(report.cards_closed)}",
        f"- children promoted: {report.children_promoted}",
        f"- evidence comments: {len(report.evidence_comments)}",
        f"- stale gate queue: {len(report.stale_gate_items)}",
        f"- exclusions: {len(report.exclusions)}",
        f"- SLO breaches: {len(report.slo_breaches)}",
        f"- API calls / 304s: {report.api_calls} / {report.api_not_modified}",
        f"- artifact cache hit: {report.artifact_cache_hit}",
        f"- artifact candidate/scanned/skipped files: {report.artifact_candidates} / "
        f"{report.artifact_files_scanned} / {report.artifact_files_skipped}",
        f"- artifact bytes scanned: {report.artifact_bytes_scanned}",
        f"- reports pruned: {report.reports_pruned}",
        f"- watermark: {report.watermark_before} -> {report.watermark_after}",
        f"- admission: {report.admission}",
        f"- errors: {len(report.errors)}",
    ]
    atomic_write_text(
        config.report_dir / f"{timestamp}.md", "\n".join(markdown) + "\n", 0o600
    )
    # latest.json is the commit marker for report publication and is written only
    # after every timestamped artifact succeeds.
    atomic_write_text(config.report_dir / "latest.json", payload, 0o600)


def summary_line(report: TickReport) -> str:
    admission_reason = report.admission.get("reason", "not-sampled")
    return (
        f"{IDENTITY} status={report.status} merges={report.merges_seen} "
        f"cleared={report.ownership_rows_cleared} released={report.guard_urls_semantically_released} "
        f"closed={len(report.cards_closed)} promoted={report.children_promoted} "
        f"evidence={len(report.evidence_comments)} stale={len(report.stale_gate_items)} "
        f"breaches={len(report.slo_breaches)} api={report.api_calls} "
        f"not_modified={report.api_not_modified} admission={admission_reason} "
        f"errors={len(report.errors)}"
    )


def main() -> int:
    try:
        config = Config.from_env()
        report = execute_tick(config)
    except ReconcilerError as exc:
        report = TickReport(
            status="error",
            observed_at=format_time(datetime.now(timezone.utc)),
            errors=[str(exc)],
        )
    except Exception:
        report = TickReport(
            status="error",
            observed_at=format_time(datetime.now(timezone.utc)),
            errors=["unexpected top-level failure"],
        )
    print(summary_line(report), flush=True)
    return 0 if report.status in {"ok", "disabled", "overlap-skip", "admission-skip"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
