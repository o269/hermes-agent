# Hermes state.db cold archival pass

This runbook documents the staged 30-day hot / 7-day archived-grace / cold-QMD pass implemented by `hermes sessions cold-archive`.

## Non-goals and hard fences

- Do not run this command against the active profile database at `~/.hermes/state.db`; the command refuses that path.
- Do not enable `sessions.auto_prune`.
- Do not run `hermes sessions optimize`, `VACUUM`, FTS optimize, WAL checkpoint, live `DELETE`, config changes, service changes, rclone sync/delete/purge/dedupe, or a production upload as part of author verification.
- The command only mutates an offline recovered candidate when `--apply-retention` is explicitly supplied.

## Policy encoded by default

- Hot: sessions active within 30 days stay live.
- Warm: already archived sessions get a 7-day reversible grace window.
- Cold eligibility begins at inactive age >= 37 days.
- A cold candidate must be ended, already archived, unpinned, and outside the hot+grace window.
- Selection is parent/child lineage-component safe: if any row in the component is open, pinned, unarchived, recent, held, or referenced by async/gateway state, the whole group is skipped.
- Candidate sizing uses actual `messages` rows, never `sessions.message_count`.
- Built-in permanent holds preserve customer/ops platform-source history (`telegram`, `discord`, `whatsapp`, `slack`, `signal`, `matrix`, `sms`, `imessage`, `photon`, `wecom`) unless the operator explicitly disables them.

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

- `GATE-B-MANIFEST.json` — redacted counts/hashes/reason summary.
- `restricted/selected-session-ids.txt` — exact IDs; keep mode 0600 and do not attach to public bus/card.
- `restricted/lineage-parent-map.json` — restricted parent map receipt.

Fable must approve the exact `gate_b_manifest_sha256` before destructive retention.

## Export, rollback bundle, and optional offsite publish

The reviewed run creates a lossless rollback bundle before any deletion and restricted redacted QMD exports for the cold set:

```bash
hermes sessions cold-archive \
  --source /secure/offline-candidate/state.db \
  --stage-root /secure/hermes-state-archive/<same-or-new-run> \
  --age-recipient-file /etc/hermes-state-archive.age.pub \
  --rclone-config /var/lib/hermes-state-offsite/rclone.conf \
  --rclone-remote gdrive:vps-offload/hermes-state-archives \
  --remote-namespace hermes-state/<approved-manifest-sha>
```

Remote publish is copy-only. It performs `rclone copyto`, `rclone check --checksum --one-way`, and an exact byte readback for every object. It does not run remote deletion, sync, purge, dedupe, move, cleanup, or retention.

The lossless rollback bundle is encrypted with age before remote publication. The private age identity must never be stored in the repository, bus, board, or on the VPS runtime path.

## Applying retention to the offline candidate

Only after the Gate-B manifest and cold export have been verified:

```bash
hermes sessions cold-archive \
  --source /secure/offline-candidate/state.db \
  --stage-root /secure/hermes-state-archive/<approved-run> \
  --apply-retention
```

The deletion transaction removes only selected sessions and their dependent `messages`, `session_model_usage`, `compression_locks`, and topic-binding rows. It never reparents surviving sessions. Post-delete verification requires:

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

Before candidate cutover, rollback is a direct restoration of the exact source bundle captured before retention while Hermes is fully stopped. Verify the rollback bundle manifest hashes before restore.

If a candidate has already served new sessions, preserve both the failed candidate and rollback bundle, then stop for an operator decision on delta recovery instead of blindly discarding new rows.
