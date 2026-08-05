from __future__ import annotations

import json

from tools.github_pr_destination_guard import (
    SAFE_HERMES_AGENT_REPO,
    check_hermes_agent_pr_command,
    preflight_receipt,
    workspace_init_receipt,
)


HERMES_FORK = "https://github.com/o269/hermes-agent.git"
HERMES_UPSTREAM = "https://github.com/NousResearch/Hermes-Agent.git"
NON_HERMES = "https://github.com/o269/example.git"


def test_fork_workspace_with_upstream_gh_default_is_repaired_not_trusted():
    receipt = workspace_init_receipt(
        origin_repo=HERMES_FORK,
        gh_default_repo=HERMES_UPSTREAM,
        gh_auth_ok=True,
    )

    assert receipt.allowed
    assert receipt.action == "set-default"
    assert receipt.command == ("gh", "repo", "set-default", SAFE_HERMES_AGENT_REPO)
    assert receipt.gh_default_repo == "nousresearch/hermes-agent"


def test_hermes_pr_create_missing_explicit_repo_fails_closed():
    decision = check_hermes_agent_pr_command(
        "gh pr create --base main --head fix/thing",
        repo_hint=HERMES_FORK,
    )

    assert not decision.allowed
    assert decision.reason_code == "missing-explicit-repo"
    assert "--repo o269/hermes-agent" in decision.message


def test_hermes_pr_create_requires_base_main_to_avoid_divergent_fork_base():
    decision = check_hermes_agent_pr_command(
        "gh pr create --repo o269/hermes-agent --base release --head fix/thing",
        repo_hint=HERMES_FORK,
    )

    assert not decision.allowed
    assert decision.reason_code == "missing-or-wrong-base"
    assert decision.target_repo == "o269/hermes-agent"
    assert decision.base == "release"


def test_preflight_rejects_stale_or_divergent_fork_main():
    receipt = preflight_receipt(
        origin_repo=HERMES_FORK,
        target_repo="o269/hermes-agent",
        base="main",
        gh_auth_ok=True,
        local_origin_main_sha="a" * 40,
        remote_fork_main_sha="b" * 40,
        merge_base_sha="a" * 40,
        head_sha="c" * 40,
    )

    assert not receipt.allowed
    assert receipt.reason_code == "stale-or-divergent-fork-main"


def test_preflight_rejects_head_not_based_on_current_main():
    base = "a" * 40
    receipt = preflight_receipt(
        origin_repo=HERMES_FORK,
        target_repo="o269/hermes-agent",
        base="main",
        gh_auth_ok=True,
        local_origin_main_sha=base,
        remote_fork_main_sha=base,
        merge_base_sha="b" * 40,
        head_sha="c" * 40,
    )

    assert not receipt.allowed
    assert receipt.reason_code == "head-not-based-on-main"


def test_explicitly_approved_public_upstream_pr_is_allowed():
    decision = check_hermes_agent_pr_command(
        "gh pr create --repo NousResearch/Hermes-Agent --base main --head fix/upstream",
        repo_hint=HERMES_FORK,
        explicit_allow_upstream=True,
    )

    assert decision.allowed

    base = "a" * 40
    receipt = preflight_receipt(
        origin_repo=HERMES_FORK,
        target_repo="NousResearch/Hermes-Agent",
        base="main",
        gh_auth_ok=True,
        local_origin_main_sha=base,
        remote_fork_main_sha=base,
        merge_base_sha=base,
        head_sha="c" * 40,
        explicit_allow_upstream=True,
    )
    assert receipt.allowed


def test_public_upstream_pr_without_card_authorization_is_rejected():
    decision = check_hermes_agent_pr_command(
        "gh pr create --repo NousResearch/Hermes-Agent --base main --head fix/leak",
        repo_hint=HERMES_FORK,
    )

    assert not decision.allowed
    assert decision.reason_code == "unsafe-pr-target"


def test_non_hermes_repository_is_not_changed_or_blocked():
    decision = check_hermes_agent_pr_command(
        "gh pr create",
        repo_hint=NON_HERMES,
    )
    assert decision.allowed
    assert decision.reason_code == "non-hermes-repo"

    receipt = workspace_init_receipt(
        origin_repo=NON_HERMES,
        gh_default_repo="NousResearch/Hermes-Agent",
        gh_auth_ok=False,
    )
    assert receipt.allowed
    assert receipt.action == "noop-non-hermes"


def test_missing_gh_auth_fails_closed_for_hermes_workspace():
    init = workspace_init_receipt(
        origin_repo=HERMES_FORK,
        gh_default_repo="o269/hermes-agent",
        gh_auth_ok=False,
    )
    assert not init.allowed
    assert init.reason_code == "missing-gh-auth"

    preflight = preflight_receipt(
        origin_repo=HERMES_FORK,
        target_repo="o269/hermes-agent",
        base="main",
        gh_auth_ok=False,
        local_origin_main_sha="a" * 40,
        remote_fork_main_sha="a" * 40,
        merge_base_sha="a" * 40,
        head_sha="b" * 40,
    )
    assert not preflight.allowed
    assert preflight.reason_code == "missing-gh-auth"


def test_init_receipt_is_idempotent_when_default_is_already_safe():
    receipt = workspace_init_receipt(
        origin_repo=HERMES_FORK,
        gh_default_repo="o269/hermes-agent",
        gh_auth_ok=True,
    )

    assert receipt.allowed
    assert receipt.action == "already-safe"
    assert receipt.command == ()


def test_safe_receipt_does_not_preserve_command_body_or_secret_values():
    secret = "SECRET_SHOULD_NOT_APPEAR"
    decision = check_hermes_agent_pr_command(
        f"gh pr create --repo o269/hermes-agent --base main --head fix/safe --body token={secret}",
        repo_hint=HERMES_FORK,
    )

    assert decision.allowed
    receipt = json.dumps(decision.receipt(), sort_keys=True)
    assert secret not in receipt
    assert "--body" not in receipt
    assert "token=" not in receipt
