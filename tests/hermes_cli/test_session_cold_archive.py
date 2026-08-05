from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Callable

import pytest

from hermes_state import SessionDB
import hermes_cli.session_cold_archive as cold
from hermes_cli.session_cold_archive import ColdArchiveError

NOW = 1_800_000_000.0
DAY = 86_400.0
REMOTE = "gdrive:vps-offload/hermes-state-archives"


def _build_current_db(path: Path) -> SessionDB:
    return SessionDB(db_path=path)


def _build_archive_candidate(path: Path) -> SessionDB:
    """Create an explicit candidate contract, not a silent current-schema shim.

    Current fork SessionDB intentionally lacks durable pin/activity columns. The
    command has a dedicated fail-closed test for that real schema below. Happy
    path fixtures model a recovered candidate that actually carries both
    canonical fields required by the archive contract.
    """

    db = SessionDB(db_path=path)
    assert db._conn is not None
    columns = {str(row[1]) for row in db._conn.execute('PRAGMA table_info("sessions")')}
    assert "pinned" not in columns
    assert "last_activity_at" not in columns
    db._conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER")
    db._conn.execute("ALTER TABLE sessions ADD COLUMN last_activity_at REAL")
    db._conn.commit()
    return db


def _make_session(
    db: SessionDB,
    session_id: str,
    *,
    days_ago: float,
    archived: bool = True,
    pinned: bool | None = False,
    ended: bool = True,
    parent_session_id: str | None = None,
    end_reason: str | None = "done",
    source: str = "cli",
    title: str | None = None,
    content: str | None = None,
    catalog_message_count: int = 999,
    durable_days_ago: float | None = None,
) -> None:
    timestamp = NOW - days_ago * DAY
    db.create_session(
        session_id=session_id,
        source=source,
        parent_session_id=parent_session_id,
    )
    if title:
        db.set_session_title(session_id, title)
    if content is not None:
        db.append_message(session_id, "user", content, timestamp=timestamp)
    durable = (
        NOW - (durable_days_ago if durable_days_ago is not None else days_ago) * DAY
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
            None if pinned is None else (1 if pinned else 0),
            durable,
            catalog_message_count,
            session_id,
        ),
    )
    db._conn.commit()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _readonly_rows(
    path: Path, sql: str, params: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _logical_snapshot(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        result: dict[str, list[tuple[Any, ...]]] = {}
        for table in (
            "sessions",
            "messages",
            "session_model_usage",
            "compression_locks",
            "telegram_dm_topic_bindings",
            "system_prompts",
        ):
            if table in tables:
                result[table] = [
                    tuple(row)
                    for row in conn.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    ).fetchall()
                ]
        return result
    finally:
        conn.close()


def _fake_age(
    command: list[str], text: bool = True, capture_output: bool = True
) -> subprocess.CompletedProcess[str]:
    del text, capture_output
    source = Path(command[-1])
    output = Path(command[command.index("-o") + 1])
    output.write_bytes(
        b"AGE-TEST-CIPHERTEXT\n" + hashlib.sha256(source.read_bytes()).digest()
    )
    os.chmod(output, 0o600)
    return subprocess.CompletedProcess(command, 0, "", "")


def _failing_age(
    command: list[str], text: bool = True, capture_output: bool = True
) -> subprocess.CompletedProcess[str]:
    del text, capture_output
    return subprocess.CompletedProcess(command, 1, "", "forced age failure")


class FakeRclone:
    def __init__(
        self,
        *,
        fail_step: str | None = None,
        partial_upload: bool = False,
        lie_on_check: bool = False,
        corrupt_same_size: bool = False,
        partial_upload_index: int | None = None,
        callback: Callable[[str], None] | None = None,
    ) -> None:
        self.fail_step = fail_step
        self.partial_upload = partial_upload
        self.lie_on_check = lie_on_check
        self.corrupt_same_size = corrupt_same_size
        self.partial_upload_index = partial_upload_index
        self.callback = callback
        self.remote: dict[str, bytes] = {}
        self.commands: list[tuple[str, list[str]]] = []
        self.upload_count = 0

    def __call__(
        self, command: list[str], text: bool = True, capture_output: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del text, capture_output
        verb = command[1]
        source = command[2]
        destination = command[3]
        if verb == "check":
            step = "check"
        elif verb == "copyto" and source in self.remote:
            step = "readback"
        elif verb == "copyto":
            step = "upload"
        else:
            raise AssertionError(command)
        self.commands.append((step, list(command)))
        if self.callback is not None:
            self.callback(step)
        if step == self.fail_step:
            return subprocess.CompletedProcess(command, 1, "", f"forced {step} failure")
        if step == "upload":
            self.upload_count += 1
            data = Path(source).read_bytes()
            if self.partial_upload or self.partial_upload_index == self.upload_count:
                data = data[:-1]
            elif self.corrupt_same_size and data:
                data = bytes([data[0] ^ 0x01]) + data[1:]
            self.remote[destination] = data
        elif step == "check":
            expected = Path(source).read_bytes()
            success = self.lie_on_check or self.remote.get(destination) == expected
            return subprocess.CompletedProcess(command, 0 if success else 1, "", "")
        else:
            Path(destination).write_bytes(self.remote.get(source, b""))
        return subprocess.CompletedProcess(command, 0, "", "")


def _producer(
    tmp_path: Path,
    db_path: Path,
    fake: FakeRclone,
    *,
    stage_name: str = "stage",
    age_runner: Callable[..., subprocess.CompletedProcess[str]] = _fake_age,
    manifest_only: bool = False,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    recipient = tmp_path / f"{stage_name}.age.pub"
    recipient.write_text("age1testfixture", encoding="utf-8")
    config = tmp_path / f"{stage_name}.rclone.conf"
    config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
    stage = tmp_path / stage_name
    receipt = cold.run_cold_archive_pass(
        source_db=db_path,
        stage_root=stage,
        now=NOW,
        hold_sources=[],
        manifest_only=manifest_only,
        rclone_remote=REMOTE if not manifest_only else None,
        rclone_config=config if not manifest_only else None,
        age_recipient_file=recipient if not manifest_only else None,
        age_runner=age_runner,
        rclone_runner=fake,
    )
    return stage, recipient, config, receipt


def _apply(
    db_path: Path,
    stage: Path,
    config: Path,
    producer_receipt: dict[str, Any],
    fake: FakeRclone,
) -> dict[str, Any]:
    return cold.run_cold_archive_pass(
        source_db=db_path,
        stage_root=stage,
        apply_retention=True,
        approved_manifest_path=stage / "GATE-B-MANIFEST.json",
        approved_manifest_sha256=producer_receipt["manifest_file_sha256"],
        rclone_config=config,
        rclone_runner=fake,
    )


def test_current_fork_schema_without_pin_and_activity_fails_closed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "current.db"
    db = _build_current_db(db_path)
    try:
        db.create_session(session_id="old", source="cli")
        db._conn.execute(
            "UPDATE sessions SET archived=1, ended_at=?, started_at=? WHERE id='old'",
            (NOW - 80 * DAY, NOW - 80 * DAY),
        )
        db._conn.commit()
    finally:
        db.close()

    with pytest.raises(
        ColdArchiveError, match="missing sessions columns: last_activity_at, pinned"
    ):
        cold.build_gate_b_manifest(db_path, now=NOW, hold_sources=[])


def test_manifest_uses_canonical_activity_and_exact_37_day_boundary(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(
            db, "durable-recent", days_ago=90, durable_days_ago=2, content="old"
        )
        _make_session(
            db, "message-recent", days_ago=2, durable_days_ago=90, content="recent"
        )
        _make_session(db, "inside-by-one", days_ago=37 - 1 / DAY, content="inside")
        _make_session(db, "exact-boundary", days_ago=37, content="exact")
        _make_session(db, "outside-by-one", days_ago=37 + 1 / DAY, content="outside")
    finally:
        db.close()

    manifest = cold.build_gate_b_manifest(db_path, now=NOW, hold_sources=[])

    assert manifest["_restricted_selected_ids"] == ["exact-boundary", "outside-by-one"]
    assert manifest["skipped_group_reason_counts"]["inside_30d_hot_plus_7d_grace"] == 3
    assert manifest["policy"]["cold_boundary_inclusive"] is True


def test_null_canonical_activity_or_pin_state_is_not_selectable(tmp_path: Path) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "no-activity", days_ago=80, content="cold")
        _make_session(db, "no-pin", days_ago=80, content="cold")
        db._conn.execute(
            "UPDATE sessions SET last_activity_at=NULL WHERE id='no-activity'"
        )
        db._conn.execute("UPDATE sessions SET pinned=NULL WHERE id='no-pin'")
        db._conn.commit()
    finally:
        db.close()

    manifest = cold.build_gate_b_manifest(db_path, now=NOW, hold_sources=[])
    assert manifest["_restricted_selected_ids"] == []
    assert manifest["skipped_group_reason_counts"] == {
        "canonical_last_activity_unprovable": 1,
        "pin_state_unprovable": 1,
    }


def test_rejects_default_named_profile_and_sidecar_hardlink_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    current = root / "profiles" / "current"
    other = root / "profiles" / "other"
    current.mkdir(parents=True)
    other.mkdir(parents=True)
    default_db = root / "state.db"
    named_db = other / "state.db"
    sidecar = current / "state.db-wal"
    for path in (default_db, named_db, sidecar):
        path.write_bytes(b"live")
    monkeypatch.setattr(cold, "get_default_hermes_root", lambda: root)
    monkeypatch.setattr(cold, "get_hermes_home", lambda: current)
    monkeypatch.setattr(cold, "DEFAULT_DB_PATH", default_db)

    for index, protected in enumerate((default_db, named_db, sidecar)):
        alias = tmp_path / f"alias-{index}.db"
        os.link(protected, alias)
        assert os.path.samefile(protected, alias)
        with pytest.raises(ColdArchiveError, match="hardlink/inode alias"):
            cold.reject_live_state_db(alias)

    offline = tmp_path / "offline.db"
    offline.write_bytes(b"offline")
    offline_wal = offline.with_name(offline.name + "-wal")
    os.link(sidecar, offline_wal)
    with pytest.raises(ColdArchiveError, match="hardlink/inode alias"):
        cold.reject_live_state_db(offline)


def test_existing_stage_is_untouched_and_never_chmodded_or_overwritten(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()
    stage = tmp_path / "existing-stage"
    stage.mkdir(mode=0o755)
    sentinel = stage / "GATE-B-MANIFEST.json"
    sentinel.write_bytes(b"approved-original")
    before = (stage.stat().st_ino, stat.S_IMODE(stage.stat().st_mode), _sha(sentinel))

    with pytest.raises(ColdArchiveError, match="refusing existing stage path"):
        cold.run_cold_archive_pass(
            source_db=db_path,
            stage_root=stage,
            now=NOW,
            hold_sources=[],
            manifest_only=True,
        )

    after = (stage.stat().st_ino, stat.S_IMODE(stage.stat().st_mode), _sha(sentinel))
    assert after == before
    assert sentinel.read_bytes() == b"approved-original"


def test_malicious_ids_are_json_encoded_and_qmd_paths_stay_immediate_children(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(
            db,
            "../escaped\nline",
            days_ago=80,
            title="../../hostile title",
            content="hostile id payload",
        )
        _make_session(db, "a/b", days_ago=80, title="same slash", content="slash")
        _make_session(db, "a:b", days_ago=80, title="same colon", content="colon")
        _make_session(db, "nul\x00id", days_ago=80, title="control", content="nul")
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, _config, receipt = _producer(tmp_path, db_path, fake)

    ids_payload = json.loads(
        (stage / "restricted" / "selected-session-ids.json").read_text()
    )
    assert sorted(ids_payload["selected_ids"]) == [
        "../escaped\nline",
        "a/b",
        "a:b",
        "nul\x00id",
    ]
    qmd_paths = [Path(item["path"]) for item in receipt["qmd_export"]["exported_files"]]
    assert len(qmd_paths) == 4
    assert len({path.name for path in qmd_paths}) == 4
    assert all(path.parent == stage / "cold-qmd" for path in qmd_paths)
    assert all(path.name == Path(path.name).name for path in qmd_paths)
    assert not (stage / "escaped").exists()
    assert not (tmp_path / "escaped").exists()


def test_remote_publish_is_two_opaque_age_packets_then_only_clear_manifest(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(
            db,
            "secret-session-id",
            days_ago=80,
            title="Customer Secret Title",
            content="restricted payload",
        )
    finally:
        db.close()
    fake = FakeRclone()
    _stage, _recipient, _config, receipt = _producer(tmp_path, db_path, fake)

    uploads = [command for step, command in fake.commands if step == "upload"]
    assert [Path(command[2]).name for command in uploads] == [
        "ROLLBACK-SOURCE-BUNDLE.tar.gz.age",
        "RESTRICTED-COLD-QMD.tar.gz.age",
        "GATE-B-MANIFEST.json",
    ]
    remote_names = [command[3].rsplit("/", 1)[-1] for command in uploads]
    assert remote_names == [
        "ROLLBACK-SOURCE-BUNDLE.tar.gz.age",
        "RESTRICTED-COLD-QMD.tar.gz.age",
        "GATE-B-MANIFEST.json",
    ]
    command_text = "\n".join(" ".join(command) for command in uploads)
    for forbidden in (
        "secret-session-id",
        "Customer Secret Title",
        "selected-session-ids",
        "lineage-parent-map",
        ".qmd",
    ):
        assert forbidden not in command_text
    assert len(receipt["remote_publish"]) == 3


@pytest.mark.parametrize("failure", ["age", "upload", "check", "readback", "partial"])
def test_full_producer_failure_never_reaches_retention_or_changes_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible producer witness")
    finally:
        db.close()
    before = _logical_snapshot(db_path)
    calls = 0

    def forbidden_apply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        del args, kwargs
        calls += 1
        raise AssertionError("retention must not be reached by producer")

    monkeypatch.setattr(cold, "apply_retention_to_candidate", forbidden_apply)
    fake = FakeRclone(
        fail_step=failure if failure in {"upload", "check", "readback"} else None,
        partial_upload=failure == "partial",
    )
    age_runner = _failing_age if failure == "age" else _fake_age

    with pytest.raises(ColdArchiveError):
        _producer(tmp_path, db_path, fake, age_runner=age_runner)

    assert calls == 0
    assert _logical_snapshot(db_path) == before
    assert _readonly_rows(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _readonly_rows(db_path, "PRAGMA foreign_key_check") == []
    if failure == "partial":
        assert fake.remote
        remote_bytes = next(iter(fake.remote.values()))
        upload = next(command for step, command in fake.commands if step == "upload")
        assert len(remote_bytes) == len(Path(upload[2]).read_bytes()) - 1


def test_true_partial_remote_fails_readback_even_when_checksum_lies(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()
    fake = FakeRclone(partial_upload=True, lie_on_check=True)

    with pytest.raises(ColdArchiveError, match="readback size mismatch"):
        _producer(tmp_path, db_path, fake)

    assert _readonly_rows(db_path, "SELECT id FROM sessions") == [("eligible",)]
    assert all(len(value) > 0 for value in fake.remote.values())


def test_same_size_remote_corruption_fails_sha_readback(tmp_path: Path) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()
    fake = FakeRclone(corrupt_same_size=True, lie_on_check=True)

    with pytest.raises(ColdArchiveError, match="readback sha256 mismatch"):
        _producer(tmp_path, db_path, fake)


def test_apply_requires_exact_external_manifest_bytes_and_never_rewrites_stage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    manifest = stage / "GATE-B-MANIFEST.json"
    original = manifest.read_bytes()
    before_modes = {
        path.relative_to(stage).as_posix(): stat.S_IMODE(path.lstat().st_mode)
        for path in stage.rglob("*")
    }
    before_hashes = {
        path.relative_to(stage).as_posix(): _sha(path)
        for path in stage.rglob("*")
        if path.is_file()
    }

    result = _apply(db_path, stage, config, producer, fake)

    assert result["retention"]["applied"] is True
    assert manifest.read_bytes() == original
    for relative, digest in before_hashes.items():
        assert _sha(stage / relative) == digest
    for relative, mode in before_modes.items():
        assert stat.S_IMODE((stage / relative).lstat().st_mode) == mode
    assert (
        stat.S_IMODE((stage / "COLD-ARCHIVE-RETENTION-RECEIPT.json").stat().st_mode)
        == 0o600
    )


def test_tampered_approved_manifest_fails_without_delete(tmp_path: Path) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    manifest = stage / "GATE-B-MANIFEST.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    os.chmod(manifest, 0o600)

    with pytest.raises(ColdArchiveError, match="exact-byte sha256 mismatch"):
        _apply(db_path, stage, config, producer, fake)

    assert _readonly_rows(db_path, "SELECT id FROM sessions") == [("eligible",)]
    assert not (stage / "COLD-ARCHIVE-RETENTION-RECEIPT.json").exists()


def test_manifest_only_stage_cannot_self_authorize_apply(tmp_path: Path) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=80, content="eligible")
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(
        tmp_path, db_path, fake, manifest_only=True
    )

    with pytest.raises(ColdArchiveError, match="incomplete"):
        _apply(db_path, stage, config, producer, fake)

    assert _readonly_rows(db_path, "SELECT id FROM sessions") == [("eligible",)]


def test_retention_preserves_all_32_parent_84_child_rows_and_edges(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
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
            for _child_index in range(child_total):
                child_id = f"recent-child-{child_counter:02d}"
                _make_session(
                    db,
                    child_id,
                    days_ago=2,
                    archived=False,
                    ended=False,
                    parent_session_id=parent_id,
                    content=f"recent child witness {child_counter}",
                )
                child_counter += 1
        assert child_counter == 84
        _make_session(
            db,
            "eligible-control",
            days_ago=90,
            content="selected uniqueeligiblecontroltoken",
        )
    finally:
        db.close()

    hazard_map = dict(
        _readonly_rows(
            db_path,
            "SELECT id, parent_session_id FROM sessions WHERE id <> 'eligible-control' ORDER BY id",
        )
    )
    assert len(hazard_map) == 116
    assert sum(value is not None for value in hazard_map.values()) == 84
    assert len({value for value in hazard_map.values() if value is not None}) == 32
    with sqlite3.connect(db_path) as conn:
        payload_before = cold._capture_payload_survivors(
            conn,
            selected_session_ids=["eligible-control"],
            prompt_hashes_to_delete=[],
        )

    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    public_manifest = json.loads((stage / "GATE-B-MANIFEST.json").read_text())
    assert public_manifest["counts"]["selected_sessions"] == 1
    assert public_manifest["counts"]["selected_messages_actual"] == 1
    result = _apply(db_path, stage, config, producer, fake)

    assert result["retention"]["deleted_sessions"] == 1
    assert result["retention"]["deleted_messages_actual"] == 1
    after_map = dict(
        _readonly_rows(
            db_path, "SELECT id, parent_session_id FROM sessions ORDER BY id"
        )
    )
    assert after_map == hazard_map
    assert len(after_map) == 116
    assert sum(value is not None for value in after_map.values()) == 84
    with sqlite3.connect(db_path) as conn:
        payload_after = cold._capture_payload_survivors(
            conn, selected_session_ids=[], prompt_hashes_to_delete=[]
        )
    assert payload_after == payload_before
    assert _readonly_rows(db_path, "SELECT COUNT(*) FROM messages") == [(116,)]
    assert _readonly_rows(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _readonly_rows(db_path, "PRAGMA foreign_key_check") == []


def test_rollback_bundle_restores_exact_pre_retention_state(tmp_path: Path) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="rollback selected")
        _make_session(
            db, "survivor", days_ago=2, archived=False, ended=False, content="survivor"
        )
    finally:
        db.close()
    pre_sha = _sha(db_path)
    pre_snapshot = _logical_snapshot(db_path)
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    bundle_path = Path(producer["rollback_bundle"]["path"])
    assert producer["rollback_bundle"]["sha256"] == _sha(bundle_path)
    result = _apply(db_path, stage, config, producer, fake)
    assert result["retention"]["applied"] is True
    assert _logical_snapshot(db_path) != pre_snapshot

    restore_path = tmp_path / "restored.db"
    with tarfile.open(bundle_path, "r:gz") as archive:
        names = archive.getnames()
        assert all(name == Path(name).name for name in names)
        manifest_member = archive.extractfile("ROLLBACK-BUNDLE-MANIFEST.json")
        assert manifest_member is not None
        bundle_manifest = json.loads(manifest_member.read())
        for item in bundle_manifest["files"]:
            if item["status"] != "copied":
                continue
            member = archive.extractfile(item["name"])
            assert member is not None
            data = member.read()
            assert len(data) == item["bytes"]
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            if item["name"] == db_path.name:
                restore_path.write_bytes(data)
                os.chmod(restore_path, 0o600)
                assert item["sha256"] == pre_sha
            else:
                suffix = item["name"][len(db_path.name) :]
                sidecar_restore = restore_path.with_name(restore_path.name + suffix)
                sidecar_restore.write_bytes(data)
                os.chmod(sidecar_restore, 0o600)

    assert _sha(restore_path) == pre_sha
    assert _logical_snapshot(restore_path) == pre_snapshot
    assert _readonly_rows(restore_path, "PRAGMA integrity_check") == [("ok",)]
    assert _readonly_rows(restore_path, "PRAGMA foreign_key_check") == []
    assert _readonly_rows(restore_path, "SELECT id FROM sessions ORDER BY id") == [
        ("eligible",),
        ("survivor",),
    ]


def test_post_delete_parent_invariant_failure_rolls_back_every_change(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
        _make_session(
            db,
            "survivor-parent",
            days_ago=2,
            archived=False,
            ended=False,
            content="hot",
        )
        _make_session(
            db,
            "survivor-child",
            days_ago=2,
            archived=False,
            ended=False,
            parent_session_id="survivor-parent",
            content="hot child",
        )
        db._conn.execute(
            """
            CREATE TRIGGER force_parent_drift
            AFTER DELETE ON sessions WHEN OLD.id = 'eligible'
            BEGIN
                UPDATE sessions SET parent_session_id = NULL WHERE id = 'survivor-child';
            END
            """
        )
        db._conn.commit()
    finally:
        db.close()
    before = _logical_snapshot(db_path)
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)

    with pytest.raises(
        ColdArchiveError, match="surviving parent_session_id map changed"
    ):
        _apply(db_path, stage, config, producer, fake)

    assert _logical_snapshot(db_path) == before
    assert _readonly_rows(
        db_path, "SELECT parent_session_id FROM sessions WHERE id='survivor-child'"
    ) == [("survivor-parent",)]
    assert _readonly_rows(db_path, "SELECT id FROM sessions WHERE id='eligible'") == [
        ("eligible",)
    ]
    assert not (stage / "COLD-ARCHIVE-RETENTION-RECEIPT.json").exists()


def test_late_fts_verifier_failure_runs_inside_transaction_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    original = cold._verify_fts_counts

    def fail_late(
        conn: sqlite3.Connection, *, deleted_message_ids: list[int]
    ) -> dict[str, Any]:
        assert conn.in_transaction
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id='eligible'"
            ).fetchone()[0]
            == 0
        )
        original(conn, deleted_message_ids=deleted_message_ids)
        raise ColdArchiveError("forced late FTS failure")

    monkeypatch.setattr(cold, "_verify_fts_counts", fail_late)
    with pytest.raises(ColdArchiveError, match="forced late FTS failure"):
        _apply(db_path, stage, config, producer, fake)

    assert _readonly_rows(db_path, "SELECT id FROM sessions") == [("eligible",)]
    assert _readonly_rows(db_path, "SELECT session_id FROM messages") == [("eligible",)]


def test_remote_preflight_drift_is_rejected_by_complete_approved_snapshot(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
    finally:
        db.close()
    producer_fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, producer_fake)
    mutated = False

    def mutate_on_first_check(step: str) -> None:
        nonlocal mutated
        if step != "check" or mutated:
            return
        mutated = True
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE sessions SET pinned=1 WHERE id='eligible'")
            conn.commit()
        finally:
            conn.close()

    apply_fake = FakeRclone(callback=mutate_on_first_check)
    apply_fake.remote = dict(producer_fake.remote)
    with pytest.raises(ColdArchiveError, match="logical snapshot differs"):
        _apply(db_path, stage, config, producer, apply_fake)

    assert mutated is True
    assert _readonly_rows(db_path, "SELECT id, pinned FROM sessions") == [
        ("eligible", 1)
    ]


def test_system_prompt_cleanup_is_selected_only_and_fts_match_negatives_hold(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        db._conn.execute("ALTER TABLE sessions ADD COLUMN system_prompt_hash TEXT")
        db._conn.execute(
            "CREATE TABLE system_prompts (hash TEXT PRIMARY KEY, content TEXT)"
        )
        db._conn.executemany(
            "INSERT INTO system_prompts(hash, content) VALUES (?, ?)",
            [
                ("selected-prompt", "selected"),
                ("shared-prompt", "shared"),
                ("unrelated-orphan", "must survive"),
            ],
        )
        _make_session(
            db,
            "eligible-selected",
            days_ago=90,
            content="deletedmatchtoken",
        )
        _make_session(
            db,
            "eligible-shared",
            days_ago=90,
            content="deletedsharedtoken",
        )
        _make_session(
            db,
            "survivor",
            days_ago=2,
            archived=False,
            ended=False,
            content="survivormatchtoken",
        )
        db._conn.execute(
            "UPDATE sessions SET system_prompt_hash='selected-prompt' WHERE id='eligible-selected'"
        )
        db._conn.execute(
            "UPDATE sessions SET system_prompt_hash='shared-prompt' WHERE id IN ('eligible-shared','survivor')"
        )
        db._conn.commit()
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    _apply(db_path, stage, config, producer, fake)

    assert _readonly_rows(db_path, "SELECT hash FROM system_prompts ORDER BY hash") == [
        ("shared-prompt",),
        ("unrelated-orphan",),
    ]
    assert _readonly_rows(
        db_path,
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'deletedmatchtoken'",
    ) == [(0,)]
    assert _readonly_rows(
        db_path,
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'deletedsharedtoken'",
    ) == [(0,)]
    assert _readonly_rows(
        db_path,
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'survivormatchtoken'",
    ) == [(1,)]
    assert _readonly_rows(
        db_path,
        "SELECT COUNT(*) FROM messages_fts_trigram "
        "WHERE messages_fts_trigram MATCH 'deletedmatchtoken'",
    ) == [(0,)]
    assert _readonly_rows(
        db_path,
        "SELECT COUNT(*) FROM messages_fts_trigram "
        "WHERE messages_fts_trigram MATCH 'survivormatchtoken'",
    ) == [(1,)]


def test_source_bundle_retention_is_clocked_from_cutover_and_exactly_14_days(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, _config, _producer_receipt = _producer(tmp_path, db_path, fake)
    rollback_dir = stage / "rollback"
    bundle = rollback_dir / "rollback-source-bundle.tar.gz"
    source_bundle = rollback_dir / "source-bundle"
    encrypted = rollback_dir / "ROLLBACK-SOURCE-BUNDLE.tar.gz.age"

    awaiting = cold.prune_source_bundle_after_retention(
        stage, candidate_health_confirmed=True, now=NOW + 100 * DAY
    )
    assert awaiting == {"pruned": False, "reason": "awaiting-cutover"}
    assert bundle.exists() and source_bundle.exists()

    cutover = NOW + 101 * DAY
    marker = cold.record_candidate_cutover(
        stage, candidate_health_confirmed=True, now=cutover
    )
    assert marker["cutover_epoch"] == cutover
    before = cold.prune_source_bundle_after_retention(
        stage,
        candidate_health_confirmed=True,
        now=cutover + cold.DEFAULT_SOURCE_BUNDLE_RETENTION_SECONDS - 1,
    )
    assert before["pruned"] is False
    assert bundle.exists() and source_bundle.exists()

    exact = cold.prune_source_bundle_after_retention(
        stage,
        candidate_health_confirmed=True,
        now=cutover + cold.DEFAULT_SOURCE_BUNDLE_RETENTION_SECONDS,
    )
    assert exact["pruned"] is True
    assert not bundle.exists()
    assert not source_bundle.exists()
    assert encrypted.exists()
    assert (stage / "restricted" / "RESTRICTED-COLD-QMD.tar.gz.age").exists()
    assert (stage / "GATE-B-MANIFEST.json").exists()
    assert list((stage / "cold-qmd").glob("*.qmd"))
    assert fake.remote
    replay = cold.prune_source_bundle_after_retention(
        stage,
        candidate_health_confirmed=True,
        now=cutover + cold.DEFAULT_SOURCE_BUNDLE_RETENTION_SECONDS + DAY,
    )
    assert replay["pruned"] is True
    assert replay["replayed"] is True


def test_source_bundle_prune_fails_closed_on_tampered_cutover_marker(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, _config, _producer_receipt = _producer(tmp_path, db_path, fake)
    cutover = NOW + DAY
    cold.record_candidate_cutover(stage, candidate_health_confirmed=True, now=cutover)
    marker_path = stage / "rollback" / "CANDIDATE-CUTOVER.json"
    marker = json.loads(marker_path.read_text())
    marker["rollback_bundle_sha256"] = "0" * 64
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    os.chmod(marker_path, 0o600)

    with pytest.raises(ColdArchiveError, match="does not match retention policy"):
        cold.prune_source_bundle_after_retention(
            stage,
            candidate_health_confirmed=True,
            now=cutover + cold.DEFAULT_SOURCE_BUNDLE_RETENTION_SECONDS,
        )

    assert (stage / "rollback" / "rollback-source-bundle.tar.gz").exists()
    assert (stage / "rollback" / "source-bundle").exists()


def test_apply_replay_does_not_delete_again_or_clobber_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
        _make_session(
            db, "survivor", days_ago=2, archived=False, ended=False, content="hot"
        )
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    first = _apply(db_path, stage, config, producer, fake)
    receipt_path = stage / "COLD-ARCHIVE-RETENTION-RECEIPT.json"
    receipt_sha = _sha(receipt_path)
    second = _apply(db_path, stage, config, producer, fake)

    assert first["retention"]["deleted_sessions"] == 1
    assert second["replayed"] is True
    assert _sha(receipt_path) == receipt_sha
    assert _readonly_rows(db_path, "SELECT id FROM sessions") == [("survivor",)]


def test_surviving_tool_message_is_indexed_and_preserved_in_both_fts_tables(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selectedtooldelete")
        _make_session(
            db,
            "survivor",
            days_ago=2,
            archived=False,
            ended=False,
            content="survivor user content",
        )
        db.append_message(
            "survivor", "tool", "survivingtoolmatchtoken", timestamp=NOW - DAY
        )
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    _apply(db_path, stage, config, producer, fake)

    assert _readonly_rows(
        db_path,
        "SELECT COUNT(*) FROM messages WHERE role='tool' AND content='survivingtoolmatchtoken'",
    ) == [(1,)]
    for table in ("messages_fts", "messages_fts_trigram"):
        assert _readonly_rows(
            db_path,
            f'SELECT COUNT(*) FROM "{table}" WHERE "{table}" MATCH \'survivingtoolmatchtoken\'',
        ) == [(1,)]


def test_survivor_session_and_message_payload_drift_rolls_back_all_changes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
        _make_session(
            db,
            "survivor",
            days_ago=2,
            archived=False,
            ended=False,
            title="original survivor",
            content="survivor payload",
        )
        db._conn.execute(
            """
            CREATE TRIGGER mutate_survivor_payload
            AFTER DELETE ON sessions WHEN OLD.id = 'eligible'
            BEGIN
                UPDATE sessions SET title='mutated title' WHERE id='survivor';
                UPDATE messages SET token_count=999, finish_reason='mutated'
                WHERE session_id='survivor';
            END
            """
        )
        db._conn.commit()
    finally:
        db.close()
    before = _logical_snapshot(db_path)
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)

    with pytest.raises(ColdArchiveError, match="full surviving row payloads changed"):
        _apply(db_path, stage, config, producer, fake)

    assert _logical_snapshot(db_path) == before


def test_survivor_fts_content_drift_rolls_back_and_preserves_match_semantics(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
        _make_session(
            db,
            "survivor",
            days_ago=2,
            archived=False,
            ended=False,
            content="survivorsemanticmatch",
        )
        survivor_message_id = int(
            db._conn.execute(
                "SELECT id FROM messages WHERE session_id='survivor'"
            ).fetchone()[0]
        )
        db._conn.execute(
            f"""
            CREATE TRIGGER mutate_survivor_fts
            AFTER DELETE ON sessions WHEN OLD.id = 'eligible'
            BEGIN
                UPDATE messages_fts SET content='corrupted' WHERE rowid={survivor_message_id};
                UPDATE messages_fts_trigram SET content='corrupted'
                WHERE rowid={survivor_message_id};
            END
            """
        )
        db._conn.commit()
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)

    with pytest.raises(ColdArchiveError, match="search-index invariants changed"):
        _apply(db_path, stage, config, producer, fake)

    for table in ("messages_fts", "messages_fts_trigram"):
        assert _readonly_rows(
            db_path,
            f'SELECT COUNT(*) FROM "{table}" WHERE "{table}" MATCH \'survivorsemanticmatch\'',
        ) == [(1,)]


def test_unselected_candidate_drift_during_remote_preflight_is_rejected(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
        _make_session(
            db,
            "survivor",
            days_ago=2,
            archived=False,
            ended=False,
            title="approved title",
            content="survivor",
        )
    finally:
        db.close()
    producer_fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, producer_fake)
    mutated = False

    def mutate(step: str) -> None:
        nonlocal mutated
        if step == "check" and not mutated:
            mutated = True
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE sessions SET title='unapproved' WHERE id='survivor'"
                )
                conn.commit()
            finally:
                conn.close()

    apply_fake = FakeRclone(callback=mutate)
    apply_fake.remote = dict(producer_fake.remote)
    with pytest.raises(ColdArchiveError, match="logical snapshot differs"):
        _apply(db_path, stage, config, producer, apply_fake)

    assert _readonly_rows(db_path, "SELECT id FROM sessions ORDER BY id") == [
        ("eligible",),
        ("survivor",),
    ]


def test_candidate_replacement_during_remote_preflight_is_never_deleted(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    approved_db = _build_archive_candidate(db_path)
    try:
        _make_session(approved_db, "eligible", days_ago=90, content="approved")
    finally:
        approved_db.close()
    producer_fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, producer_fake)

    replacement_path = tmp_path / "replacement.db"
    replacement = _build_archive_candidate(replacement_path)
    try:
        _make_session(
            replacement, "replacement-row", days_ago=90, content="replacement"
        )
    finally:
        replacement.close()
    approved_saved = tmp_path / "approved-saved.db"
    swapped = False

    def swap(step: str) -> None:
        nonlocal swapped
        if step == "check" and not swapped:
            swapped = True
            os.replace(db_path, approved_saved)
            os.replace(replacement_path, db_path)

    apply_fake = FakeRclone(callback=swap)
    apply_fake.remote = dict(producer_fake.remote)
    with pytest.raises(ColdArchiveError, match="device/inode differs"):
        _apply(db_path, stage, config, producer, apply_fake)

    assert swapped is True
    assert _readonly_rows(db_path, "SELECT id FROM sessions") == [("replacement-row",)]
    assert _readonly_rows(approved_saved, "SELECT id FROM sessions") == [("eligible",)]


def test_approved_snapshot_is_rechecked_after_connection_before_begin_immediate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    original_connect = cold._connect_candidate

    def connect_then_mutate(path: Path) -> sqlite3.Connection:
        candidate = original_connect(path)
        writer = sqlite3.connect(path)
        try:
            writer.execute("UPDATE sessions SET pinned=1 WHERE id='eligible'")
            writer.commit()
        finally:
            writer.close()
        return candidate

    monkeypatch.setattr(cold, "_connect_candidate", connect_then_mutate)
    with pytest.raises(
        ColdArchiveError, match="logical snapshot changed before destructive lock"
    ):
        _apply(db_path, stage, config, producer, fake)

    assert _readonly_rows(db_path, "SELECT id, pinned FROM sessions") == [
        ("eligible", 1)
    ]


def test_committed_apply_recovers_receipt_from_prepared_record_after_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
        _make_session(
            db, "survivor", days_ago=2, archived=False, ended=False, content="hot"
        )
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    original_write = cold._exclusive_write_json
    failed = False

    def fail_final_receipt(path: Path, payload: dict[str, Any]) -> Path:
        nonlocal failed
        if path.name == "COLD-ARCHIVE-RETENTION-RECEIPT.json" and not failed:
            failed = True
            raise ColdArchiveError("forced final receipt write failure")
        return original_write(path, payload)

    monkeypatch.setattr(cold, "_exclusive_write_json", fail_final_receipt)
    with pytest.raises(ColdArchiveError, match="forced final receipt write failure"):
        _apply(db_path, stage, config, producer, fake)

    assert failed is True
    assert (stage / "COLD-ARCHIVE-APPLY-PREPARED.json").exists()
    assert not (stage / "COLD-ARCHIVE-RETENTION-RECEIPT.json").exists()
    assert _readonly_rows(db_path, "SELECT id FROM sessions") == [("survivor",)]

    monkeypatch.setattr(cold, "_exclusive_write_json", original_write)
    recovered = _apply(db_path, stage, config, producer, fake)
    assert recovered["recovered_from_prepared"] is True
    assert (stage / "COLD-ARCHIVE-RETENTION-RECEIPT.json").exists()
    assert _readonly_rows(db_path, "SELECT id FROM sessions") == [("survivor",)]


@pytest.mark.parametrize("partial_index", [1, 2, 3])
def test_each_remote_object_position_rejects_true_partial_readback(
    tmp_path: Path, partial_index: int
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
    finally:
        db.close()
    fake = FakeRclone(
        partial_upload_index=partial_index,
        lie_on_check=True,
    )

    with pytest.raises(ColdArchiveError, match="readback size mismatch"):
        _producer(tmp_path, db_path, fake)

    assert fake.upload_count == partial_index
    assert _readonly_rows(db_path, "SELECT id FROM sessions") == [("eligible",)]
    assert {command[1] for _step, command in fake.commands} <= {"copyto", "check"}


def test_successful_full_producer_never_calls_apply_or_changes_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
    finally:
        db.close()
    before = _logical_snapshot(db_path)

    def forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("producer must never call destructive apply")

    monkeypatch.setattr(cold, "apply_retention_to_candidate", forbidden)
    _producer(tmp_path, db_path, FakeRclone())
    assert _logical_snapshot(db_path) == before


def test_selected_prompt_delete_failure_rolls_back_sessions_messages_fts_and_prompts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        db._conn.execute("ALTER TABLE sessions ADD COLUMN system_prompt_hash TEXT")
        db._conn.execute(
            "CREATE TABLE system_prompts (hash TEXT PRIMARY KEY, content TEXT)"
        )
        db._conn.execute(
            "INSERT INTO system_prompts(hash, content) VALUES ('selected-prompt', 'body')"
        )
        _make_session(db, "eligible", days_ago=90, content="selectedpromptmatch")
        db._conn.execute(
            "UPDATE sessions SET system_prompt_hash='selected-prompt' WHERE id='eligible'"
        )
        db._conn.execute(
            """
            CREATE TRIGGER fail_selected_prompt_delete
            BEFORE DELETE ON system_prompts WHEN OLD.hash='selected-prompt'
            BEGIN
                SELECT RAISE(ABORT, 'prompt delete denied');
            END
            """
        )
        db._conn.commit()
    finally:
        db.close()
    before = _logical_snapshot(db_path)
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)

    with pytest.raises(sqlite3.IntegrityError, match="prompt delete denied"):
        _apply(db_path, stage, config, producer, fake)

    assert _logical_snapshot(db_path) == before
    assert not (stage / "COLD-ARCHIVE-APPLY-PREPARED.json").exists()


def test_wrong_external_sha_and_manifest_copy_outside_stage_are_rejected(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
    finally:
        db.close()
    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)

    with pytest.raises(ColdArchiveError, match="exact-byte sha256 mismatch"):
        cold.run_cold_archive_pass(
            source_db=db_path,
            stage_root=stage,
            apply_retention=True,
            approved_manifest_path=stage / "GATE-B-MANIFEST.json",
            approved_manifest_sha256="0" * 64,
            rclone_config=config,
            rclone_runner=fake,
        )

    copied = tmp_path / "approved-copy.json"
    copied.write_bytes((stage / "GATE-B-MANIFEST.json").read_bytes())
    os.chmod(copied, 0o600)
    with pytest.raises(ColdArchiveError, match="must be the stage"):
        cold.run_cold_archive_pass(
            source_db=db_path,
            stage_root=stage,
            apply_retention=True,
            approved_manifest_path=copied,
            approved_manifest_sha256=producer["manifest_file_sha256"],
            rclone_config=config,
            rclone_runner=fake,
        )
    assert _readonly_rows(db_path, "SELECT id FROM sessions") == [("eligible",)]


def test_source_and_stage_symlinks_fail_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="selected")
    finally:
        db.close()
    source_link = tmp_path / "candidate-link.db"
    source_link.symlink_to(db_path)
    with pytest.raises(ColdArchiveError, match="symlink path component"):
        cold.run_cold_archive_pass(
            source_db=source_link,
            stage_root=tmp_path / "link-source-stage",
            now=NOW,
            hold_sources=[],
            manifest_only=True,
        )

    fake = FakeRclone()
    stage, _recipient, config, producer = _producer(tmp_path, db_path, fake)
    stage_link = tmp_path / "stage-link"
    stage_link.symlink_to(stage, target_is_directory=True)
    with pytest.raises(ColdArchiveError, match="symlink path component"):
        cold.run_cold_archive_pass(
            source_db=db_path,
            stage_root=stage_link,
            apply_retention=True,
            approved_manifest_path=stage / "GATE-B-MANIFEST.json",
            approved_manifest_sha256=producer["manifest_file_sha256"],
            rclone_config=config,
            rclone_runner=fake,
        )


def test_rollback_bundle_contains_nonempty_wal_and_restores_wal_backed_row(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.db"
    db = _build_archive_candidate(db_path)
    try:
        _make_session(db, "eligible", days_ago=90, content="wal selected")
    finally:
        db.close()
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "UPDATE sessions SET title='wal-backed-title' WHERE id='eligible'"
        )
        writer.commit()
        wal_path = db_path.with_name(db_path.name + "-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0

        fake = FakeRclone()
        stage, _recipient, _config, producer = _producer(tmp_path, db_path, fake)
        copied = {
            item["name"]: item
            for item in producer["rollback_bundle"]["files"]
            if item["status"] == "copied"
        }
        assert copied[db_path.name + "-wal"]["bytes"] > 0

        restored = tmp_path / "restored-wal.db"
        bundle = stage / "rollback" / "rollback-source-bundle.tar.gz"
        with tarfile.open(bundle, "r:gz") as archive:
            for source_name, item in copied.items():
                member = archive.extractfile(source_name)
                assert member is not None
                data = member.read()
                assert hashlib.sha256(data).hexdigest() == item["sha256"]
                suffix = source_name[len(db_path.name) :]
                target = restored.with_name(restored.name + suffix)
                target.write_bytes(data)
                os.chmod(target, 0o600)
        restored_conn = sqlite3.connect(restored)
        try:
            assert restored_conn.execute(
                "SELECT title FROM sessions WHERE id='eligible'"
            ).fetchone() == ("wal-backed-title",)
            assert restored_conn.execute("PRAGMA integrity_check").fetchall() == [
                ("ok",)
            ]
            assert restored_conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            restored_conn.close()
    finally:
        writer.close()


def test_cli_help_requires_external_approval_flags_and_has_no_clock_override() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from hermes_cli.main import main; "
                "sys.argv=['hermes','sessions','cold-archive','--help']; main()"
            ),
        ],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--approved-manifest APPROVED_MANIFEST" in result.stdout
    assert "--approved-manifest-sha256 APPROVED_MANIFEST_SHA256" in result.stdout
    assert "--now-epoch" not in result.stdout
