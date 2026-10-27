"""Command-line interface. Handlers parse arguments, call services, and render output."""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path

from tua import __version__
from tua.adapters import ProviderAdapter, create_adapter
from tua.config import SUPPORTED_PROVIDERS, CommandPolicy, load_global_config
from tua.db import Database
from tua.doctor import FAIL, OK, WARN, run_doctor
from tua.errors import (
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    TuaError,
)
from tua.feedback import record_feedback
from tua.logging_utils import LOGGER_NAME, setup_logging
from tua.models import RETAINED_STATES, Task, TaskStatus
from tua.paths import TuaPaths
from tua.peer_review import run_review, worktree_notice
from tua.permissions import build_plan
from tua.projects import init_project, load_registered
from tua.review import ReviewService, task_diff
from tua.skills import compile_project_skills
from tua.tasks import RunOptions, TaskService


def out(text: str = "") -> None:
    print(text, flush=True)


class TerminalConfirmer:
    """Interactive confirmations. EOF or anything but 'y'/'yes' means no."""

    def confirm(self, prompt: str) -> bool:
        try:
            answer = input(f"{prompt} [y/N] ")
        except EOFError:
            out()
            return False
        return answer.strip().lower() in ("y", "yes")

    def confirm_typed(self, prompt: str, expected: str) -> bool:
        if not sys.stdin.isatty():
            out("A second confirmation requires an interactive terminal; aborting.")
            return False
        try:
            answer = input(f"{prompt} ")
        except EOFError:
            return False
        return answer.strip() == expected


class App:
    """Lazily wires services so `tua --help` never touches local state."""

    def __init__(self, paths: TuaPaths) -> None:
        self.paths = paths
        self._db: Database | None = None
        self.confirmer = TerminalConfirmer()

    @property
    def db(self) -> Database:
        if self._db is None:
            setup_logging(self.paths.log_dir)
            self._db = Database.open(self.paths.db_path)
        return self._db

    def adapter(self, provider: str = "claude-code") -> ProviderAdapter:
        return create_adapter(provider, scratch_dir=self.paths.tmp_dir)

    def tasks(self) -> TaskService:
        return TaskService(self.db, self.paths, self.adapter, on_progress=out)

    def review(self) -> ReviewService:
        return ReviewService(self.db, self.paths, self.tasks(), self.confirmer, out)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()


# ------------------------------------------------------------------------- handlers


def cmd_interactive(app: App, args: argparse.Namespace) -> int:
    from tua.interactive import Session

    return Session(app.db, app.paths, app.tasks()).run(Path.cwd())


def cmd_doctor(app: App, args: argparse.Namespace) -> int:
    checks = run_doctor(app.paths, create_adapter, app.paths.tmp_dir)
    out("TUA doctor")
    marks = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}
    for check in checks:
        out(f"  {marks[check.status]} {check.summary}")
        if check.hint and check.status != OK:
            out(f"         hint: {check.hint}")
    failed = sum(1 for c in checks if c.status == FAIL)
    out(f"\n{failed} problem(s) found." if failed else "\nNo blocking problems found.")
    return EXIT_ERROR if failed else EXIT_OK


def cmd_project_init(app: App, args: argparse.Namespace) -> int:
    result = init_project(app.db, app.paths, Path(args.path), args.name, allow=args.allow or ())
    for path in result.created_files:
        out(f"created {path}")
    out(f"Registered project '{result.project.name}' at {result.project.root_path}")
    allowed = ", ".join(CommandPolicy.render(c) for c in result.config.commands.allow)
    out(f"  commands    {allowed or 'none allowed (the provider cannot run tests)'}")
    out(f"  config      {result.project.config_path} ({result.config.fingerprint[:19]}…)")
    out(
        f"  skills      {len(result.config.skills)} project file(s); "
        f"compiled {result.compiled.compiled_hash[:19]}…"
    )
    if result.created_files:
        out("\nNext: review the new files, then commit them (or list .tua/ in .git/info/exclude):")
        out(
            f"  git -C {result.project.root_path} add .tua && "
            f"git -C {result.project.root_path} commit -m 'Add TUA project config'"
        )
        out("TUA refuses to start tasks while the checkout has uncommitted or untracked files.")
    return EXIT_OK


