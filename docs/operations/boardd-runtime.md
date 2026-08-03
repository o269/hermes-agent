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
