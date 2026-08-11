# Hermes state.db cold archival pass

This runbook documents the fail-closed 30-day hot / 7-day archived-grace / cold-QMD workflow implemented by `hermes sessions cold-archive`.

The workflow has separate custody phases:

1. a producer creates a new restricted stage, rollback source bundle, encrypted offsite packets, and producer receipt;
2. Fable/operator freezes and approves exact external copies of both
   `GATE-B-MANIFEST.json` and `COLD-ARCHIVE-PRODUCER-RECEIPT.json`, including
   their SHA-256 values;
3. apply loads that existing stage without regenerating or overwriting anything, re-verifies encrypted remote custody, and mutates only the offline candidate;
4. after cutover, an explicit marker starts the 14-day source-bundle retention clock.

## Non-goals and hard fences

- Do not run this command against any active default, current, or named-profile `state.db`, sidecar, symlink, or hardlink alias. The command compares device/inode identity across all profiles.
- Do not enable `sessions.auto_prune`.
- Do not run `hermes sessions optimize`, `VACUUM`, FTS optimize, WAL checkpoint, live `DELETE`, config changes, service changes, rclone sync/delete/purge/dedupe, or a production upload as part of author verification.
- The command never installs a candidate over an active database. Fable is the sole cutover/landing operator.
- A producer stage must not exist. Existing paths, modes, files, symlinks, and sentinels are refused rather than chmodded, repaired, resumed, or overwritten.
- Apply accepts only an existing mode-0700 stage with mode-0600 regular artifacts
  and current-user-owned, single-link, mode-0400 approval copies outside the stage,
  each accompanied by its exact-byte SHA-256.

## Required candidate schema and policy

Cold retention is intentionally unavailable when durable policy state cannot be proven. The candidate `sessions` table must carry canonical `pinned` and `last_activity_at` columns. Missing columns fail the whole command closed; `NULL` values make that lineage ineligible.
Both canonical FTS5 roots and all six message-sync triggers are mandatory; missing
search roots/triggers fail closed before selection or deletion.

The current selection rules are:

- Hot: sessions active within 30 days stay live.
- Warm: already archived sessions get a 7-day reversible grace window.
- Cold eligibility begins at inactive age **>= 37 days**. Exactly on the 37-day boundary is eligible.
- `--hot-days` plus `--archive-grace-days` may lengthen that window, but their combined value may never be below 37 days. Producer and apply both reject a shorter policy before stage creation or candidate mutation; they never silently clamp it.
- Effective activity is the freshest of durable `sessions.last_activity_at`, actual `messages.timestamp`, and `sessions.started_at`.
- A candidate must be ended, archived, unpinned, canonically activity-proven, and outside the hot+grace window.
- Selection is parent/child lineage-component safe. If any row in a component is open, pinned, unarchived, recent, held, or referenced by async/gateway/compression state, the whole component is skipped.
- Candidate sizing uses actual `messages` rows, never `sessions.message_count`.
- Built-in permanent holds preserve customer/ops platform-source history (`telegram`, `discord`, `whatsapp`, `slack`, `signal`, `matrix`, `sms`, `imessage`, `photon`, `wecom`) unless the operator explicitly disables them.

## Gate-B manifest-only sizing

After Fable/operator has quiesced Hermes and produced an offline candidate, a manifest-only sizing run may be created:

```bash
umask 077
hermes sessions cold-archive \
  --source /secure/offline-candidate/state.db \
  --stage-root /secure/hermes-state-archive/manifest-$(date -u +%Y%m%dT%H%M%SZ) \
  --manifest-only
```

Review:

- `GATE-B-MANIFEST.json` — redacted counts, policy, source byte/device/inode hash,
  a deterministic type-preserving logical hash over schema and every application table,
  lineage hashes, and reason summary;
- `restricted/selected-session-ids.json` — exact selected IDs, JSON-encoded to preserve hostile/control characters safely;
- `restricted/lineage-parent-map.json` — restricted full parent map receipt.

A manifest-only stage intentionally cannot authorize deletion: it has no rollback/QMD/encrypted-offsite producer proof. To proceed, create a full producer stage below and approve that stage's manifest bytes.

