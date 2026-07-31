# Fleet board reconciler — VPS2 dumb-executor integration (R4)

Blitz remains the **sole dispatcher**. VPS2 workers are SSH-spawned from blitz;
the remote host runs `hermes chat` and reports back through the existing
reverse-tunnel boardd socket. No remote dispatcher, custody RPC, or cross-host
PID comparison.

## Install

After the PR lands, install the helper next to the reconciler:

```bash
install -m 0755 scripts/fleet/fleet-board-reconciler-vps2-worker \
  ~/.local/bin/fleet-board-reconciler-vps2-worker
```

## Reconciler hook

Add this block to `fleet-board-reconciler` **after** the local dispatch section
(section 4) and before the final stats log:

```bash
# ---- 4b. VPS2 dumb-executor dispatch (blitz SSH-spawn) -----------------------
VPS2_DISPATCH_HELPER=${VPS2_DISPATCH_HELPER:-/home/odai/.local/bin/fleet-board-reconciler-vps2-worker}
vps2_spawned=0
if [ "$GATE_CLASS" = "green" ] && [ -x "$VPS2_DISPATCH_HELPER" ]; then
  vps2_out=$("$VPS2_DISPATCH_HELPER" 2>&1) || true
  vps2_spawned=$(printf '%s' "$vps2_out" | sed -nE 's/^Spawned:[[:space:]]*([0-9]+).*/\1/p' | head -1)
  printf '%s' "$vps2_out" | sed 's/^/  VPS2-DISPATCH /' >> "$LOG"
fi
```

Update the final PASS log line to include `vps2_spawned=${vps2_spawned:-0}`.

## Rollback

Remove the section 4b block (or unset `VPS2_DISPATCH_HELPER`). Blitz continues
dispatching local lanes only; vps2-* cards remain ready until re-enabled.

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `VPS2_SSH_HOST` | `vps2` | SSH target host |
| `VPS2_SSH_USER` | `root` | SSH user |
| `VPS2_HOST_LOCAL_MAX` | `4` | Concurrent vps2 workers |
| `VPS2_GLOBAL_MAX` | `20` | Fleet-wide running cap |
| `VPS2_REMOTE_BOARDD_SOCK` | `/run/boardd-blitz.sock` | Tunnel socket on vps2 |
| `BOARDD_SOCK` | blitz boardd | Claim authority stays on blitz |

## Liveness

`claim_lock` encodes the **local SSH session PID** on blitz:
`blitz-vps:vps2-ssh:vps2-eng1:<ssh_pid>`. The reconciler heartbeats while the
SSH process is alive; when it dies (worker done, crash, or tunnel drop), the
existing stale-claim reaper releases the card.
