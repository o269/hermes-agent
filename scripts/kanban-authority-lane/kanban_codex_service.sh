#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME=${HERMES_HOME:-"$HOME/.hermes"}
STATE=${KANBAN_BRIDGE_STATE:-"$HERMES_HOME/scripts/kanban_bridge_state.py"}
BRIDGE_LABEL=${KANBAN_BRIDGE_LABEL:-claude-acp-bridge-codex}
BRIDGE_BIN=${KANBAN_BRIDGE_BIN:-"$HOME/.local/bin/claude-acp-bridge-codex"}
ACP_VERIFY=${KANBAN_ACP_VERIFY:-"$HOME/.local/bin/acp-verify-result"}
TASK= WT= BRANCH= REQ= OUT= ERR= REPO= EXISTING_PR=
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
    --existing-pr) EXISTING_PR=$2; shift 2 ;;
    --model-timeout) MODEL_TIMEOUT=$2; shift 2 ;;
    --verify) VERIFY_CMDS+=("$2"); shift 2 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
for v in TASK WT BRANCH REQ OUT ERR REPO; do
  if [ -z "${!v}" ]; then printf 'missing --%s\n' "${v,,}" >&2; exit 2; fi
done
VERIFY_FILE="${OUT%.jsonl}-acp-verify.txt"
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
args=("$BRIDGE_BIN" --acp --stdio)
for cmd in "${VERIFY_CMDS[@]}"; do args+=(--verify "$cmd"); done
outer_timeout=$((MODEL_TIMEOUT + 600 * ${#VERIFY_CMDS[@]} + 180))
set +e
CLAUDE_ACP_TIMEOUT="$MODEL_TIMEOUT" timeout "$outer_timeout" "${args[@]}" < "$REQ" > "$OUT" 2> "$ERR"
rc=$?
"$ACP_VERIFY" "$OUT" > "$VERIFY_FILE" 2>&1
vrc=$?
python3 - "$OUT" <<'PY'
import os,sys
s=open(sys.argv[1],errors='replace').read() if os.path.exists(sys.argv[1]) else ''
sys.exit(1 if 'verify_result: FAIL' in s else 0)
PY
failed_verify=$?
git -C "$WT" fetch origin "$BRANCH" >/dev/null 2>&1
frc=$?
local_head=$(git -C "$WT" rev-parse HEAD 2>/dev/null || true)
remote_head=$(git -C "$WT" rev-parse FETCH_HEAD 2>/dev/null || true)
clean=$(git -C "$WT" status --porcelain 2>/dev/null || true)
if [ -n "$EXISTING_PR" ]; then
  pr_url=$(gh pr view "$EXISTING_PR" --repo "$REPO" --json url --jq .url 2>/dev/null || true)
else
  pr_url=$(gh pr list --repo "$REPO" --head "$BRANCH" --state open --json url --jq '.[0].url // ""' 2>/dev/null || true)
fi
set -e

if [ "$rc" -eq 0 ] && [ "$vrc" -eq 0 ] && [ "$failed_verify" -eq 0 ] && [ "$frc" -eq 0 ] && [ -n "$local_head" ] && [ "$local_head" != "$start_head" ] && [ "$local_head" = "$remote_head" ] && [ -z "$clean" ] && [ -n "$pr_url" ]; then
  "$STATE" "$TASK" review --bridge "$BRIDGE_LABEL" --worktree "$WT" --branch "$BRANCH" --result "ACP trusted; PR=$pr_url local=$local_head remote=$remote_head" --comment "✓ $BRIDGE_LABEL completed and verified @ ${local_head:0:12}; $pr_url → Review"
  exit 0
fi
reason="bridge/verification failure rc=$rc acp=$vrc verify_fail=$failed_verify fetch=$frc start=$start_head local=${local_head:-none} remote=${remote_head:-none} dirty=$([ -n "$clean" ] && echo yes || echo no) pr=${pr_url:-none}; artifacts $OUT $ERR $VERIFY_FILE"
"$STATE" "$TASK" blocked --bridge "$BRIDGE_LABEL" --worktree "$WT" --branch "$BRANCH" --result "$reason" --comment "✗ $BRIDGE_LABEL stopped without a reviewable remote-equal PR; see $ERR and $VERIFY_FILE"
exit 1