def cmd_project_list(app: App, args: argparse.Namespace) -> int:
    projects = app.db.list_projects()
    if not projects:
        out("No projects registered. Run `tua project init <PATH> --name <NAME>`.")
        return EXIT_OK
    width = max(len(p.name) for p in projects)
    for project in projects:
        out(f"{project.name:<{width}}  {project.root_path}")
    return EXIT_OK


def _print_rows(rows: list[tuple[str, str]]) -> None:
    width = max(len(key) for key, _ in rows)
    for key, value in rows:
        out(f"  {key:<{width}}  {value}")


def cmd_project_show(app: App, args: argparse.Namespace) -> int:
    project, config = load_registered(app.db, args.name, verify_fingerprint=False)
    changed = config.fingerprint != project.config_fingerprint
    compiled = compile_project_skills(config, load_global_config(app.paths))
    plan = build_plan(
        config.permission_profile,
        worktree=app.paths.worktrees_dir / "<task-id>",
        commands=config.commands,
        paths=app.paths,
    )
    allow = ", ".join(CommandPolicy.render(c) for c in config.commands.allow)
    deny = ", ".join(CommandPolicy.render(c) for c in config.commands.deny)
    current = config.fingerprint
    if changed:
        current += "  <- CHANGED; re-register with `tua project init`"
    out(f"Project {project.name}")
    _print_rows(
        [
            ("root", project.root_path),
            ("config", project.config_path),
            ("registered config", project.config_fingerprint),
            ("current config", current),
            ("provider", config.default_provider),
            ("model alias", config.default_model or "CLI default"),
            ("timeout", f"{config.timeout_seconds}s"),
            ("max turns", str(config.max_turns or "CLI default")),
            ("effort", config.effort or "CLI default"),
            ("permission profile", config.permission_profile),
            ("eval_dir", config.eval_dir),
            ("commands.allow", allow or "(none)"),
            ("commands.deny", deny or "(none)"),
            ("skill sources", ", ".join(source.label for source in compiled.sources)),
            ("rules", f"{len(compiled.must_rules)} must, {len(compiled.default_rules)} default"),
            *[
                ("override", f"{o.rule_id}: {o.winning_source} replaces {o.replaced_source}")
                for o in compiled.overrides
            ],
            ("source_skill_hash", compiled.source_hash),
            ("compiled_skill_hash", compiled.compiled_hash),
            ("claude permission mode", plan.permission_mode),
            ("allow rules", ", ".join(plan.allow)),
            ("deny rules", ", ".join(plan.deny)),
        ]
    )
    return EXIT_OK


_RUN_EXIT = {
    TaskStatus.COMPLETED: EXIT_OK,
    TaskStatus.TIMEOUT: EXIT_TIMEOUT,
    TaskStatus.CANCELLED: EXIT_CANCELLED,
}


def cmd_run(app: App, args: argparse.Namespace) -> int:
    service = app.tasks()
    task = service.run(
        args.project,
        RunOptions(
            prompt=args.prompt,
            model=args.model,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
            permission_profile=args.profile,
            provider=args.provider,
            effort=args.effort,
            web=args.web,
        ),
    )
    out()
    _print_outcome(app, task)
    return _RUN_EXIT.get(task.status, EXIT_ERROR)


def cmd_review(app: App, args: argparse.Namespace) -> int:
    tasks = app.tasks()
    outcome = run_review(
        tasks,
        app.review(),
        app.paths,
        args.task_id,
        model=args.model,
        provider=args.provider,
        effort=args.effort,
        focus=args.focus,
        timeout_seconds=args.timeout,
        keep_worktree=args.keep_worktree,
    )
    review = outcome.task
    out()
    if not outcome.completed:
        _print_outcome(app, review)
        return _RUN_EXIT.get(review.status, EXIT_ERROR)
    out(
        f"Review {review.id} of task {args.task_id} "
        f"({_model_label(review)}, {review.duration_seconds:.1f}s):\n"
    )
    out(outcome.text or "(the reviewer returned no text)")
    if outcome.diff_truncated:
        out("\nNote: the diff was too large and was truncated in the review prompt.")
    out(
        "\nA model review is an extra opinion, not a verdict: confirm findings with tests "
        f"before acting. Record what you concluded with `tua good|bad {args.task_id}`."
    )
    notice = worktree_notice(outcome, kept_on_request=args.keep_worktree)
    if notice:
        out(notice)
    return EXIT_OK


