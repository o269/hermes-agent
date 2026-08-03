# boardd restart-safe runtime

This package moves the broker's executable runtime out of `/home` and into an
immutable, versioned release under `/opt/hermes-boardd`. The systemd unit runs as
the dedicated `boardd` principal with `ProtectHome=yes`, owns only
`/var/lib/boardd`, and exposes its group-readable Unix socket at
`/run/boardd/boardd.sock`.

> **Custody:** installing or activating a release, changing a system unit or
> client drop-in, running `systemctl daemon-reload`, restarting or rolling back a
> service, and writing a canary to the live board are Fable/operator actions.
> Authors and tests use disposable directories and processes only. Fable is the
> sole lander and installer.

The installer stages the release and unit and can atomically move the `current`
pointer, but deliberately never calls `systemctl`, enables a unit, or restarts a
service. The successful apply path below issues exactly one controlled
`restart boardd.service` after every non-restart gate passes.

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
their canonical board pin and use `BOARDD_SOCK=/run/boardd/boardd.sock`.

## One controlled restart

Run this only in an approved maintenance window. Start from the exact clean,
reviewed checkout Fable intends to land; do not run it from a worker workspace
or a checkout with tracked changes. Keep the shell open until postflight passes.
Before running the first block, Fable must confirm a current integrity-checked
backup through the separate backup procedure and quiesce every known
board-writing client using the deployment's recorded unit inventory. Do not
guess client unit names here; keep those clients quiesced through the canary.

### 1. Set immutable inputs and capture preflight

```bash
set -euo pipefail
repo=/absolute/path/to/clean/hermes-agent
board_db=/var/lib/boardd/fleet/kanban.db
board_sock=/run/boardd/boardd.sock
source_head=$(git -C "$repo" rev-parse HEAD)
evidence_dir=$(mktemp -d "${TMPDIR:-/tmp}/boardd-restart.XXXXXXXX")
chmod 0700 "$evidence_dir"

# The service must be healthy before it is changed. Record values used later to
# prove that exactly one controlled start replaced this process.
test "$(sudo systemctl show boardd.service -p ActiveState --value)" = active
test "$(sudo systemctl show boardd.service -p SubState --value)" = running
pre_pid=$(sudo systemctl show boardd.service -p MainPID --value)
pre_started=$(sudo systemctl show boardd.service -p ExecMainStartTimestampMonotonic --value)
pre_nrestarts=$(sudo systemctl show boardd.service -p NRestarts --value)
pre_release=$(sudo readlink -e /opt/hermes-boardd/current)
pre_python=$(sudo readlink -e "$pre_release/venv/bin/python")

env_probe=(
  sudo -u boardd env
  HERMES_HOME=/var/lib/boardd
  HERMES_KANBAN_BROKER=1
  HERMES_KANBAN_BOARD=fleet
  HERMES_KANBAN_DB="$board_db"
  BOARDD_SOCK="$board_sock"
  "$pre_python" -m hermes_cli.kb_client
)
counts_sql='SELECT (SELECT COUNT(*) FROM tasks) AS tasks, (SELECT COUNT(*) FROM task_links) AS task_links, (SELECT COUNT(*) FROM task_comments) AS task_comments, (SELECT COUNT(*) FROM task_runs) AS task_runs, (SELECT COUNT(*) FROM task_links l LEFT JOIN tasks p ON p.id=l.parent_id LEFT JOIN tasks c ON c.id=l.child_id WHERE p.id IS NULL OR c.id IS NULL) AS orphan_links'

{
  date -u +%FT%TZ
  printf 'source_head=%s\npre_pid=%s\npre_started=%s\npre_nrestarts=%s\npre_release=%s\n' \
    "$source_head" "$pre_pid" "$pre_started" "$pre_nrestarts" "$pre_release"
  sudo systemctl show boardd.service \
    -p ActiveState -p SubState -p MainPID -p ExecMainStartTimestamp \
    -p ExecMainStartTimestampMonotonic -p NRestarts -p FragmentPath
  sudo systemctl cat boardd.service
  sudo stat -Lc 'socket=%n owner=%U:%G mode=%a inode=%i' "$board_sock"
  sudo sha256sum "$pre_release/libexec/boardd.py" \
    "$pre_release/venv/bin/python"
  "${env_probe[@]}" ping
  "${env_probe[@]}" query 'PRAGMA database_list'
  "${env_probe[@]}" query 'PRAGMA integrity_check'
  "${env_probe[@]}" query 'PRAGMA schema_version'
  "${env_probe[@]}" query 'PRAGMA user_version'
  "${env_probe[@]}" query 'PRAGMA table_info(tasks)'
  "${env_probe[@]}" query "$counts_sql"
} | tee "$evidence_dir/preflight.txt"

pre_counts_json=$("${env_probe[@]}" query "$counts_sql")
```

