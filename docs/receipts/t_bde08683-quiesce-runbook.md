# D4.5 quiesce runbook — board consumers and stale units

**CARD:** `t_bde08683`
**Shape:** receipt (plan only)
**Captured:** 2026-08-12T18:48:42Z (11:48 PDT) on `blitz-vps`
**Status:** PLAN — not executed. No timer, service, unit file, cron job, or board row was changed by this card.

This is the cutover runbook Fable runs in a window. A runbook that has already been executed is not reviewable. Do not treat any command below as having been run.

---

## 0. What "the board" is

| Role | Path / object | Notes |
|---|---|---|
| Authoritative SQLite | `/var/lib/boardd/fleet/kanban.db` (user `boardd`, mode denies `odai` `stat`) | Single-writer broker owns this file |
| Hermes pointer | `~/.hermes/kanban/boards/fleet/kanban.db` → symlink to the boardd file | `~/.hermes/kanban/current` = `fleet` (6 bytes, 2026-08-06) |
| Broker socket | `/run/boardd/boardd.sock` | `HERMES_KANBAN_BROKER=1` + `BOARDD_SOCK` |
| VPS2 reachability | `/run/boardd-blitz.sock` on `blitz-vps-2` | SSH `-R` from **blitz user** `boardd-tunnel-vps2.service` |
| Stale/wrong path still referenced | `boardd-intruder-watch.service` `BOARDD_DB=/home/odai/.hermes/kanban/boards/fleet/kanban.db` | Same inode via symlink, but the watch is not looking at the dedicated-uid file descriptors |

Deleting "the board" means: stop every writer, then remove or replace `/var/lib/boardd/fleet/kanban.db` (and sidecars `-wal`/`-shm`) **after** a fresh snapshot. This runbook stops at quiesce/repoint/retire. It does not delete the DB.

**Hard precondition (criterion 12, not this card):** take a fresh snapshot *in the window*, immediately before Step A. A 2026-08-12 09:07Z export already exists at `/mnt/HC_Volume_106418160/board-export-pre-deletion-20260812T090727Z/var_lib_boardd_fleet_kanban.db`. That is yesterday-morning relative to a deletion window. Re-export.

