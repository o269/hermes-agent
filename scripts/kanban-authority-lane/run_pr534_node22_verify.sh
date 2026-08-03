#!/usr/bin/env bash
set -euo pipefail
HERMES_HOME=${HERMES_HOME:-"$HOME/.hermes"}
STATE=${KANBAN_BRIDGE_STATE:-"$HERMES_HOME/scripts/kanban_bridge_state.py"}
if [ -z "${KANBAN_TASK:-}" ]; then
 printf 'missing required KANBAN_TASK\n' >&2
 exit 64
fi
TASK=$KANBAN_TASK
WT=${KANBAN_WORKTREE:-/tmp/omnia-pr534-verify}
BRANCH=${KANBAN_BRANCH:-security/rm-auth-dbauth-t3d9e8eb3}
PATH=${KANBAN_VERIFY_PATH:-"$HOME/.nvm/versions/node/v22.23.1/bin:/usr/local/bin:/usr/bin:/bin"}
export PATH
LOG=${KANBAN_VERIFY_LOG:-/tmp/pr534-node22-independent-verify.log}
ACP_VERIFY=${KANBAN_ACP_VERIFY:-"$HOME/.local/bin/acp-verify-result"}
ACP_OUTPUT=${KANBAN_ACP_OUTPUT:-/tmp/cfr-auth-dbauth-kimi-r2-output.jsonl}
PR_JSON=${KANBAN_PR_JSON:-/tmp/pr534-current.json}
PR_CHECKS=${KANBAN_PR_CHECKS:-/tmp/pr534-current-checks.txt}
"$STATE" "$TASK" running --bridge kimi-cli-acp-bridge+orchestrator-node22-verification --worktree "$WT" --branch "$BRANCH" --pid $$ --comment "Kimi PR #534 authored; running fresh-clone Node 22 independent verification."
: > "$LOG"
set +e
(cd "$WT" && timeout 600 corepack pnpm install --frozen-lockfile) >> "$LOG" 2>&1; install=$?
(cd "$WT" && timeout 600 corepack pnpm --filter @omnia/api exec vitest run src/lib/request-database-authority.test.ts src/lib/tenant-security-kernel.test.ts src/lib/db-public-boundary.test.ts src/lib/supabase-boundary.test.ts src/__tests__/live-tenant-authority.test.ts src/__tests__/rls-tenant-filter-guard.test.ts) >> "$LOG" 2>&1; focused=$?
(cd "$WT" && timeout 600 node scripts/biome-changed.mjs && timeout 600 corepack pnpm check-types) >> "$LOG" 2>&1; static=$?
(cd "$WT" && timeout 600 corepack pnpm check:rls-guard && timeout 600 corepack pnpm check:ambient-db-admin && timeout 600 corepack pnpm test:ambient-db-admin-lint) >> "$LOG" 2>&1; guards=$?
(cd "$WT" && timeout 600 corepack pnpm --filter @omnia/api run build) >> "$LOG" 2>&1; build=$?
(cd "$WT" && git diff --check && test -z "$(git status --porcelain)" && test -z "$(git diff origin/main...HEAD -- .rls-admin-allowlist)") >> "$LOG" 2>&1; clean=$?
local=$(git -C "$WT" rev-parse HEAD 2>/dev/null || true)
remote=$(git -C "$WT" ls-remote origin refs/heads/"$BRANCH" | cut -f1)
"$ACP_VERIFY" "$ACP_OUTPUT" >> "$LOG" 2>&1; acp=$?
gh pr view 534 --repo o269/omnia --json url,state,isDraft,mergeable,mergeStateStatus,headRefOid,statusCheckRollup > "$PR_JSON" 2>>"$LOG"; pr=$?
gh pr checks 534 --repo o269/omnia > "$PR_CHECKS" 2>>"$LOG"; checks=$?
set -e
summary="install=$install focused=$focused static=$static guards=$guards build=$build clean=$clean acp=$acp pr=$pr checks=$checks local=${local:0:12} remote=${remote:0:12}"
if [ "$install" -eq 0 ] && [ "$focused" -eq 0 ] && [ "$static" -eq 0 ] && [ "$guards" -eq 0 ] && [ "$build" -eq 0 ] && [ "$clean" -eq 0 ] && [ -n "$local" ] && [ "$local" = "$remote" ] && [ "$acp" -eq 0 ] && [ "$pr" -eq 0 ] && [ "$checks" -eq 0 ]; then
 "$STATE" "$TASK" review --bridge kimi-cli-acp-bridge+orchestrator-node22-verification --worktree "$WT" --branch "$BRANCH" --result "PR #534 remote-equal $local; Node 22 independent focused/static/guard/build gates green. A2-07 composite-FK follow-up remains deferred." --comment "✓ Kimi PR #534 independently verified and CI green → Review; A2-07 remains separate follow-up."
 exit 0
fi
"$STATE" "$TASK" blocked --bridge kimi-cli-acp-bridge+orchestrator-node22-verification --worktree "$WT" --branch "$BRANCH" --result "PR #534 not review-ready: $summary. Current CI includes migration drift and RLS/API integration red; A2-07 deferred. Logs: $LOG $PR_CHECKS" --comment "✗ PR #534 remains blocked on exact verification/CI gates; no implementation loss."
exit 1
