#!/usr/bin/env bash
# Install the broker-native split dispatcher without touching production SQL.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)

requested_identity=${FLEET_INSTALL_HOST_IDENTITY:-}
if [[ -z "$requested_identity" ]]; then
  case "$(hostname)" in
    blitz-vps-2) requested_identity=blitz-vps-2 ;;
    *) requested_identity=blitz ;;
  esac
fi
case "$requested_identity" in
  blitz|blitz-vps-2) ;;
  *) printf 'FATAL: unsupported FLEET_INSTALL_HOST_IDENTITY=%s\n' "$requested_identity" >&2; exit 2 ;;
esac

if [[ "$requested_identity" == "blitz-vps-2" ]]; then
  TARGET_HOME=${FLEET_TARGET_HOME:-/root}
  boardd_sock=${BOARDD_SOCK:-/run/boardd-blitz.sock}
else
  TARGET_HOME=${FLEET_TARGET_HOME:-/home/odai}
  boardd_sock=${BOARDD_SOCK:-/home/odai/.hermes/kanban/boardd-run/boardd.sock}
fi
BIN_DIR="$TARGET_HOME/.local/bin"
HERMES_AGENT="$TARGET_HOME/.hermes/hermes-agent"
BOARD_SCRIPTS="$TARGET_HOME/.hermes/scripts"
SYSTEMD_USER_DIR="$TARGET_HOME/.config/systemd/user"
BACKUP_SUFFIX=.pre-vps2-safe-dispatch

as_root() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

(
  cd "$SCRIPT_DIR"
  sha256sum --check SHA256SUMS
)

install -d -m 0755 "$BIN_DIR" "$BOARD_SCRIPTS" "$HERMES_AGENT/hermes_cli" "$SYSTEMD_USER_DIR"

backup_once() {
  local target=$1
  if [[ -e "$target" && ! -e "${target}${BACKUP_SUFFIX}" ]]; then
    install -m "$(stat -c '%a' "$target")" "$target" "${target}${BACKUP_SUFFIX}"
  fi
}

# Stage broker/client authority first. Existing files are retained for manual
# recovery; the rollback intentionally keeps the compatible broker endpoint.
backup_once "$BOARD_SCRIPTS/boardd.py"
backup_once "$HERMES_AGENT/hermes_cli/kb_client.py"
install -m 0755 "$SCRIPT_DIR/boardd.py" "$BOARD_SCRIPTS/boardd.py"
install -m 0644 "$REPO_ROOT/hermes_cli/kb_client.py" "$HERMES_AGENT/hermes_cli/kb_client.py"

# Stage both dispatch entry points before replacing the standing blitz owner.
install -m 0755 "$SCRIPT_DIR/fleet-board-reconciler-vps2" "$BIN_DIR/fleet-board-reconciler-vps2"
backup_once "$BIN_DIR/fleet-board-reconciler"
install -m 0755 "$SCRIPT_DIR/fleet-board-reconciler" "$BIN_DIR/fleet-board-reconciler"

identity_tmp=$(mktemp)
printf 'FLEET_HOST_IDENTITY=%s\n' "$requested_identity" > "$identity_tmp"
as_root install -m 0644 "$identity_tmp" /etc/default/fleet-board-reconciler
rm -f "$identity_tmp"

cat > "$SYSTEMD_USER_DIR/fleet-board-reconciler-vps2.service" <<UNIT
[Unit]
Description=Broker-native Fleet dispatcher for blitz-vps-2
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/default/fleet-board-reconciler
Environment=FLEET_DISPATCH_EXPECTED_HOST=blitz-vps-2
Environment=BOARDD_SOCK=$boardd_sock
ExecStart=$BIN_DIR/fleet-board-reconciler-vps2
TimeoutStartSec=120
KillMode=process
UNIT

cat > "$SYSTEMD_USER_DIR/fleet-board-reconciler-vps2.timer" <<'UNIT'
[Unit]
Description=Run the broker-native VPS2 Fleet dispatcher every 60 seconds

[Timer]
OnBootSec=2min
OnUnitActiveSec=60s
AccuracySec=10s
Unit=fleet-board-reconciler-vps2.service

[Install]
WantedBy=timers.target
UNIT

# Ensure the standing blitz unit receives a process-start token that boardd can
# authenticate through SO_PEERCRED + /proc/<pid>/environ.
install -d -m 0755 "$SYSTEMD_USER_DIR/fleet-board-reconciler.service.d"
cat > "$SYSTEMD_USER_DIR/fleet-board-reconciler.service.d/host-identity.conf" <<'UNIT'
[Service]
EnvironmentFile=/etc/default/fleet-board-reconciler
Environment=FLEET_DISPATCH_EXPECTED_HOST=blitz
UNIT

systemctl --user daemon-reload
if [[ "$requested_identity" == "blitz-vps-2" ]]; then
  systemctl --user enable --now fleet-board-reconciler-vps2.timer
fi

printf 'Installed broker-native Fleet dispatch for %s (boardd=%s)\n' \
  "$requested_identity" "$boardd_sock"
