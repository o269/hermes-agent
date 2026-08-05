"""Parameterized max_spawn ceiling (DEFAULT_MAX_SPAWN=16, fail-closed).

Card t_4cc8b20a / operator ruling KEEP live MAX_DISPATCH=16:
  * unset -> 16
  * valid override honored
  * invalid -> fail closed to 16 + max_spawn_invalid audit event

The R7 classifier reference package hardcodes 12 and must NOT be installed
over the live reconciler; this is the hermes-agent integration surface that
parameterizes the ceiling inside _dispatch_once_locked.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


@pytest.fixture()
def kb(monkeypatch):
    """Fresh HERMES_HOME with enough profiles to fill a ceiling of 16."""
    home = tempfile.mkdtemp(prefix="kanban_max_spawn_")
    for i in range(24):
        os.makedirs(os.path.join(home, "profiles", f"p{i:02d}"), exist_ok=True)
    os.makedirs(os.path.join(home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", home)
    monkeypatch.setenv("HERMES_KANBAN_BROKER", "0")
    # Isolate from ambient fleet env that may set MAX_DISPATCH / HERMES_KANBAN_*.
    monkeypatch.delenv("HERMES_KANBAN_MAX_SPAWN", raising=False)
    monkeypatch.delenv("MAX_DISPATCH", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db

    yield kanban_db


def _seed_ready(kb_mod, n: int, *, priority_base: int = 100):
    """Create n ready tasks on distinct profiles (one-card-per-profile)."""
    ids = []
    with kb_mod.connect_closing() as conn:
        try:
            kb_mod.create_board(slug="default", name="Test")
        except Exception:
            pass
        for i in range(n):
            tid = kb_mod.create_task(
                conn,
                title=f"ready-{i}",
                assignee=f"p{i:02d}",
                priority=priority_base - i,
            )
            ids.append(tid)
    return ids


def test_resolve_unset_defaults_to_16(kb):
    res = kb.resolve_max_spawn_ceiling(None, env={})
    assert res.value == 16
    assert res.value == kb.DEFAULT_MAX_SPAWN
    assert res.source == "default"
    assert res.invalid is False


def test_resolve_valid_explicit_override(kb):
    res = kb.resolve_max_spawn_ceiling(24, env={})
    assert res.value == 24
    assert res.source == "explicit"
    assert res.invalid is False


def test_resolve_valid_env_override(kb, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MAX_SPAWN", "7")
    res = kb.resolve_max_spawn_ceiling(None)
    assert res.value == 7
    assert res.source == "env"
    assert res.env_key == "HERMES_KANBAN_MAX_SPAWN"
    assert res.invalid is False


def test_resolve_ignores_bare_max_dispatch_env(kb, monkeypatch):
    """Host-global MAX_DISPATCH must not clamp hermes (reconciler uses --max)."""
    monkeypatch.setenv("MAX_DISPATCH", "1")
    res = kb.resolve_max_spawn_ceiling(None, env={"MAX_DISPATCH": "1"})
    assert res.value == 16
    assert res.source == "default"
    assert res.invalid is False


def test_resolve_invalid_explicit_fail_closed(kb):
    res = kb.resolve_max_spawn_ceiling("not-a-number", env={})
    assert res.value == 16
    assert res.source == "fail_closed"
    assert res.invalid is True
    assert res.raw == "not-a-number"


def test_resolve_invalid_env_fail_closed(kb, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MAX_SPAWN", "0")
    res = kb.resolve_max_spawn_ceiling(None)
    assert res.value == 16
    assert res.invalid is True
    assert res.source == "fail_closed"


def test_resolve_never_hardcodes_r7_ceiling_12(kb):
    """Guard: default must stay 16 (live), not the R7 reference package's 12."""
    assert kb.DEFAULT_MAX_SPAWN == 16
    assert kb.DEFAULT_MAX_SPAWN != 12
    assert kb.resolve_max_spawn_ceiling(None, env={}).value != 12


def test_dispatch_once_unset_applies_ceiling_16(kb, monkeypatch):
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _a: True)
    _seed_ready(kb, 20)

    with kb.connect_closing() as conn:
        result = kb.dispatch_once(conn, dry_run=True)  # no explicit max_spawn
        assert result.max_spawn_ceiling == 16
        assert result.max_spawn_resolution is not None
        assert result.max_spawn_resolution["value"] == 16
        assert result.max_spawn_resolution["source"] == "default"
        assert result.max_spawn_resolution["invalid"] is False
        assert result.max_spawn_audit_event_id is None

        spawned = [d for d in result.dispositions if d.outcome == "spawned"]
        held_ceiling = [
            d for d in result.dispositions
            if d.outcome == "held" and d.reason == "max_spawn"
        ]
        assert len(spawned) == 16
        assert len(held_ceiling) == 4


def test_dispatch_once_valid_override_honored(kb, monkeypatch):
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _a: True)
    _seed_ready(kb, 5)

    with kb.connect_closing() as conn:
        result = kb.dispatch_once(conn, dry_run=True, max_spawn=2)
        assert result.max_spawn_ceiling == 2
        assert result.max_spawn_resolution["source"] == "explicit"
        assert result.max_spawn_resolution["invalid"] is False

        spawned = [d for d in result.dispositions if d.outcome == "spawned"]
        held = [
            d for d in result.dispositions
            if d.outcome == "held" and d.reason == "max_spawn"
        ]
        assert len(spawned) == 2
        assert len(held) == 3


def test_dispatch_once_invalid_fail_closed_with_audit_event(kb, monkeypatch):
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _a: True)
    _seed_ready(kb, 18)

    def _spawn(task, workspace, board=None):
        return 424242

    with kb.connect_closing() as conn:
        result = kb.dispatch_once(conn, spawn_fn=_spawn, max_spawn="bogus")
        assert result.max_spawn_ceiling == 16
        assert result.max_spawn_resolution["invalid"] is True
        assert result.max_spawn_resolution["source"] == "fail_closed"
        assert result.max_spawn_resolution["raw"] == "bogus"
        assert result.max_spawn_audit_event_id is not None

        assert len(result.spawned) == 16
        held = [
            d for d in result.dispositions
            if d.outcome == "held" and d.reason == "max_spawn"
        ]
        assert len(held) == 2

        events = kb.list_events(conn, kb.DISPATCHER_AUDIT_TASK_ID)
        kinds = [e.kind for e in events]
        assert "max_spawn_invalid" in kinds
        audit = [e for e in events if e.kind == "max_spawn_invalid"][-1]
        assert audit.payload["fallback"] == 16
        assert audit.payload["resolved"] == 16
        assert audit.payload["raw"] == "bogus"
        assert audit.id == result.max_spawn_audit_event_id


def test_dispatch_once_invalid_fail_closed_dry_run_still_surfaces_resolution(kb, monkeypatch):
    """dry_run skips DB audit write but still fail-closes the ceiling to 16."""
    monkeypatch.setattr(kb, "_assignee_has_spawn_target", lambda _a: True)
    _seed_ready(kb, 18)

    with kb.connect_closing() as conn:
        result = kb.dispatch_once(conn, dry_run=True, max_spawn="nope")
        assert result.max_spawn_ceiling == 16
        assert result.max_spawn_resolution["invalid"] is True
        assert result.max_spawn_audit_event_id is None  # dry_run: no DB write
        spawned = [d for d in result.dispositions if d.outcome == "spawned"]
        held = [
            d for d in result.dispositions
            if d.outcome == "held" and d.reason == "max_spawn"
        ]
        assert len(spawned) == 16
        assert len(held) == 2
