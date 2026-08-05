from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARDD = REPO_ROOT / "scripts" / "fleet" / "boardd.py"
SERVICE = REPO_ROOT / "scripts" / "fleet" / "boardd.service"
ROLLBACK = REPO_ROOT / "scripts" / "fleet" / "rollback-boardd-runtime.sh"

import pytest


def _load_repo_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kb_client = _load_repo_module(
    "boardd_runtime_kb_client",
    REPO_ROOT / "hermes_cli" / "kb_client.py",
)
boardd_runtime = _load_repo_module("boardd_runtime", BOARDD)
BoarddError = kb_client.BoarddError
BoarddUnavailable = kb_client.BoarddUnavailable
Client = kb_client.Client
boardd_runtime = _load_repo_module("boardd_runtime_daemon", BOARDD)


def _repo_schema_sql() -> str:
    from hermes_cli import kanban_db

    return kanban_db.SCHEMA_SQL


@contextmanager
def running_boardd(
    tmp_path: Path,
    *,
    schema_file: Path | None = None,
    import_schema: bool = False,
    write_canary_mode: str = "disabled",
    write_canary_start_delay: float = 0,
    write_canary_interval: float = 300,
    write_canary_timeout: float = 5,
    env_overrides: dict[str, str] | None = None,
):
    db_path = tmp_path / "kanban.db"
    sock_path = tmp_path / "boardd.sock"
    command = [
        sys.executable,
        str(BOARDD),
        "--db",
        str(db_path),
        "--sock",
        str(sock_path),
        "--log-level",
        "DEBUG",
        "--write-canary-mode",
        write_canary_mode,
        "--write-canary-start-delay",
        str(write_canary_start_delay),
        "--write-canary-interval",
        str(write_canary_interval),
        "--write-canary-timeout",
        str(write_canary_timeout),
    ]
    if schema_file is not None:
        command.extend(["--schema-sql-file", str(schema_file)])
    if import_schema:
        command.append("--import-schema")
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(tmp_path / "hermes-home"),
            "HERMES_KANBAN_BROKER": "0",
            "KB_CLIENT_RETRY_DEADLINE_S": "2",
            "BOARDD_DISKGUARD_MIN_FREE_BYTES": "0",
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    client: Client | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"boardd exited during startup (rc={proc.returncode})\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        if sock_path.exists():
            probe = Client(str(sock_path))
            try:
                probe.ping()
            except Exception:
                probe.close()
            else:
                client = probe
                break
        time.sleep(0.05)
    if client is None:
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)
        raise AssertionError(
            f"boardd did not become ready\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
    try:
        yield client, db_path, sock_path
    finally:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _create_through_kanban_db(
    db_path: Path, socket_path: Path, *, reasoning_effort: str
) -> dict[str, object]:
    """Exercise kanban_db.create_task through the broker, never raw SQLite."""
    hermes_home = db_path.parent / "client-home"
    fleet_dir = hermes_home / "kanban" / "boards" / "fleet"
    fleet_dir.mkdir(parents=True, exist_ok=True)
    (fleet_dir / "kanban.db").symlink_to(db_path)
    code = """
import json
from hermes_cli import kanban_db as kb
with kb.connect() as conn:
    assert type(conn).__name__ == "BrokerConnection"
    task_id = kb.create_task(
        conn,
        title="kanban-db broker create",
        assignee="codex7",
        reasoning_effort=%r,
    )
    task = kb.get_task(conn, task_id)
    print(json.dumps({"id": task_id, "reasoning_effort": task.reasoning_effort}))
""" % reasoning_effort
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "HERMES_HOME": str(hermes_home),
            "HERMES_KANBAN_BROKER": "1",
            "HERMES_KANBAN_DB": str(db_path),
            "BOARDD_SOCK": str(socket_path),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_kanban_db_script(db_path: Path, socket_path: Path, code: str) -> dict:
    """Run a focused canonical kanban_db client against a disposable broker."""
    hermes_home = db_path.parent / "script-client-home"
    fleet_dir = hermes_home / "kanban" / "boards" / "fleet"
    fleet_dir.mkdir(parents=True, exist_ok=True)
    db_link = fleet_dir / "kanban.db"
    if not db_link.exists():
        db_link.symlink_to(db_path)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "HERMES_HOME": str(hermes_home),
            "HERMES_KANBAN_BROKER": "1",
            "HERMES_KANBAN_DB": str(db_path),
            "BOARDD_SOCK": str(socket_path),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_typed_client_exact_dead_lane_bypass_is_atomic_and_positive_control_works(
    tmp_path: Path,
):
    receipts = tmp_path / "lane-health"
    receipts.mkdir()
    probed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (receipts / "codex1.env").write_text(
        f"LANE_OK=true\nPROBED_AT={probed_at}\nPROVIDER=test\n",
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        "kanban:\n"
        "  authority_profiles: fable\n"
        f"  lane_health_receipts_dir: {receipts}\n"
        "  lane_health_max_age_seconds: 86400\n"
        "  de_rostered_profile_prefixes: kimi\n",
        encoding="utf-8",
    )

    with running_boardd(tmp_path, import_schema=True) as (client, _, _):
        before = client.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"]
        with pytest.raises(BoarddError, match="lane_derostered:kimi1"):
            client.create_task(
                id="t_dead0001",
                title="[AUTHOR] exact dead-lane typed-client bypass",
                assignee="kimi1",
                status="ready",
            )
        assert client.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == before
        assert client.get_task("t_dead0001") is None
        assert client.query(
            "SELECT id, worker_pid FROM tasks WHERE id=?", ["t_dead0001"]
        ) == []
        assert client.query(
            "SELECT COUNT(*) AS n FROM task_runs WHERE task_id=?", ["t_dead0001"]
        )[0]["n"] == 0

        created = client.create_task(
            id="t_good0001",
            title="[AUTHOR] healthy typed-client positive control",
            assignee="codex1",
            status="ready",
        )
        assert created == {"id": "t_good0001", "status": "ready"}
        assert client.get_task("t_good0001")["assignee"] == "codex1"

        with pytest.raises(BoarddError, match="lane_derostered:kimi1"):
            client.assign_task("t_good0001", "kimi1")
        assert client.get_task("t_good0001")["assignee"] == "codex1"

        archived = client.create_task(
            id="t_dead0002",
            title="[AUTHOR] dead-lane terminal history",
            assignee="kimi1",
            status="done",
        )
        assert archived == {"id": "t_dead0002", "status": "done"}
        with pytest.raises(BoarddError, match="lane_derostered:kimi1"):
            client.set_status("t_dead0002", "ready")
        terminal = client.get_task("t_dead0002")
        assert terminal["status"] == "done"
        assert terminal["worker_pid"] is None
        assert client.query(
            "SELECT COUNT(*) AS n FROM task_runs WHERE task_id=?", ["t_dead0002"]
        )[0]["n"] == 0

        with pytest.raises(BoarddError, match="typed Kanban broker API"):
            client.exec_write(
                "UPDATE tasks SET assignee=? WHERE id=?",
                ["kimi1", "t_good0001"],
            )
        assert client.get_task("t_good0001")["assignee"] == "codex1"


def test_restart_preserves_broker_reasoning_effort_create_list_show(tmp_path: Path):
    with running_boardd(tmp_path, import_schema=True) as (
        client,
        db_path,
        socket_path,
    ):
        through_db = _create_through_kanban_db(
            db_path,
            socket_path,
            reasoning_effort="minimal",
        )
        assert through_db["reasoning_effort"] == "minimal"

        created = client.create_task(
            title="reasoning restart probe",
            assignee="Codex7",
            status="running",
            reasoning_effort="HIGH",
        )
        task_id = created["id"]
        shown = client.get_task(task_id)
        listed = {row["id"]: row for row in client.list_tasks(assignee="codex7")}
        assert shown["reasoning_effort"] == "high"
        assert listed[task_id]["reasoning_effort"] == "high"
        assert db_path.exists()

    # Start a new daemon process on the same broker-owned DB. All verification
    # still travels over the socket; this test never opens SQLite directly.
    with running_boardd(tmp_path, import_schema=True) as (client, _, _):
        assert client.ping()["pid"] > 0
        assert client.get_task(task_id)["reasoning_effort"] == "high"
        assert task_id in {row["id"] for row in client.list_tasks(assignee="codex7")}
        with pytest.raises(BoarddError, match="reasoning_effort"):
            client.create_task(
                title="bad reasoning",
                status="running",
                reasoning_effort="turbo",
            )


def test_import_schema_adds_reasoning_effort_to_legacy_board(tmp_path: Path):
    legacy_schema = _repo_schema_sql().replace(
        "    reasoning_effort     TEXT,\n",
        "",
    )
    assert "    reasoning_effort     TEXT,\n" not in legacy_schema
    legacy_schema_path = tmp_path / "legacy-schema.sql"
    legacy_schema_path.write_text(legacy_schema, encoding="utf-8")

    with running_boardd(tmp_path, schema_file=legacy_schema_path) as (client, _, _):
        columns = {row["name"] for row in client.query("PRAGMA table_info(tasks)")}
        assert "reasoning_effort" not in columns

    with running_boardd(tmp_path, import_schema=True) as (client, _, _):
        columns = {row["name"] for row in client.query("PRAGMA table_info(tasks)")}
        assert "reasoning_effort" in columns
        task_id = client.create_task(
            title="legacy migration probe",
            status="running",
            reasoning_effort="medium",
        )["id"]
        assert client.get_task(task_id)["reasoning_effort"] == "medium"


def test_schema_drift_check_reports_healthy_task_column_parity(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_repo_schema_sql())
    broker = boardd_runtime.Broker(
        str(tmp_path / "kanban.db"),
        str(tmp_path / "boardd.sock"),
        import_schema=True,
    )
    broker.conn = conn
    try:
        result = broker._check_task_schema_drift()
    finally:
        conn.close()

    assert result["status"] == "ok"
    assert result["expected_column_count"] == result["live_column_count"]
    assert broker._schema_drift_alarm is None
    assert broker._schema_check_error is None


def test_schema_drift_check_names_missing_expected_column(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    legacy_schema = _repo_schema_sql().replace(
        "    reasoning_effort     TEXT,\n",
        "",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(legacy_schema)
    broker = boardd_runtime.Broker(
        str(tmp_path / "kanban.db"),
        str(tmp_path / "boardd.sock"),
        import_schema=True,
    )
    broker.conn = conn
    caplog.set_level(logging.ERROR, logger="boardd")
    try:
        result = broker._check_task_schema_drift()
    finally:
        conn.close()

    assert result["status"] == "drift"
    assert result["missing_columns"] == ["reasoning_effort"]
    assert broker._schema_drift_alarm["missing_columns"] == ["reasoning_effort"]
    assert '"kind": "missing_task_columns"' in caplog.text
    assert '"missing_columns": ["reasoning_effort"]' in caplog.text


def test_boardd_startup_invokes_schema_drift_check(tmp_path: Path):
    legacy_schema = _repo_schema_sql().replace(
        "    reasoning_effort     TEXT,\n",
        "",
    )
    schema_file = tmp_path / "legacy-schema.sql"
    schema_file.write_text(legacy_schema, encoding="utf-8")

    with running_boardd(tmp_path, schema_file=schema_file) as (client, _, _):
        health = client.ping()
        assert health["schema_checks"] >= 1
        assert health["last_schema_check_ts"] > 0
        assert health["schema_drift_alarm"]["missing_columns"] == [
            "reasoning_effort"
        ]
        assert health["schema_check_error"] is None


def test_periodic_schema_drift_check_dedupes_unchanged_alert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    legacy_schema = _repo_schema_sql().replace(
        "    reasoning_effort     TEXT,\n",
        "",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(legacy_schema)
    broker = boardd_runtime.Broker(
        str(tmp_path / "kanban.db"),
        str(tmp_path / "boardd.sock"),
        import_schema=True,
    )
    broker.conn = conn

    class StopAfterTwoChecks:
        calls = 0

        def wait(self, _timeout: float) -> bool:
            self.calls += 1
            return self.calls > 2

    broker._stop = StopAfterTwoChecks()

    def call_immediately(req: dict, timeout: float):
        del timeout
        return broker._handle(req)

    monkeypatch.setattr(broker, "call_sync", call_immediately)
    caplog.set_level(logging.ERROR, logger="boardd")
    try:
        broker._schema_check_loop()
    finally:
        conn.close()

    assert broker._schema_checks == 2
    alerts = [
        record
        for record in caplog.records
        if "SCHEMA DRIFT ALERT" in record.getMessage()
    ]
    assert len(alerts) == 1
    assert '"missing_columns": ["reasoning_effort"]' in alerts[0].getMessage()


def test_schema_drift_introspection_failure_is_safe_and_deduped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    class FailingConnection:
        def execute(self, _sql: str):
            raise sqlite3.OperationalError("read-only introspection failed")

    broker = boardd_runtime.Broker(
        str(tmp_path / "kanban.db"),
        str(tmp_path / "boardd.sock"),
        import_schema=True,
    )
    broker.conn = FailingConnection()
    caplog.set_level(logging.WARNING, logger="boardd")

    first = broker._check_task_schema_drift()
    second = broker._check_task_schema_drift()

    assert first["status"] == "error"
    assert second["status"] == "error"
    assert broker._schema_drift_alarm is None
    assert broker._schema_check_error["stage"] == "live_schema"
    warnings = [
        record
        for record in caplog.records
        if "SCHEMA DRIFT CHECK WARNING" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert '"kind": "introspection_error"' in warnings[0].getMessage()


def test_service_unit_and_reasoning_tool_schema_are_runtime_valid():
    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze is not None:
        verified = subprocess.run(
            [systemd_analyze, "security", "--offline=yes", str(SERVICE)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert verified.returncode == 0, verified.stderr
        assert "Overall exposure level for boardd.service" in verified.stdout

    schema_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from tools.kanban_tools import KANBAN_CREATE_SCHEMA as s; "
                "p=s['parameters']['properties']['reasoning_effort']; "
                "assert 'ultra' in p['enum'] and 'none' in p['enum']"
            ),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert schema_probe.returncode == 0, schema_probe.stderr


def test_rollback_swaps_release_links_without_touching_service(tmp_path: Path):
    prefix = tmp_path / "opt" / "hermes-boardd"
    for release in ("old", "new"):
        python = prefix / "releases" / release / "venv" / "bin" / "python"
        boardd = prefix / "releases" / release / "libexec" / "boardd.py"
        python.parent.mkdir(parents=True)
        boardd.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(0o755)
        boardd.write_text("# broker\n", encoding="utf-8")
    (prefix / "current").symlink_to("releases/new")
    (prefix / "previous").symlink_to("releases/old")

    result = subprocess.run(
        [str(ROLLBACK), "--destdir", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert os.readlink(prefix / "current") == "releases/old"
    assert os.readlink(prefix / "previous") == "releases/new"
    assert "service_mutation=none" in result.stdout


class _FakeCanaryOps:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.created = 0
        self.archive_calls: list[str] = []
        self.archive_outcomes: list[object] = []
        self.create_error: Exception | None = None
        self.mismatch_next_get = False
        self.before_archive: Callable[[dict], None] | None = None

    def find_active_candidates(self, limit: int) -> list[dict]:
        rows = [row.copy() for row in self.rows.values() if row["status"] != "archived"]
        return rows[:limit]

    def create(self, identity: dict) -> str:
        if self.create_error is not None:
            raise self.create_error
        self.created += 1
        task_id = f"t_canary{self.created:02d}"
        self.rows[task_id] = {
            "id": task_id,
            "title": identity["title"],
            "body": identity["body"],
            "status": "blocked",
            "created_by": boardd_runtime.WRITE_CANARY_CREATED_BY,
            "idempotency_key": identity["idempotency_key"],
            "created_at": self.created,
        }
        return task_id

    def get(self, task_id: str) -> dict | None:
        row = self.rows.get(task_id)
        if row is None:
            return None
        shown = row.copy()
        if self.mismatch_next_get:
            self.mismatch_next_get = False
            shown["body"] = "{}"
        return shown

    def archive(self, task_id: str, identity: dict) -> bool:
        self.archive_calls.append(task_id)
        if self.before_archive is not None:
            callback = self.before_archive
            self.before_archive = None
            callback(self.rows[task_id])
        row = self.rows.get(task_id)
        if row is None:
            return False
        expected = {
            "title": identity["title"],
            "body": identity["body"],
            "created_by": identity.get(
                "created_by", boardd_runtime.WRITE_CANARY_CREATED_BY
            ),
            "idempotency_key": identity["idempotency_key"],
        }
        if any(row.get(field) != value for field, value in expected.items()):
            return False
        if self.archive_outcomes:
            outcome = self.archive_outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if not outcome:
                return False
        row["status"] = "archived"
        return True


def _unit_broker(
    tmp_path: Path,
    *,
    alert_repeat_s: float = 3600,
    mode: str = "periodic",
):
    return boardd_runtime.Broker(
        str(tmp_path / "kanban.db"),
        str(tmp_path / "boardd.sock"),
        import_schema=True,
        write_canary_mode=mode,
        write_canary_start_delay_s=0,
        write_canary_timeout_s=2,
        write_canary_alert_repeat_s=alert_repeat_s,
    )


def test_client_total_deadline_rejects_trickled_response():
    client_sock, server_sock = socket.socketpair()
    client = Client(retry_deadline_s=1, read_timeout_s=1, total_timeout_s=0.15)
    client._sock = client_sock

    def trickle_response() -> None:
        try:
            server_sock.recv(4096)
            for byte in b'{"ok":true,"result":{}}\n':
                time.sleep(0.1)
                server_sock.send(bytes([byte]))
        except OSError:
            pass
        finally:
            server_sock.close()

    server = threading.Thread(target=trickle_response, daemon=True)
    server.start()
    started = time.monotonic()
    try:
        with pytest.raises(BoarddUnavailable, match="after 1 attempts"):
            client.ping()
    finally:
        client.close()
        server.join(timeout=1)

    # A per-read timeout resets on every byte and takes >2s for this frame;
    # the absolute deadline must reject it well before that loose CI bound.
    assert time.monotonic() - started < 2


def test_write_canary_success_and_overlap_suppression(tmp_path: Path):
    broker = _unit_broker(tmp_path)
    ops = _FakeCanaryOps()

    result = broker.run_write_canary_once(ops)

    assert result["ok"] is True
    assert result["status"] == "healthy"
    assert ops.rows[result["task_id"]]["status"] == "archived"
    health = broker._health()
    assert health["write_canary_ok"] is True
    assert health["write_canary"]["runs"] == 1

    broker._write_canary_lock.acquire()
    try:
        overlap = broker.run_write_canary_once(ops)
    finally:
        broker._write_canary_lock.release()
    assert overlap == {
        "ok": True,
        "status": "suppressed-overlap",
        "reason": "canary",
    }

    broker._maintenance_lock.acquire()
    try:
        backup_overlap = broker.run_write_canary_once(ops)
    finally:
        broker._maintenance_lock.release()
    assert backup_overlap["reason"] == "backup-maintenance"
    assert broker._health()["write_canary"]["overlap_suppressed"] == 2


def test_write_canary_once_retries_overlap_until_an_actual_probe(
    tmp_path: Path, monkeypatch
):
    broker = _unit_broker(tmp_path, mode="once")
    results = [
        {"ok": True, "status": "suppressed-overlap"},
        {"ok": True, "status": "healthy"},
    ]
    calls = []

    def run_once():
        calls.append(len(calls))
        return results.pop(0)

    monkeypatch.setattr(broker, "run_write_canary_once", run_once)

    broker._write_canary_loop()

    assert calls == [0, 1]


def test_write_canary_failure_dedup_repeat_and_recovery(tmp_path: Path, monkeypatch):
    broker = _unit_broker(tmp_path, alert_repeat_s=10)
    ops = _FakeCanaryOps()
    ops.create_error = sqlite3.OperationalError(
        "table tasks has no column named reasoning_effort"
    )
    clock = [100]
    events: list[dict] = []
    monkeypatch.setattr(boardd_runtime, "_now", lambda: clock[0])
    monkeypatch.setattr(broker, "_emit_write_canary_event", events.append)

    first = broker.run_write_canary_once(ops)
    clock[0] = 101
    ops.create_error = sqlite3.OperationalError("no such column: a_different_column")
    second = broker.run_write_canary_once(ops)
    clock[0] = 111
    third = broker.run_write_canary_once(ops)

    assert first["kind"] == "schema-drift"
    assert second["kind"] == "schema-drift"
    assert third["kind"] == "schema-drift"
    assert [event["event"] for event in events] == ["failure", "repeat"]
    assert broker._health()["write_canary_alarm"]["count"] == 3

    ops.create_error = None
    clock[0] = 112
    recovered = broker.run_write_canary_once(ops)
    assert recovered["ok"] is True
    assert [event["event"] for event in events] == ["failure", "repeat", "recovery"]
    assert broker._health()["write_canary_alarm"] is None


def test_write_canary_restores_dedupe_and_recovery_state_after_restart(
    tmp_path: Path, monkeypatch
):
    clock = [100]
    monkeypatch.setattr(boardd_runtime, "_now", lambda: clock[0])
    ops = _FakeCanaryOps()
    ops.create_error = sqlite3.OperationalError("no such column: reasoning_effort")

    first_broker = _unit_broker(tmp_path, alert_repeat_s=10)
    first_broker.run_write_canary_once(ops)
    alert_path = tmp_path / boardd_runtime.WRITE_CANARY_ALERT_FILE
    assert len(alert_path.read_text(encoding="utf-8").splitlines()) == 1

    clock[0] = 101
    restarted_broker = _unit_broker(tmp_path, alert_repeat_s=10)
    restored_health = restarted_broker._health()
    assert restored_health["write_canary_ok"] is False
    assert restored_health["write_canary_alarm"]["count"] == 1

    restarted_broker.run_write_canary_once(ops)
    assert len(alert_path.read_text(encoding="utf-8").splitlines()) == 1
    assert restarted_broker._health()["write_canary_alarm"]["count"] == 2

    clock[0] = 102
    ops.create_error = None
    restarted_broker.run_write_canary_once(ops)
    events = [
        json.loads(line)
        for line in alert_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == ["failure", "recovery"]

    after_recovery = _unit_broker(tmp_path, alert_repeat_s=10)
    assert after_recovery._health()["write_canary_alarm"] is None


def test_write_canary_reports_orphan_then_reconciles_it(tmp_path: Path, monkeypatch):
    broker = _unit_broker(tmp_path)
    ops = _FakeCanaryOps()
    # Initial archive + one bounded cleanup retry both fail.
    ops.archive_outcomes = [False, False]
    monkeypatch.setattr(broker, "_emit_write_canary_event", lambda _event: None)

    failed = broker.run_write_canary_once(ops)

    orphan_id = failed["task_id"]
    assert failed["kind"] == "write-canary-archive"
    assert failed["phase"] == "archive"
    assert failed["orphan_task_ids"] == [orphan_id]
    assert ops.rows[orphan_id]["status"] == "blocked"

    recovered = broker.run_write_canary_once(ops)
    assert recovered["ok"] is True
    assert recovered["reconciled_task_ids"] == [orphan_id]
    assert ops.rows[orphan_id]["status"] == "archived"


def test_write_canary_verification_failure_attempts_terminal_cleanup(
    tmp_path: Path, monkeypatch
):
    broker = _unit_broker(tmp_path)
    ops = _FakeCanaryOps()
    ops.mismatch_next_get = True
    monkeypatch.setattr(broker, "_emit_write_canary_event", lambda _event: None)

    result = broker.run_write_canary_once(ops)

    assert result["ok"] is False
    assert result["kind"] == "write-canary-verification"
    assert result["phase"] == "verify-create"
    assert result["orphan_task_ids"] == []
    assert ops.rows[result["task_id"]]["status"] == "archived"


def test_write_canary_classifies_slow_transaction_timeout(tmp_path: Path, monkeypatch):
    broker = _unit_broker(tmp_path)
    ops = _FakeCanaryOps()
    ops.create_error = sqlite3.OperationalError(
        "TxnStale: interactive txn exceeded absolute cap 2.0s (slow holder)"
    )
    monkeypatch.setattr(broker, "_emit_write_canary_event", lambda _event: None)

    result = broker.run_write_canary_once(ops)

    assert result["ok"] is False
    assert result["kind"] == "write-canary-timeout"
    assert result["phase"] == "create"
    assert boardd_runtime.TXN_MAX_S == 2.0


def test_write_canary_refuses_partial_marker_collision(tmp_path: Path, monkeypatch):
    broker = _unit_broker(tmp_path)
    ops = _FakeCanaryOps()
    ops.rows["t_real"] = {
        "id": "t_real",
        "title": "operator card",
        "body": "not a canary",
        "status": "blocked",
        "created_by": boardd_runtime.WRITE_CANARY_CREATED_BY,
        "idempotency_key": None,
        "created_at": 1,
    }
    monkeypatch.setattr(broker, "_emit_write_canary_event", lambda _event: None)

    result = broker.run_write_canary_once(ops)

    assert result["kind"] == "write-canary-identity-collision"
    assert result["phase"] == "reconcile-identity"
    assert result["orphan_task_ids"] == []
    assert ops.archive_calls == []
    assert ops.rows["t_real"]["status"] == "blocked"


def test_write_canary_reconcile_limit_reports_discovered_orphans(
    tmp_path: Path, monkeypatch
):
    broker = _unit_broker(tmp_path)
    ops = _FakeCanaryOps()
    expected_ids = []
    for index in range(boardd_runtime.WRITE_CANARY_STALE_LIMIT + 1):
        identity = broker._canary_identity(f"stale-{index}")
        task_id = f"t_stale{index:02d}"
        expected_ids.append(task_id)
        ops.rows[task_id] = {
            "id": task_id,
            "title": identity["title"],
            "body": identity["body"],
            "status": "blocked",
            "created_by": boardd_runtime.WRITE_CANARY_CREATED_BY,
            "idempotency_key": identity["idempotency_key"],
            "created_at": index,
        }
    monkeypatch.setattr(broker, "_emit_write_canary_event", lambda _event: None)

    result = broker.run_write_canary_once(ops)

    assert result["kind"] == "write-canary-reconcile-limit"
    assert result["orphan_task_ids"] == expected_ids
    assert ops.archive_calls == []


def test_stale_canary_archive_failure_gets_one_guarded_cleanup_retry(
    tmp_path: Path, monkeypatch
):
    broker = _unit_broker(tmp_path)
    ops = _FakeCanaryOps()
    stale_identity = broker._canary_identity()
    stale_id = "t_stale_retry"
    ops.rows[stale_id] = {
        "id": stale_id,
        "title": stale_identity["title"],
        "body": stale_identity["body"],
        "status": "blocked",
        "created_by": boardd_runtime.WRITE_CANARY_CREATED_BY,
        "idempotency_key": stale_identity["idempotency_key"],
        "created_at": 1,
    }
    ops.archive_outcomes = [False, True]
    monkeypatch.setattr(broker, "_emit_write_canary_event", lambda _event: None)

    result = broker.run_write_canary_once(ops)

    assert result["ok"] is False
    assert result["phase"] == "reconcile-archive"
    assert result["task_id"] == stale_id
    assert result["orphan_task_ids"] == []
    assert ops.archive_calls == [stale_id, stale_id]
    assert ops.rows[stale_id]["status"] == "archived"


def test_write_canary_guard_refuses_repurposed_row(tmp_path: Path, monkeypatch):
    broker = _unit_broker(tmp_path)
    ops = _FakeCanaryOps()
    ops.before_archive = lambda row: row.__setitem__("body", "operator changed it")
    monkeypatch.setattr(broker, "_emit_write_canary_event", lambda _event: None)

    result = broker.run_write_canary_once(ops)

    assert result["kind"] == "write-canary-archive"
    assert result["phase"] == "archive"
    assert result["orphan_task_ids"] == []
    assert "guarded cleanup refused" in result["cleanup_errors"][0]
    assert ops.rows[result["task_id"]]["status"] == "blocked"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("write_canary_start_delay_s", float("nan")),
        ("write_canary_start_delay_s", float("inf")),
        ("write_canary_interval_s", float("nan")),
        ("write_canary_interval_s", float("inf")),
        ("write_canary_timeout_s", float("nan")),
        ("write_canary_timeout_s", float("inf")),
        ("write_canary_alert_repeat_s", float("nan")),
        ("write_canary_alert_repeat_s", float("inf")),
    ],
)
def test_write_canary_rejects_non_finite_durations(
    tmp_path: Path, field: str, value: float
):
    kwargs = {
        "import_schema": True,
        "write_canary_mode": "once",
        "write_canary_start_delay_s": 0,
        "write_canary_interval_s": 300,
        "write_canary_timeout_s": 2,
        "write_canary_alert_repeat_s": 3600,
        field: value,
    }
    with pytest.raises(ValueError, match="must be finite"):
        boardd_runtime.Broker(
            str(tmp_path / "kanban.db"),
            str(tmp_path / "boardd.sock"),
            **kwargs,
        )


def test_write_canary_once_uses_real_broker_create_and_archive_path(tmp_path: Path):
    with running_boardd(
        tmp_path,
        import_schema=True,
        write_canary_mode="once",
        write_canary_start_delay=1,
    ) as (client, db_path, socket_path):
        near_key = boardd_runtime.WRITE_CANARY_MARKER.replace("_", "x") + ":near"
        near = _run_kanban_db_script(
            db_path,
            socket_path,
            f"""
import json
from hermes_cli import kanban_db as kb
with kb.connect() as conn:
    task_id = kb.create_task(
        conn,
        title="operator near-match",
        body="not a canary",
        created_by="operator",
        initial_status="blocked",
        idempotency_key={near_key!r},
    )
    print(json.dumps({{"id": task_id}}))
""",
        )
        deadline = time.monotonic() + 10
        health = None
        while time.monotonic() < deadline:
            health = client.ping()
            if health["write_canary"]["last_success_ts"] is not None:
                break
            time.sleep(0.05)
        assert health is not None
        assert health["write_canary_ok"] is True
        assert health["write_canary"]["runs"] == 1
        rows = client.query(
            "SELECT id, title, status, created_by, idempotency_key FROM tasks "
            "WHERE created_by = ?",
            [boardd_runtime.WRITE_CANARY_CREATED_BY],
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "archived"
        assert rows[0]["title"].startswith(boardd_runtime.WRITE_CANARY_TITLE_PREFIX)
        assert rows[0]["idempotency_key"].startswith(
            f"{boardd_runtime.WRITE_CANARY_MARKER}:"
        )
        near_row = client.get_task(near["id"])
        assert near_row["status"] == "blocked"
        assert near_row["body"] == "not a canary"


def test_write_canary_detects_title_only_reserved_marker_collision(tmp_path: Path):
    # Periodic short-interval mode (not once+wall-clock delay): insert the
    # collision first, then wait for a post-insertion canary run. Avoids the
    # race where once-mode fires healthy before the collision exists.
    with running_boardd(
        tmp_path,
        import_schema=True,
        write_canary_mode="periodic",
        write_canary_start_delay=0,
        write_canary_interval=0.15,
        write_canary_timeout=5,
    ) as (client, db_path, socket_path):
        collision = _run_kanban_db_script(
            db_path,
            socket_path,
            f"""
import json
from hermes_cli import kanban_db as kb
with kb.connect() as conn:
    task_id = kb.create_task(
        conn,
        title={f"{boardd_runtime.WRITE_CANARY_TITLE_PREFIX} operator"!r},
        body="operator card",
        created_by="operator",
        initial_status="blocked",
        idempotency_key="operator-key",
    )
    print(json.dumps({{"id": task_id}}))
""",
        )
        runs_at_insert = client.ping()["write_canary"]["runs"]
        deadline = time.monotonic() + 10
        health = None
        while time.monotonic() < deadline:
            health = client.ping()
            alarm = health.get("write_canary_alarm") or {}
            saw_post_insert_run = health["write_canary"]["runs"] > runs_at_insert
            # Concurrent run that already observed the collision is also fine.
            saw_collision = (
                health["write_canary_ok"] is False
                and alarm.get("kind") == "write-canary-identity-collision"
                and alarm.get("task_id") == collision["id"]
            )
            if saw_collision and (
                saw_post_insert_run or health["write_canary"]["runs"] >= 1
            ):
                break
            time.sleep(0.05)

        assert health is not None
        assert health["write_canary_ok"] is False
        assert health["write_canary_alarm"]["kind"] == (
            "write-canary-identity-collision"
        )
        assert health["write_canary_alarm"]["task_id"] == collision["id"]
        assert client.get_task(collision["id"])["status"] == "blocked"


def test_guarded_archive_rejects_identity_changed_by_prior_broker_write(tmp_path: Path):
    with running_boardd(tmp_path, import_schema=True) as (_, db_path, socket_path):
        result = _run_kanban_db_script(
            db_path,
            socket_path,
            """
import json
from hermes_cli import kanban_db as kb
with kb.connect() as conn:
    task_id = kb.create_task(
        conn,
        title="guarded archive probe",
        body="before",
        created_by="guard-test",
        initial_status="blocked",
        idempotency_key="guard-test-key",
    )
    expected = {
        "title": "guarded archive probe",
        "body": "before",
        "created_by": "guard-test",
        "idempotency_key": "guard-test-key",
    }
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("after", task_id))
    archived = kb.archive_task(
        conn,
        task_id,
        expected_identity=expected,
        recompute_dependents=False,
    )
    linked = kb.create_task(
        conn,
        title="guarded linked probe",
        body="linked",
        created_by="guard-test",
        initial_status="blocked",
        idempotency_key="guard-link-key",
    )
    child = kb.create_task(conn, title="guard child", initial_status="blocked")
    kb.link_tasks(conn, linked, child)
    linked_expected = {
        "title": "guarded linked probe",
        "body": "linked",
        "created_by": "guard-test",
        "idempotency_key": "guard-link-key",
    }
    linked_archived = kb.archive_task(
        conn,
        linked,
        expected_identity=linked_expected,
        recompute_dependents=False,
    )
    task = kb.get_task(conn, task_id)
    linked_task = kb.get_task(conn, linked)
    print(json.dumps({
        "archived": archived,
        "status": task.status,
        "body": task.body,
        "linked_archived": linked_archived,
        "linked_status": linked_task.status,
    }))
""",
        )

    assert result == {
        "archived": False,
        "status": "blocked",
        "body": "after",
        "linked_archived": False,
        "linked_status": "blocked",
    }


def test_write_canary_total_deadline_bounds_slow_transaction_holder(tmp_path: Path):
    with running_boardd(
        tmp_path,
        import_schema=True,
        write_canary_mode="once",
        write_canary_start_delay=0.3,
        write_canary_timeout=0.15,
        env_overrides={
            "BOARDD_TXN_DEADLINE_S": "2",
            "BOARDD_TXN_MAX_S": "0.8",
        },
    ) as (client, _, _):
        token = client.txn_begin()
        assert token
        time.sleep(1)
        health = client.ping()

        alarm = health["write_canary_alarm"]
        assert health["write_canary_ok"] is False
        assert health["txn_capped"] >= 1
        assert alarm["kind"] == "write-canary-timeout"
        assert alarm["phase"] == "reconcile-discovery"
        assert alarm["duration_ms"] < 750
        assert "after 1 attempts" in alarm["detail"]


def test_shutdown_waits_for_canary_before_stopping_server(tmp_path: Path):
    broker = _unit_broker(tmp_path)
    order: list[str] = []

    class _Server:
        def shutdown(self) -> None:
            order.append("server-stopped")

    def finish_canary() -> None:
        order.append("canary-started")
        time.sleep(0.05)
        order.append("canary-finished")

    broker._server = _Server()
    broker._write_canary_thread_obj = threading.Thread(target=finish_canary)
    broker._write_canary_thread_obj.start()

    broker.shutdown()

    assert order == ["canary-started", "canary-finished", "server-stopped"]


def test_shutdown_is_idempotent_across_repeated_calls(tmp_path: Path):
    broker = _unit_broker(tmp_path)
    stops: list[str] = []

    class _Server:
        def shutdown(self) -> None:
            stops.append("server-stopped")

    broker._server = _Server()
    broker.shutdown()
    broker.shutdown()
    assert stops == ["server-stopped"]
    assert broker._shutdown_done.is_set()


def test_signal_shutdown_uses_coordinator_thread_not_serve_thread(tmp_path: Path):
    """Signal path must not invoke BaseServer.shutdown on the calling thread."""
    broker = _unit_broker(tmp_path)
    caller_ids: list[int] = []
    done = threading.Event()

    class _Server:
        def shutdown(self) -> None:
            caller_ids.append(threading.get_ident())
            done.set()

    broker._server = _Server()
    main_ident = threading.get_ident()
    first_coord = None
    broker._signal_shutdown(signal.SIGTERM, None)
    first_coord = broker._shutdown_coord_thread

    assert done.wait(timeout=5), "coordinator did not stop server"
    assert broker._shutdown_done.wait(timeout=5)
    assert first_coord is not None
    assert caller_ids == [first_coord.ident]
    assert caller_ids[0] != main_ident
    # Second signal is a no-op (single coordinator).
    broker._signal_shutdown(signal.SIGTERM, None)
    assert broker._shutdown_coord_thread is first_coord


def test_sigterm_subprocess_completes_ordered_teardown_without_sigkill(
    tmp_path: Path,
):
    """Real process: SIGTERM must finish canary->server->DB->unlink without SIGKILL."""
    db_path = tmp_path / "kanban.db"
    sock_path = tmp_path / "boardd.sock"
    command = [
        sys.executable,
        str(BOARDD),
        "--db",
        str(db_path),
        "--sock",
        str(sock_path),
        "--import-schema",
        "--log-level",
        "INFO",
        "--write-canary-mode",
        "once",
        "--write-canary-start-delay",
        "30",
        "--write-canary-timeout",
        "2",
    ]
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(tmp_path / "hermes-home"),
            "HERMES_KANBAN_BROKER": "0",
            "KB_CLIENT_RETRY_DEADLINE_S": "2",
            "BOARDD_DISKGUARD_MIN_FREE_BYTES": "0",
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    proc = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        client: Client | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                raise AssertionError(
                    f"boardd exited during startup (rc={proc.returncode})\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            if sock_path.exists():
                probe = Client(str(sock_path))
                try:
                    probe.ping()
                except Exception:
                    probe.close()
                else:
                    client = probe
                    break
            time.sleep(0.05)
        assert client is not None, "boardd did not become ready"
        client.close()

        # Deliver SIGTERM while serve_forever is on the main thread.
        proc.send_signal(signal.SIGTERM)

        # TimeoutStopSec is 45s in the unit; regression bound is far tighter.
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
            raise AssertionError(
                "SIGTERM_HUNG: boardd did not exit within 10s after SIGTERM; "
                "required SIGKILL\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )

        assert proc.returncode is not None
        # Clean coordinator path returns 0; never leave a hang that needs kill.
        assert proc.returncode == 0, (
            f"unexpected exit rc={proc.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
        assert not sock_path.exists(), "socket should be unlinked on SIGTERM teardown"
        # Ordered completion markers from the coordinator body.
        assert "boardd: shutdown requested" in stderr
        assert "boardd: write canary stopped" in stderr
        assert "boardd: server stopped" in stderr
        assert "boardd: DB thread stopped" in stderr
        assert "boardd: socket unlinked" in stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)