"""Broker failures must never become successful/empty dispatch ticks.

Real temporary Unix sockets exercise ENOENT, ECONNREFUSED, read timeout,
connection loss, and an empty SQLite-backed broker. No production board opens.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import socket
import sqlite3
import threading

import pytest


@pytest.fixture
def broker_env(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kb_client

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "fleet")
    path = kb.board_dir("fleet") / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    sock = tmp_path / "broker.sock"
    monkeypatch.setenv("BOARDD_SOCK", str(sock))
    monkeypatch.setenv("HERMES_KANBAN_BROKER", "1")
    monkeypatch.setenv("KB_CLIENT_RETRY_DEADLINE_S", "0")
    monkeypatch.setenv("KB_CLIENT_READ_TIMEOUT_S", "0.05")
    monkeypatch.setenv("KB_CLIENT_CONNECT_TIMEOUT_S", "0.05")
    monkeypatch.setattr(kb_client, "_RETRY_DEADLINE_S", 0)
    monkeypatch.setattr(kb_client, "_READ_TIMEOUT_S", 0.05)
    monkeypatch.setattr(kb_client, "_CONNECT_TIMEOUT_S", 0.05)
    client = kb_client.Client(str(sock))
    monkeypatch.setattr(kb_client._tl, "client", client, raising=False)
    yield path, sock
    client.close()


@contextlib.contextmanager
def server(sock_path, *, silent=False, disconnect=False, backend=None):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(5)
    listener.settimeout(0.1)
    stop = threading.Event()
    errors = []

    def serve():
        db = sqlite3.connect(backend, isolation_level=None) if backend else None
        if db:
            db.row_factory = sqlite3.Row
        try:
            while not stop.is_set():
                try:
                    peer, _ = listener.accept()
                except socket.timeout:
                    continue
                with peer:
                    if disconnect:
                        # Close only after the request was sent: deterministic
                        # EOF/ECONNRESET rather than a send-side EPIPE race.
                        with peer.makefile("rb") as reader:
                            reader.readline()
                        continue
                    if silent:
                        stop.wait(1)
                        continue
                    with peer.makefile("rb") as reader:
                        for raw in reader:
                            req = json.loads(raw)
                            args = req.get("args", {})
                            op = req["op"]
                            try:
                                if op == "txn_begin":
                                    db.execute("BEGIN IMMEDIATE")
                                    result = {"txn": "temporary-test-transaction"}
                                elif op in ("txn_commit", "txn_rollback"):
                                    db.execute("COMMIT" if op == "txn_commit" else "ROLLBACK")
                                    result = {}
                                else:
                                    cur = db.execute(args["sql"], args.get("params", []))
                                    rows = [dict(row) for row in cur.fetchall()]
                                    result = rows if op == "query" else {
                                        "rows": rows, "rowcount": cur.rowcount,
                                        "lastrowid": cur.lastrowid,
                                    }
                                response = {"ok": True, "result": result}
                            except sqlite3.Error as exc:
                                response = {"ok": False, "error": str(exc), "etype": type(exc).__name__}
                            peer.sendall((json.dumps(response) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            pass  # client failure deliberately closes the test connection
        except Exception as exc:
            errors.append(exc)
        finally:
            if db:
                db.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(2)
        listener.close()
        assert not thread.is_alive()
        assert not errors, errors


@pytest.mark.parametrize("failure,expected_errno", [("missing", 2), ("refused", 111), ("timeout", 110), ("closed", 104)])
def test_dispatch_cli_broker_failure_is_nonzero_not_empty(broker_env, failure, expected_errno, capsys, caplog):
    from hermes_cli import kanban

    path, sock = broker_env
    if failure == "refused":
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(sock))
        stale.close()
    context = server(sock, silent=failure == "timeout", disconnect=failure == "closed") if failure in ("timeout", "closed") else contextlib.nullcontext()
    with context:
        rc = kanban.kanban_command(argparse.Namespace(
            kanban_action="dispatch", board=None, dry_run=True, max=0, json=False,
        ))
    out = capsys.readouterr()
    assert rc != 0
    assert "Spawned:" not in out.out
    assert "BROKER_UNREACHABLE" in out.err + caplog.text
    assert str(sock) in out.err + caplog.text
    assert f"errno={expected_errno}" in out.err + caplog.text
    assert any(record.levelname == "ERROR" and "BROKER_UNREACHABLE" in record.message for record in caplog.records)
    assert not path.exists(), "a failed broker tick must not create a fallback local DB"


def test_broker_healthy_empty_is_success_without_local_db(broker_env, tmp_path, capsys):
    from hermes_cli import kanban
    from hermes_cli import kanban_db as kb
    from hermes_cli import kb_client

    path, sock = broker_env
    backend = tmp_path / "backend.sqlite"
    with sqlite3.connect(backend) as db:
        db.executescript(kb.SCHEMA_SQL)
    with server(sock, backend=backend):
        rc = kanban.kanban_command(argparse.Namespace(
            kanban_action="dispatch", board=None, dry_run=True, max=0, json=False,
        ))
        kb_client.get_client().close()
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert "Spawned:      0" in out.out
    assert "BROKER_UNREACHABLE" not in out.err
    assert not path.exists(), "a healthy broker must not be bypassed with a local SQLite DB"


def test_existing_broker_connection_cannot_skip_unreachable_tick(broker_env, caplog):
    from hermes_cli import boardd_shim as shim
    from hermes_cli import kanban_db_dispatch as dispatch

    with pytest.raises(RuntimeError, match="BROKER_UNREACHABLE"):
        dispatch.dispatch_once(shim.BrokerConnection(), dry_run=True, max_spawn=0)
    assert "BROKER_UNREACHABLE" in caplog.text


def test_nonfleet_local_board_stays_independent(broker_env, tmp_path):
    from hermes_cli import kanban_db_connect as connect

    local = tmp_path / "independent.sqlite"
    with connect.connect_closing(local) as db:
        assert isinstance(db, sqlite3.Connection)
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("healthy", [False, True])
def test_real_cli_process_preserves_broker_exit_status(broker_env, tmp_path, healthy):
    import subprocess
    import sys
    from hermes_cli import kanban_db as kb

    path, sock = broker_env
    backend = tmp_path / "backend.sqlite"
    if healthy:
        with sqlite3.connect(backend) as db:
            db.executescript(kb.SCHEMA_SQL)
    context = server(sock, backend=backend) if healthy else contextlib.nullcontext()
    with context:
        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "kanban", "dispatch", "--dry-run", "--max", "0"],
            text=True, capture_output=True, timeout=15,
        )
    if healthy:
        assert result.returncode == 0, result.stderr
        assert "Spawned:      0" in result.stdout
        assert "BROKER_UNREACHABLE" not in result.stderr
    else:
        assert result.returncode != 0, result.stdout
        assert "BROKER_UNREACHABLE" in result.stderr
        assert "Spawned:" not in result.stdout
    assert not path.exists()


def test_transport_failure_cannot_be_swallowed_then_reported_as_empty(broker_env, tmp_path):
    from hermes_cli import boardd_shim as shim
    from hermes_cli import kanban_db as kb
    from hermes_cli import kb_client
    path, sock = broker_env
    conn = shim.BrokerConnection()
    with pytest.raises(kb_client.BoarddUnavailable):
        conn.execute("SELECT COUNT(*) FROM tasks")
    backend = tmp_path / "backend.sqlite"
    with sqlite3.connect(backend) as db:
        db.executescript(kb.SCHEMA_SQL)
    # Even if transport recovers, a swallowed failure earlier in THIS tick
    # must remain a failed tick; the next tick gets a new connection.
    with server(sock, backend=backend):
        with pytest.raises(kb_client.BoarddUnavailable, match="BROKER_UNREACHABLE"):
            conn.check_broker_health()
    assert not path.exists()


@pytest.mark.parametrize("entrypoint", ["tick", "ready", "decompose"])
def test_gateway_broker_failure_is_not_success_or_idle(broker_env, monkeypatch, entrypoint, caplog):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kb_client
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _DispatcherSettings
    settings = _DispatcherSettings(60, 0, 1, 6, 0, False, None, None)
    dispatcher = _KanbanDispatcher(kb, settings)
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["fleet"])
    with pytest.raises(kb_client.BoarddUnavailable, match="BROKER_UNREACHABLE"):
        if entrypoint == "tick":
            dispatcher.tick_once_for_board("fleet")
        elif entrypoint == "ready":
            dispatcher.ready_nonempty()
        else:
            dispatcher.auto_decompose_tick(1)
    assert "BROKER_UNREACHABLE" in caplog.text
    assert dispatcher.disabled_corrupt_boards == {}


def test_gateway_healthy_empty_tick_remains_success(broker_env, tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kb_client
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _DispatcherSettings
    path, sock = broker_env
    backend = tmp_path / "backend.sqlite"
    with sqlite3.connect(backend) as db:
        db.executescript(kb.SCHEMA_SQL)
    dispatcher = _KanbanDispatcher(kb, _DispatcherSettings(60, 0, 1, 6, 0, False, None, None))
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["fleet"])
    with server(sock, backend=backend):
        result = dispatcher.tick_once_for_board("fleet")
        assert isinstance(result, kb.DispatchResult)
        assert result.spawned == []
        assert dispatcher.ready_nonempty() is False
        kb_client.get_client().close()
    assert not path.exists()
