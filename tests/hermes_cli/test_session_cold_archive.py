from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_state import SessionDB
import hermes_cli.session_cold_archive as cold
from hermes_cli.session_cold_archive import ColdArchiveError

NOW = 2_000_000.0
DAY = 86_400.0


def _make_session(
    db: SessionDB,
    session_id: str,
    *,
    days_ago: float,
    archived: bool = True,
    pinned: bool = False,
    ended: bool = True,
    parent_session_id: str | None = None,
    end_reason: str | None = "done",
    source: str = "cli",
    title: str | None = None,
    content: str | None = None,
    catalog_message_count: int = 999,
) -> None:
    db.create_session(
        session_id=session_id,
        source=source,
        parent_session_id=parent_session_id,
    )
    if title:
        db.set_session_title(session_id, title)
    if content is not None:
        db.append_message(session_id, "user", content)
    timestamp = NOW - days_ago * DAY
    db._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE session_id = ?",
        (timestamp, session_id),
    )
    db._conn.execute(
        """
        UPDATE sessions
        SET started_at = ?, ended_at = ?, end_reason = ?, archived = ?, pinned = ?,
            message_count = ?
        WHERE id = ?
        """,
        (
            timestamp - 10,
            timestamp if ended else None,
            end_reason if ended else None,
            1 if archived else 0,
            1 if pinned else 0,
            catalog_message_count,
            session_id,
        ),
    )
    db._conn.commit()


def _build_db(path: Path) -> SessionDB:
    db = SessionDB(db_path=path)
    assert db._conn is not None
    columns = {
        str(row[1]) for row in db._conn.execute('PRAGMA table_info("sessions")')
    }
    if "pinned" not in columns:
        db._conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        db._conn.commit()
    return db


