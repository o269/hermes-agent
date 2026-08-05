#!/usr/bin/env bash
set -euo pipefail
umask 022

usage() {
  printf '%s\n' \
    'Usage: install-boardd-backup-monitor.sh [--source PATH] [--destdir PATH]' \
    '       [--release-id ID] [--activate]' \
    '' \
    'Stages an immutable monitor release plus systemd service/timer units.' \
    'It never calls systemctl, daemon-reload, enable, start, or restart.'
}

SOURCE_ROOT=""
DESTDIR=""
RELEASE_ID=""
ACTIVATE=0
while (($#)); do
  case "$1" in
    --source) SOURCE_ROOT=${2:?missing --source value}; shift 2 ;;
    --destdir) DESTDIR=${2:?missing --destdir value}; shift 2 ;;
    --release-id) RELEASE_ID=${2:?missing --release-id value}; shift 2 ;;
    --activate) ACTIVATE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ -z "$SOURCE_ROOT" ]]; then
  SOURCE_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
else
  SOURCE_ROOT=$(CDPATH= cd -- "$SOURCE_ROOT" && pwd -P)
fi
if [[ -n "$DESTDIR" ]]; then
  [[ "$DESTDIR" = /* ]] || { printf '%s\n' '--destdir must be absolute' >&2; exit 2; }
  DESTDIR=${DESTDIR%/}
  [[ -n "$DESTDIR" ]] || { printf '%s\n' '--destdir / is not allowed; omit it for production' >&2; exit 2; }
elif [[ $(id -u) -ne 0 ]]; then
  printf '%s\n' 'production installation requires root; use --destdir for staging' >&2
  exit 1
fi

files=(
  scripts/fleet/boardd-backup-monitor.py
  scripts/fleet/boardd-backup-monitor.service
  scripts/fleet/boardd-backup-monitor.timer
)
for relative in "${files[@]}"; do
  [[ -r "$SOURCE_ROOT/$relative" ]] || {
    printf 'missing required source: %s\n' "$SOURCE_ROOT/$relative" >&2
    exit 1
  }
done
git -C "$SOURCE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  printf 'source must be a git worktree: %s\n' "$SOURCE_ROOT" >&2
  exit 1
}
git -C "$SOURCE_ROOT" diff --quiet HEAD -- || {
  printf '%s\n' 'refusing a source tree with unstaged tracked changes' >&2
  exit 1
}
git -C "$SOURCE_ROOT" diff --cached --quiet HEAD -- || {
  printf '%s\n' 'refusing a source tree with staged but uncommitted changes' >&2
  exit 1
}
for relative in "${files[@]}"; do
  git -C "$SOURCE_ROOT" ls-files --error-unmatch "$relative" >/dev/null 2>&1 || {
    printf 'required source is not tracked at HEAD: %s\n' "$relative" >&2
    exit 1
  }
done

source_fingerprint=$(
  for relative in "${files[@]}"; do
    printf '%s  %s\n' \
      "$(sha256sum "$SOURCE_ROOT/$relative" | cut -d' ' -f1)" \
      "$relative"
  done | sha256sum | cut -c1-12
)
git_head=$(git -C "$SOURCE_ROOT" rev-parse HEAD)
if [[ -z "$RELEASE_ID" ]]; then
  RELEASE_ID="${git_head:0:12}-${source_fingerprint}"
fi
[[ "$RELEASE_ID" =~ ^[A-Za-z0-9._-]+$ ]] || {
  printf 'invalid release id: %s\n' "$RELEASE_ID" >&2
  exit 2
}

prefix="$DESTDIR/opt/hermes-boardd-backup-monitor"
releases_dir="$prefix/releases"
release_dir="$releases_dir/$RELEASE_ID"
unit_dir="$DESTDIR/etc/systemd/system"
install -d -m 0755 "$releases_dir" "$unit_dir"

stage_dir="$releases_dir/.stage-${RELEASE_ID}.$$"
cleanup() { rm -rf -- "$stage_dir"; }
trap cleanup EXIT

if [[ ! -e "$release_dir" && ! -L "$release_dir" ]]; then
  install -d -m 0755 "$stage_dir"
  install -m 0555 \
    "$SOURCE_ROOT/scripts/fleet/boardd-backup-monitor.py" \
    "$stage_dir/boardd-backup-monitor.py"
  /usr/bin/python3 "$stage_dir/boardd-backup-monitor.py" --help >/dev/null
  {
    printf 'release_id=%s\n' "$RELEASE_ID"
    printf 'git_head=%s\n' "$git_head"
    printf 'source_fingerprint=%s\n' "$source_fingerprint"
    printf 'monitor_sha256=%s\n' "$(sha256sum "$stage_dir/boardd-backup-monitor.py" | cut -d' ' -f1)"
    printf 'python=%s\n' "$(/usr/bin/python3 --version 2>&1)"
  } > "$stage_dir/MANIFEST"
  chmod -R go-w "$stage_dir"
  if [[ -z "$DESTDIR" ]]; then
    chown -R root:root "$stage_dir"
  fi
  mv -- "$stage_dir" "$release_dir"
else
  [[ -d "$release_dir" && ! -L "$release_dir" ]] || {
    printf 'existing release is not a real directory: %s\n' "$release_dir" >&2
    exit 1
  }
  [[ -x "$release_dir/boardd-backup-monitor.py" ]] || {
    printf 'existing release is not executable: %s\n' "$release_dir" >&2
    exit 1
  }
  [[ -r "$release_dir/MANIFEST" ]] || {
    printf 'existing release has no manifest: %s\n' "$release_dir" >&2
    exit 1
  }
  grep -Fqx "git_head=$git_head" "$release_dir/MANIFEST" || {
    printf 'existing release git head does not match source: %s\n' "$release_dir" >&2
    exit 1
  }
  grep -Fqx "source_fingerprint=$source_fingerprint" "$release_dir/MANIFEST" || {
    printf 'existing release fingerprint does not match source: %s\n' "$release_dir" >&2
    exit 1
  }
  installed_hash=$(sha256sum "$release_dir/boardd-backup-monitor.py" | cut -d' ' -f1)
  grep -Fqx "monitor_sha256=$installed_hash" "$release_dir/MANIFEST" || {
    printf 'existing release monitor hash does not match manifest: %s\n' "$release_dir" >&2
    exit 1
  }
fi

for unit in boardd-backup-monitor.service boardd-backup-monitor.timer; do
  install -m 0644 "$SOURCE_ROOT/scripts/fleet/$unit" "$unit_dir/$unit"
  if [[ -z "$DESTDIR" ]]; then
    chown root:root "$unit_dir/$unit"
  fi
done

atomic_link() {
  local target=$1
  local link=$2
  local temp="${link}.new.$$"
  ln -s -- "$target" "$temp"
  mv -Tf -- "$temp" "$link"
}

if ((ACTIVATE)); then
  old_target=$(readlink "$prefix/current" 2>/dev/null || true)
  if [[ -n "$old_target" && ! "$old_target" =~ ^releases/[A-Za-z0-9._-]+$ ]]; then
    printf 'refusing unsafe current monitor target: %s\n' "$old_target" >&2
    exit 1
  fi
  if [[ -n "$old_target" ]] && {
    [[ ! -d "$prefix/$old_target" ]] ||
      [[ -L "$prefix/$old_target" ]] ||
      [[ ! -x "$prefix/$old_target/boardd-backup-monitor.py" ]]
  }; then
    printf 'refusing invalid current monitor release: %s\n' "$old_target" >&2
    exit 1
  fi
  if [[ -n "$old_target" && "$old_target" != "releases/$RELEASE_ID" ]]; then
    atomic_link "$old_target" "$prefix/previous"
  fi
  atomic_link "releases/$RELEASE_ID" "$prefix/current"
fi

printf 'staged_release=%s\n' "$release_dir"
printf 'service_unit=%s\n' "$unit_dir/boardd-backup-monitor.service"
printf 'timer_unit=%s\n' "$unit_dir/boardd-backup-monitor.timer"
printf 'activated=%s\n' "$ACTIVATE"
printf '%s\n' 'systemctl_mutation=none'
printf '%s\n' 'next=Fable verifies one read-only check, then daemon-reloads and enables the timer in a quiet window'
