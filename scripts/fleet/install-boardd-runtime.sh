#!/usr/bin/env bash
set -euo pipefail
umask 022

usage() {
  printf '%s\n' \
    'Usage: install-boardd-runtime.sh [--source PATH] [--destdir PATH]' \
    '       [--python PATH] [--release-id ID] [--activate]' \
    '' \
    'Stages an immutable boardd release and systemd unit. It never calls' \
    'systemctl, enables a unit, or restarts a service.'
}

SOURCE_ROOT=""
DESTDIR=""
# Never inherit a profile-managed Python from PATH: its base runtime may live
# under an inaccessible home directory even when venv uses --copies.
PYTHON_BIN="/usr/bin/python3"
RELEASE_ID=""
ACTIVATE=0
while (($#)); do
  case "$1" in
    --source) SOURCE_ROOT=${2:?missing --source value}; shift 2 ;;
    --destdir) DESTDIR=${2:?missing --destdir value}; shift 2 ;;
    --python) PYTHON_BIN=${2:?missing --python value}; shift 2 ;;
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

for required in \
  "$SOURCE_ROOT/scripts/fleet/boardd.py" \
  "$SOURCE_ROOT/scripts/fleet/boardd.service" \
  "$SOURCE_ROOT/hermes_cli/kanban_db.py" \
  "$SOURCE_ROOT/hermes_cli/kb_client.py" \
  "$SOURCE_ROOT/pyproject.toml"; do
  [[ -r "$required" ]] || { printf 'missing required source: %s\n' "$required" >&2; exit 1; }
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
for tracked in \
  scripts/fleet/boardd.py \
  scripts/fleet/boardd.service \
  hermes_cli/kanban_db.py \
  hermes_cli/kb_client.py \
  pyproject.toml; do
  git -C "$SOURCE_ROOT" ls-files --error-unmatch "$tracked" >/dev/null 2>&1 || {
    printf 'required source is not tracked at HEAD: %s\n' "$tracked" >&2
    exit 1
  }
done
command -v "$PYTHON_BIN" >/dev/null 2>&1 || { printf 'python not found: %s\n' "$PYTHON_BIN" >&2; exit 1; }
PYTHON_BIN=$(readlink -f "$(command -v "$PYTHON_BIN")")

source_fingerprint=$(
  sha256sum \
    "$SOURCE_ROOT/scripts/fleet/boardd.py" \
    "$SOURCE_ROOT/hermes_cli/kanban_db.py" \
    "$SOURCE_ROOT/hermes_cli/kb_client.py" \
    "$SOURCE_ROOT/pyproject.toml" |
    sha256sum | cut -c1-12
)
git_head=$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null || printf 'no-git-source')
python_fingerprint=$(
  {
    printf '%s\n' "$PYTHON_BIN"
    "$PYTHON_BIN" --version 2>&1
  } | sha256sum | cut -c1-8
)
if [[ -z "$RELEASE_ID" ]]; then
  RELEASE_ID="${git_head:0:12}-${source_fingerprint}-${python_fingerprint}"
fi
[[ "$RELEASE_ID" =~ ^[A-Za-z0-9._-]+$ ]] || {
  printf 'invalid release id: %s\n' "$RELEASE_ID" >&2
  exit 2
}

prefix="$DESTDIR/opt/hermes-boardd"
releases_dir="$prefix/releases"
release_dir="$releases_dir/$RELEASE_ID"
unit_dir="$DESTDIR/etc/systemd/system"
state_dir="$DESTDIR/var/lib/boardd/fleet"

if [[ -z "$DESTDIR" ]]; then
  getent passwd boardd >/dev/null || { printf '%s\n' 'missing boardd user' >&2; exit 1; }
  getent group boardd >/dev/null || { printf '%s\n' 'missing boardd group' >&2; exit 1; }
  if [[ -d "$state_dir" ]]; then
    owner=$(stat -c '%U:%G' "$state_dir")
    [[ "$owner" == 'boardd:boardd' ]] || {
      printf 'refusing to change existing state ownership (%s): %s\n' "$owner" "$state_dir" >&2
      exit 1
    }
  else
    install -d -o boardd -g boardd -m 0700 "$state_dir"
  fi
else
  install -d -m 0700 "$state_dir"
