# Host install: shared board-liveness + the missing alarm

Fable is the sole lander. This file is the install packet for the
unversioned host reapers. Do not `--apply` any reaper from this document.

## Why this exists

On 2026-08-13 21:41:28 UTC, `/usr/local/bin/disk-watchdog.sh` deleted 1400
`/tmp` entries (40 GiB) and named none of them. Three of those entries were
the only copy of queued card work. Nothing paged.

The in-repo helper is `scripts/board_liveness.py` + `scripts/board_liveness.sh`.
Host reapers must source the shell helper rather than re-implement a guess.

## Install (Fable)

```bash
sudo install -m 0755 scripts/board_liveness.sh /usr/local/lib/board_liveness.sh
# then edit /usr/local/bin/disk-watchdog.sh to:
#   source /usr/local/lib/board_liveness.sh
#   board_liveness_load "$BOARD_DB"
#   board_liveness_holds "$base" && keep
# Same for ~/godmode-bus/bin/tmp-reaper.sh (via fleet-disk-common.sh
# fleet_extract_task_ids) and /home/odai/.local/bin/tmp-build-gc.sh.
```

`tmp-build-gc.timer` is currently **enabled and active** every 15 minutes
and deletes `/tmp` children by name pattern + age with **no board oracle**.
That is the same defect class, still armed.

## Alarm that should have fired

The 21:41 pass logged only:

```
GC: seen=1696 deleted=1400 would=0 kept_live=9 kept_prot=242 freed=40731MiB
```

No paths, no page. Add both of the following to disk-watchdog (and any
reaper that actually unlinks):

1. **Receipt.** Every deleted path is appended to
   `~/godmode-bus/artifacts/disk-watchdog-deletes.log` as
   `TS DELETED <path> <bytes> shaped=<0|1>`. Rotate like the existing log.
2. **Page.** If any deleted basename is task-id-shaped
   (`board_liveness_is_shaped` / `board_liveness.is_task_id_shaped`), write
   `~/godmode-bus/to-claude/disk-watchdog-TASK-ID-DELETE-<TS>.md` listing
   every shaped path and page the same way GC-FAILURE already pages.
   A shaped delete is never "routine cleanup".

Dry-run must emit `WOULD-DELETE` with the same shaped flag so a test can
assert the alarm would have fired on the three victim names.

## Proposed disk-watchdog snippet (do not apply from this PR)

```bash
shaped_deleted=0
# inside the delete branch, after the dry-run/live split:
if board_liveness_is_shaped "$base"; then
  shaped_deleted=$((shaped_deleted+1))
  printf '%s SHAPED-DELETE %s\n' "$TS" "$path" >> "$LOG"
fi
# after the GC loop:
if [[ "$DRY_RUN" != "1" && "$shaped_deleted" -gt 0 ]]; then
  printf '%s 🔴 SHAPED-DELETE: %d task-id-shaped /tmp names deleted — page operator\n' \
    "$TS" "$shaped_deleted" >> "$ALERT_FILE"
  cat > "${TO_CLAUDE}/disk-watchdog-TASK-ID-DELETE-${TS}.md" <<EOF
# 🔴 DISK GC deleted ${shaped_deleted} task-id-shaped names
See $LOG for SHAPED-DELETE lines. This is the 2026-08-13 failure mode.
EOF
fi
```

## Consumers that still re-implement a guess

| reaper | board? | same defect? |
| --- | --- | --- |
| `/usr/local/bin/disk-watchdog.sh` | patched 2026-08-13 (inline) | must switch to this helper |
| `scripts/fleet_tmp_reaper.py` | yes, now via this helper | regex was `t_[8 hex]` |
| `~/godmode-bus/bin/tmp-reaper.sh` | yes, via `fleet-disk-common.sh` | extractor is still `t_[[:xdigit:]]{8,}` so underscoreless names skip the board check |
| `~/.local/bin/tmp-build-gc.sh` | official workspaces only | `/tmp` arm is name+age, **armed every 15 min** |
| `~/.local/bin/workspace-reaper` | yes, fail-closed | exact dirname==id; official dirs only |
| `fleet-workspace-reclaim` | no | age-only on the scratch volume; timer disabled |
| `scripts/fleet/tmp_reaper.py` (PR #81) | running workspaces only | queued cards unprotected |
