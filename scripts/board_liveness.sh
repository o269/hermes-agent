#!/usr/bin/env bash
# Shared board-liveness oracle for shell reapers (disk-watchdog, tmp-reaper,
# workspace-reaper, tmp-build-gc). Source this file; do not re-implement.
#
# Contract (must match scripts/board_liveness.py):
#   - live  = status NOT IN (done, archived, completed, cancelled, canceled)
#   - tokens = t_?<hex>{4,}  (underscore optional, truncated names match by prefix)
#   - unreadable board => hold every task-id-shaped name; junk still collectable
#
# Usage:
#   source /usr/local/lib/board_liveness.sh   # after Fable installs
#   board_liveness_load "$BOARD_DB"           # sets BOARD_LIVENESS_OK / BOARD_LIVENESS_HEX
#   board_liveness_holds "$basename" && keep
#
# Never run a reaper --apply from this file.
set -u

BOARD_LIVENESS_OK=0
BOARD_LIVENESS_HEX=""

board_liveness_is_shaped() {
  local name="$1"
  grep -qE 't_?[0-9a-f]{4,}' <<<"$name"
}

board_liveness_load() {
  local db="${1:-}"
  BOARD_LIVENESS_OK=0
  BOARD_LIVENESS_HEX=""
  if [[ -z "$db" ]]; then
    return 1
  fi
  if [[ ! -r "$db" ]] && ! sudo -n test -r "$db" 2>/dev/null; then
    return 1
  fi
  local raw
  raw=$(sudo -n sqlite3 -readonly "$db" \
    "SELECT id FROM tasks WHERE lower(status) NOT IN ('done','archived','completed','cancelled','canceled');" \
    2>/dev/null | grep -oE '[0-9a-f]{4,}' | sort -u) || raw=""
  if [[ -z "$raw" ]]; then
    # Readable file + zero rows is indistinguishable from a failed query.
    # Fail closed: caller must treat this as board-unavailable.
    return 1
  fi
  BOARD_LIVENESS_HEX="$raw"
  BOARD_LIVENESS_OK=1
  return 0
}

# Return 0 (held / KEEP) when $1 must not be deleted.
board_liveness_holds() {
  local name="$1" tok hex
  local shaped=0
  for tok in $(grep -oE 't_?[0-9a-f]{4,}' <<<"$name" 2>/dev/null); do
    shaped=1
    hex=${tok#t}
    hex=${hex#_}
    [[ -z "$hex" ]] && continue
    if [[ "$BOARD_LIVENESS_OK" != "1" ]]; then
      return 0
    fi
    if grep -qE "^${hex}" <<<"$BOARD_LIVENESS_HEX" 2>/dev/null; then
      return 0
    fi
  done
  if [[ "$shaped" -eq 1 && "$BOARD_LIVENESS_OK" != "1" ]]; then
    return 0
  fi
  return 1
}
