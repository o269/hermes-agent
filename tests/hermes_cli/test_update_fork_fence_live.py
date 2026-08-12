"""Exact-live regression coverage for the PR #10 updater fence port."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd


class TestUpdateRemoteFenceLivePort:
    @staticmethod
    def _git(repo, *args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=check,
        )

    def _init_repo(self, repo):
        self._git(repo, "init", "--initial-branch=main")
        self._git(repo, "config", "user.email", "test@example.com")
        self._git(repo, "config", "user.name", "Test")
        (repo / "base.txt").write_text("base", encoding="utf-8")
        self._git(repo, "add", "base.txt")
        self._git(repo, "commit", "-m", "base")

    def test_resolver_prefers_fork_when_origin_is_upstream(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._git(
            repo,
            "remote",
            "add",
            "origin",
            "https://github.com/NousResearch/Hermes-Agent.git",
        )
        self._git(
            repo,
            "remote",
            "add",
            "fork",
            "https://github.com/o269/hermes-agent.git",
        )

        assert update_cmd._resolve_update_remote(["git"], repo) == (
            "fork",
            "https://github.com/o269/hermes-agent.git",
        )

    def test_local_commit_counter_detects_committed_work(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        base_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()
        self._git(repo, "update-ref", "refs/remotes/fork/main", base_sha)
        (repo / "local.txt").write_text("local", encoding="utf-8")
        self._git(repo, "add", "local.txt")
        self._git(repo, "commit", "-m", "local-only")

        assert update_cmd._count_local_commits_ahead(["git"], repo, "fork/main") == 1

    def test_implicit_update_cannot_leave_non_default_branch(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._git(repo, "switch", "-c", "broker-cutover-20260805")

        with pytest.raises(SystemExit) as exc_info:
            update_cmd._guard_non_default_update_branch(
                ["git"],
                repo,
                "main",
                branch_explicit=False,
            )

        assert exc_info.value.code == 1
        assert (
            "Refusing to leave non-default checkout 'broker-cutover-20260805'"
            in capsys.readouterr().out
        )

    def test_explicit_active_branch_update_is_allowed(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._git(repo, "switch", "-c", "broker-cutover-20260805")

        update_cmd._guard_non_default_update_branch(
            ["git"],
            repo,
            "broker-cutover-20260805",
            branch_explicit=True,
        )

    def test_apply_path_refuses_reset_when_local_commits_exist(
        self, tmp_path, monkeypatch
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        from hermes_cli import main as hm

        monkeypatch.setattr(hm, "PROJECT_ROOT", repo)
        monkeypatch.setattr(hm, "_is_windows", lambda: False)
        monkeypatch.setattr(hm, "_run_pre_update_backup", lambda _args: None)
        monkeypatch.setattr(hm, "_pause_windows_gateways_for_update", lambda: None)
        monkeypatch.setattr(hm, "_resolve_update_branch", lambda _args: "main")
        monkeypatch.setattr(hm, "_stash_local_changes_if_needed", lambda *_args: None)
        monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *_args: None)
        monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *_args: None)
        monkeypatch.setattr(
            update_cmd,
            "_guard_non_default_update_branch",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            update_cmd,
            "_resolve_update_remote",
            lambda *_args: (
                "fork",
                "https://github.com/o269/hermes-agent.git",
            ),
        )
        monkeypatch.setattr(
            update_cmd,
            "_count_local_commits_ahead",
            lambda *_args, **_kwargs: 2,
        )

        commands: list[list[str]] = []

        def run_side_effect(command, **_kwargs):
            command = [str(part) for part in command]
            commands.append(command)
            if command[-3:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return subprocess.CompletedProcess(
                    command, 0, stdout="main\n", stderr=""
                )
            if "rev-list" in command:
                return subprocess.CompletedProcess(command, 0, stdout="1\n", stderr="")
            if "merge" in command and "--ff-only" in command:
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr="fatal: Not possible to fast-forward",
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(update_cmd.subprocess, "run", run_side_effect)

        with pytest.raises(SystemExit) as exc_info:
            update_cmd._cmd_update_impl(SimpleNamespace(), gateway_mode=False)

        assert exc_info.value.code == 1
        assert ["git", "fetch", "fork", "main"] in commands
        assert not any(
            "reset" in command and "--hard" in command for command in commands
        )

    @pytest.mark.parametrize(
        "url",
        [
            "file:///tmp/hermes-agent",
            "ext::sh -c id",
            "http://github.com/example/hermes-agent.git",
            "https://evil.example/example/hermes-agent.git",
            "https://github.com/example/not-hermes.git",
            "https://user@github.com/example/hermes-agent.git",
        ],
    )
    def test_unsafe_remote_urls_are_rejected(self, tmp_path, url):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._git(repo, "remote", "add", "origin", url)

        with pytest.raises(SystemExit) as exc_info:
            update_cmd._resolve_update_remote(["git"], repo)

        assert exc_info.value.code == 1
