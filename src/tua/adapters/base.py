"""The provider adapter contract: a typed request in, a normalized result out."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from tua.models import ErrorType, TaskStatus


@dataclass(frozen=True)
class AdapterRequest:
    task_id: str
    prompt: str
    worktree: Path
    compiled_skill_path: Path
    compiled_skill_hash: str
    # Claude Code permission plan; None for providers that use their own sandbox.
    settings_path: Path | None
    permission_mode: str
    cli_deny: tuple[str, ...]
    output_dir: Path
    timeout_seconds: int
    model: str | None = None
    max_turns: int | None = None
    permission_profile: str = "safe"
    effort: str | None = None


@dataclass(frozen=True)
class CliInfo:
    path: str
    version: str | None


@dataclass(frozen=True)
class AdapterResult:
    """Normalized outcome. Unknown values are ``None``; nothing is guessed."""

    status: TaskStatus
    exit_code: int | None
    started_at: str
    ended_at: str
    duration_seconds: float
    stdout_path: Path | None
    stderr_path: Path | None
    cli_version: str | None
    model_alias: str | None
    error_type: ErrorType | None = None
    error_message: str | None = None
    result_excerpt: str | None = None
    # Full final answer, already redacted; stored as result.md in the task directory.
    result_text: str | None = None
    actual_models: tuple[str, ...] | None = None
    session_id: str | None = None
    num_turns: int | None = None
    cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    permission_denials: tuple[str, ...] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    name: str

    def detect(self) -> CliInfo:
        """Locate the CLI and read its version; raise ProviderError(cli_missing) if absent."""

    def check_ready(self, request: AdapterRequest) -> None:
        """Verify flags and authentication without using quota; raise ProviderError."""

    def login_state(self, cli_path: str) -> bool | None:
        """True/False when the login state is known, None when it cannot be determined."""

    def run(
        self, request: AdapterRequest, *, on_start: Callable[[int], None] | None = None
    ) -> AdapterResult:
        """Run one headless task. Must convert timeout and Ctrl+C into a result."""
