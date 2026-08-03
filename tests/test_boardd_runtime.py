from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
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
BoarddError = kb_client.BoarddError
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
