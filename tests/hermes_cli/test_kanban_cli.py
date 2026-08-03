"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------


def test_continuation_operator_allowlist_reads_root_config(kanban_home):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  continuation_operator_profiles: default,fable\n",
        encoding="utf-8",
    )
    with kb.connect() as conn:
        assert kb._continuation_operator_profiles(conn) == ("default", "fable")


@pytest.mark.parametrize(
    "malformed",
    ["- not-a-mapping\n", "kanban: not-a-mapping\n"],
)
def test_continuation_operator_allowlist_malformed_root_config_fails_closed(
    kanban_home, malformed
):
    (kanban_home / "config.yaml").write_text(malformed, encoding="utf-8")
    with kb.connect() as conn:
        assert kb._continuation_operator_profiles(conn) == ()


def test_continuation_operator_allowlist_cannot_be_redirected_by_env(
    kanban_home, monkeypatch
):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  continuation_operator_profiles: default,fable\n",
        encoding="utf-8",
    )
    with kb.connect() as conn:
        attacker_root = kanban_home.parent / "attacker-root"
        attacker_root.mkdir()
        (attacker_root / "config.yaml").write_text(
            "kanban:\n  continuation_operator_profiles: engineer\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(attacker_root))

        assert kb._continuation_operator_profiles(conn) == ("default", "fable")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("scratch",              ("scratch", None)),
        ("worktree",              ("worktree", None)),
        ("worktree:/tmp/wt",       ("worktree", "/tmp/wt")),
        ("dir:/tmp/work",         ("dir", "/tmp/work")),
    ],
)
def test_parse_workspace_flag_valid(value, expected):
    assert kc._parse_workspace_flag(value) == expected


def test_parse_workspace_flag_expands_user():
    kind, path = kc._parse_workspace_flag("dir:~/vault")
    assert kind == "dir"
    assert path.endswith("/vault")
    assert not path.startswith("~")

    kind, path = kc._parse_workspace_flag("worktree:~/trees/t6-wire")
    assert kind == "worktree"
    assert path.endswith("/trees/t6-wire")
    assert not path.startswith("~")

@pytest.mark.parametrize("bad", ["cloud", "dir:", "worktree:", ""])
def test_parse_workspace_flag_rejects(bad):
    if not bad:
        # Empty -> defaults; not an error.
        assert kc._parse_workspace_flag(bad) == ("scratch", None)
        return
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_workspace_flag(bad)


def test_parse_branch_flag_rejects_empty_and_option_like():
    assert kc._parse_branch_flag(None) is None
    assert kc._parse_branch_flag(" wt/t6-wire ") == "wt/t6-wire"
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("   ")
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("-bad")
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("bad branch")


# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_no_args_shows_usage(kanban_home):
    out = kc.run_slash("")
    assert "kanban" in out.lower()
    assert "create" in out.lower() or "subcommand" in out.lower() or "action" in out.lower()


def test_run_slash_create_and_list(kanban_home):
    out = kc.run_slash("create 'ship feature' --assignee alice")
    assert "Created" in out
    out = kc.run_slash("list")
    assert "ship feature" in out
    assert "alice" in out


def test_run_slash_create_and_show_reasoning_effort(kanban_home):
    out = kc.run_slash(
        "create 'deep task' --assignee alice --reasoning-effort high"
    )
    task_id = out.split()[1]

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.reasoning_effort == "high"
    assert "reasoning: high" in kc.run_slash(f"show {task_id}")


def test_run_slash_create_worktree_path_and_branch(kanban_home, tmp_path):
    target = tmp_path / ".worktrees" / "t6-wire"
    target_arg = target.as_posix()
    out = kc.run_slash(
        f"create 'ship worktree' --workspace worktree:{target_arg} --branch wt/t6-wire"
    )
    assert "Created" in out

    with kb.connect() as conn:
        tasks = kb.list_tasks(conn)
    task = tasks[0]
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == target_arg
    assert task.branch_name == "wt/t6-wire"


