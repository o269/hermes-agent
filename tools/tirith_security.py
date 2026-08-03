"""Tirith pre-exec security scanning wrapper.

Runs the tirith binary as a subprocess to scan commands for content-level
threats (homograph URLs, pipe-to-interpreter, terminal injection, etc.).

Exit code is the verdict source of truth:
  0 = allow, 1 = block, 2 = warn

JSON stdout enriches findings/summary but never overrides the verdict.
Operational failures (spawn error, timeout, unknown exit code) respect
the fail_open config setting. Programming errors propagate.

Auto-install: if tirith is not found on PATH or at the configured path,
it is automatically downloaded from GitHub releases to $HERMES_HOME/bin/tirith.
The download always verifies SHA-256 checksums.  When cosign is available on
PATH, provenance verification (GitHub Actions workflow signature) is also
performed.  If cosign is not installed, the download proceeds with SHA-256
verification only — still secure via HTTPS + checksum, just without supply
chain provenance proof.  Installation runs in a background thread so startup
never blocks.
"""

import errno
import hashlib
import json
import logging
import os
import platform
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_REPO = "sheeki03/tirith"

# Cosign provenance verification — pinned to the specific release workflow
_COSIGN_IDENTITY_REGEXP = f"^https://github.com/{_REPO}/\\.github/workflows/release\\.yml@refs/tags/v"
_COSIGN_ISSUER = "https://token.actions.githubusercontent.com"

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes"}


def _env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _load_security_config() -> dict:
    """Load security settings from config.yaml, with env var overrides."""
    defaults = {
        "tirith_enabled": True,
        "tirith_path": "tirith",
        "tirith_timeout": 5,
        "tirith_fail_open": True,
    }
    try:
        from hermes_cli.config import load_config
        cfg = load_config().get("security", {}) or {}
    except Exception:
        cfg = {}

    return {
        "tirith_enabled": _env_bool("TIRITH_ENABLED", cfg.get("tirith_enabled", defaults["tirith_enabled"])),
        "tirith_path": os.getenv("TIRITH_BIN", cfg.get("tirith_path", defaults["tirith_path"])),
        "tirith_timeout": _env_int("TIRITH_TIMEOUT", cfg.get("tirith_timeout", defaults["tirith_timeout"])),
        "tirith_fail_open": _env_bool("TIRITH_FAIL_OPEN", cfg.get("tirith_fail_open", defaults["tirith_fail_open"])),
    }


# ---------------------------------------------------------------------------
# Auto-install
# ---------------------------------------------------------------------------

# Cached path after first resolution (avoids repeated shutil.which per command).
# _INSTALL_FAILED means "we tried and failed" — prevents retry on every command.
_resolved_path: str | None | bool = None
_INSTALL_FAILED = False  # sentinel: distinct from "not yet tried"
_install_failure_reason: str = ""  # reason tag when _resolved_path is _INSTALL_FAILED

# Circuit breaker: after _CRASH_LIMIT consecutive spawn/execution failures,
# disable tirith for the rest of the process to prevent agent hangs (#41400).
# Reset on successful execution (see _record_tirith_crash / check_command_security).
#
# Thread safety: _crash_count and _circuit_open are module-level globals
# mutated without a lock. check_command_security can be called from
# concurrent agent threads (gateway multi-session). The race is benign —
# at worst two threads both increment past _CRASH_LIMIT and both set
# _circuit_open = True, opening the breaker one call early. No data
# corruption or security bypass is possible. This intentionally matches
# the lock-free style of error counters in mcp_tool.py rather than the
# locked _warn_once pattern, because the worst case is harmless.
_CRASH_LIMIT = 3
_crash_count: int = 0
_circuit_open: bool = False


def _record_tirith_crash() -> None:
    """Increment the crash counter and open the circuit breaker if needed."""
    global _crash_count, _circuit_open
    _crash_count += 1
    if _crash_count >= _CRASH_LIMIT:
        _circuit_open = True
        logger.warning(
            "tirith circuit breaker opened after %d consecutive failures; "
            "disabling for the rest of the process",
            _crash_count,
        )


def _reset_tirith_circuit() -> None:
    """Close the breaker after an explicit successful install/recovery."""
    global _crash_count, _circuit_open
    _crash_count = 0
    _circuit_open = False

# Background install thread coordination
_install_lock = threading.Lock()
_install_thread: threading.Thread | None = None

# Warning de-duplication. The spawn/path warnings live in the hot path —
# without this dedupe set, a Windows install where ``tirith`` isn't on PATH
# (e.g. background install thread still running, or install marked failed)
# spams ``tirith spawn failed: [WinError 2]...`` once per terminal command,
# easily filling errors.log with hundreds of identical lines.
_warned_messages: set[str] = set()
_warned_lock = threading.Lock()