fi
install -d -m 0755 "$releases_dir" "$unit_dir"

stage_dir="$releases_dir/.stage-${RELEASE_ID}.$$"
cleanup() { rm -rf -- "$stage_dir"; }
trap cleanup EXIT

if [[ ! -d "$release_dir" ]]; then
  install -d -m 0755 "$stage_dir/libexec" "$stage_dir/source-build"
  # Build from an exact tracked snapshot. This avoids mutating the source
  # checkout with setuptools' build/ directory and excludes untracked secrets.
  git -C "$SOURCE_ROOT" archive --format=tar HEAD |
    tar -xf - -C "$stage_dir/source-build"
  "$PYTHON_BIN" -m venv --copies "$stage_dir/venv"
  "$stage_dir/venv/bin/python" -m pip install \
    --disable-pip-version-check --no-cache-dir 'PyYAML==6.0.3'
  "$stage_dir/venv/bin/python" -m pip install \
    --disable-pip-version-check --no-cache-dir --no-deps "$stage_dir/source-build"
  install -m 0555 "$stage_dir/source-build/scripts/fleet/boardd.py" "$stage_dir/libexec/boardd.py"
  rm -rf -- "$stage_dir/source-build"
  "$stage_dir/venv/bin/python" -m compileall -q "$stage_dir/venv"
  if [[ $(<"$stage_dir/venv/pyvenv.cfg") == *'/home/'* ]]; then
    printf 'refusing home-bound Python runtime: %s\n' "$stage_dir/venv/pyvenv.cfg" >&2
    exit 1
  fi
  env HERMES_KANBAN_BROKER=0 \
    "$stage_dir/venv/bin/python" "$stage_dir/libexec/boardd.py" --help >/dev/null
  {
    printf 'release_id=%s\n' "$RELEASE_ID"
    printf 'git_head=%s\n' "$git_head"
    printf 'source_fingerprint=%s\n' "$source_fingerprint"
    printf 'python_fingerprint=%s\n' "$python_fingerprint"
    printf 'boardd_sha256=%s\n' "$(sha256sum "$stage_dir/libexec/boardd.py" | cut -d' ' -f1)"
    printf 'kanban_db_sha256=%s\n' "$(sha256sum "$SOURCE_ROOT/hermes_cli/kanban_db.py" | cut -d' ' -f1)"
    printf 'kb_client_sha256=%s\n' "$(sha256sum "$SOURCE_ROOT/hermes_cli/kb_client.py" | cut -d' ' -f1)"
    printf 'python=%s\n' "$($stage_dir/venv/bin/python --version 2>&1)"
    printf 'pyyaml=%s\n' "$($stage_dir/venv/bin/python -c 'import yaml; print(yaml.__version__)')"
  } > "$stage_dir/MANIFEST"
  chmod -R go-w "$stage_dir"
  if [[ -z "$DESTDIR" ]]; then
    chown -R root:root "$stage_dir"
  fi
  mv -- "$stage_dir" "$release_dir"
else
  [[ -r "$release_dir/MANIFEST" ]] || {
    printf 'existing release has no manifest: %s\n' "$release_dir" >&2
    exit 1
  }
fi

install -m 0644 "$SOURCE_ROOT/scripts/fleet/boardd.service" "$unit_dir/boardd.service"
if [[ -z "$DESTDIR" ]]; then
  chown root:root "$unit_dir/boardd.service"
fi

atomic_link() {
  local target=$1
  local link=$2
  local temp="${link}.new.$$"
  ln -s -- "$target" "$temp"
  mv -Tf -- "$temp" "$link"
}

if ((ACTIVATE)); then
  old_target=$(readlink "$prefix/current" 2>/dev/null || true)
  if [[ -n "$old_target" && "$old_target" != "releases/$RELEASE_ID" ]]; then
    atomic_link "$old_target" "$prefix/previous"
  fi
  atomic_link "releases/$RELEASE_ID" "$prefix/current"
fi

printf 'staged_release=%s\n' "$release_dir"
printf 'unit=%s\n' "$unit_dir/boardd.service"
printf 'activated=%s\n' "$ACTIVATE"
printf '%s\n' 'service_mutation=none'
printf '%s\n' 'next=verify the release as boardd; Fable may then update client socket drop-ins, daemon-reload, and restart once'
