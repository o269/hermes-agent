# boardd backup freshness and integrity monitor

`boardd-backup-monitor` is an external, read-only control for finalized boardd
backups. It runs independently of `boardd.service`, so a stopped or wedged broker
cannot suppress the check. It does not connect to the boardd socket and never
opens the live database.

The monitor deliberately keeps no success cache. Every timer invocation scans the
backup directory, selects the newest regular finalized copy, checks freshness, and
runs `PRAGMA integrity_check` through an immutable read-only SQLite connection.
Repeated checks are idempotent, while a newly arrived copy is selected and checked
automatically.

## Selection and failure contract

Only exact, calendar-valid `kanban.YYYYMMDD-HHMMSS.db` regular files are
candidates. `.kanban.*.partial`, temporary files, malformed names, directories,
and symlinks are ignored. The candidate with the newest completion mtime wins;
its embedded boardd timestamp is also enforced as a freshness bound.

The command exits nonzero and writes one `CRITICAL boardd-backup-monitor` line to
stderr when:

- no finalized candidate exists;
- any canonical candidate has a future filename timestamp or mtime;
- filename and completion-mtime ordering disagree about the newest copy;
- the newest candidate exceeds the configured maximum age;
- the file changes while it is being checked;
- SQLite cannot open/query it read-only; or
- the complete `PRAGMA integrity_check` result is not exactly one row containing
  `ok`.

A corrupt newest copy always fails the check. The monitor never falls back to an
older clean copy. Success is one `OK boardd-backup-monitor` line on stdout.

## Configuration

The unit defaults match boardd's current production contract:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOARDD_BACKUP_DIR` | `/var/lib/boardd/fleet/boardd-backups` | Finalized-copy directory beside the canonical board DB |
| `BOARDD_BACKUP_INTERVAL_S` | `900` | Expected boardd backup cadence |
| `BOARDD_BACKUP_MONITOR_MAX_AGE_SECONDS` | `2700` | Fail after 45 minutes (three backup intervals) |

The maximum age must be at least twice the configured cadence. Override values in
a root-owned `/etc/default/boardd-backup-monitor` file or a systemd drop-in. These
values are non-secret. There is intentionally no `BOARDD_SOCK` dependency, so the
monitor is compatible with `/run/boardd/boardd.sock` and remains useful while the
broker is unavailable.

## Stage and preflight (no running-service mutation)

Fable is the sole installer. From a clean, reviewed Git checkout:

```bash
release="backup-monitor-$(git rev-parse --short=12 HEAD)"
sudo scripts/fleet/install-boardd-backup-monitor.sh \
  --source "$PWD" --release-id "$release"
sudo -u boardd /usr/bin/python3 \
  "/opt/hermes-boardd-backup-monitor/releases/$release/boardd-backup-monitor.py" \
  --backup-dir /var/lib/boardd/fleet/boardd-backups \
  --backup-interval-seconds 900 \
  --max-age-seconds 2700
systemd-analyze verify \
  scripts/fleet/boardd-backup-monitor.service \
  scripts/fleet/boardd-backup-monitor.timer
```

The installer stages an immutable release plus unit files. It does not call
`systemctl`, reload systemd, enable/start a timer, restart boardd, or touch the
live DB/backups. Use `--destdir /absolute/path` for an unprivileged disposable
staging tree.

## Quiet-window activation and canary (Fable only)

1. Capture the current release links, staged manifest/hash, newest finalized
   backup name/mtime, and existing timer state.
2. Re-run the installer with the same staged `--release-id "$release"` plus
   `--activate` to atomically move only the monitor's `current` link:
   `sudo scripts/fleet/install-boardd-backup-monitor.sh --source "$PWD"
   --release-id "$release" --activate`.
3. Run the monitor command once as `boardd`. Require exit 0 and the exact selected
   newest backup in its `OK` line.
4. Prove a negative canary in a disposable directory (never the live backup
   directory): an absent/future/stale/corrupt newest file must exit nonzero.
5. Run `systemctl daemon-reload`.
6. Start `boardd-backup-monitor.service` once and verify its exit status plus
   `journalctl -u boardd-backup-monitor.service`.
7. Only after both canaries pass, run
   `systemctl enable --now boardd-backup-monitor.timer` and confirm the next trigger.

No boardd restart is required. Do not rename, delete, chmod, repair, or otherwise
mutate production backups for a canary.

## Alert response

Treat any `CRITICAL` line or failed unit as fail-closed. Inspect the reason and the
newest finalized copy without opening the live DB. Check `boardd.service`, disk
guard/space, backup logs, host clock, directory permissions, and recent release
changes. Preserve a corrupt copy for forensics. Database restore or backup removal
requires a separate operator-reviewed recovery plan.

## Rollback

To return to the prior monitor implementation without touching boardd or backups:

```bash
sudo systemctl stop \
  boardd-backup-monitor.timer boardd-backup-monitor.service
sudo scripts/fleet/rollback-boardd-backup-monitor.sh
sudo systemctl start boardd-backup-monitor.service
sudo journalctl -u boardd-backup-monitor.service -n 20 --no-pager
sudo systemctl start boardd-backup-monitor.timer
```

The rollback script only swaps the monitor's `current` and `previous` links and
prints `systemctl_mutation=none`. If the control must be fully withdrawn, Fable may
disable the timer, remove its two unit files, and daemon-reload after preserving
the last status/journal receipt. Withdrawal does not justify touching the live DB
or finalized backups.