def _warn_once(key: str, message: str, *args) -> None:
    """``logger.warning`` but at-most-once per ``key`` for the process
    lifetime. Used to avoid drowning the log when a fail-open tirith
    misconfiguration fires on every command."""
    with _warned_lock:
        if key in _warned_messages:
            return
        _warned_messages.add(key)
    logger.warning(message, *args)


def _reset_spawn_warning_state() -> None:
    """Clear the warn-once dedupe set. Called when tirith is freshly
    (re)installed so a subsequent failure surfaces again — e.g. user
    deletes the binary mid-session.
    """
    with _warned_lock:
        _warned_messages.clear()

# Disk-persistent failure marker — avoids retry across process restarts
_MARKER_TTL = 86400  # 24 hours


def _get_hermes_home() -> str:
    """Return the Hermes home directory, respecting HERMES_HOME env var."""
    return str(get_hermes_home())


def _failure_marker_path() -> str:
    """Return the path to the install-failure marker file."""
    return os.path.join(_get_hermes_home(), ".tirith-install-failed")


def _read_failure_reason() -> str | None:
    """Read the failure reason from the disk marker.

    Returns the reason string, or None if the marker doesn't exist or is
    older than _MARKER_TTL.
    """
    try:
        p = _failure_marker_path()
        mtime = os.path.getmtime(p)
        if (time.time() - mtime) >= _MARKER_TTL:
            return None
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _is_install_failed_on_disk() -> bool:
    """Check if a recent install failure was persisted to disk.

    Returns False (allowing retry) when:
    - No marker exists
    - Marker is older than _MARKER_TTL (24h)
    - Marker reason is 'cosign_missing' and cosign is now on PATH
    """
    reason = _read_failure_reason()
    if reason is None:
        return False
    if reason == "cosign_missing" and shutil.which("cosign"):
        _clear_install_failed()
        return False
    return True


def _mark_install_failed(reason: str = ""):
    """Persist install failure to disk to avoid retry on next process.

    Args:
        reason: Short tag identifying the failure cause. Use "cosign_missing"
                when cosign is not on PATH so the marker can be auto-cleared
                once cosign becomes available.
    """
    try:
        p = _failure_marker_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(reason)
    except OSError:
        pass


def _clear_install_failed():
    """Remove the failure marker after successful install."""
    # Reset the warn-once dedupe set so a subsequent failure (e.g. user
    # deletes the binary) surfaces in the log again instead of being
    # silently suppressed by a stale dedupe key from before the fix.
    _reset_spawn_warning_state()
    try:
        os.unlink(_failure_marker_path())
    except OSError:
        pass


def _hermes_bin_dir() -> str:
    """Return $HERMES_HOME/bin, creating new directories owner-only.

    Existing directories are not chmodded behind the user's back. Publication
    performs a descriptor-bound ownership/mode check and fails closed if an
    existing directory is unsafe.
    """
    home = os.path.abspath(_get_hermes_home())
    os.makedirs(home, mode=0o700, exist_ok=True)
    d = os.path.join(home, "bin")
    try:
        os.mkdir(d, mode=0o700)
    except FileExistsError:
        pass
    return d


def _detect_target() -> str | None:
    """Return the Rust target triple for the current platform, or None.

    Windows is intentionally unsupported — tirith does not ship a Windows
    build. Callers should treat `None` as "this platform will never have
    tirith" and silently fall back to pattern-matching guards.
    """
    system = platform.system()
    machine = platform.machine().lower()

    # Android (Termux) is ABI-compatible with Linux — reuse Linux binaries.
    if system == "Darwin":
        plat = "apple-darwin"
    elif system in {"Linux", "Android"}:
        plat = "unknown-linux-gnu"
    else:
        return None

    if machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        arch = "aarch64"
    else:
        return None

    return f"{arch}-{plat}"


def is_platform_supported() -> bool:
    """True when tirith ships a prebuilt binary for this OS+arch.

    Used by callers (CLI banner, etc.) to distinguish "tirith failed to
    install" from "tirith was never going to install here" — the latter
    is silent because there is nothing the user can do about it.
    """
    return _detect_target() is not None


