# Fleet VPS2 attached-SSH Kanban workers

VPS2 is a dumb executor. The normal Kanban dispatcher on blitz remains the only
scanner, policy engine, claimer, capacity authority, and run writer. A card
assigned to `vps2-*` follows the same `dispatch_once()` path as every local card;
only `_default_spawn()` selects an attached SSH transport after the canonical
claim and run have been created.

There is no VPS2 dispatcher, broker RPC, host/PID comparison, independent claim
loop, or reconciler hook.

## Operator configuration

The transport is disabled by default. After the PR lands, Fable may enable the
narrow fleet contract in the dispatcher's existing `config.yaml`:

```yaml
kanban:
  vps2_ssh:
    enabled: true
    host: vps2
    user: root
    hermes_bin: /root/.local/bin/hermes
    boardd_sock: /run/boardd-blitz.sock
    workspace_root: /mnt/HC_Volume_106418160/fleet-workspaces
    log_root: /tmp
    path: /root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    connect_timeout_seconds: 15
    server_alive_interval_seconds: 15
    server_alive_count_max: 2
    start_timeout_seconds: 20
    start_grace_seconds: 0.1
    lease_interval_seconds: 1
    lease_timeout_seconds: 4
```

This is normal non-secret `config.yaml` state; there is no VPS2/HERMES
user-facing environment-variable surface. The remote worker still receives the
canonical task/run/claim/profile payload internally.

Prerequisites on VPS2:

- Profiles such as `/root/.hermes/profiles/vps2-eng1/` exist.
- `hermes_bin` is executable.
- The reverse-tunnel board socket exists at `boardd_sock`.
- The configured workspace root is writable by the SSH user.
- The card's canonical resolved workspace is below the dispatcher's
  `HERMES_KANBAN_WORKSPACES_ROOT`; its relative path is preserved below the
  configured VPS2 `workspace_root`. Unmappable workspaces fail closed.

## Lifecycle contract

1. `dispatch_once()` applies parent, continuation, respawn, global-capacity, and
   one-active-card-per-profile policy.
2. It atomically claims the card and creates its `task_runs` row.
3. `_default_spawn()` starts a local transport supervisor, which owns one
   foreground OpenSSH child with keepalives and writes lease pulses to it.
4. The remote shell verifies the canonical mapped workspace, Hermes executable,
   and boardd tunnel socket, then foreground-`exec`s a small Python supervisor.
   It never uses `nohup` or `&`; Hermes remains that supervisor's attached child.
5. Hermes itself emits a nonce-bearing readiness token through an inherited
   internal FD only after profile, credentials, and the real agent initialize.
   The remote and local supervisors relay that exact token fail-closed.
6. `_set_worker_pid()` persists the actual local OpenSSH PID and process identity in both
   `tasks` and `task_runs`, including the canonical `spawned` event.
7. If OpenSSH exits or stops delivering pulses, the local supervisor retains the
   same process group/lifecycle identity beyond the remote lease deadline. The
   remote supervisor kills Hermes on EOF or lease expiry, so even a one-way
   partition cannot make the canonical PID/group disappear before remote death.
8. Healthy workers that exceed claim TTL retain the normal live-PID extension;
   no duplicate remote worker is spawned.

Remote stdout/stderr is appended to `log_root/kanban-<task-id>.log` on VPS2.
Local SSH diagnostics continue to use the board's standard per-task log.

## Rollback

Set `kanban.vps2_ssh.enabled: false` and restart/reload the dispatcher config.
No new VPS2 workers will be claimed or spawned; `vps2-*` cards remain ready as
nonspawnable remote lanes. Existing attached SSH workers keep their canonical
run identity and should be allowed to finish or reclaimed through the existing
operator flow before disabling. Reverting the PR removes the transport entirely;
there is no separate service, helper install, timer, or reconciler hook to undo.
