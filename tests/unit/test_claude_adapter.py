"""Unit tests for argv construction and result normalization (no process is started)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tua.adapters.base import AdapterRequest
from tua.adapters.claude_code import (
    ClaudeCodeAdapter,
    assert_safe_argv,
    classify_error_text,
    normalize,
    parse_result_json,
    parse_version,
)
from tua.errors import ProviderError
from tua.models import ErrorType, TaskStatus
from tua.process import ProcessOutcome

FIXTURE = Path(__file__).parents[1] / "fixtures" / "claude_result_success.json"


def request(tmp_path: Path, **overrides: Any) -> AdapterRequest:
    values: dict[str, Any] = {
        "task_id": "t",
        "prompt": "secret-free prompt",
        "worktree": tmp_path,
        "compiled_skill_path": tmp_path / "c.md",
        "compiled_skill_hash": "sha256:x",
        "settings_path": tmp_path / "s.json",
        "permission_mode": "dontAsk",
        "cli_deny": ("WebFetch", "Bash(git push *)"),
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
        "duration_seconds": 1.25,
        "pid": 42,
    }
    values.update(kw)
    return ProcessOutcome(**values)


def norm(o: ProcessOutcome) -> Any:
    return normalize(
        o,
        stdout_text=o.stdout.decode(),
        stderr_text=o.stderr.decode(),
        cli_version="2.1.301",
        model_alias="sonnet",
        started_at="s",
        ended_at="e",
        stdout_path=None,
        stderr_path=None,
    )


def result_json(**overrides: Any) -> str:
    data = json.loads(FIXTURE.read_text())
    data.update(overrides)
    return json.dumps(data)


def test_argv_uses_headless_flags_and_never_bypasses(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(scratch_dir=tmp_path)
    argv = adapter.build_argv(request(tmp_path, model="sonnet", max_turns=7), "/bin/claude")
    assert argv[:4] == ["/bin/claude", "--print", "--output-format", "json"]
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(tmp_path / "c.md")
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--disallowedTools") :] == [
        "--disallowedTools",
        "WebFetch",
        "Bash(git push *)",
    ]
    assert "secret-free prompt" not in argv  # the prompt goes through stdin
    assert not any("dangerously" in a for a in argv)


def test_optional_flags_are_omitted(tmp_path: Path) -> None:
    argv = ClaudeCodeAdapter(scratch_dir=tmp_path).build_argv(request(tmp_path), "claude")
    assert "--model" not in argv
    assert "--max-turns" not in argv


@pytest.mark.parametrize(
    "argv",
    [
        ["claude", "--dangerously-skip-permissions"],
        ["claude", "--allow-dangerously-skip-permissions"],
        ["claude", "--permission-mode", "bypassPermissions"],
        ["claude", "--permission-mode=bypassPermissions"],
    ],
)
def test_bypass_is_refused(argv: list[str]) -> None:
    with pytest.raises(ProviderError):
        assert_safe_argv(argv)


def test_bypass_mode_cannot_come_from_a_request(tmp_path: Path) -> None:
    with pytest.raises(ProviderError):
        ClaudeCodeAdapter(scratch_dir=tmp_path).build_argv(
            request(tmp_path, permission_mode="bypassPermissions"), "claude"
        )


def test_parse_version() -> None:
    assert parse_version("2.1.301 (Claude Code)\n") == "2.1.301"
    assert parse_version("garbage") is None


def test_parse_result_json_variants() -> None:
    good = result_json()
    assert parse_result_json(good) is not None
    assert parse_result_json("warning: something\n" + good + "\n") is not None
    assert parse_result_json("not json") is None
    assert parse_result_json('{"type": "assistant"}') is None
    assert parse_result_json("") is None


def test_success_is_normalized_from_real_shape() -> None:
    result = norm(outcome(result_json()))
    assert result.status is TaskStatus.COMPLETED
    assert result.error_type is None
    assert result.session_id
    assert result.num_turns == json.loads(FIXTURE.read_text())["num_turns"]
    assert result.actual_models
    assert all(isinstance(m, str) for m in result.actual_models)
    assert result.permission_denials == ("Bash",)
    assert result.actual_models == ("claude-haiku-5-5",)
    assert result.cost_usd is not None
    assert result.extra["terminal_reason"] == "completed"
    assert result.cli_version == "2.1.301"
    assert result.model_alias == "sonnet"


def test_is_error_is_never_reported_as_success() -> None:
    result = norm(outcome(result_json(is_error=True, subtype="error_during_execution"), code=1))
    assert result.status is TaskStatus.FAILED
    assert result.error_type is ErrorType.PROVIDER_ERROR
    assert "error_during_execution" in (result.error_message or "")
    # Exit code 0 with is_error=true is still a failure.
    assert norm(outcome(result_json(is_error=True))).status is TaskStatus.FAILED
    # A non-zero exit with an apparently successful payload is still a failure.
    assert norm(outcome(result_json(), code=1)).status is TaskStatus.FAILED
    assert norm(outcome(result_json(subtype="error_max_turns"))).status is TaskStatus.FAILED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Invalid API key · Please run /login", ErrorType.UNAUTHENTICATED),
        ("OAuth token has expired", ErrorType.UNAUTHENTICATED),
        ("Claude AI usage limit reached|1700000000", ErrorType.RATE_LIMITED),
        ('API Error: 429 {"type":"rate_limit_error"}', ErrorType.RATE_LIMITED),
        ("I implemented rate limiting for the API.", None),
        ("Could not finish", None),
    ],
)
def test_error_classification(text: str, expected: ErrorType | None) -> None:
    assert classify_error_text(text) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ErrorType.UNAUTHENTICATED),
        (429, ErrorType.RATE_LIMITED),
        (500, ErrorType.PROVIDER_ERROR),
    ],
)
def test_structured_api_status_is_preferred(status: int, expected: ErrorType) -> None:
    payload = result_json(is_error=True, api_error_status=status, result="opaque failure")
    result = norm(outcome(payload, code=1))
    assert result.status is TaskStatus.FAILED
    assert result.error_type is expected


def test_auth_and_rate_limit_results() -> None:
    auth = norm(
        outcome(result_json(is_error=True, result="Invalid API key · Please run /login"), code=1)
    )
    assert auth.error_type is ErrorType.UNAUTHENTICATED
    rate = norm(
        outcome(result_json(is_error=True, result="Claude AI usage limit reached|1"), code=1)
    )
    assert rate.error_type is ErrorType.RATE_LIMITED


def test_invalid_and_missing_output() -> None:
    assert norm(outcome("not json")).error_type is ErrorType.INVALID_OUTPUT
    crash = norm(outcome("", "boom", code=3))
    assert crash.status is TaskStatus.FAILED
    assert crash.error_type is ErrorType.PROVIDER_ERROR
    assert "boom" in (crash.error_message or "")
    assert norm(outcome("", "Not logged in", code=1)).error_type is ErrorType.UNAUTHENTICATED


def test_timeout_cancel_and_spawn_failure() -> None:
    timed = norm(outcome("", code=-15, timed_out=True))
    assert timed.status is TaskStatus.TIMEOUT
    assert timed.error_type is ErrorType.TIMEOUT
    cancelled = norm(outcome("", code=-15, cancelled=True))
    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.error_type is ErrorType.CANCELLED
    spawn = norm(outcome("", code=None, pid=None, spawn_error="FileNotFoundError: nope"))
    assert spawn.status is TaskStatus.FAILED
    assert spawn.error_type is ErrorType.CLI_MISSING


def test_unreliable_metadata_is_left_unknown() -> None:
    payload = result_json(
        modelUsage={"bad key with spaces": {}},
        num_turns=True,
        total_cost_usd="1.0",
        permission_denials="x",
    )
    result = norm(outcome(payload))
    assert result.actual_models is None
    assert result.num_turns is None
    assert result.cost_usd is None
    assert result.permission_denials is None
    minimal = norm(outcome('{"type": "result", "subtype": "success", "is_error": false}'))
    assert minimal.status is TaskStatus.COMPLETED
    assert minimal.actual_models is None
    assert minimal.session_id is None


def test_result_excerpt_is_redacted() -> None:
    token = "gh" + "p_" + "Q" * 36
    result = norm(outcome(result_json(result=f"done, token {token}")))
    assert token not in (result.result_excerpt or "")
