"""Permission profiles: translate a named profile into Claude Code permission settings.

These are Claude Code *tool permission* rules, not an operating-system sandbox. They stop
the provider's built-in tools from editing outside the worktree, using the web tools
(unless the user allowed them for the task), or running commands that are not allowlisted,
but an allowlisted command (for example a test runner) can still execute arbitrary code from
the repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from tua.config import ALWAYS_DENIED_COMMANDS, CommandPolicy
from tua.errors import ConfigError
from tua.paths import TuaPaths, write_private_file

# Paths the provider's file tools must never read, even if user settings would allow it.
SENSITIVE_HOME_PATHS = (
    "~/.ssh/**",
    "~/.gnupg/**",
    "~/.aws/**",
    "~/.azure/**",
    "~/.config/gcloud/**",
    "~/.config/gh/**",
    "~/.docker/config.json",
    "~/.kube/**",
    "~/.netrc",
    "~/.git-credentials",
    "~/.claude/**",
    "~/.claude.json",
)
NETWORK_TOOLS = ("WebFetch", "WebSearch")
# Profiles ordered from least to most access.
PROFILE_RANK = {"read-only": 0, "safe": 1}


def narrow_profile(configured: str, requested: str | None) -> str:
    """Apply a per-task profile, which may restrict but never widen the project's."""
    if requested is None:
        return configured
    if requested not in PROFILE_RANK:
        raise ConfigError(
            f"unknown permission profile {requested!r}",
            hint=f"Choose one of: {', '.join(PROFILE_RANK)}.",
        )
    if PROFILE_RANK[requested] > PROFILE_RANK[configured]:
        raise ConfigError(
            f"--profile {requested} would widen the project's '{configured}' profile",
            hint="Change permission_profile in .tua/project.yaml (and re-register) instead.",
        )
    return requested


EDIT_TOOLS_DENY = ("Edit", "Write", "NotebookEdit")


def _abs_rule(tool: str, path: Path, suffix: str = "") -> str:
    # Claude Code uses a leading '//' for absolute filesystem paths in permission rules.
    return f"{tool}(/{path.as_posix()}{suffix})"


def bash_rules(prefix: tuple[str, ...]) -> list[str]:
    command = CommandPolicy.render(prefix)
    return [f"Bash({command})", f"Bash({command} *)"]


@dataclass(frozen=True)
class PermissionPlan:
    """The Claude Code permission configuration for one task."""

    profile: str
    permission_mode: str
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    cli_deny: tuple[str, ...]

    def settings(self) -> dict[str, object]:
        return {"permissions": {"allow": list(self.allow), "deny": list(self.deny)}}


def build_plan(
    profile: str,
    *,
    worktree: Path,
    commands: CommandPolicy,
    paths: TuaPaths,
    context_dir: Path | None = None,
    web: bool = False,
) -> PermissionPlan:
    """Build the allow/deny rules for ``profile`` scoped to the task ``worktree``.

    ``context_dir`` (where the user started TUA) becomes readable but never editable.
    ``web`` allows the web search and fetch tools, which are otherwise denied.
    """
    worktree = worktree.resolve()
    always_deny = [rule for prefix in ALWAYS_DENIED_COMMANDS for rule in bash_rules(prefix)]
    sensitive = [f"Read({p})" for p in SENSITIVE_HOME_PATHS]
    state = paths.state_dir.resolve()
    private_state = [
        _abs_rule(tool, state, sub)
        for tool in ("Read", "Edit")
        for sub in ("/tua.db*", "/tasks/**", "/logs/**", "/evals/**")
    ]
    network_deny: tuple[str, ...] = () if web else NETWORK_TOOLS
    deny = [*network_deny, *always_deny, *sensitive, *private_state]
    allow = [_abs_rule("Read", worktree, "/**"), *(NETWORK_TOOLS if web else ())]
    if context_dir is not None:
        context = context_dir.resolve()
        allow.append(_abs_rule("Read", context, "/**"))
        deny.append(_abs_rule("Edit", context, "/**"))

    if profile == "safe":
        allow.append(_abs_rule("Edit", worktree, "/**"))
        for prefix in commands.allow:
            allow.extend(bash_rules(prefix))
        for prefix in commands.deny:
            deny.extend(bash_rules(prefix))
    elif profile == "read-only":
        deny.extend([*EDIT_TOOLS_DENY, "Bash"])
    else:
        raise ConfigError(f"unknown permission profile {profile!r}")

    return PermissionPlan(
        profile=profile,
        permission_mode="dontAsk",
        allow=tuple(dict.fromkeys(allow)),
        deny=tuple(dict.fromkeys(deny)),
        cli_deny=(*network_deny, *always_deny),
    )


def write_settings(plan: PermissionPlan, path: Path) -> Path:
    write_private_file(path, (json.dumps(plan.settings(), indent=2) + "\n").encode("utf-8"))
    return path


def command_allowed(argv: list[str], commands: CommandPolicy) -> tuple[bool, str]:
    """Check an eval command (already split into tokens) against the project policy."""
    tokens = tuple(argv)
    for denied in (*ALWAYS_DENIED_COMMANDS, *commands.deny):
        if tokens[: len(denied)] == denied:
            return False, f"matches denied prefix '{CommandPolicy.render(denied)}'"
    for allowed in commands.allow:
        if tokens[: len(allowed)] == allowed:
            return True, f"matches allowed prefix '{CommandPolicy.render(allowed)}'"
    return False, "does not match any commands.allow prefix"
