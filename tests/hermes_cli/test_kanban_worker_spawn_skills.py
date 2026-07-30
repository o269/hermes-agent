"""Focused regressions for task-skill normalization at worker spawn."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from hermes_cli import kanban_db as kb


_NO_DB_OVERRIDE = object()


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Keep the real worker-argv builder deterministic without stubbing its
    # toolset resolver: the explicit list limits it to mandatory lifecycle
    # additions instead of inheriting a platform composite.
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  cli: []\n", encoding="utf-8"
    )
    for name in (
        "HERMES_KANBAN_BROKER",
        "BOARDD_SOCK",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
    ):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    return home


def _task(skills: object) -> kb.Task:
    return kb.Task(
        id="t_spawn_skills",
        title="spawn skills",
        body=None,
        assignee="worker",
        status="running",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        skills=cast(Any, skills),
    )


def _skill_names(cmd: list[str]) -> list[str]:
    return [
        cmd[index + 1]
        for index, token in enumerate(cmd)
        if token == "--skills" and index + 1 < len(cmd)
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["alpha", "beta"], ["alpha", "beta"]),
        (("alpha", "beta"), ["alpha", "beta"]),
        ('["alpha", "beta"]', ["alpha", "beta"]),
        ("alpha", ["alpha"]),
        ("alpha, beta", ["alpha", "beta"]),
        (None, []),
        ([], []),
        ((), []),
        ("[]", []),
        ("   ", []),
        (
            [" category/alpha ", "plugin:beta", "category/alpha", "plugin:beta "],
            ["category/alpha", "plugin:beta"],
        ),
        (
            ' [" category/alpha ", "plugin:beta", "category/alpha"] ',
            ["category/alpha", "plugin:beta"],
        ),
    ],
    ids=(
        "typed-list",
        "typed-tuple",
        "json-array-string",
        "single-string",
        "comma-delimited-string",
        "none",
        "empty-list",
        "empty-tuple",
        "empty-json-array",
        "blank-string",
        "typed-whitespace-duplicates",
        "json-whitespace-duplicates",
    ),
)
def test_default_spawn_emits_normalized_skill_pairs(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: object,
    expected: list[str],
) -> None:
    captured: dict[str, list[str]] = {}

    class FakeProc:
        pid = 42

    def fake_popen(cmd: list[str], **_kwargs: object) -> FakeProc:
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(
        kb, "_read_worker_process_identity", lambda _pid: (None, None, None)
    )
    monkeypatch.setattr(kb, "_read_host_boot_id", lambda: None)

    pid = kb._default_spawn(_task(raw), str(tmp_path))

    assert int(pid or 0) == 42
    cmd = captured["cmd"]
    assert _skill_names(cmd) == expected
    if expected:
        assert max(
            index for index, token in enumerate(cmd) if token == "--skills"
        ) < cmd.index("chat")


@pytest.mark.parametrize(
    "raw",
    [
        '["alpha"',
        '{"skill": "alpha"',
        '"alpha',
    ],
    ids=("array", "object", "quoted-string"),
)
def test_normalizer_rejects_malformed_json_looking_values(raw: str) -> None:
    with pytest.raises(
        ValueError, match=r"task t_bad has invalid skills .*malformed JSON"
    ):
        kb._normalize_spawn_skills(raw, task_id="t_bad")


@pytest.mark.parametrize(
    "raw",
    [
        '{"skill": "alpha"}',
        '"alpha"',
        "42",
        "true",
        "null",
    ],
    ids=("object", "string", "number", "boolean", "null"),
)
def test_normalizer_rejects_non_array_json(raw: str) -> None:
    with pytest.raises(
        ValueError, match=r"task t_bad has invalid skills .*must be an array"
    ):
        kb._normalize_spawn_skills(raw, task_id="t_bad")


@pytest.mark.parametrize(
    "raw",
    [
        ["alpha", 1],
        ["alpha", None],
        '["alpha", 1]',
        ["alpha", ""],
        ["alpha", "   "],
        "alpha,,beta",
        "alpha,",
    ],
    ids=(
        "typed-integer",
        "typed-none",
        "json-integer",
        "typed-empty",
        "typed-blank",
        "plain-empty-middle",
        "plain-empty-tail",
    ),
)
def test_normalizer_rejects_non_string_or_empty_members(raw: object) -> None:
    with pytest.raises(ValueError, match=r"task t_bad has invalid skills"):
        kb._normalize_spawn_skills(raw, task_id="t_bad")


@pytest.mark.parametrize(
    "raw",
    [
        {"alpha"},
        {"skill": "alpha"},
        7,
        True,
    ],
    ids=("set", "mapping", "integer", "boolean"),
)
def test_normalizer_rejects_unsupported_types(raw: object) -> None:
    with pytest.raises(
        ValueError, match=r"task t_bad has invalid skills .*unsupported"
    ):
        kb._normalize_spawn_skills(raw, task_id="t_bad")


@pytest.mark.parametrize(
    "name",
    [
        "bad skill",
        "bad$skill",
        "/absolute",
        "relative/../escape",
        "plugin:",
        "alpha\\beta",
    ],
)
def test_normalizer_rejects_invalid_identifiers(name: str) -> None:
    with pytest.raises(
        ValueError, match=r"task t_bad has invalid skills .*valid identifier"
    ):
        kb._normalize_spawn_skills([name], task_id="t_bad")


def test_normalizer_error_is_bounded_and_names_task() -> None:
    with pytest.raises(ValueError) as exc_info:
        kb._normalize_spawn_skills(
            ["bad skill" + ("x" * 10_000)],
            task_id="t_" + ("z" * 10_000),
        )

    message = str(exc_info.value)
    assert message.startswith("task t_zzz")
    assert "expected None, list[str]/tuple[str, ...]" in message
    assert len(message) < 400
    assert "x" * 100 not in message


def test_default_spawn_rejects_invalid_input_before_popen(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popen_called = False

    def fake_popen(*_args: object, **_kwargs: object) -> None:
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen must not run for invalid skills")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(ValueError, match=r"task t_spawn_skills has invalid skills"):
        kb._default_spawn(_task('["unterminated"'), str(tmp_path))

    assert popen_called is False


@pytest.mark.parametrize(
    ("created_skills", "stored_skills", "expected"),
    [
        (["alpha", "beta"], _NO_DB_OVERRIDE, ["alpha", "beta"]),
        (("alpha", "beta"), _NO_DB_OVERRIDE, ["alpha", "beta"]),
        (["seed"], "alpha", ["alpha"]),
        (["seed"], " alpha, beta ", ["alpha", "beta"]),
        (
            ["seed"],
            '[" alpha ", "beta", "alpha", "beta "]',
            ["alpha", "beta"],
        ),
    ],
    ids=(
        "typed-list",
        "typed-tuple",
        "plain-single",
        "comma-delimited",
        "stored-json-stable-dedupe",
    ),
)
def test_real_db_claim_dispatch_emits_exact_normalized_argv(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    created_skills: object,
    stored_skills: object,
    expected: list[str],
) -> None:
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeProc:
        pid = 2_000_000_000

    def fake_popen(cmd: list[str], **kwargs: object) -> FakeProc:
        popen_calls.append((list(cmd), dict(kwargs)))
        return FakeProc()

    # This is the sole production-boundary stub: task creation, row decoding,
    # claim_task, workspace resolution, dispatch, argv construction, and spawn
    # bookkeeping all run through their real implementations.
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="real DB skills dispatch",
            assignee="default",
            skills=cast(Any, created_skills),
        )
        if stored_skills is not _NO_DB_OVERRIDE:
            conn.execute(
                "UPDATE tasks SET skills = ? WHERE id = ?",
                (stored_skills, task_id),
            )
            conn.commit()

        result = kb.dispatch_once(conn, failure_limit=2)

        assert result.spawned == [(task_id, "default", result.spawned[0][2])]
        assert result.auto_blocked == []
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None

    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    expected_cmd = [
        *kb._resolve_hermes_argv(),
        "-p",
        "default",
        "--cli",
        "--accept-hooks",
    ]
    for skill in expected:
        expected_cmd.extend(["--skills", skill])
    spawn_env = cast(dict[str, str], kwargs["env"])
    worker_toolsets = kb._resolve_worker_cli_toolsets(spawn_env.get("HERMES_HOME"))
    if worker_toolsets:
        expected_cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    expected_cmd.extend(["chat", "-q", f"work kanban task {task_id}"])
    assert cmd == expected_cmd
    assert kwargs["cwd"] == result.spawned[0][2]


@pytest.mark.parametrize(
    ("stored_skills", "reason"),
    [
        ('["alpha"', "malformed JSON-looking value"),
        ('{"skill": "alpha"}', "JSON value must be an array"),
        ('"alpha"', "JSON value must be an array"),
        ('["alpha", 1]', "members must be strings"),
        ('["alpha", ""]', "members must be non-empty"),
        ("alpha,,beta", "members must be non-empty"),
        ('["bad skill"]', "member is not a valid identifier"),
        (b'["alpha"]', "unsupported value type"),
        (
            '["bad skill' + ("x" * 10_000) + '"]',
            "member is not a valid identifier",
        ),
    ],
    ids=(
        "malformed-json-looking",
        "json-object",
        "json-scalar",
        "mixed-non-string-array",
        "empty-json-member",
        "empty-delimited-member",
        "invalid-identifier",
        "unsupported-db-blob",
        "bounded-large-invalid-member",
    ),
)
def test_real_db_claim_dispatch_rejects_invalid_skills_before_popen(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored_skills: object,
    reason: str,
) -> None:
    popen_calls: list[object] = []

    def fail_popen(*args: object, **_kwargs: object) -> None:
        popen_calls.append(args)
        raise AssertionError("Popen must not run for invalid stored skills")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="invalid legacy DB skills",
            assignee="default",
            skills=["seed"],
        )
        conn.execute(
            "UPDATE tasks SET skills = ? WHERE id = ?",
            (stored_skills, task_id),
        )
        conn.commit()

        first = kb.dispatch_once(conn, failure_limit=2)

        assert first.spawned == []
        assert first.auto_blocked == []
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        error = task.last_failure_error
        assert error is not None
        assert error.startswith(f"task {task_id} has invalid skills ({reason})")
        assert len(error) < 400
        assert "\n" not in error
        assert "x" * 100 not in error

        first_run = conn.execute(
            "SELECT status, outcome, error FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert first_run is not None
        assert first_run["status"] == "spawn_failed"
        assert first_run["outcome"] == "spawn_failed"
        assert first_run["error"] == error

        second = kb.dispatch_once(conn, failure_limit=2)

        assert second.spawned == []
        assert second.auto_blocked == [task_id]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 2
        assert task.last_failure_error == error
        latest_run = conn.execute(
            "SELECT status, outcome, error FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert latest_run is not None
        assert latest_run["status"] == "gave_up"
        assert latest_run["outcome"] == "gave_up"
        assert latest_run["error"] == error

    assert popen_calls == []