def _model_label(task: Task) -> str:
    """Reported models when the CLI supplies them, otherwise the requested alias, labelled."""
    if task.actual_models:
        return ", ".join(task.actual_models)
    if task.model_alias:
        return f"{task.model_alias} (requested; {task.provider} does not report the model used)"
    return "unknown model"


def _cost(task: Task) -> str | None:
    if task.cost_usd is None:
        return None
    return (
        f"${task.cost_usd:.4f} API-equivalent estimate from Claude Code "
        "(subscriptions use plan quota and are not billed per token)"
    )


def _print_outcome(app: App, task: Task) -> None:
    duration = f" in {task.duration_seconds:.1f}s" if task.duration_seconds is not None else ""
    out(f"Task {task.id} {task.status.value}{duration}.")
    if task.error_type:
        out(f"  error: {task.error_type}: {task.error_message}")
    if task.permission_denials:
        out(f"  permission denials: {task.permission_denials} (see `tua status {task.id}`)")
    cost = _cost(task)
    if cost:
        out(f"  usage: {cost}")
    if task.result_excerpt:
        text = task.result_excerpt
        out("  result: " + (text if len(text) <= 600 else text[:600] + " …"))
    if Path(task.worktree_path).is_dir():
        try:
            stat = task_diff(task, app.paths).stat.strip()
        except TuaError as exc:
            stat = f"(diff unavailable: {exc.message})"
        out(f"  worktree: {task.worktree_path}")
        out("  changes: " + (stat.splitlines()[-1].strip() if stat else "none"))
    if task.status in RETAINED_STATES:
        out("Next steps:")
        out(f"  tua diff {task.id}")
        merge = f"tua merge {task.id}" + ("" if task.status is TaskStatus.COMPLETED else " --force")
        out(f"  {merge}")
        out(f"  tua discard {task.id}")
        out(f'  tua good {task.id}   |   tua bad {task.id} --reason "..."')


def cmd_status(app: App, args: argparse.Namespace) -> int:
    service = app.tasks()
    if args.task_id:
        _print_task_detail(app, service, service.get(args.task_id))
        return EXIT_OK
    project_id = app.db.get_project(args.project).id if args.project else None
    tasks = [
        service.reconcile(t) for t in app.db.list_tasks(project_id=project_id, limit=args.limit)
    ]
    if not tasks:
        out('No tasks yet. Start one with `tua run "<prompt>" --project <NAME>`.')
        return EXIT_OK
    names = {p.id: p.name for p in app.db.list_projects()}
    out(f"{'ID':<22} {'PROJECT':<16} {'STATUS':<10} {'CREATED':<20} PROMPT")
    for task in tasks:
        prompt = " ".join(task.prompt.split())
        if task.review_of:
            prompt = f"[review of {task.review_of}]"
        prompt = prompt if len(prompt) <= 50 else prompt[:49] + "…"
        created = task.created_at.replace("T", " ")[:19]
        name = names.get(task.project_id, "?")[:16]
        out(f"{task.id:<22} {name:<16} {task.status.value:<10} {created:<20} {prompt}")
    return EXIT_OK


