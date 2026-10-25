"""Structured, user-facing errors.

Every expected failure raises a :class:`TuaError` subclass. The CLI renders these as a
short message plus an optional hint, without a traceback. Unexpected exceptions are
treated as bugs and only show a traceback when debugging is enabled.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 124
EXIT_CANCELLED = 130


class TuaError(Exception):
    """Base class for expected failures that should be shown to the user."""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(TuaError):
    """A configuration file is missing, malformed, or fails validation."""


class UnsafePathError(ConfigError):
    """A configured path is absolute, escapes its allowed root, or is otherwise unsafe."""


class StateError(TuaError):
    """The local state database is unusable (corrupt, newer schema, unexpected content)."""


class ProjectNotFoundError(TuaError):
    """No registered project has the requested name."""


class TaskNotFoundError(TuaError):
    """No task has the requested ID."""


class InvalidTransitionError(TuaError):
    """A task state change is not allowed by the lifecycle state machine."""


class RepositoryError(TuaError):
    """A Git repository is missing, invalid, or in a state TUA refuses to operate on."""


class DirtyRepositoryError(RepositoryError):
    """The checkout has uncommitted or untracked changes."""


class GitCommandError(RepositoryError):
    """A Git command failed."""


class WorktreeError(RepositoryError):
    """A task worktree could not be created, inspected, or removed."""


class MergeRefusedError(TuaError):
    """A merge was refused by a safety check; nothing was modified."""


class SkillError(TuaError):
    """A skill file is malformed or rules conflict."""


class ProviderError(TuaError):
    """The provider CLI is missing, unsupported, or not ready to run a task."""

    def __init__(self, message: str, *, error_type: str, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.error_type = error_type


class EvalError(TuaError):
    """An evaluation suite is invalid or cannot be run safely."""


class SecretDetectedError(TuaError):
    """Input that would be sent to a provider appears to contain a secret."""


class AbortedError(TuaError):
    """The user declined a confirmation; nothing was changed."""
