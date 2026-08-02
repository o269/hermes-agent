# Canonical PR Rework and Respawn-Guard Recovery

Use this procedure when a Kanban worker opened the canonical PR, marked the task complete/reviewable, but exact-head CI is red or an authoring-route boundary was violated.

## Authority order

1. Live repository/PR state at the exact full SHA: local `HEAD`, remote branch, PR `headRefOid`, required check conclusions, merge state.
2. Current task row, run, claim, PID/process tree, heartbeat, and native/bridge session evidence.
3. Task result, comments, worker summary, watcher output, and handoff packets.

A worker calling `kanban_complete`, a task showing `done`, or model prose saying “ready” never overrides red exact-head CI. Do not move to Review, submit a land request, merge, or deploy while any required check is pending, skipped unexpectedly, canceled, or failed.

## Canonical-PR repair invariant

- Prefer the existing card, branch, and PR. Never create a duplicate branch or PR merely to escape scheduler friction, and do not create generic `-v2` repair cards as the first response.
- **Narrow custodian exception:** if the original card is non-dispatchable because `active_pr` recovery has no trusted override (or the override implementation itself fails security review), the sole custodian may create one explicit **continuation card** on the SAME branch and PR. First prove zero live writers; bind the continuation to the exact residual findings and head; structurally park the original card behind/dependent on that continuation; make only the continuation eligible; and reconcile both cards when the canonical PR reaches a terminal outcome. This is scheduler recovery, not a second implementation or duplicate PR.
- Before repair, capture the full head SHA, failed check names/job URLs, changed paths, current assignee/run, and whether any live writer already owns the workspace.
- Classify each failure by the standing provider split. If a worker crosses the split (for example, a backend lane authors SQL), freeze before commit, prove the remote/PR head stayed unchanged, preserve valid WIP, and transfer custody to the correct native provider.
- After every push, invalidate prior receipts and re-run exact-head equality and CI checks.

## Hermes dispatcher semantics that matter

### `--max` is a live concurrency ceiling

Existing `running` cards count against `hermes kanban dispatch --max N`. If `R` cards are running and one more lane is authorized, dispatch with at least `--max R+1`. `spawned: []` requires diagnosis; it is not a successful launch.

### `recent_success` versus `active_pr`

`check_respawn_guard` can suppress a ready task for different reasons:

- `recent_success`: an explicit post-completion `status`, `promoted`, `unblocked`, or `reclaimed` event can authorize a rerun.
- `active_pr`: a recent task **comment** containing a GitHub PR URL can suppress respawn even after an explicit requeue. In the observed implementation the window was 24 hours and there was no CLI override.

Preventive practice while a PR is still under active repair:

- Refer to it in task comments as `owner/repo#N` rather than pasting a literal GitHub URL.
- Put the clickable URL in a structured PR field or task body when available; verify the current implementation before relying on this because guard behavior can change.
- Add the literal URL to the terminal handoff only after no further worker respawn is expected.

Never create a duplicate PR merely because `active_pr` blocks a repair worker. Prefer the original card; use the narrowly controlled same-PR continuation-card exception above only when the trusted override path is unavailable or unsafe.

### An `active_pr` override is an authorization boundary

Treat continuation authorization like a capability, not a convenience flag:

- Never trust `HERMES_PROFILE_NAME`, caller-supplied `authorized_by`, a worker-authored comment, or an unverified event-author string as approval. A lane must not be able to self-authorize its own scheduler bypass.
- Approval must pre-exist the worker launch and come from a board-authenticated operator/custodian identity whose evidence author is mechanically validated. If the runtime cannot prove that identity, fail closed.
- Bind one authorization to exact `task_id + owner/repo#N + full head SHA + destination profile`, plus a short expiry/nonce. For multi-PR work, require every PR/head pair.
- Consume it atomically with the claim using one compare-and-set. Clear it on successful claim; reject replay, stale head, wrong profile, other card, expired approval, current run/claim, or any live competing writer.
- Prove negative cases: spoofed profile/author, mismatched head, expired/replayed approval, other card, and live-writer collision must all remain guarded. Prove the positive case dispatches exactly once through the normal gateway and produces a real native/ACP session receipt.
- A security FIX-REQUIRED verdict on the override means the override is not installed, regardless of passing unit tests or sound CAS mechanics. Keep the canonical PR on hold and use custodian-controlled continuation recovery instead of silently hand-launching a worker.