def test_run_slash_rejects_branch_without_worktree(kanban_home):
    out = kc.run_slash("create 'bad branch' --workspace scratch --branch wt/bad")
    assert "--branch is only valid with --workspace worktree" in out


def test_run_slash_create_with_parent_and_cascade(kanban_home):
    # Parent then child via --parent
    out1 = kc.run_slash("create 'parent' --assignee alice")
    # Extract the "t_xxxx" id from "Created t_xxxx (ready, ...)"
    import re
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    p = m.group(1)
    out2 = kc.run_slash(f"create 'child' --assignee bob --parent {p}")
    assert "todo" in out2  # child starts as todo

    # Complete parent; list should promote child to ready
    kc.run_slash(f"complete {p}")
    # Explicit filter: child should now be ready (was todo before complete).
    ready_list = kc.run_slash("list --status ready")
    assert "child" in ready_list


def test_run_slash_show_includes_comments(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    show = kc.run_slash(f"show {tid}")
    assert "performance section" in show


def test_run_slash_comment_max_len_trims_long_body(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} '{'x' * 30}' --max-len 20")
    show = kc.run_slash(f"show {tid}")
    assert "trimmed to 20 chars by --max-len" in show
    assert "x" * 30 not in show


def test_run_slash_block_unblock_cycle(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    # Claim first so block() finds it running
    kc.run_slash(f"claim {tid}")
    assert "Blocked" in kc.run_slash(f"block {tid} 'need decision'")
    assert "Unblocked" in kc.run_slash(f"unblock {tid}")


def test_run_slash_json_output(kanban_home):
    out = kc.run_slash("create 'jsontask' --assignee alice --json")
    payload = json.loads(out)
    assert payload["title"] == "jsontask"
    assert payload["assignee"] == "alice"
    assert payload["status"] == "ready"


def test_run_slash_continuation_repeated_prs_and_consumed_readback(
    kanban_home, monkeypatch
):
    sha_a = "a" * 40
    sha_b = "b" * 40
    pr_a = f"o269/omnia#568@{sha_a}"
    pr_b = f"o269/omnia-v2#198@{sha_b}"

    monkeypatch.setattr(
        kb,
        "_default_github_pr_verifier",
        lambda pr: kb.GitHubPRState(
            canonical_url=pr.canonical_url,
            state="OPEN",
            is_draft=True,
            head_sha=pr.head_sha,
        ),
    )
    monkeypatch.setattr(
        kb,
        "_default_profile_provider_resolver",
        lambda _profile: "openai-codex",
    )
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        kb,
        "_continuation_operator_context",
        lambda _conn: ("fable", ("default", "fable"), None),
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="repair", assignee="engineer")
        for raw in (pr_a, pr_b):
            pr = kb.parse_continuation_pr_tuple(raw)
            kb.add_comment(conn, task_id, "worker", f"Opened {pr.canonical_url}")

    reviewed = json.loads(
        kc.run_slash(
            f"continuation review {task_id} --verdict fix-required "
            "--reason 'repair both exact heads' --json"
        )
    )
    assert reviewed["verdict"] == "fix_required"

    authorized = json.loads(
        kc.run_slash(
            f"continuation authorize {task_id} "
            f"--pr {pr_a} --pr {pr_b} "
            "--reason 'repair exact review findings' "
            "--profile engineer --provider openai-codex --json"
        )
    )
    assert authorized["status"] == "active"
    assert [pr["head_sha"] for pr in authorized["prs"]] == [sha_a, sha_b]
    assert authorized["authorized_by"] == "fable"

    assert "Claimed" in kc.run_slash(f"claim {task_id}")
    history = json.loads(kc.run_slash(f"continuation show {task_id} --json"))
    assert history[0]["status"] == "consumed"
    assert history[0]["consumed_run_id"] is not None
    assert history[0]["expires_at"] > history[0]["created_at"]


def test_run_slash_operator_claim_override_is_supported(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        kb,
        "_continuation_operator_context",
        lambda _conn: ("fable", ("fable",), None),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="follow up", assignee="engineer")
        kb.add_comment(
            conn,
            task_id,
            "engineer",
            "PR merged: https://github.com/o269/omnia/pull/568",
        )

    denied = kc.run_slash(f"claim {task_id}")
    assert "guard=active_pr" in denied
    claimed = kc.run_slash(
        f"claim {task_id} --operator-override-reason 'verify merged follow-up'"
    )
    assert "Claimed" in claimed
    with kb.connect() as conn:
        events = kb.list_events(conn, task_id)
    assert any(event.kind == "respawn_guard_bypassed" for event in events)


def test_run_slash_dispatch_dry_run_counts(kanban_home):
    kc.run_slash("create 'a' --assignee alice")
    kc.run_slash("create 'b' --assignee bob")
    out = kc.run_slash("dispatch --dry-run")
    assert "Spawned:" in out


def test_run_slash_dispatch_logs_active_pr_identity_and_expiry(
    kanban_home, monkeypatch
):
    result = kb.DispatchResult()
    result.add_respawn_guard(
        "t_owned",
        "active_pr",
        detail={
            "pr_url": "https://github.com/o269/hermes-agent/pull/8",
            "expires_at": 1785660000,
            "pr_details": [
                {
                    "pr_url": "https://github.com/o269/hermes-agent/pull/8",
                    "expires_at": 1785660000,
                },
                {
                    "pr_url": "https://github.com/o269/hermes-agent/pull/9",
                    "expires_at": 1785660300,
                },
            ],
        },
        phase="ready",
    )
    result.add_respawn_guard(
        "t_recent",
        "recent_success",
        detail={"expires_at": 1785660600, "window_seconds": 600},
        phase="ready",
    )
    monkeypatch.setattr(kb, "dispatch_once", lambda _conn, **_kwargs: result)

    out = kc.run_slash("dispatch --dry-run")

    assert (
        "SKIP t_owned respawn_guarded=active_pr "
        "pr=https://github.com/o269/hermes-agent/pull/8 "
        "expires=1785660000 "
        "pr=https://github.com/o269/hermes-agent/pull/9 "
        "expires=1785660300 phase=ready"
    ) in out
    assert (
        "SKIP t_recent respawn_guarded=recent_success "
        "expires=1785660600 phase=ready"
    ) in out


def test_run_slash_dispatch_json_includes_respawn_guard_diagnostics(
    kanban_home, monkeypatch
):
    result = kb.DispatchResult()
    result.add_respawn_guard(
        "t_owned",
        "active_pr",
        detail={
            "pr_url": "https://github.com/o269/hermes-agent/pull/8",
            "expires_at": 1785660000,
            "pr_details": [
                {
                    "pr_url": "https://github.com/o269/hermes-agent/pull/8",
                    "expires_at": 1785660000,
                },
                {
                    "pr_url": "https://github.com/o269/hermes-agent/pull/9",
                    "expires_at": 1785660300,
                },
            ],
        },
        phase="ready",
    )
    result.add_respawn_guard(
        "t_rate",
        "rate_limit_cooldown",
        detail={"expires_at": 1785660600, "window_seconds": 600},
        phase="ready",
    )
    monkeypatch.setattr(kb, "dispatch_once", lambda _conn, **_kwargs: result)

    payload = json.loads(kc.run_slash("dispatch --dry-run --json"))

    assert payload["respawn_guarded"] == [
        {
            "pr_url": "https://github.com/o269/hermes-agent/pull/8",
            "expires_at": 1785660000,
            "pr_details": [
                {
                    "pr_url": "https://github.com/o269/hermes-agent/pull/8",
                    "expires_at": 1785660000,
                },
                {
                    "pr_url": "https://github.com/o269/hermes-agent/pull/9",
                    "expires_at": 1785660300,
                },
            ],
            "task_id": "t_owned",
            "reason": "active_pr",
            "phase": "ready",
        },
        {
            "expires_at": 1785660600,
            "window_seconds": 600,
            "task_id": "t_rate",
            "reason": "rate_limit_cooldown",
            "phase": "ready",
        },
    ]


def test_legacy_daemon_tick_uses_shared_respawn_diagnostics(
    kanban_home, monkeypatch, capsys
):
    result = kb.DispatchResult()
    result.add_respawn_guard(
        "t_rate",
        "rate_limit_cooldown",
        detail={"expires_at": 1785660600, "window_seconds": 600},
        phase="ready",
    )

    def run_one_tick(**kwargs):
        kwargs["on_tick"](result)

    monkeypatch.setattr(kb, "run_daemon", run_one_tick)
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda _conn: False)

    rc = kc._cmd_daemon(
        argparse.Namespace(
            force=True,
            pidfile=None,
            verbose=False,
            interval=1,
            max=1,
            failure_limit=2,
        )
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert (
        "SKIP t_rate respawn_guarded=rate_limit_cooldown "
        "expires=1785660600 phase=ready"
    ) in captured.out


def test_run_slash_context_output_format(kanban_home):
    out = kc.run_slash("create 'tech spec' --assignee alice --body 'write an RFC'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    ctx = kc.run_slash(f"context {tid}")
    assert "tech spec" in ctx
    assert "write an RFC" in ctx
    assert "performance section" in ctx


def test_run_slash_tenant_filter(kanban_home):
    kc.run_slash("create 'biz-a task' --tenant biz-a --assignee alice")
    kc.run_slash("create 'biz-b task' --tenant biz-b --assignee alice")
    a = kc.run_slash("list --tenant biz-a")
    b = kc.run_slash("list --tenant biz-b")
    assert "biz-a task" in a and "biz-b task" not in a
    assert "biz-b task" in b and "biz-a task" not in b


def test_run_slash_session_filter(kanban_home):
    """`hermes kanban list --session <id>` filters by the originating
    chat session id stamped on tasks created from inside an ACP loop."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="from sess-1 a", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-1 b", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-2", assignee="alice", session_id="sess-2"
        )
        kb.create_task(conn, title="cli only", assignee="alice")
    out_1 = kc.run_slash("list --session sess-1")
    out_2 = kc.run_slash("list --session sess-2")
    assert "from sess-1 a" in out_1
    assert "from sess-1 b" in out_1
    assert "from sess-2" not in out_1
    assert "cli only" not in out_1
    assert "from sess-2" in out_2
    assert "from sess-1 a" not in out_2


def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_run_slash_usage_error_returns_message(kanban_home):
    # Missing required argument for create
    out = kc.run_slash("create")
    assert "usage" in out.lower() or "error" in out.lower()


def test_run_slash_assign_reassigns(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    assert "Assigned" in kc.run_slash(f"assign {tid} bob")
    show = kc.run_slash(f"show {tid}")
    assert "bob" in show


def test_run_slash_link_unlink(kanban_home):
    a = kc.run_slash("create 'a'")
    b = kc.run_slash("create 'b'")
    import re
    ta = re.search(r"(t_[a-f0-9]+)", a).group(1)
    tb = re.search(r"(t_[a-f0-9]+)", b).group(1)
    assert "Linked" in kc.run_slash(f"link {ta} {tb}")
    # After link, b is todo
    show = kc.run_slash(f"show {tb}")
    assert "todo" in show
    assert "Unlinked" in kc.run_slash(f"unlink {ta} {tb}")


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------

def test_kanban_is_resolvable():
    from hermes_cli.commands import resolve_command

    cmd = resolve_command("kanban")
    assert cmd is not None
    assert cmd.name == "kanban"


def test_kanban_bypasses_active_session_guard():
    from hermes_cli.commands import should_bypass_active_session

    assert should_bypass_active_session("kanban")


def test_kanban_in_autocomplete_table():
    from hermes_cli.commands import COMMANDS, SUBCOMMANDS

    assert "/kanban" in COMMANDS
    subs = SUBCOMMANDS.get("/kanban") or []
    assert "create" in subs
    assert "dispatch" in subs
    assert "continuation" in subs


def test_kanban_autocomplete_includes_live_subcommands():
    from prompt_toolkit.document import Document

    from hermes_cli.commands import SlashCommandCompleter

    completer = SlashCommandCompleter()
    doc = Document("/kanban sp", cursor_position=len("/kanban sp"))
    texts = {c.text for c in completer.get_completions(doc, None)}

    assert "specify" in texts

    doc = Document("/kanban re", cursor_position=len("/kanban re"))
    texts = {c.text for c in completer.get_completions(doc, None)}

    assert "reclaim" in texts
    assert "reassign" in texts


def test_kanban_not_gateway_only():
    # kanban is available in BOTH CLI and gateway surfaces.
    from hermes_cli.commands import COMMAND_REGISTRY

    cmd = next(c for c in COMMAND_REGISTRY if c.name == "kanban")
    assert not cmd.cli_only
    assert not cmd.gateway_only


# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()


def test_run_slash_reassign_with_reclaim_flag(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'switch model' --assignee orig")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    tid = m.group(1)

    # Simulate a running claim.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reassign {tid} newbie --reclaim --reason 'switch'")
    assert "Reassigned" in out, out
    out2 = kc.run_slash(f"show {tid}")
    assert "newbie" in out2


# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_specify_end_to_end(kanban_home, monkeypatch):
    """The /kanban specify slash command routes through run_slash, which
    both the interactive CLI and every gateway platform use. This test
    covers both surfaces."""
    from unittest.mock import MagicMock

    # Create a triage task via the same slash surface.
    create_out = kc.run_slash("create 'rough idea' --triage")
    import re
    m = re.search(r"(t_[a-f0-9]+)", create_out)
    assert m, f"no task id in: {create_out!r}"
    tid = m.group(1)

    # Mock the auxiliary client so we don't hit a real provider.
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = (
        '{"title": "Spec: rough idea", "body": "**Goal**\\nShip it."}'
    )
    # specify_task routes through call_llm now (#35566) — mock it directly.
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        MagicMock(return_value=resp),
    )

    # Specify via slash.
    out = kc.run_slash(f"specify {tid}")
    assert "Specified" in out
    assert tid in out

    # Task is promoted and retitled.
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status in {"todo", "ready"}
    assert task.title == "Spec: rough idea"


def test_run_slash_specify_help_is_reachable(kanban_home):
    """`-h`/`--help` on a subcommand returns the actual help text — see
    issue #21794. argparse writes help to stdout and exits 0; run_slash
    must capture both streams and treat exit 0 as success, not error."""
    out = kc.run_slash("specify --help")
    assert "specify" in out.lower()
    # Help dump should NOT come back wrapped as a usage error.
    assert not out.startswith("⚠")


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------

def test_run_slash_bare_returns_curated_help(kanban_home):
    """Bare `/kanban` returns the curated short-help block — not a 5KB
    argparse usage dump."""
    out = kc.run_slash("")
    assert "/kanban" in out
    assert "list" in out
    assert "show" in out
    assert "decompose" in out
    assert "gate" in out
    assert "continuation" in out
    # Sanity: should be a chat-friendly size, not the raw usage tree.
    assert len(out) < 2000
    # Shouldn't surface argparse's usage-error sentinel.
    assert "usage error" not in out.lower()


@pytest.mark.parametrize("alias", ["help", "--help", "-h", "?"])
def test_run_slash_help_aliases_match_bare(kanban_home, alias):
    """Every documented help alias produces the same curated output."""
    bare = kc.run_slash("")
    out = kc.run_slash(alias)
    assert out == bare


def test_run_slash_subcommand_help_returns_help_text(kanban_home):
    """`/kanban show -h` returns the actual subcommand help, not a
    fake `(usage error: 0)` sentinel."""
    out = kc.run_slash("show -h")
    assert "task_id" in out
    assert "/kanban show" in out
    assert not out.startswith("⚠")


def test_run_slash_decompose_json_reports_stale_skip(kanban_home):
    outcome = decomp.DecomposeOutcome(
        "t_stale",
        False,
        "skipped: task status changed to 'ready'; task left unchanged",
        skipped=True,
        root_status="ready",
    )
    with patch.object(decomp, "decompose_task", return_value=outcome):
        payload = json.loads(kc.run_slash("decompose t_stale --json"))

    assert payload["ok"] is False
    assert payload["skipped"] is True
    assert payload["root_status"] == "ready"
    assert "status changed" in payload["reason"]


def test_run_slash_decompose_reports_dependency_closure(kanban_home):
    outcome = decomp.DecomposeOutcome(
        "t_graph",
        True,
        "decomposed into 3 children",
        fanout=True,
        child_ids=["t_a", "t_b", "t_c"],
        root_status="triage",
        dependency_edges=2,
        root_dependencies=3,
        leaf_count=1,
    )
    with patch.object(decomp, "decompose_task", return_value=outcome):
        out = kc.run_slash("decompose t_graph")

    assert "root custody preserved in triage" in out
    assert "dependency closure: 2 internal edge(s)" in out
    assert "root waits on 3 child task(s)" in out
    assert "1 leaf task(s)" in out


def test_run_slash_unknown_action_friendly_error(kanban_home):
    """Unknown subcommand surfaces a single-line usage error prefixed
    with our marker — no `(usage error: 2)` wrapping, no doubled
    `kanban kanban` prog string."""
    out = kc.run_slash("frobnicate")
    assert "/kanban" in out
    assert "frobnicate" in out
    assert "/kanban-wrap" not in out
    assert "/kanban kanban" not in out
    assert "(usage error: " not in out


def test_run_slash_missing_required_arg_friendly_error(kanban_home):
    """Missing positional argument shows the subcommand-scoped usage
    line, not the top-level kanban tree."""
    out = kc.run_slash("show")
    assert "/kanban show" in out
    assert "task_id" in out


def test_run_slash_board_override_restores_prior_env(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "beta")

    kc.run_slash("--board alpha list")

    assert os.environ.get("HERMES_KANBAN_BOARD") == "beta"


def test_run_slash_board_override_does_not_change_boards_show_current(kanban_home):
    kb.create_board("alpha")
    kb.create_board("beta")
    kb.set_current_board("alpha")

    out = kc.run_slash("--board beta boards show")

    assert "Current board: alpha" in out


# ---------------------------------------------------------------------------
# Privileged audit-reason visibility (Unicode-invisible rejection)
# ---------------------------------------------------------------------------

_INVISIBLE_CLI_AUDIT_REASONS = [
    "\u200b",
    "\ufeff",
    "\u2060",
    "\u034f",
    "\ufe00",
    "\U000e0100",
    "\u115f",
    "\u1160",
    "\u3164",
    "\uffa0",
    "\u2800",
    "\u0301",
    " \u034f \ufe00 \U000e0100 ",
]


@pytest.mark.parametrize("invisible", _INVISIBLE_CLI_AUDIT_REASONS)
def test_run_slash_continuation_review_rejects_invisible_reason(kanban_home, invisible):
    import re

    out = kc.run_slash("create 'invisible review' --assignee engineer")
    task_id = re.search(r"(t_[a-f0-9]+)", out).group(1)

    out = kc.run_slash(
        f"continuation review {task_id} --verdict fix-required --reason '{invisible}'"
    )

    assert "review reason required" in out
    with kb.connect() as conn:
        events = kb.list_events(conn, task_id)
        assert not [e for e in events if e.kind == "continuation_reviewed"]


@pytest.mark.parametrize("invisible", _INVISIBLE_CLI_AUDIT_REASONS)
def test_run_slash_claim_operator_override_rejects_invisible_reason(kanban_home, invisible):
    import re

    out = kc.run_slash("create 'invisible override' --assignee engineer")
    task_id = re.search(r"(t_[a-f0-9]+)", out).group(1)

    out = kc.run_slash(f"claim {task_id} --operator-override-reason '{invisible}'")

    assert "operator override reason required" in out
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"