Stop if preflight is not clean, `integrity_check` is not exactly `ok`, any
`orphan_links` value is nonzero, or the socket contract differs. Never `cp` or
`rsync` the live WAL database.

### 2. Stage and activate without touching the running service

```bash
install_receipt=$(
  sudo "$repo/scripts/fleet/install-boardd-runtime.sh" \
    --source "$repo" \
    --activate
)
printf '%s\n' "$install_receipt" | tee "$evidence_dir/install.txt"
printf '%s\n' "$install_receipt" | grep -Fx 'activated=1'
printf '%s\n' "$install_receipt" | grep -Fx 'service_mutation=none'

expected_release=$(sudo readlink -e /opt/hermes-boardd/current)
expected_id=$(basename "$expected_release")
case "$expected_release" in
  /opt/hermes-boardd/releases/*) ;;
  *) printf 'refusing unexpected release target: %s\n' "$expected_release" >&2; exit 1 ;;
esac

test "$(sudo readlink /opt/hermes-boardd/current)" = "releases/$expected_id"
test "$(sudo awk -F= '$1 == "release_id" {print $2}' "$expected_release/MANIFEST")" = "$expected_id"
test "$(sudo awk -F= '$1 == "git_head" {print $2}' "$expected_release/MANIFEST")" = "$source_head"
sudo stat -c 'current=%N owner=%U:%G mode=%a' /opt/hermes-boardd/current
sudo stat -Lc 'release=%n owner=%U:%G mode=%a' "$expected_release"
```

Before any service action, prove the dedicated principal can traverse every
parent, read the source and manifest, execute the interpreter, and load the
packaged command. Also prove it cannot write the immutable release.

```bash
sudo -u boardd test -x /opt
sudo -u boardd test -x /opt/hermes-boardd
sudo -u boardd test -x /opt/hermes-boardd/releases
sudo -u boardd test -x "$expected_release"
sudo -u boardd test -x /opt/hermes-boardd/current
sudo -u boardd test -x "$expected_release/venv/bin/python"
sudo -u boardd test -x "$expected_release/venv/bin/hermes"
sudo -u boardd test -r "$expected_release/libexec/boardd.py"
sudo -u boardd test -r "$expected_release/MANIFEST"
sudo -u boardd test ! -w "$expected_release"
sudo -u boardd test ! -w "$expected_release/libexec/boardd.py"
sudo -u boardd env HERMES_KANBAN_BROKER=0 \
  "$expected_release/venv/bin/python" \
  "$expected_release/libexec/boardd.py" --help >/dev/null

sudo systemd-analyze verify /etc/systemd/system/boardd.service
sudo grep -F -- '--import-schema' /etc/systemd/system/boardd.service
sudo grep -F -- '/opt/hermes-boardd/current/venv/bin/python' \
  /etc/systemd/system/boardd.service
sudo grep -F -- "$board_db" /etc/systemd/system/boardd.service
sudo grep -F -- "$board_sock" /etc/systemd/system/boardd.service
```

Any failure above is a no-restart stop. Fix or restage; do not use a second
installer checkout and do not select a release with `find | tail`. The
root-owned `current` pointer and its `MANIFEST` are authoritative.

### 3. Reload and restart exactly once

This is the only successful-path `restart boardd.service` command in the
procedure. Fable/operator retains the shell and service custody.

```bash
sudo systemctl daemon-reload
sudo systemctl restart boardd.service
```

Do not restart a second time to see whether a failure clears. A failed
postflight enters the rollback decision below.

### 4. Prove release, broker, schema, integrity, and write path

