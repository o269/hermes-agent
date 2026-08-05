from __future__ import annotations

import json
import subprocess

import tools.terminal_tool as terminal_tool


def _minimal_terminal_config(cwd: str) -> dict[str, object]:
    return {
        "env_type": "local",
        "cwd": cwd,
        "timeout": 60,
        "lifetime_seconds": 3600,
    }


def _git_repo(tmp_path, remote: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=repo, check=True)
    return repo


class FakeEnv:
    env: dict[str, str] = {}

    def __init__(self):
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.cwd = ""

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"output": "ok", "returncode": 0}


def _patch_terminal(monkeypatch, repo):
    fake = FakeEnv()
    task_id = "pr-destination-guard"
    fake.cwd = str(repo)
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: fake})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _minimal_terminal_config(str(repo)))
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_resolve_container_task_id", lambda value: value or "default")
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )
    return task_id, fake


def test_terminal_blocks_hermes_pr_create_that_relies_on_gh_default(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, "https://github.com/o269/hermes-agent.git")
    task_id, fake = _patch_terminal(monkeypatch, repo)

    result = json.loads(
        terminal_tool.terminal_tool(
            command="gh pr create --base main --head fix/unsafe-default",
            task_id=task_id,
        )
    )

    assert result["status"] == "blocked"
    assert result["guard"]["reason_code"] == "missing-explicit-repo"
    assert fake.calls == []


def test_terminal_allows_explicit_safe_hermes_pr_destination(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, "https://github.com/o269/hermes-agent.git")
    task_id, fake = _patch_terminal(monkeypatch, repo)

    result = json.loads(
        terminal_tool.terminal_tool(
            command="gh pr create --repo o269/hermes-agent --base main --head fix/safe",
            task_id=task_id,
        )
    )

    assert result["exit_code"] == 0
    assert fake.calls == [(
        "gh pr create --repo o269/hermes-agent --base main --head fix/safe",
        {"timeout": 60, "cwd": str(repo), "bounded_capture": True},
    )]


def test_terminal_does_not_apply_hermes_pr_guard_to_other_repositories(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, "https://github.com/o269/not-hermes.git")
    task_id, fake = _patch_terminal(monkeypatch, repo)

    result = json.loads(
        terminal_tool.terminal_tool(
            command="gh pr create",
            task_id=task_id,
        )
    )

    assert result["exit_code"] == 0
    assert fake.calls == [(
        "gh pr create",
        {"timeout": 60, "cwd": str(repo), "bounded_capture": True},
    )]
