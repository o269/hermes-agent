# boardd restart-safe runtime

This package moves the broker's executable runtime out of `/home` and into an
immutable, versioned release under `/opt/hermes-boardd`. The systemd unit runs as
the dedicated `boardd` principal with `ProtectHome=yes`, owns only
`/var/lib/boardd`, and exposes its group-readable Unix socket at
`/run/boardd/boardd.sock`.

The installer stages a release and unit but deliberately does not call
`systemctl daemon-reload`, enable, restart, or stop. Fable (the sole lander)
performs the one maintenance-window restart after the preflight gates below.

## Filesystem contract

| Path | Owner/mode | Purpose |
| --- | --- | --- |
| `/opt/hermes-boardd/releases/<id>` | `root:root`, no group/other write | Immutable Python runtime and broker source |
| `/opt/hermes-boardd/current` | root-owned atomic symlink | Active release |
| `/opt/hermes-boardd/previous` | root-owned atomic symlink | One-step rollback release |
| `/var/lib/boardd/fleet/kanban.db` | `boardd:boardd` (existing custody retained) | Canonical board DB |
| `/run/boardd/boardd.sock` | `boardd:boardd`, `0660` | Client endpoint |
| `/etc/systemd/system/boardd.service` | `root:root`, `0644` | Fixed-path unit |

No ACL or permission change is made beneath `/home`. Client users must already
be members of the `boardd` group. Existing gateway/dispatcher drop-ins keep
their board pin and change only `BOARDD_SOCK` to `/run/boardd/boardd.sock`.

## Stage and verify (no live service mutation)

```bash
sudo scripts/fleet/install-boardd-runtime.sh --source "$PWD"
release=$(readlink /opt/hermes-boardd/current 2>/dev/null || true)
newest=$(find /opt/hermes-boardd/releases -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort | tail -1)
sudo -u boardd test -x "/opt/hermes-boardd/releases/$newest/venv/bin/python"
sudo -u boardd test -r "/opt/hermes-boardd/releases/$newest/libexec/boardd.py"
sudo -u boardd "/opt/hermes-boardd/releases/$newest/venv/bin/python" \
  "/opt/hermes-boardd/releases/$newest/libexec/boardd.py" --help
systemd-analyze verify scripts/fleet/boardd.service
```

The source must be a Git worktree with no staged or unstaged tracked changes.
The installer archives `HEAD`, so untracked files and credentials cannot enter
the runtime and setuptools cannot leave a root-owned `build/` in the checkout.

For an unprivileged disposable staging tree, pass `--destdir /absolute/path`.
The staged unit intentionally keeps production absolute paths; run the staged
release executable by its full staging path.

## One restart apply

1. Freeze `systemctl show boardd.service`, `systemctl cat boardd.service`, the
   current `ExecStart` target hashes, socket inode, and `PRAGMA database_list`
   plus `PRAGMA table_info(tasks)` through boardd.
2. Run the installer with `--activate`. It atomically moves `current` and saves
   the old target in `previous`; it still does not touch the running service.
3. Update the existing gateway/dispatcher unit drop-ins to
   `BOARDD_SOCK=/run/boardd/boardd.sock`; keep the canonical board DB pin.
4. Run `systemctl daemon-reload`, then restart `boardd.service` exactly once.
5. Verify `ActiveState=active`, `SubState=running`, the new `ExecMainStartTimestamp`,
   `ping`, socket ownership/mode, and broker-only create/list/show of a disposable
   card with `reasoning_effort=high`.
6. Restart/reload the clients only after boardd health passes.

`--import-schema` runs additive, idempotent migrations before readiness. The
`reasoning_effort` column is nullable, so legacy tasks retain profile-default
thinking behavior. Invalid levels fail closed.

Before accepting its first queued request, and every five minutes thereafter,
boardd read-only compares `PRAGMA table_info(tasks)` on its broker-owned live
connection with the `tasks` columns declared by the installed
`hermes_cli.kanban_db` schema. Missing columns emit one structured
`SCHEMA DRIFT ALERT` per distinct missing-column set and are exposed through
`ping`/`stats` as `schema_drift_alarm`; unchanged drift is deduplicated. Probe
errors are warning-only (`schema_check_error`) and never block healthy startup.
The detector does not run migrations or repair the live schema.

## Functional write canary

`boardd` owns a broker-loopback functional probe. It connects to the Unix socket
with `kb_client.Client`, opens a `boardd_shim.BrokerConnection`, and exercises the
same bounded interactive-transaction surface used by kanban clients. A healthy
run performs four broker-visible checks:

1. open `BEGIN IMMEDIATE` through boardd;
2. insert one task, one task event, and one task run using a unique reserved id;
3. read all three rows back inside the transaction and verify exact counts; and
4. explicitly `ROLLBACK`, then query the unique id again and require zero task,
   event, and run rows.

