"""Provider adapters: Claude Code CLI and OpenAI Codex CLI."""

from pathlib import Path

from tua.adapters.base import AdapterRequest, AdapterResult, CliInfo, ProviderAdapter
from tua.adapters.claude_code import ClaudeCodeAdapter
from tua.adapters.codex import CodexAdapter
from tua.errors import ConfigError
from tua.process import DEFAULT_GRACE_SECONDS


def create_adapter(
    provider: str, *, scratch_dir: Path, grace_seconds: float = DEFAULT_GRACE_SECONDS
) -> ProviderAdapter:
    if provider == "claude-code":
        return ClaudeCodeAdapter(scratch_dir=scratch_dir, grace_seconds=grace_seconds)
    if provider == "codex":
        return CodexAdapter(scratch_dir=scratch_dir, grace_seconds=grace_seconds)
    raise ConfigError(f"unknown provider {provider!r}")


__all__ = [
    "AdapterRequest",
    "AdapterResult",
    "ClaudeCodeAdapter",
    "CliInfo",
    "CodexAdapter",
    "ProviderAdapter",
    "create_adapter",
]
