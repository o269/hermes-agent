"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect
import logging

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_embedded_dispatcher_logs_guard_reason_pr_and_expiry(caplog):
    from gateway.kanban_watchers import _log_respawn_guard_results
    from hermes_cli import kanban_db as kb

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

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        _log_respawn_guard_results("fleet", result)

    assert (
        "kanban dispatcher [fleet]: SKIP t_owned respawn_guarded=active_pr "
        "pr=https://github.com/o269/hermes-agent/pull/8 "
        "expires=1785660000 "
        "pr=https://github.com/o269/hermes-agent/pull/9 "
        "expires=1785660300 phase=ready"
    ) in caplog.messages
    assert (
        "kanban dispatcher [fleet]: SKIP t_recent "
        "respawn_guarded=recent_success expires=1785660600 phase=ready"
    ) in caplog.messages


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)