### Manual native launches can create a stale claim lock

Do not assume that chaining `hermes kanban claim <task> && exec hermes ...` creates a durable worker registration. The `claim` CLI may record the PID of its short-lived claim subprocess, not the long-lived agent process. The gateway can then reclaim the supposedly dead lock and move/block/triage the card while the unregistered agent continues running.

Before any manual active-PR recovery:

1. Re-read the full task graph immediately before launch, including newly created child tasks, current runs, worker PIDs, claims, session lists, and exact PR/branch ownership. A parent card can look idle while a hidden repair child already owns the same PR.
2. Prefer a dispatcher launch after a guarded, auditable `active_pr` override over a shell-level manual claim.
3. If a manual native process is unavoidable, register the real worker PID, run, claim lock, and native session ID atomically before the next gateway tick. Use the guarded SQLite procedure only when no supported CLI registration command exists; back up first and require exact task/run/process preconditions.
4. If another live writer is discovered, stop the later process immediately, verify the PR head did not move, preserve the earlier writer as sole owner, add a structural child→parent dependency, and repair any loop-induced `triage` state with an audited transaction.

A worker that calls `kanban_block` after detecting a one-writer collision has done the right thing, but blocking the board run does not necessarily terminate the external agent process. Verify and terminate the duplicate process explicitly.

### Board-race + zero-child broker is ambiguous

A card can become Blocked while its Hermes broker PID remains alive with zero provider children because the board dispatcher, watchdog, and worker raced. Do not equate any single signal with a safe reclaim:

- `Blocked` does not prove the writer stopped.
- An alive broker PID does not prove provider work is active.
- Zero children does not prove no artifacts were produced or that the broker cannot resume.

Preserve the sole worktree. The watchdog/sole custodian—not the operator-chat seat—must inspect session/API-call age, child history, broker logs, worktree status/diff, artifact timestamps, run/claim state, and exact PR head. Then choose exactly one path: reconcile valid artifacts after a clean exit, or terminate the canonical process group, prove zero surviving writers/children, preserve the worktree/diff, and re-dispatch the same canonical continuation. Never unblock or start a second writer while ownership is unresolved.

### Spawnable native profile is not the same as a logical assignee label

Before dispatch, inspect the board's live assignee/profile inventory. A label such as `backend` may be meaningful in prose but nonspawnable because no corresponding Hermes profile exists on disk.

- Reassign the existing card to the real on-disk profile pinned to the required native provider; do not invent a replacement card or lane.
- Verify the resulting process command, native session ID, model, billing provider, and billing mode. A claim event's model field is insufficient.
- Put this in the task body **before** dispatch when inherited orchestration rules could confuse the worker: `This spawned process is the subscription executor; author directly in this workspace; do not delegate, spawn a nested agent, or use ACP.` Late comments may never be read.
- Record native session IDs in a card comment immediately after liveness verification. Session databases may compact older rows before final land-submit reconciliation; the board comment is the durable telemetry receipt.

## Status-recovery pitfalls

- Read back every status mutation. Some helpers append a comment even when the requested transition is rejected.
- A completed task may not support `done -> ready` through the CLI. `block`/`unblock` can no-op on `done` while still emitting comments.
- Repeated `block` cycles can trip `block_recurrences` and move the card to `triage`.
- `promote` may support only `todo`/`blocked`, not `triage`; do not trust ambiguous help text over command read-back.
- Do not repeatedly try guessed verbs or columns. Diagnose the live implementation once.

### Moving a technically green card to Review

Do not mark a land-ready implementation `Done`; the sole lander still owns merge/deploy and any live receipt.

