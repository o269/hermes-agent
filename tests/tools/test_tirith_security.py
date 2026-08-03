"""Tests for the tirith security scanning subprocess wrapper."""

import errno
import io
import json
import os
import stat
import subprocess
import tarfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import tools.tirith_security as _tirith_mod
from tools.tirith_security import check_command_security, ensure_installed


@pytest.fixture(autouse=True)
def _reset_resolved_path():
    """Pre-set cached path to skip auto-install in scan tests.
    Tests that specifically test ensure_installed / resolve behavior
    reset this to None themselves.
    """
    _tirith_mod._resolved_path = "tirith"
    _tirith_mod._install_thread = None
    _tirith_mod._install_failure_reason = ""
    _tirith_mod._crash_count = 0
    _tirith_mod._circuit_open = False
    yield
    _tirith_mod._resolved_path = None
    _tirith_mod._install_thread = None
    _tirith_mod._install_failure_reason = ""
    _tirith_mod._crash_count = 0
    _tirith_mod._circuit_open = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_run(returncode=0, stdout="", stderr=""):
    """Build a mock subprocess.CompletedProcess."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def _json_stdout(findings=None, summary=""):
    return json.dumps({"findings": findings or [], "summary": summary})


def _native_binary_bytes(target: str) -> bytes:
    """Minimal native header bytes accepted by the target-format parser."""
    header = bytearray(64)
    if target.endswith("unknown-linux-gnu"):
        header[:8] = b"\x7fELF\x02\x01\x01\x00"
        header[16:18] = (2).to_bytes(2, "little")  # ET_EXEC
        machine = {
            "x86_64-unknown-linux-gnu": 0x3E,
            "aarch64-unknown-linux-gnu": 0xB7,
        }[target]
        header[18:20] = machine.to_bytes(2, "little")
        return bytes(header)

    header[:4] = b"\xcf\xfa\xed\xfe"
    cpu_type = {
        "x86_64-apple-darwin": 0x01000007,
        "aarch64-apple-darwin": 0x0100000C,
    }[target]
    header[4:8] = cpu_type.to_bytes(4, "little")
    header[12:16] = (2).to_bytes(4, "little")  # MH_EXECUTE
    return bytes(header)


def _foreign_target(target: str) -> str:
    targets = [
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "x86_64-apple-darwin",
        "aarch64-apple-darwin",
    ]
    return next(candidate for candidate in targets if candidate != target)


# ---------------------------------------------------------------------------
# Exit code → action mapping
# ---------------------------------------------------------------------------

class TestExitCodeMapping:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_0_allow(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.return_value = _mock_run(0, _json_stdout())
        result = check_command_security("echo hello")
        assert result["action"] == "allow"
        assert result["findings"] == []

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_1_block_with_findings(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        findings = [{"rule_id": "homograph_url", "severity": "high"}]
        mock_run.return_value = _mock_run(1, _json_stdout(findings, "homograph detected"))
        result = check_command_security("curl http://gооgle.com")
        assert result["action"] == "block"
        assert len(result["findings"]) == 1
        assert result["summary"] == "homograph detected"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_2_warn_with_findings(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        findings = [{"rule_id": "shortened_url", "severity": "medium"}]
        mock_run.return_value = _mock_run(2, _json_stdout(findings, "shortened URL"))
        result = check_command_security("curl https://bit.ly/abc")
        assert result["action"] == "warn"
        assert len(result["findings"]) == 1
        assert result["summary"] == "shortened URL"


# ---------------------------------------------------------------------------
# JSON parse failure (exit code still wins)
# ---------------------------------------------------------------------------

class TestJsonParseFailure:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_1_invalid_json_still_blocks(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.return_value = _mock_run(1, "NOT JSON")
        result = check_command_security("bad command")
        assert result["action"] == "block"
        assert "details unavailable" in result["summary"]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_2_invalid_json_still_warns(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.return_value = _mock_run(2, "{broken")
        result = check_command_security("suspicious command")
        assert result["action"] == "warn"
        assert "details unavailable" in result["summary"]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_0_invalid_json_allows(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.return_value = _mock_run(0, "NOT JSON")
        result = check_command_security("safe command")
        assert result["action"] == "allow"


# ---------------------------------------------------------------------------
# Operational failures + fail_open
# ---------------------------------------------------------------------------

class TestOSErrorFailOpen:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_file_not_found_fail_open(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.side_effect = FileNotFoundError("No such file: tirith")
        result = check_command_security("echo hi")
        assert result["action"] == "allow"
        assert "unavailable" in result["summary"]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_permission_error_fail_open(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.side_effect = PermissionError("Permission denied")
        result = check_command_security("echo hi")
        assert result["action"] == "allow"
        assert "unavailable" in result["summary"]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_os_error_fail_closed(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": False}
        mock_run.side_effect = FileNotFoundError("No such file: tirith")
        result = check_command_security("echo hi")
        assert result["action"] == "block"
        assert "fail-closed" in result["summary"]

    @patch("tools.tirith_security._load_security_config")
    def test_open_circuit_honors_fail_closed(self, mock_cfg):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": False}
        _tirith_mod._circuit_open = True
        result = check_command_security("echo hi")
        assert result["action"] == "block"
        assert "circuit breaker" in result["summary"]
        assert "fail-closed" in result["summary"]


class TestTimeoutFailOpen:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_timeout_fail_open(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tirith", timeout=5)
        result = check_command_security("slow command")
        assert result["action"] == "allow"
        assert "timed out" in result["summary"]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_timeout_fail_closed(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": False}
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tirith", timeout=5)
        result = check_command_security("slow command")
        assert result["action"] == "block"
        assert "fail-closed" in result["summary"]


class TestUnknownExitCode:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_unknown_exit_code_fail_open(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.return_value = _mock_run(99, "")
        result = check_command_security("cmd")
        assert result["action"] == "allow"
        assert "exit code 99" in result["summary"]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_unknown_exit_code_fail_closed(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": False}
        mock_run.return_value = _mock_run(99, "")
        result = check_command_security("cmd")
        assert result["action"] == "block"
        assert "exit code 99" in result["summary"]


# ---------------------------------------------------------------------------
# Disabled + path expansion
# ---------------------------------------------------------------------------

class TestDisabled:
    @patch("tools.tirith_security._load_security_config")
    def test_disabled_returns_allow(self, mock_cfg):
        mock_cfg.return_value = {"tirith_enabled": False, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        result = check_command_security("rm -rf /")
        assert result["action"] == "allow"


class TestPathExpansion:
    def test_tilde_expanded_in_resolve(self, tmp_path, monkeypatch):
        """_resolve_tirith_path should expand ~ and cache an absolute path."""
        from tools.tirith_security import _resolve_tirith_path
        home = tmp_path / "home"
        binary = home / "bin" / "tirith"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(_native_binary_bytes("x86_64-unknown-linux-gnu"))
        binary.chmod(0o755)
        monkeypatch.setenv("HOME", str(home))
        _tirith_mod._resolved_path = None
        with patch(
            "tools.tirith_security._detect_target",
            return_value="x86_64-unknown-linux-gnu",
        ):
            result = _resolve_tirith_path("~/bin/tirith")
        assert result is not None
        assert result == str(binary)
        assert "~" not in result
        _tirith_mod._resolved_path = None


# ---------------------------------------------------------------------------
# Findings cap + summary cap
# ---------------------------------------------------------------------------

class TestCaps:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_findings_capped_at_50(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        findings = [{"rule_id": f"rule_{i}"} for i in range(100)]
        mock_run.return_value = _mock_run(2, _json_stdout(findings, "many findings"))
        result = check_command_security("cmd")
        assert len(result["findings"]) == 50

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_summary_capped_at_500(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        long_summary = "x" * 1000
        mock_run.return_value = _mock_run(2, _json_stdout([], long_summary))
        result = check_command_security("cmd")
        assert len(result["summary"]) == 500


# ---------------------------------------------------------------------------
# Programming errors propagate
# ---------------------------------------------------------------------------

class TestProgrammingErrors:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_attribute_error_propagates(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.side_effect = AttributeError("unexpected bug")
        with pytest.raises(AttributeError):
            check_command_security("cmd")

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_type_error_propagates(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.side_effect = TypeError("unexpected bug")
        with pytest.raises(TypeError):
            check_command_security("cmd")


# ---------------------------------------------------------------------------
# ensure_installed
# ---------------------------------------------------------------------------

class TestEnsureInstalled:
    @patch("tools.tirith_security._load_security_config")
    def test_disabled_returns_none(self, mock_cfg):
        mock_cfg.return_value = {"tirith_enabled": False, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        _tirith_mod._resolved_path = None
        assert ensure_installed() is None

    @patch("tools.tirith_security.shutil.which", return_value="/usr/local/bin/tirith")
    @patch("tools.tirith_security._load_security_config")
    def test_found_on_path_returns_immediately(self, mock_cfg, mock_which):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        _tirith_mod._resolved_path = None
        with patch("os.path.isfile", return_value=True), \
             patch("os.access", return_value=True), \
             patch("tools.tirith_security._is_compatible_tirith_binary", return_value=True):
            result = ensure_installed()
        assert result == "/usr/local/bin/tirith"
        _tirith_mod._resolved_path = None

    @patch("tools.tirith_security._load_security_config")
    def test_not_found_returns_none(self, mock_cfg):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        _tirith_mod._resolved_path = None
        with patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._is_install_failed_on_disk", return_value=False), \
             patch("tools.tirith_security.threading.Thread") as MockThread:
            mock_thread = MagicMock()
            MockThread.return_value = mock_thread
            result = ensure_installed()
            assert result is None
            # Should have launched background thread
            mock_thread.start.assert_called_once()
        _tirith_mod._resolved_path = None

    @patch("tools.tirith_security._load_security_config")
    def test_startup_prefetch_can_suppress_install_failure_logs(self, mock_cfg):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        _tirith_mod._resolved_path = None
        with patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._is_install_failed_on_disk", return_value=False), \
             patch("tools.tirith_security.threading.Thread") as MockThread:
            mock_thread = MagicMock()
            MockThread.return_value = mock_thread
            result = ensure_installed(log_failures=False)
            assert result is None
            assert MockThread.call_args.kwargs["kwargs"] == {"log_failures": False}
            mock_thread.start.assert_called_once()
        _tirith_mod._resolved_path = None


# ---------------------------------------------------------------------------
# Unsupported platform (Windows etc.) — silent fast-path everywhere
# ---------------------------------------------------------------------------

class TestUnsupportedPlatform:
    """When _detect_target() returns None (no tirith binary for this OS+arch),
    the entire subsystem must stay silent: no PATH probes, no download thread,
    no disk failure marker, no spawn attempts, no CLI banner. Pattern-matching
    guards still cover the gap; tirith content scanning is just absent."""

    def test_is_platform_supported_true_on_linux_x86_64(self):
        with patch("tools.tirith_security.platform.system", return_value="Linux"), \
             patch("tools.tirith_security.platform.machine", return_value="x86_64"):
            assert _tirith_mod.is_platform_supported() is True

    def test_is_platform_supported_true_on_darwin_arm64(self):
        with patch("tools.tirith_security.platform.system", return_value="Darwin"), \
             patch("tools.tirith_security.platform.machine", return_value="arm64"):
            assert _tirith_mod.is_platform_supported() is True

    def test_is_platform_supported_false_on_windows(self):
        with patch("tools.tirith_security.platform.system", return_value="Windows"), \
             patch("tools.tirith_security.platform.machine", return_value="AMD64"):
            assert _tirith_mod.is_platform_supported() is False

    def test_is_platform_supported_false_on_unknown_arch(self):
        with patch("tools.tirith_security.platform.system", return_value="Linux"), \
             patch("tools.tirith_security.platform.machine", return_value="riscv64"):
            assert _tirith_mod.is_platform_supported() is False

    @patch("tools.tirith_security._load_security_config")
    def test_ensure_installed_unsupported_returns_none_no_thread(self, mock_cfg):
        """Windows: don't start a background install thread, don't write a
        failure marker — just cache the verdict and return None."""
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        _tirith_mod._resolved_path = None
        with patch("tools.tirith_security.is_platform_supported", return_value=False), \
             patch("tools.tirith_security.threading.Thread") as MockThread, \
             patch("tools.tirith_security._mark_install_failed") as mock_mark, \
             patch("tools.tirith_security.shutil.which") as mock_which:
            result = ensure_installed()
            assert result is None
            MockThread.assert_not_called()
            mock_mark.assert_not_called()
            mock_which.assert_not_called()
            assert _tirith_mod._resolved_path is _tirith_mod._INSTALL_FAILED
            assert _tirith_mod._install_failure_reason == "unsupported_platform"

    @patch("tools.tirith_security._load_security_config")
    def test_check_command_security_unsupported_allows_silently(self, mock_cfg):
        """Windows: skip the resolver and spawn entirely — return allow with
        an empty summary so callers can't accidentally surface 'tirith
        unavailable' messaging to the user."""
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        with patch("tools.tirith_security.is_platform_supported", return_value=False), \
             patch("tools.tirith_security.subprocess.run") as mock_run, \
             patch("tools.tirith_security._resolve_tirith_path") as mock_resolve:
            result = check_command_security("rm -rf /")
            assert result == {"action": "allow", "findings": [], "summary": ""}
            mock_run.assert_not_called()
            mock_resolve.assert_not_called()

    @patch("tools.tirith_security._load_security_config")
    def test_resolve_path_unsupported_caches_failure_without_probing(self, mock_cfg):
        """The per-command resolver must also short-circuit on Windows so
        long-running gateways don't churn through `shutil.which` and disk
        I/O for every scanned command."""
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        _tirith_mod._resolved_path = None
        with patch("tools.tirith_security.is_platform_supported", return_value=False), \
             patch("tools.tirith_security.shutil.which") as mock_which:
            result = _tirith_mod._resolve_tirith_path("tirith")
            assert result is None
            mock_which.assert_not_called()
            assert _tirith_mod._resolved_path is _tirith_mod._INSTALL_FAILED
            assert _tirith_mod._install_failure_reason == "unsupported_platform"

    @patch("tools.tirith_security._load_security_config")
    def test_explicit_path_still_honored_on_unsupported_platform(self, mock_cfg):
        """If a user explicitly configured a tirith_path (e.g. they built it
        themselves under WSL), the unsupported-platform short-circuit must
        NOT override that — explicit config wins."""
        mock_cfg.return_value = {"tirith_enabled": True,
                                 "tirith_path": "/opt/custom/tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        _tirith_mod._resolved_path = None
        with patch("tools.tirith_security._detect_target", return_value=None), \
             patch("os.path.isfile", return_value=True), \
             patch("os.access", return_value=True):
            result = _tirith_mod._resolve_tirith_path("/opt/custom/tirith")
            assert result == "/opt/custom/tirith"
            assert _tirith_mod._resolved_path == "/opt/custom/tirith"


# ---------------------------------------------------------------------------
# Native release target validation
# ---------------------------------------------------------------------------

class TestBinaryTargetValidation:
    TARGETS = [
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "x86_64-apple-darwin",
        "aarch64-apple-darwin",
    ]

    @pytest.mark.parametrize("target", TARGETS)
    def test_binary_target_recognizes_supported_native_headers(self, target, tmp_path):
        binary = tmp_path / "tirith"
        binary.write_bytes(_native_binary_bytes(target))
        assert _tirith_mod._binary_target(str(binary)) == target

    @pytest.mark.parametrize("elf_type", [0, 1])  # ET_NONE, ET_REL
    def test_binary_target_rejects_non_executable_elf_types(self, elf_type, tmp_path):
        payload = bytearray(_native_binary_bytes("x86_64-unknown-linux-gnu"))
        payload[16:18] = elf_type.to_bytes(2, "little")
        binary = tmp_path / "tirith"
        binary.write_bytes(payload)
        assert _tirith_mod._binary_target(str(binary)) is None

    @pytest.mark.parametrize("file_type", [0, 1, 6, 8])
    def test_binary_target_rejects_non_executable_macho_types(self, file_type, tmp_path):
        payload = bytearray(_native_binary_bytes("aarch64-apple-darwin"))
        payload[12:16] = file_type.to_bytes(4, "little")
        binary = tmp_path / "tirith"
        binary.write_bytes(payload)
        assert _tirith_mod._binary_target(str(binary)) is None

    @pytest.mark.parametrize("payload", [b"", b"\x7fELF", b"not-a-binary" * 4])
    def test_binary_target_rejects_malformed_headers(self, payload, tmp_path):
        binary = tmp_path / "tirith"
        binary.write_bytes(payload)
        assert _tirith_mod._binary_target(str(binary)) is None

    @pytest.mark.parametrize("target", TARGETS)
    def test_rejects_foreign_binary_for_every_target(self, target, tmp_path):
        binary = tmp_path / "tirith"
        binary.write_bytes(_native_binary_bytes(_foreign_target(target)))
        binary.chmod(0o755)
        assert not _tirith_mod._is_compatible_tirith_binary(str(binary), target)

    @pytest.mark.parametrize("target", TARGETS)
    @patch("tools.tirith_security._mark_install_failed")
    @patch("tools.tirith_security._is_install_failed_on_disk", return_value=False)
    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_resolver_replaces_incompatible_managed_binary(
        self, mock_which, mock_disk_check, mock_mark, target, tmp_path, monkeypatch
    ):
        del mock_which, mock_disk_check, mock_mark
        hermes_home = tmp_path / "hermes-home"
        managed = hermes_home / "bin" / "tirith"
        managed.parent.mkdir(parents=True)
        managed.write_bytes(_native_binary_bytes(_foreign_target(target)))
        managed.chmod(0o755)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        _tirith_mod._resolved_path = None

        installed = str(hermes_home / "bin" / "tirith-native")
        with patch("tools.tirith_security._detect_target", return_value=target), \
             patch("tools.tirith_security._install_tirith",
                   return_value=(installed, "")) as mock_install:
            result = _tirith_mod._resolve_tirith_path("tirith")

        assert result == installed
        mock_install.assert_called_once()
        assert _tirith_mod._resolved_path == installed

    @pytest.mark.parametrize("target", TARGETS)
    def test_cached_absolute_binary_is_revalidated(self, target, tmp_path):
        binary = tmp_path / "tirith"
        binary.write_bytes(_native_binary_bytes(_foreign_target(target)))
        binary.chmod(0o755)
        _tirith_mod._resolved_path = str(binary)

        with patch("tools.tirith_security._detect_target", return_value=target), \
             patch("tools.tirith_security._load_security_config", return_value={
                 "tirith_enabled": True,
                 "tirith_path": "/explicit/tirith",
                 "tirith_timeout": 5,
                 "tirith_fail_open": False,
             }), patch("tools.tirith_security.shutil.which", return_value=None):
            assert ensure_installed() is None

        assert _tirith_mod._resolved_path is _tirith_mod._INSTALL_FAILED
        assert _tirith_mod._install_failure_reason == "explicit_path_unusable"

    @pytest.mark.parametrize(("fail_open", "action"), [(True, "allow"), (False, "block")])
    def test_unsupported_explicit_artifact_honors_fail_mode_without_spawn(
        self, tmp_path, fail_open, action
    ):
        target = "x86_64-unknown-linux-gnu"
        foreign = tmp_path / "tirith-foreign"
        foreign.write_bytes(_native_binary_bytes(_foreign_target(target)))
        foreign.chmod(0o755)
        _tirith_mod._resolved_path = None
        cfg = {
            "tirith_enabled": True,
            "tirith_path": str(foreign),
            "tirith_timeout": 5,
            "tirith_fail_open": fail_open,
        }

        with patch("tools.tirith_security._detect_target", return_value=target), \
             patch("tools.tirith_security._load_security_config", return_value=cfg), \
             patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security.subprocess.run") as mock_run:
            result = check_command_security("echo safe")

        assert result["action"] == action
        assert "path unavailable" in result["summary"]
        if not fail_open:
            assert "fail-closed" in result["summary"]
        mock_run.assert_not_called()

    @pytest.mark.parametrize("target", TARGETS)
    def test_explicit_relative_path_is_absolute_and_revalidated(
        self, target, tmp_path, monkeypatch
    ):
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        original = first_dir / "tirith-custom"
        original.write_bytes(_native_binary_bytes(target))
        original.chmod(0o755)

        monkeypatch.chdir(first_dir)
        _tirith_mod._resolved_path = None
        with patch("tools.tirith_security._detect_target", return_value=target):
            resolved = _tirith_mod._resolve_tirith_path("./tirith-custom")
            assert resolved is not None
            assert resolved == str(original)
            assert os.path.isabs(resolved)

            # Cwd rebinding cannot redirect the cache to a same-named foreign file.
            rebound = second_dir / "tirith-custom"
            rebound.write_bytes(_native_binary_bytes(_foreign_target(target)))
            rebound.chmod(0o755)
            monkeypatch.chdir(second_dir)
            assert _tirith_mod._resolve_tirith_path("./tirith-custom") == str(original)

            # Replacing the original cached artifact is detected on the next call.
            original.write_bytes(_native_binary_bytes(_foreign_target(target)))
            original.chmod(0o755)
            assert _tirith_mod._resolve_tirith_path("./tirith-custom") is None

    @pytest.mark.parametrize("target", TARGETS)
    def test_relative_path_lookup_is_cached_as_absolute(self, target, tmp_path, monkeypatch):
        work = tmp_path / "work"
        work.mkdir()
        binary = work / "tirith"
        binary.write_bytes(_native_binary_bytes(target))
        binary.chmod(0o755)
        monkeypatch.chdir(work)
        _tirith_mod._resolved_path = None

        with patch("tools.tirith_security._detect_target", return_value=target), \
             patch("tools.tirith_security.shutil.which", return_value="tirith"):
            assert _tirith_mod._resolve_tirith_path("tirith") == str(binary)
        assert _tirith_mod._resolved_path == str(binary)

    def test_preexisting_relative_cache_is_canonicalized(self, tmp_path, monkeypatch):
        target = "x86_64-unknown-linux-gnu"
        binary = tmp_path / "tirith-custom"
        binary.write_bytes(_native_binary_bytes(target))
        binary.chmod(0o755)
        monkeypatch.chdir(tmp_path)
        _tirith_mod._resolved_path = "tirith-custom"

        with patch("tools.tirith_security._detect_target", return_value=target):
            result = _tirith_mod._resolve_tirith_path("tirith-custom")

        assert result == str(binary)
        assert _tirith_mod._resolved_path == str(binary)


# ---------------------------------------------------------------------------
# Failed download caches the miss (Finding #1)
# ---------------------------------------------------------------------------

class TestFailedDownloadCaching:
    @patch("tools.tirith_security._mark_install_failed")
    @patch("tools.tirith_security._is_install_failed_on_disk", return_value=False)
    @patch("tools.tirith_security._install_tirith", return_value=(None, "download_failed"))
    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_failed_install_cached_no_retry(self, mock_which, mock_install,
                                             mock_disk_check, mock_mark):
        """After a failed download, subsequent resolves must not retry."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        _tirith_mod._resolved_path = None

        # First call: tries install, fails
        _resolve_tirith_path("tirith")
        assert mock_install.call_count == 1
        assert _tirith_mod._resolved_path is _INSTALL_FAILED
        mock_mark.assert_called_once_with("download_failed")  # reason persisted

        # Second call: hits the cache, does NOT call _install_tirith again
        _resolve_tirith_path("tirith")
        assert mock_install.call_count == 1  # still 1, not 2

        _tirith_mod._resolved_path = None

    @patch("tools.tirith_security._mark_install_failed")
    @patch("tools.tirith_security._is_install_failed_on_disk", return_value=False)
    @patch(
        "tools.tirith_security._install_tirith",
        return_value=(None, "binary_target_mismatch"),
    )
    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_sync_target_mismatch_marker_is_persisted_once(
        self, mock_which, mock_install, mock_disk_check, mock_mark
    ):
        del mock_which, mock_disk_check
        _tirith_mod._resolved_path = None

        assert _tirith_mod._resolve_tirith_path("tirith") is None
        assert _tirith_mod._resolve_tirith_path("tirith") is None

        mock_install.assert_called_once()
        mock_mark.assert_called_once_with("binary_target_mismatch")
        assert _tirith_mod._install_failure_reason == "binary_target_mismatch"

    @patch("tools.tirith_security._mark_install_failed")
    @patch("tools.tirith_security._is_install_failed_on_disk", return_value=False)
    @patch("tools.tirith_security._install_tirith", return_value=(None, "download_failed"))
    @patch("tools.tirith_security.shutil.which", return_value=None)
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_failed_install_scan_uses_fail_open(self, mock_cfg, mock_run,
                                                 mock_which, mock_install,
                                                 mock_disk_check, mock_mark):
        """A cached miss applies fail-open without spawning or tripping the breaker."""
        _tirith_mod._resolved_path = None
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        # First command triggers one install attempt and returns path-unavailable.
        result = check_command_security("echo hello")
        assert result["action"] == "allow"
        assert mock_install.call_count == 1

        # Second command uses the cached failure without install/spawn retry.
        result = check_command_security("echo world")
        assert result["action"] == "allow"
        assert mock_install.call_count == 1
        mock_run.assert_not_called()
        assert _tirith_mod._crash_count == 0

        _tirith_mod._resolved_path = None


# ---------------------------------------------------------------------------
# Explicit path must not auto-download (Finding #2)
# ---------------------------------------------------------------------------

class TestExplicitPathNoAutoDownload:
    @patch("tools.tirith_security._install_tirith")
    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_explicit_path_missing_no_download(self, mock_which, mock_install):
        """An explicit tirith_path that doesn't exist must NOT trigger download."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        _tirith_mod._resolved_path = None

        result = _resolve_tirith_path("/opt/custom/tirith")
        # Should cache failure, not call _install_tirith or spawn the bad path.
        mock_install.assert_not_called()
        assert _tirith_mod._resolved_path is _INSTALL_FAILED
        assert result is None

        _tirith_mod._resolved_path = None

    @patch("tools.tirith_security._install_tirith")
    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_tilde_explicit_path_missing_no_download(self, mock_which, mock_install):
        """An explicit ~/path that doesn't exist must NOT trigger download."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        _tirith_mod._resolved_path = None

        result = _resolve_tirith_path("~/bin/tirith")
        mock_install.assert_not_called()
        assert _tirith_mod._resolved_path is _INSTALL_FAILED
        assert result is None

        _tirith_mod._resolved_path = None

    @patch("tools.tirith_security._mark_install_failed")
    @patch("tools.tirith_security._is_install_failed_on_disk", return_value=False)
    @patch("tools.tirith_security._install_tirith", return_value=("/auto/tirith", ""))
    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_default_path_does_auto_download(self, mock_which, mock_install,
                                              mock_disk_check, mock_mark):
        """The default bare 'tirith' SHOULD trigger auto-download."""
        from tools.tirith_security import _resolve_tirith_path
        _tirith_mod._resolved_path = None

        result = _resolve_tirith_path("tirith")
        mock_install.assert_called_once()
        assert result == "/auto/tirith"

        _tirith_mod._resolved_path = None


# ---------------------------------------------------------------------------
# Cosign provenance verification (P1)
# ---------------------------------------------------------------------------

class TestCosignVerification:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security.shutil.which", return_value="/usr/bin/cosign")
    def test_cosign_pass(self, mock_which, mock_run):
        """cosign verify-blob exits 0 → returns True."""
        from tools.tirith_security import _verify_cosign
        mock_run.return_value = _mock_run(0, "Verified OK")
        result = _verify_cosign("/tmp/checksums.txt", "/tmp/checksums.txt.sig",
                                "/tmp/checksums.txt.pem")
        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "verify-blob" in args
        assert "--certificate-identity-regexp" in args

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security.shutil.which", return_value="/usr/bin/cosign")
    def test_cosign_identity_pinned_to_release_workflow(self, mock_which, mock_run):
        """Identity regexp must pin to the release workflow, not the whole repo."""
        from tools.tirith_security import _verify_cosign
        mock_run.return_value = _mock_run(0, "Verified OK")
        _verify_cosign("/tmp/checksums.txt", "/tmp/sig", "/tmp/cert")
        args = mock_run.call_args[0][0]
        # Find the value after --certificate-identity-regexp
        idx = args.index("--certificate-identity-regexp")
        identity = args[idx + 1]
        # The identity contains regex-escaped dots
        assert "workflows/release" in identity
        assert "refs/tags/v" in identity

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security.shutil.which", return_value="/usr/bin/cosign")
    def test_cosign_fail_aborts(self, mock_which, mock_run):
        """cosign verify-blob exits non-zero → returns False (abort install)."""
        from tools.tirith_security import _verify_cosign
        mock_run.return_value = _mock_run(1, "", "signature mismatch")
        result = _verify_cosign("/tmp/checksums.txt", "/tmp/checksums.txt.sig",
                                "/tmp/checksums.txt.pem")
        assert result is False

    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_cosign_not_found_returns_none(self, mock_which):
        """cosign not on PATH → returns None (proceed with SHA-256 only)."""
        from tools.tirith_security import _verify_cosign
        result = _verify_cosign("/tmp/checksums.txt", "/tmp/checksums.txt.sig",
                                "/tmp/checksums.txt.pem")
        assert result is None

    @patch("tools.tirith_security.subprocess.run",
           side_effect=subprocess.TimeoutExpired("cosign", 15))
    @patch("tools.tirith_security.shutil.which", return_value="/usr/bin/cosign")
    def test_cosign_timeout_returns_none(self, mock_which, mock_run):
        """cosign times out → returns None (proceed with SHA-256 only)."""
        from tools.tirith_security import _verify_cosign
        result = _verify_cosign("/tmp/checksums.txt", "/tmp/checksums.txt.sig",
                                "/tmp/checksums.txt.pem")
        assert result is None

    @patch("tools.tirith_security.subprocess.run",
           side_effect=OSError("exec format error"))
    @patch("tools.tirith_security.shutil.which", return_value="/usr/bin/cosign")
    def test_cosign_os_error_returns_none(self, mock_which, mock_run):
        """cosign OSError → returns None (proceed with SHA-256 only)."""
        from tools.tirith_security import _verify_cosign
        result = _verify_cosign("/tmp/checksums.txt", "/tmp/checksums.txt.sig",
                                "/tmp/checksums.txt.pem")
        assert result is None

    @patch("tools.tirith_security._verify_cosign", return_value=False)
    @patch("tools.tirith_security.shutil.which", return_value="/usr/local/bin/cosign")
    @patch("tools.tirith_security._download_file")
    @patch("tools.tirith_security._detect_target", return_value="aarch64-apple-darwin")
    def test_install_aborts_on_cosign_rejection(self, mock_target, mock_dl,
                                                 mock_which, mock_cosign):
        """_install_tirith returns None when cosign rejects the signature."""
        from tools.tirith_security import _install_tirith
        path, reason = _install_tirith()
        assert path is None
        assert reason == "cosign_verification_failed"

    @patch("tools.tirith_security.tarfile.open")
    @patch("tools.tirith_security._verify_checksum", return_value=True)
    @patch("tools.tirith_security.shutil.which", return_value=None)
    @patch("tools.tirith_security._download_file")
    @patch("tools.tirith_security._detect_target", return_value="aarch64-apple-darwin")
    def test_install_proceeds_without_cosign(self, mock_target, mock_dl,
                                              mock_which, mock_checksum,
                                              mock_tarfile):
        """_install_tirith proceeds with SHA-256 only when cosign is not on PATH."""
        from tools.tirith_security import _install_tirith
        mock_tar = MagicMock()
        mock_tar.__enter__ = MagicMock(return_value=mock_tar)
        mock_tar.__exit__ = MagicMock(return_value=False)
        mock_tar.getmembers.return_value = []
        mock_tarfile.return_value = mock_tar

        path, reason = _install_tirith()
        # Reaches extraction (no binary in mock archive), but got past cosign
        assert path is None
        assert reason == "binary_not_in_archive"
        assert mock_checksum.called  # SHA-256 verification ran

    @patch("tools.tirith_security.tarfile.open")
    @patch("tools.tirith_security._verify_checksum", return_value=True)
    @patch("tools.tirith_security._verify_cosign", return_value=None)
    @patch("tools.tirith_security.shutil.which", return_value="/usr/local/bin/cosign")
    @patch("tools.tirith_security._download_file")
    @patch("tools.tirith_security._detect_target", return_value="aarch64-apple-darwin")
    def test_install_proceeds_when_cosign_exec_fails(self, mock_target, mock_dl,
                                                       mock_which, mock_cosign,
                                                       mock_checksum, mock_tarfile):
        """_install_tirith falls back to SHA-256 when cosign exists but fails to execute."""
        from tools.tirith_security import _install_tirith
        mock_tar = MagicMock()
        mock_tar.__enter__ = MagicMock(return_value=mock_tar)
        mock_tar.__exit__ = MagicMock(return_value=False)
        mock_tar.getmembers.return_value = []
        mock_tarfile.return_value = mock_tar

        path, reason = _install_tirith()
        assert path is None
        assert reason == "binary_not_in_archive"  # got past cosign
        assert mock_checksum.called

    @patch("tools.tirith_security.tarfile.open")
    @patch("tools.tirith_security._verify_checksum", return_value=True)
    @patch("tools.tirith_security.shutil.which", return_value="/usr/local/bin/cosign")
    @patch("tools.tirith_security._download_file")
    @patch("tools.tirith_security._detect_target", return_value="aarch64-apple-darwin")
    def test_install_proceeds_when_cosign_artifacts_missing(self, mock_target,
                                                              mock_dl, mock_which,
                                                              mock_checksum, mock_tarfile):
        """_install_tirith proceeds with SHA-256 when .sig/.pem downloads fail."""
        from tools.tirith_security import _install_tirith
        import urllib.error

        def _dl_side_effect(url, dest, timeout=10):
            if url.endswith(".sig") or url.endswith(".pem"):
                raise urllib.error.URLError("404 Not Found")

        mock_dl.side_effect = _dl_side_effect
        mock_tar = MagicMock()
        mock_tar.__enter__ = MagicMock(return_value=mock_tar)
        mock_tar.__exit__ = MagicMock(return_value=False)
        mock_tar.getmembers.return_value = []
        mock_tarfile.return_value = mock_tar

        path, reason = _install_tirith()
        assert path is None
        assert reason == "binary_not_in_archive"  # got past cosign
        assert mock_checksum.called

    @patch("tools.tirith_security.tarfile.open")
    @patch("tools.tirith_security._verify_checksum", return_value=True)
    @patch("tools.tirith_security._verify_cosign", return_value=True)
    @patch("tools.tirith_security.shutil.which", return_value="/usr/local/bin/cosign")
    @patch("tools.tirith_security._download_file")
    @patch("tools.tirith_security._detect_target", return_value="aarch64-apple-darwin")
    def test_install_proceeds_when_cosign_passes(self, mock_target, mock_dl,
                                                   mock_which, mock_cosign,
                                                   mock_checksum, mock_tarfile):
        """_install_tirith proceeds only when cosign explicitly passes (True)."""
        from tools.tirith_security import _install_tirith
        # Mock tarfile — empty archive means "binary not found" return
        mock_tar = MagicMock()
        mock_tar.__enter__ = MagicMock(return_value=mock_tar)
        mock_tar.__exit__ = MagicMock(return_value=False)
        mock_tar.getmembers.return_value = []
        mock_tarfile.return_value = mock_tar

        path, reason = _install_tirith()
        assert path is None  # no binary in mock archive, but got past cosign
        assert reason == "binary_not_in_archive"
        assert mock_checksum.called  # reached SHA-256 step
        assert mock_cosign.called  # cosign was invoked


class TestInstallArchiveMemberValidation:
    def _write_archive(
        self, tmp_path, target: str, member: tarfile.TarInfo, data: bytes | None = None
    ):
        archive_name = f"tirith-{target}.tar.gz"
        archive = tmp_path / archive_name
        checksums = tmp_path / "checksums.txt"
        with tarfile.open(archive, "w:gz") as tar:
            if data is None:
                tar.addfile(member)
            else:
                tar.addfile(member, io.BytesIO(data))
        checksums.write_text(f"ignored  {archive_name}\n", encoding="utf-8")
        return archive, checksums

    def _download_side_effect(self, archive, checksums):
        def _download(url, dest, timeout=10):
            del timeout
            if url.endswith(".tar.gz"):
                with open(archive, "rb") as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                return
            if url.endswith("checksums.txt"):
                with open(checksums, "rb") as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                return
            raise AssertionError(f"unexpected download URL: {url}")

        return _download

    @pytest.mark.parametrize("target", TestBinaryTargetValidation.TARGETS)
    @patch("tools.tirith_security._verify_checksum", return_value=True)
    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_install_extracts_regular_tirith_member(
        self, mock_which, mock_checksum, target, tmp_path, monkeypatch
    ):
        """A valid regular-file Tirith member is installed for every target."""
        del mock_which, mock_checksum
        from tools.tirith_security import _install_tirith

        payload = _native_binary_bytes(target)
        member = tarfile.TarInfo("bin/tirith")
        member.mode = 0o755
        member.size = len(payload)
        archive, checksums = self._write_archive(tmp_path, target, member, payload)

        hermes_home = tmp_path / "hermes-home"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        with patch("tools.tirith_security._detect_target", return_value=target), \
             patch("tools.tirith_security._download_file",
                   side_effect=self._download_side_effect(archive, checksums)):
            path, reason = _install_tirith(log_failures=False)

        assert reason == ""
        expected_path = str(hermes_home / "bin" / "tirith")
        assert path is not None
        assert path == expected_path
        assert os.path.isfile(path)
        assert not os.path.islink(path)
        assert stat.S_IMODE(os.stat(expected_path).st_mode) == 0o755
        with open(path, "rb") as f:
            assert f.read() == payload

    @pytest.mark.parametrize("target", TestBinaryTargetValidation.TARGETS)
    @patch("tools.tirith_security._verify_checksum", return_value=True)
    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_install_rejects_wrong_target_before_publication(
        self, mock_which, mock_checksum, target, tmp_path, monkeypatch
    ):
        """A signed/checksummed archive still cannot publish a foreign binary."""
        del mock_which, mock_checksum
        from tools.tirith_security import _install_tirith

        payload = _native_binary_bytes(_foreign_target(target))
        member = tarfile.TarInfo("bin/tirith")
        member.mode = 0o755
        member.size = len(payload)
        archive, checksums = self._write_archive(tmp_path, target, member, payload)

        hermes_home = tmp_path / "hermes-home"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        with patch("tools.tirith_security._detect_target", return_value=target), \
             patch("tools.tirith_security._download_file",
                   side_effect=self._download_side_effect(archive, checksums)):
            path, reason = _install_tirith(log_failures=False)

        assert path is None
        assert reason == "binary_target_mismatch"
        assert not os.path.lexists(hermes_home / "bin" / "tirith")

    @patch("tools.tirith_security._verify_checksum", return_value=True)
    @patch("tools.tirith_security.shutil.which", return_value=None)
    @patch("tools.tirith_security._detect_target", return_value="aarch64-apple-darwin")
    def test_install_rejects_non_regular_tirith_member(
        self, mock_target, mock_which, mock_checksum, tmp_path, monkeypatch
    ):
        """Symlink or hardlink tar members must not be installed as Tirith."""
        del mock_target, mock_which, mock_checksum
        from tools.tirith_security import _install_tirith

        member = tarfile.TarInfo("bin/tirith")
        member.type = tarfile.SYMTYPE
        member.linkname = "/bin/sh"
        archive, checksums = self._write_archive(
            tmp_path, "aarch64-apple-darwin", member
        )

        hermes_home = tmp_path / "hermes-home"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        with patch("tools.tirith_security._download_file",
                   side_effect=self._download_side_effect(archive, checksums)):
            path, reason = _install_tirith(log_failures=False)

        assert path is None
        assert reason == "binary_not_regular_file"
        assert not os.path.lexists(hermes_home / "bin" / "tirith")


class TestAtomicPublication:
    TARGET = "x86_64-unknown-linux-gnu"

    def _source(self, tmp_path, suffix: bytes = b""):
        source = tmp_path / "downloaded-tirith"
        source.write_bytes(_native_binary_bytes(self.TARGET) + suffix)
        return source

    def test_existing_destination_symlink_is_replaced_not_followed(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / "hermes-home"
        bin_dir = hermes_home / "bin"
        bin_dir.mkdir(parents=True, mode=0o700)
        victim = tmp_path / "victim"
        victim.write_bytes(b"do-not-overwrite")
        destination = bin_dir / "tirith"
        destination.symlink_to(victim)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        payload = self._source(tmp_path).read_bytes()

        path, reason = _tirith_mod._publish_tirith_binary(
            str(tmp_path / "downloaded-tirith"), self.TARGET
        )

        assert reason == ""
        assert path == str(destination)
        assert victim.read_bytes() == b"do-not-overwrite"
        assert not destination.is_symlink()
        assert destination.read_bytes() == payload

    def test_exdev_never_falls_back_through_destination_symlink(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / "hermes-home"
        bin_dir = hermes_home / "bin"
        bin_dir.mkdir(parents=True, mode=0o700)
        victim = tmp_path / "victim"
        victim.write_bytes(b"do-not-overwrite")
        destination = bin_dir / "tirith"
        destination.symlink_to(victim)
        source = self._source(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        with patch(
            "tools.tirith_security.os.replace",
            side_effect=OSError(errno.EXDEV, "cross-device link"),
        ):
            path, reason = _tirith_mod._publish_tirith_binary(
                str(source), self.TARGET
            )

        assert path is None
        assert reason == "atomic_publish_failed"
        assert destination.is_symlink()
        assert victim.read_bytes() == b"do-not-overwrite"
        assert list(bin_dir.glob(".tirith-stage-*")) == []

    def test_directory_destination_is_rejected_without_nesting(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / "hermes-home"
        destination = hermes_home / "bin" / "tirith"
        destination.mkdir(parents=True)
        destination.parent.chmod(0o700)
        source = self._source(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        path, reason = _tirith_mod._publish_tirith_binary(str(source), self.TARGET)

        assert path is None
        assert reason == "atomic_publish_failed"
        assert destination.is_dir()
        assert list(destination.parent.glob(".tirith-stage-*")) == []

    def test_group_writable_install_directory_fails_closed(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / "hermes-home"
        bin_dir = hermes_home / "bin"
        bin_dir.mkdir(parents=True)
        bin_dir.chmod(0o770)
        source = self._source(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        path, reason = _tirith_mod._publish_tirith_binary(str(source), self.TARGET)

        assert path is None
        assert reason == "install_directory_insecure"
        assert not (bin_dir / "tirith").exists()

    def test_new_install_directory_is_private_under_permissive_umask(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / "new-hermes-home"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        previous_umask = os.umask(0)
        try:
            bin_dir = _tirith_mod._hermes_bin_dir()
        finally:
            os.umask(previous_umask)

        assert stat.S_IMODE(os.stat(bin_dir).st_mode) == 0o700

    def test_reader_observes_only_complete_old_or_new_binary(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / "hermes-home"
        bin_dir = hermes_home / "bin"
        bin_dir.mkdir(parents=True, mode=0o700)
        destination = bin_dir / "tirith"
        old_payload = _native_binary_bytes(self.TARGET) + b"old"
        new_payload = _native_binary_bytes(self.TARGET) + b"new" * 100_000
        destination.write_bytes(old_payload)
        destination.chmod(0o755)
        source = tmp_path / "downloaded-tirith"
        source.write_bytes(new_payload)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        before_replace = threading.Event()
        allow_replace = threading.Event()
        real_replace = os.replace
        result: list[tuple[str | None, str]] = []

        def gated_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            before_replace.set()
            assert allow_replace.wait(timeout=5)
            return real_replace(
                src,
                dst,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        with patch("tools.tirith_security.os.replace", side_effect=gated_replace):
            publisher = threading.Thread(
                target=lambda: result.append(
                    _tirith_mod._publish_tirith_binary(str(source), self.TARGET)
                )
            )
            publisher.start()
            assert before_replace.wait(timeout=5)
            assert destination.read_bytes() == old_payload
            allow_replace.set()
            publisher.join(timeout=5)

        assert not publisher.is_alive()
        assert result == [(str(destination), "")]
        assert destination.read_bytes() == new_payload


# ---------------------------------------------------------------------------
# Background install / non-blocking startup (P2)
# ---------------------------------------------------------------------------

class TestBackgroundInstall:
    def test_ensure_installed_non_blocking(self):
        """ensure_installed must return immediately when download needed."""
        _tirith_mod._resolved_path = None

        with patch("tools.tirith_security._load_security_config",
                   return_value={"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}), \
             patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._is_install_failed_on_disk", return_value=False), \
             patch("tools.tirith_security.threading.Thread") as MockThread:
            mock_thread = MagicMock()
            mock_thread.is_alive.return_value = False
            MockThread.return_value = mock_thread

            result = ensure_installed()
            assert result is None  # not available yet
            MockThread.assert_called_once()
            mock_thread.start.assert_called_once()

        _tirith_mod._resolved_path = None

    def test_ensure_installed_skips_on_disk_marker(self):
        """ensure_installed skips network attempt when disk marker exists."""
        _tirith_mod._resolved_path = None

        with patch("tools.tirith_security._load_security_config",
                   return_value={"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}), \
             patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._read_failure_reason", return_value="download_failed"), \
             patch("tools.tirith_security._is_install_failed_on_disk", return_value=True):

            result = ensure_installed()
            assert result is None
            assert _tirith_mod._resolved_path is _tirith_mod._INSTALL_FAILED
            assert _tirith_mod._install_failure_reason == "download_failed"

        _tirith_mod._resolved_path = None

    def test_resolve_returns_none_when_thread_alive(self):
        """A live installer never respawns a rejected/default PATH artifact."""
        from tools.tirith_security import _resolve_tirith_path
        _tirith_mod._resolved_path = None
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        _tirith_mod._install_thread = mock_thread

        with patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"):
            result = _resolve_tirith_path("tirith")
            assert result is None

        _tirith_mod._install_thread = None
        _tirith_mod._resolved_path = None

    def test_resolve_picks_up_background_result(self):
        """After background thread finishes, _resolve_tirith_path uses cached path."""
        from tools.tirith_security import _resolve_tirith_path
        # Simulate background thread having completed and set the path
        _tirith_mod._resolved_path = "/usr/local/bin/tirith"

        with patch("tools.tirith_security._is_usable_tirith_binary", return_value=True):
            result = _resolve_tirith_path("tirith")
        assert result == "/usr/local/bin/tirith"

        _tirith_mod._resolved_path = None

    def test_incompatible_path_during_install_does_not_open_breaker_and_recovers(
        self, tmp_path
    ):
        target = "x86_64-unknown-linux-gnu"
        foreign = tmp_path / "path-tirith"
        foreign.write_bytes(_native_binary_bytes("aarch64-apple-darwin"))
        foreign.chmod(0o755)
        installed = tmp_path / "managed" / "tirith"
        installed.parent.mkdir()
        installed.write_bytes(_native_binary_bytes(target))
        installed.chmod(0o755)
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()

        live_thread = MagicMock()
        live_thread.is_alive.return_value = True
        _tirith_mod._install_thread = live_thread
        _tirith_mod._resolved_path = None
        cfg = {
            "tirith_enabled": True,
            "tirith_path": "tirith",
            "tirith_timeout": 5,
            "tirith_fail_open": True,
        }

        with patch("tools.tirith_security._detect_target", return_value=target), \
             patch("tools.tirith_security.shutil.which", return_value=str(foreign)), \
             patch("tools.tirith_security._hermes_bin_dir", return_value=str(empty_bin)), \
             patch("tools.tirith_security._load_security_config", return_value=cfg), \
             patch("tools.tirith_security.subprocess.run") as mock_run:
            for _ in range(_tirith_mod._CRASH_LIMIT + 1):
                result = check_command_security("echo waiting")
                assert result["action"] == "allow"
                assert "path unavailable" in result["summary"]

        mock_run.assert_not_called()
        assert _tirith_mod._crash_count == 0
        assert _tirith_mod._circuit_open is False

        # A successful background install closes even a previously open breaker.
        _tirith_mod._install_thread = None
        _tirith_mod._circuit_open = True
        _tirith_mod._crash_count = _tirith_mod._CRASH_LIMIT
        with patch("tools.tirith_security._detect_target", return_value=target), \
             patch("tools.tirith_security.shutil.which", return_value=str(foreign)), \
             patch("tools.tirith_security._hermes_bin_dir", return_value=str(empty_bin)), \
             patch("tools.tirith_security._install_tirith",
                   return_value=(str(installed), "")), \
             patch("tools.tirith_security._clear_install_failed"):
            _tirith_mod._background_install()

        assert _tirith_mod._resolved_path == str(installed)
        assert _tirith_mod._crash_count == 0
        assert _tirith_mod._circuit_open is False

        with patch("tools.tirith_security._detect_target", return_value=target), \
             patch("tools.tirith_security._load_security_config", return_value=cfg), \
             patch("tools.tirith_security.subprocess.run",
                   return_value=_mock_run(0, _json_stdout())) as mock_run:
            result = check_command_security("echo recovered")

        assert result["action"] == "allow"
        assert mock_run.call_args.args[0][0] == str(installed)

    def test_background_target_mismatch_marker_is_persisted_once(self):
        _tirith_mod._resolved_path = None
        with patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._install_tirith",
                   return_value=(None, "binary_target_mismatch")), \
             patch("tools.tirith_security._mark_install_failed") as mock_mark:
            _tirith_mod._background_install()
            _tirith_mod._background_install()

        mock_mark.assert_called_once_with("binary_target_mismatch")
        assert _tirith_mod._resolved_path is _tirith_mod._INSTALL_FAILED
        assert _tirith_mod._install_failure_reason == "binary_target_mismatch"


# ---------------------------------------------------------------------------
# Disk failure marker persistence (P2)
# ---------------------------------------------------------------------------

class TestDiskFailureMarker:
    def test_mark_and_check(self):
        """Writing then reading the marker should work."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        marker = os.path.join(tmpdir, ".tirith-install-failed")
        with patch("tools.tirith_security._failure_marker_path", return_value=marker):
            from tools.tirith_security import (
                _mark_install_failed, _is_install_failed_on_disk, _clear_install_failed,
            )
            assert not _is_install_failed_on_disk()
            _mark_install_failed("download_failed")
            assert _is_install_failed_on_disk()
            _clear_install_failed()
            assert not _is_install_failed_on_disk()

    def test_expired_marker_ignored(self):
        """Marker older than TTL should be ignored."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        marker = os.path.join(tmpdir, ".tirith-install-failed")
        with patch("tools.tirith_security._failure_marker_path", return_value=marker):
            from tools.tirith_security import _mark_install_failed, _is_install_failed_on_disk
            _mark_install_failed("download_failed")
            # Backdate the file past 24h TTL
            old_time = time.time() - 90000  # 25 hours ago
            os.utime(marker, (old_time, old_time))
            assert not _is_install_failed_on_disk()

    def test_cosign_missing_marker_clears_when_cosign_appears(self):
        """Marker with 'cosign_missing' reason clears if cosign is now on PATH."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        marker = os.path.join(tmpdir, ".tirith-install-failed")
        with patch("tools.tirith_security._failure_marker_path", return_value=marker):
            from tools.tirith_security import _mark_install_failed, _is_install_failed_on_disk
            _mark_install_failed("cosign_missing")
            with patch("tools.tirith_security.shutil.which", return_value=None):
                assert _is_install_failed_on_disk()  # cosign still absent

            # Now cosign appears on PATH
            with patch("tools.tirith_security.shutil.which", return_value="/usr/local/bin/cosign"):
                assert not _is_install_failed_on_disk()
            # Marker file should have been removed
            assert not os.path.exists(marker)

    def test_cosign_missing_marker_stays_when_cosign_still_absent(self):
        """Marker with 'cosign_missing' reason stays if cosign is still missing."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        marker = os.path.join(tmpdir, ".tirith-install-failed")
        with patch("tools.tirith_security._failure_marker_path", return_value=marker):
            from tools.tirith_security import _mark_install_failed, _is_install_failed_on_disk
            _mark_install_failed("cosign_missing")
            with patch("tools.tirith_security.shutil.which", return_value=None):
                assert _is_install_failed_on_disk()

    def test_non_cosign_marker_not_affected_by_cosign_presence(self):
        """Markers with other reasons are NOT cleared by cosign appearing."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        marker = os.path.join(tmpdir, ".tirith-install-failed")
        with patch("tools.tirith_security._failure_marker_path", return_value=marker):
            from tools.tirith_security import _mark_install_failed, _is_install_failed_on_disk
            _mark_install_failed("download_failed")
            with patch("tools.tirith_security.shutil.which", return_value="/usr/local/bin/cosign"):
                assert _is_install_failed_on_disk()  # still failed

    @patch("tools.tirith_security._mark_install_failed")
    @patch("tools.tirith_security._is_install_failed_on_disk", return_value=False)
    @patch("tools.tirith_security._install_tirith", return_value=(None, "cosign_missing"))
    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_sync_resolve_persists_failure(self, mock_which, mock_install,
                                            mock_disk_check, mock_mark):
        """Synchronous _resolve_tirith_path persists failure to disk."""
        from tools.tirith_security import _resolve_tirith_path
        _tirith_mod._resolved_path = None

        _resolve_tirith_path("tirith")
        mock_mark.assert_called_once_with("cosign_missing")

        _tirith_mod._resolved_path = None

    @patch("tools.tirith_security._clear_install_failed")
    @patch("tools.tirith_security._is_install_failed_on_disk", return_value=False)
    @patch("tools.tirith_security._install_tirith", return_value=("/installed/tirith", ""))
    @patch("tools.tirith_security.shutil.which", return_value=None)
    def test_sync_resolve_clears_marker_on_success(self, mock_which, mock_install,
                                                    mock_disk_check, mock_clear):
        """Successful install clears the disk failure marker."""
        from tools.tirith_security import _resolve_tirith_path
        _tirith_mod._resolved_path = None

        result = _resolve_tirith_path("tirith")
        assert result == "/installed/tirith"
        mock_clear.assert_called_once()

        _tirith_mod._resolved_path = None

    def test_sync_resolve_skips_install_on_disk_marker(self):
        """_resolve_tirith_path skips download when disk marker is recent."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        _tirith_mod._resolved_path = None

        with patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._read_failure_reason", return_value="download_failed"), \
             patch("tools.tirith_security._is_install_failed_on_disk", return_value=True), \
             patch("tools.tirith_security._install_tirith") as mock_install:
            _resolve_tirith_path("tirith")
            mock_install.assert_not_called()
            assert _tirith_mod._resolved_path is _INSTALL_FAILED
            assert _tirith_mod._install_failure_reason == "download_failed"

        _tirith_mod._resolved_path = None

    def test_install_failed_still_checks_local_paths(self):
        """After _INSTALL_FAILED, a manual install on PATH is picked up."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        _tirith_mod._resolved_path = _INSTALL_FAILED

        with patch("tools.tirith_security.shutil.which", return_value="/usr/local/bin/tirith"), \
             patch("tools.tirith_security._is_usable_tirith_binary", return_value=True), \
             patch("tools.tirith_security._clear_install_failed") as mock_clear:
            result = _resolve_tirith_path("tirith")
            assert result == "/usr/local/bin/tirith"
            assert _tirith_mod._resolved_path == "/usr/local/bin/tirith"
            mock_clear.assert_called_once()

        _tirith_mod._resolved_path = None

    @pytest.mark.parametrize("target", TestBinaryTargetValidation.TARGETS)
    def test_install_failed_recovers_from_hermes_bin(self, target, tmp_path):
        """After _INSTALL_FAILED, every native managed target is picked up."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        hermes_bin = str(tmp_path / "tirith")
        with open(hermes_bin, "wb") as f:
            f.write(_native_binary_bytes(target))
        os.chmod(hermes_bin, 0o755)

        _tirith_mod._resolved_path = _INSTALL_FAILED

        with patch("tools.tirith_security._detect_target", return_value=target), \
             patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value=str(tmp_path)), \
             patch("tools.tirith_security._clear_install_failed") as mock_clear:
            result = _resolve_tirith_path("tirith")
            assert result == hermes_bin
            assert _tirith_mod._resolved_path == hermes_bin
            mock_clear.assert_called_once()

        _tirith_mod._resolved_path = None

    def test_install_failed_skips_network_when_local_absent(self):
        """After _INSTALL_FAILED, if local checks fail, network is NOT retried."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        _tirith_mod._resolved_path = _INSTALL_FAILED

        with patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._install_tirith") as mock_install:
            result = _resolve_tirith_path("tirith")
            assert result is None
            mock_install.assert_not_called()

        _tirith_mod._resolved_path = None

    def test_cosign_missing_disk_marker_allows_retry(self):
        """Disk marker with cosign_missing reason allows retry when cosign appears."""
        from tools.tirith_security import _resolve_tirith_path
        _tirith_mod._resolved_path = None

        # _is_install_failed_on_disk sees "cosign_missing" + cosign on PATH → returns False
        with patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._is_install_failed_on_disk", return_value=False), \
             patch("tools.tirith_security._install_tirith", return_value=("/new/tirith", "")) as mock_install, \
             patch("tools.tirith_security._clear_install_failed"):
            result = _resolve_tirith_path("tirith")
            mock_install.assert_called_once()  # network retry happened
            assert result == "/new/tirith"

        _tirith_mod._resolved_path = None

    def test_in_memory_cosign_missing_retries_when_cosign_appears(self):
        """In-memory _INSTALL_FAILED with cosign_missing retries when cosign appears."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        _tirith_mod._resolved_path = _INSTALL_FAILED
        _tirith_mod._install_failure_reason = "cosign_missing"

        def _which_side_effect(name):
            if name == "tirith":
                return None  # tirith not on PATH
            if name == "cosign":
                return "/usr/local/bin/cosign"  # cosign now available
            return None

        with patch("tools.tirith_security.shutil.which", side_effect=_which_side_effect), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._is_install_failed_on_disk", return_value=False), \
             patch("tools.tirith_security._install_tirith", return_value=("/new/tirith", "")) as mock_install, \
             patch("tools.tirith_security._clear_install_failed"):
            result = _resolve_tirith_path("tirith")
            mock_install.assert_called_once()  # network retry happened
            assert result == "/new/tirith"

        _tirith_mod._resolved_path = None

    def test_in_memory_cosign_exec_failed_not_retried(self):
        """In-memory _INSTALL_FAILED with cosign_exec_failed is NOT retried."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        _tirith_mod._resolved_path = _INSTALL_FAILED
        _tirith_mod._install_failure_reason = "cosign_exec_failed"

        with patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._install_tirith") as mock_install:
            result = _resolve_tirith_path("tirith")
            assert result is None
            mock_install.assert_not_called()

        _tirith_mod._resolved_path = None

    def test_in_memory_cosign_missing_stays_when_cosign_still_absent(self):
        """In-memory cosign_missing is NOT retried when cosign is still absent."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        _tirith_mod._resolved_path = _INSTALL_FAILED
        _tirith_mod._install_failure_reason = "cosign_missing"

        with patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._install_tirith") as mock_install:
            result = _resolve_tirith_path("tirith")
            assert result is None
            mock_install.assert_not_called()

        _tirith_mod._resolved_path = None

    def test_disk_marker_reason_preserved_in_memory(self):
        """Disk marker reason is loaded into _install_failure_reason, not a generic tag."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED
        _tirith_mod._resolved_path = None

        # First call: disk marker with cosign_missing is active, cosign still absent
        with patch("tools.tirith_security.shutil.which", return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._read_failure_reason", return_value="cosign_missing"), \
             patch("tools.tirith_security._is_install_failed_on_disk", return_value=True):
            _resolve_tirith_path("tirith")
            assert _tirith_mod._resolved_path is _INSTALL_FAILED
            assert _tirith_mod._install_failure_reason == "cosign_missing"

        # Second call: cosign now on PATH → in-memory retry fires
        def _which_side_effect(name):
            if name == "tirith":
                return None
            if name == "cosign":
                return "/usr/local/bin/cosign"
            return None

        with patch("tools.tirith_security.shutil.which", side_effect=_which_side_effect), \
             patch("tools.tirith_security._hermes_bin_dir", return_value="/nonexistent"), \
             patch("tools.tirith_security._is_install_failed_on_disk", return_value=False), \
             patch("tools.tirith_security._install_tirith", return_value=("/new/tirith", "")) as mock_install, \
             patch("tools.tirith_security._clear_install_failed"):
            result = _resolve_tirith_path("tirith")
            mock_install.assert_called_once()
            assert result == "/new/tirith"

        _tirith_mod._resolved_path = None


# ---------------------------------------------------------------------------
# HERMES_HOME isolation
# ---------------------------------------------------------------------------

class TestHermesHomeIsolation:
    def test_hermes_bin_dir_respects_hermes_home(self):
        """_hermes_bin_dir must use HERMES_HOME, not hardcoded ~/.hermes."""
        from tools.tirith_security import _hermes_bin_dir
        import tempfile
        tmpdir = tempfile.mkdtemp()
        with patch.dict(os.environ, {"HERMES_HOME": tmpdir}):
            result = _hermes_bin_dir()
        assert result == os.path.join(tmpdir, "bin")
        assert os.path.isdir(result)

    def test_failure_marker_respects_hermes_home(self):
        """_failure_marker_path must use HERMES_HOME, not hardcoded ~/.hermes."""
        from tools.tirith_security import _failure_marker_path
        with patch.dict(os.environ, {"HERMES_HOME": "/custom/hermes"}):
            result = _failure_marker_path()
        assert result == "/custom/hermes/.tirith-install-failed"

    def test_conftest_isolation_prevents_real_home_writes(self):
        """The conftest autouse fixture sets HERMES_HOME; verify it's active."""
        hermes_home = os.getenv("HERMES_HOME")
        assert hermes_home is not None, "HERMES_HOME should be set by conftest"
        assert "hermes_test" in hermes_home, "Should point to test temp dir"

    def test_get_hermes_home_fallback(self):
        """Without HERMES_HOME set, falls back to the active OS home."""
        from tools.tirith_security import _get_hermes_home
        with patch.dict(os.environ, {}, clear=True):
            # Remove HERMES_HOME entirely. With HOME also absent, expanduser
            # falls back to the account database; compute expected under the
            # same environment instead of after patch.dict restores HOME.
            os.environ.pop("HERMES_HOME", None)
            expected = os.path.join(os.path.expanduser("~"), ".hermes")
            result = _get_hermes_home()
        assert result == expected


# ---------------------------------------------------------------------------
# Warn-once dedupe (issue: tirith spawn failed spamming on Windows)
# ---------------------------------------------------------------------------

class TestSpawnWarningDedup:
    """When tirith isn't installed yet (background install in flight, or
    install marked failed), every terminal command spammed an identical
    ``tirith spawn failed: [WinError 2]`` warning to ``errors.log``. The
    dedupe set in ``_warn_once`` collapses repeats by ``(exc class, errno)``
    while still surfacing the first occurrence so users see the failure.
    """

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_repeated_spawn_failure_logs_once(self, mock_cfg, mock_run, caplog):
        mock_cfg.return_value = {
            "tirith_enabled": True, "tirith_path": "tirith",
            "tirith_timeout": 5, "tirith_fail_open": True,
        }
        mock_run.side_effect = FileNotFoundError("[WinError 2]")
        # Fresh dedupe state — clear any keys left by other tests.
        _tirith_mod._reset_spawn_warning_state()

        with caplog.at_level("WARNING", logger="tools.tirith_security"):
            for i in range(15):
                result = check_command_security("echo hi")
                # Behavior must remain the same on every call —
                # fail-open allow, with the exception captured in summary.
                assert result["action"] == "allow"
                if i < _tirith_mod._CRASH_LIMIT:
                    # Before circuit breaker opens, summary has the exception
                    assert "unavailable" in result["summary"]
                else:
                    # After circuit breaker opens, summary is generic
                    assert "circuit breaker" in result["summary"]

        spawn_warnings = [
            rec for rec in caplog.records
            if "tirith spawn failed" in rec.message
        ]
        assert len(spawn_warnings) == 1, (
            f"expected exactly 1 spawn-failed warning across 15 commands, "
            f"got {len(spawn_warnings)}: {[r.message for r in spawn_warnings]}"
        )

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_distinct_exception_types_each_log_once(self, mock_cfg, mock_run, caplog):
        """``FileNotFoundError`` and ``PermissionError`` are distinct
        failure modes and each deserves its own first-occurrence log
        line; the dedupe key includes the exception class.
        
        After _CRASH_LIMIT consecutive failures the circuit breaker opens
        and subsequent calls short-circuit without spawning, so we only
        see the warnings from the first batch."""
        mock_cfg.return_value = {
            "tirith_enabled": True, "tirith_path": "tirith",
            "tirith_timeout": 5, "tirith_fail_open": True,
        }
        _tirith_mod._reset_spawn_warning_state()

        with caplog.at_level("WARNING", logger="tools.tirith_security"):
            mock_run.side_effect = FileNotFoundError("[WinError 2]")
            for _ in range(3):
                check_command_security("a")
            # Circuit breaker is now open — switching to PermissionError
            # won't generate a new warning because the function returns
            # before reaching subprocess.run.
            mock_run.side_effect = PermissionError("denied")
            for _ in range(3):
                check_command_security("b")

        spawn_warnings = [
            rec for rec in caplog.records
            if "tirith spawn failed" in rec.message
        ]
        assert len(spawn_warnings) == 1, (
            f"expected 1 warning before circuit breaker opens, "
            f"got {len(spawn_warnings)}"
        )

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_repeated_timeout_logs_once(self, mock_cfg, mock_run, caplog):
        mock_cfg.return_value = {
            "tirith_enabled": True, "tirith_path": "tirith",
            "tirith_timeout": 5, "tirith_fail_open": True,
        }
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tirith", timeout=5)
        _tirith_mod._reset_spawn_warning_state()

        with caplog.at_level("WARNING", logger="tools.tirith_security"):
            for _ in range(10):
                result = check_command_security("slow")
                assert result["action"] == "allow"

        timeout_warnings = [
            rec for rec in caplog.records
            if "tirith timed out" in rec.message
        ]
        assert len(timeout_warnings) == 1

    @patch("tools.tirith_security._load_security_config")
    def test_path_none_logs_once(self, mock_cfg, caplog):
        """``_resolve_tirith_path`` returning ``None`` (explicit path set
        but resolver returned None — unusual) should not spam the log
        either."""
        mock_cfg.return_value = {
            "tirith_enabled": True, "tirith_path": "tirith",
            "tirith_timeout": 5, "tirith_fail_open": True,
        }
        _tirith_mod._reset_spawn_warning_state()

        with patch(
            "tools.tirith_security._resolve_tirith_path", return_value=None
        ):
            with caplog.at_level("WARNING", logger="tools.tirith_security"):
                for _ in range(10):
                    result = check_command_security("echo")
                    assert result["action"] == "allow"
                    assert "tirith path unavailable" in result["summary"]

        none_warnings = [
            rec for rec in caplog.records
            if "tirith path resolved to None" in rec.message
        ]
        assert len(none_warnings) == 1


# ---------------------------------------------------------------------------
# .app TLD suppression (issue #24461)
# ---------------------------------------------------------------------------

_CFG = {"tirith_enabled": True, "tirith_path": "tirith",
        "tirith_timeout": 5, "tirith_fail_open": True}


class TestAppTldSuppression:
    """warn verdicts whose only finding is lookalike_tld/.app are downgraded to allow."""

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_app_only_warn_downgraded_to_allow(self, mock_cfg, mock_run):
        mock_cfg.return_value = _CFG
        findings = [{"rule_id": "lookalike_tld", "value": ".app",
                     "message": "Domain uses '.app' TLD which can be confused with file extensions"}]
        mock_run.return_value = _mock_run(2, _json_stdout(findings, ".app TLD warning"))
        result = check_command_security("curl https://example.app")
        assert result["action"] == "allow"
        assert result["findings"] == []
        assert result["summary"] == ""

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_app_tld_in_description_field_also_suppressed(self, mock_cfg, mock_run):
        mock_cfg.return_value = _CFG
        findings = [{"rule_id": "lookalike_tld",
                     "description": "TLD .app looks like a file extension"}]
        mock_run.return_value = _mock_run(2, _json_stdout(findings))
        result = check_command_security("curl https://api.app/v1")
        assert result["action"] == "allow"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_mixed_findings_preserve_warn(self, mock_cfg, mock_run):
        """If .app finding is accompanied by another finding, warn is preserved."""
        mock_cfg.return_value = _CFG
        findings = [
            {"rule_id": "lookalike_tld", "value": ".app"},
            {"rule_id": "shortened_url", "severity": "medium"},
        ]
        mock_run.return_value = _mock_run(2, _json_stdout(findings, "mixed"))
        result = check_command_security("curl https://bit.ly/test.app")
        assert result["action"] == "warn"
        assert len(result["findings"]) == 2

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_non_app_lookalike_tld_preserved(self, mock_cfg, mock_run):
        """lookalike_tld for a non-.app TLD is not suppressed."""
        mock_cfg.return_value = _CFG
        findings = [{"rule_id": "lookalike_tld", "value": ".zip",
                     "message": "TLD .zip can be confused with zip archives"}]
        mock_run.return_value = _mock_run(2, _json_stdout(findings, ".zip TLD warning"))
        result = check_command_security("curl https://victim.zip")
        assert result["action"] == "warn"
        assert len(result["findings"]) == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_block_verdict_never_suppressed(self, mock_cfg, mock_run):
        """block exit code is never downgraded, even if finding looks like .app."""
        mock_cfg.return_value = _CFG
        findings = [{"rule_id": "lookalike_tld", "value": ".app"}]
        mock_run.return_value = _mock_run(1, _json_stdout(findings, "block"))
        result = check_command_security("curl https://example.app")
        assert result["action"] == "block"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_multiple_app_tld_findings_all_suppressed(self, mock_cfg, mock_run):
        """All findings being .app lookalike_tld → allow."""
        mock_cfg.return_value = _CFG
        findings = [
            {"rule_id": "lookalike_tld", "value": ".app"},
            {"rule_id": "lookalike_tld", "tld": ".app"},
        ]
        mock_run.return_value = _mock_run(2, _json_stdout(findings))
        result = check_command_security("curl https://a.app https://b.app")
        assert result["action"] == "allow"


class TestIsAppTldFinding:
    """Unit tests for the _is_app_tld_finding helper."""

    def setup_method(self):
        from tools.tirith_security import _is_app_tld_finding
        self.fn = _is_app_tld_finding

    def test_matching_value_field(self):
        assert self.fn({"rule_id": "lookalike_tld", "value": ".app"})

    def test_matching_tld_field(self):
        assert self.fn({"rule_id": "lookalike_tld", "tld": ".app"})

    def test_matching_description_field(self):
        assert self.fn({"rule_id": "lookalike_tld",
                        "description": "TLD .app looks like an executable"})

    def test_matching_message_field(self):
        assert self.fn({"rule_id": "lookalike_tld",
                        "message": "Domain uses '.app' TLD"})

    def test_wrong_rule_id(self):
        assert not self.fn({"rule_id": "shortened_url", "value": ".app"})

    def test_non_app_tld(self):
        assert not self.fn({"rule_id": "lookalike_tld", "value": ".zip"})

    def test_no_tld_value_fields(self):
        assert not self.fn({"rule_id": "lookalike_tld", "severity": "low"})

    def test_non_dict_input(self):
        assert not self.fn("not a dict")  # type: ignore[arg-type]

    def test_case_insensitive_match(self):
        assert self.fn({"rule_id": "lookalike_tld", "value": ".APP"})


# ---------------------------------------------------------------------------
# mkdtemp OSError → no_space (disk-full leak prevention)
# ---------------------------------------------------------------------------

class TestMkdtempOSErrorNoSpace:
    """When tempfile.mkdtemp raises OSError (e.g. disk full), _install_tirith
    must return (None, "no_space") instead of propagating the exception.
    This prevents the unbounded retry + temp-dir leak described in #51826.
    """

    def test_mkdtemp_oserror_returns_no_space(self):
        from tools.tirith_security import _install_tirith

        with patch("tools.tirith_security.tempfile.mkdtemp",
                   side_effect=OSError(28, "No space left on device")):
            result, reason = _install_tirith(log_failures=False)
            assert result is None
            assert reason == "no_space"

    def test_mkdtemp_oserror_does_not_leak_tempdir(self):
        """No temp directory should remain after a mkdtemp failure."""
        import glob
        from tools.tirith_security import _install_tirith

        before = set(glob.glob("/tmp/tirith-install-*"))
        with patch("tools.tirith_security.tempfile.mkdtemp",
                   side_effect=OSError(28, "No space left on device")):
            _install_tirith(log_failures=False)
        after = set(glob.glob("/tmp/tirith-install-*"))
        assert after - before == set()

    def test_mkdtemp_oserror_propagates_to_ensure_installed(self):
        """ensure_installed should cache the failure via _mark_install_failed."""
        from tools.tirith_security import _resolve_tirith_path, _INSTALL_FAILED

        _tirith_mod._resolved_path = None
        with patch("tools.tirith_security.tempfile.mkdtemp",
                   side_effect=OSError(28, "No space left on device")), \
             patch("tools.tirith_security.shutil.which",
                   return_value=None), \
             patch("tools.tirith_security._hermes_bin_dir",
                   return_value="/nonexistent"), \
             patch("tools.tirith_security._is_install_failed_on_disk",
                   return_value=False), \
             patch("tools.tirith_security._mark_install_failed") as mock_mark:
            result = _resolve_tirith_path("tirith")
            assert _tirith_mod._resolved_path is _INSTALL_FAILED
            mock_mark.assert_called_once_with("no_space")
