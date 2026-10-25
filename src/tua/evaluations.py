"""Minimal evaluation runner with pinned, immutable baselines and hard budgets.

An eval suite is executable input: every setup/check/metric command must match the
project's ``commands.allow`` prefixes (and no deny prefix), all commands are shown, and
the user confirms before anything runs. Commands are split with :mod:`shlex` and executed
without a shell, inside the run's task worktree.

``tua eval baseline`` runs the suite once with the current skills and stores an immutable
baseline (a read-only JSON manifest whose SHA-256 is kept in SQLite). ``tua eval run`` runs
the suite as a candidate and compares it with the baseline pinned in the YAML file; it
never re-runs the baseline. Budgets are a maximum number of provider runs and a total
wall-clock limit; a run is only started if its worst case still fits in the budget.
Results from a handful of LLM runs are noisy, and the report says so.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tua.config import (
    MAX_TIMEOUT_SECONDS,
    NAME_RE,
    CommandPolicy,
    ProjectConfig,
    load_global_config,
    load_yaml_mapping,
    read_limited,
)
from tua.db import Database
from tua.errors import AbortedError, ConfigError, EvalError, TuaError
from tua.logging_utils import excerpt, log_event, redact
from tua.models import Task, TaskStatus, utc_now
from tua.paths import TuaPaths, ensure_private_dir, write_private_file
from tua.permissions import command_allowed
from tua.process import cancellation_signals, child_env, run_process
from tua.projects import load_registered
from tua.repository import Repository
from tua.review import Confirmer, ReviewService, task_diff
from tua.skills import CompiledSkills, compile_project_skills
from tua.tasks import RunOptions, TaskService, validate_prompt

EVAL_SCHEMA_VERSION = 1
RUNNER_VERSION = 1
MANIFEST_VERSION = 1
MAX_CASES = 50
MAX_RUNS_PER_CASE = 20
MAX_COMMANDS = 20
MAX_BUDGET_RUNS = 500
MAX_BUDGET_SECONDS = 7 * 24 * 3600
# Allowance on top of a run's timeout for process termination and bookkeeping.
RUN_OVERHEAD_SECONDS = 15
COMMAND_OUTPUT_LIMIT = 1024 * 1024
DIFF_ARTIFACT_LIMIT = 1024 * 1024
DIRECTIONS = ("lower_is_better", "higher_is_better")
_FORBIDDEN = set(";&|<>`$(){}*?\\\n\r\t\x00")


# ---------------------------------------------------------------------------- schema


@dataclass(frozen=True)
class Command:
    raw: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Metric:
    name: str
    command: Command
    direction: str


@dataclass(frozen=True)
class Case:
    id: str
    prompt: str
    repo_commit: str | None
    setup: tuple[Command, ...]
    checks: tuple[Command, ...]
    metric: Metric | None
    runs: int
    timeout_seconds: int


@dataclass(frozen=True)
class BaselinePin:
    id: str
    suite_fingerprint: str | None
    source_skill_hash: str | None
    compiled_skill_hash: str | None


@dataclass(frozen=True)
class Suite:
    path: Path
    suite_id: str
    project: str
    repo_commit: str | None
    cases: tuple[Case, ...]
    max_runs: int
    max_total_seconds: int
    baseline: BaselinePin | None
    fingerprint: str

    @property
    def planned_runs(self) -> int:
        return sum(case.runs for case in self.cases)

    def commands(self) -> list[tuple[str, str, Command]]:
        found: list[tuple[str, str, Command]] = []
        for case in self.cases:
            found.extend((case.id, "setup", c) for c in case.setup)
            found.extend((case.id, "check", c) for c in case.checks)
            if case.metric:
                found.append((case.id, "metric", case.metric.command))
        return found


class _Map:
    def __init__(self, data: Any, where: str, allowed: set[str]) -> None:
        if not isinstance(data, dict):
            raise EvalError(f"{where} must be a mapping")
        unknown = sorted(str(k) for k in set(data) - allowed)
        if unknown:
            raise EvalError(f"{where}: unknown key(s): {', '.join(unknown)}")
        self.data = data
        self.where = where

    def get_str(self, key: str, *, required: bool = False) -> str | None:
        value = self.data.get(key)
        if value is None:
            if required:
                raise EvalError(f"{self.where}: '{key}' is required")
            return None
        if not isinstance(value, str) or not value.strip():
            raise EvalError(f"{self.where}: '{key}' must be a non-empty string")
        return value

    def get_int(self, key: str, *, minimum: int, maximum: int, default: int | None) -> int:
        value = self.data.get(key, default)
        if value is None:
            raise EvalError(f"{self.where}: '{key}' is required")
        if isinstance(value, bool) or not isinstance(value, int):
            raise EvalError(f"{self.where}: '{key}' must be an integer")
        if not minimum <= value <= maximum:
            raise EvalError(f"{self.where}: '{key}' must be between {minimum} and {maximum}")
        return value

    def get_list(self, key: str) -> list[Any]:
        value = self.data.get(key, [])
        if not isinstance(value, list):
            raise EvalError(f"{self.where}: '{key}' must be a list")
        return value


def _command(raw: Any, where: str) -> Command:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 500:
        raise EvalError(f"{where}: commands must be non-empty strings of at most 500 characters")
    if any(ch in _FORBIDDEN for ch in raw):
        raise EvalError(
            f"{where}: {raw!r} contains shell syntax; commands run without a shell",
            hint="Use a plain command such as 'python -m pytest -q tests/test_x.py'.",
        )
    try:
        argv = tuple(shlex.split(raw))
    except ValueError as exc:
        raise EvalError(f"{where}: cannot parse {raw!r}: {exc}") from exc
    if not argv:
        raise EvalError(f"{where}: empty command")
    return Command(raw=raw, argv=argv)


def _commands(items: list[Any], where: str) -> tuple[Command, ...]:
    if len(items) > MAX_COMMANDS:
        raise EvalError(f"{where}: at most {MAX_COMMANDS} commands are allowed")
    return tuple(_command(item, f"{where}[{i}]") for i, item in enumerate(items))


def _slug(value: str | None, where: str) -> str:
    if value is None or not NAME_RE.match(value):
        raise EvalError(f"{where} must be 1-64 lowercase letters, digits, '.', '_' or '-'")
    return value


def _fingerprint(data: dict[str, Any]) -> str:
    canonical = {
        "schema_version": data.get("schema_version"),
        "suite_id": data.get("suite_id"),
        "project": data.get("project"),
        "repo_commit": data.get("repo_commit"),
        "cases": [
            {k: case.get(k) for k in ("id", "prompt", "repo_commit", "setup", "checks", "metric")}
            for case in data.get("cases", [])
            if isinstance(case, dict)
        ],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def parse_suite(path: Path, *, default_timeout: int) -> Suite:
    """Validate an eval YAML file completely before anything is executed."""
    try:
        data = load_yaml_mapping(read_limited(path, what="eval suite"), source=path)
    except ConfigError as exc:
        raise EvalError(exc.message, hint=exc.hint) from exc
    top = _Map(
        data,
        str(path),
        {"schema_version", "suite_id", "project", "repo_commit", "baseline", "budget", "cases"},
    )
    if data.get("schema_version") != EVAL_SCHEMA_VERSION:
        raise EvalError(f"{path}: schema_version must be {EVAL_SCHEMA_VERSION}")
    suite_id = _slug(top.get_str("suite_id", required=True), f"{path}: suite_id")
    project = _slug(top.get_str("project", required=True), f"{path}: project")

    budget = _Map(data.get("budget"), f"{path}: budget", {"max_runs", "max_total_seconds"})
    max_runs = budget.get_int("max_runs", minimum=1, maximum=MAX_BUDGET_RUNS, default=None)
    max_seconds = budget.get_int(
        "max_total_seconds", minimum=1, maximum=MAX_BUDGET_SECONDS, default=None
    )

    pin = None
    if data.get("baseline") is not None:
        b = _Map(
            data["baseline"],
            f"{path}: baseline",
            {"id", "suite_fingerprint", "source_skill_hash", "compiled_skill_hash"},
        )
        pin = BaselinePin(
            id=b.get_str("id", required=True) or "",
            suite_fingerprint=b.get_str("suite_fingerprint"),
            source_skill_hash=b.get_str("source_skill_hash"),
            compiled_skill_hash=b.get_str("compiled_skill_hash"),
        )

    raw_cases = top.get_list("cases")
    if not raw_cases:
        raise EvalError(f"{path}: 'cases' must contain at least one case")
    if len(raw_cases) > MAX_CASES:
        raise EvalError(f"{path}: at most {MAX_CASES} cases are allowed")
    cases: list[Case] = []
    for index, raw in enumerate(raw_cases):
        where = f"{path}: cases[{index}]"
        c = _Map(
            raw,
            where,
            {"id", "prompt", "repo_commit", "setup", "checks", "metric", "runs", "timeout_seconds"},
        )
        case_id = _slug(c.get_str("id", required=True), f"{where}.id")
        if any(existing.id == case_id for existing in cases):
            raise EvalError(f"{where}: duplicate case id '{case_id}'")
        prompt = c.get_str("prompt", required=True) or ""
        try:
            validate_prompt(prompt)
        except TuaError as exc:
            raise EvalError(f"{where}.prompt: {exc.message}") from exc
        metric = None
        if c.data.get("metric") is not None:
            m = _Map(c.data["metric"], f"{where}.metric", {"name", "command", "direction"})
            direction = m.get_str("direction", required=True)
            if direction not in DIRECTIONS:
                raise EvalError(f"{where}.metric.direction must be one of {', '.join(DIRECTIONS)}")
            metric = Metric(
                name=_slug(m.get_str("name", required=True), f"{where}.metric.name"),
                command=_command(m.get_str("command", required=True), f"{where}.metric.command"),
                direction=direction,
            )
        cases.append(
            Case(
                id=case_id,
                prompt=prompt,
                repo_commit=c.get_str("repo_commit"),
                setup=_commands(c.get_list("setup"), f"{where}.setup"),
                checks=_commands(c.get_list("checks"), f"{where}.checks"),
                metric=metric,
                runs=c.get_int("runs", minimum=1, maximum=MAX_RUNS_PER_CASE, default=1),
                timeout_seconds=c.get_int(
                    "timeout_seconds",
                    minimum=1,
                    maximum=MAX_TIMEOUT_SECONDS,
                    default=default_timeout,
                ),
            )
        )
    return Suite(
        path=path,
        suite_id=suite_id,
        project=project,
        repo_commit=top.get_str("repo_commit"),
        cases=tuple(cases),
        max_runs=max_runs,
        max_total_seconds=max_seconds,
        baseline=pin,
        fingerprint=_fingerprint(data),
    )


def check_commands(suite: Suite, policy: CommandPolicy) -> list[tuple[str, str, Command, str]]:
    """Return (case, kind, command, reason) for every command; raise if any is not allowed."""
    rows = []
    refused = []
    for case_id, kind, command in suite.commands():
        ok, reason = command_allowed(list(command.argv), policy)
        rows.append((case_id, kind, command, reason))
        if not ok:
            refused.append(f"  {case_id} {kind}: {command.raw} ({reason})")
    if refused:
        raise EvalError(
            "eval suite contains commands the project does not allow:\n" + "\n".join(refused),
            hint="Add a matching prefix to commands.allow in .tua/project.yaml (and "
            "re-register the project) only if you trust these commands.",
        )
    return rows


# --------------------------------------------------------------------------- results


@dataclass
class RunRecord:
    case_id: str
    run_index: int
    repo_commit: str
    status: str
    task_id: str | None = None
    task_status: str | None = None
    checks_passed: int | None = None
    checks_total: int | None = None
    metric_name: str | None = None
    metric_value: float | None = None
    duration_seconds: float | None = None
    detail: str | None = None
    compiled_skill_hash: str | None = None
    actual_models: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def aggregate(case: Case, runs: list[RunRecord]) -> dict[str, Any]:
    executed = [r for r in runs if not r.status.startswith("skipped")]
    passed = [r for r in executed if r.status == "passed"]
    values = [r.metric_value for r in executed if r.metric_value is not None]
    durations = [r.duration_seconds for r in executed if r.duration_seconds is not None]
    result: dict[str, Any] = {
        "case_id": case.id,
        "runs_planned": case.runs,
        "runs_executed": len(executed),
        "runs_skipped": len(runs) - len(executed),
        "runs_passed": len(passed),
        "pass_rate": round(len(passed) / len(executed), 4) if executed else None,
        "mean_duration_seconds": round(statistics.fmean(durations), 3) if durations else None,
    }
    if case.metric:
        result["metric"] = {
            "name": case.metric.name,
            "direction": case.metric.direction,
            "values": values,
            "mean": round(statistics.fmean(values), 6) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
    return result


@dataclass
class EvalReport:
    kind: str
    run_id: str
    suite: Suite
    status: str
    records: list[RunRecord]
    summary: dict[str, Any]
    compiled: CompiledSkills
    commits: dict[str, str]
    default_commit: str
    cli_version: str | None
    elapsed: float
    budget: dict[str, int]
    lines: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return {"complete": 0, "cancelled": 130}.get(self.status, 1)

    def render(self) -> str:
        return "\n".join(self.lines)


def _new_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(2)}"


def _short(value: str | None) -> str:
    return (value or "unknown")[:19] + "…" if value and len(value) > 19 else (value or "unknown")


# --------------------------------------------------------------------------- service


class EvalService:
    def __init__(
        self,
        db: Database,
        paths: TuaPaths,
        tasks: TaskService,
        review: ReviewService,
        confirmer: Confirmer,
        out: Callable[[str], None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.paths = paths
        self.tasks = tasks
        self.review = review
        self.confirmer = confirmer
        self.out = out
        self.clock = clock

    # ------------------------------------------------------------------ entry points

    def baseline(
        self,
        eval_file: Path,
        *,
        replace: bool = False,
        max_runs: int | None = None,
        max_seconds: int | None = None,
        assume_yes: bool = False,
        keep_worktrees: bool = False,
    ) -> EvalReport:
        suite, project_id, config = self._load(eval_file)
        active = self._active_baseline(project_id, suite.suite_id)
        if active is not None and not replace:
            raise EvalError(
                f"suite '{suite.suite_id}' already has baseline {active['id']} "
                f"(created {active['created_at']}); refusing to overwrite it",
                hint="Compare against it with `tua eval run`, or pass --replace to supersede "
                "it (the old baseline is kept).",
            )
        if active is not None and not self.confirmer.confirm(
            f"Replace baseline {active['id']} for suite '{suite.suite_id}'? "
            "It will be kept as superseded."
        ):
            raise AbortedError("baseline not replaced")
        report = self._execute(
            "baseline",
            suite,
            project_id,
            config,
            max_runs,
            max_seconds,
            assume_yes,
            keep_worktrees,
            baseline_row=None,
        )
        if report.status == "cancelled":
            report.lines.append("No baseline was stored because the run was cancelled.")
            return report
        if not any(not r.status.startswith("skipped") for r in report.records):
            report.lines.append(
                "No baseline was stored: no run fit in the budget. Raise budget.max_runs / "
                "budget.max_total_seconds or lower the cases' timeout_seconds."
            )
            return report
        baseline_id = self._store_baseline(report, project_id, config, active)
        report.lines.append(f"\nStored immutable baseline {baseline_id}. Pin it in {suite.path}:")
        row = self.db.fetch_one("SELECT * FROM eval_baselines WHERE id = ?", (baseline_id,))
        if row is None:
            raise EvalError(f"baseline {baseline_id} was not stored")
        report.lines.extend(
            [
                "baseline:",
                f"  id: {baseline_id}",
                f'  suite_fingerprint: "{row["suite_fingerprint"]}"',
                f'  source_skill_hash: "{row["source_skill_hash"]}"',
                f'  compiled_skill_hash: "{row["compiled_skill_hash"]}"',
            ]
        )
        return report

    def run(
        self,
        eval_file: Path,
        *,
        max_runs: int | None = None,
        max_seconds: int | None = None,
        assume_yes: bool = False,
        keep_worktrees: bool = False,
    ) -> EvalReport:
        suite, project_id, config = self._load(eval_file)
        baseline_row, manifest = self._pinned_baseline(suite, project_id)
        report = self._execute(
            "candidate",
            suite,
            project_id,
            config,
            max_runs,
            max_seconds,
            assume_yes,
            keep_worktrees,
            baseline_row=baseline_row,
        )
        report.lines.extend(self._compare(suite, manifest, report))
        return report

    # ---------------------------------------------------------------------- loading

    def _load(self, eval_file: Path) -> tuple[Suite, int, ProjectConfig]:
        path = eval_file.expanduser().resolve()
        # Peek at the project name, then validate the full suite with its defaults.
        try:
            raw = load_yaml_mapping(read_limited(path, what="eval suite"), source=path)
        except ConfigError as exc:
            raise EvalError(exc.message, hint=exc.hint) from exc
        name = raw.get("project")
        if not isinstance(name, str):
            raise EvalError(f"{path}: 'project' is required")
        project, config = load_registered(self.db, name)
        eval_root = config.eval_root
        if not path.is_relative_to(eval_root):
            raise EvalError(
                f"{path} is outside the project's eval_dir ({eval_root})",
                hint="Move the suite into eval_dir; TUA only runs suites from there.",
            )
        suite = parse_suite(path, default_timeout=config.timeout_seconds)
        check_commands(suite, config.commands)
        return suite, project.id, config

    def _active_baseline(self, project_id: int, suite_id: str) -> Any:
        return self.db.fetch_one(
            "SELECT * FROM eval_baselines WHERE project_id = ? AND suite_id = ? "
            "AND superseded_at IS NULL",
            (project_id, suite_id),
        )

    def _pinned_baseline(self, suite: Suite, project_id: int) -> tuple[Any, dict[str, Any]]:
        if suite.baseline is None:
            active = self._active_baseline(project_id, suite.suite_id)
            hint = "Run `tua eval baseline` first."
            if active is not None:
                hint = (
                    f"Add this to {suite.path}:\nbaseline:\n  id: {active['id']}\n"
                    f'  suite_fingerprint: "{active["suite_fingerprint"]}"\n'
                    f'  source_skill_hash: "{active["source_skill_hash"]}"\n'
                    f'  compiled_skill_hash: "{active["compiled_skill_hash"]}"'
                )
            raise EvalError(f"suite '{suite.suite_id}' does not pin a baseline", hint=hint)
        pin = suite.baseline
        row = self.db.fetch_one(
            "SELECT * FROM eval_baselines WHERE id = ? AND project_id = ? AND suite_id = ?",
            (pin.id, project_id, suite.suite_id),
        )
        if row is None:
            raise EvalError(f"pinned baseline {pin.id} does not exist for suite '{suite.suite_id}'")
        for key in ("suite_fingerprint", "source_skill_hash", "compiled_skill_hash"):
            pinned = getattr(pin, key)
            if pinned is not None and pinned != row[key]:
                raise EvalError(
                    f"pinned {key} {pinned} does not match baseline {pin.id} ({row[key]})"
                )
        manifest_path = Path(row["manifest_path"])
        try:
            data = manifest_path.read_bytes()
        except OSError as exc:
            raise EvalError(f"baseline manifest {manifest_path} is unreadable: {exc}") from exc
        if "sha256:" + hashlib.sha256(data).hexdigest() != row["manifest_sha256"]:
            raise EvalError(f"baseline manifest {manifest_path} was modified; refusing to use it")
        if row["suite_fingerprint"] != suite.fingerprint:
            raise EvalError(
                f"the cases in {suite.path} changed since baseline {pin.id} was created",
                hint="Create a new baseline with `tua eval baseline --replace`, then update "
                "the pin.",
            )
        manifest: dict[str, Any] = json.loads(data)
        return row, manifest

    # -------------------------------------------------------------------- execution

    def _execute(
        self,
        kind: str,
        suite: Suite,
        project_id: int,
        config: ProjectConfig,
        max_runs: int | None,
        max_seconds: int | None,
        assume_yes: bool,
        keep_worktrees: bool,
        *,
        baseline_row: Any,
    ) -> EvalReport:
        runs_budget = max_runs or suite.max_runs
        seconds_budget = max_seconds or suite.max_total_seconds
        repo = Repository.open(config.root)
        default_commit = repo.resolve_commit(suite.repo_commit or "HEAD")
        commits = {
            case.id: repo.resolve_commit(case.repo_commit) if case.repo_commit else default_commit
            for case in suite.cases
        }
        compiled = compile_project_skills(config, load_global_config(self.paths))

        self._show_plan(kind, suite, config, runs_budget, seconds_budget, repo)
        if not assume_yes and not self.confirmer.confirm("Run this evaluation?"):
            raise AbortedError("evaluation not started")

        run_id = _new_id("ev")
        artifact_dir = ensure_private_dir(
            self.paths.evals_dir / suite.project / suite.suite_id / run_id
        )
        with self.db.transaction() as conn:
            self.db.insert_row(
                conn,
                "eval_runs",
                {
                    "id": run_id,
                    "kind": kind,
                    "project_id": project_id,
                    "suite_id": suite.suite_id,
                    "suite_fingerprint": suite.fingerprint,
                    "runner_version": RUNNER_VERSION,
                    "eval_file": str(suite.path.relative_to(config.root)),
                    "baseline_id": baseline_row["id"] if baseline_row is not None else None,
                    "source_skill_hash": compiled.source_hash,
                    "compiled_skill_hash": compiled.compiled_hash,
                    "provider": config.default_provider,
                    "model_alias": config.default_model,
                    "max_runs": runs_budget,
                    "max_total_seconds": seconds_budget,
                    "runs_planned": suite.planned_runs,
                    "status": "running",
                    "started_at": utc_now(),
                    "artifact_dir": str(artifact_dir),
                },
            )
        log_event("eval.started", eval_run_id=run_id, kind=kind, suite=suite.suite_id)

        started = self.clock()
        records: list[RunRecord] = []
        started_runs = 0
        cancelled = False
        cli_version: str | None = None
        with cancellation_signals():
            for case in suite.cases:
                for index in range(case.runs):
                    commit = commits[case.id]
                    if cancelled:
                        records.append(RunRecord(case.id, index, commit, "skipped_cancelled"))
                        continue
                    elapsed = self.clock() - started
                    if started_runs >= runs_budget:
                        records.append(
                            RunRecord(
                                case.id, index, commit, "skipped_budget", detail="max_runs reached"
                            )
                        )
                        continue
                    if elapsed + case.timeout_seconds + RUN_OVERHEAD_SECONDS > seconds_budget:
                        records.append(
                            RunRecord(
                                case.id,
                                index,
                                commit,
                                "skipped_budget",
                                detail=f"would exceed max_total_seconds ({elapsed:.0f}s used)",
                            )
                        )
                        continue
                    started_runs += 1
                    try:
                        record = self._run_case(
                            run_id, case, index, commit, suite, artifact_dir, keep_worktrees
                        )
                    except KeyboardInterrupt:
                        record = RunRecord(
                            case.id,
                            index,
                            commit,
                            "cancelled",
                            detail="cancelled while starting the run",
                        )
                    records.append(record)
                    if record.task_id and cli_version is None:
                        cli_version = self.db.get_task(record.task_id).cli_version
                    if record.status == "cancelled":
                        cancelled = True
                    self._save_record(run_id, record, artifact_dir)

        for record in records:
            if record.status.startswith("skipped"):
                self._save_record(run_id, record, artifact_dir)
        elapsed = round(self.clock() - started, 3)
        executed = sum(1 for r in records if not r.status.startswith("skipped"))
        status = (
            "cancelled"
            if cancelled
            else ("complete" if executed == suite.planned_runs else "incomplete")
        )
        summary = {
            "cases": [aggregate(c, [r for r in records if r.case_id == c.id]) for c in suite.cases],
            "commits": commits,
            "compiled_skill_hashes": sorted(
                {r.compiled_skill_hash for r in records if r.compiled_skill_hash}
            ),
        }
        self.db.update_eval_run(
            run_id,
            {
                "status": status,
                "ended_at": utc_now(),
                "elapsed_seconds": elapsed,
                "runs_started": started_runs,
                "runs_skipped": sum(1 for r in records if r.status.startswith("skipped")),
                "cli_version": cli_version,
                "summary_json": json.dumps(summary, sort_keys=True),
            },
        )
        log_event("eval.finished", eval_run_id=run_id, status=status, runs=started_runs)

        report = EvalReport(
            kind=kind,
            run_id=run_id,
            suite=suite,
            status=status,
            records=records,
            summary=summary,
            compiled=compiled,
            commits=commits,
            default_commit=default_commit,
            cli_version=cli_version,
            elapsed=elapsed,
            budget={"max_runs": runs_budget, "max_total_seconds": seconds_budget},
        )
        report.lines.extend(
            [
                f"\nEval {kind} {run_id} for suite '{suite.suite_id}' (project {suite.project}): "
                f"{status.upper()}",
                f"  skills       source {_short(compiled.source_hash)}, compiled "
                f"{_short(compiled.compiled_hash)}",
                f"  budget       max_runs {runs_budget}, max_total_seconds {seconds_budget}; "
                f"used {started_runs} run(s), {elapsed:.1f}s",
                f"  coverage     {executed}/{suite.planned_runs} planned run(s) executed",
                f"  artifacts    {artifact_dir}",
            ]
        )
        if len(summary["compiled_skill_hashes"]) > 1:
            report.lines.append("  WARNING      skills changed during the run; results are mixed")
        for record in records:
            report.lines.append("    " + _describe(record))
        return report

    def _show_plan(
        self,
        kind: str,
        suite: Suite,
        config: ProjectConfig,
        runs_budget: int,
        seconds_budget: int,
        repo: Repository,
    ) -> None:
        self.out(
            f"Eval {kind} for suite '{suite.suite_id}' (project {suite.project}): "
            f"{len(suite.cases)} case(s), {suite.planned_runs} planned run(s); budget "
            f"max_runs {runs_budget}, max_total_seconds {seconds_budget}."
        )
        self.out("Each run starts one provider task, then runs these commands in its worktree:")
        for case_id, kind_name, command, reason in check_commands(suite, config.commands):
            self.out(f"  {case_id:<16} {kind_name:<6} {command.raw}   [{reason}]")
        if not suite.commands():
            self.out("  (no commands)")
        self.out(
            "Allowlist matching is a prefix check, not a sandbox: these commands run with "
            "your user's permissions."
        )
        if repo.status():
            self.out(
                "Note: the source checkout has uncommitted changes; eval worktrees are "
                "created from commits and do not include them."
            )

    def _run_case(
        self,
        run_id: str,
        case: Case,
        index: int,
        commit: str,
        suite: Suite,
        artifact_dir: Path,
        keep_worktrees: bool,
    ) -> RunRecord:
        run_started = self.clock()
        deadline = run_started + case.timeout_seconds
        task = self.tasks.run(
            suite.project,
            RunOptions(
                prompt=case.prompt,
                timeout_seconds=case.timeout_seconds,
                base_commit=commit,
                require_clean=False,
                eval_run_id=run_id,
            ),
        )
        record = RunRecord(
            case.id,
            index,
            commit,
            status="pending",
            task_id=task.id,
            task_status=task.status.value,
            metric_name=case.metric.name if case.metric else None,
            compiled_skill_hash=task.compiled_skill_hash,
            actual_models=list(task.actual_models) if task.actual_models else None,
        )
        run_dir = ensure_private_dir(artifact_dir / case.id / str(index))
        if task.status is not TaskStatus.COMPLETED:
            record.status = {
                TaskStatus.TIMEOUT: "timeout",
                TaskStatus.CANCELLED: "cancelled",
            }.get(task.status, "provider_failed")
            record.detail = f"{task.error_type}: {excerpt(task.error_message, 200)}"
            record.duration_seconds = round(self.clock() - run_started, 3)
            return record

        try:
            self._measure(case, task, record, run_dir, deadline)
        except KeyboardInterrupt:
            record.status, record.detail = "cancelled", "cancelled during measurement"
        record.duration_seconds = round(self.clock() - run_started, 3)
        if record.status != "cancelled" and not keep_worktrees:
            try:
                self.review.discard(task.id, assume_yes=True, reason="eval run cleanup")
            except TuaError as exc:
                record.detail = (record.detail or "") + f"; worktree kept: {exc.message}"
        return record

    def _measure(
        self, case: Case, task: Task, record: RunRecord, run_dir: Path, deadline: float
    ) -> None:
        """Run setup, checks, and the metric in the task worktree within the run deadline."""
        self._save_diff(task, run_dir)
        worktree = Path(task.worktree_path)
        status, detail = "passed", None
        for n, command in enumerate(case.setup, 1):
            ok, info = self._command(command, worktree, deadline, run_dir / f"setup-{n}.log")
            if not ok:
                status, detail = ("timeout" if info == "timeout" else "setup_failed"), command.raw
                break
        if status == "passed":
            passed = 0
            for n, command in enumerate(case.checks, 1):
                ok, info = self._command(command, worktree, deadline, run_dir / f"check-{n}.log")
                if info == "timeout":
                    status, detail = "timeout", command.raw
                    break
                passed += int(ok)
            record.checks_passed, record.checks_total = passed, len(case.checks)
            if status == "passed" and passed < len(case.checks):
                status = "checks_failed"
        if status in ("passed", "checks_failed") and case.metric:
            ok, info = self._command(
                case.metric.command, worktree, deadline, run_dir / "metric.log"
            )
            if ok:
                record.metric_value = _parse_metric(info)
                if record.metric_value is None:
                    detail = "metric output is not a number"
            elif info == "timeout":
                status, detail = "timeout", case.metric.command.raw
            else:
                detail = "metric command failed"
        record.status, record.detail = status, detail

    def _command(
        self, command: Command, worktree: Path, deadline: float, log_path: Path
    ) -> tuple[bool, str]:
        remaining = deadline - self.clock()
        if remaining <= 0:
            return False, "timeout"
        result = run_process(
            list(command.argv),
            cwd=worktree,
            env=child_env(),
            timeout_seconds=remaining,
            max_output_bytes=COMMAND_OUTPUT_LIMIT,
        )
        stdout = result.stdout.decode("utf-8", "replace")
        stderr = result.stderr.decode("utf-8", "replace")
        write_private_file(
            log_path,
            redact(
                f"$ {command.raw}\nexit: {result.exit_code}\n--- stdout\n{stdout}\n"
                f"--- stderr\n{stderr}\n"
            ).encode(),
        )
        if result.cancelled:
            raise KeyboardInterrupt
        if result.timed_out:
            return False, "timeout"
        if result.spawn_error:
            return False, result.spawn_error
        return result.exit_code == 0, stdout

    def _save_diff(self, task: Task, run_dir: Path) -> None:
        try:
            diff = task_diff(task, self.paths)
        except TuaError:
            return
        write_private_file(run_dir / "diff.stat", diff.stat.encode())
        patch = diff.patch.encode()[:DIFF_ARTIFACT_LIMIT]
        write_private_file(
            run_dir / "diff.patch", redact(patch.decode("utf-8", "replace")).encode()
        )

    def _save_record(self, run_id: str, record: RunRecord, artifact_dir: Path) -> None:
        with self.db.transaction() as conn:
            self.db.insert_row(
                conn,
                "eval_case_runs",
                {
                    "eval_run_id": run_id,
                    "case_id": record.case_id,
                    "run_index": record.run_index,
                    "task_id": record.task_id,
                    "repo_commit": record.repo_commit,
                    "status": record.status,
                    "task_status": record.task_status,
                    "checks_passed": record.checks_passed,
                    "checks_total": record.checks_total,
                    "metric_name": record.metric_name,
                    "metric_value": record.metric_value,
                    "duration_seconds": record.duration_seconds,
                    "detail": record.detail,
                    "artifact_dir": str(artifact_dir / record.case_id / str(record.run_index)),
                    "created_at": utc_now(),
                },
            )

    # ------------------------------------------------------------------- baselines

    def _store_baseline(
        self, report: EvalReport, project_id: int, config: ProjectConfig, active: Any
    ) -> str:
        suite = report.suite
        baseline_id = _new_id("bl")
        compiled = report.compiled
        records = report.records
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "baseline_id": baseline_id,
            "eval_run_id": report.run_id,
            "suite_id": suite.suite_id,
            "project": suite.project,
            "suite_fingerprint": suite.fingerprint,
            "runner_version": RUNNER_VERSION,
            "eval_file": str(suite.path.relative_to(config.root)),
            "created_at": utc_now(),
            "repo_commit": report.default_commit,
            "case_commits": report.commits,
            "source_skill_hash": compiled.source_hash,
            "compiled_skill_hash": compiled.compiled_hash,
            "provider": config.default_provider,
            "cli_version": report.cli_version,
            "model_alias": config.default_model,
            "budget": report.budget,
            "usage": {"elapsed_seconds": report.elapsed},
            "coverage": report.status,
            "cases": report.summary["cases"],
            "runs": [r.as_dict() for r in records],
        }
        data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        path = (
            self.paths.evals_dir
            / suite.project
            / suite.suite_id
            / "baselines"
            / f"{baseline_id}.json"
        )
        write_private_file(path, data)
        os.chmod(path, 0o400)  # read-only: baselines are never edited in place
        now = utc_now()
        with self.db.transaction() as conn:
            if active is not None:
                conn.execute(
                    "UPDATE eval_baselines SET superseded_at = ? WHERE id = ?", (now, active["id"])
                )
            self.db.insert_row(
                conn,
                "eval_baselines",
                {
                    "id": baseline_id,
                    "project_id": project_id,
                    "suite_id": suite.suite_id,
                    "eval_run_id": report.run_id,
                    "suite_fingerprint": suite.fingerprint,
                    "repo_commit": manifest["repo_commit"],
                    "source_skill_hash": compiled.source_hash,
                    "compiled_skill_hash": compiled.compiled_hash,
                    "manifest_path": str(path),
                    "manifest_sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "created_at": now,
                },
            )
            if active is not None:
                conn.execute(
                    "UPDATE eval_baselines SET superseded_by = ? WHERE id = ?",
                    (baseline_id, active["id"]),
                )
        log_event("eval.baseline_stored", baseline_id=baseline_id, suite=suite.suite_id)
        if report.status != "complete":
            report.lines.append(
                "WARNING: this baseline has incomplete coverage; comparisons will say so."
            )
        return baseline_id

    def _compare(self, suite: Suite, manifest: dict[str, Any], report: EvalReport) -> list[str]:
        compiled = report.compiled
        lines = [
            f"\nComparison with pinned baseline {manifest['baseline_id']} "
            f"(created {manifest['created_at']}, coverage {manifest['coverage']}):",
            f"  baseline skills   compiled {_short(manifest['compiled_skill_hash'])}",
            f"  candidate skills  compiled {_short(compiled.compiled_hash)}",
        ]
        if manifest["compiled_skill_hash"] == compiled.compiled_hash:
            lines.append("  note: candidate skills are identical to the baseline skills")
        baseline_cases = {c["case_id"]: c for c in manifest["cases"]}
        for case_summary in report.summary["cases"]:
            case_id = case_summary["case_id"]
            base = baseline_cases.get(case_id)
            if base is None:
                lines.append(f"  {case_id}: not in baseline")
                continue
            parts = [
                f"pass {base['runs_passed']}/{base['runs_executed']} -> "
                f"{case_summary['runs_passed']}/{case_summary['runs_executed']}"
            ]
            if "metric" in case_summary and "metric" in base:
                b_mean, c_mean = base["metric"]["mean"], case_summary["metric"]["mean"]
                direction = case_summary["metric"]["direction"]
                verdict = "n/a"
                if b_mean is not None and c_mean is not None:
                    if c_mean == b_mean:
                        verdict = "same"
                    else:
                        better = (
                            c_mean < b_mean if direction == "lower_is_better" else c_mean > b_mean
                        )
                        verdict = "better" if better else "worse"
                parts.append(
                    f"{case_summary['metric']['name']} mean {b_mean} -> {c_mean} "
                    f"({direction.replace('_', ' ')}: {verdict})"
                )
            base_commit = manifest.get("case_commits", {}).get(case_id)
            if base_commit and base_commit != report.commits.get(case_id):
                parts.append("COMMIT DIFFERS: results also reflect code changes")
            incomplete = case_summary["runs_skipped"] or base["runs_skipped"]
            if incomplete:
                parts.append("incomplete coverage")
            lines.append(f"  {case_id}: " + "; ".join(parts))
        lines.append(
            "Few LLM runs per case: differences may be noise. TUA does not claim statistical "
            "significance."
        )
        return lines


def _describe(record: RunRecord) -> str:
    text = f"{record.case_id}#{record.run_index}: {record.status}"
    if record.task_id:
        text += f" task {record.task_id}"
    if record.checks_total is not None:
        text += f" checks {record.checks_passed}/{record.checks_total}"
    if record.metric_value is not None:
        text += f" {record.metric_name}={record.metric_value}"
    if record.detail:
        text += f" ({record.detail})"
    return text


def _parse_metric(stdout: str) -> float | None:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return float(lines[-1])
    except ValueError:
        return None