Suggested snapshot (run as the lander, not this session):

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST=/mnt/HC_Volume_106418160/board-export-pre-quiesce-$STAMP
mkdir -p "$DEST"
sudo -n sqlite3 /var/lib/boardd/fleet/kanban.db ".backup '$DEST/var_lib_boardd_fleet_kanban.db'"
sudo -n sqlite3 "$DEST/var_lib_boardd_fleet_kanban.db" "SELECT status, COUNT(*) FROM tasks GROUP BY status;"
sha256sum "$DEST/var_lib_boardd_fleet_kanban.db" | tee "$DEST/SHA256"
```

Do not copy the live file with `cp` while boardd is up (WAL tear). `.backup` is the consistent read.

---

## 1. Doctrine (why disable-alone is not quiesce)

A consumer is **not** quiesced unless all three are false:

1. `systemctl [--user] is-enabled UNIT` → `disabled` or `masked`
2. `systemctl [--user] is-active UNIT` → `inactive` (or `failed` that you then `reset-failed`)
3. no `timers.target.wants/` / `default.target.wants/` / `multi-user.target.wants/` symlink

A unit that is `disabled` + file still on disk **re-arms on next login / `enable` / mistaken `start timers.target`**. Criterion 14 calls this out by name. Quiesce = `disable --now`. Retire = `mask` + remove the unit file *in a later step*, after the window is proven.

`UnitFileState=disabled` with `preset: enabled` in `list-unit-files` means "disabled, but the vendor/preset would enable it." That is the "11 stale units" class.

**Wedged-but-enabled is its own class.** `workspace-reaper.timer` and `qb-snapshot.timer` are `is-enabled=enabled` with a wants symlink and `NextElapse=` empty (`infinity` / inactive). A `daemon-reload` or user-session recycle re-arms them. Treat them as live.

---

## 2. Evidence — what is live right now

### 2.1 The eight live fleet/kanban timers (card claim: confirmed)

Trinity captured 2026-08-12T18:48Z. All eight are `enabled` + `active` + wants symlink present.

| Timer | Cadence | ExecStart | Class |
|---|---|---|---|
| `kanban-dispatch.timer` | OnCalendar every minute (`*:*:00`) | `hermes kanban dispatch --max 16 --failure-limit 6` | **CREATE / CLAIM / SPAWN** |
| `fleet-board-reconciler.timer` | OnUnitActiveSec=60s | `fleet-board-reconciler` → also `hermes kanban dispatch` in a transient `qb-dispatch-*` unit | **CREATE / CLAIM / SPAWN / RECLAIM / COMMENT** |
| `fleet-heartbeat.timer` | 2 min | `fleet-heartbeat` | **WRITE** (`schedule`, `promote`, `comment`) + service resurrection |
| `fleet-keeper.timer` | 15 min | `fleet-keeper.sh` | **WRITE** (`promote` up to 25/tick). Land/clear is report-only |
| `fleet-page-render.timer` | 5 min | `godmode-bus/bin/render-page.py` | **READ** `kanban.db` via panel renderers |
| `fleet-buzz-ticker.timer` | 5 min | `fleet-buzz-ticker` | **NOT a board consumer** (see §4) |
| `fleet-skills-guard.timer` | 1 h | `fleet-skills-guard` | **NOT a board consumer** (see §4) |
| `fleet-gc.timer` | daily 07:17 | `fleet-gc.sh` with `BOARDD_SOCK=/run/boardd/boardd.sock` | **READ / FS mutate** (workspace walk keyed off board dirs) |

Positive-control probe (same `rg` that is used below for zeros):

```
rg -c -i 'hermes kanban|kanban\.db|BOARDD' /home/odai/.local/bin/fleet-heartbeat  → 5
rg -c -i 'hermes kanban|kanban\.db|BOARDD' /home/odai/godmode-bus/bin/render-tiles.py → 1
rg -c -i 'hermes kanban|kanban\.db|BOARDD' /home/odai/.local/bin/fleet-skills-guard → 0
rg -c -i 'hermes kanban|kanban\.db|BOARDD' /home/odai/.local/bin/status-ticker → 0
rg -c -i 'hermes kanban|kanban\.db|BOARDD' /home/odai/.local/bin/merge-accelerator → 0
```

### 2.2 Live board consumers the "8 timers" framing misses

These are live **right now** and will break or refill a deletion if left up.

| Unit / job | Host | Scope | What it does to the board |
|---|---|---|---|
| `boardd.service` | blitz | system, enabled+active since 2026-08-05 | Single-writer broker. **Write canary** (`BOARDD_WRITE_CANARY_MODE=periodic`, every 300s) **creates and archives** `[SYSTEM CANARY]` tasks |
| `boardd-fence.service` | blitz | system, enabled+active | Kills any non-boardd holder of `/var/lib/boardd/fleet/kanban.db` |
| `boardd-tunnel-vps2.service` | blitz | user, enabled+active | `ssh -R /run/boardd-blitz.sock:/run/boardd/boardd.sock root@100.101.99.62` |
| `vps2-dispatch.timer` | **vps2** | system, enabled+active, wants present, last fire ~39s before sample | `hermes kanban dispatch --max 24 --failure-limit 6` via `BOARDD_SOCK=/run/boardd-blitz.sock` |
| `boardd-intruder-watch.timer` | blitz | user, enabled+active | Reads fd holders of the fleet db path; fails the unit if an "intruder" exists |
| `boardd-traverse-guard.timer` | blitz | user, enabled+active, every 20s | `chmod o+x ~/.hermes` so user `boardd` can traverse. Not a DB client |
| `hermes-gateway.service` | blitz | user, enabled+active | `dispatch_in_gateway: false` in `~/.hermes/config.yaml`. Still pinned to fleet via drop-in (`HERMES_KANBAN_BROKER=1`, `HERMES_KANBAN_DB=.../fleet/kanban.db`, `BOARDD_SOCK=/run/boardd/boardd.sock`). Slash `/kanban` and session tools can still **create/comment** |
| `hermes-gateway-low.service` | blitz | user, enabled+active | Same Hermes gateway family |
| `hermes-serve.service` | blitz | user, enabled+active, `:9119` | Dashboard backend. Query-cap drop-in exists *because* the dashboard reads `task_events` through the broker. Mutations via UI: **not fully classified** (§8) |
| `hermes.service` (user) | blitz | enabled, **inactive** | Duplicate dashboard unit; wants in `default.target.wants/`. Re-arm risk |
| `workspace-reaper.timer` | blitz | enabled+active(elapsed), **NextElapse=infinity**, last fire 2026-08-11 23:23 | `hermes kanban ls` then deletes workspaces whose ids are not open. **If the board is gone, `ls` is empty → it will reap every workspace** |
| `vps2-tunnel-watch.timer` | blitz | enabled+active | `hermes kanban --board fleet show` locally and over ssh to vps2 |
| `qb-snapshot.timer` | blitz | **enabled + wants + inactive**, last fire 2026-08-06 11:59 | Does **not** read the board (reads `QB-*` bus files + `systemctl show boardqb`). Re-arm risk |
| user crontab `*/3 lane-health` | blitz | cron | Read-only boardd client (status ages). No set_status/promote/create in the script |
| user crontab `*/5 wave-gate-watch.sh` | blitz | cron | GitHub PR state only. Not a board consumer |
| user crontab `*/3 sol-dispatcher.sh` | blitz | cron | `~/permit-work` Codex slots. Not the fleet board |
| Buzz seats (`buzz-seat-*-fleet`, `buzz-acp-hermes*`) | blitz | user, enabled+active | ACP relays. They do not dispatch, but a seat with Hermes tools can write the board through the gateway pin |

`systemctl --user --machine=odai@` on vps2 returned empty. VPS2 **user-scope** timers are **unclassified** (§8).

Hermes cron: 21 jobs, **6 enabled**. None of the six is a board writer. `fleet-board-canvas` (`01fd0e93fc19`, `*/10 * * * *`, script `~/.hermes/scripts/fleet_board_canvas.py`) is **enabled=False**. Last cron output dir is from 2026-08-06. That is the Buzz replica named on card `t_d52f8b58`.

### 2.3 Disabled-but-still-present unit files

Card said **11**. Live `list-unit-files --state=disabled` matching board/fleet/kanban/hermes/buzz/gate/conc is **17**. The card's 11 is a subset. An omitted stale unit is how a disabled writer re-arms. All 17:

| Unit | Board I/O if started | Disposition |
|---|---|---|
| `kanban-dispatch` family is live, not in this table | — | — |
| `fleet-reviewed-land.timer` | `BOARDD_SOCK` + `fleet-reviewed-land` (mechanical merge + board) | **RETIRE** (already disabled 2026-08-12; file remains) |
| `fleet-board-sync.timer` | comments / heartbeat / `promote --force` / `claim` / `unblock` | **RETIRE** |
| `board-marshal.timer` + `board-marshal.service` (static) | `board-marshal-tick` (routes cards) | **RETIRE** |
| `boardqb.service` | daemon, `HERMES_KANBAN_BROKER=1`, cycle 60s | **RETIRE** (or keep masked until a successor QB exists) |
| `boardqb-probe.timer` | liveness probe for boardqb | **RETIRE** with boardqb |
| `flame-board-tick.timer` | `hermes -p orchestrator cron tick` — can fire orchestrator jobs that write the board | **RETIRE** |
| `fleet-activity-watchdog.timer` | `hermes kanban --board ultra-chad-fleet list` (legacy board name) | **RETIRE** |
| `fleet-autoland2.timer` | GitHub lander; `AUTOLAND_ENACT=0` today | **RETIRE** (two-key arming is not a substitute for mask) |
| `fleet-disk-janitor.timer` | disk hygiene; no board verbs in unit | **RETIRE** or leave as non-board (classify: not a board consumer) |
| `fleet-workspace-reclaim.timer` | prune git-clone workspaces | **RETIRE** or keep only after workspace-reaper is redesigned off the board |
| `launch-blockers-sync.timer` | `launch-blockers --sync` **writes derived tags onto the board** | **RETIRE** |
| `conc-ticker.timer` | read-only boardd → `CONC-LEDGER.psv` | **RETIRE** or **REPOINT** at a ledger |
| `fable-gate-sweep.timer` | boardd client (`PYTHONPATH=/tmp/kanban-boardd`) | **RETIRE** |
| `hermes-serve-rss-sampler.{timer,service}` | RSS of hermes-serve cgroup | not a board consumer; optional retire |
| `permits-dispatcher.service` | Permit scrape / Supabase | not a board consumer |
| `buzz-seat-nous-fleet.service` | unused Buzz seat | not a board consumer |

`kanban-sync.py` is already a permanent no-op (boardd cutover). Leave the file; do not unmask any old timer for it.

---

## 3. Per-consumer actions

Convention for every command block:

- **Quiesce** = stop firing, leave the unit file.
- **Repoint** = keep the process, change its source of truth off the board.
- **Retire** = mask + later delete the unit file. Do retire only after the window's quiesce checks pass.

Rollback for every `disable --now` is `enable --now` of the same unit (requires the file still present — that is why retire is a later step).

### 3.1 `kanban-dispatch.timer` — QUIESCE first

- **Reads:** fleet board via broker (implicit; `hermes kanban dispatch`).
- **Writes:** claim, spawn workers, reclaim stale, promote ready, auto_decompose (config `kanban.auto_decompose: true`, up to 3 children per dispatch tick).
- **If the board disappears while this is live:** dispatch errors every minute; any leftover worker still heartbeats into a missing DB; a later `kanban init` / schema import **recreates** a board and this timer fills it.
- **Quiesce:**

```bash
systemctl --user disable --now kanban-dispatch.timer
systemctl --user stop kanban-dispatch.service || true
```

- **Check:**

```bash
systemctl --user is-enabled kanban-dispatch.timer   # disabled
systemctl --user is-active kanban-dispatch.timer    # inactive
test ! -e ~/.config/systemd/user/timers.target.wants/kanban-dispatch.timer && echo wants=gone
journalctl --user -u kanban-dispatch.service --since '2 min ago' --no-pager | tail
# expect: no new 'Starting Kanban dispatch pulse' after the disable timestamp
```

- **Rollback:** `systemctl --user enable --now kanban-dispatch.timer`

### 3.2 `fleet-board-reconciler.timer` — QUIESCE first (same wave as 3.1)

- **Reads:** live PIDs, `/var/lib/boardd/fleet/kanban.db` (status SQL), `hermes kanban`.
- **Writes:** reclaim, unblock, comment (`STALE-BLOCKER`), **dispatch** via `systemd-run --user --unit=qb-dispatch-<ns>`.
- **If the board disappears:** 60s loop of broker errors; if it comes back, this is the unit that **re-spawns workers behind you**.
- **Quiesce:**

```bash
systemctl --user disable --now fleet-board-reconciler.timer
systemctl --user stop fleet-board-reconciler.service || true
# reap any in-flight transient dispatcher
systemctl --user list-units --type=service --no-pager | rg 'qb-dispatch-' || echo 'no qb-dispatch transients'
```

- **Check:** `is-enabled=disabled`, `is-active=inactive`, wants symlink gone, no `qb-dispatch-*` units, `journalctl --user -u fleet-board-reconciler.service --since '2 min ago'` has no new `PASS live=` line.
- **Rollback:** `systemctl --user enable --now fleet-board-reconciler.timer`

### 3.3 `vps2-dispatch.timer` — QUIESCE first (same wave, **other host**)

- **Host:** `blitz-vps-2` / `100.101.99.62`. System unit `/etc/systemd/system/vps2-dispatch.{timer,service}`. Wants: `/etc/systemd/system/timers.target.wants/vps2-dispatch.timer`.
- **Reads/writes:** same `hermes kanban dispatch --max 24` through `BOARDD_SOCK=/run/boardd-blitz.sock`.
- **If skipped:** stopping blitz dispatch is cosmetic. VPS2 will keep claiming `vps2-eng*` cards and will **recreate work** the moment a board exists.
- **Quiesce (on vps2, as root):**

```bash
ssh -o BatchMode=yes -i /home/odai/.ssh/blitz-vps root@100.101.99.62 \
  'systemctl disable --now vps2-dispatch.timer; systemctl stop vps2-dispatch.service || true'
