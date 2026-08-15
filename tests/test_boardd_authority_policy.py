from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARDD = REPO_ROOT / "scripts" / "fleet" / "boardd.py"

from hermes_cli.kb_client import BoarddError, Client


@contextmanager
def running_boardd(tmp_path: Path):
    db_path = tmp_path / "kanban.db"
    sock_path = tmp_path / "boardd.sock"
    env = {
        **os.environ,
        "HERMES_HOME": str(tmp_path),
        "HERMES_KANBAN_BROKER": "0",
        "KB_CLIENT_RETRY_DEADLINE_S": "2",
        "BOARDD_DISKGUARD_MIN_FREE_BYTES": "0",
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            str(BOARDD),
            "--db",
            str(db_path),
            "--sock",
            str(sock_path),
            "--import-schema",
            "--log-level",
            "DEBUG",
            "--write-canary-mode",
            "disabled",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    client: Client | None = None
    deadline = time.monotonic() + 10
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
        raise AssertionError(f"boardd did not become ready\n{stdout}\n{stderr}")
    try:
        yield client
    finally:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def native_create(client: Client, **args) -> dict:
    return client._request("create_task", args, mutation=True)["result"]


@pytest.mark.live_system_guard_bypass  # terminates only running_boardd's tmp child
def test_boardd_authority_fence_covers_native_and_raw_writers(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        "kanban:\n  authority_profiles: fable\n",
        encoding="utf-8",
    )
    with running_boardd(tmp_path) as client:
        # Must-fire first: the pre-fence daemon accepts this native write.
        with pytest.raises(BoarddError, match="authority_executor_not_parked"):
            native_create(
                client,
                title="[AUTHOR] ordinary executor work",
                assignee="Fable",
                status="ready",
            )

        health = client.ping()
        assert health["assignment_policy_ready"] is True
        assert health["assignment_authority_profiles"] == ["fable"]

        created = native_create(
            client,
            title="[AUTHOR] executor patch",
            assignee="codex7",
            status="running",
        )
        with pytest.raises(BoarddError, match="kanban assignment policy violation"):
            client.exec_write(
                "UPDATE tasks SET assignee = ? WHERE id = ?",
                ["fable", created["id"]],
            )

        txn = client.txn_begin()
        try:
            with pytest.raises(BoarddError, match="kanban assignment policy violation"):
                client.txn_exec(
                    txn,
                    "UPDATE tasks SET assignee = ? WHERE id = ?",
                    ["fable", created["id"]],
                )
        finally:
            client.txn_rollback(txn)

        assert client.get_task(created["id"])["assignee"] == "codex7"


@pytest.mark.live_system_guard_bypass  # terminates only running_boardd's tmp child
def test_boardd_authority_fence_preserves_idempotent_parked_create(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        "kanban:\n  authority_profiles: fable\n",
        encoding="utf-8",
    )
    with running_boardd(tmp_path) as client:
        parked = native_create(
            client,
            title="[GATE] operator acceptance",
            body="Decision-only authority lane card.",
            assignee="Fable",
            status="blocked",
            idempotency_key="authority-retry-key",
        )
        retried = native_create(
            client,
            title="[AUTHOR] must not replace original",
            assignee="Fable",
            status="ready",
            idempotency_key="authority-retry-key",
        )
        assert retried == {
            "id": parked["id"],
            "status": "blocked",
            "deduplicated": True,
        }