def _target_from_binary_header(header: bytes) -> str | None:
    """Identify a supported executable target from a native binary header."""
    # ELF64, little-endian, current ELF version. Shared-object (ET_DYN) is
    # accepted because modern PIE executables use it; relocatable/object files
    # and unspecified types are not executable artifacts.
    if len(header) >= 20 and header[:4] == b"\x7fELF":
        if header[4] != 2 or header[5] != 1 or header[6] != 1:
            return None
        if header[7] not in {0, 3}:  # System V or GNU/Linux OSABI
            return None
        elf_type = int.from_bytes(header[16:18], "little")
        if elf_type not in {2, 3}:  # ET_EXEC or ET_DYN
            return None
        machine = int.from_bytes(header[18:20], "little")
        if machine == 0x3E:
            return "x86_64-unknown-linux-gnu"
        if machine == 0xB7:
            return "aarch64-unknown-linux-gnu"
        return None

    # Thin little-endian Mach-O 64-bit. Only MH_EXECUTE is runnable; MH_OBJECT,
    # dylibs, bundles, and malformed zero-filled fixtures must not pass.
    if len(header) >= 16 and header[:4] == b"\xcf\xfa\xed\xfe":
        if int.from_bytes(header[12:16], "little") != 2:  # MH_EXECUTE
            return None
        cpu_type = int.from_bytes(header[4:8], "little")
        if cpu_type == 0x01000007:
            return "x86_64-apple-darwin"
        if cpu_type == 0x0100000C:
            return "aarch64-apple-darwin"

    return None


def _binary_target(path: str) -> str | None:
    """Identify the supported release target encoded in a native binary header.

    Hermes downloads target-specific tirith archives, so accepting an executable
    bit alone is not enough: a copied macOS/arm64 binary is executable according
    to ``os.access`` on Linux but fails later with ``ENOEXEC``. Parse the small
    stable portion of ELF64 and Mach-O 64-bit headers directly instead of relying
    on the optional external ``file`` command.
    """
    try:
        with open(path, "rb") as binary:
            header = binary.read(32)
    except OSError:
        return None
    return _target_from_binary_header(header)


def _is_compatible_tirith_binary(path: str, target: str | None = None) -> bool:
    """Return whether ``path`` is a native tirith artifact for ``target``."""
    expected = target or _detect_target()
    if expected is None:
        return False
    actual = _binary_target(path)
    if actual == expected:
        return True
    _warn_once(
        f"tirith_incompatible_binary:{os.path.abspath(path)}:{actual}:{expected}",
        "Ignoring incompatible tirith binary at %s (found %s, expected %s)",
        path,
        actual or "unknown format",
        expected,
    )
    return False


def _is_usable_tirith_binary(path: str, target: str | None = None) -> bool:
    """Require a regular executable with the current platform's native format."""
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    expected = target or _detect_target()
    # Preserve explicit-path support on platforms for which Hermes has no
    # downloadable release. The user may provide a locally built executable,
    # and there is no supported target header against which to compare it.
    if expected is None:
        return True
    return _is_compatible_tirith_binary(path, expected)


