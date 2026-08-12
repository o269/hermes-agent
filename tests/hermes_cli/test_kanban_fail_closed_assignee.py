"""Fail-closed spawn policy: default / missing registry never spawn."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn(*_args, **_kwargs):
    return 12345


def test_assignee_default_is_not_a_spawn_target():
    assert kb._assignee_has_spawn_target("default") is False
    assert kb._assignee_has_spawn_target("Default") is False
    assert kb._assignee_has_spawn_target("") is False
    assert kb._assignee_has_spawn_target(None) is False


def test_missing_profile_registry_fails_closed(monkeypatch):
    def _boom(_name):
        raise RuntimeError("profiles unavailable")

    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", _boom, raising=False
    )
    # normalize_profile_name still works; profile_exists must fail closed.
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(
        profiles, "profile_exists", _boom,
    )
    assert kb._assignee_has_spawn_target("worker") is False


def test_explicit_default_assignee_is_skipped_nonspawnable(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="parked", assignee="default")
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert task_id in res.skipped_nonspawnable
    assert res.spawned == []


def test_default_assignee_config_default_does_not_auto_spawn(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="unassigned", assignee=None)
        res = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=False,
            default_assignee="default",
        )
    assert task_id in res.skipped_unassigned
    assert task_id not in res.auto_assigned_default
    assert res.spawned == []
