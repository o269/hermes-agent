#!/usr/bin/env bash
set -euo pipefail
umask 022

DESTDIR=""
while (($#)); do
  case "$1" in
    --destdir) DESTDIR=${2:?missing --destdir value}; shift 2 ;;
    -h|--help)
      printf '%s\n' 'Usage: rollback-boardd-backup-monitor.sh [--destdir ABSOLUTE_PATH]'
      exit 0
      ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ -n "$DESTDIR" ]]; then
  [[ "$DESTDIR" = /* ]] || { printf '%s\n' '--destdir must be absolute' >&2; exit 2; }
  DESTDIR=${DESTDIR%/}
  [[ -n "$DESTDIR" ]] || { printf '%s\n' '--destdir / is not allowed' >&2; exit 2; }
elif [[ $(id -u) -ne 0 ]]; then
  printf '%s\n' 'production rollback requires root; use --destdir for staging' >&2
  exit 1
fi

prefix="$DESTDIR/opt/hermes-boardd-backup-monitor"
current=$(readlink "$prefix/current" 2>/dev/null || true)
previous=$(readlink "$prefix/previous" 2>/dev/null || true)
[[ -n "$current" ]] || { printf '%s\n' 'current monitor runtime link is missing' >&2; exit 1; }
[[ -n "$previous" ]] || { printf '%s\n' 'previous monitor runtime link is missing' >&2; exit 1; }
[[ "$current" =~ ^releases/[A-Za-z0-9._-]+$ ]] || {
  printf 'refusing unsafe current monitor target: %s\n' "$current" >&2
  exit 1
}
[[ "$previous" =~ ^releases/[A-Za-z0-9._-]+$ ]] || {
  printf 'refusing unsafe previous monitor target: %s\n' "$previous" >&2
  exit 1
}
previous_dir="$prefix/$previous"
[[ -d "$previous_dir" && ! -L "$previous_dir" ]] || {
  printf 'previous monitor release is not a real directory: %s\n' "$previous_dir" >&2
  exit 1
}
[[ -x "$previous_dir/boardd-backup-monitor.py" ]] || {
  printf 'previous monitor is not executable: %s/%s\n' "$prefix" "$previous" >&2
  exit 1
}
[[ -r "$previous_dir/MANIFEST" ]] || {
  printf 'previous monitor has no manifest: %s\n' "$previous_dir" >&2
  exit 1
}
manifest_hash=$(grep -m1 '^monitor_sha256=' "$previous_dir/MANIFEST" || true)
manifest_hash=${manifest_hash#monitor_sha256=}
[[ "$manifest_hash" =~ ^[0-9a-f]{64}$ ]] || {
  printf 'previous monitor manifest has an invalid hash: %s\n' "$previous_dir" >&2
  exit 1
}
actual_hash=$(sha256sum "$previous_dir/boardd-backup-monitor.py" | cut -d' ' -f1)
[[ "$actual_hash" == "$manifest_hash" ]] || {
  printf 'previous monitor hash does not match manifest: %s\n' "$previous_dir" >&2
  exit 1
}

atomic_link() {
  local target=$1
  local link=$2
  local temp="${link}.new.$$"
  ln -s -- "$target" "$temp"
  mv -Tf -- "$temp" "$link"
}
atomic_link "$previous" "$prefix/current"
atomic_link "$current" "$prefix/previous"

printf 'current=%s\n' "$previous"
printf 'previous=%s\n' "$current"
printf '%s\n' 'systemctl_mutation=none'
printf '%s\n' 'next=Fable runs one read-only check; no boardd restart or database restore is required'
