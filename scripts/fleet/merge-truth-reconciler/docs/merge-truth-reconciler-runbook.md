# Merge-truth reconciler runbook

## Status and custody

`merge-truth-reconciler` is an independent two-minute oneshot. Source of truth lives in
`scripts/fleet/merge-truth-reconciler/` on o269/hermes-agent (post-PR36/PR38/PR41 main).
This package is author-only until Fable installs it: it has not been enabled, deployed, or
connected to the live board. Fable is the sole installer and lander.

Keep `MERGE_TRUTH_RECONCILER_ENABLED=0` until independent exact-head review passes and the
bundled broker helper proves its capability handshake against live boardd:

- PR36 custody (landed): atomic/idempotent ownership convergence for both declared and
  referenced rows plus URL-specific semantic release from the owned active-PR candidate set.
- PR38 custody (landed): terminal-custody-safe `recompute_ready` and receipt-backed promotion counts.
- PR41 custody (landed): boardd native idempotency / applied_ops replay for helper mutations.

The process never opens the fleet database and has no SQL fallback. Production board I/O
is exclusively through `merge_truth_board_helper.py` → boardd (`HERMES_KANBAN_BROKER=1`).

## Tick ordering and safety

A tick performs these stages in order:

1. Check the kill switch. Disabled ticks print one line and perform no host, board, GitHub, state, or report access.
2. Sample load1 and free KiB on `/mnt/HC_Volume_106418160`. Load1 above 12, SDB free below 15 GiB, or an unavailable sample returns `admission-skip` before any board/GitHub/state/report I/O. Configuration may tighten but cannot weaken these limits.
3. Require the source-artifact directory to be readable/searchable, require the pre-bootstrapped state/report/alert directories and ticker file to be writable, then acquire a nonblocking file lock. Source artifacts remain read-only input under the home sandbox; enabled execution never relies on creating paths through that tree.
4. Recover any durable alert outbox from a prior process death before starting new board or GitHub work. Ticker lines and alert filenames carry deterministic receipt keys, so replay is idempotent.
5. Prove broker-helper capabilities and query live broker/card citation inventory on every admitted tick. A TTL applies only to bounded artifact-derived repository inventory; live board repositories are merged additively every tick. Any broker inventory error fails the read phase.
6. Scan only allowlisted receipt/artifact `.json`, `.md`, and `.txt` filenames. Defaults cap the scan at 500 candidates, 1,000,000 bytes per file, 20,000,000 bytes total, and 5,000 ms. Cap, truncation, timeout, or read failure fails closed. Full GitHub PR URLs and split receipts of the form `repo: owner/name` followed by `pr: #N` are accepted.
7. Fetch every required GitHub page successfully using an in-memory `gh auth token`, conditional ETags, a durable watermark, and overlap. Feed/auth/parse ambiguity causes no board writes and no watermark advance. Environment configuration may tighten but cannot exceed the hard maxima: 86,400 seconds inventory TTL, 30 bootstrap days, 600 overlap seconds, 20 request-timeout seconds, 30 helper-timeout seconds, and 20 pages per repository.
8. Read and parse all candidate cards, including canonical authority booleans, before the first board mutation.
9. Converge ownership through one atomic broker operation per canonical merged URL, then prove URL-specific semantic release.
10. Complete exact Tier-A gates, add idempotent evidence to Tier-B/excluded cards, and call the canonical promotion path only after completion. Every board operation and recompute call has a deterministic operation ID.
11. Queue SLO outputs, advance/prune state, and durably save the watermark plus pending outbox before publishing any `ok` report or alert. Flush alerts idempotently, save the cleared outbox, prune timestamped reports, write timestamped JSON/Markdown, and write `latest.json` last as the report commit marker.

An exact Tier-A gate may complete only when canonical `terminal`, `protected_custody`, and `active_run` are all false and local security/OPERATOR safeguards pass. Canonical terminal/failure states, protected custody, active runs (including drifted statuses), security scope, Fable/terminal defense-in-depth markers, and OPERATOR holds are never auto-completed or promoted. They may receive idempotent merge evidence. Malformed, mixed, unknown, or unmerged gates are never auto-completed.

There is no cross-system transaction spanning the broker and local files. Retry safety comes from deterministic broker operation IDs plus a local durable output outbox. A board operation may already be committed when a later local save fails; replay uses the same operation ID. Local state is committed before `ok` report/alert publication, and a pending alert remains retryable until both deterministic outputs and the cleared-outbox checkpoint succeed.

## Broker-helper contract

Set `MERGE_TRUTH_RECONCILER_BOARD_HELPER` to the bundled executable
`merge_truth_board_helper.py` (installed beside the reconciler). The helper requires
`HERMES_KANBAN_BROKER=1` and `BOARDD_SOCK` and uses `hermes_cli.kb_client` plus
lifecycle functions over `BrokerConnection` only. The reconciler sends one JSON object
on stdin and places no card text or token in argv. Every request contains `action`,
`author=merge-truth-reconciler`, and `payload`. Every success response is:

```json
{"ok": true, "result": {}}
```

Required capability names:

- `inventory_hex_v1`
- `card_citations_hex_v2`
- `card_authority_v1`
- `ownership_converge_v1`
- `semantic_url_release_v1`
- `gate_complete_opid_v1`
- `evidence_comment_opid_v1`
- `recompute_ready_receipts_v1`

