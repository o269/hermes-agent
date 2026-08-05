"""Fail-closed GitHub PR destination guard for Hermes-Agent workspaces.

The incident this guards against is subtle: in a fork clone of Hermes Agent,
``gh pr create`` can silently use a repository default that points at the public
upstream (``NousResearch/Hermes-Agent``) instead of the fleet fork
(``o269/hermes-agent``).  The terminal dangerous-command layer only sees a shell
command, so this module keeps the policy small, deterministic, and free of raw
command/secret data in receipts.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Mapping, Optional, Sequence

SAFE_HERMES_AGENT_REPO = "o269/hermes-agent"
PUBLIC_HERMES_AGENT_UPSTREAM = "nousresearch/hermes-agent"
UPSTREAM_ALLOW_ENV = "HERMES_ALLOW_PUBLIC_HERMES_AGENT_UPSTREAM_PR"

_TRUE_VALUES = {"1", "true", "yes", "on", "allow", "allowed", "approved"}
_COMMAND_SEPARATORS = {"&&", "||", ";", "|"}
_GITHUB_REPO_RE = re.compile(
    r"(?:github\.com[:/])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_UPSTREAM_AUTH_RE = re.compile(
    r"(?:(?:explicit(?:ly)?\s+)?(?:authori[sz](?:e[sd]?|ation)|approved?|allow(?:ed)?))"
    r".{0,80}\b(?:upstream|public\s+upstream|NousResearch/Hermes-Agent)\b|"
    r"\b(?:upstream|public\s+upstream|NousResearch/Hermes-Agent)\b.{0,80}"
    r"(?:authori[sz](?:e[sd]?|ation)|approved?|allow(?:ed)?)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class GuardDecision:
    """Sanitized decision suitable for terminal errors and durable receipts."""

    allowed: bool
    reason_code: str
    message: str
    action: str = "none"
    workspace_repo: Optional[str] = None
    target_repo: Optional[str] = None
    base: Optional[str] = None

    def receipt(self) -> dict[str, object]:
        data = asdict(self)
        data["guard"] = "hermes-agent-pr-destination"
        # Intentionally do not include the raw command or environment: either can
        # contain branch names, body text, or credentials injected by a shell.
        return data


@dataclass(frozen=True)
class WorkspaceInitReceipt:
    allowed: bool
    action: str
    reason_code: str
    workspace_repo: Optional[str]
    gh_default_repo: Optional[str]
    command: tuple[str, ...] = ()
    message: str = ""

    def receipt(self) -> dict[str, object]:
        data = asdict(self)
        data["guard"] = "hermes-agent-pr-destination:init"
        data["command"] = list(self.command)
        return data


@dataclass(frozen=True)
class PreflightReceipt:
    allowed: bool
    reason_code: str
    workspace_repo: Optional[str]
    target_repo: Optional[str]
    base: Optional[str]
    local_origin_main_sha: Optional[str] = None
    remote_fork_main_sha: Optional[str] = None
    merge_base_sha: Optional[str] = None
    head_sha: Optional[str] = None
    changed_paths: tuple[str, ...] = ()
    message: str = ""

    def receipt(self) -> dict[str, object]:
        data = asdict(self)
        data["guard"] = "hermes-agent-pr-destination:preflight"
        data["changed_paths"] = list(self.changed_paths)
        return data


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def normalize_repo(value: object) -> Optional[str]:
    """Return lower-case ``owner/repo`` for GitHub shorthands/URLs."""

    if value is None:
        return None
    text = str(value).strip().strip("'\"")
    if not text:
        return None
    match = _GITHUB_REPO_RE.search(text)
    if match:
        owner = match.group("owner")
        repo = match.group("repo")
    else:
        # Accept owner/repo and optional trailing path fragments such as
        # owner/repo/pull/123.  Strip .git from the repository segment.
        if text.startswith("git@github.com:"):
            text = text.split(":", 1)[1]
        parts = [p for p in text.split("/") if p]
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1]
    repo = repo.removesuffix(".git")
    if not owner or not repo:
        return None
    return f"{owner.lower()}/{repo.lower()}"


def is_hermes_agent_repo(repo: object) -> bool:
    normalized = normalize_repo(repo)
    return bool(normalized and normalized.endswith("/hermes-agent"))


def is_safe_fleet_repo(repo: object) -> bool:
    return normalize_repo(repo) == SAFE_HERMES_AGENT_REPO


def is_public_upstream_repo(repo: object) -> bool:
    return normalize_repo(repo) == PUBLIC_HERMES_AGENT_UPSTREAM


def upstream_authorized(
    *,
    env: Optional[Mapping[str, str]] = None,
    task_title: str = "",
    task_body: str = "",
    explicit_allow: bool = False,
) -> bool:
    if explicit_allow:
        return True
    env_map = os.environ if env is None else env
    if _truthy(env_map.get(UPSTREAM_ALLOW_ENV)):
        return True
    return bool(_UPSTREAM_AUTH_RE.search(f"{task_title or ''}\n{task_body or ''}"))


def _shell_words(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _effective_cwd_from_leading_cd(command: str, cwd: Optional[Path]) -> Optional[Path]:
    """Honor the common ``cd <dir> && gh ...`` wrapper when it is first."""

    words = _shell_words(command)
    if len(words) >= 4 and words[0] == "cd" and words[2] in {"&&", ";"}:
        target = Path(words[1]).expanduser()
        if not target.is_absolute() and cwd is not None:
            target = cwd / target
        return target.resolve(strict=False)
    return cwd


def _workspace_repo_from_git(cwd: Optional[Path]) -> Optional[str]:
    if cwd is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "--all", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        repo = normalize_repo(line)
        if repo:
            return repo
    return None


def _option_value(args: Sequence[str], *, long_name: str, short_name: str) -> Optional[str]:
    for i, token in enumerate(args):
        if token == long_name or token == short_name:
            return args[i + 1] if i + 1 < len(args) else None
        if token.startswith(long_name + "="):
            return token.split("=", 1)[1]
        if token.startswith(short_name + "="):
            return token.split("=", 1)[1]
    return None


def _iter_gh_invocations(words: Sequence[str]) -> list[list[str]]:
    invocations: list[list[str]] = []
    i = 0
    while i < len(words):
        if words[i] != "gh":
            i += 1
            continue
        j = i + 1
        while j < len(words) and words[j] not in _COMMAND_SEPARATORS:
            j += 1
        invocations.append(list(words[i:j]))
        i = j + 1
    return invocations


def _repo_set_default_target(invocation: Sequence[str]) -> Optional[str]:
    args = list(invocation[3:])
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("-"):
            # Current gh repo set-default options are flags. If a future option
            # takes a value, skipping only the flag still keeps us conservative:
            # a missing target fails closed for Hermes-Agent workspaces.
            i += 1
            continue
        return token
    return None


def check_hermes_agent_pr_command(
    command: str,
    *,
    cwd: Optional[str | Path] = None,
    env: Optional[Mapping[str, str]] = None,
    repo_hint: Optional[str] = None,
    task_title: str = "",
    task_body: str = "",
    explicit_allow_upstream: bool = False,
) -> GuardDecision:
    """Validate GitHub PR-destination commands in Hermes-Agent workspaces.

    Non-Hermes repositories are deliberately no-ops.  Hermes-Agent workspaces
    fail closed for PR creation that relies on gh defaults, targets the public
    upstream without explicit authorization, or omits ``--base main``.
    """

    base_cwd = Path(cwd).expanduser().resolve(strict=False) if cwd else None
    effective_cwd = _effective_cwd_from_leading_cd(command, base_cwd)
    workspace_repo = normalize_repo(repo_hint) or _workspace_repo_from_git(effective_cwd)
    words = _shell_words(command)
    invocations = _iter_gh_invocations(words)
    if not invocations:
        return GuardDecision(True, "not-gh", "no GitHub CLI PR destination command found")
    if not is_hermes_agent_repo(workspace_repo):
        return GuardDecision(
            True,
            "non-hermes-repo",
            "not a Hermes-Agent workspace; PR destination guard not applied",
            workspace_repo=workspace_repo,
        )

    allow_upstream = upstream_authorized(
        env=env,
        task_title=task_title,
        task_body=task_body,
        explicit_allow=explicit_allow_upstream,
    )

    for invocation in invocations:
        if len(invocation) >= 3 and invocation[1:3] == ["repo", "set-default"]:
            target_repo = normalize_repo(_repo_set_default_target(invocation))
            if target_repo is None:
                return GuardDecision(
                    False,
                    "missing-set-default-target",
                    "Hermes-Agent workspaces must set an explicit gh repo default; use `gh repo set-default o269/hermes-agent`.",
                    action="gh repo set-default",
                    workspace_repo=workspace_repo,
                )
            if target_repo == SAFE_HERMES_AGENT_REPO:
                continue
            if target_repo == PUBLIC_HERMES_AGENT_UPSTREAM and allow_upstream:
                continue
            return GuardDecision(
                False,
                "unsafe-set-default-target",
                "Hermes-Agent workspaces may not set gh's default repository to a wrong or public-upstream target without explicit upstream authorization.",
                action="gh repo set-default",
                workspace_repo=workspace_repo,
                target_repo=target_repo,
            )

        if len(invocation) >= 3 and invocation[1:3] == ["pr", "create"]:
            repo = normalize_repo(_option_value(invocation[3:], long_name="--repo", short_name="-R"))
            base = _option_value(invocation[3:], long_name="--base", short_name="-B")
            if repo is None:
                return GuardDecision(
                    False,
                    "missing-explicit-repo",
                    "Hermes-Agent PR creation must pass `--repo o269/hermes-agent`; do not rely on gh repository defaults.",
                    action="gh pr create",
                    workspace_repo=workspace_repo,
                )
            if base != "main":
                return GuardDecision(
                    False,
                    "missing-or-wrong-base",
                    "Hermes-Agent PR creation must pass `--base main` so a divergent fork/default base cannot be selected.",
                    action="gh pr create",
                    workspace_repo=workspace_repo,
                    target_repo=repo,
                    base=base,
                )
            if repo == SAFE_HERMES_AGENT_REPO:
                continue
            if repo == PUBLIC_HERMES_AGENT_UPSTREAM and allow_upstream:
                continue
            return GuardDecision(
                False,
                "unsafe-pr-target",
                "Hermes-Agent PR creation must target o269/hermes-agent; public NousResearch/Hermes-Agent requires explicit upstream authorization on the card.",
                action="gh pr create",
                workspace_repo=workspace_repo,
                target_repo=repo,
                base=base,
            )

    return GuardDecision(
        True,
        "ok",
        "GitHub PR destination guard accepted command",
        workspace_repo=workspace_repo,
    )


def workspace_init_receipt(
    *,
    origin_repo: object,
    gh_default_repo: object = None,
    gh_auth_ok: bool,
    task_title: str = "",
    task_body: str = "",
    explicit_allow_upstream: bool = False,
) -> WorkspaceInitReceipt:
    """Return a fail-closed, sanitized receipt for Hermes-Agent workspace init."""

    workspace_repo = normalize_repo(origin_repo)
    default_repo = normalize_repo(gh_default_repo)
    if not is_hermes_agent_repo(workspace_repo):
        return WorkspaceInitReceipt(
            True,
            "noop-non-hermes",
            "non-hermes-repo",
            workspace_repo,
            default_repo,
            message="not a Hermes-Agent workspace",
        )
    if not gh_auth_ok:
        return WorkspaceInitReceipt(
            False,
            "blocked",
            "missing-gh-auth",
            workspace_repo,
            default_repo,
            message="gh authentication is required before Hermes-Agent PR authoring",
        )
    allow_upstream = upstream_authorized(
        task_title=task_title,
        task_body=task_body,
        explicit_allow=explicit_allow_upstream,
    )
    if default_repo == SAFE_HERMES_AGENT_REPO:
        return WorkspaceInitReceipt(
            True,
            "already-safe",
            "ok",
            workspace_repo,
            default_repo,
            message="gh default already points at the fleet fork",
        )
    if default_repo == PUBLIC_HERMES_AGENT_UPSTREAM and allow_upstream:
        return WorkspaceInitReceipt(
            True,
            "authorized-upstream",
            "ok-upstream-authorized",
            workspace_repo,
            default_repo,
            message="public upstream default accepted only because upstream contribution is explicit",
        )
    return WorkspaceInitReceipt(
        True,
        "set-default",
        "needs-safe-default",
        workspace_repo,
        default_repo,
        command=("gh", "repo", "set-default", SAFE_HERMES_AGENT_REPO),
        message="set gh's repository default to the fleet fork before PR authoring",
    )


def preflight_receipt(
    *,
    origin_repo: object,
    target_repo: object,
    base: object,
    gh_auth_ok: bool,
    local_origin_main_sha: Optional[str],
    remote_fork_main_sha: Optional[str],
    merge_base_sha: Optional[str],
    head_sha: Optional[str],
    changed_paths: Sequence[str] = (),
    allowed_path_prefixes: Sequence[str] = (),
    task_title: str = "",
    task_body: str = "",
    explicit_allow_upstream: bool = False,
) -> PreflightReceipt:
    """Validate a sanitized PR preflight tuple before push/PR handoff."""

    workspace_repo = normalize_repo(origin_repo)
    normalized_target = normalize_repo(target_repo)
    base_text = str(base or "") or None
    changed = tuple(str(path) for path in changed_paths)
    if not is_hermes_agent_repo(workspace_repo):
        return PreflightReceipt(
            True,
            "non-hermes-repo",
            workspace_repo,
            normalized_target,
            base_text,
            changed_paths=changed,
            message="not a Hermes-Agent workspace",
        )
    if not gh_auth_ok:
        return PreflightReceipt(
            False,
            "missing-gh-auth",
            workspace_repo,
            normalized_target,
            base_text,
            changed_paths=changed,
            message="gh authentication is required before push/PR",
        )
    allow_upstream = upstream_authorized(
        task_title=task_title,
        task_body=task_body,
        explicit_allow=explicit_allow_upstream,
    )
    if normalized_target == PUBLIC_HERMES_AGENT_UPSTREAM and not allow_upstream:
        return PreflightReceipt(
            False,
            "public-upstream-not-authorized",
            workspace_repo,
            normalized_target,
            base_text,
            changed_paths=changed,
            message="public upstream PR target is not authorized by the card",
        )
    if normalized_target not in {SAFE_HERMES_AGENT_REPO, PUBLIC_HERMES_AGENT_UPSTREAM}:
        return PreflightReceipt(
            False,
            "wrong-target-repo",
            workspace_repo,
            normalized_target,
            base_text,
            changed_paths=changed,
            message="Hermes-Agent PR target must be o269/hermes-agent unless public upstream is explicitly authorized",
        )
    if base_text != "main":
        return PreflightReceipt(
            False,
            "wrong-base",
            workspace_repo,
            normalized_target,
            base_text,
            changed_paths=changed,
            message="Hermes-Agent PR base must be main",
        )
    if not local_origin_main_sha or not remote_fork_main_sha or local_origin_main_sha != remote_fork_main_sha:
        return PreflightReceipt(
            False,
            "stale-or-divergent-fork-main",
            workspace_repo,
            normalized_target,
            base_text,
            local_origin_main_sha,
            remote_fork_main_sha,
            merge_base_sha,
            head_sha,
            changed,
            message="local origin/main must equal the remote fork main before PR creation",
        )
    if not head_sha or not merge_base_sha or merge_base_sha != remote_fork_main_sha:
        return PreflightReceipt(
            False,
            "head-not-based-on-main",
            workspace_repo,
            normalized_target,
            base_text,
            local_origin_main_sha,
            remote_fork_main_sha,
            merge_base_sha,
            head_sha,
            changed,
            message="head must have the current fork main as its merge base",
        )
    prefixes = tuple(p for p in allowed_path_prefixes if p)
    if prefixes:
        out_of_scope = [p for p in changed if not p.startswith(prefixes)]
        if out_of_scope:
            return PreflightReceipt(
                False,
                "changed-path-out-of-scope",
                workspace_repo,
                normalized_target,
                base_text,
                local_origin_main_sha,
                remote_fork_main_sha,
                merge_base_sha,
                head_sha,
                changed,
                message="changed paths exceed the card's allowed scope",
            )
    return PreflightReceipt(
        True,
        "ok",
        workspace_repo,
        normalized_target,
        base_text,
        local_origin_main_sha,
        remote_fork_main_sha,
        merge_base_sha,
        head_sha,
        changed,
        message="Hermes-Agent PR preflight accepted",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes-Agent PR destination guard")
    sub = parser.add_subparsers(dest="cmd", required=True)

    check = sub.add_parser("check-command")
    check.add_argument("--cwd", type=Path, default=Path.cwd())
    check.add_argument("--repo-hint")
    check.add_argument("--task-title", default="")
    check.add_argument("--task-body", default="")
    check.add_argument("--allow-upstream", action="store_true")
    check.add_argument("command", nargs=argparse.REMAINDER)

    init = sub.add_parser("init-receipt")
    init.add_argument("--origin-repo", required=True)
    init.add_argument("--gh-default-repo")
    init.add_argument("--gh-auth-ok", action="store_true")
    init.add_argument("--task-title", default="")
    init.add_argument("--task-body", default="")
    init.add_argument("--allow-upstream", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "check-command":
        raw = list(args.command)
        if raw and raw[0] == "--":
            raw = raw[1:]
        command = " ".join(shlex.quote(part) for part in raw)
        decision = check_hermes_agent_pr_command(
            command,
            cwd=args.cwd,
            repo_hint=args.repo_hint,
            task_title=args.task_title,
            task_body=args.task_body,
            explicit_allow_upstream=args.allow_upstream,
        )
        print(json.dumps(decision.receipt(), sort_keys=True))
        return 0 if decision.allowed else 3
    if args.cmd == "init-receipt":
        receipt = workspace_init_receipt(
            origin_repo=args.origin_repo,
            gh_default_repo=args.gh_default_repo,
            gh_auth_ok=args.gh_auth_ok,
            task_title=args.task_title,
            task_body=args.task_body,
            explicit_allow_upstream=args.allow_upstream,
        )
        print(json.dumps(receipt.receipt(), sort_keys=True))
        return 0 if receipt.allowed else 3
    raise AssertionError(args.cmd)


if __name__ == "__main__":  # pragma: no cover - exercised through CLI tests
    raise SystemExit(main())