```

- **Check:**

```bash
ssh -o BatchMode=yes -i /home/odai/.ssh/blitz-vps root@100.101.99.62 \
  'systemctl is-enabled vps2-dispatch.timer; systemctl is-active vps2-dispatch.timer; \
   test ! -e /etc/systemd/system/timers.target.wants/vps2-dispatch.timer && echo wants=gone; \
   journalctl -u vps2-dispatch.service --since "2 min ago" --no-pager | tail'
```

- **Rollback:** same ssh, `systemctl enable --now vps2-dispatch.timer`

### 3.4 `fleet-heartbeat.timer` — QUIESCE immediately after spawners

- **Writes:** `hermes kanban --board fleet schedule|promote|comment`. Also resurrects other systemd units (can **undo a disable** if those units are still enabled).
- **If left up:** it will `promote` parked cards back to ready, which the next forgotten dispatcher will spawn.
- **Quiesce:** `systemctl --user disable --now fleet-heartbeat.timer`
- **Check:** trinity + `journalctl --user -u fleet-heartbeat.service --since '3 min ago'` has no `refloat` / `schedule` lines after T0.
- **Rollback:** `systemctl --user enable --now fleet-heartbeat.timer`
- **Note:** inspect the script before the window if you rely on it to keep `hermes-serve` / tunnels alive. After disable, those have their own units.

### 3.5 `fleet-keeper.timer` — QUIESCE with heartbeat

- **Writes:** `hermes kanban --board fleet promote` (cap 25). Land path is report-only (`[REPORT] landable …`).
- **If left up:** dependency-clear cards return to ready → refill.
- **Quiesce:** `systemctl --user disable --now fleet-keeper.timer`
- **Check:** trinity; journal has no `keeper: promoted N` with N>0 after T0.
- **Rollback:** `systemctl --user enable --now fleet-keeper.timer`

### 3.6 `workspace-reaper.timer` — QUIESCE before the board can go empty

- **Reads:** `hermes kanban ls`, keeps ids that are not `done`/`archived`.
- **Writes:** filesystem only (`~/.hermes/kanban/boards/fleet/workspaces/*`).
- **If the board disappears first:** `ls` fails or returns empty → **every workspace looks completed → mass delete**. This is the highest-blast *reader*.
- **State today:** enabled, active(elapsed), `NextElapseUSecMonotonic=infinity` (wedged since 2026-08-11 23:47). Disable it anyway; a reload re-arms it.
- **Quiesce:** `systemctl --user disable --now workspace-reaper.timer`
- **Check:** trinity; `systemctl --user show workspace-reaper.timer -p NextElapseUSecMonotonic` is irrelevant once disabled.
- **Rollback:** `systemctl --user enable --now workspace-reaper.timer`

### 3.7 `fleet-gc.timer` — QUIESCE (same reason as reaper)

- **Reads:** board workspace dirs + `kanban.db` readability; env pins `BOARDD_SOCK`.
- **Writes:** disk (clone GC, sdb reclaim). `FLEET_SDB_RECLAIM_APPLY=1`.
- **If the board is gone:** skips board-dir branches that fail the `kanban.db` readable test; still walks `/tmp` clones. Safer than the reaper, but it is a board-path walker.
- **Quiesce:** `systemctl --user disable --now fleet-gc.timer` (or leave it if you want disk GC during a long window — then accept it will skip the fleet board tree).
- **Recommended for a deletion window:** disable.
- **Check / rollback:** standard trinity / `enable --now`.

### 3.8 `fleet-page-render.timer` — QUIESCE or REPOINT

- **Reads:** `/var/lib/boardd/fleet/kanban.db` from `render-tiles.py`, `render-receipts.py`, `render-rescues.py`, `render-sessions.py`, `render-landqueue.py`. Other panels (waves/gates/mobile) are ledgers/bus.
- **Writes:** the fleet HTML page under godmode-bus (not the board).
- **If the board disappears:** those panels error; `render-page.py` rolls the page back on validation failure (non-zero). The public fleet page freezes on last-good content or goes stale.
- **Quiesce (safe for the window):** `systemctl --user disable --now fleet-page-render.timer`
- **Repoint (if the page must keep ticking):** temporarily drop the five kanban.db renderers from `RENDERERS` in `godmode-bus/bin/render-page.py` and keep the ledger panels. That is a code change — do it on a branch, not as a live edit in the window unless you already have the patch.
- **Check:** trinity; next 5 minutes of journal show no `render-page.py` start.
- **Rollback:** `enable --now`.

### 3.9 `vps2-tunnel-watch.timer` — QUIESCE

- **Reads:** `hermes kanban --board fleet show <card>` on blitz and on vps2.
- **Writes:** none to the board (watch script).
- **If the board disappears:** fail every 5 min (noise, not refill).
- **Quiesce:** `systemctl --user disable --now vps2-tunnel-watch.timer`
- **Rollback:** `enable --now`

### 3.10 `boardd-intruder-watch.timer` — QUIESCE after writers, before boardd stop

- **Reads:** fd table for `BOARDD_DB`.
- **Writes:** alert file `~/.hermes/kanban/boardd-INTRUDER-ALERT`.
- **If boardd is stopped first:** the watch may alarm (no MainPID / unexpected holders) and trip `--failed` watchdogs.
- **Quiesce:** `systemctl --user disable --now boardd-intruder-watch.timer`
- **Rollback:** `enable --now`

### 3.11 `boardd-traverse-guard.timer` — QUIESCE with boardd (or leave)

- **Reads/writes:** `chmod o+x /home/odai/.hermes` only.
- **If left up after boardd is retired:** harmless chmod every 20s.
- **If boardd stays as a broker for a successor empty board:** keep it.
- **For a full boardd stop:** disable to reduce noise. `systemctl --user disable --now boardd-traverse-guard.timer`
- **Rollback:** `enable --now`

### 3.12 `boardd-tunnel-vps2.service` — QUIESCE after vps2-dispatch is proven dead

- **Reads/writes:** none locally; it is the pipe vps2 uses to write.
- **If you drop the tunnel before vps2-dispatch is disabled:** vps2 dispatch will error (good) but also any leftover vps2 worker cannot heartbeat (looks like a dead card).
- **Quiesce:** `systemctl --user disable --now boardd-tunnel-vps2.service`
- **Check:** `is-active=inactive`; on vps2, `ss -xl | rg boardd-blitz` shows no socket, or a stale unlinked socket.
- **Rollback:** `systemctl --user enable --now boardd-tunnel-vps2.service` then confirm `ssh` forward is up (`systemctl --user is-active boardd-tunnel-vps2.service`).

### 3.13 `boardd-fence.service` — QUIESCE immediately before boardd

- **Reads:** `/var/lib/boardd/fleet/kanban.db` fd holders.
- **Writes:** SIGKILL to non-boardd PIDs + `/var/lib/boardd/fleet/boardd-FENCE.log`.
- **If left up while you snapshot or sqlite3 the file as root:** the fence may kill your snapshot process.
- **Quiesce (system):** `sudo systemctl disable --now boardd-fence.service`
- **Check:** `systemctl is-enabled boardd-fence.service` → disabled; `is-active` → inactive; `test ! -e /etc/systemd/system/multi-user.target.wants/boardd-fence.service`
- **Rollback:** `sudo systemctl enable --now boardd-fence.service`

### 3.14 `boardd.service` — QUIESCE last among writers

- **Reads/writes:** the DB. Periodic write canary **creates + archives** canary tasks. `ExecStartPre` chmod on `~/.hermes`. Condition: `ConditionPathIsReadWrite=/var/lib/boardd/fleet`.
- **If you delete the DB while boardd is up:** canary / clients recreate schema (`--import-schema`) and you get a **new empty board that automation will refill**.
- **If you stop boardd while dispatchers are live:** they error, then succeed and refill when you start it again.
- **Quiesce:**

```bash
sudo systemctl disable --now boardd.service
# confirm the process is gone
systemctl is-active boardd.service          # inactive
pgrep -af '/opt/hermes-boardd/current/libexec/boardd.py' || echo 'no boardd py'
test ! -e /etc/systemd/system/multi-user.target.wants/boardd.service && echo wants=gone
# socket should disappear with RuntimeDirectory=boardd
test ! -S /run/boardd/boardd.sock && echo sock=gone
```

- **Rollback:** `sudo systemctl enable --now boardd.service` then `systemctl is-active boardd.service` + `test -S /run/boardd/boardd.sock` + one `HERMES_KANBAN_BROKER=1 BOARDD_SOCK=/run/boardd/boardd.sock hermes kanban --board fleet stats`.

### 3.15 `hermes-gateway.service` / `hermes-gateway-low.service` — QUIESCE or accept residual writes

- **Reads/writes:** broker-pinned. `kanban.dispatch_in_gateway: false` so the gateway is **not** a dispatcher today. It still exposes `/kanban` and session tool use.
- **If left up:** any human or Buzz seat can `create` / `comment` / `set-status` during the window. That is enough to break "the board is gone" if boardd is still up, and enough to error-spam if boardd is down.
- **Recommended for a deletion window:** disable both gateways **after** spawners, **before** boardd stop, or accept §8 residual.
- **Quiesce:**

```bash
systemctl --user disable --now hermes-gateway.service hermes-gateway-low.service
```

- **Check:** both `inactive`; Buzz ACP seats will degrade (messages queue / fail). That is expected.
- **Rollback:** `systemctl --user enable --now hermes-gateway.service hermes-gateway-low.service`
- **Do not** flip `dispatch_in_gateway` to true as a "fix."

### 3.16 `hermes-serve.service` + `hermes-serve-watchdog.timer` — QUIESCE or leave

- **Reads:** dashboard diagnostics against the broker (`BOARDD_MAX_QUERY_ROWS=1000000` exists for this).
- **Writes:** not proven. Treat UI mutations as **unclassified** (§8).
- **If left up after boardd stop:** dashboard 500s (already seen historically when the broker cap was too low).
- **Quiesce (if you want a quiet dashboard):**

```bash
systemctl --user disable --now hermes-serve-watchdog.timer
systemctl --user disable --now hermes-serve.service
# also the inactive duplicate, so it cannot come back on login:
systemctl --user disable hermes.service
```

- **Check:** `curl -sS -o /dev/null -w '%{http_code}\n' --max-time 3 http://127.0.0.1:9119/` → connection refused (or whatever bind you use; live bind is `100.114.23.95:9119`).
- **Rollback:** `enable --now hermes-serve.service hermes-serve-watchdog.timer`

### 3.17 `lane-health` cron — QUIESCE

- **Reads:** boardd (read-only ages).
- **Writes:** `~/godmode-bus/logs/lane-health.log` only.
- **Quiesce:** comment out or delete the `*/3 * * * * ... lane-health` line from `crontab -e`. Do not `crontab -r`.
- **Check:** `crontab -l | rg lane-health` empty; no new lines in the log after T0.
- **Rollback:** restore the line.

### 3.18 `kb-setstatus.py` — not a daemon; FREEZE by procedure

- **Path:** `~/godmode-bus/bin/kb-setstatus.py` (not `~/.local/bin`).
- **Writes:** `kb_client.set_status` through the broker.
- **Quiesce:** no unit. During the window, do not run it. Optional hard freeze (reversible):

```bash
chmod a-x /home/odai/godmode-bus/bin/kb-setstatus.py
```

- **Check:** `test ! -x /home/odai/godmode-bus/bin/kb-setstatus.py`
- **Rollback:** `chmod a+x /home/odai/godmode-bus/bin/kb-setstatus.py`

### 3.19 Buzz replica (`fleet_board_canvas.py` / card `t_d52f8b58`) — already quiesced; RETIRE the job

- **Reads:** `hermes kanban --board fleet list`.
- **Writes:** Buzz canvas on channel configured in the script (via `buzz canvas set`). Not the board.
- **State:** Hermes cron job `01fd0e93fc19` `enabled=False`. Last fires 2026-08-06.
- **If someone re-enables the job after deletion:** canvas goes empty/fails every 10 min.
- **Retire:** leave `enabled=False`. Optionally delete the job from `~/.hermes/cron/jobs.json` *in the window* (gateway rewrite risk — prefer `hermes cron` CLI if you touch it). Do not start `flame-board-tick.timer`.
- **Check:**

```bash
python3 - <<'PY'
import json
from pathlib import Path
jobs=json.loads(Path.home().joinpath('.hermes/cron/jobs.json').read_text())['jobs']
# jobs may be list or dict
items=jobs.values() if isinstance(jobs, dict) else jobs
for j in items:
    if j.get('id')=='01fd0e93fc19' or j.get('name')=='fleet-board-canvas':
        print('enabled', j.get('enabled'), 'sched', j.get('schedule'))
