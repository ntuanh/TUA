"""``tua doctor``: actionable environment diagnostics that never print secrets.

Authentication is checked only through ``claude auth status --json`` and only its
``loggedIn`` boolean is read. Environment variables and credential files are never read.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tua import __version__
from tua.adapters.base import AdapterRequest
from tua.adapters.claude_code import ClaudeCodeAdapter
from tua.config import CommandPolicy, load_global_config
from tua.db import SCHEMA_VERSION, Database
from tua.errors import ProviderError, TuaError
from tua.models import TaskStatus
from tua.paths import TuaPaths, is_private
from tua.permissions import build_plan, write_settings
from tua.projects import load_registered
from tua.repository import MIN_GIT_VERSION, git_version
from tua.skills import builtin_source, compile_sources, store_compiled

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class Check:
    status: str
    summary: str
    hint: str | None = None


def _check_python() -> Check:
    version = ".".join(map(str, sys.version_info[:3]))
    return Check(OK, f"tua {__version__} on Python {version}")


def _check_git() -> Check:
    version = git_version()
    if version is None:
        return Check(FAIL, "git not found", "Install Git 2.25 or newer.")
    rendered = ".".join(map(str, version))
    if version < MIN_GIT_VERSION:
        return Check(FAIL, f"git {rendered} is too old", "Install Git 2.25 or newer.")
    return Check(OK, f"git {rendered}")


def _check_claude(paths: TuaPaths, adapter: ClaudeCodeAdapter) -> list[Check]:
    try:
        info = adapter.detect()
    except ProviderError as exc:
        return [Check(FAIL, exc.message, exc.hint)]
    checks = [Check(OK, f"claude {info.version} at {info.path}")]
    # Preflight the exact flag set a task uses, against throwaway files. No input is
    # given, so the CLI cannot call the API.
    with tempfile.TemporaryDirectory(prefix="tua-doctor-") as tmp:
        tmp_path = Path(tmp)
        compiled = compile_sources([builtin_source()])
        doctor_paths = TuaPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
        compiled_path = store_compiled(doctor_paths, compiled)
        plan = build_plan(
            "safe",
            worktree=tmp_path,
            commands=CommandPolicy(allow=(), deny=()),
            paths=doctor_paths,
        )
        settings = write_settings(plan, tmp_path / "settings.json")
        request = AdapterRequest(
            task_id="doctor",
            prompt="",
            worktree=tmp_path,
            compiled_skill_path=compiled_path,
            compiled_skill_hash=compiled.compiled_hash,
            settings_path=settings,
            permission_mode=plan.permission_mode,
            cli_deny=plan.cli_deny,
            output_dir=tmp_path,
            timeout_seconds=30,
            max_turns=1,
        )
        try:
            adapter.check_ready(request)
        except ProviderError as exc:
            checks.append(Check(FAIL, exc.message, exc.hint))
            return checks
    checks.append(
        Check(
            OK,
            "headless flags accepted (--print --output-format json "
            "--append-system-prompt-file --permission-mode dontAsk --settings ...)",
        )
    )
    logged_in = adapter.auth_logged_in(info.path)
    if logged_in is None:
        checks.append(
            Check(WARN, "could not determine Claude Code login state", "Run `claude auth status`.")
        )
    else:
        checks.append(Check(OK, "claude authentication: logged in"))
    return checks


def _check_dirs(paths: TuaPaths) -> list[Check]:
    checks = []
    for label, path in (("state", paths.state_dir), ("config", paths.config_dir)):
        if not path.exists():
            checks.append(Check(OK, f"{label} directory {path} (created on first use)"))
        elif not is_private(path):
            checks.append(
                Check(
                    WARN,
                    f"{label} directory {path} is accessible by other users",
                    f"Run `chmod 700 {path}`.",
                )
            )
        else:
            checks.append(Check(OK, f"{label} directory {path} (private)"))
    return checks


def _check_state(paths: TuaPaths) -> list[Check]:
    try:
        db = Database.open(paths.db_path)
    except TuaError as exc:
        return [Check(FAIL, exc.message, exc.hint)]
    checks = []
    try:
        integrity = db.quick_check()
        if integrity != "ok":
            checks.append(Check(FAIL, f"database integrity check: {integrity}"))
        else:
            checks.append(Check(OK, f"database {paths.db_path} (schema v{SCHEMA_VERSION})"))
        try:
            load_global_config(paths)
            checks.append(Check(OK, "global config valid (or absent)"))
        except TuaError as exc:
            checks.append(Check(FAIL, exc.message, exc.hint))
        projects = db.list_projects()
        if not projects:
            checks.append(
                Check(
                    WARN, "no projects registered", "Run `tua project init <PATH> --name <NAME>`."
                )
            )
        for project in projects:
            try:
                load_registered(db, project.name)
                checks.append(Check(OK, f"project '{project.name}' at {project.root_path}"))
            except TuaError as exc:
                checks.append(Check(FAIL, f"project '{project.name}': {exc.message}", exc.hint))
        active = db.list_tasks(statuses=[TaskStatus.PREPARING, TaskStatus.RUNNING])
        if active:
            checks.append(
                Check(
                    WARN,
                    f"{len(active)} task(s) preparing or running",
                    "Run `tua status` to detect interrupted tasks.",
                )
            )
        retained = db.list_tasks(
            statuses=[
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.TIMEOUT,
                TaskStatus.CANCELLED,
            ]
        )
        if retained:
            checks.append(
                Check(
                    OK,
                    f"{len(retained)} task(s) awaiting review (worktrees kept)",
                    "Review with `tua diff <id>`, then `tua merge` or `tua discard`.",
                )
            )
        failed_cleanup = [
            t for t in db.list_tasks(statuses=[TaskStatus.MERGED]) if t.cleanup_status == "failed"
        ]
        if failed_cleanup:
            checks.append(
                Check(
                    WARN,
                    f"{len(failed_cleanup)} merged task(s) with leftover worktrees",
                    "Retry cleanup with `tua discard <id>`.",
                )
            )
    finally:
        db.close()
    return checks


def run_doctor(paths: TuaPaths, adapter: ClaudeCodeAdapter) -> list[Check]:
    checks = [_check_python(), _check_git()]
    checks.extend(_check_claude(paths, adapter))
    checks.extend(_check_dirs(paths))
    checks.extend(_check_state(paths))
    return checks
