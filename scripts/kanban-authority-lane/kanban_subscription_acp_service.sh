#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME=${HERMES_HOME:-"$HOME/.hermes"}
STATE=${KANBAN_BRIDGE_STATE:-"$HERMES_HOME/scripts/kanban_bridge_state.py"}
ACP_VERIFY=${KANBAN_ACP_VERIFY:-"$HOME/.local/bin/acp-verify-result"}
TASK= WT= BRANCH= REQ= OUT= ERR= REPO= BRIDGE_LABEL= BRIDGE_BIN= TIMEOUT_ENV=
EXISTING_PR=
MODEL_TIMEOUT=1800
VERIFY_CMDS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --task) TASK=$2; shift 2 ;;
    --worktree) WT=$2; shift 2 ;;
    --branch) BRANCH=$2; shift 2 ;;
    --request) REQ=$2; shift 2 ;;
    --output) OUT=$2; shift 2 ;;
    --error) ERR=$2; shift 2 ;;
    --repo) REPO=$2; shift 2 ;;
    --bridge-label) BRIDGE_LABEL=$2; shift 2 ;;
    --bridge-bin) BRIDGE_BIN=$2; shift 2 ;;
    --timeout-env) TIMEOUT_ENV=$2; shift 2 ;;
    --existing-pr) EXISTING_PR=$2; shift 2 ;;
    --model-timeout) MODEL_TIMEOUT=$2; shift 2 ;;
    --verify) VERIFY_CMDS+=("$2"); shift 2 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
for v in TASK WT BRANCH REQ OUT ERR REPO BRIDGE_LABEL BRIDGE_BIN TIMEOUT_ENV; do
  if [ -z "${!v}" ]; then printf 'missing required %s\n' "$v" >&2; exit 2; fi
done
VERIFY_FILE="${OUT%.jsonl}-acp-verify.txt"
DIRECT_LOG="${OUT%.jsonl}-direct-verify.log"
start_head=$(git -C "$WT" rev-parse HEAD 2>/dev/null || true)
if [ -z "$start_head" ]; then printf 'not a git worktree: %s\n' "$WT" >&2; exit 2; fi

"$STATE" "$TASK" running --bridge "$BRIDGE_LABEL" --worktree "$WT" --branch "$BRANCH" --pid $$ --comment "→ dispatched to $BRIDGE_LABEL @ $WT"
parent=$$
(while kill -0 "$parent" 2>/dev/null; do sleep 60; "$STATE" "$TASK" heartbeat --bridge "$BRIDGE_LABEL" --worktree "$WT" --branch "$BRANCH" --pid "$parent" || true; done) >/dev/null 2>&1 &
hb=$!
cleanup() { kill "$hb" 2>/dev/null || true; }
on_term() {
  "$STATE" "$TASK" blocked --bridge "$BRIDGE_LABEL" --worktree "$WT" --branch "$BRANCH" --result "durable ACP service received termination signal; inspect $ERR" --comment "✗ $BRIDGE_LABEL service terminated; preserved worktree @ $WT; see $ERR" || true
  exit 143
}
trap cleanup EXIT
trap on_term TERM INT

git -C "$WT" fetch origin main:refs/remotes/origin/main >/dev/null 2>&1 || true
set +e
env "$TIMEOUT_ENV=$MODEL_TIMEOUT" timeout $((MODEL_TIMEOUT + 60)) "$BRIDGE_BIN" < "$REQ" > "$OUT" 2> "$ERR"
rc=$?
: > "$DIRECT_LOG"
verify_rc=0
for cmd in "${VERIFY_CMDS[@]}"; do
  printf '\n===== VERIFY: %s =====\n' "$cmd" >> "$DIRECT_LOG"
  timeout 600 bash -lc "cd \"$WT\" && $cmd" >> "$DIRECT_LOG" 2>&1
  one=$?
  printf '\n===== EXIT: %s =====\n' "$one" >> "$DIRECT_LOG"
  if [ "$one" -ne 0 ]; then verify_rc=$one; fi
done
local_head=$(git -C "$WT" rev-parse HEAD 2>/dev/null || true)
branch_now=$(git -C "$WT" branch --show-current 2>/dev/null || true)
clean=$(git -C "$WT" status --porcelain 2>/dev/null || true)
git -C "$WT" fetch origin "$BRANCH" >/dev/null 2>&1
frc=$?
remote_head=$(git -C "$WT" rev-parse FETCH_HEAD 2>/dev/null || true)
if [ "$verify_rc" -eq 0 ]; then verdict=PASS; else verdict="FAIL(exit-$verify_rc)"; fi
{
  printf '\n===== BRIDGE GROUND TRUTH (orchestrator-collected, not model-claimed) =====\n'
  printf 'exit_code: %s\n' "$rc"
  printf 'cwd: %s\n' "$WT"
  printf 'start_head: %s\n' "$start_head"
  printf 'head_after: %s\n' "$local_head"
  printf 'branch_after: %s\n' "$branch_now"
  printf 'commits_made_during_session: %s\n' "$(git -C "$WT" rev-list --count "$start_head..$local_head" 2>/dev/null || echo 0)"
  for cmd in "${VERIFY_CMDS[@]}"; do printf 'verify_cmd: %s\n' "$cmd"; done
  printf 'verify_result: %s\n' "$verdict"
  printf 'remote_head: %s\n' "$remote_head"
  printf '===== END GROUND TRUTH =====\n'
} >> "$OUT"
"$ACP_VERIFY" "$OUT" > "$VERIFY_FILE" 2>&1
vrc=$?
if [ -n "$EXISTING_PR" ]; then
  pr_url=$(gh pr view "$EXISTING_PR" --repo "$REPO" --json url --jq .url 2>/dev/null || true)
else
  pr_url=$(gh pr list --repo "$REPO" --head "$BRANCH" --state open --json url --jq '.[0].url // ""' 2>/dev/null || true)
fi
set -e

if [ "$rc" -eq 0 ] && [ "$verify_rc" -eq 0 ] && [ "$vrc" -eq 0 ] && [ "$frc" -eq 0 ] && [ -n "$local_head" ] && [ "$local_head" != "$start_head" ] && [ "$local_head" = "$remote_head" ] && [ -z "$clean" ] && [ -n "$pr_url" ]; then
  "$STATE" "$TASK" review --bridge "$BRIDGE_LABEL" --worktree "$WT" --branch "$BRANCH" --result "ACP trusted; PR=$pr_url local=$local_head remote=$remote_head" --comment "✓ $BRIDGE_LABEL completed and verified @ ${local_head:0:12}; $pr_url → Review"
  exit 0
fi
reason="bridge/verification failure rc=$rc direct=$verify_rc acp=$vrc fetch=$frc start=$start_head local=${local_head:-none} remote=${remote_head:-none} dirty=$([ -n "$clean" ] && echo yes || echo no) pr=${pr_url:-none}; artifacts $OUT $ERR $VERIFY_FILE $DIRECT_LOG"
"$STATE" "$TASK" blocked --bridge "$BRIDGE_LABEL" --worktree "$WT" --branch "$BRANCH" --result "$reason" --comment "✗ $BRIDGE_LABEL stopped without a reviewable remote-equal PR; see $ERR, $VERIFY_FILE, and $DIRECT_LOG"
exit 1