PY
```

Expect `enabled False`.

- **Rollback:** re-enable that cron job only after a successor surface exists.

### 3.20 `fleet-buzz-ticker.timer` — LEAVE or REPOINT (not a board consumer)

- **Reads:** `~/godmode-bus/STATUS-TICKER.md`, `~/godmode-bus/LIVE-LOG.md`.
- **Writes:** Buzz `#fleet` via `buzz messages send`.
- **If the board disappears:** ticker keeps posting derived ledger status. That is the intended successor signal.
- **Action:** leave running. Service is currently `failed` (last oneshot failed) but the timer is live and will keep trying.
- **Do not** treat this as the Buzz *board replica*. That is §3.19.

### 3.21 `fleet-skills-guard.timer` — LEAVE

Zero hits for `hermes kanban|kanban.db|BOARDD` against a probe that found 5 hits in `fleet-heartbeat`. Filesystem skills integrity only.

### 3.22 `status-ticker.timer` — LEAVE

Same zero-hit probe. Writes `~/godmode-bus/STATUS-TICKER.md` from ledgers.

### 3.23 `qb-snapshot.timer` / QB dashboard — REPOINT already; disable to kill re-arm

- **Reads:** bus `QB-*` files + `systemctl --user show boardqb`. Does not open the board or the broker (script docstring + grep).
- **Writes:** `/var/www/qb/qb-snapshot.json` (stale since 2026-08-06 11:59).
- **Action:** `systemctl --user disable --now qb-snapshot.timer` so the enabled+wants re-arm cannot come back. Leave `/var/www/qb/index.html` as a static last snapshot, or later repoint the page at a ledger.
- **Rollback:** `enable --now`.