1. Prefer the canonical seat wrapper when available: `kb review <seat> <task>`.
2. If the raw `hermes kanban` CLI has no `review` verb, use the source-controlled broker helper rather than direct SQLite. Omit `--assignee`: the helper preserves the current executor and rejects executor-shaped work that is already misassigned to Fable.
   ```bash
   python ~/.hermes/scripts/kanban_bridge_state.py <task> review \
     --board fleet --bridge '<actual native or ACP executor>' \
     --worktree '<path>' --branch '<branch>' \
     --comment '<exact-head gate receipt>'
   ```
3. A distinct Fable authority card must be explicit. Every Fable assignment requires a bracketed authority action; create it through the same guarded helper. Executor-shaped and neutral titles without that action fail closed:
   ```bash
   python ~/.hermes/scripts/kanban_bridge_state.py create \
     --title '[FABLE][LAND] owner/repo#N exact-head gate' \
     --body '<immutable head/check receipts and action-time predicate>' \
     --assignee fable --status blocked
   ```
   Allowed authority actions include land, apply, operator, acceptance, approval, install/deploy, cutover/release, supersession/recovery, and decision. Classification happens before assignment: executable markers win, so an authority marker cannot convert `[AUTHOR]`, `[FIX]`, `[REVIEW]`, implementation, migration, test, or verification work into Fable custody. Mixed executor/authority cards fail closed on explicit Fable assignment and on an implicit Review transition; split the authority action into a distinct card or route the executor explicitly. Pure Review-only transitions still preserve the current executor. A bare `[GATE]` or `[FABLE]` marker is never sufficient authority.
4. Generic Codex/ACP/Kimi/Cursor wrappers must also omit `--assignee` on running, blocked, and review transitions. They preserve the card's executor lane; they never hand an implementation card to Fable implicitly.
5. Re-read status, assignee, PID, current run, claim, body, and result. A Review transition may intentionally leave `tasks.result` empty; put the immutable exact-head receipt in the body/comment instead of marking the task Done merely to populate `result`.
6. Only use the guarded SQLite fallback if both wrapper and helper lack the required transition.

### Installing the authority-lane recurrence fence

The helper, assignment policy, generic wrappers, and this reference are source-controlled under `scripts/kanban-authority-lane/`. Fable is the sole installer. Authors and reviewers may prove the package without touching live paths:

```bash
python3 scripts/kanban-authority-lane/install.py --dry-run \
  --hermes-home /path/to/staged-hermes-home
python3 scripts/kanban-authority-lane/install.py --check \
  --hermes-home /path/to/staged-hermes-home
```

After landing, Fable runs the installer against the intended Hermes home, records the JSON SHA-256 receipts, performs `--check`, and proves one natural transition by broker readback. Never hand-edit `~/.hermes/scripts` or this installed reference as the recurrence fix.

## Guarded SQLite fallback

Use only when the canonical CLI has no transition/override for the required recovery and the operator decision is already explicit.

1. Take a consistent SQLite backup.
2. Inspect `PRAGMA database_list`, `PRAGMA table_info(tasks)`, and schemas for runs/events/comments. Never assume fields such as `block_reason`, `scheduled_at`, `pr_url`, or `worker_pid` exist.
3. Re-read the exact task and require all preconditions: expected status, no claim lock, no current run, no live/non-zombie worker, expected assignee/workspace/branch/PR head.
4. Use one `BEGIN IMMEDIATE` transaction with an exact guarded `UPDATE`.
5. Mutate only proven columns needed for recovery. Clear stale completion/failure fields only when semantically required.
6. Require a pre-existing explicit operator/custodian approval whose identity is mechanically validated by the board authority boundary. Do not derive authority from environment variables, caller-supplied payload fields, or a worker-authored comment. The sole custodian performs the transaction and appends an auditable event naming the validated actor, old/new state, reason, exact PR/head, and why the CLI was insufficient.
7. Commit, then re-read task, latest runs/events/comments, and dispatcher output.
8. If bypassing `active_pr`, preserve the semantic PR receipt. Prefer replacing only the literal URL in the single triggering comment with `owner/repo#N`; record a dedicated, custodian-authored override event bound to the exact task/PR/head. Do not delete history silently, and do not let the task lane perform this mutation for itself.

A transaction that references a nonexistent column should roll back. Inspect the schema and retry once with proven fields; do not cargo-cult columns from another board version.

## Liveness and executor provenance

