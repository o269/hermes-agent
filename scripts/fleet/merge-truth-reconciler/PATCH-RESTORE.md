# Merge-truth reconciler patch / restore record

## Custody state

This package lives under `scripts/fleet/merge-truth-reconciler/` in o269/hermes-agent
and is intended for Fable-only install onto the host. It has not modified a live script,
board, unit, timer, or checkout in authoring. Fable is the sole installer, enabler,
rollback executor, and lander.

The implementation must remain disabled until independent exact-head review approves the
PR head and the bundled broker helper proves its capability handshake against live boardd
(post-PR36 ownership, post-PR38 recompute_ready terminal custody, post-PR41 applied_ops
idempotency). There is no direct-database fallback.

## Authored payload

- `merge_truth_reconciler.py`
- `merge_truth_board_helper.py` (broker-only helper; boardd via kb_client / BrokerConnection)
- `tests` under `tests/fleet/test_merge_truth_reconciler.py` and
  `tests/fleet/test_merge_truth_board_helper.py`
- `systemd/merge-truth-reconciler.service`
- `systemd/merge-truth-reconciler.timer`
- `config/merge-truth-reconciler.env.example`
- `docs/merge-truth-reconciler-runbook.md`
- `PATCH-RESTORE.md`

This does not patch or extend `fleet-board-reconciler`, `fleet-reviewed-land`, boardd,
`kanban.py`, or `kanban_db.py`. Helper mutations use existing broker surfaces only
(query / interactive txn / add_comment op_id / complete_task / recompute_ready).

## Fail-closed restore path

Fable rollback order:

1. `systemctl --user disable --now merge-truth-reconciler.timer`
2. Set `MERGE_TRUTH_RECONCILER_ENABLED=0` in the private environment file.
3. Preserve the state and reports for evidence unless the operator explicitly authorizes removal.
4. Remove only the independently installed script/unit/config payload created from this package.
5. Run `systemctl --user daemon-reload` and verify the timer is absent/inactive.

Disabling this service does not require reverting any existing fleet component because no
existing component is changed. Never restore by editing the board database or deleting
ownership rows directly outside the helper's atomic converge path.

Do not remove `%h/.local/state/merge-truth-reconciler`, timestamped/latest reports,
deterministic SLO alert files, or matching ticker receipts during routine rollback. A
pending durable alert outbox is evidence of an interrupted publication and remains safe to
inspect or replay after the code/helper gate is re-established. Evidence removal requires
separate operator authorization.

## Writable-path bootstrap / reinstall invariant

Before an enabled smoke, Fable pre-creates the state, report, and alert directories with
mode 0700, touches (without truncating) `STATUS-TICKER.md`, and sets the ticker mode to
0600 exactly as documented in the runbook. The unit uses non-optional `ReadWritePaths=`
entries; missing bootstrap paths must fail startup or the code's enabled preflight clearly.
Disabled smoke remains zero-I/O except stdout and does not require these paths.

Reinstall must preserve existing state/report/alert/ticker evidence. Never use a create
command that truncates `STATUS-TICKER.md`, never recursively remove the evidence
directories, and never reset state version/watermark to make an enabled smoke pass.

## Admission-safety restore invariant

The service has its own pre-I/O admission gate: load1 greater than 12, SDB free space below
15 GiB, or either sample being unavailable causes a successful `admission-skip` with no
board call, GitHub call, state write, or report write. Environment configuration may tighten
but cannot weaken those limits. A rollback or reinstall must not remove this gate.

Artifact scan and report-retention ceilings are also non-weakenable: at most 500 allowlisted
candidates, 1,000,000 bytes per file, 20,000,000 bytes total, 5,000 ms scan time, 100
timestamp groups, and 30 retention days by default. Reinstall may tighten these values but
must not increase them.