def _print_task_detail(app: App, service: TaskService, task: Task) -> None:
    project = app.db.get_project_by_id(task.project_id)
    worktree = Path(task.worktree_path)
    out(f"Task {task.id}")
    rows = [
        ("project", f"{project.name} ({task.source_repo})"),
        ("status", task.status.value),
        ("prompt", task.prompt if len(task.prompt) <= 500 else task.prompt[:500] + " …"),
        ("base", f"{task.base_commit} on {task.target_branch}"),
        ("worktree", f"{worktree} ({'exists' if worktree.is_dir() else 'removed'})"),
        ("branch", task.task_branch),
        ("provider", f"{task.provider} {task.cli_version or 'unknown version'}"),
        ("model alias", task.model_alias or "CLI default"),
        ("reported models", ", ".join(task.actual_models) if task.actual_models else "unknown"),
        ("effort", task.effort or "CLI default"),
        ("permission profile", task.permission_profile),
        ("web access", "on" if task.web else "off"),
        ("source_skill_hash", task.source_skill_hash or "unknown"),
        ("compiled_skill_hash", task.compiled_skill_hash or "unknown"),
        ("timeout", f"{task.timeout_seconds}s"),
        ("started", task.started_at or "-"),
        ("ended", task.ended_at or "-"),
        ("duration", f"{task.duration_seconds:.1f}s" if task.duration_seconds is not None else "-"),
        ("exit code", "-" if task.exit_code is None else str(task.exit_code)),
    ]
    if task.error_type:
        rows.append(("error", f"{task.error_type}: {task.error_message}"))
    if task.permission_denials is not None:
        rows.append(("permission denials", str(task.permission_denials)))
    if task.num_turns is not None:
        rows.append(("turns", str(task.num_turns)))
    cost = _cost(task)
    if cost:
        rows.append(("cost", cost))
    if task.review_of:
        rows.append(("review of", task.review_of))
    if task.session_id:
        rows.append(
            (
                "session",
                f"{task.session_id} (inspect: cd {worktree} && "
                + (
                    f"claude --resume {task.session_id})"
                    if task.provider == "claude-code"
                    else f"codex resume {task.session_id})"
                ),
            )
        )
    if task.stdout_path:
        rows.append(("logs", f"{task.stdout_path}, {task.stderr_path}"))
    if task.merged_commit:
        rows.append(("merged commit", task.merged_commit))
    if task.cleanup_status:
        rows.append(
            (
                "cleanup",
                task.cleanup_status + (f": {task.cleanup_error}" if task.cleanup_error else ""),
            )
        )
    if service.orphaned_provider(task):
        rows.append(
            (
                "WARNING",
                f"TUA exited but provider process {task.provider_pid} is still "
                f"running; stop it with `kill -TERM -{task.provider_pid}`",
            )
        )
    _print_rows(rows)
    if task.result_excerpt:
        out("\nResult (redacted excerpt):\n" + task.result_excerpt)
        full = app.paths.task_dir(task.id) / "result.md"
        if full.exists():
            out(f"(full result: {full})")
    reviews = app.db.list_reviews(task.id)
    if reviews:
        out("\nReviews:")
        for review in reviews:
            models = _model_label(review)
            state = review.status.value
            if review.status is TaskStatus.DISCARDED and review.error_type is None:
                state = "completed"  # finished; only its read-only worktree copy was removed
            out(f"  {review.id}  {state}  {models}  (tua status {review.id})")
    out("\nEvents:")
    for event in app.db.list_events(task.id):
        change = f"{event.from_status or '-'} -> {event.to_status}" if event.to_status else ""
        meta = f" {event.metadata}" if event.metadata else ""
        out(f"  {event.created_at}  {event.event} {change}{meta}")
    feedback = app.db.list_feedback(task.id)
    if feedback:
        out("\nFeedback:")
        for item in feedback:
            text = item.reason or item.note or ""
            out(f"  {item.created_at}  {item.rating}  {text}")


def cmd_diff(app: App, args: argparse.Namespace) -> int:
    task = app.tasks().get(args.task_id)
    result = task_diff(task, app.paths)
    if result.source == "unavailable":
        out(f"Task {task.id} ({task.status.value}) has no worktree any more; no diff available.")
        return EXIT_OK
    origin = "merged commit" if result.source == "merged-commit" else "task worktree"
    out(f"Task {task.id} ({task.status.value}) — {origin} vs base {task.base_commit[:12]}")
    if not result.has_changes:
        out("No changes.")
        return EXIT_OK
    out(result.names.rstrip())
    out()
    out(result.stat if args.stat else result.patch)
    return EXIT_OK


