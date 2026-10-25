"""Domain types: task lifecycle states, allowed transitions, and record types."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from tua.errors import InvalidTransitionError


class TaskStatus(str, Enum):
    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MERGED = "merged"
    DISCARDED = "discarded"


S = TaskStatus

ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.CREATED: frozenset({S.PREPARING}),
    S.PREPARING: frozenset({S.RUNNING, S.FAILED, S.CANCELLED}),
    S.RUNNING: frozenset({S.COMPLETED, S.FAILED, S.TIMEOUT, S.CANCELLED}),
    S.COMPLETED: frozenset({S.MERGED, S.DISCARDED}),
    S.FAILED: frozenset({S.DISCARDED, S.MERGED}),
    S.TIMEOUT: frozenset({S.DISCARDED, S.MERGED}),
    S.CANCELLED: frozenset({S.DISCARDED, S.MERGED}),
    S.MERGED: frozenset(),
    S.DISCARDED: frozenset(),
}

# Merging one of these requires --force plus a second explicit confirmation.
FORCE_MERGE_STATES = frozenset({S.FAILED, S.TIMEOUT, S.CANCELLED})
# States whose worktree and branch are kept for inspection until merge or discard.
RETAINED_STATES = frozenset({S.COMPLETED, S.FAILED, S.TIMEOUT, S.CANCELLED})
ACTIVE_STATES = frozenset({S.CREATED, S.PREPARING, S.RUNNING})


def check_transition(current: TaskStatus, new: TaskStatus, *, force: bool = False) -> None:
    """Raise :class:`InvalidTransitionError` unless ``current -> new`` is allowed."""
    if new not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"task cannot move from '{current.value}' to '{new.value}'")
    if new is S.MERGED and current in FORCE_MERGE_STATES and not force:
        raise InvalidTransitionError(
            f"a '{current.value}' task can only be merged with --force",
            hint="Review the diff first; --force asks for a second explicit confirmation.",
        )


class ErrorType(str, Enum):
    """Normalized error categories recorded on failed tasks."""

    CLI_MISSING = "cli_missing"
    CLI_UNSUPPORTED = "cli_unsupported"
    UNAUTHENTICATED = "unauthenticated"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROVIDER_ERROR = "provider_error"
    INVALID_OUTPUT = "invalid_output"
    CONFIG_ERROR = "config_error"
    SKILL_ERROR = "skill_error"
    WORKTREE_ERROR = "worktree_error"
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"
    UNKNOWN = "unknown"


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Task:
    id: str
    project_id: int
    status: TaskStatus
    prompt: str
    prompt_sha256: str
    source_repo: str
    target_branch: str
    base_commit: str
    task_branch: str
    worktree_path: str
    provider: str
    model_alias: str | None
    actual_models: tuple[str, ...] | None
    cli_path: str | None
    cli_version: str | None
    permission_profile: str
    settings_path: str | None
    config_fingerprint: str
    source_skill_hash: str | None
    compiled_skill_hash: str | None
    compiled_skill_path: str | None
    timeout_seconds: int
    max_turns: int | None
    eval_run_id: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    ended_at: str | None
    duration_seconds: float | None
    exit_code: int | None
    error_type: str | None
    error_message: str | None
    stdout_path: str | None
    stderr_path: str | None
    result_excerpt: str | None
    session_id: str | None
    num_turns: int | None
    cost_usd: float | None
    usage_json: str | None
    permission_denials: int | None
    runner_pid: int | None
    runner_host: str | None
    runner_start: str | None
    provider_pid: int | None
    provider_start: str | None
    merged_commit: str | None
    cleanup_status: str | None
    cleanup_error: str | None
    review_of: str | None
    effort: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        data: dict[str, Any] = dict(row)
        data["status"] = TaskStatus(data["status"])
        models = data.get("actual_models")
        data["actual_models"] = tuple(json.loads(models)) if models else None
        return cls(**data)


@dataclass(frozen=True)
class TaskEvent:
    id: int
    task_id: str
    event: str
    from_status: str | None
    to_status: str | None
    created_at: str
    metadata: dict[str, Any] | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TaskEvent:
        data: dict[str, Any] = dict(row)
        raw = data.get("metadata")
        data["metadata"] = json.loads(raw) if raw else None
        return cls(**data)


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    root_path: str
    config_path: str
    config_fingerprint: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Project:
        return cls(**dict(row))


@dataclass(frozen=True)
class Feedback:
    id: int
    task_id: str
    project_id: int
    rating: str
    reason: str | None
    note: str | None
    task_status: str
    source_skill_hash: str | None
    compiled_skill_hash: str | None
    provider: str
    model_alias: str | None
    actual_models: str | None
    cli_version: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Feedback:
        return cls(**dict(row))
