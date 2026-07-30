#!/usr/bin/env bash
# Disable VPS2 dispatch first, restore the standing blitz owner, then prove
# boardd has no non-blitz running custody rows. Never stops the blitz timer.
set -euo pipefail

case "${FLEET_INSTALL_HOST_IDENTITY:-$(hostname)}" in
  blitz-vps-2)
    TARGET_HOME=${FLEET_TARGET_HOME:-/root}
    default_sock=/run/boardd-blitz.sock
    ;;
  *)
    TARGET_HOME=${FLEET_TARGET_HOME:-/home/odai}
    default_sock=/home/odai/.hermes/kanban/boardd-run/boardd.sock
    ;;
esac
BIN_DIR="$TARGET_HOME/.local/bin"
SYSTEMD_USER_DIR="$TARGET_HOME/.config/systemd/user"
BACKUP="$BIN_DIR/fleet-board-reconciler.pre-vps2-safe-dispatch"
BOARDD_SOCK=${BOARDD_SOCK:-$default_sock}

as_root() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

# Stop the secondary owner before changing any executable or unit file.
systemctl --user disable --now fleet-board-reconciler-vps2.timer 2>/dev/null || true
systemctl --user stop fleet-board-reconciler-vps2.service 2>/dev/null || true
rm -f "$SYSTEMD_USER_DIR/fleet-board-reconciler-vps2.timer"
rm -f "$SYSTEMD_USER_DIR/fleet-board-reconciler-vps2.service"
rm -f "$BIN_DIR/fleet-board-reconciler-vps2"
systemctl --user daemon-reload

if [[ ! -f "$BACKUP" ]]; then
  printf 'FATAL: blitz reconciler backup missing: %s\n' "$BACKUP" >&2
  exit 2
fi
install -m 0755 "$BACKUP" "$BIN_DIR/fleet-board-reconciler"

# The remaining dispatch owner is blitz. Keep the standing blitz timer running;
# only update its process-start identity token for the next normal pulse.
identity_tmp=$(mktemp)
printf 'FLEET_HOST_IDENTITY=blitz\n' > "$identity_tmp"
as_root install -m 0644 "$identity_tmp" /etc/default/fleet-board-reconciler
rm -f "$identity_tmp"

# Fail closed if broker state still has foreign running custody. This query is
# broker-only; rollback never opens kanban.db directly and has no local fallback.
export BOARDD_SOCK
export KB_CLIENT_RETRY_DEADLINE_S=${KB_CLIENT_RETRY_DEADLINE_S:-3}
export PYTHONPATH="$TARGET_HOME/.hermes/hermes-agent${PYTHONPATH:+:$PYTHONPATH}"
python_bin=${FLEET_PYTHON:-$TARGET_HOME/.hermes/hermes-agent/venv/bin/python}
if [[ ! -x "$python_bin" ]]; then
  python_bin=$(command -v python3)
fi
if ! "$python_bin" -c '
from hermes_cli.kb_client import Client
c = Client(sock_path="'"$BOARDD_SOCK"'")
rows = c.query("SELECT id, host_identity, worker_pid FROM tasks WHERE status=? AND COALESCE(host_identity, char(0)) <> ?", ["running", "blitz"])
c.close()
if rows:
    for row in rows:
        print(f"FOREIGN-RUNNING {row['"'"'id'"'"']} host={row.get('"'"'host_identity'"'"')} pid={row.get('"'"'worker_pid'"'"')}")
    raise SystemExit(1)
' >&2; then
  printf 'FATAL: rollback single-owner assertion failed\n' >&2
  exit 1
fi

printf 'Rollback complete: VPS2 dispatcher disabled; blitz is the sole dispatch owner.\n'