Actions:

- `capabilities`: return `capabilities`.
- `inventory`: return normalized ownership `canonical_urls` and broker-returned `hex_texts` for body/comment citations.
- `list_cards_citing`: accept `canonical_urls` and `body_encoding=hex`; return cards with `task_id`, `title`, `body_hex`, `comments_hex`, `status`, `assignee`, `skills`, and explicit JSON booleans `terminal`, `protected_custody`, and `active_run`. Missing/non-boolean authority, a known terminal/failure status with `terminal=false`, a running status with `active_run=false`, or simultaneous terminal+active authority fails the entire read phase before mutation.
- `converge_ownership`: accept merge evidence, deterministic `operation_id`, and `atomic=true`; expire/clear declared=1 and declared=0 records in one canonical operation and return `cleared_rows`, `affected_task_ids`, and `semantic_released=true`. Failure to prove semantic release must roll back inside the helper.
- `complete_gate_card`: use the canonical lifecycle function, deterministic operation ID, and durable receipt comment; return `changed`.
- `add_evidence_comment`: append only when the deterministic operation has not already been applied; return `changed`.
- `recompute_ready`: call the installed canonical promotion path and return `actual_promoted_count` derived from before/after or event receipts.

Any missing capability or ambiguous result blocks the tick. The helper must use boardd/broker interfaces only.

## Install gate (Fable only)

After independent review and effective-interface proof, Fable may:

1. Copy this payload (including `merge_truth_board_helper.py`) to
   `%h/.local/lib/merge-truth-reconciler/` without modifying an existing reconciler.
2. Ensure a hermes-agent import root is available at `%h/.local/lib/hermes-agent`
   (or adjust unit `PYTHONPATH`) so the helper can import `hermes_cli.*`.
3. Copy the unit and timer to `%h/.config/systemd/user/`.
4. Create `%h/.config/merge-truth-reconciler.env` from the example with mode 0600 and keep `ENABLED=0` for the disabled smoke test.
5. Before any enabled smoke, bootstrap writable paths without truncating evidence:

   ```text
   install -d -m 0700 "$HOME/.local/state/merge-truth-reconciler"
   install -d -m 0700 "$HOME/godmode-bus/artifacts/merge-truth-reconciler"
   install -d -m 0700 "$HOME/godmode-bus/to-claude"
   : >> "$HOME/godmode-bus/STATUS-TICKER.md"
   chmod 0600 "$HOME/godmode-bus/STATUS-TICKER.md"
   ```

6. Run compile, focused pytest, secret/path scan, and `systemd-analyze --user verify`.
7. Run the service once while disabled and verify exactly one `status=disabled` line and no state/report/alert creation or ticker change.
8. Confirm all four writable paths exist with the modes above. An enabled smoke must fail early with `required writable ... is missing` if bootstrap is incomplete.
9. With boardd healthy, run one helper `capabilities` probe, set `ENABLED=1`, and run one manual tick only after independent review approval.
10. Verify report receipts, semantic URL release, no protected card completion, actual promotion counts, and an empty durable alert outbox before enabling the timer.

Do not paste a GitHub token into the environment file. The script captures `gh auth token` in memory and suppresses helper/token stderr.

## Operations

Inspect without mutating:

```text
systemctl --user status merge-truth-reconciler.timer merge-truth-reconciler.service
journalctl --user -u merge-truth-reconciler.service -n 50 --no-pager
```

The latest JSON report is under `~/godmode-bus/artifacts/merge-truth-reconciler/latest.json`; timestamped JSON and Markdown reports are retained beside it for at most 30 days and 100 timestamp groups by default. Limits may be tightened but not increased through environment configuration. `latest.json` is never pruned and is written only after both timestamped artifacts succeed. Reports surface artifact candidate/scanned/skipped/byte counts, cache use, and pruned-report counts. They contain task IDs/URLs, never full card bodies or credentials.

Every unresolved merged gate records first observation. At four minutes wall time or its second reconciler observation, it remains in every report as an `SLO-BREACH`. The ticker and deterministic `to-claude` alert are emitted once per card+URL+merge-SHA receipt. Both include the same `RECEIPT=merge-truth-reconciler:slo-alert:...` key; restart replay scans the ticker for that key and rewrites the deterministic alert path safely.

## Hard stop

```text
systemctl --user disable --now merge-truth-reconciler.timer
```

Also set `MERGE_TRUTH_RECONCILER_ENABLED=0`. Either control is sufficient; use both for a hard stop. The disabled path exits successfully after one concise line.

## Failure interpretation

- `disabled`: intentional kill switch; no I/O beyond stdout.
- `admission-skip`: load/SDB gate held the entire tick before board/GitHub/state/report access.
- `overlap-skip`: another tick owns the lock.
- `capability-blocked`: the helper or installed PR36/PR38 semantics are not proven; no guessed fallback.
- `error`: feed, helper, state, action, alert, or report ambiguity. Read/action failures and first durable-state-save failures do not advance the watermark. An alert/report publication failure can occur after the new state is already durable; in that case the watermark truthfully remains advanced and a pending alert stays in the outbox for deterministic replay. `latest.json` is never published as current `ok` before the successful state checkpoint.