### 3.24 Stale disabled writers — RETIRE (mask) after live spawners are down

Do this as one block so a `daemon-reload` cannot revive them mid-window.

```bash
# user-scope board writers / board-tied automation that are already disabled
for u in \
  fleet-reviewed-land.timer \
  fleet-board-sync.timer \
  board-marshal.timer \
  boardqb.service \
  boardqb-probe.timer \
  flame-board-tick.timer \
  fleet-activity-watchdog.timer \
  fleet-autoland2.timer \
  launch-blockers-sync.timer \
  conc-ticker.timer \
  fable-gate-sweep.timer \
  fleet-workspace-reclaim.timer
 do
  systemctl --user disable --now "$u" 2>/dev/null || true
  systemctl --user mask "$u"
done
systemctl --user daemon-reload
systemctl --user list-unit-files --state=masked | rg 'fleet-|board|kanban|flame-board|launch-blockers|conc-ticker|fable-gate|boardqb'
```

- **Check:** each `is-enabled` → `masked`; no wants symlink; `systemctl --user start fleet-board-sync.timer` is refused.
- **Rollback (per unit):** `systemctl --user unmask UNIT && systemctl --user enable --now UNIT` (only unmask what you intend to bring back).

`fleet-disk-janitor.timer`, `hermes-serve-rss-sampler.*`, `permits-dispatcher.service`, `buzz-seat-nous-fleet.service` are **not** board writers. Masking them is optional hygiene, not a cutover requirement.

