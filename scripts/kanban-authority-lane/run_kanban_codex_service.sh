#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -lt 9 ]; then
  echo "usage: $0 TASK WT BRANCH REQ OUT ERR MODEL_TIMEOUT TOTAL_TIMEOUT VERIFY..." >&2
  exit 64
fi
TASK=$1; WT=$2; BRANCH=$3; REQ=$4; OUT=$5; ERR=$6; MODEL_TIMEOUT=$7; TOTAL_TIMEOUT=$8; shift 8
VERIFY_CMDS=("$@")
HERMES_HOME=${HERMES_HOME:-"$HOME/.hermes"}
STATE=${KANBAN_BRIDGE_STATE:-"$HERMES_HOME/scripts/kanban_bridge_state.py"}
BRIDGE=${KANBAN_BRIDGE_LABEL:-claude-acp-bridge-codex}
BRIDGE_BIN=${KANBAN_BRIDGE_BIN:-"$HOME/.local/bin/claude-acp-bridge-codex"}
ACP_VERIFY=${KANBAN_ACP_VERIFY:-"$HOME/.local/bin/acp-verify-result"}
ACPV="${OUT%.jsonl}-acp-verify.txt"
start_head=$(git -C "$WT" rev-parse HEAD 2>/dev/null || true)
hb=''
cleanup(){ [ -n "$hb" ] && kill "$hb" 2>/dev/null || true; }
on_term(){
  "$STATE" "$TASK" blocked --bridge "$BRIDGE" --worktree "$WT" --branch "$BRANCH" --result "durable service received TERM/INT; artifacts $OUT $ERR" --comment "✗ $BRIDGE service terminated; preserved worktree @ $WT" || true
  exit 143
}
trap cleanup EXIT
trap on_term TERM INT
"$STATE" "$TASK" running --bridge "$BRIDGE" --worktree "$WT" --branch "$BRANCH" --pid $$ --comment "→ dispatched to $BRIDGE @ $WT"
parent=$$
(while kill -0 "$parent" 2>/dev/null; do sleep 60; "$STATE" "$TASK" heartbeat --bridge "$BRIDGE" --worktree "$WT" --branch "$BRANCH" --pid "$parent" || true; done) >/dev/null 2>&1 &
hb=$!
cmd=("$BRIDGE_BIN" --acp --stdio)
for v in "${VERIFY_CMDS[@]}"; do cmd+=(--verify "$v"); done
set +e
CLAUDE_ACP_TIMEOUT="$MODEL_TIMEOUT" timeout "$TOTAL_TIMEOUT" "${cmd[@]}" < "$REQ" > "$OUT" 2> "$ERR"
rc=$?
"$ACP_VERIFY" "$OUT" > "$ACPV" 2>&1
vrc=$?
python3 - "$OUT" <<'PY'
import os,sys
s=open(sys.argv[1],errors='replace').read() if os.path.exists(sys.argv[1]) else ''
sys.exit(1 if 'verify_result: FAIL' in s else 0)
PY
failed=$?
branch_now=$(git -C "$WT" branch --show-current 2>/dev/null || true)
local_head=$(git -C "$WT" rev-parse HEAD 2>/dev/null || true)
clean=$(git -C "$WT" status --porcelain 2>/dev/null || true)
git -C "$WT" fetch origin "$BRANCH" >/dev/null 2>&1
frc=$?
remote_head=$(git -C "$WT" rev-parse FETCH_HEAD 2>/dev/null || true)
pr_url=$(cd "$WT" && gh pr list --head "$BRANCH" --state open --json url --jq '.[0].url // ""' 2>/dev/null || true)
set -e
if [ "$rc" -eq 0 ] && [ "$vrc" -eq 0 ] && [ "$failed" -eq 0 ] && [ "$frc" -eq 0 ] && [ "$branch_now" = "$BRANCH" ] && [ -n "$local_head" ] && [ "$local_head" != "$start_head" ] && [ "$local_head" = "$remote_head" ] && [ -z "$clean" ] && [ -n "$pr_url" ]; then
  "$STATE" "$TASK" review --bridge "$BRIDGE" --worktree "$WT" --branch "$BRANCH" --result "ACP trusted; PR=$pr_url local=$local_head remote=$remote_head" --comment "✓ $BRIDGE completed and verified @ ${local_head:0:12}; $pr_url → Review"
  exit 0
fi
reason="rc=$rc acp=$vrc verify_fail=$failed fetch=$frc branch=${branch_now:-none} start=${start_head:-none} local=${local_head:-none} remote=${remote_head:-none} dirty=$([ -n "$clean" ] && echo yes || echo no) pr=${pr_url:-none}; artifacts $OUT $ERR $ACPV"
"$STATE" "$TASK" blocked --bridge "$BRIDGE" --worktree "$WT" --branch "$BRANCH" --result "$reason" --comment "✗ $BRIDGE stopped without a reviewable PR; see $ERR and $ACPV"
exit 1