def cmd_merge(app: App, args: argparse.Namespace) -> int:
    app.review().merge(args.task_id, force=args.force, message=args.message, assume_yes=args.yes)
    return EXIT_OK


def cmd_discard(app: App, args: argparse.Namespace) -> int:
    task = app.review().discard(args.task_id, assume_yes=args.yes)
    out(f"Task {task.id} is {task.status.value}; worktree and branch removed.")
    return EXIT_OK


def cmd_feedback(app: App, args: argparse.Namespace) -> int:
    rating = args.command
    feedback = record_feedback(
        app.db,
        app.tasks(),
        args.task_id,
        rating,
        reason=getattr(args, "reason", None),
        note=args.note,
    )
    out(
        f"Recorded '{rating}' feedback #{feedback.id} for task {feedback.task_id} "
        f"(compiled skills {feedback.compiled_skill_hash or 'unknown'})."
    )
    out(
        "TUA does not infer a root cause or change skills automatically; "
        "use `tua eval` to test a skill change."
    )
    return EXIT_OK


def cmd_eval(app: App, args: argparse.Namespace) -> int:
    from tua.evaluations import EvalService

    service = EvalService(app.db, app.paths, app.tasks(), app.review(), app.confirmer, out)
    common = {
        "max_runs": args.max_runs,
        "max_seconds": args.max_seconds,
        "assume_yes": args.yes,
        "keep_worktrees": args.keep_worktrees,
    }
    if args.eval_command == "baseline":
        report = service.baseline(Path(args.eval_file), replace=args.replace, **common)
    else:
        report = service.run(Path(args.eval_file), **common)
    out(report.render())
    return report.exit_code