### 3.25 `hermes-serve-watchdog` / `disk-watchdog` / `merge-accelerator`

- `disk-watchdog.service` (system): the `kanban-boardd-run` string is a **protected glob** (do not delete that scratch). Not a board client. Leave it.
- `merge-accelerator.timer`: GitHub `update-branch` only. Zero board hits. Leave it.
- `hermes-serve-watchdog.timer`: only relevant if `hermes-serve` stays up.

---

## 4. Ordering (the part that prevents respawn)

Rule: **nothing that creates or promotes cards may still be armed when anything that drains, reaps, or deletes begins.** A consumer that re-creates cards must stop before anything that drains them.

```
T0  Snapshot DB (.backup). Record SHA256 + status counts.
T1  FREEZE SPAWNERS (parallel, both hosts) — do these before anything else
      blitz:  kanban-dispatch.timer
      blitz:  fleet-board-reconciler.timer   (+ reap qb-dispatch-*)
      vps2:   vps2-dispatch.timer
T2  FREEZE RE-PROMOTERS
      fleet-heartbeat.timer
      fleet-keeper.timer
T3  Wait one full cadence (90s) and prove no new dispatch/promote journal lines
      AND no new running cards (card-liveness / hermes kanban stats)
T4  FREEZE DANGEROUS READERS (they mutate disk from board contents)
      workspace-reaper.timer
      fleet-gc.timer
T5  QUIESCE NOISY READERS
      fleet-page-render.timer
      vps2-tunnel-watch.timer
      lane-health crontab line
      optional: hermes-serve + watchdog; hermes-gateway*
T6  MASK stale writers (§3.24) so disable-but-present cannot re-arm
T7  DROP THE PIPE
      boardd-tunnel-vps2.service
T8  DROP THE GUARDS, THEN THE BROKER
      boardd-intruder-watch.timer
      boardd-fence.service
      boardd-traverse-guard.timer     (optional)
      boardd.service                  (write canary dies here)
T9  TOOL FREEZE
      chmod a-x kb-setstatus.py
T10 Only after T1–T9 checks pass: board delete / successor cutover (NOT this runbook)
```

