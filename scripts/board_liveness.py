#!/usr/bin/env python3
"""Shared board-liveness oracle for fleet reapers.

On 2026-08-13 ``disk-watchdog.sh`` deleted three scratch trees whose cards
were not running:

* ``/tmp/wave-d4-item2-t7e073169``   card ``t_7e073169`` (ready)
* ``/tmp/cursor-verify-t19ba5cba``   card ``t_19ba5cba`` (blocked)
* ``/tmp/hermes-tce22-fd-reaper-main`` card ``t_ce22cf43`` (done; unique
  unpushed commit — the board cannot save a terminal card)

The previous oracles were a name denylist, ``/proc`` hold, and
``t_[a-f0-9]{8}`` scraped from live cmdlines. Queued work has no process.
Real directory names drop the underscore and truncate the hex
(``tce22`` for ``t_ce22cf43``).

This module is the single matcher every reaper must call. It:

* treats every status that is not terminal as live (do not assume the set —
  the live board on 2026-08-13 used blocked/ready/todo/review/triage/
  scheduled/running plus done/archived);
* matches ``t_<hex>`` and ``t<hex>`` tokens of length >= 4 by hex prefix;
* fails closed on an unreadable board for every *task-id-shaped* name, while
  still allowing genuine junk (no task-id token) to be collected.

A reaper that re-implements a guess instead of calling these functions is
the defect that caused the incident.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

# Terminal on the live fleet board 2026-08-13: done, archived.
# Extra aliases kept so a future status rename does not silently start
# protecting finished work — or deleting it. Unknown statuses are live.
DEFAULT_TERMINAL_STATUSES = frozenset({
    "done",
    "archived",
    "completed",
    "cancelled",
    "canceled",
})

# Observed non-terminal statuses on /var/lib/boardd/fleet/kanban.db
# at 2026-08-13 (counts): blocked 113, ready 44, todo 40, review 30,
# triage 22, scheduled 19, running 1. The helper does not hard-code this
# set as an allowlist — anything not terminal is live.
OBSERVED_NON_TERMINAL_STATUSES = (
    "blocked",
    "ready",
    "todo",
    "review",
    "triage",
    "scheduled",
    "running",
)

# ``t_ce22cf43`` and ``tce22`` both extract. Minimum 4 hex chars so a
# truncated directory token can still prefix-match the card id.
TASK_TOKEN_RE = re.compile(r"t_?([0-9a-f]{4,})", re.IGNORECASE)


class _Statused(Protocol):
    task_id: str
    status: str


def hex_of_task_id(task_id: str) -> str:
    """Return the lowercase hex body of ``t_<hex>`` / ``t<hex>`` / bare hex."""
    value = (task_id or "").strip().lower()
    if value.startswith("t_"):
        return value[2:]
    if (
        value.startswith("t")
        and len(value) > 1
        and all(char in "0123456789abcdef" for char in value[1:])
    ):
        return value[1:]
    return value


def extract_hex_tokens(name: str) -> tuple[str, ...]:
    """Hex bodies of every task-id-shaped token in *name*."""
    return tuple(match.group(1).lower() for match in TASK_TOKEN_RE.finditer(name or ""))


def is_task_id_shaped(name: str) -> bool:
    """True when *name* carries a ``t_?<hex>{4,}`` token.

    Used by the missing alarm: page when a reaper deletes anything matching.
    """
    return bool(extract_hex_tokens(name))


def is_live_status(
    status: str, *, terminal: Iterable[str] = DEFAULT_TERMINAL_STATUSES
) -> bool:
    return (status or "").strip().lower() not in {item.lower() for item in terminal}


def live_hexes(
    tasks: Iterable[_Statused],
    *,
    terminal: Iterable[str] = DEFAULT_TERMINAL_STATUSES,
) -> frozenset[str]:
    """Hex bodies of every non-terminal task id."""
    out: set[str] = set()
    for task in tasks:
        if not is_live_status(task.status, terminal=terminal):
            continue
        body = hex_of_task_id(task.task_id)
        if body:
            out.add(body)
    return frozenset(out)


def token_matches_live_hex(token_hex: str, live: Iterable[str]) -> bool:
    """True when *token_hex* prefixes (or equals) a live card hex body."""
    token = (token_hex or "").lower()
    if not token:
        return False
    for body in live:
        candidate = hex_of_task_id(body)
        if candidate.startswith(token):
            return True
    return False


def name_is_held(
    name: str,
    live: Iterable[str],
    *,
    board_ok: bool,
) -> bool:
    """Return True when a reaper must KEEP *name*.

    * ``board_ok=False`` (unreadable / empty / failed board read): every
      task-id-shaped name is held. Names with no token are not held, so
      genuine junk is still collectable. An unreadable board is not an
      empty board.
    * ``board_ok=True``: held only when a token prefixes a live hex.
    """
    tokens = extract_hex_tokens(name)
    if not tokens:
        return False
    if not board_ok:
        return True
    live_bodies = {hex_of_task_id(item) for item in live}
    return any(token_matches_live_hex(token, live_bodies) for token in tokens)
