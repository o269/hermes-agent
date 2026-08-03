"""Regression tests for _apply_profile_override HERMES_HOME guard (issue #22502).

When HERMES_HOME is set to the hermes root (e.g. systemd hardcodes
HERMES_HOME=/root/.hermes), _apply_profile_override must still read
active_profile and update HERMES_HOME to the profile directory.

When HERMES_HOME is already a profile directory (.../profiles/<name>),
_apply_profile_override must trust it and return without re-reading
active_profile (child-process inheritance contract).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace



def _run_apply_profile_override(
    tmp_path, monkeypatch, *, hermes_home: str | None, active_profile: str | None,
    argv: list[str] | None = None,
):
    """Run _apply_profile_override in isolation.

    Returns the value of os.environ["HERMES_HOME"] after the call,
    or None if unset.
    """
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir(parents=True, exist_ok=True)

    if active_profile is not None:
        (hermes_root / "active_profile").write_text(active_profile)

    if active_profile and active_profile != "default":
        (hermes_root / "profiles" / active_profile).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if hermes_home is not None:
        monkeypatch.setenv("HERMES_HOME", hermes_home)
    else:
        monkeypatch.delenv("HERMES_HOME", raising=False)

    monkeypatch.setattr(sys, "argv", argv or ["hermes", "gateway", "start"])

    from hermes_cli.main import _apply_profile_override
    _apply_profile_override()

    return os.environ.get("HERMES_HOME")


def _run_with_worker_env(
    tmp_path, monkeypatch, *,
    hermes_home: str | None,
    active_profile: str | None,
    argv: list[str],
    worker_env: dict[str, str | None],
):
    """Run _apply_profile_override with simulated dispatcher-set worker env.

    Returns a SimpleNamespace with the post-override environment values.
    """
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir(parents=True, exist_ok=True)

    if active_profile is not None:
        (hermes_root / "active_profile").write_text(active_profile)
    if active_profile and active_profile != "default":
        (hermes_root / "profiles" / active_profile).mkdir(parents=True, exist_ok=True)

    # Create profile directories for the explicit -p argument and the parent.
    explicit_profile = None
    try:
        idx = argv.index("-p")
        explicit_profile = argv[idx + 1]
    except (ValueError, IndexError):
        pass
    if explicit_profile:
        (hermes_root / "profiles" / explicit_profile).mkdir(parents=True, exist_ok=True)
    if hermes_home and Path(hermes_home).parent.name == "profiles":
        parent_name = Path(hermes_home).name
        (hermes_root / "profiles" / parent_name).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if hermes_home is not None:
        monkeypatch.setenv("HERMES_HOME", hermes_home)
    else:
        monkeypatch.delenv("HERMES_HOME", raising=False)

    for name, value in worker_env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    monkeypatch.setattr(sys, "argv", list(argv))

    from hermes_cli.main import _apply_profile_override
    _apply_profile_override()

    return SimpleNamespace(
        hermes_home=os.environ.get("HERMES_HOME"),
        hermes_profile=os.environ.get("HERMES_PROFILE"),
        kanban_task=os.environ.get("HERMES_KANBAN_TASK"),
        kanban_run_id=os.environ.get("HERMES_KANBAN_RUN_ID"),
        kanban_claim_lock=os.environ.get("HERMES_KANBAN_CLAIM_LOCK"),
        terminal_cwd=os.environ.get("TERMINAL_CWD"),
    )

class TestApplyProfileOverrideHermesHomeGuard:
    """Regression guard for issue #22502.

    Verifies that HERMES_HOME pointing to the hermes root does NOT suppress
    the active_profile check, while HERMES_HOME already pointing to a
    profile directory IS trusted as-is.
    """

    def test_hermes_home_at_root_with_active_profile_is_redirected(
        self, tmp_path, monkeypatch
    ):
        """HERMES_HOME=/root/.hermes + active_profile=coder must redirect
        HERMES_HOME to .../profiles/coder.

        Bug scenario from #22502: systemd sets HERMES_HOME to the hermes root
        and the user switches to a profile via `hermes profile use`.
        Before the fix, the guard returned early and active_profile was ignored.
        """
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)

        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="coder",
        )

        assert result is not None, "HERMES_HOME must be set after profile redirect"
        assert "profiles" in result, (
            f"Expected HERMES_HOME to point into profiles/ dir, got: {result!r}"
        )
        assert result.endswith("coder"), (
            f"Expected HERMES_HOME to end with 'coder', got: {result!r}"
        )

    def test_hermes_home_already_profile_dir_is_trusted(self, tmp_path, monkeypatch):
        """HERMES_HOME=.../profiles/coder must not be overridden even when
        active_profile says something different.

        Preserves the child-process inheritance contract: a subprocess spawned
        with HERMES_HOME already set to a specific profile must stay in that
        profile.
        """
        hermes_root = tmp_path / ".hermes"
        profile_dir = hermes_root / "profiles" / "coder"
        profile_dir.mkdir(parents=True, exist_ok=True)

        (hermes_root / "active_profile").write_text("other")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "start"])

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") == str(profile_dir), (
            "HERMES_HOME must remain unchanged when already pointing to a profile dir"
        )

    def test_hermes_home_unset_reads_active_profile(self, tmp_path, monkeypatch):
        """Classic case: HERMES_HOME unset + active_profile=coder must set
        HERMES_HOME to the profile directory (existing behaviour must not regress).
        """
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=None,
            active_profile="coder",
        )

        assert result is not None
        assert "coder" in result

    def test_sudo_explicit_profile_resolves_invoking_users_profile(self, tmp_path, monkeypatch):
        """sudo elias ... should resolve `-p elias` under SUDO_USER, not root."""
        root_home = tmp_path / "root"
        user_home = tmp_path / "home" / "hermes"
        profile_dir = user_home / ".hermes" / "profiles" / "elias"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (root_home / ".hermes").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: root_home)
        monkeypatch.setenv("SUDO_USER", "hermes")
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(sys, "argv", ["hermes", "-p", "elias", "gateway", "install", "--system"])

        import pwd

        monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir=str(user_home)))

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") == str(profile_dir)
        assert sys.argv == ["hermes", "gateway", "install", "--system"]

    def test_hermes_home_unset_default_profile_no_redirect(self, tmp_path, monkeypatch):
        """active_profile=default must not redirect HERMES_HOME."""
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "start"])
        (hermes_root / "active_profile").write_text("default")

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") is None

    def test_subcommand_profile_flag_is_not_consumed(self, tmp_path, monkeypatch):
        """Command argv flags named --profile must stay with that command.

        Docker Desktop's MCP Toolkit uses `docker mcp gateway run --profile ...`.
        When that argv is passed through `hermes mcp add --args`, the early
        profile pre-parser must not interpret the Docker profile as a Hermes
        profile.
        """
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)
        argv = [
            "hermes",
            "mcp",
            "add",
            "docker-research",
            "--command",
            "docker",
            "--args",
            "mcp",
            "gateway",
            "run",
            "--profile",
            "research",
        ]

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", list(argv))

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") is None
        assert sys.argv == argv

    def test_profile_after_chat_subcommand_is_still_consumed(self, tmp_path, monkeypatch):
        """Profile flags historically work after normal Hermes subcommands."""
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=None,
            active_profile="coder",
            argv=["hermes", "chat", "-p", "coder", "-q", "hello"],
        )

        assert result is not None
        assert result.endswith("coder")
        assert sys.argv == ["hermes", "chat", "-q", "hello"]

    def test_top_level_profile_after_value_flag_is_consumed(self, tmp_path, monkeypatch):
        """Top-level --profile still works after other top-level value flags."""
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=None,
            active_profile="coder",
            argv=["hermes", "-m", "gpt-5", "--profile", "coder", "chat"],
        )

        assert result is not None
        assert result.endswith("coder")
        assert sys.argv == ["hermes", "-m", "gpt-5", "chat"]

    def test_top_level_profile_after_continue_flag_is_consumed(self, tmp_path, monkeypatch):
        """--continue has an optional value, so a following --profile is a flag."""
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=None,
            active_profile="coder",
            argv=["hermes", "--continue", "--profile", "coder"],
        )

        assert result is not None
        assert result.endswith("coder")
        assert sys.argv == ["hermes", "--continue"]


class TestSupervisedChildIgnoresStickyProfile:
    """The reserved default gateway s6 slot must not follow active_profile.

    Inside the Docker s6 image the ``gateway-default`` service slot runs a
    bare ``hermes gateway run`` (no ``-p``) to mean "the root HERMES_HOME
    profile". The run-script exports ``HERMES_S6_SUPERVISED_CHILD=1``.
    Without a guard, ``_apply_profile_override`` would read the sticky
    ``active_profile`` file (set by e.g. the dashboard profile switcher) and
    redirect the reserved default gateway into that profile — producing a
    duplicate gateway for the active profile and no real default gateway.
    """

    def test_supervised_child_does_not_follow_active_profile(
        self, tmp_path, monkeypatch
    ):
        """HERMES_S6_SUPERVISED_CHILD + active_profile=briefer must NOT redirect.

        Reproduces the Docker/profile scoping bug: the supervised default
        gateway is launched as bare ``hermes gateway run`` with
        HERMES_HOME=/opt/data (the container root, whose parent is NOT
        ``profiles``), and a sticky ``active_profile`` of another profile.
        The reserved default slot must stay on the root profile.
        """
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)
        (hermes_root / "active_profile").write_text("briefer")
        (hermes_root / "profiles" / "briefer").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Container root HERMES_HOME: parent dir is NOT "profiles", so the
        # #22502 guard does not short-circuit — step 2 (active_profile) runs.
        monkeypatch.setenv("HERMES_HOME", str(hermes_root))
        monkeypatch.setenv("HERMES_S6_SUPERVISED_CHILD", "1")
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") == str(hermes_root), (
            "Supervised default gateway must stay on the root profile, not be "
            f"hijacked by active_profile; got {os.environ.get('HERMES_HOME')!r}"
        )

    def test_non_supervised_run_still_follows_active_profile(
        self, tmp_path, monkeypatch
    ):
        """Without the sentinel, a normal `hermes gateway run` still honors
        active_profile — the guard is scoped strictly to supervised children."""
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=None,
            active_profile="briefer",
            argv=["hermes", "gateway", "run"],
        )

        assert result is not None
        assert result.endswith("briefer")

    def test_supervised_named_profile_flag_still_wins(self, tmp_path, monkeypatch):
        """A supervised named-profile slot passes ``-p <name>`` explicitly;
        that must still resolve (the sentinel guard only skips the sticky
        active_profile fallback, never an explicit flag)."""
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)
        (hermes_root / "active_profile").write_text("briefer")
        (hermes_root / "profiles" / "briefer").mkdir(parents=True, exist_ok=True)
        (hermes_root / "profiles" / "coder").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setenv("HERMES_S6_SUPERVISED_CHILD", "1")
        monkeypatch.setattr(sys, "argv", ["hermes", "-p", "coder", "gateway", "run"])

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        result = os.environ.get("HERMES_HOME")
        assert result is not None
        assert result.endswith("coder")


class TestCrossProfileKanbanContextScrub:
    """Containment: a cross-profile explicit ``-p`` from a dispatched worker must
    drop inherited Kanban card context so the nested session is not a worker.

    Same-profile dispatcher startup (``-p <same>`` with a matching parent profile)
    must retain the context so workers can operate.

    Regression coverage for the run-6550 incident: an engineer worker spawned a
    ``hermes -p gemini1`` liveness probe that kept HERMES_KANBAN_* and completed
    the engineer's run.
    """

    def test_same_profile_worker_retains_card_context(self, tmp_path, monkeypatch):
        """Dispatcher startup pattern: -p engineer from an engineer worker."""
        hermes_root = tmp_path / ".hermes"
        engineer_home = hermes_root / "profiles" / "engineer"
        engineer_home.mkdir(parents=True, exist_ok=True)

        result = _run_with_worker_env(
            tmp_path,
            monkeypatch,
            hermes_home=str(engineer_home),
            active_profile="engineer",
            argv=["hermes", "-p", "engineer", "chat"],
            worker_env={
                "HERMES_PROFILE": "engineer",
                "HERMES_KANBAN_TASK": "t_0e2540e1",
                "HERMES_KANBAN_RUN_ID": "r_6550",
                "HERMES_KANBAN_CLAIM_LOCK": "claim-engineer-6550",
                "TERMINAL_CWD": "/workspace/engineer",
            },
        )

        assert result.hermes_home.endswith("profiles/engineer")
        assert result.hermes_profile == "engineer"
        assert result.kanban_task == "t_0e2540e1"
        assert result.kanban_run_id == "r_6550"
        assert result.kanban_claim_lock == "claim-engineer-6550"
        assert result.terminal_cwd == "/workspace/engineer"

    def test_cross_profile_worker_scrubs_card_context(self, tmp_path, monkeypatch):
        """Incident repro: engineer worker launches a gemini1 probe."""
        hermes_root = tmp_path / ".hermes"
        engineer_home = hermes_root / "profiles" / "engineer"
        gemini1_home = hermes_root / "profiles" / "gemini1"
        engineer_home.mkdir(parents=True, exist_ok=True)
        gemini1_home.mkdir(parents=True, exist_ok=True)

        result = _run_with_worker_env(
            tmp_path,
            monkeypatch,
            hermes_home=str(engineer_home),
            active_profile="engineer",
            argv=["hermes", "-p", "gemini1", "-z", "liveness probe", "--cli"],
            worker_env={
                "HERMES_PROFILE": "engineer",
                "HERMES_KANBAN_TASK": "t_0e2540e1",
                "HERMES_KANBAN_RUN_ID": "r_6550",
                "HERMES_KANBAN_CLAIM_LOCK": "claim-engineer-6550",
                "HERMES_KANBAN_BOARD_SLUG": "fleet",
                "TERMINAL_CWD": "/workspace/engineer",
            },
        )

        assert result.hermes_home.endswith("profiles/gemini1")
        assert result.hermes_profile == "gemini1"
        assert result.kanban_task is None
        assert result.kanban_run_id is None
        assert result.kanban_claim_lock is None
        assert result.terminal_cwd is None

    def test_cross_profile_scrub_derives_parent_from_home(self, tmp_path, monkeypatch):
        """Parent profile can be derived from HERMES_HOME when HERMES_PROFILE is absent."""
        hermes_root = tmp_path / ".hermes"
        engineer_home = hermes_root / "profiles" / "engineer"
        gemini1_home = hermes_root / "profiles" / "gemini1"
        engineer_home.mkdir(parents=True, exist_ok=True)
        gemini1_home.mkdir(parents=True, exist_ok=True)

        result = _run_with_worker_env(
            tmp_path,
            monkeypatch,
            hermes_home=str(engineer_home),
            active_profile="engineer",
            argv=["hermes", "-p", "gemini1", "chat"],
            worker_env={
                "HERMES_PROFILE": None,
                "HERMES_KANBAN_TASK": "t_0e2540e1",
                "HERMES_KANBAN_RUN_ID": "r_6550",
                "TERMINAL_CWD": "/workspace/engineer",
            },
        )

        assert result.hermes_profile == "gemini1"
        assert result.kanban_task is None
        assert result.kanban_run_id is None

    def test_active_profile_fallback_does_not_scrub(self, tmp_path, monkeypatch):
        """No explicit -p means no scrub; active_profile fallback is a user choice."""
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)
        gemini1_home = hermes_root / "profiles" / "gemini1"
        gemini1_home.mkdir(parents=True, exist_ok=True)

        result = _run_with_worker_env(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="gemini1",
            argv=["hermes", "chat"],
            worker_env={
                "HERMES_PROFILE": "engineer",
                "HERMES_KANBAN_TASK": "t_0e2540e1",
                "HERMES_KANBAN_RUN_ID": "r_6550",
            },
        )

        assert result.hermes_profile == "gemini1"
        assert result.kanban_task == "t_0e2540e1"
        assert result.kanban_run_id == "r_6550"

    def test_no_kanban_task_means_no_scrub_required(self, tmp_path, monkeypatch):
        """Cross-profile explicit -p without a card is just a normal switch."""
        hermes_root = tmp_path / ".hermes"
        engineer_home = hermes_root / "profiles" / "engineer"
        gemini1_home = hermes_root / "profiles" / "gemini1"
        engineer_home.mkdir(parents=True, exist_ok=True)
        gemini1_home.mkdir(parents=True, exist_ok=True)

        result = _run_with_worker_env(
            tmp_path,
            monkeypatch,
            hermes_home=str(engineer_home),
            active_profile="engineer",
            argv=["hermes", "-p", "gemini1", "chat"],
            worker_env={
                "HERMES_PROFILE": "engineer",
                "HERMES_KANBAN_TASK": None,
                "TERMINAL_CWD": "/workspace/engineer",
            },
        )

        assert result.hermes_profile == "gemini1"
        assert result.kanban_task is None
        assert result.terminal_cwd == "/workspace/engineer"

    def test_hermes_profile_set_for_trusted_home_profile(self, tmp_path, monkeypatch):
        """When HERMES_HOME already points to a profile dir, HERMES_PROFILE must
        be updated to match, not left stale."""
        hermes_root = tmp_path / ".hermes"
        coder_home = hermes_root / "profiles" / "coder"
        coder_home.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(coder_home))
        monkeypatch.setenv("HERMES_PROFILE", "stale-parent")
        monkeypatch.setattr(sys, "argv", ["hermes", "chat"])

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") == str(coder_home)
        assert os.environ.get("HERMES_PROFILE") == "coder"

