"""Claude Code CLI adapter (headless ``claude --print`` with JSON output).

Verified against Claude Code 2.1.301. ``--append-system-prompt-file`` and ``--max-turns``
are accepted by that version but hidden from ``--help``, so TUA never trusts the help
text: before every task it runs the exact flag set with no input (stdin is /dev/null).
Without input the CLI cannot reach the API, so this preflight costs no quota. An unknown
or invalid flag fails the task before any worktree work happens.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tua.adapters.base import AdapterRequest, AdapterResult, CliInfo
from tua.errors import ProviderError
from tua.logging_utils import excerpt, redact
from tua.models import ErrorType, TaskStatus, utc_now
from tua.paths import write_private_file
from tua.process import DEFAULT_GRACE_SECONDS, ProcessOutcome, child_env, run_process

PROVIDER_NAME = "claude-code"
PREFLIGHT_TIMEOUT_SECONDS = 30
MAX_STDOUT_BYTES = 16 * 1024 * 1024

# Never passed by TUA; build_argv refuses to produce them even if a caller asked.
FORBIDDEN_FLAGS = frozenset(
    {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}
)
FORBIDDEN_PERMISSION_MODES = frozenset({"bypassPermissions"})

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?)")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]{0,127}$")
_UNKNOWN_OPTION_RE = re.compile(r"unknown option '([^']+)'")
_NO_INPUT_MARKER = "Input must be provided"

# Only distinctive provider/API error phrases; model-written prose is not classified.
_AUTH_RE = re.compile(
    r"(?i)(invalid api key|please run /login|oauth token (?:has )?expired|"
    r"authentication_error|api error: 401|not logged in)"
)
_RATE_RE = re.compile(
    r"(?i)(rate_limit_error|api error: 429|usage limit reached|too many requests|"
    r"credit balance is too low)"
)


def assert_safe_argv(argv: list[str]) -> None:
    """Code-level guard: refuse any permission-bypass flag or mode."""
    for i, arg in enumerate(argv):
        flag = arg.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            raise ProviderError(
                f"refusing to run Claude Code with {flag}", error_type=ErrorType.INTERNAL_ERROR
            )
        if flag == "--permission-mode":
            value = (
                arg.split("=", 1)[1] if "=" in arg else (argv[i + 1] if i + 1 < len(argv) else "")
            )
            if value in FORBIDDEN_PERMISSION_MODES:
                raise ProviderError(
                    f"refusing to run Claude Code with --permission-mode {value}",
                    error_type=ErrorType.INTERNAL_ERROR,
                )


def parse_version(text: str) -> str | None:
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def parse_result_json(stdout: str) -> dict[str, Any] | None:
    """Return the final ``{"type": "result", ...}`` object, or None if absent/invalid."""
    candidates = [stdout.strip()]
    candidates.extend(line.strip() for line in reversed(stdout.splitlines()))
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") == "result":
            return value
    return None


def classify_api_status(payload: dict[str, Any]) -> ErrorType | None:
    """Classify from the structured ``api_error_status`` field (an HTTP status code)."""
    status = payload.get("api_error_status")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    if status == 401:
        return ErrorType.UNAUTHENTICATED
    if status == 429:
        return ErrorType.RATE_LIMITED
    return None


def classify_error_text(text: str) -> ErrorType | None:
    if _AUTH_RE.search(text):
        return ErrorType.UNAUTHENTICATED
    if _RATE_RE.search(text):
        return ErrorType.RATE_LIMITED
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _models(payload: dict[str, Any]) -> tuple[str, ...] | None:
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None
    names = [k for k in usage if isinstance(k, str) and _MODEL_ID_RE.match(k)]
    return tuple(sorted(names)) if len(names) == len(usage) else None


def _denials(payload: dict[str, Any]) -> tuple[str, ...] | None:
    raw = payload.get("permission_denials")
    if not isinstance(raw, list):
        return None
    names = []
    for item in raw:
        name = item.get("tool_name") if isinstance(item, dict) else None
        names.append(name if isinstance(name, str) else "unknown")
    return tuple(names)


class ClaudeCodeAdapter:
    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        scratch_dir: Path,
        executable: str = "claude",
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
    ) -> None:
        self.scratch_dir = scratch_dir
        self.executable = executable
        self.grace_seconds = grace_seconds
        self._info: CliInfo | None = None

    # ------------------------------------------------------------------ detection

    def detect(self) -> CliInfo:
        path = shutil.which(self.executable)
        if path is None:
            raise ProviderError(
                "Claude Code CLI (`claude`) was not found on PATH",
                error_type=ErrorType.CLI_MISSING,
                hint="Install Claude Code and check that `claude --version` works.",
            )
        outcome = self._quick([path, "--version"])
        version = parse_version(outcome.stdout.decode("utf-8", "replace"))
        if outcome.exit_code != 0 or version is None:
            raise ProviderError(
                f"`{path} --version` did not report a version",
                error_type=ErrorType.CLI_UNSUPPORTED,
            )
        self._info = CliInfo(path=path, version=version)
        return self._info

    def auth_logged_in(self, cli_path: str) -> bool | None:
        """Read only the ``loggedIn`` boolean; all other auth fields are discarded."""
        outcome = self._quick([cli_path, "auth", "status", "--json"])
        try:
            data = json.loads(outcome.stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None
        value = data.get("loggedIn") if isinstance(data, dict) else None
        return value if isinstance(value, bool) else None

    def _quick(self, argv: list[str]) -> ProcessOutcome:
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        return run_process(
            argv,
            cwd=self.scratch_dir,
            env=child_env(),
            timeout_seconds=PREFLIGHT_TIMEOUT_SECONDS,
            max_output_bytes=256 * 1024,
            grace_seconds=2.0,
        )

    # ----------------------------------------------------------------- invocation

    def build_argv(self, request: AdapterRequest, cli_path: str) -> list[str]:
        argv = [
            cli_path,
            "--print",
            "--output-format",
            "json",
            "--append-system-prompt-file",
            str(request.compiled_skill_path),
            "--permission-mode",
            request.permission_mode,
            "--permission-prompts",
            "none",
            "--settings",
            str(request.settings_path),
            "--setting-sources",
            "user",
            "--strict-mcp-config",
        ]
        if request.model:
            argv += ["--model", request.model]
        if request.max_turns:
            argv += ["--max-turns", str(request.max_turns)]
        if request.cli_deny:
            # Variadic option: keep it last so it cannot swallow other arguments.
            argv += ["--disallowedTools", *request.cli_deny]
        assert_safe_argv(argv)
        return argv

    def check_ready(self, request: AdapterRequest) -> None:
        info = self.detect()
        outcome = self._quick(self.build_argv(request, info.path))
        text = (outcome.stdout + b"\n" + outcome.stderr).decode("utf-8", "replace")
        unknown = _UNKNOWN_OPTION_RE.search(text)
        if unknown:
            raise ProviderError(
                f"Claude Code {info.version} does not support {unknown.group(1)}",
                error_type=ErrorType.CLI_UNSUPPORTED,
                hint="Upgrade Claude Code; TUA was verified with 2.1.301.",
            )
        if outcome.exit_code == 0 or _NO_INPUT_MARKER not in text:
            first = excerpt(text.strip().splitlines()[0] if text.strip() else "no output", 300)
            raise ProviderError(
                f"Claude Code {info.version} gave an unexpected preflight response: {first}",
                error_type=ErrorType.CLI_UNSUPPORTED,
                hint="This CLI version has not been validated with TUA.",
            )
        if self.auth_logged_in(info.path) is False:
            raise ProviderError(
                "Claude Code is not logged in",
                error_type=ErrorType.UNAUTHENTICATED,
                hint="Run `claude auth login` yourself; TUA never handles credentials.",
            )

    def run(
        self, request: AdapterRequest, *, on_start: Callable[[int], None] | None = None
    ) -> AdapterResult:
        info = self._info or self.detect()
        argv = self.build_argv(request, info.path)
        started_at = utc_now()
        outcome = run_process(
            argv,
            cwd=request.worktree,
            env=child_env(),
            timeout_seconds=request.timeout_seconds,
            stdin_data=request.prompt.encode("utf-8"),
            max_output_bytes=MAX_STDOUT_BYTES,
            grace_seconds=self.grace_seconds,
            on_start=on_start,
        )
        ended_at = utc_now()
        stdout_text = outcome.stdout.decode("utf-8", "replace")
        stderr_text = outcome.stderr.decode("utf-8", "replace")
        stdout_path = request.output_dir / "claude-stdout.json"
        stderr_path = request.output_dir / "claude-stderr.log"
        write_private_file(stdout_path, redact(stdout_text).encode("utf-8"))
        write_private_file(stderr_path, redact(stderr_text).encode("utf-8"))
        return normalize(
            outcome,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            cli_version=info.version,
            model_alias=request.model,
            started_at=started_at,
            ended_at=ended_at,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )


def normalize(
    outcome: ProcessOutcome,
    *,
    stdout_text: str,
    stderr_text: str,
    cli_version: str | None,
    model_alias: str | None,
    started_at: str,
    ended_at: str,
    stdout_path: Path | None,
    stderr_path: Path | None,
) -> AdapterResult:
    """Map a finished process to a normalized result without inventing success."""
    common: dict[str, Any] = {
        "exit_code": outcome.exit_code,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(outcome.duration_seconds, 3),
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "cli_version": cli_version,
        "model_alias": model_alias,
    }
    if outcome.spawn_error is not None:
        return AdapterResult(
            status=TaskStatus.FAILED,
            error_type=ErrorType.CLI_MISSING,
            error_message=f"could not start Claude Code: {outcome.spawn_error}",
            **common,
        )
    payload = parse_result_json(stdout_text)
    fields: dict[str, Any] = {}
    if payload is not None:
        result_text = payload.get("result") if isinstance(payload.get("result"), str) else None
        num_turns = payload.get("num_turns")
        usage = payload.get("usage")
        session_id = payload.get("session_id")
        fields = {
            "result_excerpt": excerpt(result_text),
            "actual_models": _models(payload),
            "session_id": session_id if isinstance(session_id, str) else None,
            "num_turns": num_turns
            if isinstance(num_turns, int) and not isinstance(num_turns, bool)
            else None,
            "cost_usd": _number(payload.get("total_cost_usd")),
            "usage": usage if isinstance(usage, dict) else None,
            "permission_denials": _denials(payload),
            "extra": {
                key: payload.get(key)
                for key in (
                    "subtype",
                    "is_error",
                    "terminal_reason",
                    "stop_reason",
                    "api_error_status",
                )
                if isinstance(payload.get(key), (str, int, bool))
            },
        }

    if outcome.cancelled:
        return AdapterResult(
            status=TaskStatus.CANCELLED,
            error_type=ErrorType.CANCELLED,
            error_message="cancelled by the user; the provider process group was terminated",
            **common,
            **fields,
        )
    if outcome.timed_out:
        return AdapterResult(
            status=TaskStatus.TIMEOUT,
            error_type=ErrorType.TIMEOUT,
            error_message="wall-clock timeout exceeded; the provider process group was terminated",
            **common,
            **fields,
        )

    if payload is None:
        error_type = classify_error_text(stderr_text + "\n" + stdout_text)
        if error_type is None:
            error_type = (
                ErrorType.INVALID_OUTPUT if outcome.exit_code == 0 else ErrorType.PROVIDER_ERROR
            )
        detail = stderr_text.strip() or stdout_text.strip() or "no output"
        if outcome.stdout_truncated:
            detail = f"stdout exceeded {MAX_STDOUT_BYTES} bytes and was truncated"
        return AdapterResult(
            status=TaskStatus.FAILED,
            error_type=error_type,
            error_message=f"no JSON result from Claude Code (exit {outcome.exit_code}): "
            f"{excerpt(detail, 500)}",
            **common,
        )

    succeeded = (
        outcome.exit_code == 0
        and payload.get("is_error") is False
        and payload.get("subtype") == "success"
    )
    if succeeded:
        return AdapterResult(status=TaskStatus.COMPLETED, **common, **fields)

    result_text = payload.get("result") if isinstance(payload.get("result"), str) else ""
    error_type = (
        classify_api_status(payload)
        or classify_error_text(f"{result_text}\n{stderr_text}")
        or ErrorType.PROVIDER_ERROR
    )
    subtype = payload.get("subtype") if isinstance(payload.get("subtype"), str) else "error"
    reason = payload.get("terminal_reason")
    reason_text = f", {reason}" if isinstance(reason, str) else ""
    return AdapterResult(
        status=TaskStatus.FAILED,
        error_type=error_type,
        error_message=f"Claude Code reported {subtype} (exit {outcome.exit_code}{reason_text}): "
        f"{excerpt(result_text or stderr_text or 'no detail', 500)}",
        **common,
        **fields,
    )
