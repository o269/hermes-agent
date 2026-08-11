"""Regression coverage for boardd shim rebind idempotency.

The tests keep broker routing disabled and only open throwaway databases under
``tmp_path``.  A fresh reload of ``kanban_db`` restores its genuine connect
functions before each test, independent of prior in-process rebinds.
"""

import importlib

import pytest


@pytest.fixture
def fresh_shim_and_kdb(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BROKER", raising=False)

    shim = importlib.import_module("hermes_cli.boardd_shim")
    kdb = importlib.import_module("hermes_cli.kanban_db")
    kdb = importlib.reload(kdb)

    captured_names = (
        "_KDB",
        "_ORIG_CONNECT",
        "_ORIG_CONNECT_CLOSING",
        "_ORIG_SET_STATUS",
    )
    for name in captured_names:
        setattr(shim, name, None)
    try:
        yield shim, kdb
    finally:
        # install_rebind mutates module globals directly. Reload the module so
        # later tests cannot inherit broker delegates while the broker flag is
        # disabled, then clear every captured authority for the next fixture.
        importlib.reload(kdb)
        for name in captured_names:
            setattr(shim, name, None)


def test_double_install_preserves_genuine_original(fresh_shim_and_kdb):
    boardd_shim, kdb = fresh_shim_and_kdb
    genuine_set_status = kdb.set_status

    boardd_shim.install_rebind(kdb)
    boardd_shim.install_rebind(kdb)

    assert boardd_shim._ORIG_CONNECT is not boardd_shim.connect
    assert boardd_shim._ORIG_CONNECT.__module__ == "hermes_cli.kanban_db"
    assert boardd_shim._ORIG_SET_STATUS is genuine_set_status
    assert kdb.set_status is boardd_shim.set_status


def test_capture_after_rebind_does_not_poison(fresh_shim_and_kdb):
    boardd_shim, kdb = fresh_shim_and_kdb

    boardd_shim.install_rebind(kdb)
    genuine_original = boardd_shim._ORIG_CONNECT
    genuine_set_status = boardd_shim._ORIG_SET_STATUS
    boardd_shim._capture_original(kdb)

    assert boardd_shim._ORIG_CONNECT is genuine_original
    assert boardd_shim._ORIG_CONNECT is not boardd_shim.connect
    assert boardd_shim._ORIG_SET_STATUS is genuine_set_status
    assert boardd_shim._ORIG_SET_STATUS is not boardd_shim.set_status


def test_passthrough_open_no_recursion(fresh_shim_and_kdb, tmp_path):
    boardd_shim, kdb = fresh_shim_and_kdb

    boardd_shim.install_rebind(kdb)
    boardd_shim.install_rebind(kdb)

    db_file = tmp_path / "passthrough.db"
    conn = boardd_shim.connect(db_path=db_file)
    try:
        assert db_file.exists()
    finally:
        conn.close()
