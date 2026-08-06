"""Private dashboard→boardd acceptance.

This module exists so dashboard E2E validation never needs to create disposable
cards on an installed board. It deliberately replaces every inherited kanban and
broker selector before starting either child process.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BOARDD = REPO_ROOT / "scripts" / "fleet" / "boardd.py"


def _load_repo_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kb_client = _load_repo_module(
    "dashboard_boardd_e2e_kb_client",
    REPO_ROOT / "hermes_cli" / "kb_client.py",
)
Client = kb_client.Client


@contextmanager
def _running_private_boardd(tmp_path: Path):
    """Start boardd on a pytest-owned DB and socket, never inherited paths."""
    db_path = tmp_path / "kanban.db"
    socket_path = tmp_path / "boardd.sock"
    hermes_home = tmp_path / "boardd-home"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "HERMES_HOME": str(hermes_home),
            "HERMES_KANBAN_HOME": str(hermes_home),
            "HERMES_KANBAN_BROKER": "0",
            "HERMES_KANBAN_BOARD": "fleet",
            "HERMES_KANBAN_DB": str(db_path),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(hermes_home / "workspaces"),
            "HERMES_KANBAN_ATTACHMENTS_ROOT": str(hermes_home / "attachments"),
            "BOARDD_SOCK": str(socket_path),
            "BOARDD_DISKGUARD_MIN_FREE_BYTES": "0",
            "KB_CLIENT_RETRY_DEADLINE_S": "2",
        }
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            str(BOARDD),
            "--db",
            str(db_path),
            "--sock",
            str(socket_path),
            "--import-schema",
            "--write-canary-mode",
            "disabled",
            "--log-level",
            "DEBUG",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    client = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"private boardd exited during startup (rc={proc.returncode})\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        if socket_path.exists():
            probe = Client(str(socket_path))
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
            f"private boardd did not become ready\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )

    try:
        yield client, db_path, socket_path
    finally:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _run_private_dashboard_api(db_path: Path, socket_path: Path) -> dict:
    """Create, complete, archive, and read a card through the real plugin API."""
    hermes_home = db_path.parent / "dashboard-home"
    fleet_dir = hermes_home / "kanban" / "boards" / "fleet"
    fleet_dir.mkdir(parents=True)
    (fleet_dir / "kanban.db").symlink_to(db_path)

    plugin_file = REPO_ROOT / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    code = """
import importlib.util
import json
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hermes_cli import kanban_db as kb

plugin_file = Path(%r)
spec = importlib.util.spec_from_file_location(
    "hermes_dashboard_plugin_private_boardd_e2e", plugin_file
)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

with kb.connect() as conn:
    connection_type = type(conn).__name__
assert connection_type == "BrokerConnection"

app = FastAPI()
app.include_router(plugin.router, prefix="/api/plugins/kanban")
client = TestClient(app)
created = client.post(
    "/api/plugins/kanban/tasks",
    json={
        "title": "[e2e-disposable] dashboard boardd acceptance",
        "body": "Private pytest namespace; must never reach an installed board.",
    },
)
assert created.status_code == 200, created.text
task_id = created.json()["task"]["id"]

completed = client.patch(
    f"/api/plugins/kanban/tasks/{task_id}",
    json={"status": "done", "summary": "isolated dashboard E2E"},
)
assert completed.status_code == 200, completed.text
archived = client.patch(
    f"/api/plugins/kanban/tasks/{task_id}",
    json={"status": "archived"},
)
assert archived.status_code == 200, archived.text
shown = client.get(f"/api/plugins/kanban/tasks/{task_id}")
assert shown.status_code == 200, shown.text
print(json.dumps({
    "connection_type": connection_type,
    "db_path": str(kb.kanban_db_path()),
    "task": shown.json()["task"],
}))
""" % str(plugin_file)

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "HERMES_HOME": str(hermes_home),
            "HERMES_KANBAN_HOME": str(hermes_home),
            "HERMES_KANBAN_BROKER": "1",
            "HERMES_KANBAN_BOARD": "fleet",
            "HERMES_KANBAN_DB": str(db_path),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(hermes_home / "workspaces"),
            "HERMES_KANBAN_ATTACHMENTS_ROOT": str(hermes_home / "attachments"),
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


def test_dashboard_boardd_e2e_uses_private_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A fleet-pinned caller cannot redirect this E2E to its installed board."""
    protected_home = tmp_path / "must-not-touch-home"
    protected_db = tmp_path / "must-not-touch-fleet.db"
    protected_socket = tmp_path / "must-not-touch-boardd.sock"
    monkeypatch.setenv("HERMES_HOME", str(protected_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(protected_home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "fleet")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(protected_db))
    monkeypatch.setenv("HERMES_KANBAN_BROKER", "1")
    monkeypatch.setenv("BOARDD_SOCK", str(protected_socket))

    with _running_private_boardd(tmp_path) as (broker, db_path, socket_path):
        receipt = _run_private_dashboard_api(db_path, socket_path)
        task = broker.get_task(receipt["task"]["id"])

        assert receipt["connection_type"] == "BrokerConnection"
        assert Path(receipt["db_path"]).resolve() == db_path.resolve()
        assert receipt["task"]["status"] == "archived"
        assert task["status"] == "archived"
        assert task["created_by"] == "dashboard"

    assert not protected_home.exists()
    assert not protected_db.exists()
    assert not protected_socket.exists()