The task/event/run ids reported by `ping` are ephemeral transaction receipts;
they never identify persisted rows. The probe does not call `create_task`,
`archive_task`, or any lifecycle hook, so it cannot create a visible card,
append board history, trigger dispatch, or perturb dependency queues. The
reserved `__hermes_boardd_write_canary_v1__` marker exists only inside the
rolled-back transaction and in health diagnostics. A successful run reports
`probe_kind=rollback-transaction` and
`last_persisted_counts={tasks:0,task_events:0,task_runs:0}`.

The service defaults are:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `BOARDD_WRITE_CANARY_MODE` | `periodic` | `disabled`, `periodic`, or staging/test-only `once` |
| `BOARDD_WRITE_CANARY_START_DELAY_S` | `90` | Initial offset from startup and the 900s backup boundary |
| `BOARDD_WRITE_CANARY_INTERVAL_S` | `300` | Periodic cadence |
| `BOARDD_WRITE_CANARY_TIMEOUT_S` | `20` | Absolute client lifetime for one complete probe |
| `BOARDD_WRITE_CANARY_ALERT_REPEAT_S` | `3600` | Minimum repeat interval for an unchanged failure |

Enable, disable, or select `once` only through the staged unit or a systemd
drop-in that overrides `BOARDD_WRITE_CANARY_MODE`; Fable then performs the same
single `daemon-reload` + restart sequence described above. Remove the override
to restore the production `periodic` default. There is no live toggle and no
database change involved.

Backup and canary share an in-process maintenance lock; a canary due during a
backup is suppressed and counted rather than overlapped. Normal writes remain
serialized by the broker queue. The canary does not change the 2s interactive
transaction cap or add filesystem, subprocess, external-network, or sleep work
to transaction bodies. The only transport while a transaction is open is the
existing local broker-protocol round trip required by every `BrokerConnection`.

### Health and alert interpretation

Inspect the broker, not the database:

```bash
sudo -u boardd env \
  BOARDD_SOCK=/run/boardd/boardd.sock \
  /opt/hermes-boardd/current/venv/bin/python -m hermes_cli.kb_client ping
journalctl -u boardd.service -g 'WRITE CANARY' --since -2h
sudo tail -n 20 /var/lib/boardd/fleet/boardd-HEALTH-ALERT
```

`write_canary_ok=false`, `health_ok=false`, and a non-null
`write_canary_alarm` mean the real interactive write/verify/rollback path failed.
The alarm includes `kind`, `phase`, normalized `error_code`, `error_type`, an
ephemeral `task_id`, and a stable fingerprint. Raw OS and retry text is
diagnostic only and is excluded from fingerprint identity, so transport jitter
cannot create alert storms. Unknown-column failures are classified as
`schema-drift`; transaction deadlines as `write-canary-timeout`; invalid insert
receipts as `write-canary-verification`; rollback failures as
`write-canary-rollback`; and any non-zero post-rollback count as
`write-canary-persistence`. Identical failures update the in-memory count every
run but write at most one repeat event per repeat interval. A changed fingerprint
emits an immediate transition, and a successful full run emits an immediate
recovery. No transition or recovery is suppressed by the repeat limiter. Emitted
alarm state includes its first/last and last-emitted timestamps; boardd restores
the latest unresolved write-canary alarm from the alert journal at startup, so a
process restart neither resets the repeat window nor suppresses the eventual
recovery notice.

The rollback probe never searches for or mutates canary rows from older
create/archive releases. During cutover, inspect legacy rows through boardd and
archive any remaining active card once; do not repair or delete it with direct
SQLite:

```bash
sudo -u boardd env \
  BOARDD_SOCK=/run/boardd/boardd.sock \
  /opt/hermes-boardd/current/venv/bin/python -m hermes_cli.kb_client query \
  "SELECT id,title,status,created_at FROM tasks WHERE status!='archived' AND created_by='__hermes_boardd_write_canary_v1__' ORDER BY created_at"
```

`disabled` is an explicit loss of functional coverage and is only for a bounded
maintenance exception. `once` is for a disposable staging broker or automated
test; it retries a brief backup/manual overlap until one actual probe runs.
Production stays `periodic`.

### Stop / SIGTERM contract

`TimeoutStopSec=45s` leaves headroom for a draining canary (`BOARDD_WRITE_CANARY_TIMEOUT_S`
default 20s) before socket and DB teardown. SIGTERM/SIGINT start an ordered
shutdown on a dedicated coordinator thread so `BaseServer.shutdown()` never runs
on the main `serve_forever` thread (which would deadlock). Shutdown is
idempotent and always runs: canary join → server stop → DB-thread close → socket
unlink, then the process exits 0 without requiring SIGKILL.

## Rollback

If the new executable cannot become healthy, do not open the DB directly and do
not restore a database snapshot for this additive migration. Switch the runtime
symlink back, restore the old socket drop-ins, and restart once:

```bash
sudo scripts/fleet/rollback-boardd-runtime.sh
sudo systemctl daemon-reload
sudo systemctl restart boardd.service
```

Then verify the old endpoint and frozen hashes. The rollback script swaps
`current` and `previous` atomically and never restarts a service itself.