def test_manifest_skips_32_parent_84_child_hazard(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        child_counter = 0
        for parent_index in range(32):
            parent_id = f"old-parent-{parent_index:02d}"
            _make_session(
                db,
                parent_id,
                days_ago=120,
                archived=True,
                ended=True,
                end_reason="compression",
                content=f"old parent witness {parent_index}",
            )
            child_total = 3 if parent_index < 20 else 2
            for child_index in range(child_total):
                if child_counter >= 84:
                    break
                _make_session(
                    db,
                    f"recent-child-{child_counter:02d}",
                    days_ago=2,
                    archived=False,
                    ended=False,
                    parent_session_id=parent_id,
                    content=f"recent child witness {child_counter}",
                )
                child_counter += 1
        assert child_counter == 84
    finally:
        db.close()

    manifest = cold.build_gate_b_manifest(db_path, now=NOW, hold_sources=[])

    assert manifest["counts"]["selected_sessions"] == 0
    assert manifest["counts"]["selected_messages_actual"] == 0
    assert manifest["skipped_group_reason_counts"]["open_session"] == 32
    assert manifest["skipped_group_reason_counts"]["not_archived"] == 32
    assert manifest["skipped_group_reason_counts"]["inside_30d_hot_plus_7d_grace"] == 32


def test_cold_archive_export_delete_preserves_survivors_and_actual_message_counts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_db(db_path)
    try:
        _make_session(
            db,
            "cold-root",
            days_ago=90,
            archived=True,
            ended=True,
            end_reason="compression",
            content="cold payload witness root SECRET_TOKEN=not-a-real-token-redaction-fixture",
            catalog_message_count=111,
        )
        _make_session(
            db,
            "cold-tip",
            days_ago=89,
            archived=True,
            ended=True,
            parent_session_id="cold-root",
            content="cold payload witness tip",
            catalog_message_count=222,
        )
        _make_session(
            db,
            "survivor-parent",
            days_ago=3,
            archived=False,
            ended=False,
            content="hot parent witness",
        )
        _make_session(
            db,
            "survivor-child",
            days_ago=2,
            archived=False,
            ended=False,
            parent_session_id="survivor-parent",
            content="hot child witness",
        )
    finally:
        db.close()

    receipt = cold.run_cold_archive_pass(
        source_db=db_path,
        stage_root=tmp_path / "stage",
        now=NOW,
        hold_sources=[],
        apply_retention=True,
    )

    assert receipt["retention"]["applied"] is True
    assert receipt["retention"]["deleted_sessions"] == 2
    assert receipt["retention"]["deleted_messages_actual"] == 2
    assert receipt["retention"]["survivor_search_invariants"]["messages"]["count"] == 2
    assert (
        receipt["retention"]["survivor_search_invariants"]["messages_fts"]["count"] == 2
    )
    assert receipt["qmd_export"]["message_count"] == 2
    qmd_paths = [Path(item["path"]) for item in receipt["qmd_export"]["exported_files"]]
    assert len(qmd_paths) == 1
    qmd_text = qmd_paths[0].read_text(encoding="utf-8")
    assert "cold payload witness root" in qmd_text
    assert "not-a-real-token-redaction-fixture" not in qmd_text
    assert Path(receipt["rollback_bundle"]["path"]).is_file()

    conn = cold._connect_readonly(db_path)
    try:
        remaining = {
            str(row[0]): row[1]
            for row in conn.execute("SELECT id, parent_session_id FROM sessions")
        }
        assert "cold-root" not in remaining
        assert "cold-tip" not in remaining
        assert remaining["survivor-child"] == "survivor-parent"
        assert [
            row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()
        ] == ["ok"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_pinned_open_recent_and_default_platform_holds_are_negative_candidates(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "old-pinned", days_ago=100, pinned=True, content="pinned")
        _make_session(db, "old-open", days_ago=100, ended=False, content="open")
        _make_session(db, "recent-archived", days_ago=5, content="recent")
        _make_session(
            db, "photon-held", days_ago=100, source="photon", content="ops/customer"
        )
        _make_session(
            db, "eligible-cli", days_ago=100, source="cli", content="eligible"
        )
    finally:
        db.close()

    manifest = cold.build_gate_b_manifest(db_path, now=NOW)

    assert manifest["counts"]["selected_sessions"] == 1
    assert manifest["_restricted_selected_ids"] == ["eligible-cli"]
    reasons = manifest["skipped_group_reason_counts"]
    assert reasons["pinned"] == 1
    assert reasons["open_session"] == 1
    assert reasons["inside_30d_hot_plus_7d_grace"] == 1
    assert reasons["permanent_hold_source:photon"] == 1


def test_manifest_hash_mismatch_refuses_delete(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()

    manifest = cold.build_gate_b_manifest(db_path, now=NOW, hold_sources=[])

    with pytest.raises(ColdArchiveError, match="manifest hash mismatch"):
        cold.apply_retention_to_candidate(
            db_path,
            manifest,
            expected_manifest_sha256="0" * 64,
        )


def test_replay_after_success_is_idempotent_no_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()

    first = cold.run_cold_archive_pass(
        source_db=db_path,
        stage_root=tmp_path / "stage1",
        now=NOW,
        hold_sources=[],
        apply_retention=True,
    )
    second = cold.run_cold_archive_pass(
        source_db=db_path,
        stage_root=tmp_path / "stage2",
        now=NOW,
        hold_sources=[],
        apply_retention=True,
    )

    assert first["retention"]["applied"] is True
    assert second["qmd_export"]["exported_files"] == []
    assert second["retention"]["applied"] is False


def test_qmd_export_failure_fails_closed_before_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible export witness")
    finally:
        db.close()

    monkeypatch.setattr(
        cold, "verify_export_file", lambda _path, _session: (False, "forced")
    )

    with pytest.raises(ColdArchiveError, match="QMD export verification failed"):
        cold.run_cold_archive_pass(
            source_db=db_path,
            stage_root=tmp_path / "stage",
            now=NOW,
            hold_sources=[],
            apply_retention=True,
        )

    conn = cold._connect_readonly(db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id='eligible'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id='eligible'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def _rclone_runner(*, fail_step: str | None = None, partial_readback: bool = False):
    remote: dict[str, bytes] = {}

    def runner(cmd, text=True, capture_output=True):
        verb = cmd[1]
        step = "unknown"
        if verb == "copyto" and str(cmd[2]).startswith("gdrive:"):
            step = "readback"
        elif verb == "copyto":
            step = "upload"
        elif verb == "check":
            step = "check"
        if step == fail_step:
            return subprocess.CompletedProcess(cmd, 1, "", "forced failure")
        if step == "upload":
            remote[str(cmd[3])] = Path(cmd[2]).read_bytes()
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if step == "check":
            expected = Path(cmd[2]).read_bytes()
            return subprocess.CompletedProcess(
                cmd, 0 if remote.get(str(cmd[3])) == expected else 1, "", ""
            )
        if step == "readback":
            data = remote.get(str(cmd[2]), b"")
            if partial_readback:
                data = data[: max(0, len(data) - 1)]
            Path(cmd[3]).write_bytes(data)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(cmd)

    return runner


@pytest.mark.parametrize("fail_step", ["upload", "check", "readback"])
def test_rclone_publish_fails_closed_at_every_stage(
    tmp_path: Path, fail_step: str
) -> None:
    payload = tmp_path / "payload.qmd"
    payload.write_text("payload", encoding="utf-8")
    config = tmp_path / "rclone.conf"
    config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")

    with pytest.raises(ColdArchiveError):
        cold.publish_paths_with_rclone(
            [payload],
            remote_root="gdrive:vps-offload/hermes-state-archives",
            rclone_config=config,
            namespace="hermes-state/test",
            runner=_rclone_runner(fail_step=fail_step),
        )


def test_rclone_partial_remote_object_fails_readback(tmp_path: Path) -> None:
    payload = tmp_path / "payload.qmd"
    payload.write_text("payload", encoding="utf-8")
    config = tmp_path / "rclone.conf"
    config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")

    with pytest.raises(ColdArchiveError, match="size mismatch"):
        cold.publish_paths_with_rclone(
            [payload],
            remote_root="gdrive:vps-offload/hermes-state-archives",
            rclone_config=config,
            namespace="hermes-state/test",
            runner=_rclone_runner(partial_readback=True),
        )


def test_rejects_active_live_state_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live = tmp_path / "state.db"
    live.write_bytes(b"not a sqlite db")
    monkeypatch.setattr(cold, "DEFAULT_DB_PATH", live)

    with pytest.raises(ColdArchiveError, match="active Hermes state.db"):
        cold.reject_live_state_db(live)