**Do not** stop boardd before T1–T3. Stopping the broker first just makes every still-enabled timer fail-and-retry; when boardd returns they all fire at once.

**Do not** stop `workspace-reaper` after the board is empty. Stop it while `hermes kanban ls` still returns the open set, or while it is already wedged but *disabled*.

**Do not** disable `boardd-fence` while you still have non-boardd writers you have not stopped — the fence is the last backstop against a rogue sqlite.

---

## 5. Window check script (run after T3 and again after T8)

Positive control is baked in: the script fails if `kanban-dispatch.timer` is still enabled (we know that unit exists and was live at capture).

```bash
#!/usr/bin/env bash
set -euo pipefail
fail=0
ck() { # ck <host-label> <cmd...>
  echo -n "$1: "
  if eval "$2"; then echo OK; else echo FAIL; fail=1; fi
}

# spawners must be dead
ck dispatch-enabled "[[ \$(systemctl --user is-enabled kanban-dispatch.timer) == disabled || \$(systemctl --user is-enabled kanban-dispatch.timer) == masked ]]"
ck dispatch-active  "[[ \$(systemctl --user is-active kanban-dispatch.timer) == inactive ]]"
ck dispatch-wants   "[[ ! -e ~/.config/systemd/user/timers.target.wants/kanban-dispatch.timer ]]"
ck reconciler-en    "[[ \$(systemctl --user is-enabled fleet-board-reconciler.timer) != enabled ]]"
ck heartbeat-en     "[[ \$(systemctl --user is-enabled fleet-heartbeat.timer) != enabled ]]"
ck keeper-en        "[[ \$(systemctl --user is-enabled fleet-keeper.timer) != enabled ]]"
ck reaper-en        "[[ \$(systemctl --user is-enabled workspace-reaper.timer) != enabled ]]"
ck qb-snap-en       "[[ \$(systemctl --user is-enabled qb-snapshot.timer) != enabled ]]"

# vps2
ck vps2-dispatch "ssh -o BatchMode=yes -o ConnectTimeout=8 -i /home/odai/.ssh/blitz-vps root@100.101.99.62 \
  '[[ \$(systemctl is-enabled vps2-dispatch.timer) != enabled ]] && [[ \$(systemctl is-active vps2-dispatch.timer) == inactive ]]'"

# no transient dispatcher
ck no-qb-dispatch "! systemctl --user list-units --type=service --no-pager | rg -q 'qb-dispatch-'"

# after T8 only:
# ck boardd "[[ \$(systemctl is-active boardd.service) == inactive ]]"
# ck sock   "[[ ! -S /run/boardd/boardd.sock ]]"

exit $fail
```

Save as `/tmp/t_bde08683-quiesce-check.sh` in the window. Do not ship it as a standing timer.

---

## 6. Full-window rollback

Reverse order. Never enable dispatchers while boardd is down.

```
R1  sudo systemctl enable --now boardd.service
    prove: is-active + sock + hermes kanban stats
R2  sudo systemctl enable --now boardd-fence.service
R3  systemctl --user enable --now boardd-traverse-guard.timer boardd-intruder-watch.timer
R4  systemctl --user enable --now boardd-tunnel-vps2.service
    prove: ssh to vps2 can `timeout 15 hermes kanban --board fleet stats` via BOARDD_SOCK=/run/boardd-blitz.sock
R5  unmask any unit you masked and do **not** enable spawners yet
R6  restore hermes-serve / gateways / page-render / lane-health if you stopped them
R7  LAST: enable spawners
      systemctl --user enable --now fleet-keeper.timer fleet-heartbeat.timer
      systemctl --user enable --now fleet-board-reconciler.timer kanban-dispatch.timer
      ssh vps2: systemctl enable --now vps2-dispatch.timer
R8  chmod a+x ~/godmode-bus/bin/kb-setstatus.py
```

If you deleted unit files, rollback is restore from this receipt + git history of `~/.config/systemd/user/` (not in this repo). **Do not delete unit files in the first window.** Mask is enough.

---

## 7. What to leave running

These are live and **classified as non-consumers** of the fleet board (zeros proven in §2.1):

| Unit | Why it can stay |
|---|---|
| `fleet-skills-guard.timer` | skills symlink integrity |
| `status-ticker.timer` | ledger → `STATUS-TICKER.md` |
| `fleet-buzz-ticker.timer` | posts that ledger to Buzz `#fleet` |
| `disk-watchdog.timer` | disk tiers; protects a *directory name*, does not open the DB |
| `merge-accelerator.timer` | GitHub only |
| `hermes-quota-bridge.timer` and other quota/bridge timers | provider meters |
| enabled Hermes cron (token keepalive, disk janitor, provider check, graphify, PermitStack, release-watch) | no board verbs |

Leaving `fleet-buzz-ticker` up is the cheap **repoint**: status stays visible after the board is gone.