## Full producer: rollback, QMD, encryption, and offsite proof

The full producer also never deletes. It requires age and rclone custody up front:

```bash
umask 077
hermes sessions cold-archive \
  --source /secure/offline-candidate/state.db \
  --stage-root /secure/hermes-state-archive/producer-$(date -u +%Y%m%dT%H%M%SZ) \
  --age-recipient-file /etc/hermes-state-archive.age.pub \
  --rclone-config /var/lib/hermes-state-offsite/rclone.conf \
  --rclone-remote gdrive:vps-offload/hermes-state-archives
```

The producer creates and verifies, in order:

1. an exact local rollback source bundle using bounded member names `state.db`,
   `state.db-wal`, `state.db-shm`, and `state.db-journal` for the database and every
   present sidecar, plus a per-member hash manifest;
2. restricted, redacted QMD under safe collision-resistant basenames that cannot escape `cold-qmd/`;
3. `ROLLBACK-SOURCE-BUNDLE.tar.gz.age`, an opaque encrypted rollback packet;
4. `RESTRICTED-COLD-QMD.tar.gz.age`, one opaque encrypted packet containing exact IDs, the parent map, QMD, and a file/hash index;
5. both encrypted packets uploaded, checksum-checked, and exactly read back;
6. the clear redacted `GATE-B-MANIFEST.json` uploaded last as the sole clear commit marker.

No selected ID, title, QMD basename, parent-map filename, or clear restricted tar is published remotely. Uploads use directory-targeted `rclone copy --immutable`
(not `copyto`, which can replace a destination on supported backends), followed by
`rclone check --checksum --one-way` and exact `copyto` readback. There is no remote
delete, sync, purge, dedupe, move, cleanup, or retention verb. The age recipient is
read once into `restricted/AGE-RECIPIENTS.txt`; rclone config bytes are copied once
into a private per-operation temporary snapshot. Every subprocess uses only those
frozen bytes, and their hashes are rechecked around use.

The private age identity must never be stored in the repository, bus, board, stage, or runtime path.

## External approval and destructive apply

Fable/operator must freeze exact copies of the manifest and final producer receipt
outside the mutable stage and record both hashes:

```bash
install -m 0400 /secure/hermes-state-archive/<producer>/GATE-B-MANIFEST.json \
  /secure/hermes-state-approvals/GATE-B-MANIFEST.json
install -m 0400 /secure/hermes-state-archive/<producer>/COLD-ARCHIVE-PRODUCER-RECEIPT.json \
  /secure/hermes-state-approvals/COLD-ARCHIVE-PRODUCER-RECEIPT.json
sha256sum /secure/hermes-state-approvals/{GATE-B-MANIFEST,COLD-ARCHIVE-PRODUCER-RECEIPT}.json
```

Apply requires that exact file and exact SHA-256. It does not regenerate the manifest, rewrite exports, chmod the stage, or upload replacement objects:

```bash
hermes sessions cold-archive \
  --source /secure/offline-candidate/state.db \
  --stage-root /secure/hermes-state-archive/<producer> \
  --apply-retention \
  --approved-manifest /secure/hermes-state-approvals/GATE-B-MANIFEST.json \
  --approved-manifest-sha256 <exact-64-hex-file-sha256> \
  --approved-producer-receipt /secure/hermes-state-approvals/COLD-ARCHIVE-PRODUCER-RECEIPT.json \
  --approved-producer-receipt-sha256 <exact-64-hex-producer-receipt-sha256> \
  --rclone-config /var/lib/hermes-state-offsite/rclone.conf
```

Before opening a write transaction, apply proves candidate device/inode/bytes/SHA and
complete logical state match the approved source snapshot and performs fresh
checksum/readback verification of both encrypted packets and the final manifest.

Then it acquires `BEGIN IMMEDIATE` and, while holding that lock:

- repeats active-DB inode custody fencing and approved source device/inode/logical-state checks;
- revalidates ended, archived, unpinned, canonical activity, exact cold boundary, permanent holds, whole-lineage selection, async/gateway/compression references, and selected message count;
- captures complete type-preserving survivor rows plus parent/open/pinned/hot/message/FTS invariants;
- deletes only selected sessions and their documented dependent rows;
- deletes only prompt hashes formerly referenced by selected sessions and now unreferenced; unrelated pre-existing prompt orphans remain untouched;
- verifies all session/message deltas, complete survivor row payloads, survivor parent map,
  open/pinned/hot sets, `PRAGMA integrity_check == ['ok']`, zero foreign-key violations,
  total-message parity in both FTS tables, deleted FTS rowid absence, and type-preserving
  survivor FTS content digests;
- fsyncs `COLD-ARCHIVE-APPLY-PREPARED.json`, including the verified post-state logical
  digest, and commits only after every invariant passes. Any failure rolls back all
  selected/dependent deletion and trigger side effects.

`COLD-ARCHIVE-RETENTION-RECEIPT.json` is exclusively created only after the checked
transaction commits. If the process stops after commit but before that final receipt
write, replay verifies the prepared post-state digest and reconstructs the final receipt
without deleting again. Existing receipts are never clobbered.

## Rollback proof and restore

`rollback/rollback-source-bundle.tar.gz` is the exact pre-retention local source bundle. Before any restore:

1. verify the tar SHA-256 from `COLD-ARCHIVE-PRODUCER-RECEIPT.json`;
2. read `ROLLBACK-BUNDLE-MANIFEST.json` from the tar without unsafe `extractall`;
3. verify every member basename, byte count, SHA-256, and private mode;
4. restore `state.db` as the main database and append each copied fixed suffix
   (`-wal`, `-shm`, `-journal`) to the chosen restore basename;
5. open the restored database and require `PRAGMA integrity_check` exactly `ok`, zero foreign-key rows, and logical selected/dependent row parity.

If a candidate has already served new sessions, preserve both the failed candidate and rollback bundle, then stop for an operator decision on delta recovery instead of blindly discarding new rows.

## Cutover-clocked 14-day source-bundle retention

Bundle creation writes immutable `rollback/SOURCE-BUNDLE-RETENTION-POLICY.json` in state `awaiting-cutover`. Creation time does **not** start deletion eligibility.

After a successful healthy cutover, record the real wall clock (the CLI exposes no arbitrary/backdated timestamp):

```bash
hermes sessions cold-archive-mark-cutover \
  --stage-root /secure/hermes-state-archive/<producer> \
  --rclone-config /var/lib/hermes-state-offsite/rclone.conf \
  --candidate-health-confirmed
```

This requires the committed retention receipt, freshly re-reads remote custody, and
exclusively creates `rollback/CANDIDATE-CUTOVER.json` bound to rollback, producer,
and retention-receipt hashes. Freeze its exact hash externally before pruning:

```bash
sha256sum /secure/hermes-state-archive/<producer>/rollback/CANDIDATE-CUTOVER.json
```

Only after the candidate has remained healthy for at least 1,209,600 seconds (14 days), run:

```bash
hermes sessions cold-archive-prune-bundle \
  --stage-root /secure/hermes-state-archive/<producer> \
  --approved-cutover-marker-sha256 <exact-64-hex-cutover-marker-sha256> \
  --rclone-config /var/lib/hermes-state-offsite/rclone.conf \
  --candidate-health-confirmed
```

Before the exact boundary, or forever without a cutover marker, it deletes nothing. At `cutover_epoch + 1_209_600` or later it removes only:

- local plaintext `rollback/source-bundle/`;
- local plaintext `rollback/rollback-source-bundle.tar.gz`.

It first fsyncs `SOURCE-BUNDLE-PRUNE-PREPARED.json` with exact member hashes, then
removes the plaintext members and exclusively creates `SOURCE-BUNDLE-PRUNED.json`.
Replay recovers from any already-deleted prepared member, so a final-receipt crash is
idempotent. Replay also revalidates the complete cutover/retention/prepare binding and
fails closed if either plaintext bundle artifact reappears. It retains encrypted rollback, encrypted restricted/QMD, manifests, QMD,
producer/retention receipts, and every remote object. Marker/hash tampering, missing
freshly verified remote custody, non-finite/rolled-back clocks, or missing
candidate-health confirmation fails closed.

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
