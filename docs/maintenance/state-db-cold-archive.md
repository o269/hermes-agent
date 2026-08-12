# Hermes state.db cold archival pass

This runbook documents the staged 30-day hot / 7-day archived-grace / cold-QMD pass implemented by `hermes sessions cold-archive`.

## Non-goals and hard fences

- Do not run this command against the active profile database at `~/.hermes/state.db`; the command refuses that path, any symlink to it, and any hardlink/alias that shares its inode.
- Do not enable `sessions.auto_prune`.
- Do not run `hermes sessions optimize`, `VACUUM`, FTS optimize, WAL checkpoint, live `DELETE`, config changes, service changes, rclone sync/delete/purge/dedupe, or a production upload as part of author verification.
- The command only mutates an offline recovered candidate when `--apply-retention` is explicitly supplied **and** custody gates below are satisfied.
- Never remotely publish restricted session IDs, the lineage parent map, or QMD plaintext. Ever.
- Never delete `system_prompts` rows (including orphans); they are out of declared scope.
- Do not overwrite an existing `GATE-B-MANIFEST.json` with different bytes; use a fresh `--stage-root`.
- **Gate-B must not self-approve.** Destructive retention requires an externally supplied `--approved-gate-b-sha256` and a distinct `--approver-identity` that is not the `--requestor-identity`.
- **No delete without verified offsite permanence.** `--apply-retention` refuses unless encrypted offsite publish + readback succeeded for public Gate-B + the `.age` rollback bundle (live in this invocation via rclone, or via `--verified-offsite-receipt` covering those objects).

## Schema prerequisites (fail closed)

Selection and deletion require durable `sessions.pinned` and `sessions.last_activity_at` columns. If either is missing, the pass refuses. Missing columns are never treated as an empty invariant set.

## Policy encoded by default

- Hot: sessions active within 30 days stay live.
- Warm: already archived sessions get a 7-day reversible grace window.
- **Hard floor:** cold eligibility cannot drop below inactive age **>= 37 days** (3_196_800 seconds), even if `--hot-days` / `--archive-grace-days` are set to 0.
- Exact 37-day boundary is eligible (`last_active > cold_cutoff` is the skip rule).
- A cold candidate must be ended, already archived, unpinned, and outside the effective cold window.
- Selection is parent/child lineage-component safe: if any row in the component is open, pinned, unarchived, recent, held, or referenced by async/gateway state, the whole group is skipped.
- Candidate sizing uses actual `messages` rows, never `sessions.message_count`.
- Built-in permanent holds preserve customer/ops platform-source history (`telegram`, `discord`, `whatsapp`, `slack`, `signal`, `matrix`, `sms`, `imessage`, `photon`, `wecom`) unless the operator explicitly disables them.
- Rollback/stage bundles have an explicit **14-day** retention floor (1_209_600 seconds) via `classify_bundle_retention`.

## Gate-B manifest only

After Fable/operator has quiesced Hermes and produced a recovered offline candidate:

```bash
umask 077
hermes sessions cold-archive \
  --source /secure/offline-candidate/state.db \
  --stage-root /secure/hermes-state-archive/$(date -u +%Y%m%dT%H%M%SZ) \
  --manifest-only
```

Review:

- `GATE-B-MANIFEST.json` — redacted counts/hashes/reason summary (integrity-gated; not self-overwritten).
- `restricted/selected-session-ids.txt` — exact IDs; keep mode 0600 and do not attach to public bus/card; **never remote-publish**.
- `restricted/lineage-parent-map.json` — restricted parent map receipt; **never remote-publish**.

Record `gate_b_manifest_sha256` from the receipt. **A distinct approver (Fable)** must approve that exact hash before destructive retention. The same actor who requests deletion cannot approve it.

## Export, rollback bundle, and required offsite publish

The reviewed run creates a lossless rollback bundle before any deletion and local redacted QMD exports for the cold set (QMD stays on the stage; it is not remotely published). Encrypted offsite permanence is required before delete:

```bash
hermes sessions cold-archive \
  --source /secure/offline-candidate/state.db \
  --stage-root /secure/hermes-state-archive/<same-or-new-run> \
  --age-recipient-file /etc/hermes-state-archive.age.pub \
  --rclone-config /var/lib/hermes-state-offsite/rclone.conf \
  --rclone-remote gdrive:vps-offload/hermes-state-archives \
  --remote-namespace hermes-state/<approved-manifest-sha>
```

Remote publish is copy-only and limited to:

- public `GATE-B-MANIFEST.json`
- age-encrypted rollback bundle (`.tar.gz.age`)

It performs `rclone copyto`, `rclone check --checksum --one-way`, and an exact byte readback for every object. It does not run remote deletion, sync, purge, dedupe, move, cleanup, or retention. Only integrity token `rclone-checksum-and-readback-ok` counts as a verified offsite receipt entry.

The private age identity must never be stored in the repository, bus, board, or on the VPS runtime path.

## Applying retention to the offline candidate

Only after:

1. Gate-B export is complete,
2. A **distinct** approver has approved the exact `gate_b_manifest_sha256`,
3. Encrypted offsite publish + readback has produced a positive permanence receipt,

```bash
hermes sessions cold-archive \
  --source /secure/offline-candidate/state.db \
  --stage-root /secure/hermes-state-archive/<approved-run> \
  --age-recipient-file /etc/hermes-state-archive.age.pub \
  --rclone-config /var/lib/hermes-state-offsite/rclone.conf \
  --rclone-remote gdrive:vps-offload/hermes-state-archives \
  --remote-namespace hermes-state/<approved-manifest-sha> \
  --approved-gate-b-sha256 <approved-manifest-sha> \
  --requestor-identity "ops@example" \
  --approver-identity "fable" \
  --apply-retention
```

Alternatively, pass `--verified-offsite-receipt /path/to/prior-receipt.json` (full prior cold-archive receipt or its `remote_publish` list) instead of re-running rclone in the delete invocation. The receipt is re-verified (existence of Gate-B + `.age` entries, integrity token, sha256==readback_sha256, and local Gate-B bytes bind) — prior-step success is never assumed.

Single-shot self-build + self-hash + delete **without** external approval identities and offsite proof **fails closed**.

The deletion transaction removes only selected sessions and their dependent `messages`, `session_model_usage`, `compression_locks`, and topic-binding rows. It never reparents surviving sessions. **Invariant checks run inside the same transaction as the deletes** — any failure rolls the deletion back.

Post-delete verification requires:

- `PRAGMA integrity_check` exactly `ok`;
- `PRAGMA foreign_key_check` zero rows;
- actual session/message deltas equal the approved manifest;
- open, pinned, and hot session ID sets unchanged;
- surviving `parent_session_id` map byte-equivalent to the pre-delete map;
- FTS document counts match `messages` and non-tool messages.

## Rebuild / cutover custody

For physical shrinkage, Fable/operator uses the existing offline recovery path against the retained candidate:

```bash
hermes sessions recover \
  --source /secure/offline-candidate/state.db \
  --output /secure/final/state.db \
  --work-dir /secure/recover-work \
  --report /secure/final/recovery.json
```

This command also never installs the candidate over the active database automatically. Fable is the sole lander/cutover operator.

## Rollback

Before candidate cutover, rollback is a direct restoration of the exact source bundle captured before retention while Hermes is fully stopped. Verify the rollback bundle manifest hashes before restore. Bundles younger than 14 days (1_209_600s) must be retained.

If a candidate has already served new sessions, preserve both the failed candidate and rollback bundle, then stop for an operator decision on delta recovery instead of blindly discarding new rows.