# --------------------------------------------------------------------------- parser


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _provider_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--provider",
        choices=list(SUPPORTED_PROVIDERS),
        help="provider CLI for this task (default: the project's default_provider)",
    )
    p.add_argument(
        "--effort", help="reasoning effort, e.g. low, medium, high (validated per provider)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tua",
        description="Run headless AI coding CLI tasks in isolated Git worktrees, then review, "
        "merge, or discard the result.",
    )
    parser.add_argument("--version", action="version", version=f"tua {__version__}")
    parser.add_argument("--debug", action="store_true", help="show tracebacks for internal errors")
    sub = parser.add_subparsers(
        dest="command",
        metavar="<command>",
        required=False,
        description="Run `tua` with no command inside a project folder for interactive mode.",
    )

    p = sub.add_parser("doctor", help="check the environment and local state")
    p.set_defaults(handler=cmd_doctor)

    project = sub.add_parser("project", help="register and inspect projects")
    psub = project.add_subparsers(dest="project_command", metavar="<subcommand>", required=True)
    p = psub.add_parser("init", help="create .tua/project.yaml if missing and register the project")
    p.add_argument("path", help="repository root")
    p.add_argument("--name", required=True, help="project name (lowercase, digits, . _ -)")
    p.add_argument(
        "--allow",
        action="append",
        metavar="COMMAND",
        help="command prefix the provider may run, e.g. 'python -m pytest' (repeatable; "
        "only when creating the config)",
    )
    p.set_defaults(handler=cmd_project_init)
    p = psub.add_parser("list", help="list registered projects")
    p.set_defaults(handler=cmd_project_list)
    p = psub.add_parser("show", help="show a project's configuration, skills and permissions")
    p.add_argument("name")
    p.set_defaults(handler=cmd_project_show)

    p = sub.add_parser("run", help="run one headless task in a new worktree")
    p.add_argument("prompt", help="the task prompt")
    p.add_argument("--project", required=True, help="registered project name")
    p.add_argument("--model", help="model alias for this task (overrides default_model)")
    p.add_argument("--timeout", type=_positive, help="wall-clock timeout in seconds")
    p.add_argument("--max-turns", type=_positive, help="passed to `claude --max-turns`")
    p.add_argument(
        "--profile",
        choices=["safe", "read-only"],
        help="permission profile for this task; may only narrow the project's profile",
    )
    p.add_argument(
        "--web",
        action="store_true",
        help="let the AI search the web and open pages (off by default; pages can try to "
        "mislead it, and what it reads could leave in a web request)",
    )
    _provider_options(p)
    p.set_defaults(handler=cmd_run)

    p = sub.add_parser(
        "review", help="have a model review a task's changes, as a recorded read-only task"
    )
    p.add_argument("task_id", help="the task whose changes to review")
    p.add_argument("--model", help="reviewer model (for example sonnet, or gpt-6-luna)")
    _provider_options(p)
    p.add_argument("--focus", help="extra instructions, for example 'input validation'")
    p.add_argument("--timeout", type=_positive, help="wall-clock timeout in seconds")
    p.add_argument(
        "--keep-worktree", action="store_true", help="keep the review's read-only worktree copy"
    )
    p.set_defaults(handler=cmd_review)

    p = sub.add_parser("status", help="list tasks, or show one task in detail")
    p.add_argument("task_id", nargs="?")
    p.add_argument("--project", help="only tasks of this project")
    p.add_argument("--limit", type=_positive, default=20)
    p.set_defaults(handler=cmd_status)

    p = sub.add_parser("diff", help="show a task's changes against its base commit")
    p.add_argument("task_id")
    p.add_argument("--stat", action="store_true", help="only the diffstat")
    p.set_defaults(handler=cmd_diff)

    p = sub.add_parser("merge", help="review and squash-merge a task into its target branch")
    p.add_argument("task_id")
    p.add_argument(
        "--force",
        action="store_true",
        help="allow a failed/timeout/cancelled task (asks twice; never skips checks)",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation for a completed task (the diff is still shown)",
    )
    p.add_argument("-m", "--message", help="commit message (default derived from the prompt)")
    p.set_defaults(handler=cmd_merge)

    p = sub.add_parser("discard", help="remove a task's worktree and branch after confirmation")
    p.add_argument("task_id")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.set_defaults(handler=cmd_discard)

    p = sub.add_parser("good", help="record positive feedback for a task")
    p.add_argument("task_id")
    p.add_argument("--note")
    p.set_defaults(handler=cmd_feedback)

    p = sub.add_parser("bad", help="record negative feedback for a task")
    p.add_argument("task_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--note")
    p.set_defaults(handler=cmd_feedback)

    ev = sub.add_parser("eval", help="pinned-baseline evaluation suites")
    esub = ev.add_subparsers(dest="eval_command", metavar="<subcommand>", required=True)
    for name, text in (
        ("baseline", "run a suite once and store it as the immutable baseline"),
        ("run", "run a suite as a candidate and compare it with its pinned baseline"),
    ):
        p = esub.add_parser(name, help=text)
        p.add_argument("eval_file", help="eval YAML inside the project's eval_dir")
        p.add_argument("--max-runs", type=_positive, help="override budget.max_runs")
        p.add_argument("--max-seconds", type=_positive, help="override budget.max_total_seconds")
        p.add_argument("--yes", action="store_true", help="skip the command confirmation")
        p.add_argument(
            "--keep-worktrees",
            action="store_true",
            help="keep worktrees of completed runs (failed runs are always kept)",
        )
        if name == "baseline":
            p.add_argument(
                "--replace",
                action="store_true",
                help="supersede the current baseline (asks; the old one is kept)",
            )
        p.set_defaults(handler=cmd_eval)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    app = App(TuaPaths.from_env())
    if args.command is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            parser.print_help()
            return EXIT_USAGE
        args.handler = cmd_interactive
    handler: Callable[[App, argparse.Namespace], int] = args.handler
    try:
        return handler(app, args)
    except TuaError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_CANCELLED
    except Exception as exc:  # unexpected: a bug, not a user error
        logging.getLogger(LOGGER_NAME).error("internal_error", exc_info=exc)
        if args.debug:
            traceback.print_exc()
        else:
            print(
                f"error: internal error ({type(exc).__name__}: {exc}). "
                "Re-run with --debug for a traceback.",
                file=sys.stderr,
            )
        return EXIT_ERROR
    finally:
        app.close()