def _download_file(url: str, dest: str, timeout: int = 10):
    """Download a URL to a local file."""
    req = urllib.request.Request(url)
    token = os.getenv("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"token {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _verify_cosign(checksums_path: str, sig_path: str, cert_path: str) -> bool | None:
    """Verify cosign provenance signature on checksums.txt.

    Returns:
        True  — cosign verified successfully
        False — cosign found but verification failed
        None  — cosign not available (not on PATH, or execution failed)

    The caller treats both False and None as "abort auto-install" — only
    True allows the install to proceed.
    """
    cosign = shutil.which("cosign")
    if not cosign:
        logger.info("cosign not found on PATH")
        return None

    try:
        result = subprocess.run(
            [cosign, "verify-blob",
             "--certificate", cert_path,
             "--signature", sig_path,
             "--certificate-identity-regexp", _COSIGN_IDENTITY_REGEXP,
             "--certificate-oidc-issuer", _COSIGN_ISSUER,
             checksums_path],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            logger.info("cosign provenance verification passed")
            return True
        else:
            logger.warning("cosign verification failed (exit %d): %s",
                          result.returncode, result.stderr.strip())
            return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("cosign execution failed: %s", exc)
        return None


def _verify_checksum(archive_path: str, checksums_path: str, archive_name: str) -> bool:
    """Verify SHA-256 of the archive against checksums.txt."""
    expected = None
    with open(checksums_path, encoding="utf-8") as f:
        for line in f:
            # Format: "<hash>  <filename>"
            parts = line.strip().split("  ", 1)
            if len(parts) == 2 and parts[1] == archive_name:
                expected = parts[0]
                break
    if not expected:
        logger.warning("No checksum entry for %s", archive_name)
        return False

    sha = hashlib.sha256()
    with open(archive_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    actual = sha.hexdigest()
    if actual != expected:
        logger.warning("Checksum mismatch: expected %s, got %s", expected, actual)
        return False
    return True


def _extract_tirith_binary(tar: tarfile.TarFile, dest_dir: str, log) -> tuple[str | None, str]:
    """Extract the tirith binary from a release archive into dest_dir."""
    for member in tar.getmembers():
        if member.name == "tirith" or member.name.endswith("/tirith"):
            if ".." in member.name:
                continue
            if not member.isfile():
                log("tirith archive member is not a regular file: %s", member.name)
                return None, "binary_not_regular_file"
            src_file = tar.extractfile(member)
            if src_file is None:
                log("tirith binary could not be read from archive")
                return None, "binary_extract_failed"

            dest_path = os.path.join(dest_dir, "tirith")
            try:
                with open(dest_path, "wb") as out:
                    shutil.copyfileobj(src_file, out)
            finally:
                src_file.close()
            return dest_path, ""

    log("tirith binary not found in archive")
    return None, "binary_not_in_archive"


_MANAGED_BINARY_MODE = 0o755


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _effective_uid() -> int | None:
    """Return the effective uid where POSIX ownership checks are available."""
    getter = getattr(os, "geteuid", None)
    return getter() if getter is not None else None


def _open_secure_install_dir(path: str) -> int:
    """Open and bind the managed bin directory after security validation."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    dir_fd = os.open(path, flags)
    try:
        bound = os.fstat(dir_fd)
        named = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(bound.st_mode) or not _same_inode(bound, named):
            raise OSError(errno.EPERM, "managed bin path is not a stable directory", path)
        owner = _effective_uid()
        if owner is None or bound.st_uid != owner:
            raise OSError(errno.EPERM, "managed bin directory is not owned by current user", path)
        if stat.S_IMODE(bound.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise OSError(errno.EPERM, "managed bin directory is group/world-writable", path)
        return dir_fd
    except Exception:
        os.close(dir_fd)
        raise


def _open_staged_binary(dir_fd: int) -> tuple[int, str]:
    """Create an unpredictable no-follow staging file in the destination dir."""
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _ in range(16):
        name = f".tirith-stage-{secrets.token_hex(16)}"
        try:
            return os.open(name, flags, 0o700, dir_fd=dir_fd), name
        except FileExistsError:
            continue
    raise OSError(errno.EEXIST, "could not allocate a unique Tirith staging file")


def _copy_binary_to_fd(src: str, dest_fd: int) -> bytes:
    digest = hashlib.sha256()
    with open(src, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(dest_fd, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short write while staging Tirith")
                view = view[written:]
    return digest.digest()


def _digest_fd(fd: int) -> bytes:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.digest()


def _verify_managed_binary_fd(fd: int, target: str, digest: bytes) -> bool:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        return False
    owner = _effective_uid()
    if owner is None or info.st_uid != owner or info.st_nlink != 1:
        return False
    if stat.S_IMODE(info.st_mode) != _MANAGED_BINARY_MODE:
        return False
    if _digest_fd(fd) != digest:
        return False
    os.lseek(fd, 0, os.SEEK_SET)
    return _target_from_binary_header(os.read(fd, 32)) == target


def _publish_tirith_binary(src: str, target: str) -> tuple[str | None, str]:
    """Atomically publish a verified executable without following destination links."""
    try:
        bin_dir = _hermes_bin_dir()
        dir_fd = _open_secure_install_dir(bin_dir)
    except OSError:
        return None, "install_directory_insecure"

    stage_fd: int | None = None
    stage_name: str | None = None
    stage_identity: os.stat_result | None = None
    try:
        stage_fd, stage_name = _open_staged_binary(dir_fd)
        stage_identity = os.fstat(stage_fd)
        digest = _copy_binary_to_fd(src, stage_fd)
        os.fchmod(stage_fd, _MANAGED_BINARY_MODE)
        os.fsync(stage_fd)
        if not _verify_managed_binary_fd(stage_fd, target, digest):
            return None, "staged_binary_unusable"

        # Both names are relative to the same descriptor-bound directory, so
        # publication cannot cross devices. Never fall back to a symlink-following
        # copy if replace is unavailable or fails.
        os.replace(stage_name, "tirith", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        stage_name = None
        os.fsync(dir_fd)

        read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        read_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        published_fd = os.open("tirith", read_flags, dir_fd=dir_fd)
        try:
            published = os.fstat(published_fd)
            named = os.stat("tirith", dir_fd=dir_fd, follow_symlinks=False)
            canonical_dir = os.stat(bin_dir, follow_symlinks=False)
            if not _same_inode(published, named):
                return None, "published_binary_rebound"
            if not _same_inode(os.fstat(dir_fd), canonical_dir):
                return None, "install_directory_rebound"
            if not _verify_managed_binary_fd(published_fd, target, digest):
                return None, "published_binary_unusable"
        finally:
            os.close(published_fd)

        return os.path.join(bin_dir, "tirith"), ""
    except OSError:
        return None, "atomic_publish_failed"
    finally:
        if stage_name is not None and stage_identity is not None:
            try:
                named_stage = os.stat(stage_name, dir_fd=dir_fd, follow_symlinks=False)
                if _same_inode(stage_identity, named_stage):
                    os.unlink(stage_name, dir_fd=dir_fd)
            except OSError:
                pass
        if stage_fd is not None:
            os.close(stage_fd)
        os.close(dir_fd)


def _install_tirith(*, log_failures: bool = True) -> tuple[str | None, str]:
    """Download and install tirith to $HERMES_HOME/bin/tirith.

    Verifies provenance via cosign and SHA-256 checksum.
    Returns (installed_path, failure_reason).  On success failure_reason is "".
    failure_reason is a short tag used by the disk marker to decide if the
    failure is retryable (e.g. "cosign_missing" clears when cosign appears).
    """
    log = logger.warning if log_failures else logger.debug

    target = _detect_target()
    if not target:
        logger.info("tirith auto-install: unsupported platform %s/%s",
                     platform.system(), platform.machine())
        return None, "unsupported_platform"

    archive_name = f"tirith-{target}.tar.gz"
    base_url = f"https://github.com/{_REPO}/releases/latest/download"

    try:
        tmpdir = tempfile.mkdtemp(prefix="tirith-install-")
    except OSError as exc:
        log("tirith install failed: cannot create temp dir: %s", exc)
        return None, "no_space"
    try:
        archive_path = os.path.join(tmpdir, archive_name)
        checksums_path = os.path.join(tmpdir, "checksums.txt")
        sig_path = os.path.join(tmpdir, "checksums.txt.sig")
        cert_path = os.path.join(tmpdir, "checksums.txt.pem")

        logger.info("tirith not found — downloading latest release for %s...", target)

        try:
            _download_file(f"{base_url}/{archive_name}", archive_path)
            _download_file(f"{base_url}/checksums.txt", checksums_path)
        except Exception as exc:
            log("tirith download failed: %s", exc)
            return None, "download_failed"

        # Cosign provenance verification — preferred but not mandatory.
        # When cosign is available, we verify that the release was produced
        # by the expected GitHub Actions workflow (full supply chain proof).
        # Without cosign, SHA-256 checksum + HTTPS still provides integrity
        # and transport-level authenticity.
        cosign_verified = False
        if shutil.which("cosign"):
            try:
                _download_file(f"{base_url}/checksums.txt.sig", sig_path)
                _download_file(f"{base_url}/checksums.txt.pem", cert_path)
            except Exception as exc:
                logger.info("cosign artifacts unavailable (%s), proceeding with SHA-256 only", exc)
            else:
                cosign_result = _verify_cosign(checksums_path, sig_path, cert_path)
                if cosign_result is True:
                    cosign_verified = True
                elif cosign_result is False:
                    # Verification explicitly rejected — abort, the release
                    # may have been tampered with.
                    log("tirith install aborted: cosign provenance verification failed")
                    return None, "cosign_verification_failed"
                else:
                    # None = execution failure (timeout/OSError) — proceed
                    # with SHA-256 only since cosign itself is broken.
                    logger.info("cosign execution failed, proceeding with SHA-256 only")
        else:
            logger.info("cosign not on PATH — installing tirith with SHA-256 verification only "
                        "(install cosign for full supply chain verification)")

        if not _verify_checksum(archive_path, checksums_path, archive_name):
            return None, "checksum_failed"

        with tarfile.open(archive_path, "r:gz") as tar:
            src, reason = _extract_tirith_binary(tar, tmpdir, log)
            if src is None:
                return None, reason

        # The checksum authenticates archive bytes, but it cannot prove that
        # release asset selection matched this host. Refuse to publish a
        # cross-platform/cross-architecture binary into HERMES_HOME/bin.
        if not _is_compatible_tirith_binary(src, target):
            log("tirith release binary is incompatible with target %s", target)
            return None, "binary_target_mismatch"

        dest, publish_reason = _publish_tirith_binary(src, target)
        if dest is None:
            log("tirith install failed during atomic publication: %s", publish_reason)
            return None, publish_reason

        verification = "cosign + SHA-256" if cosign_verified else "SHA-256 only"
        logger.info("tirith installed to %s (%s)", dest, verification)
        return dest, ""

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _is_explicit_path(configured_path: str) -> bool:
    """Return True if the user explicitly configured a non-default tirith path."""
    return configured_path != "tirith"


def _canonical_binary_path(path: str) -> str:
    """Freeze a successful relative resolution against later cwd changes."""
    return os.path.abspath(os.path.expanduser(path))


def _resolve_tirith_path(configured_path: str) -> str | None:
    """Resolve a currently usable Tirith path, auto-installing if necessary.

    Every successful resolution is cached as an absolute path and revalidated on
    every call. Missing, rejected, failed, and install-in-progress states return
    None so the caller applies fail-open/fail-closed without spawning a known-bad
    PATH artifact or incrementing the crash circuit.
    """
    global _resolved_path, _install_failure_reason

    if isinstance(_resolved_path, str):
        # The exact bare default exists only for legacy/test callers. Production
        # resolutions below are always canonical absolute paths.
        if _resolved_path == "tirith":
            return _resolved_path
        cached = _canonical_binary_path(_resolved_path)
        if _is_usable_tirith_binary(cached):
            _resolved_path = cached
            return cached
        _resolved_path = None
        _install_failure_reason = "cached_binary_incompatible"

    expanded = os.path.expanduser(configured_path)
    explicit = _is_explicit_path(configured_path)
    install_failed = _resolved_path is _INSTALL_FAILED

    if not explicit and not is_platform_supported():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "unsupported_platform"
        return None

    if explicit:
        candidate = _canonical_binary_path(expanded)
        if _is_usable_tirith_binary(candidate):
            _resolved_path = candidate
            _install_failure_reason = ""
            return candidate
        found = shutil.which(expanded)
        if found:
            found = _canonical_binary_path(found)
        if found and _is_usable_tirith_binary(found):
            _resolved_path = found
            _install_failure_reason = ""
            return found
        logger.warning(
            "Configured tirith path %r is missing or incompatible; scanning disabled",
            configured_path,
        )
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "explicit_path_unusable"
        return None

    # Always re-run cheap local checks so a corrected manual install is picked up
    # even after a previous network failure.
    found = shutil.which("tirith")
    if found:
        found = _canonical_binary_path(found)
    if found and _is_usable_tirith_binary(found):
        _resolved_path = found
        _install_failure_reason = ""
        _clear_install_failed()
        return found

    hermes_bin = _canonical_binary_path(os.path.join(_hermes_bin_dir(), "tirith"))
    if _is_usable_tirith_binary(hermes_bin):
        _resolved_path = hermes_bin
        _install_failure_reason = ""
        _clear_install_failed()
        return hermes_bin

    if install_failed:
        if _install_failure_reason == "cosign_missing" and shutil.which("cosign"):
            _resolved_path = None
            _install_failure_reason = ""
            _clear_install_failed()
            install_failed = False
        else:
            return None

    if _install_thread is not None and _install_thread.is_alive():
        return None

    disk_reason = _read_failure_reason()
    if disk_reason is not None and _is_install_failed_on_disk():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = disk_reason
        return None

    installed, reason = _install_tirith()
    if installed:
        installed = _canonical_binary_path(installed)
        _resolved_path = installed
        _install_failure_reason = ""
        _clear_install_failed()
        _reset_tirith_circuit()
        return installed

    _resolved_path = _INSTALL_FAILED
    _install_failure_reason = reason
    _mark_install_failed(reason)
    return None


def _background_install(*, log_failures: bool = True):
    """Background thread target: download and install tirith."""
    global _resolved_path, _install_failure_reason
    with _install_lock:
        # Double-check after acquiring lock (another thread may have resolved)
        if _resolved_path is not None:
            return

        # Re-check local paths (may have been installed by another process)
        found = shutil.which("tirith")
        if found:
            found = _canonical_binary_path(found)
        if found and _is_usable_tirith_binary(found):
            _resolved_path = found
            _install_failure_reason = ""
            _reset_tirith_circuit()
            return

        hermes_bin = _canonical_binary_path(os.path.join(_hermes_bin_dir(), "tirith"))
        if _is_usable_tirith_binary(hermes_bin):
            _resolved_path = hermes_bin
            _install_failure_reason = ""
            _reset_tirith_circuit()
            return

        installed, reason = _install_tirith(log_failures=log_failures)
        if installed:
            _resolved_path = _canonical_binary_path(installed)
            _install_failure_reason = ""
            _clear_install_failed()
            _reset_tirith_circuit()
        else:
            _resolved_path = _INSTALL_FAILED
            _install_failure_reason = reason
            _mark_install_failed(reason)


def ensure_installed(*, log_failures: bool = True):
    """Ensure tirith is available, downloading in background if needed.

    Quick PATH/local checks are synchronous; network download runs in a
    daemon thread so startup never blocks. Safe to call multiple times.
    Returns the resolved path immediately if available, or None.
    """
    global _resolved_path, _install_thread, _install_failure_reason

    cfg = _load_security_config()
    if not cfg["tirith_enabled"]:
        return None

    # Already resolved from a previous call
    if isinstance(_resolved_path, str):
        path = _resolved_path
        if path == "tirith":
            return path
        path = _canonical_binary_path(path)
        if _is_usable_tirith_binary(path):
            _resolved_path = path
            return path
        _resolved_path = None
        _install_failure_reason = "cached_binary_incompatible"

    # Platform has no tirith build (e.g. Windows) — don't probe PATH,
    # don't start a download thread, don't write a disk failure marker.
    # Pattern-matching guards still run; this path stays silent.
    if not is_platform_supported():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "unsupported_platform"
        return None

    configured_path = cfg["tirith_path"]
    explicit = _is_explicit_path(configured_path)
    expanded = os.path.expanduser(configured_path)

    # Explicit path: synchronous check only, no download
    if explicit:
        candidate = _canonical_binary_path(expanded)
        if _is_usable_tirith_binary(candidate):
            _resolved_path = candidate
            _reset_tirith_circuit()
            return candidate
        found = shutil.which(expanded)
        if found:
            found = _canonical_binary_path(found)
        if found and _is_usable_tirith_binary(found):
            _resolved_path = found
            _reset_tirith_circuit()
            return found
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "explicit_path_unusable"
        return None

    # Default "tirith" — quick local checks first (no network)
    found = shutil.which("tirith")
    if found:
        found = _canonical_binary_path(found)
    if found and _is_usable_tirith_binary(found):
        _resolved_path = found
        _install_failure_reason = ""
        _clear_install_failed()
        _reset_tirith_circuit()
        return found

    hermes_bin = _canonical_binary_path(os.path.join(_hermes_bin_dir(), "tirith"))
    if _is_usable_tirith_binary(hermes_bin):
        _resolved_path = hermes_bin
        _install_failure_reason = ""
        _clear_install_failed()
        _reset_tirith_circuit()
        return hermes_bin

    # If previously failed in-memory, check if the cause is now resolved
    if _resolved_path is _INSTALL_FAILED:
        if _install_failure_reason == "cosign_missing" and shutil.which("cosign"):
            _resolved_path = None
            _install_failure_reason = ""
            _clear_install_failed()
        else:
            return None

    # Check disk failure marker (skip network attempt for 24h, unless
    # the cosign_missing reason was resolved — handled by _is_install_failed_on_disk).
    # Preserve the marker's real reason for in-memory retry logic.
    disk_reason = _read_failure_reason()
    if disk_reason is not None and _is_install_failed_on_disk():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = disk_reason
        return None

    # Need to download — launch background thread so startup doesn't block
    if _install_thread is None or not _install_thread.is_alive():
        _install_thread = threading.Thread(
            target=_background_install,
            kwargs={"log_failures": log_failures},
            daemon=True,
        )
        _install_thread.start()

    return None  # Not available yet; commands will fail-open until ready


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

_MAX_FINDINGS = 50
_MAX_SUMMARY_LEN = 500


def check_command_security(command: str) -> dict:
    """Run tirith security scan on a command.

    Exit code determines action (0=allow, 1=block, 2=warn). JSON enriches
    findings/summary. Spawn failures and timeouts respect fail_open config.
    Programming errors propagate.

    Returns:
        {"action": "allow"|"warn"|"block", "findings": [...], "summary": str}
    """
    global _crash_count, _circuit_open

    cfg = _load_security_config()

    if not cfg["tirith_enabled"]:
        return {"action": "allow", "findings": [], "summary": ""}

    fail_open = cfg["tirith_fail_open"]

    # Circuit breaker: if tirith has crashed _CRASH_LIMIT times in a row,
    # stop trying for the rest of the process.  Without this, a corrupted
    # or missing binary causes every tool call to hit the same spawn failure
    # → fail-open → agent retry loop, hanging the user for 20+ minutes
    # (issue #41400).
    if _circuit_open:
        action = "allow" if fail_open else "block"
        suffix = "" if fail_open else " (fail-closed)"
        return {
            "action": action,
            "findings": [],
            "summary": f"tirith disabled (circuit breaker){suffix}",
        }

    # Unsupported platform (Windows etc.) — tirith has no binary here and
    # never will. Skip the resolver entirely so we don't even try to spawn.
    # Pattern-matching guards still run via the rest of approval.py.
    if not is_platform_supported():
        return {"action": "allow", "findings": [], "summary": ""}

    tirith_path = _resolve_tirith_path(cfg["tirith_path"])
    timeout = cfg["tirith_timeout"]

    if tirith_path is None:
        _warn_once(
            "tirith_path_none",
            "tirith path resolved to None; scanning disabled",
        )
        if fail_open:
            return {"action": "allow", "findings": [], "summary": "tirith path unavailable"}
        return {"action": "block", "findings": [], "summary": "tirith path unavailable (fail-closed)"}

    try:
        result = subprocess.run(
            [tirith_path, "check", "--json", "--non-interactive",
             "--shell", "posix", "--", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        # Covers FileNotFoundError, PermissionError, exec format error.
        # Dedupe by ``(errno, exc class)`` so a transient failure mode
        # surfaces once but doesn't drown the log on every command —
        # commonly seen on Windows when the configured path "tirith"
        # isn't on PATH yet (background install still running, or
        # install marked failed for the day).
        spawn_key = f"tirith_spawn_failed:{type(exc).__name__}:{getattr(exc, 'errno', '')}"
        _warn_once(spawn_key, "tirith spawn failed: %s", exc)
        _record_tirith_crash()
        if fail_open:
            return {"action": "allow", "findings": [], "summary": f"tirith unavailable: {exc}"}
        return {"action": "block", "findings": [], "summary": f"tirith spawn failed (fail-closed): {exc}"}
    except subprocess.TimeoutExpired:
        _warn_once(
            f"tirith_timeout:{timeout}",
            "tirith timed out after %ds",
            timeout,
        )
        _record_tirith_crash()
        if fail_open:
            return {"action": "allow", "findings": [], "summary": f"tirith timed out ({timeout}s)"}
        return {"action": "block", "findings": [], "summary": "tirith timed out (fail-closed)"}

    # Map exit code to action
    exit_code = result.returncode
    if exit_code == 0:
        action = "allow"
        # Successful execution — reset circuit breaker
        _crash_count = 0
    elif exit_code == 1:
        action = "block"
    elif exit_code == 2:
        action = "warn"
    else:
        # Unknown exit code (includes signal-killed processes like -11/SIGSEGV)
        # — respect fail_open
        logger.warning("tirith returned unexpected exit code %d", exit_code)
        _record_tirith_crash()
        if fail_open:
            return {"action": "allow", "findings": [], "summary": f"tirith exit code {exit_code} (fail-open)"}
        return {"action": "block", "findings": [], "summary": f"tirith exit code {exit_code} (fail-closed)"}

    # Parse JSON for enrichment (never overrides the exit code verdict)
    findings = []
    summary = ""
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        raw_findings = data.get("findings", [])
        findings = raw_findings[:_MAX_FINDINGS]
        summary = (data.get("summary", "") or "")[:_MAX_SUMMARY_LEN]
    except (json.JSONDecodeError, AttributeError):
        # JSON parse failure degrades findings/summary, not the verdict
        logger.debug("tirith JSON parse failed, using exit code only")
        if action == "block":
            summary = "security issue detected (details unavailable)"
        elif action == "warn":
            summary = "security warning detected (details unavailable)"

    # Suppress warn verdicts that consist solely of a lookalike_tld finding for
    # the .app TLD.  .app is a legitimate gTLD used by many production services
    # and the "can be confused with file extensions" heuristic generates false
    # positives for normal API calls.  Any other finding (including other
    # lookalike_tld entries for non-.app TLDs) preserves the warn action.
    if action == "warn" and findings:
        non_suppressible = [f for f in findings if not _is_app_tld_finding(f)]
        if not non_suppressible:
            action = "allow"
            findings = []
            summary = ""

    return {"action": action, "findings": findings, "summary": summary}


def _is_app_tld_finding(finding: dict) -> bool:
    """Return True if this finding is a lookalike_tld warning for the .app TLD only.

    Checks the rule_id and inspects common value/detail field names that
    Tirith may use to carry the TLD string.
    """
    if not isinstance(finding, dict):
        return False
    if finding.get("rule_id") != "lookalike_tld":
        return False
    for field in ("value", "tld", "detail", "description", "message"):
        val = finding.get(field)
        if val is not None and ".app" in str(val).lower():
            return True
    return False