```bash
test "$(sudo systemctl show boardd.service -p ActiveState --value)" = active
test "$(sudo systemctl show boardd.service -p SubState --value)" = running
post_pid=$(sudo systemctl show boardd.service -p MainPID --value)
post_started=$(sudo systemctl show boardd.service -p ExecMainStartTimestampMonotonic --value)
post_nrestarts=$(sudo systemctl show boardd.service -p NRestarts --value)
test "$post_pid" -gt 0
test "$post_pid" != "$pre_pid"
test "$post_started" != "$pre_started"
test "$post_nrestarts" -le "$pre_nrestarts"

test "$(sudo readlink -e /opt/hermes-boardd/current)" = "$expected_release"
expected_exe=$(sudo readlink -e "$expected_release/venv/bin/python")
actual_exe=$(sudo readlink -e "/proc/$post_pid/exe")
test "$actual_exe" = "$expected_exe"
test "$(sudo stat -Lc '%U:%G %a' "$board_sock")" = 'boardd:boardd 660'

runtime_python="$expected_release/venv/bin/python"
runtime_hermes="$expected_release/venv/bin/hermes"
run_probe() {
  sudo -u boardd env \
    HERMES_HOME=/var/lib/boardd \
    HERMES_KANBAN_BROKER=1 \
    HERMES_KANBAN_BOARD=fleet \
    HERMES_KANBAN_DB="$board_db" \
    BOARDD_SOCK="$board_sock" \
    "$runtime_python" -m hermes_cli.kb_client "$@"
}
run_hermes() {
  sudo -u boardd env \
    HERMES_HOME=/var/lib/boardd \
    HERMES_KANBAN_BROKER=1 \
    HERMES_KANBAN_BOARD=fleet \
    HERMES_KANBAN_DB="$board_db" \
    BOARDD_SOCK="$board_sock" \
    "$runtime_hermes" "$@"
}

ping_json=$(run_probe ping)
integrity_json=$(run_probe query 'PRAGMA integrity_check')
schema_version_json=$(run_probe query 'PRAGMA schema_version')
user_version_json=$(run_probe query 'PRAGMA user_version')
columns_json=$(run_probe query 'PRAGMA table_info(tasks)')

PING_JSON="$ping_json" INTEGRITY_JSON="$integrity_json" \
SCHEMA_VERSION_JSON="$schema_version_json" USER_VERSION_JSON="$user_version_json" \
COLUMNS_JSON="$columns_json" "$runtime_python" -c '
import json, os
ping = json.loads(os.environ["PING_JSON"])
integrity = json.loads(os.environ["INTEGRITY_JSON"])
schema_version = json.loads(os.environ["SCHEMA_VERSION_JSON"])
user_version = json.loads(os.environ["USER_VERSION_JSON"])
columns = json.loads(os.environ["COLUMNS_JSON"])
assert int(ping["pid"]) > 0
assert integrity == [{"integrity_check": "ok"}]
assert len(schema_version) == 1 and int(schema_version[0]["schema_version"]) >= 0
assert len(user_version) == 1 and int(user_version[0]["user_version"]) >= 0
assert "reasoning_effort" in {row["name"] for row in columns}
'

printf '%s\n' "$ping_json" "$integrity_json" \
  "$schema_version_json" "$user_version_json" "$columns_json" \
  | tee "$evidence_dir/postflight-schema.txt"

canary_json=$(run_hermes kanban --board fleet create \
  "boardd restart canary $expected_id" \
  --body 'Fable/operator postflight canary; archive after create/list/show proof.' \
  --created-by boardd-postflight \
  --initial-status blocked \
  --reasoning-effort high \
  --idempotency-key "boardd-restart-canary-$expected_id" \
  --json)
canary_id=$(printf '%s' "$canary_json" | "$runtime_python" -c \
  'import json,sys; print(json.load(sys.stdin)["id"])')

show_json=$(run_hermes kanban --board fleet show "$canary_id" --json)
list_json=$(run_hermes kanban --board fleet list --status blocked --json)
CANARY_ID="$canary_id" SHOW_JSON="$show_json" LIST_JSON="$list_json" \
  "$runtime_python" -c '
import json, os
canary_id = os.environ["CANARY_ID"]
shown = json.loads(os.environ["SHOW_JSON"])
listed = json.loads(os.environ["LIST_JSON"])
assert shown["id"] == canary_id
assert shown["status"] == "blocked"
assert shown["reasoning_effort"] == "high"
assert canary_id in {row["id"] for row in listed}
'

run_hermes kanban --board fleet archive "$canary_id"
archived_json=$(run_hermes kanban --board fleet show "$canary_id" --json)
ARCHIVED_JSON="$archived_json" "$runtime_python" -c \
  'import json,os; assert json.loads(os.environ["ARCHIVED_JSON"])["status"] == "archived"'

post_counts_json=$(run_probe query "$counts_sql")
PRE_COUNTS_JSON="$pre_counts_json" POST_COUNTS_JSON="$post_counts_json" \
  "$runtime_python" -c '
import json, os
pre = json.loads(os.environ["PRE_COUNTS_JSON"])[0]
post = json.loads(os.environ["POST_COUNTS_JSON"])[0]
assert post["tasks"] == pre["tasks"] + 1  # archived canary remains auditable
assert post["task_links"] == pre["task_links"]
assert post["orphan_links"] == 0
'

{
  date -u +%FT%TZ
  printf 'expected_release=%s\npost_pid=%s\npost_started=%s\npost_nrestarts=%s\ncanary_id=%s\n' \
    "$expected_release" "$post_pid" "$post_started" "$post_nrestarts" "$canary_id"
  sudo systemctl show boardd.service \
    -p ActiveState -p SubState -p MainPID -p ExecMainStartTimestamp \
    -p ExecMainStartTimestampMonotonic -p NRestarts -p FragmentPath
  sudo stat -Lc 'socket=%n owner=%U:%G mode=%a inode=%i' "$board_sock"
  printf 'ping=%s\nintegrity=%s\nschema_version=%s\nuser_version=%s\n' \
    "$ping_json" "$integrity_json" "$schema_version_json" "$user_version_json"
  printf 'pre_counts=%s\npost_counts=%s\ncanary=%s\n' \
    "$pre_counts_json" "$post_counts_json" "$archived_json"
} | tee "$evidence_dir/postflight.txt"
```

