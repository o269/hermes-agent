"""Tests for the fail-closed multi-DB session schema migration tool."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_state import SCHEMA_SQL, SCHEMA_VERSION, SessionDB


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "migrate_session_archive_schema.py"
)
SPEC = importlib.util.spec_from_file_location(
    "migrate_session_archive_schema", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)


def _clean_lsof(*args, **kwargs):
    del args, kwargs
    return subprocess.CompletedProcess(["lsof"], 1, stdout="", stderr="")


def _holder_lsof(*args, **kwargs):
    del args, kwargs
    return subprocess.CompletedProcess(
        ["lsof"],
        0,
        stdout="p123\nf9\nn/tmp/held-state.db\n",
        stderr="",
    )


def _create_real_shaped_db(
    path: Path,
    *,
    include_pinned: bool,
    include_activity: bool,
    activity_complete: bool = False,
) -> None:
    schema = SCHEMA_SQL
    if not include_pinned:
        schema = schema.replace("    pinned BOOLEAN NOT NULL DEFAULT 0,\n", "")
    if not include_activity:
        schema = schema.replace("    last_activity_at REAL,\n", "")

    conn = sqlite3.connect(path)
    conn.executescript(schema)
    session_columns = ["id", "started_at", "source"]
    if include_pinned:
        session_columns.append("pinned")
    if include_activity:
        session_columns.append("last_activity_at")
    placeholders = ", ".join("?" for _ in session_columns)
    insert_sql = (
        f"INSERT INTO sessions ({', '.join(session_columns)}) VALUES ({placeholders})"
    )

    rows = []
    for session_id, started_at, pinned, activity in (
        ("message-session", 9_999.0, 1, 300.0 if activity_complete else None),
        ("forward-session", 8_888.0, 0, 900.0),
        ("empty-session", 7_777.0, 0, None),
    ):
        row = [session_id, started_at, "cli"]
        if include_pinned:
            row.append(pinned)
        if include_activity:
            row.append(activity)
        rows.append(tuple(row))
    conn.executemany(insert_sql, rows)
    conn.executemany(
        """INSERT INTO messages (id, session_id, role, content, timestamp)
           VALUES (?, ?, 'user', 'content', ?)""",
        [
            (1, "message-session", 100.0),
            (2, "message-session", 300.0),
            (3, "forward-session", 500.0),
        ],
    )
    conn.commit()
    conn.close()


def _policy_rows(path: Path) -> dict[str, tuple[int, float | None]]:
    conn = sqlite3.connect(path)
    try:
        return {
            str(row[0]): (int(row[1]), None if row[2] is None else float(row[2]))
            for row in conn.execute(
                "SELECT id, pinned, last_activity_at FROM sessions ORDER BY id"
            ).fetchall()
        }
    finally:
        conn.close()


def _set_schema_version(path: Path, version: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
    finally:
        conn.close()


def test_sessiondb_v23_reconciles_and_tracks_real_message_activity(tmp_path):
    db_path = tmp_path / "legacy-runtime-state.db"
    _create_real_shaped_db(db_path, include_pinned=False, include_activity=False)
    _set_schema_version(db_path, SCHEMA_VERSION - 1)

    db = SessionDB(db_path=db_path)
    try:
        assert db._conn is not None
        columns = {
            str(row[1])
            for row in db._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        assert {"pinned", "last_activity_at"} <= columns
        assert (
            db._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert _policy_rows(db_path) == {
            "empty-session": (0, None),
            "forward-session": (0, 500.0),
            "message-session": (0, 300.0),
        }

        # An imported/late older message cannot move the durable watermark back.
        db.append_message("forward-session", "user", "older", timestamp=400.0)
        assert _policy_rows(db_path)["forward-session"] == (0, 500.0)
        db.append_message("forward-session", "user", "newer", timestamp=700.0)
        assert _policy_rows(db_path)["forward-session"] == (0, 700.0)
    finally:
        db.close()


def test_sessiondb_v23_preserves_pins_and_newer_durable_activity(tmp_path):
    db_path = tmp_path / "partially-reconciled-runtime-state.db"
    _create_real_shaped_db(db_path, include_pinned=True, include_activity=True)
    _set_schema_version(db_path, SCHEMA_VERSION - 1)

    db = SessionDB(db_path=db_path)
    try:
        assert _policy_rows(db_path) == {
            "empty-session": (0, None),
            "forward-session": (0, 900.0),
            "message-session": (1, 300.0),
        }
    finally:
        db.close()


def test_mixed_schema_apply_alias_dedupe_receipts_and_replay_noop(tmp_path):
    pinned_only = tmp_path / "pinned-only-state.db"
    both_missing = tmp_path / "both-missing-state.db"
    null_activity = tmp_path / "null-activity-state.db"
    populated = tmp_path / "populated-state.db"
    alias = tmp_path / "alias-of-pinned-only.db"
    _create_real_shaped_db(pinned_only, include_pinned=True, include_activity=False)
    _create_real_shaped_db(both_missing, include_pinned=False, include_activity=False)
    _create_real_shaped_db(null_activity, include_pinned=True, include_activity=True)
    _create_real_shaped_db(
        populated,
        include_pinned=True,
        include_activity=True,
        activity_complete=True,
    )
    os.link(pinned_only, alias)

    targets = migration.deduplicate_targets([
        pinned_only,
        alias,
        both_missing,
        null_activity,
        populated,
    ])
    plan = migration.build_plan(targets)
    assert plan["input_paths"] == 5
    assert plan["unique_databases"] == 4
    assert plan["databases_needing_apply"] == 3
    assert any(len(target.aliases) == 2 for target in targets)

    receipt = migration.apply_plan(
        targets,
        plan,
        backup_suffix=".bak-test",
        lsof_runner=_clean_lsof,
    )
    assert receipt["status"] == "applied"
    assert receipt["lsof_zero_checks"] == 2
    assert len(receipt["backups"]) == 4
    assert len(receipt["databases"]) == 4
    assert {item["status"] for item in receipt["databases"]} == {
        "applied",
        "unchanged",
    }
    assert all(item["byte_exact"] for item in receipt["backups"])
    assert all(Path(item["backup_path"]).is_file() for item in receipt["backups"])
    assert all(item["backup"]["byte_exact"] for item in receipt["databases"])
    assert all(item["integrity_check"] == "ok" for item in receipt["databases"])
    assert all(item["quick_check"] == "ok" for item in receipt["databases"])
    assert all(item["foreign_key_check_rows"] == 0 for item in receipt["databases"])

    assert _policy_rows(pinned_only) == {
        "empty-session": (0, None),
        "forward-session": (0, 500.0),
        "message-session": (1, 300.0),
    }
    assert _policy_rows(alias) == _policy_rows(pinned_only)
    assert _policy_rows(null_activity)["message-session"] == (1, 300.0)
    assert _policy_rows(null_activity)["forward-session"] == (0, 900.0)
    # A schema that had no durable pin column receives false, never a guessed pin.
    assert _policy_rows(both_missing)["message-session"] == (0, 300.0)
    # Explicit zero-message rule: leave activity NULL/fail-closed.
    assert _policy_rows(both_missing)["empty-session"] == (0, None)

    before_replay = {
        target.identity: migration.sha256_file(target.canonical_path)
        for target in targets
    }
    replay_plan = migration.build_plan(targets)
    assert replay_plan["databases_needing_apply"] == 0
    replay_receipt = migration.apply_plan(
        targets,
        replay_plan,
        backup_suffix=".bak-replay",
        lsof_runner=_clean_lsof,
    )
    assert replay_receipt["status"] == "noop"
    assert replay_receipt["lsof_zero_checks"] == 2
    assert len(replay_receipt["backups"]) == 4
    assert len(replay_receipt["databases"]) == 4
    assert {item["status"] for item in replay_receipt["databases"]} == {"unchanged"}
    assert all(item["integrity_check"] == "ok" for item in replay_receipt["databases"])
    assert {
        target.identity: migration.sha256_file(target.canonical_path)
        for target in targets
    } == before_replay


def test_failure_rolls_back_to_byte_exact_backup(tmp_path):
    db_path = tmp_path / "rollback-state.db"
    _create_real_shaped_db(db_path, include_pinned=False, include_activity=False)
    original_hash = migration.sha256_file(db_path)
    targets = migration.deduplicate_targets([db_path])
    plan = migration.build_plan(targets)

    def fail_after_backfill(stage, target, conn):
        del target, conn
        if stage == "after_backfill":
            raise RuntimeError("injected post-backfill failure")

    with pytest.raises(migration.MigrationApplyError) as exc_info:
        migration.apply_plan(
            targets,
            plan,
            backup_suffix=".bak-rollback",
            lsof_runner=_clean_lsof,
            failpoint=fail_after_backfill,
        )

    receipt = exc_info.value.receipt
    assert receipt["status"] == "rolled_back"
    assert receipt["rollback_error"] is None
    assert receipt["restored"][0]["matches_backup"] is True
    assert len(receipt["databases"]) == 1
    assert receipt["databases"][0]["status"] == "restored"
    assert migration.sha256_file(db_path) == original_hash
    assert (
        migration.sha256_file(Path(receipt["backups"][0]["backup_path"]))
        == original_hash
    )
    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        assert "pinned" not in columns
        assert "last_activity_at" not in columns
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_live_holder_aborts_before_backup_or_mutation(tmp_path):
    db_path = tmp_path / "held-state.db"
    _create_real_shaped_db(db_path, include_pinned=True, include_activity=False)
    original_hash = migration.sha256_file(db_path)
    targets = migration.deduplicate_targets([db_path])
    plan = migration.build_plan(targets)

    with pytest.raises(migration.MigrationError, match="live holder detected"):
        migration.apply_plan(
            targets,
            plan,
            backup_suffix=".bak-holder",
            lsof_runner=_holder_lsof,
        )

    assert migration.sha256_file(db_path) == original_hash
    assert not Path(str(db_path) + ".bak-holder").exists()


def test_lsof_zero_holder_command_suppresses_stock_warnings(tmp_path):
    db_path = tmp_path / "lsof-warning-state.db"
    _create_real_shaped_db(db_path, include_pinned=True, include_activity=False)
    targets = migration.deduplicate_targets([db_path])
    commands = []

    def warning_sensitive_lsof(command, **kwargs):
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        commands.append(command)
        warning = "" if "-w" in command else "lsof: WARNING: can't stat() tracefs"
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=warning)

    migration.assert_no_lsof_holders(targets, runner=warning_sensitive_lsof)

    assert commands == [["lsof", "-w", "-F", "pfn", "--", str(db_path)]]


def test_lsof_error_output_is_not_mistaken_for_zero_holders(tmp_path):
    db_path = tmp_path / "lsof-error-state.db"
    _create_real_shaped_db(db_path, include_pinned=True, include_activity=False)
    original_hash = migration.sha256_file(db_path)
    targets = migration.deduplicate_targets([db_path])
    plan = migration.build_plan(targets)

    def broken_lsof(*args, **kwargs):
        del args, kwargs
        return subprocess.CompletedProcess(
            ["lsof"], 1, stdout="", stderr="lsof: cannot stat target"
        )

    with pytest.raises(migration.MigrationError, match="lsof failed"):
        migration.apply_plan(
            targets,
            plan,
            backup_suffix=".bak-lsof-error",
            lsof_runner=broken_lsof,
        )
    assert migration.sha256_file(db_path) == original_hash
    assert not Path(str(db_path) + ".bak-lsof-error").exists()


def test_preexisting_sidecar_is_preserved_and_aborts_apply(tmp_path):
    db_path = tmp_path / "sidecar-state.db"
    _create_real_shaped_db(db_path, include_pinned=True, include_activity=False)
    targets = migration.deduplicate_targets([db_path])
    plan = migration.build_plan(targets)
    sidecar = Path(str(db_path) + "-shm")
    sidecar.write_bytes(b"operator-owned-sidecar")

    with pytest.raises(migration.MigrationError, match="sidecar must be absent"):
        migration.apply_plan(
            targets,
            plan,
            backup_suffix=".bak-sidecar",
            lsof_runner=_clean_lsof,
        )
    assert sidecar.read_bytes() == b"operator-owned-sidecar"
    assert not Path(str(db_path) + ".bak-sidecar").exists()


def test_reserved_sqlite_sidecar_backup_suffix_is_rejected(tmp_path):
    db_path = tmp_path / "suffix-state.db"
    _create_real_shaped_db(db_path, include_pinned=True, include_activity=False)
    target = migration.deduplicate_targets([db_path])[0]
    with pytest.raises(migration.MigrationError, match="must start with .bak-"):
        migration._backup_path(target, "-shm")


def test_stale_noop_plan_is_refreshed_under_lsof_gates(tmp_path):
    db_path = tmp_path / "stale-plan-state.db"
    _create_real_shaped_db(
        db_path,
        include_pinned=True,
        include_activity=True,
        activity_complete=True,
    )
    targets = migration.deduplicate_targets([db_path])
    stale_plan = migration.build_plan(targets)
    assert stale_plan["databases_needing_apply"] == 0
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE sessions SET last_activity_at = NULL WHERE id = 'message-session'"
    )
    conn.commit()
    conn.close()

    receipt = migration.apply_plan(
        targets,
        stale_plan,
        backup_suffix=".bak-stale-plan",
        lsof_runner=_clean_lsof,
    )
    assert receipt["status"] == "applied"
    assert receipt["lsof_zero_checks"] == 2
    assert receipt["databases_needing_apply"] == 1
    assert _policy_rows(db_path)["message-session"] == (1, 300.0)


def test_late_multi_db_failure_restores_every_target(tmp_path):
    first = tmp_path / "a-first-state.db"
    second = tmp_path / "b-second-state.db"
    for path in (first, second):
        _create_real_shaped_db(path, include_pinned=False, include_activity=False)
    targets = migration.deduplicate_targets([first, second])
    plan = migration.build_plan(targets)
    original_hashes = {
        target.identity: migration.sha256_file(target.canonical_path)
        for target in targets
    }
    calls = 0

    def fail_on_second_target(stage, target, conn):
        nonlocal calls
        del target, conn
        if stage == "after_backfill":
            calls += 1
            if calls == 2:
                raise RuntimeError("injected second-target failure")

    with pytest.raises(migration.MigrationApplyError) as exc_info:
        migration.apply_plan(
            targets,
            plan,
            backup_suffix=".bak-multi-rollback",
            lsof_runner=_clean_lsof,
            failpoint=fail_on_second_target,
        )

    receipt = exc_info.value.receipt
    assert receipt["status"] == "rolled_back"
    assert receipt["rollback_error"] is None
    assert len(receipt["restored"]) == 2
    assert len(receipt["databases"]) == 2
    assert {item["status"] for item in receipt["databases"]} == {"restored"}
    assert {
        target.identity: migration.sha256_file(target.canonical_path)
        for target in targets
    } == original_hashes


def test_drifted_backup_is_validated_before_target_overwrite(tmp_path):
    db_path = tmp_path / "backup-drift-state.db"
    _create_real_shaped_db(db_path, include_pinned=True, include_activity=False)
    target = migration.deduplicate_targets([db_path])[0]
    backups = migration.create_backups([target], ".bak-drift")
    target_hash = migration.sha256_file(db_path)
    backups[0].backup_path.write_bytes(b"corrupt")

    restored, errors = migration.restore_backups(backups)
    assert restored == []
    assert errors and "backup drifted" in errors[0]
    assert migration.sha256_file(db_path) == target_hash


def test_rollback_preserves_unproven_sidecar_and_refuses_overwrite(tmp_path):
    db_path = tmp_path / "raced-sidecar-state.db"
    _create_real_shaped_db(db_path, include_pinned=False, include_activity=False)
    original_hash = migration.sha256_file(db_path)
    targets = migration.deduplicate_targets([db_path])
    plan = migration.build_plan(targets)
    sidecar = Path(str(db_path) + "-shm")

    def fail_after_creating_unproven_sidecar(stage, target, conn):
        del conn
        if stage == "after_backfill":
            sidecar.write_bytes(b"unproven-concurrent-writer-state")
            raise RuntimeError(f"injected failure for {target.canonical_path}")

    with pytest.raises(migration.MigrationApplyError) as exc_info:
        migration.apply_plan(
            targets,
            plan,
            backup_suffix=".bak-raced-sidecar",
            lsof_runner=_clean_lsof,
            failpoint=fail_after_creating_unproven_sidecar,
        )

    receipt = exc_info.value.receipt
    assert receipt["status"] == "rollback_failed"
    assert "rollback preflight refused" in receipt["rollback_error"]
    assert len(receipt["databases"]) == 1
    assert receipt["databases"][0]["status"] == "rollback_failed"
    assert sidecar.read_bytes() == b"unproven-concurrent-writer-state"
    # The per-DB SQL transaction rolled back before the outer restore path.
    assert migration.sha256_file(db_path) == original_hash


def test_existing_receipt_aborts_cli_before_apply(tmp_path):
    db_path = tmp_path / "receipt-state.db"
    manifest = tmp_path / "manifest.json"
    receipt = tmp_path / "receipt.json"
    _create_real_shaped_db(db_path, include_pinned=True, include_activity=False)
    original_hash = migration.sha256_file(db_path)
    receipt.write_text("do-not-overwrite", encoding="utf-8")

    with pytest.raises(migration.MigrationError, match="refusing to overwrite receipt"):
        migration.main([
            "--db",
            str(db_path),
            "--apply",
            "--backup-suffix",
            ".bak-receipt",
            "--manifest",
            str(manifest),
            "--receipt",
            str(receipt),
        ])
    assert migration.sha256_file(db_path) == original_hash
    assert receipt.read_text(encoding="utf-8") == "do-not-overwrite"
    assert not Path(str(db_path) + ".bak-receipt").exists()


def test_evidence_output_cannot_collide_with_sqlite_sidecar(tmp_path):
    db_path = tmp_path / "protected-sidecar-state.db"
    manifest = tmp_path / "manifest.json"
    sidecar_receipt = Path(str(db_path) + "-shm")
    _create_real_shaped_db(db_path, include_pinned=True, include_activity=False)
    original_hash = migration.sha256_file(db_path)

    with pytest.raises(migration.MigrationError, match="DB, sidecar, and backup"):
        migration.main([
            "--db",
            str(db_path),
            "--apply",
            "--backup-suffix",
            ".bak-sidecar-output",
            "--manifest",
            str(manifest),
            "--receipt",
            str(sidecar_receipt),
        ])

    assert migration.sha256_file(db_path) == original_hash
    assert not manifest.exists()
    assert not sidecar_receipt.exists()
    assert not Path(str(db_path) + ".bak-sidecar-output").exists()


def test_cli_apply_writes_immutable_receipt_and_replay_is_db_noop(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "cli-state.db"
    _create_real_shaped_db(db_path, include_pinned=True, include_activity=False)
    monkeypatch.setattr(
        migration,
        "assert_no_lsof_holders",
        lambda targets, runner=subprocess.run: None,
    )

    manifest = tmp_path / "manifest.json"
    receipt_path = tmp_path / "receipt.json"
    assert (
        migration.main([
            "--db",
            str(db_path),
            "--apply",
            "--backup-suffix",
            ".bak-cli-first",
            "--manifest",
            str(manifest),
            "--receipt",
            str(receipt_path),
        ])
        == 0
    )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "applied"
    assert receipt["databases"][0]["rows_backfilled"] == 2
    assert receipt["databases"][0]["rows_advanced"] == 0
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o400
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400
    first_final_hash = migration.sha256_file(db_path)

    replay_manifest = tmp_path / "replay-manifest.json"
    replay_receipt_path = tmp_path / "replay-receipt.json"
    assert (
        migration.main([
            "--db",
            str(db_path),
            "--apply",
            "--backup-suffix",
            ".bak-cli-replay",
            "--manifest",
            str(replay_manifest),
            "--receipt",
            str(replay_receipt_path),
        ])
        == 0
    )

    replay_receipt = json.loads(replay_receipt_path.read_text(encoding="utf-8"))
    assert replay_receipt["status"] == "noop"
    assert replay_receipt["databases"][0]["schema_changes"] == 0
    assert replay_receipt["databases"][0]["rows_backfilled"] == 0
    assert replay_receipt["databases"][0]["rows_advanced"] == 0
    assert migration.sha256_file(db_path) == first_final_hash
