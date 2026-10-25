"""Task service: create a task, prepare its worktree, run the provider, record the outcome.

Lifecycle (validated by :func:`tua.models.check_transition`)::

    created -> preparing -> running -> completed | failed | timeout | cancelled
    completed -> merged | discarded
    failed | timeout | cancelled -> discarded | merged (--force + second confirmation)

Every state change and its event are written in one SQLite transaction. A task is never
marked ``completed`` unless the provider reported success, and the worktree of a failed,
timed-out, cancelled, or interrupted task is always kept for inspection.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tua.adapters.base import AdapterRequest, AdapterResult, ProviderAdapter
from tua.config import ProjectConfig, check_effort, check_provider, load_global_config
from tua.db import Database
from tua.errors import (
    ConfigError,
    ProviderError,
    RepositoryError,
    SecretDetectedError,
    SkillError,
    StateError,
    TuaError,
    WorktreeError,
)
from tua.logging_utils import LOGGER_NAME, detect_secrets, log_event
from tua.models import ErrorType, Task, TaskStatus, utc_now
from tua.paths import TuaPaths, ensure_private_dir, write_private_file
from tua.permissions import build_plan, narrow_profile, write_settings
from tua.process import (
    CancelRequested,
    cancellation_signals,
    deferred_interrupts,
    hostname,
    last_signal_name,
    process_alive,
    process_identity,
)
from tua.projects import load_registered
from tua.repository import Repository
from tua.skills import compile_project_skills, store_compiled, verify_compiled_file
from tua.worktrees import create_worktree, task_branch_name

MAX_PROMPT_CHARS = 100_000
MAX_RESULT_CHARS = 1_000_000
ID_ATTEMPTS = 5

# Builds the adapter for a provider name ("claude-code" or "codex").
AdapterFactory = Callable[[str], ProviderAdapter]


def new_task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


@dataclass(frozen=True)
class RunOptions:
    prompt: str
    model: str | None = None
    timeout_seconds: int | None = None
    max_turns: int | None = None
    base_commit: str | None = None
    require_clean: bool = True
    eval_run_id: str | None = None
    # May only narrow the project's profile (for example safe -> read-only).
    permission_profile: str | None = None
    review_of: str | None = None
    provider: str | None = None
    effort: str | None = None


def validate_prompt(prompt: str) -> str:
    if not prompt.strip():
        raise TuaError("the task prompt is empty")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise TuaError(f"the task prompt is longer than {MAX_PROMPT_CHARS} characters")
    kinds = detect_secrets(prompt)
    if kinds:
        raise SecretDetectedError(
            f"the prompt appears to contain a secret ({', '.join(kinds)}); refusing to send "
            "it to the provider or store it",
            hint="Remove the credential from the prompt. If it is real, rotate it.",
        )
    return prompt


class TaskService:
    def __init__(
        self,
        db: Database,
        paths: TuaPaths,
        adapter_factory: AdapterFactory,
        *,
        id_factory: Callable[[], str] = new_task_id,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.db = db
        self.paths = paths
        self.adapter_factory = adapter_factory
        self.id_factory = id_factory
        self.on_progress = on_progress or (lambda _msg: None)

    # -------------------------------------------------------------------------- run

    def run(self, project_name: str, options: RunOptions) -> Task:
        """Run one task to a terminal execution state and return the stored record.

        Expected setup problems raise before a task exists (invalid project, dirty
        checkout, secret in prompt). Anything after the task row exists is recorded on
        the task instead of raised.
        """
        prompt = validate_prompt(options.prompt)
        project, config = load_registered(self.db, project_name)
        repo = Repository.open(config.root)
        if options.require_clean:
            repo.ensure_clean(purpose="start a task")
        target_branch = repo.current_branch()
        base = (
            repo.resolve_commit(options.base_commit) if options.base_commit else repo.head_commit()
        )
        timeout = options.timeout_seconds or config.timeout_seconds
        model = options.model or config.default_model
        max_turns = options.max_turns or config.max_turns
        profile = narrow_profile(config.permission_profile, options.permission_profile)
        provider = check_provider(options.provider or config.default_provider)
        # The project's effort level applies only to the project's own provider.
        default_effort = config.effort if provider == config.default_provider else None
        effort = check_effort(provider, options.effort or default_effort)
        config = dataclasses.replace(
            config, permission_profile=profile, default_provider=provider, effort=effort
        )

        task = self._create(
            project_id=project.id,
            prompt=prompt,
            repo=repo,
            target_branch=target_branch,
            base=base,
            config=config,
            timeout=timeout,
            model=model,
            max_turns=max_turns,
            eval_run_id=options.eval_run_id,
            review_of=options.review_of,
        )
        self.on_progress(
            f"Task {task.id} created for project '{project.name}' "
            f"(base {base[:12]} on {target_branch})."
        )
        with cancellation_signals():
            try:
                return self._execute(task, repo, config)
            except KeyboardInterrupt:
                return self._finish_interrupted(task.id)

    def _create(
        self,
        *,
        project_id: int,
        prompt: str,
        repo: Repository,
        target_branch: str,
        base: str,
        config: ProjectConfig,
        timeout: int,
        model: str | None,
        max_turns: int | None,
        eval_run_id: str | None,
        review_of: str | None,
    ) -> Task:
        last_error: Exception | None = None
        for _ in range(ID_ATTEMPTS):
            task_id = self.id_factory()
            fields = {
                "id": task_id,
                "project_id": project_id,
                "prompt": prompt,
                "prompt_sha256": "sha256:" + hashlib.sha256(prompt.encode()).hexdigest(),
                "source_repo": str(repo.root),
                "target_branch": target_branch,
                "base_commit": base,
                "task_branch": task_branch_name(task_id),
                "worktree_path": str(self.paths.worktree_path(task_id)),
                "provider": config.default_provider,
                "model_alias": model,
                "permission_profile": config.permission_profile,
                "config_fingerprint": config.fingerprint,
                "timeout_seconds": timeout,
                "max_turns": max_turns,
                "effort": config.effort,
                "eval_run_id": eval_run_id,
                "review_of": review_of,
                "runner_pid": os.getpid(),
                "runner_host": hostname(),
                "runner_start": process_identity(os.getpid()),
            }
            try:
                task = self.db.insert_task(fields)
            except sqlite3.IntegrityError as exc:
                last_error = exc  # duplicate ID (or path/branch); try a fresh ID
                continue
            log_event("task.created", task_id=task.id, base=base, project_id=project_id)
            return task
        raise StateError(f"could not allocate a unique task id: {last_error}")

    def _execute(self, task: Task, repo: Repository, config: ProjectConfig) -> Task:
        task_dir = ensure_private_dir(self.paths.task_dir(task.id))
        worktree = Path(task.worktree_path)
        try:
            compiled = compile_project_skills(config, load_global_config(self.paths))
            compiled_path = store_compiled(self.paths, compiled)
            settings_path: Path | None = None
            permission_mode = ""
            cli_deny: tuple[str, ...] = ()
            if task.provider == "claude-code":
                plan = build_plan(
                    config.permission_profile,
                    worktree=worktree,
                    commands=config.commands,
                    paths=self.paths,
                )
                settings_path = write_settings(plan, task_dir / "claude-settings.json")
                permission_mode, cli_deny = plan.permission_mode, plan.cli_deny
            elif config.commands.allow:
                self.on_progress(
                    f"Note: {task.provider} enforces permissions with its OS sandbox "
                    f"({config.permission_profile}); commands.allow is not used per command."
                )
            task = self.db.update_task(
                task.id,
                {
                    "source_skill_hash": compiled.source_hash,
                    "compiled_skill_hash": compiled.compiled_hash,
                    "compiled_skill_path": str(compiled_path),
                    "settings_path": str(settings_path) if settings_path else None,
                },
            )
            request = AdapterRequest(
                task_id=task.id,
                prompt=task.prompt,
                worktree=worktree,
                compiled_skill_path=compiled_path,
                compiled_skill_hash=compiled.compiled_hash,
                settings_path=settings_path,
                permission_mode=permission_mode,
                cli_deny=cli_deny,
                output_dir=task_dir,
                timeout_seconds=task.timeout_seconds,
                model=task.model_alias,
                max_turns=task.max_turns,
                permission_profile=task.permission_profile,
                effort=task.effort,
            )
            adapter = self.adapter_factory(task.provider)
            info = adapter.detect()
            task = self.db.update_task(
                task.id, {"cli_path": info.path, "cli_version": info.version}
            )
            adapter.check_ready(request)
            create_worktree(repo, task_id=task.id, path=worktree, base_commit=task.base_commit)
            # The payload must still be byte-identical to what was fingerprinted.
            verify_compiled_file(compiled_path, compiled.compiled_hash)
        except ProviderError as exc:
            return self._fail(task.id, exc.error_type, exc)
        except SkillError as exc:
            return self._fail(task.id, ErrorType.SKILL_ERROR, exc)
        except ConfigError as exc:
            return self._fail(task.id, ErrorType.CONFIG_ERROR, exc)
        except (WorktreeError, RepositoryError) as exc:
            return self._fail(task.id, ErrorType.WORKTREE_ERROR, exc)
        except TuaError as exc:
            return self._fail(task.id, ErrorType.INTERNAL_ERROR, exc)
        except Exception as exc:  # a bug must not leave the task stuck in 'preparing'
            return self._fail(task.id, ErrorType.INTERNAL_ERROR, exc)

        self.db.transition(task.id, TaskStatus.RUNNING, fields={"started_at": utc_now()})
        self.on_progress(
            f"Running {task.provider} in {worktree} (timeout {task.timeout_seconds}s). "
            "Press Ctrl+C to cancel."
        )
        log_event("task.running", task_id=task.id, cli_version=info.version)

        def on_start(pid: int) -> None:
            self.db.update_task(
                task.id, {"provider_pid": pid, "provider_start": process_identity(pid)}
            )

        try:
            result = adapter.run(request, on_start=on_start)
        except Exception as exc:  # an adapter bug must not leave the task 'running'
            return self._fail(task.id, ErrorType.INTERNAL_ERROR, exc)
        return self._record_result(task.id, result)

    # ------------------------------------------------------------------- recording

    def _record_result(self, task_id: str, result: AdapterResult) -> Task:
        fields = {
            "started_at": result.started_at,
            "ended_at": result.ended_at,
            "duration_seconds": result.duration_seconds,
            "exit_code": result.exit_code,
            "error_type": result.error_type.value if result.error_type else None,
            "error_message": result.error_message,
            "stdout_path": str(result.stdout_path) if result.stdout_path else None,
            "stderr_path": str(result.stderr_path) if result.stderr_path else None,
            "result_excerpt": result.result_excerpt,
            "session_id": result.session_id,
            "num_turns": result.num_turns,
            "cost_usd": result.cost_usd,
            "usage_json": json.dumps(result.usage, sort_keys=True) if result.usage else None,
            "permission_denials": (
                len(result.permission_denials) if result.permission_denials is not None else None
            ),
            "actual_models": json.dumps(list(result.actual_models))
            if result.actual_models
            else None,
            "cli_version": result.cli_version,
        }
        metadata = {
            "exit_code": result.exit_code,
            "error_type": fields["error_type"],
            "denied_tools": sorted(set(result.permission_denials or ())),
        }
        if result.status is TaskStatus.CANCELLED:
            metadata["signal"] = last_signal_name()
        if result.result_text:
            write_private_file(
                self.paths.task_dir(task_id) / "result.md",
                result.result_text[:MAX_RESULT_CHARS].encode("utf-8"),
            )
        # Do not let a second Ctrl+C interrupt the final, small database write.
        with deferred_interrupts():
            task = self.db.transition(task_id, result.status, fields=fields, metadata=metadata)
        log_event("task.finished", task_id=task_id, status=task.status.value, **metadata)
        return task

    def _fail(self, task_id: str, error_type: ErrorType | str, exc: BaseException) -> Task:
        error = error_type.value if isinstance(error_type, ErrorType) else str(error_type)
        if not isinstance(exc, TuaError):
            logging.getLogger(LOGGER_NAME).error(
                "task.internal_error", exc_info=exc, extra={"fields": {"task_id": task_id}}
            )
        message = exc.message if isinstance(exc, TuaError) else f"{type(exc).__name__}: {exc}"
        if isinstance(exc, TuaError) and exc.hint:
            message = f"{message} (hint: {exc.hint})"
        with deferred_interrupts():
            task = self.db.transition(
                task_id,
                TaskStatus.FAILED,
                fields={"error_type": error, "error_message": message},
                metadata={"error_type": error},
            )
        log_event("task.failed", task_id=task_id, error_type=error)
        return task

    def _finish_interrupted(self, task_id: str) -> Task:
        """Ctrl+C/SIGTERM outside the provider run (for example while preparing)."""
        task = self.db.get_task(task_id)
        if task.status not in (TaskStatus.PREPARING, TaskStatus.RUNNING):
            return task
        with deferred_interrupts():
            task = self.db.transition(
                task_id,
                TaskStatus.CANCELLED,
                fields={
                    "error_type": ErrorType.CANCELLED.value,
                    "error_message": f"cancelled ({last_signal_name()}) while {task.status.value}",
                },
                metadata={"signal": last_signal_name(), "during": task.status.value},
            )
        log_event("task.cancelled", task_id=task_id)
        return task

    # ---------------------------------------------------------------- reconciliation

    def reconcile(self, task: Task) -> Task:
        """Mark a task whose TUA process died mid-flight as failed (``interrupted``).

        Nothing is deleted. If the runner might still be alive, or the provider process
        it started is still running, the task is left untouched.
        """
        if task.status not in (TaskStatus.PREPARING, TaskStatus.RUNNING):
            return task
        runner = process_alive(task.runner_pid, task.runner_start, task.runner_host)
        if runner is not False:
            return task
        if task.status is TaskStatus.RUNNING and task.provider_pid is not None:
            provider = process_alive(task.provider_pid, task.provider_start, task.runner_host)
            if provider is not False:
                return task
        try:
            task = self.db.transition(
                task.id,
                TaskStatus.FAILED,
                fields={
                    "error_type": ErrorType.INTERRUPTED.value,
                    "error_message": (
                        f"the TUA process ended unexpectedly while the task was "
                        f"{task.status.value}; the worktree was kept for inspection"
                    ),
                },
                metadata={"reconciled_from": task.status.value},
            )
        except (StateError, TuaError):
            return self.db.get_task(task.id)
        log_event("task.reconciled", task_id=task.id)
        return task

    def orphaned_provider(self, task: Task) -> bool:
        """True when the runner is gone but the provider process it started still runs."""
        if task.status is not TaskStatus.RUNNING or task.provider_pid is None:
            return False
        runner = process_alive(task.runner_pid, task.runner_start, task.runner_host)
        provider = process_alive(task.provider_pid, task.provider_start, task.runner_host)
        return runner is False and provider is True

    def get(self, task_id: str) -> Task:
        return self.reconcile(self.db.get_task(task_id))


__all__ = ["CancelRequested", "RunOptions", "TaskService", "new_task_id", "validate_prompt"]
