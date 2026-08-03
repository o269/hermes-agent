from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MONITOR = REPO_ROOT / "scripts" / "fleet" / "boardd-backup-monitor.py"
SERVICE = REPO_ROOT / "scripts" / "fleet" / "boardd-backup-monitor.service"
TIMER = REPO_ROOT / "scripts" / "fleet" / "boardd-backup-monitor.timer"
INSTALL = REPO_ROOT / "scripts" / "fleet" / "install-boardd-backup-monitor.sh"
ROLLBACK = REPO_ROOT / "scripts" / "fleet" / "rollback-boardd-backup-monitor.sh"


def _load_monitor():
    spec = importlib.util.spec_from_file_location("boardd_backup_monitor", MONITOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


monitor = _load_monitor()


def _backup_path(directory: Path, timestamp: float) -> Path:
    name = time.strftime("kanban.%Y%m%d-%H%M%S.db", time.localtime(timestamp))
    return directory / name


def _clean_backup(directory: Path, timestamp: float) -> Path:
    path = _backup_path(directory, timestamp)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE proof (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO proof(value) VALUES ('ok')")
        connection.commit()
    finally:
        connection.close()
    os.utime(path, (timestamp, timestamp))
    return path


def _failure_reason(exc: pytest.ExceptionInfo[Exception]) -> str:
    return exc.value.reason


def test_fresh_clean_backup_passes(tmp_path: Path):
    now = 2_000_000_000.0
    backup = _clean_backup(tmp_path, now - 100)
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
    }

    result = monitor.run_check(
        tmp_path,
        max_age_seconds=2700,
        now_ns=int(now * 1_000_000_000),
    )

    assert result.backup == backup
    assert result.age_seconds == pytest.approx(100)
    assert result.size > 0
    assert {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
    } == before


def test_stale_backup_fails_closed(tmp_path: Path):
    now = 2_000_000_000.0
    _clean_backup(tmp_path, now - 2701)

    with pytest.raises(monitor.CheckFailure) as exc:
        monitor.run_check(
            tmp_path,
            max_age_seconds=2700,
            now_ns=int(now * 1_000_000_000),
        )

    assert _failure_reason(exc) == "stale_backup"
    assert "age_seconds=2701" in exc.value.detail


def test_absent_backup_fails_closed(tmp_path: Path):
    with pytest.raises(monitor.CheckFailure) as exc:
        monitor.run_check(tmp_path, max_age_seconds=2700)

    assert _failure_reason(exc) == "no_finalized_backup"


def test_partial_and_malformed_names_are_ignored(tmp_path: Path):
    partial = tmp_path / ".kanban.20330518-033140.partial"
    partial.write_bytes(b"partial")
    (tmp_path / "kanban.not-a-timestamp.db").write_bytes(b"malformed")
    (tmp_path / "kanban.20339999-999999.db").write_bytes(b"invalid calendar")

    with pytest.raises(monitor.CheckFailure) as exc:
        monitor.run_check(tmp_path, max_age_seconds=2700)

    assert _failure_reason(exc) == "no_finalized_backup"


def test_corrupt_or_unopenable_backup_fails_closed(tmp_path: Path):
    now = 2_000_000_000.0
    backup = _backup_path(tmp_path, now - 10)
    backup.write_bytes(b"not a sqlite database")
    os.utime(backup, (now - 10, now - 10))

    with pytest.raises(monitor.CheckFailure) as exc:
        monitor.run_check(
            tmp_path,
            max_age_seconds=2700,
            now_ns=int(now * 1_000_000_000),
        )

    assert _failure_reason(exc) == "sqlite_open_or_query_failed"
    assert backup.name in exc.value.detail


def test_integrity_result_must_be_exactly_ok(tmp_path: Path):
    now = 2_000_000_000.0
    backup = _clean_backup(tmp_path, now - 10)

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConnection:
        def execute(self, sql: str):
            if "integrity_check" in sql:
                return FakeCursor([("row 7 missing from index proof",)])
            return FakeCursor([])

        def close(self):
            return None

    def connect(*_args, **_kwargs):
        return FakeConnection()

    with pytest.raises(monitor.CheckFailure) as exc:
        monitor.run_check(
            tmp_path,
            max_age_seconds=2700,
            now_ns=int(now * 1_000_000_000),
            connector=connect,
        )

    assert _failure_reason(exc) == "integrity_not_ok"
    assert backup.name in exc.value.detail


def test_corrupt_newest_is_not_hidden_by_older_clean_backup(tmp_path: Path):
    now = 2_000_000_000.0
    _clean_backup(tmp_path, now - 100)
    newest = _backup_path(tmp_path, now - 10)
    newest.write_bytes(b"newest but corrupt")
    os.utime(newest, (now - 10, now - 10))

    with pytest.raises(monitor.CheckFailure) as exc:
        monitor.run_check(
            tmp_path,
            max_age_seconds=2700,
            now_ns=int(now * 1_000_000_000),
        )

    assert _failure_reason(exc) == "sqlite_open_or_query_failed"
    assert newest.name in exc.value.detail


def test_disagreeing_timestamp_order_cannot_hide_newer_named_backup(tmp_path: Path):
    now = 2_000_000_000.0
    older_by_name = _clean_backup(tmp_path, now - 100)
    newer_by_name = _backup_path(tmp_path, now - 10)
    newer_by_name.write_bytes(b"newer name but older mtime")
    os.utime(older_by_name, (now - 5, now - 5))
    os.utime(newer_by_name, (now - 200, now - 200))

    with pytest.raises(monitor.CheckFailure) as exc:
        monitor.run_check(
            tmp_path,
            max_age_seconds=2700,
            now_ns=int(now * 1_000_000_000),
        )

    assert _failure_reason(exc) == "ambiguous_backup_order"
    assert older_by_name.name in exc.value.detail
    assert newer_by_name.name in exc.value.detail


def test_repeated_unchanged_backup_is_reverified_without_cached_success(tmp_path: Path):
    now = 2_000_000_000.0
    backup = _clean_backup(tmp_path, now - 10)
    calls = 0

    def connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        return sqlite3.connect(*args, **kwargs)

    first = monitor.run_check(
        tmp_path,
        max_age_seconds=2700,
        now_ns=int(now * 1_000_000_000),
        connector=connect,
    )
    second = monitor.run_check(
        tmp_path,
        max_age_seconds=2700,
        now_ns=int(now * 1_000_000_000),
        connector=connect,
    )

    assert first.backup == backup
    assert second.backup == backup
    assert calls == 2


def test_newly_arriving_backup_is_selected_and_verified(tmp_path: Path):
    now = 2_000_000_000.0
    older = _clean_backup(tmp_path, now - 100)
    calls = 0

    def connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        return sqlite3.connect(*args, **kwargs)

    first = monitor.run_check(
        tmp_path,
        max_age_seconds=2700,
        now_ns=int(now * 1_000_000_000),
        connector=connect,
    )
    newer = _clean_backup(tmp_path, now - 10)
    second = monitor.run_check(
        tmp_path,
        max_age_seconds=2700,
        now_ns=int(now * 1_000_000_000),
        connector=connect,
    )

    assert first.backup == older
    assert second.backup == newer
    assert calls == 2


def test_future_timestamp_fails_clock_skew_check(tmp_path: Path):
    now = 2_000_000_000.0
    future = _clean_backup(tmp_path, now + 60)

    with pytest.raises(monitor.CheckFailure) as exc:
        monitor.run_check(
            tmp_path,
            max_age_seconds=2700,
            now_ns=int(now * 1_000_000_000),
        )

    assert _failure_reason(exc) == "clock_skew"
    assert future.name in exc.value.detail


def test_cli_rejects_threshold_that_is_not_safely_above_cadence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    return_code = monitor.main([
        "--backup-dir",
        str(tmp_path),
        "--backup-interval-seconds",
        "900",
        "--max-age-seconds",
        "1799",
    ])

    captured = capsys.readouterr()
    assert return_code == 2
    assert "reason=unsafe_threshold" in captured.err


def test_units_are_valid_and_isolate_the_live_database():
    service_text = SERVICE.read_text(encoding="utf-8")
    timer_text = TIMER.read_text(encoding="utf-8")
    assert "InaccessiblePaths=-/var/lib/boardd/fleet/kanban.db" in service_text
    assert "BOARDD_SOCK" not in service_text
    assert "/home/" not in service_text
    assert "OnCalendar=*:0/5" in timer_text
    assert "Persistent=true" in timer_text

    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze is not None:
        verified = subprocess.run(
            [systemd_analyze, "verify", str(SERVICE), str(TIMER)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert verified.returncode == 0, verified.stderr


def test_installer_stages_immutable_release_without_service_mutation(tmp_path: Path):
    source = tmp_path / "source"
    for path in (MONITOR, SERVICE, TIMER, INSTALL):
        relative = path.relative_to(REPO_ROOT)
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )

    destdir = tmp_path / "stage"
    result = subprocess.run(
        [
            str(source / INSTALL.relative_to(REPO_ROOT)),
            "--source",
            str(source),
            "--destdir",
            str(destdir),
            "--release-id",
            "test-release",
            "--activate",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    prefix = destdir / "opt" / "hermes-boardd-backup-monitor"
    release = prefix / "releases" / "test-release"
    assert (release / "boardd-backup-monitor.py").stat().st_mode & 0o777 == 0o555
    assert (release / "MANIFEST").is_file()
    assert os.readlink(prefix / "current") == "releases/test-release"
    assert (destdir / "etc/systemd/system/boardd-backup-monitor.service").is_file()
    assert (destdir / "etc/systemd/system/boardd-backup-monitor.timer").is_file()
    assert "systemctl_mutation=none" in result.stdout

    repeated = subprocess.run(
        [
            str(source / INSTALL.relative_to(REPO_ROOT)),
            "--source",
            str(source),
            "--destdir",
            str(destdir),
            "--release-id",
            "test-release",
            "--activate",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "staged_release=" in repeated.stdout

    source_monitor = source / MONITOR.relative_to(REPO_ROOT)
    source_monitor.chmod(0o755)
    source_monitor.write_text(
        source_monitor.read_text(encoding="utf-8") + "\n# changed fixture\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "changed fixture",
        ],
        check=True,
    )
    collision = subprocess.run(
        [
            str(source / INSTALL.relative_to(REPO_ROOT)),
            "--source",
            str(source),
            "--destdir",
            str(destdir),
            "--release-id",
            "test-release",
        ],
        text=True,
        capture_output=True,
    )
    assert collision.returncode != 0
    assert "does not match source" in collision.stderr


def test_rollback_only_swaps_monitor_release_links(tmp_path: Path):
    prefix = tmp_path / "opt" / "hermes-boardd-backup-monitor"
    for release in ("old", "new"):
        monitor_path = prefix / "releases" / release / "boardd-backup-monitor.py"
        monitor_path.parent.mkdir(parents=True, exist_ok=True)
        payload = "#!/usr/bin/env python3\n"
        monitor_path.write_text(payload, encoding="utf-8")
        monitor_path.chmod(0o555)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        (monitor_path.parent / "MANIFEST").write_text(
            f"monitor_sha256={digest}\n",
            encoding="utf-8",
        )
    (prefix / "current").symlink_to("releases/new")
    (prefix / "previous").symlink_to("releases/old")

    result = subprocess.run(
        [str(ROLLBACK), "--destdir", str(tmp_path)],
        check=True,
        text=True,
        capture_output=True,
    )

    assert os.readlink(prefix / "current") == "releases/old"
    assert os.readlink(prefix / "previous") == "releases/new"
    assert "systemctl_mutation=none" in result.stdout


def test_rollback_rejects_unsafe_release_link(tmp_path: Path):
    prefix = tmp_path / "opt" / "hermes-boardd-backup-monitor"
    monitor_path = prefix / "releases" / "old" / "boardd-backup-monitor.py"
    monitor_path.parent.mkdir(parents=True, exist_ok=True)
    monitor_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monitor_path.chmod(0o555)
    (prefix / "current").symlink_to("../../outside")
    (prefix / "previous").symlink_to("releases/old")

    result = subprocess.run(
        [str(ROLLBACK), "--destdir", str(tmp_path)],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "refusing unsafe current monitor target" in result.stderr
