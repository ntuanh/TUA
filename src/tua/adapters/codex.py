"""OpenAI Codex CLI adapter (headless ``codex exec --json``).

Verified against codex-cli 0.164.0 with a ChatGPT-plan login. Differences from the Claude
Code adapter, all deliberate:

* Skills are passed as ``-c developer_instructions=<payload>``; Codex has no flag that
  appends a system-prompt file. The payload is not secret, but it does appear in argv.
* Permissions come from Codex's own OS sandbox: ``safe`` maps to ``workspace-write`` (writes
  only inside the worktree, no network) and ``read-only`` to ``read-only``. Approvals are
  set to ``never`` so nothing waits for a person. Codex has no per-command allowlist, so
  ``commands.allow``/``deny`` are not enforced for Codex tasks; the sandbox limits what any
  command can do instead.
* ``--ignore-user-config`` and ``--ignore-rules`` keep user/project Codex configuration
  and exec rules out of the task, and ``--strict-config`` makes unknown keys fatal.

As with Claude Code, the exact argv is preflighted with no prompt (stdin is /dev/null):
Codex then stops with ``No prompt provided via stdin.`` before contacting any service.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tua.adapters.base import AdapterRequest, AdapterResult, CliInfo
from tua.adapters.claude_code import parse_version
from tua.config import EFFORT_LEVELS
from tua.errors import ProviderError
from tua.logging_utils import excerpt, redact
from tua.models import ErrorType, TaskStatus, utc_now
from tua.paths import write_private_file
from tua.process import DEFAULT_GRACE_SECONDS, ProcessOutcome, child_env, run_process

PROVIDER_NAME = "codex"
PREFLIGHT_TIMEOUT_SECONDS = 60
MAX_STDOUT_BYTES = 16 * 1024 * 1024
# Linux limits a single argv string to 128 KiB; stay well below it.
MAX_INSTRUCTIONS_BYTES = 100_000
SANDBOX_FOR_PROFILE = {"safe": "workspace-write", "read-only": "read-only"}

FORBIDDEN_FLAGS = frozenset(
    {
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--approve-for-me",
        "--full-auto",
        "--yolo",
    }
)
FORBIDDEN_SANDBOXES = frozenset({"danger-full-access"})

_NO_PROMPT_MARKER = "No prompt provided"
_UNKNOWN_FLAG_RE = re.compile(r"unexpected argument '([^']+)'")
_UNKNOWN_KEY_RE = re.compile(r"unknown configuration field `([^`]+)`")
_AUTH_RE = re.compile(
    r"(?i)(\"status\":\s*401|unauthorized|not logged in|please (?:log|sign) in|codex login)"
)
_RATE_RE = re.compile(r"(?i)(\"status\":\s*429|rate[_ ]limit|usage limit|too many requests|quota)")


def toml_string(value: str) -> str:
    """A TOML basic string. JSON escapes are valid TOML; non-ASCII stays literal UTF-8."""
    return json.dumps(value, ensure_ascii=False)


def assert_safe_argv(argv: list[str]) -> None:
    for i, arg in enumerate(argv):
        flag = arg.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            raise ProviderError(
                f"refusing to run Codex with {flag}", error_type=ErrorType.INTERNAL_ERROR
            )
        if flag in ("--sandbox", "-s"):
            value = (
                arg.split("=", 1)[1] if "=" in arg else (argv[i + 1] if i + 1 < len(argv) else "")
            )
            if value in FORBIDDEN_SANDBOXES:
                raise ProviderError(
                    f"refusing to run Codex with --sandbox {value}",
                    error_type=ErrorType.INTERNAL_ERROR,
                )


def classify_error_text(text: str) -> ErrorType | None:
    if _AUTH_RE.search(text):
        return ErrorType.UNAUTHENTICATED
    if _RATE_RE.search(text):
        return ErrorType.RATE_LIMITED
    return None


def parse_events(stdout: str) -> dict[str, Any]:
    """Summarize ``codex exec --json`` JSONL events. Unknown event types are ignored."""
    summary: dict[str, Any] = {
        "thread_id": None,
        "last_message": None,
        "usage": None,
        "completed": False,
        "errors": [],
        "events": 0,
    }
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        summary["events"] += 1
        kind = event.get("type")
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            summary["thread_id"] = event["thread_id"]
        elif kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                summary["last_message"] = item["text"]
        elif kind == "turn.completed":
            summary["completed"] = True
            if isinstance(event.get("usage"), dict):
                summary["usage"] = event["usage"]
        elif kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            summary["errors"].append(str(message or "turn failed"))
        elif kind == "error":
            summary["errors"].append(str(event.get("message") or "error"))
    return summary


def _error_detail(message: str) -> str:
    """Codex wraps API errors as JSON inside the message; surface the inner message."""
    try:
        inner = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return message
    if isinstance(inner, dict) and isinstance(inner.get("error"), dict):
        text = inner["error"].get("message")
        if isinstance(text, str):
            return text
    return message


class CodexAdapter:
    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        scratch_dir: Path,
        executable: str = "codex",
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
    ) -> None:
        self.scratch_dir = scratch_dir
        self.executable = executable
        self.grace_seconds = grace_seconds
        self._info: CliInfo | None = None

    def detect(self) -> CliInfo:
        path = shutil.which(self.executable)
        if path is None:
            raise ProviderError(
                "Codex CLI (`codex`) was not found on PATH",
                error_type=ErrorType.CLI_MISSING,
                hint="Install it (for example `npm install -g @openai/codex`) and run "
                "`codex login` yourself.",
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

    def logged_in(self, cli_path: str) -> bool | None:
        """Only the exit status and the leading words of `codex login status` are used."""
        outcome = self._quick([cli_path, "login", "status"])
        text = (outcome.stdout + outcome.stderr).decode("utf-8", "replace").strip().lower()
        if outcome.exit_code == 0 and text.startswith("logged in"):
            return True
        if "not logged in" in text or outcome.exit_code not in (0, None):
            return False
        return None

    def login_state(self, cli_path: str) -> bool | None:
        return self.logged_in(cli_path)

    def _quick(self, argv: list[str], *, cwd: Path | None = None) -> ProcessOutcome:
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        return run_process(
            argv,
            cwd=cwd or self.scratch_dir,
            env=child_env(),
            timeout_seconds=PREFLIGHT_TIMEOUT_SECONDS,
            max_output_bytes=256 * 1024,
            grace_seconds=2.0,
        )

    def build_argv(self, request: AdapterRequest, cli_path: str) -> list[str]:
        sandbox = SANDBOX_FOR_PROFILE.get(request.permission_profile)
        if sandbox is None:
            raise ProviderError(
                f"no Codex sandbox for permission profile {request.permission_profile!r}",
                error_type=ErrorType.CONFIG_ERROR,
            )
        if request.effort and request.effort not in EFFORT_LEVELS[PROVIDER_NAME]:
            raise ProviderError(
                f"Codex does not support effort {request.effort!r}",
                error_type=ErrorType.CONFIG_ERROR,
            )
        payload = request.compiled_skill_path.read_bytes()
        if len(payload) > MAX_INSTRUCTIONS_BYTES:
            raise ProviderError(
                f"compiled skills are {len(payload)} bytes; Codex receives them on the command "
                f"line, which TUA limits to {MAX_INSTRUCTIONS_BYTES} bytes",
                error_type=ErrorType.SKILL_ERROR,
                hint="Shorten the skill files used by this project.",
            )
        argv = [
            cli_path,
            "exec",
            "--json",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--sandbox",
            sandbox,
            "--cd",
            str(request.worktree),
            "--output-last-message",
            str(request.output_dir / "codex-last-message.md"),
            "-c",
            'approval_policy="never"',
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            "developer_instructions=" + toml_string(payload.decode("utf-8")),
        ]
        if request.model:
            argv += ["--model", request.model]
        if request.effort:
            argv += ["-c", f"model_reasoning_effort={toml_string(request.effort)}"]
        argv.append("-")  # read the prompt from stdin
        assert_safe_argv(argv)
        return argv

    def check_ready(self, request: AdapterRequest) -> None:
        info = self.detect()
        argv = self.build_argv(request, info.path)
        # The worktree does not exist yet during preflight; use the scratch directory.
        cd_index = argv.index("--cd") + 1
        argv[cd_index] = str(self.scratch_dir)
        outcome = self._quick(argv)
        text = (outcome.stdout + b"\n" + outcome.stderr).decode("utf-8", "replace")
        flag = _UNKNOWN_FLAG_RE.search(text)
        if flag:
            raise ProviderError(
                f"Codex {info.version} does not support {flag.group(1)}",
                error_type=ErrorType.CLI_UNSUPPORTED,
                hint="Upgrade Codex; TUA was verified with 0.164.0.",
            )
        key = _UNKNOWN_KEY_RE.search(text)
        if key:
            raise ProviderError(
                f"Codex {info.version} does not recognize the setting {key.group(1)}",
                error_type=ErrorType.CLI_UNSUPPORTED,
                hint="Upgrade Codex; TUA was verified with 0.164.0.",
            )
        if outcome.exit_code == 0 or _NO_PROMPT_MARKER not in text:
            first = excerpt(text.strip().splitlines()[0] if text.strip() else "no output", 300)
            raise ProviderError(
                f"Codex {info.version} gave an unexpected preflight response: {first}",
                error_type=ErrorType.CLI_UNSUPPORTED,
                hint="This Codex version has not been validated with TUA.",
            )
        if self.logged_in(info.path) is False:
            raise ProviderError(
                "Codex is not logged in",
                error_type=ErrorType.UNAUTHENTICATED,
                hint="Run `codex login` yourself; TUA never handles credentials.",
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
        stdout_path = request.output_dir / "codex-events.jsonl"
        stderr_path = request.output_dir / "codex-stderr.log"
        write_private_file(stdout_path, redact(stdout_text).encode("utf-8"))
        write_private_file(stderr_path, redact(stderr_text).encode("utf-8"))
        last_message_file = request.output_dir / "codex-last-message.md"
        last_message = None
        if last_message_file.exists():
            last_message = last_message_file.read_text(encoding="utf-8", errors="replace")
            write_private_file(last_message_file, redact(last_message).encode("utf-8"))
        return normalize(
            outcome,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            last_message=last_message,
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
    last_message: str | None,
    cli_version: str | None,
    model_alias: str | None,
    started_at: str,
    ended_at: str,
    stdout_path: Path | None,
    stderr_path: Path | None,
) -> AdapterResult:
    """Map a finished ``codex exec`` run to a normalized result without inventing success.

    Codex does not report the model it actually used, a cost, a turn count, or permission
    denials, so those stay ``None`` (unknown).
    """
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
            error_message=f"could not start Codex: {outcome.spawn_error}",
            **common,
        )
    events = parse_events(stdout_text)
    text = last_message if last_message is not None else events["last_message"]
    fields: dict[str, Any] = {
        "result_excerpt": excerpt(text),
        "result_text": redact(text) if text else None,
        "session_id": events["thread_id"],
        "usage": events["usage"],
        "extra": {"events": events["events"]},
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
    if outcome.exit_code == 0 and events["completed"] and not events["errors"]:
        return AdapterResult(status=TaskStatus.COMPLETED, **common, **fields)

    details = [_error_detail(e) for e in events["errors"]]
    raw = "\n".join([*events["errors"], stderr_text])
    if outcome.exit_code == 0 and not events["errors"]:
        # Exit 0 without a completed turn or any reported error: unusable output.
        error_type = ErrorType.INVALID_OUTPUT
    else:
        error_type = classify_error_text(raw) or ErrorType.PROVIDER_ERROR
    detail = details[-1] if details else (stderr_text.strip() or "no result event")
    return AdapterResult(
        status=TaskStatus.FAILED,
        error_type=error_type,
        error_message=f"Codex failed (exit {outcome.exit_code}): {excerpt(detail, 500)}",
        **common,
        **fields,
    )
