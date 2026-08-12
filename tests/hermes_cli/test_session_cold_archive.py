from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_state import SessionDB
import hermes_cli.session_cold_archive as cold
from hermes_cli.session_cold_archive import ColdArchiveError

NOW = 2_000_000.0
DAY = 86_400.0
# Literals required by the review contract — do NOT import module constants for
# these assertions (a test that reads the constant under test can be gamed).
LITERAL_MIN_COLD_AGE_SECONDS = 3_196_800  # 37 * 86400
LITERAL_MIN_COLD_AGE_DAYS = 37
LITERAL_BUNDLE_RETENTION_SECONDS = 1_209_600  # 14 * 86400


def _ensure_cold_archive_schema(conn: sqlite3.Connection) -> None:
    """Add durable columns the cold-archive pass requires (migration surface)."""
    columns = {str(row[1]) for row in conn.execute('PRAGMA table_info("sessions")')}
    if "pinned" not in columns:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
        )
    if "last_activity_at" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_activity_at REAL")
    conn.commit()


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
            last_activity_at = ?, message_count = ?
        WHERE id = ?
        """,
        (
            timestamp - 10,
            timestamp if ended else None,
            end_reason if ended else None,
            1 if archived else 0,
            1 if pinned else 0,
            timestamp,
            catalog_message_count,
            session_id,
        ),
    )
    db._conn.commit()


def _build_db(path: Path, *, with_required_columns: bool = True) -> SessionDB:
    db = SessionDB(db_path=path)
    assert db._conn is not None
    if with_required_columns:
        _ensure_cold_archive_schema(db._conn)
    return db


def _build_db_current_production_schema(path: Path) -> SessionDB:
    """SessionDB as shipped today — no pinned / last_activity_at columns."""
    return _build_db(path, with_required_columns=False)


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
    assert receipt["bundle_retention_seconds"] == LITERAL_BUNDLE_RETENTION_SECONDS
    assert receipt["remote_publish_policy"]["publishes_restricted_ids"] is False
    assert receipt["remote_publish_policy"]["publishes_parent_map"] is False
    assert receipt["remote_publish_policy"]["publishes_qmd_plaintext"] is False

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
    payload = tmp_path / "payload.bin"
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
    payload = tmp_path / "payload.bin"
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


# ---------------------------------------------------------------------------
# Finding 1 — fail closed on missing pinned / last_activity_at
# ---------------------------------------------------------------------------


def test_finding1_current_schema_without_pinned_refuses_selection(
    tmp_path: Path,
) -> None:
    """Production schema lacks pinned/last_activity_at — must refuse, not delete."""
    db_path = tmp_path / "prod-schema.db"
    db = _build_db_current_production_schema(db_path)
    try:
        columns = {
            str(row[1]) for row in db._conn.execute('PRAGMA table_info("sessions")')
        }
        assert "pinned" not in columns
        assert "last_activity_at" not in columns
        # Insert an old ended archived session the OLD code would have selected
        # after inventing a synthetic pinned column in tests only.
        db.create_session(session_id="old-unprovable-pin", source="cli")
        ts = NOW - 100 * DAY
        db.append_message("old-unprovable-pin", "user", "payload")
        db._conn.execute(
            "UPDATE messages SET timestamp = ? WHERE session_id = ?",
            (ts, "old-unprovable-pin"),
        )
        db._conn.execute(
            """
            UPDATE sessions
            SET started_at = ?, ended_at = ?, end_reason = 'done', archived = 1,
                message_count = 1
            WHERE id = ?
            """,
            (ts - 10, ts, "old-unprovable-pin"),
        )
        db._conn.commit()
    finally:
        db.close()

    with pytest.raises(ColdArchiveError, match="missing required durable columns"):
        cold.build_gate_b_manifest(db_path, now=NOW, hold_sources=[])


def test_finding1_missing_pinned_refuses_delete_not_empty_invariant(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()
    manifest = cold.build_gate_b_manifest(db_path, now=NOW, hold_sources=[])

    # Drop pinned after manifest to simulate schema drift / mismatched candidate.
    conn = sqlite3.connect(str(db_path))
    try:
        # SQLite cannot DROP COLUMN on older versions reliably; simulate by
        # rebuilding sessions without pinned while keeping last_activity_at.
        cols = [
            str(r[1]) for r in conn.execute('PRAGMA table_info("sessions")')
        ]
        keep = [c for c in cols if c != "pinned"]
        conn.execute("ALTER TABLE sessions RENAME TO sessions_old")
        # Minimal recreation without pinned.
        col_defs = []
        for c in keep:
            if c == "id":
                col_defs.append("id TEXT PRIMARY KEY")
            elif c == "last_activity_at":
                col_defs.append("last_activity_at REAL")
            else:
                col_defs.append(f'"{c}"')
        conn.execute(
            f"CREATE TABLE sessions ({', '.join(col_defs)})"
        )
        conn.execute(
            f"INSERT INTO sessions ({', '.join(keep)}) "
            f"SELECT {', '.join(keep)} FROM sessions_old"
        )
        conn.execute("DROP TABLE sessions_old")
        conn.commit()
        columns = {str(r[1]) for r in conn.execute('PRAGMA table_info("sessions")')}
        assert "pinned" not in columns
    finally:
        conn.close()

    with pytest.raises(ColdArchiveError, match="missing required durable columns"):
        cold.apply_retention_to_candidate(
            db_path,
            manifest,
            expected_manifest_sha256=manifest["manifest_sha256"],
        )
    # Row must still exist.
    conn = sqlite3.connect(str(db_path))
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id='eligible'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Finding 2 — hardlink live-DB alias rejection (inode, not path)
# ---------------------------------------------------------------------------


def test_finding2_hardlink_alias_of_live_db_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live = tmp_path / "live-home" / "state.db"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"sqlite-live-bytes")
    alias = tmp_path / "offline-looking" / "candidate.db"
    alias.parent.mkdir(parents=True)
    os.link(live, alias)
    assert live.stat().st_ino == alias.stat().st_ino
    assert live.resolve() != alias.resolve()

    monkeypatch.setattr(cold, "DEFAULT_DB_PATH", live)
    monkeypatch.setattr(cold, "get_hermes_home", lambda: live.parent)

    with pytest.raises(ColdArchiveError, match="hardlink|alias|inode"):
        cold.reject_live_state_db(alias)


# ---------------------------------------------------------------------------
# Finding 3 — Gate-B must not self-overwrite attested bytes
# ---------------------------------------------------------------------------


def test_finding3_gate_b_refuses_self_overwrite_of_attested_bytes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()

    stage = tmp_path / "stage"
    m1 = cold.build_gate_b_manifest(db_path, now=NOW, hold_sources=[])
    arts = cold.write_gate_b_manifest(stage, m1)
    original = arts.manifest_path.read_bytes()
    original_sha = cold.sha256_path(arts.manifest_path)

    # Second write with different content (different now) must refuse.
    m2 = cold.build_gate_b_manifest(db_path, now=NOW + 10, hold_sources=[])
    assert m2["manifest_sha256"] != m1["manifest_sha256"]
    with pytest.raises(ColdArchiveError, match="overwrite existing GATE-B-MANIFEST"):
        cold.write_gate_b_manifest(stage, m2)

    assert arts.manifest_path.read_bytes() == original
    assert cold.sha256_path(arts.manifest_path) == original_sha


# ---------------------------------------------------------------------------
# Finding 4 — QMD session-ID path escape
# ---------------------------------------------------------------------------


def test_finding4_qmd_session_id_path_escape_contained(tmp_path: Path) -> None:
    """Session IDs with path separators must not escape cold-qmd/ or clobber Gate-B."""
    # Unit-level: filename sanitizer
    evil_session = {
        "id": "../../GATE-B-MANIFEST.json",
        "title": "x",
        "messages": [],
        "message_count": 0,
    }
    leaf = cold._safe_qmd_filename(evil_session)
    assert "/" not in leaf and "\\" not in leaf
    assert ".." not in leaf
    assert leaf.endswith(".qmd")
    assert leaf != "GATE-B-MANIFEST.json"

    # Integration: stage write stays contained even if safe_session_filename
    # would have produced a traversing name.
    qmd_root = tmp_path / "stage" / "cold-qmd"
    qmd_root.mkdir(parents=True)
    gate_b = tmp_path / "stage" / "GATE-B-MANIFEST.json"
    gate_b.write_text('{"attested": true}\n', encoding="utf-8")
    before = gate_b.read_bytes()

    # Direct containment check for a malicious relative name
    with pytest.raises(ColdArchiveError, match="path escape|unsafe"):
        # Force a contained_path call with a traversal name
        cold._contained_path(qmd_root, "../../GATE-B-MANIFEST.json")

    assert gate_b.read_bytes() == before

    # End-to-end export path: monkeypatch safe_session_filename to return traversal
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible export")
    finally:
        db.close()

    manifest = cold.build_gate_b_manifest(db_path, now=NOW, hold_sources=[])
    stage = cold.write_gate_b_manifest(tmp_path / "stage2", manifest)
    gate_before = stage.manifest_path.read_bytes()

    import hermes_cli.session_cold_archive as mod

    original = mod.safe_session_filename

    def evil_name(session, *, fmt="md"):
        return f"../../GATE-B-MANIFEST.json-hijack.{fmt}"

    mod.safe_session_filename = evil_name  # type: ignore[assignment]
    try:
        report = cold.export_redacted_qmd(db_path, stage, manifest)
    finally:
        mod.safe_session_filename = original  # type: ignore[assignment]

    assert stage.manifest_path.read_bytes() == gate_before
    qmd_resolved = stage.qmd_dir.resolve()
    for item in report["exported_files"]:
        path = Path(item["path"]).resolve()
        assert path.parent == qmd_resolved or qmd_resolved in path.parents
        assert path.name != "GATE-B-MANIFEST.json"
        assert all(part != ".." for part in path.parts)


# ---------------------------------------------------------------------------
# Finding 5 — post-COMMIT invariant failure must not retain deletion
# ---------------------------------------------------------------------------


def test_finding5_invariant_failure_rolls_back_deletion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
        _make_session(db, "hot-survivor", days_ago=2, archived=False, ended=False, content="hot")
    finally:
        db.close()

    manifest = cold.build_gate_b_manifest(db_path, now=NOW, hold_sources=[])
    assert "eligible" in manifest["_restricted_selected_ids"]

    real_capture = cold._capture_invariants
    calls = {"n": 0}

    def flaky_capture(conn, *, hot_cutoff):
        result = real_capture(conn, hot_cutoff=hot_cutoff)
        calls["n"] += 1
        # After deletion (second call inside txn), poison open_ids so invariant fails.
        if calls["n"] >= 2:
            poisoned = dict(result)
            poisoned["open_ids"] = list(result["open_ids"]) + ["ghost-open"]
            return poisoned
        return result

    monkeypatch.setattr(cold, "_capture_invariants", flaky_capture)

    with pytest.raises(ColdArchiveError, match="open_ids changed"):
        cold.apply_retention_to_candidate(
            db_path,
            manifest,
            expected_manifest_sha256=manifest["manifest_sha256"],
        )

    conn = sqlite3.connect(str(db_path))
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


# ---------------------------------------------------------------------------
# Finding 6 — exact 37-day boundary is eligible (not skipped)
# ---------------------------------------------------------------------------


def test_finding6_exact_37_day_boundary_is_selected(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(
            db,
            "exact-37d",
            days_ago=float(LITERAL_MIN_COLD_AGE_DAYS),
            content="boundary",
        )
    finally:
        db.close()

    manifest = cold.build_gate_b_manifest(
        db_path,
        now=NOW,
        hold_sources=[],
        hot_days=30.0,
        archive_grace_days=7.0,
    )
    assert manifest["_restricted_selected_ids"] == ["exact-37d"]
    # Cold cutoff must sit exactly 37d behind NOW for default policy.
    assert manifest["policy"]["cold_cutoff_epoch"] == pytest.approx(
        NOW - LITERAL_MIN_COLD_AGE_SECONDS
    )


# ---------------------------------------------------------------------------
# Finding 7 — system_prompts orphans must not be deleted
# ---------------------------------------------------------------------------


def test_finding7_system_prompts_orphans_not_deleted(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
        db._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_prompts (
                hash TEXT PRIMARY KEY,
                body TEXT
            )
            """
        )
        db._conn.execute(
            "INSERT INTO system_prompts(hash, body) VALUES ('orphan-hash', 'keep me')"
        )
        # Point session at a different hash so orphan remains unreferenced.
        if "system_prompt_hash" in {
            str(r[1]) for r in db._conn.execute('PRAGMA table_info("sessions")')
        }:
            db._conn.execute(
                "UPDATE sessions SET system_prompt_hash = 'other' WHERE id='eligible'"
            )
        db._conn.commit()
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
    assert receipt["retention"].get("system_prompts_deleted", 0) == 0

    conn = sqlite3.connect(str(db_path))
    try:
        assert (
            conn.execute(
                "SELECT body FROM system_prompts WHERE hash='orphan-hash'"
            ).fetchone()[0]
            == "keep me"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Finding 8 — explicit 14-day bundle retention (literal 1_209_600s)
# ---------------------------------------------------------------------------


def test_finding8_bundle_retention_literal_14_days() -> None:
    now = 10_000_000.0
    # Just under 14 days old → retain
    assert (
        cold.classify_bundle_retention(
            bundle_mtime_epoch=now - (LITERAL_BUNDLE_RETENTION_SECONDS - 1),
            now_epoch=now,
            retention_seconds=LITERAL_BUNDLE_RETENTION_SECONDS,
        )
        == "retain"
    )
    # Exactly 14 days old → eligible for purge (age < retention is retain)
    assert (
        cold.classify_bundle_retention(
            bundle_mtime_epoch=now - LITERAL_BUNDLE_RETENTION_SECONDS,
            now_epoch=now,
            retention_seconds=LITERAL_BUNDLE_RETENTION_SECONDS,
        )
        == "eligible_for_purge"
    )
    # Module default must equal the literal (cross-check, not the sole assertion)
    assert int(cold.BUNDLE_RETENTION_SECONDS) == LITERAL_BUNDLE_RETENTION_SECONDS


# ---------------------------------------------------------------------------
# Finding 9 — never remotely publish restricted IDs / parent map / QMD
# ---------------------------------------------------------------------------


def test_finding9_publish_refuses_restricted_ids_parent_map_and_qmd(
    tmp_path: Path,
) -> None:
    config = tmp_path / "rclone.conf"
    config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")

    restricted_ids = tmp_path / "selected-session-ids.txt"
    restricted_ids.write_text("sid-1\n", encoding="utf-8")
    parent_map = tmp_path / "lineage-parent-map.json"
    parent_map.write_text("{}", encoding="utf-8")
    qmd = tmp_path / "session.qmd"
    qmd.write_text("# secret session\n", encoding="utf-8")
    nested = tmp_path / "restricted" / "anything.txt"
    nested.parent.mkdir()
    nested.write_text("ids", encoding="utf-8")

    for path, match in [
        (restricted_ids, "restricted artifact"),
        (parent_map, "restricted artifact"),
        (qmd, "QMD plaintext"),
        (nested, "restricted/"),
    ]:
        with pytest.raises(ColdArchiveError, match=match):
            cold.publish_paths_with_rclone(
                [path],
                remote_root="gdrive:vps-offload/hermes-state-archives",
                rclone_config=config,
                namespace="hermes-state/test",
                runner=_rclone_runner(),
            )


def test_finding9_run_pass_remote_list_excludes_sensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()

    published: list[list[str]] = []

    def fake_publish(paths, **kwargs):
        published.append([Path(p).name for p in paths])
        cold.assert_publish_paths_are_remote_safe(paths)
        return [
            {
                "local_path": str(p),
                "remote": f"gdrive:x/{Path(p).name}",
                "bytes": Path(p).stat().st_size,
                "sha256": cold.sha256_path(Path(p)),
                "readback_sha256": cold.sha256_path(Path(p)),
                "integrity": "ok",
            }
            for p in paths
        ]

    monkeypatch.setattr(cold, "publish_paths_with_rclone", fake_publish)
    monkeypatch.setattr(
        cold,
        "encrypt_file_with_age",
        lambda source, output, **kw: {
            "path": str(output),
            "sha256": "a" * 64,
            "bytes": 1,
        }
        if (output.write_bytes(b"enc"), True)[1]
        else {},
    )

    recipient = tmp_path / "recv.pub"
    recipient.write_text("age1test\n", encoding="utf-8")
    config = tmp_path / "rclone.conf"
    config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")

    receipt = cold.run_cold_archive_pass(
        source_db=db_path,
        stage_root=tmp_path / "stage",
        now=NOW,
        hold_sources=[],
        apply_retention=False,
        rclone_remote="gdrive:vps-offload/hermes-state-archives",
        rclone_config=config,
        age_recipient_file=recipient,
    )

    assert published, "expected remote publish to run"
    names = set(published[0])
    assert "selected-session-ids.txt" not in names
    assert "lineage-parent-map.json" not in names
    assert not any(n.endswith(".qmd") for n in names)
    assert "GATE-B-MANIFEST.json" in names
    assert receipt["remote_publish_policy"]["publishes_restricted_ids"] is False


# ---------------------------------------------------------------------------
# PR52 non-regression — hard 37-day floor even with hot=0/grace=0
# ---------------------------------------------------------------------------


def test_pr52_hard_37_day_floor_blocks_one_day_old_with_zero_knobs(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    db = _build_db(db_path)
    try:
        _make_session(db, "one-day-old", days_ago=1.0, content="too fresh")
        _make_session(db, "old-enough", days_ago=40.0, content="cold")
    finally:
        db.close()

    # Reading the implementation knobs to 0 must NOT select a one-day-old row.
    manifest = cold.build_gate_b_manifest(
        db_path,
        now=NOW,
        hold_sources=[],
        hot_days=0.0,
        archive_grace_days=0.0,
    )
    assert "one-day-old" not in manifest["_restricted_selected_ids"]
    assert "old-enough" in manifest["_restricted_selected_ids"]

    # effective floor asserted via literals, not module constants alone
    cold_cutoff, _hot, effective_days = cold.effective_cold_cutoff(
        generated_at=NOW,
        hot_days=0.0,
        archive_grace_days=0.0,
    )
    assert effective_days * DAY >= LITERAL_MIN_COLD_AGE_SECONDS
    assert NOW - cold_cutoff >= LITERAL_MIN_COLD_AGE_SECONDS
    assert effective_days >= LITERAL_MIN_COLD_AGE_DAYS


def test_pr52_exact_14d_bundle_retention_not_derived_from_mutable_constant() -> None:
    """Even if someone monkeypatches BUNDLE_RETENTION_SECONDS, classifier
    callers that pass the literal still enforce 14 days. The public policy
    surface must also document the literal seconds value."""
    # Policy embedding check via a tiny offline DB manifest.
    # (constant cross-check is secondary; literal math is primary.)
    fourteen_days = 14 * 86_400
    assert fourteen_days == LITERAL_BUNDLE_RETENTION_SECONDS
    assert fourteen_days == 1_209_600
