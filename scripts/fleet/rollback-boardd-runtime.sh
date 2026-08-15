#!/usr/bin/env bash
set -euo pipefail
umask 022

DESTDIR=""
while (($#)); do
  case "$1" in
    --destdir) DESTDIR=${2:?missing --destdir value}; shift 2 ;;
    -h|--help)
      printf '%s\n' 'Usage: rollback-boardd-runtime.sh [--destdir ABSOLUTE_PATH]'
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

prefix="$DESTDIR/opt/hermes-boardd"
current=$(readlink "$prefix/current" 2>/dev/null || true)
previous=$(readlink "$prefix/previous" 2>/dev/null || true)
[[ -n "$current" ]] || { printf '%s\n' 'current runtime link is missing' >&2; exit 1; }
[[ -n "$previous" ]] || { printf '%s\n' 'previous runtime link is missing' >&2; exit 1; }
[[ -x "$prefix/$previous/venv/bin/python" ]] || {
  printf 'previous runtime is not executable: %s/%s\n' "$prefix" "$previous" >&2
  exit 1
}
[[ -r "$prefix/$previous/libexec/boardd.py" ]] || {
  printf 'previous broker source is not readable: %s/%s\n' "$prefix" "$previous" >&2
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
printf '%s\n' 'service_mutation=none'
printf '%s\n' 'next=Fable restores the prior client socket drop-ins, daemon-reloads, and restarts boardd once'
