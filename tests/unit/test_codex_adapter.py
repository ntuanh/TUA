"""Unit tests for the Codex adapter's argv, safety guards, and event normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tua.adapters.base import AdapterRequest
from tua.adapters.codex import (
    MAX_INSTRUCTIONS_BYTES,
    CodexAdapter,
    assert_safe_argv,
    normalize,
    parse_events,
    toml_string,
)
from tua.errors import ProviderError
from tua.models import ErrorType, TaskStatus
from tua.process import ProcessOutcome


def request(tmp_path: Path, **overrides: Any) -> AdapterRequest:
    skills = tmp_path / "skills.md"
    skills.write_text('# Rules\nSay "hi" and use ünïcode.\n')
    values: dict[str, Any] = {
        "task_id": "t",
        "prompt": "the prompt",
        "worktree": tmp_path / "wt",
        "compiled_skill_path": skills,
        "compiled_skill_hash": "sha256:x",
        "settings_path": None,
        "permission_mode": "",
        "cli_deny": (),
        "output_dir": tmp_path,
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return AdapterRequest(**values)


def outcome(stdout: str = "", stderr: str = "", code: int | None = 0, **kw: Any) -> ProcessOutcome:
    values: dict[str, Any] = {
        "exit_code": code,
        "stdout": stdout.encode(),
        "stderr": stderr.encode(),
        "stdout_truncated": False,
        "stderr_truncated": False,
        "timed_out": False,
        "cancelled": False,
        "duration_seconds": 2.0,
        "pid": 7,
    }
    values.update(kw)
    return ProcessOutcome(**values)


def norm(o: ProcessOutcome, last: str | None = None) -> Any:
    return normalize(
        o,
        stdout_text=o.stdout.decode(),
        stderr_text=o.stderr.decode(),
        last_message=last,
        cli_version="0.164.0",
        model_alias="gpt-6-luna",
        started_at="s",
        ended_at="e",
        stdout_path=None,
        stderr_path=None,
    )


def events(*items: dict[str, Any]) -> str:
    return "\n".join(json.dumps(i) for i in items) + "\n"


OK_EVENTS = events(
    {"type": "thread.started", "thread_id": "th-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "All done."}},
    {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
)


def failed(status: int, message: str) -> str:
    inner = json.dumps({"status": status, "error": {"message": message}})
    return events(
        {"type": "thread.started", "thread_id": "th-2"},
        {"type": "turn.failed", "error": {"message": inner}},
    )


def test_argv_for_safe_profile(tmp_path: Path) -> None:
    argv = CodexAdapter(scratch_dir=tmp_path).build_argv(
        request(tmp_path, model="gpt-6-luna", effort="medium"), "/bin/codex"
    )
    assert argv[:3] == ["/bin/codex", "exec", "--json"]
    for flag in ("--ignore-user-config", "--ignore-rules", "--strict-config"):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--cd") + 1] == str(tmp_path / "wt")
    assert argv[argv.index("--model") + 1] == "gpt-6-luna"
    assert 'approval_policy="never"' in argv
    assert "sandbox_workspace_write.network_access=false" in argv
    assert 'model_reasoning_effort="medium"' in argv
    instructions = next(a for a in argv if a.startswith("developer_instructions="))
    assert json.loads(instructions.split("=", 1)[1]) == '# Rules\nSay "hi" and use ünïcode.\n'
    assert argv[-1] == "-"  # prompt on stdin
    assert "the prompt" not in argv
    assert not any("dangerously" in a for a in argv)


def test_read_only_profile_uses_read_only_sandbox(tmp_path: Path) -> None:
    argv = CodexAdapter(scratch_dir=tmp_path).build_argv(
        request(tmp_path, permission_profile="read-only"), "codex"
    )
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--model" not in argv


@pytest.mark.parametrize(
    "argv",
    [
        ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
        ["codex", "exec", "--sandbox", "danger-full-access"],
        ["codex", "exec", "--sandbox=danger-full-access"],
        ["codex", "exec", "--approve-for-me"],
    ],
)
def test_unsafe_argv_is_refused(argv: list[str]) -> None:
    with pytest.raises(ProviderError):
        assert_safe_argv(argv)


def test_invalid_effort_profile_and_oversized_skills(tmp_path: Path) -> None:
    adapter = CodexAdapter(scratch_dir=tmp_path)
    with pytest.raises(ProviderError, match="effort"):
        adapter.build_argv(request(tmp_path, effort="max"), "codex")
    with pytest.raises(ProviderError, match="sandbox"):
        adapter.build_argv(request(tmp_path, permission_profile="admin"), "codex")
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (MAX_INSTRUCTIONS_BYTES + 1))
    with pytest.raises(ProviderError, match="limits"):
        adapter.build_argv(request(tmp_path, compiled_skill_path=big), "codex")


def test_toml_string_escaping() -> None:
    value = 'a "quoted"\\ line\nnext\ttab ü'
    encoded = toml_string(value)
    assert encoded.startswith('"')
    assert "\n" not in encoded
    assert json.loads(encoded) == value


def test_parse_events() -> None:
    summary = parse_events("noise\n" + OK_EVENTS + "{bad json\n")
    assert summary["thread_id"] == "th-1"
    assert summary["last_message"] == "All done."
    assert summary["completed"] is True
    assert summary["errors"] == []
    assert summary["usage"] == {"input_tokens": 5, "output_tokens": 2}


def test_success() -> None:
    result = norm(outcome(OK_EVENTS), last="Final answer from -o file.")
    assert result.status is TaskStatus.COMPLETED
    assert result.result_text == "Final answer from -o file."
    assert result.session_id == "th-1"
    assert result.usage == {"input_tokens": 5, "output_tokens": 2}
    # Codex does not report these reliably, so they stay unknown.
    assert result.actual_models is None
    assert result.cost_usd is None
    assert result.permission_denials is None
    assert result.model_alias == "gpt-6-luna"


@pytest.mark.parametrize(
    ("stdout", "code", "expected", "text"),
    [
        (
            failed(400, "The model is not supported with a ChatGPT account."),
            1,
            ErrorType.PROVIDER_ERROR,
            "not supported",
        ),
        (failed(429, "You've hit your usage limit."), 1, ErrorType.RATE_LIMITED, "usage limit"),
        (failed(401, "Unauthorized"), 1, ErrorType.UNAUTHENTICATED, "Unauthorized"),
        ("not events\n", 0, ErrorType.INVALID_OUTPUT, "no result event"),
        ("", 101, ErrorType.PROVIDER_ERROR, "panicked"),
    ],
)
def test_failures(stdout: str, code: int, expected: ErrorType, text: str) -> None:
    result = norm(outcome(stdout, "thread 'main' panicked" if code == 101 else "", code=code))
    assert result.status is TaskStatus.FAILED
    assert result.error_type is expected
    assert text in (result.error_message or "")


def test_exit_zero_without_turn_completed_is_not_success() -> None:
    partial = events({"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"})
    assert norm(outcome(partial)).status is TaskStatus.FAILED
    assert norm(outcome(partial)).error_type is ErrorType.INVALID_OUTPUT


def test_timeout_cancel_spawn() -> None:
    assert norm(outcome("", code=-15, timed_out=True)).status is TaskStatus.TIMEOUT
    assert norm(outcome("", code=-15, cancelled=True)).status is TaskStatus.CANCELLED
    spawn = norm(outcome("", code=None, pid=None, spawn_error="FileNotFoundError: x"))
    assert spawn.error_type is ErrorType.CLI_MISSING
