#!/usr/bin/env bash
set -euo pipefail
HERMES_HOME=${HERMES_HOME:-"$HOME/.hermes"}
STATE=${KANBAN_BRIDGE_STATE:-"$HERMES_HOME/scripts/kanban_bridge_state.py"}
if [ -z "${KANBAN_TASK:-}" ]; then
 printf 'missing required KANBAN_TASK\n' >&2
 exit 64
fi
TASK=$KANBAN_TASK
WT=${KANBAN_WORKTREE:-/tmp/cfr-estimates-ef2-cursor}
BRANCH=${KANBAN_BRANCH:-fix/rm-estimates-ef2-t-ac57bb0f-v2}
LOG=${KANBAN_VERIFY_LOG:-/tmp/cfr-estimates-ef2-post-cursor-verify.log}
ACP_VERIFY=${KANBAN_ACP_VERIFY:-"$HOME/.local/bin/acp-verify-result"}
ACP_OUTPUT=${KANBAN_ACP_OUTPUT:-/tmp/cfr-estimates-ef2-cursor-output.jsonl}
"$STATE" "$TASK" running --bridge orchestrator-direct:post-cursor-verification --worktree "$WT" --branch "$BRANCH" --pid $$ --comment "Cursor authoring complete in PR #175; running corrected npm-based independent verification."
: > "$LOG"
set +e
(cd "$WT" && timeout 300 npm ci) >> "$LOG" 2>&1; ci=$?
(cd "$WT" && timeout 300 npm test) >> "$LOG" 2>&1; tests=$?
(cd "$WT" && timeout 300 npm run build:widget) >> "$LOG" 2>&1; widget=$?
(cd "$WT" && timeout 300 npx tsc -b && timeout 300 npx vite build) >> "$LOG" 2>&1; build=$?
(cd "$WT" && git diff --check && test -z "$(git status --porcelain)") >> "$LOG" 2>&1; clean=$?
git -C "$WT" fetch origin "$BRANCH" >> "$LOG" 2>&1; fetch=$?
local=$(git -C "$WT" rev-parse HEAD 2>/dev/null || true); remote=$(git -C "$WT" rev-parse FETCH_HEAD 2>/dev/null || true)
"$ACP_VERIFY" "$ACP_OUTPUT" >> "$LOG" 2>&1; av=$?
gh pr view 175 --repo o269/omnia-v2 --json url,state,isDraft,mergeable,mergeStateStatus,headRefOid,statusCheckRollup >> "$LOG" 2>&1; prc=$?
set -e
if [ "$ci" -eq 0 ] && [ "$tests" -eq 0 ] && [ "$widget" -eq 0 ] && [ "$build" -eq 0 ] && [ "$clean" -eq 0 ] && [ "$fetch" -eq 0 ] && [ -n "$local" ] && [ "$local" = "$remote" ] && [ "$av" -eq 0 ] && [ "$prc" -eq 0 ]; then
 "$STATE" "$TASK" review --bridge cursor-agent-acp-bridge+orchestrator-verification --worktree "$WT" --branch "$BRANCH" --result "PR #175 remote-equal at $local; Cursor ACP trusted; npm ci/test (307)/widget build/tsc+vite build independently green." --comment "✓ Cursor PR #175 independently verified with correct npm toolchain; remote-equal ${local:0:12} → Review."
 exit 0
fi
"$STATE" "$TASK" blocked --bridge cursor-agent-acp-bridge+orchestrator-verification --worktree "$WT" --branch "$BRANCH" --result "post-Cursor verify failed ci=$ci tests=$tests widget=$widget build=$build clean=$clean fetch=$fetch acp=$av pr=$prc; log $LOG" --comment "✗ PR #175 independent verification failed; see $LOG"
exit 1