---

## 8. Unclassified (these are the ones that break the cutover)

An unclassified consumer is worse than a live classified one. Do not delete the board while any row below is still open.

| Residual | Why it is unclassified | How to close it in the window |
|---|---|---|
| **Hermes session standing order** (`~/.hermes/config.yaml` "EVERYTHING EVERYWHERE gets tracked on the fleet kanban", `hermes kanban --board fleet create`) | No unit. Every live profile can create a card the moment boardd exists | Announce a create-freeze. Optionally rename `~/.local/bin/hermes` to `hermes.real` and install a wrapper that rejects `kanban create` / `dispatch`. Wrapper is the only mechanical close |
| **Gateway `/kanban` + tool use** | `dispatch_in_gateway: false` is proven; create/comment paths were not exhaustively traced through `gateway.py` | Disable both gateway units (§3.15) or accept residual |
| **`hermes-serve` dashboard mutations** | No `pages/Kanban*.tsx` in the live tree; broker read path is proven; write path not proven | Disable serve + watchdog, or treat as residual |
| **VPS2 user-scope timers** | `systemctl --user --machine=odai@` on vps2 returned empty. Only the **system** `vps2-dispatch.timer` was inventoried | On vps2, as `odai`, run the same `list-timers` / `list-unit-files` sweep this card ran on blitz. Until that output exists, assume there is a second dispatcher |
| **Buzz seats with Hermes tools** | Units are ACP relays (`buzz-acp`); a seat can still invoke `hermes kanban` | Disable gateways, or disable the `buzz-seat-*-fleet` units for the window |
| **In-flight workers** | `ps` showed leftover workspace processes (e.g. `t_cd66fe2e` fake_endpoint / seat_watchdog) and many `/tmp/wave-d4-item1-*` scratch `boardd.py` pytest brokers | Criterion 10 / `card-liveness`. This runbook does not reclaim or kill workers. A live worker will try to heartbeat/comment after T8 and fail — that is OK. A live worker that still has a broker connection during T1–T7 can still write |
| **`auto_decompose: true`** | Fires *inside* dispatch. Stopping both dispatchers closes it. A manual `hermes kanban dispatch` does not | Wrapper in row 1 |
| **boardd `--import-schema` on next start** | Starting boardd against a missing DB creates a new board | After deletion, do not `enable --now boardd` until the successor path is decided |
| **`hermes kanban init`** | Idempotent create | Same wrapper / operator freeze |
| **Flame / orchestrator cron tick** | `flame-board-tick.timer` is disabled, but `hermes-gateway` ticks `jobs.json`. Enabled jobs are non-board today; a one-line enable of `fleet-board-canvas` or `AUTO strict broker watchdog` brings writers back | Keep those jobs `enabled=False`; do not start `flame-board-tick.timer` |

### Closed classifications (not residual)

| Item | Verdict |
|---|---|
| `kanban-sync.py` | Permanent no-op. Not a consumer |
| `fleet-board-canvas` cron | Disabled 2026-08-06. Not live |
| `sol-dispatcher` / `permits-dispatcher` / `wave-gate-watch` | Not fleet-board |
| `qb-snapshot` body | Not a board reader; only a re-arm risk |
| System `hermes.service` | `disabled` at system scope. User copy is the re-arm risk |

---

## 9. What this receipt does **not** authorize

- Stopping, masking, or deleting any unit (this session did not).
- Deleting `/var/lib/boardd/fleet/kanban.db`.
- Reclaiming or killing in-flight workers.
- Changing kanban/card status.
- Merging, deploying, production DB writes, credential rotation.

Criterion 10 (zero running/review cards, `card-liveness` oracle) and criterion 12 (fresh snapshot) remain **outside** this document's execution. This document is criterion 9.

---

## 10. Capture appendix (raw counts)

User timers matching `fleet|kanban|board` that were **active** at capture:

- live fire: `kanban-dispatch`, `fleet-board-reconciler`, `fleet-heartbeat`, `fleet-keeper`, `fleet-page-render`, `fleet-buzz-ticker`, `fleet-skills-guard`, `fleet-gc`, plus `boardd-intruder-watch`, `boardd-traverse-guard`
- enabled+wedged: `workspace-reaper.timer` (active/elapsed, next=infinity), `qb-snapshot.timer` (enabled, inactive, wants present)

System on blitz: `boardd.service` active, `boardd-fence.service` active.

System on vps2: `vps2-dispatch.timer` enabled+active, last fire ~39s before sample, wants present.

Disabled user units in the board/fleet/hermes/buzz/gate family: **17** (card said 11). Listed in §2.3.

No `/etc/cron.d` match for kanban/boardd. User crontab board reader: `lane-health` only.

```
DONE REPORT
CARD: t_bde08683
STATUS: receipt (plan only; nothing executed)
BRANCH: docs/t_bde08683-quiesce-runbook
HEAD: (filled at PR open)
FILES: docs/receipts/t_bde08683-quiesce-runbook.md
CHECKS: read-only inventory only. systemctl list-timers / list-unit-files / trinity / ssh vps2 unit cat / rg positive-control (fleet-heartbeat=5, render-tiles=1, skills-guard=0, status-ticker=0, merge-accelerator=0). No disable/stop/mask/rm of any unit.
SCOPE-FENCE: runbook text only. No unit, timer, cron, board row, or secret changed.
RISKS: VPS2 user-scope timers unclassified; gateway/session create-freeze is procedural unless a wrapper is installed; workspace-reaper will mass-delete workspaces if it fires against an empty/missing board; boardd --import-schema recreates a board if started after delete.
BLOCKED-ON: nothing for this receipt. Window execution is Fable's.
```
