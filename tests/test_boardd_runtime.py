from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
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


def _repo_schema_sql() -> str:
    from hermes_cli import kanban_db

    return kanban_db.SCHEMA_SQL


def _service_exec_start_command(
    db_path: Path,
    sock_path: Path,
    *,
    unit_text: str | None = None,
) -> list[str]:
    """Map the checked-in production ExecStart contract onto disposable paths."""
    if unit_text is None:
        unit_text = SERVICE.read_text(encoding="utf-8")
    exec_starts = [
        line.removeprefix("ExecStart=")
        for line in unit_text.splitlines()
        if line.startswith("ExecStart=")
    ]
    assert len(exec_starts) == 1, "boardd.service must declare exactly one ExecStart"
    command = shlex.split(exec_starts[0])
    assert command[:2] == [
        "/opt/hermes-boardd/current/venv/bin/python",
        "/opt/hermes-boardd/current/libexec/boardd.py",
    ]
    assert command.count("--import-schema") == 1, (
        "boardd.service ExecStart must explicitly apply pending additive migrations"
    )
    assert "--schema-sql-file" not in command
    assert command.count("--db") == 1
    assert command.count("--sock") == 1

    command[0] = sys.executable
    command[1] = str(BOARDD)
    command[command.index("--db") + 1] = str(db_path)
    command[command.index("--sock") + 1] = str(sock_path)
    return command


@contextmanager
def running_boardd(
    tmp_path: Path,
    *,
    schema_file: Path | None = None,
    import_schema: bool = False,
    service_contract: bool = False,
):
    db_path = tmp_path / "kanban.db"
    sock_path = tmp_path / "boardd.sock"
    if service_contract:
        assert schema_file is None
        assert not import_schema
        command = _service_exec_start_command(db_path, sock_path)
    else:
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
    env.update({
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "HERMES_KANBAN_BROKER": "0",
        "KB_CLIENT_RETRY_DEADLINE_S": "2",
        "PYTHONPATH": str(REPO_ROOT),
    })
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
    code = (
        """
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
"""
        % reasoning_effort
    )
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(REPO_ROOT),
        "HERMES_HOME": str(hermes_home),
        "HERMES_KANBAN_BROKER": "1",
        "HERMES_KANBAN_DB": str(db_path),
        "BOARDD_SOCK": str(socket_path),
    })
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


def test_service_restart_imports_pending_schema_and_preserves_board(tmp_path: Path):
    legacy_task_id = "t_legacy_restart"
    legacy_schema = _repo_schema_sql().replace(
        "    reasoning_effort     TEXT,\n",
        "",
    )
    assert "    reasoning_effort     TEXT,\n" not in legacy_schema
    legacy_schema += f"""
INSERT INTO tasks (id, title, assignee, status, created_by, created_at)
VALUES ('{legacy_task_id}', 'pre-migration task', 'engineer', 'todo', 'test', 1);
"""
    legacy_schema_path = tmp_path / "legacy-schema.sql"
    legacy_schema_path.write_text(legacy_schema, encoding="utf-8")

    # Negative control: the broker creates and reads a genuinely stale board
    # before the production service command is allowed to run its migration.
    with running_boardd(tmp_path, schema_file=legacy_schema_path) as (client, _, _):
        columns = {row["name"] for row in client.query("PRAGMA table_info(tasks)")}
        assert "reasoning_effort" not in columns
        stale_schema_version = client.query("PRAGMA schema_version")[0][
            "schema_version"
        ]
        assert client.get_task(legacy_task_id)["title"] == "pre-migration task"
        assert client.integrity_check() == ["ok"]

    # Contract negative control: dropping --import-schema from the checked-in
    # unit must make the harness fail before it can produce a false-green test.
    service_text = SERVICE.read_text(encoding="utf-8")
    without_import = service_text.replace(" --import-schema", "", 1)
    with pytest.raises(AssertionError, match="explicitly apply pending"):
        _service_exec_start_command(
            tmp_path / "negative.db",
            tmp_path / "negative.sock",
            unit_text=without_import,
        )

    # This command is parsed from boardd.service's ExecStart and only remaps
    # the interpreter, source, DB, and socket to this non-root temporary tree.
    with running_boardd(tmp_path, service_contract=True) as (client, _, _):
        columns = {row["name"] for row in client.query("PRAGMA table_info(tasks)")}
        assert "reasoning_effort" in columns
        migrated_schema_version = client.query("PRAGMA schema_version")[0][
            "schema_version"
        ]
        assert migrated_schema_version > stale_schema_version
        legacy = client.get_task(legacy_task_id)
        assert legacy["title"] == "pre-migration task"
        assert legacy["reasoning_effort"] is None
        task_id = client.create_task(
            title="legacy migration probe",
            assignee="engineer",
            status="running",
            reasoning_effort="medium",
        )["id"]
        assert client.get_task(task_id)["reasoning_effort"] == "medium"
        listed = {row["id"]: row for row in client.list_tasks(assignee="engineer")}
        assert {legacy_task_id, task_id} <= listed.keys()
        assert client.integrity_check() == ["ok"]

    # A second production-contract start on the same persistent DB is the
    # controlled restart regression: migration is idempotent and data survives.
    with running_boardd(tmp_path, service_contract=True) as (client, _, _):
        assert client.ping()["pid"] > 0
        assert client.get_task(legacy_task_id)["title"] == "pre-migration task"
        assert client.get_task(task_id)["reasoning_effort"] == "medium"
        restarted_schema_version = client.query("PRAGMA schema_version")[0][
            "schema_version"
        ]
        assert restarted_schema_version >= migrated_schema_version
        assert client.integrity_check() == ["ok"]


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
