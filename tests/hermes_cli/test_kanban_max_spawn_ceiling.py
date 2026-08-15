"""Parameterized max_spawn ceiling (DEFAULT_MAX_SPAWN=16, fail-closed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def test_resolve_unset_defaults_to_16():
    res = kb.resolve_max_spawn_ceiling(None, env={})
    assert res.value == 16
    assert res.source == "default"
    assert res.invalid is False


def test_resolve_explicit_positive():
    res = kb.resolve_max_spawn_ceiling(4, env={})
    assert res.value == 4
    assert res.source == "explicit"


def test_resolve_invalid_fails_closed():
    res = kb.resolve_max_spawn_ceiling("nope", env={})
    assert res.value == 16
    assert res.source == "fail_closed"
    assert res.invalid is True


def test_resolve_env_override():
    res = kb.resolve_max_spawn_ceiling(None, env={"HERMES_KANBAN_MAX_SPAWN": "3"})
    assert res.value == 3
    assert res.source == "env"


def test_resolve_zero_and_bool_fail_closed():
    assert kb.resolve_max_spawn_ceiling(0, env={}).invalid is True
    assert kb.resolve_max_spawn_ceiling(True, env={}).invalid is True


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_MAX_SPAWN", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb.init_db()
    return home


def test_dispatch_applies_default_ceiling(kanban_home):
    with kb.connect() as conn:
        res = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 1, dry_run=True)
    assert res.max_spawn_ceiling == 16


def test_dispatch_invalid_max_spawn_fails_closed(kanban_home):
    with kb.connect() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=lambda *_a, **_k: 1, dry_run=True, max_spawn="xyz"
        )
    assert res.max_spawn_ceiling == 16