Only after every assertion passes may Fable resume the quiesced clients. Verify
each client still points at `/run/boardd/boardd.sock`, then watch the boardd and
client journals through the maintenance window for restart, integrity, timeout,
permission, or schema errors.

`--import-schema` runs additive migrations before readiness. The checked-in
restart regression derives its command from the unit's `ExecStart`, proves the
legacy column is absent before startup, proves it appears automatically, and
proves existing and new broker data survive a second process start.

## Rollback decision and procedure

Do not roll back for an unrelated client issue. Roll back before clients resume
if any boardd postflight gate fails: inactive service, automatic restart count
increase, wrong process/release, wrong socket owner or mode, broker ping failure,
non-`ok` integrity, missing additive column, canary create/list/show/archive
failure, unexpected row/link accounting, or new boardd startup errors.

Rollback is an emergency failure path, not a second attempt at the successful
apply. It necessarily performs one controlled restart back to the previously
verified release and remains Fable/operator custody:

```bash
# Preserve the failed postflight evidence first. Do not edit or restore the DB
# for an additive-schema failure and do not open it directly.
sudo "$repo/scripts/fleet/rollback-boardd-runtime.sh"
sudo systemctl daemon-reload
sudo systemctl restart boardd.service

test "$(sudo systemctl show boardd.service -p ActiveState --value)" = active
test "$(sudo systemctl show boardd.service -p SubState --value)" = running
test "$(sudo readlink -e /opt/hermes-boardd/current)" = "$pre_release"
rollback_python=$(sudo readlink -e "$pre_release/venv/bin/python")
sudo -u boardd env \
  HERMES_HOME=/var/lib/boardd \
  HERMES_KANBAN_BROKER=1 \
  HERMES_KANBAN_BOARD=fleet \
  HERMES_KANBAN_DB="$board_db" \
  BOARDD_SOCK="$board_sock" \
  "$rollback_python" -m hermes_cli.kb_client ping
```

The rollback script only swaps `current` and `previous`; it never calls
`systemctl`. Keep clients quiesced until the old release, socket, broker ping,
integrity, and frozen hashes are re-verified and the failure evidence is handed
to the independent reviewer.