For a native worker, require all of:

- actual `hermes -p <profile> ... work kanban task <id>` process;
- `kill -0`, non-`Z` state, positive/advancing CPU;
- task/current-run/claim agreement and fresh heartbeat;
- profile session record with the real model, `billing_provider`, and billing mode.

Do not trust the model shown only in a `claimed` event: task-level overrides or dispatcher metadata can be stale while the heartbeat/session correctly shows the actual provider. A PID in `Z` state is dead even when `kill -0` succeeds.

## Safe provider-boundary stop

When forbidden authoring appears in the diff:

1. Freeze the writer immediately (`SIGSTOP`) before commit/push.
2. Verify dirty paths, commit count, local/remote/PR head, and the exact boundary violation.
3. Terminate cleanly (`SIGTERM`, then `SIGCONT` so a stopped process can exit); use `SIGKILL` only if needed.
4. Treat `Z` as terminated, not live.
5. Reclaim the run, update card custody/body, resolve any recurrence/triage state, and launch exactly one correctly routed provider.
6. Verify the new native/bridge executor trace before calling the lane live.

## Iterative exact-head CI repair

A required failure remains actionable even when it reproduces on the immediately preceding PR commit. Parent-head reproduction isolates which phase introduced the failure; it does **not** waive the failure when that parent is still part of the same PR.

1. Extract failed-job logs and name the exact tests/gates before redispatching.
2. Route each repair by capability. Finish schema/bootstrap failures with the schema provider before starting backend/security work; do not let a later phase hide an earlier red slice.
3. Keep one card, worktree, branch, and PR throughout. Every push invalidates all earlier CI receipts.
4. Treat delayed watcher/process notifications as historical run events. Correlate them to run ID and exact SHA before changing current card state.
5. For static ratchet gates, do not create a false green by increasing an allowlist or hiding a raw write behind dynamic syntax. A one-for-one legacy-allowance relocation is acceptable only when the total allowance does not increase and independent diff/tests prove the canonical helper/caller preserves required transactional envelope semantics.
6. If broad tests fail, reproduce the exact failing subset and, when useful, compare the prior PR head in an isolated read-only verification worktree. Fix the current PR until required exact-head CI is green even when the defect originated in an earlier commit.
7. After the final author commits and pushes, end or reclaim that authoring run and park the card on an explicit O2/reviewer CI gate. This prevents the worker from creating a duplicate land request while checks are still running.

## Final handoff gate

Only hand to the sole lander when:

- local head = remote branch head = PR `headRefOid`;
- the worktree is clean;
- PR is open, non-draft, and `MERGEABLE`;
- all required exact-head checks are complete and successful; any skipped check is explicitly expected from workflow scope;
- local mandatory gates pass;
- changed paths and PR diff totals match scope;
- executor provenance is recorded with PLANNED versus ACTUAL routing and native session/ACP evidence;
- card is moved to Review, not Done;
- exactly one canonical land request is submitted.

### Final metadata and queue sequence

1. Inspect the PR title and body after the last code push. Early workers often leave an audit-only title/body even though later commits expand the PR into the full feature. Correct stale metadata before queueing, then read back the title/body and prove `headRefOid` did not change.
2. Re-run exact SHA equality and `gh pr checks` after metadata correction. PR metadata edits should not invalidate CI, but never assume.
3. Search the entire canonical land-queue tree—pending, held, done, and archive—for the PR number, URL, branch, and exact SHA. Do not search only the inbox root.
4. Write one pending request containing exact heads, clean status, scope totals, all local/GitHub gates, PLANNED versus ACTUAL routing, native session or ACP trace, stopped/precommit routing corrections, and sole-lander custody.
5. Mirror the handoff to the fleet bus, read the queue file back, verify exactly one matching request, and add the queue path to the card comment.
6. If GitHub reports `MERGEABLE` but `BEHIND`, disclose it and leave synchronization to the sole lander unless branch policy explicitly requires an update. Do not rewrite or force-push a shared branch merely to obtain `CLEAN`.

If the sole lander reports the PR already landed/deployed, treat that as custody ground truth, archive/drop the implementation lane, and do not requeue from stale land-ready text.