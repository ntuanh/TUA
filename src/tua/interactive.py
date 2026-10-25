"""Interactive mode: run ``tua`` with no arguments inside a project folder.

The first time, TUA offers to set the project up (one or two yes/no questions). After that
it asks what the AI should do, runs the task, and offers the next step: merge, view the
diff, get a review, discard, or keep it for later. Every safety rule of the normal
commands still applies; this is only a friendlier front end.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tua.config import CommandPolicy, load_project_config, project_config_path
from tua.db import Database
from tua.errors import DirtyRepositoryError, RepositoryError, TuaError
from tua.models import Project, Task, TaskStatus
from tua.paths import TuaPaths
from tua.peer_review import run_review
from tua.projects import init_project, load_registered
from tua.repository import Repository, run_git
from tua.review import ReviewService, task_diff
from tua.tasks import MAX_PROMPT_CHARS, RunOptions, TaskService

Ask = Callable[[str], str]
Say = Callable[[str], None]

_SLUG_INVALID = re.compile(r"[^a-z0-9._-]+")


def project_name_for(root: Path, taken: set[str]) -> str:
    """A valid project name derived from the folder name, unique among ``taken``."""
    base = _SLUG_INVALID.sub("-", root.name.lower()).strip("-._")[:56] or "project"
    if not base[0].isalnum():
        base = "p" + base
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def suggest_test_command(root: Path) -> str | None:
    """Guess the project's test command from common marker files."""
    if any((root / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini")):
        python = "python" if shutil.which("python") else "python3"
        return f"{python} -m pytest"
    package = root / "package.json"
    if package.exists() and '"test"' in package.read_text(errors="replace"):
        return "npm test"
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."
    makefile = root / "Makefile"
    if makefile.exists() and re.search(r"^test:", makefile.read_text(errors="replace"), re.M):
        return "make test"
    return None


@dataclass
class AskConfirmer:
    """Routes the review service's confirmations through the same input function."""

    ask: Ask

    def confirm(self, prompt: str) -> bool:
        return self.ask(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")

    def confirm_typed(self, prompt: str, expected: str) -> bool:
        return self.ask(f"{prompt} ").strip() == expected


class Session:
    def __init__(
        self,
        db: Database,
        paths: TuaPaths,
        tasks: TaskService,
        *,
        ask: Ask = input,
        say: Say = print,
    ) -> None:
        self.db = db
        self.paths = paths
        self.tasks = tasks
        self.ask = ask
        self.say = say
        self.review = ReviewService(db, paths, tasks, AskConfirmer(ask), say)

    def yes(self, question: str, *, default: bool) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        answer = self.ask(f"{question} {hint} ").strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")

    # -------------------------------------------------------------------- entry point

    def run(self, cwd: Path) -> int:
        try:
            repo = Repository.open(cwd, require_toplevel=False)
        except RepositoryError:
            self.say(
                "This folder is not a Git project. TUA works on Git projects so it can keep "
                "your files safe.\nCreate one first, then run `tua` again:\n"
                "  git init && git add . && git commit -m 'Initial commit'"
            )
            return 1
        try:
            project = self.project_for(repo)
        except (EOFError, KeyboardInterrupt):
            self.say("")
            return 0
        if project is None:
            return 1
        _, config = load_registered(self.db, project.name)
        model = config.default_model or "default model"
        self.say(f"\nTUA · {project.name} · {config.default_provider} ({model})")
        self.say("Describe what you want, like you would to a colleague: what, where, and how to")
        self.say("check it. End a line with \\ to continue it; type @file to send a file's text.")
        while True:
            try:
                prompt = self.read_prompt()
            except (EOFError, KeyboardInterrupt):
                self.say("")
                return 0
            if prompt is None:
                return 0
            task = self.start(project, prompt)
            if task is not None:
                try:
                    self.next_step(task)
                except (EOFError, KeyboardInterrupt):
                    self.say(f"\nKept task {task.id} for later.")
                    return 0

    # ----------------------------------------------------------------------- set up

    def project_for(self, repo: Repository) -> Project | None:
        root = repo.root
        project = self.db.find_project_by_root(str(root))
        if project is not None:
            _, config = load_registered(self.db, project.name, verify_fingerprint=False)
            if config.fingerprint == project.config_fingerprint:
                return project
            self.say("TUA's settings for this project (.tua/project.yaml) have changed.")
            self.show_permissions(root)
            if not self.yes("Accept the new settings?", default=False):
                return None
            return init_project(self.db, self.paths, root, project.name).project

        if project_config_path(root).exists():
            # Settings that came with the repository: show them before trusting them.
            self.say(
                f"This repository already contains TUA settings ({project_config_path(root)})."
            )
            self.show_permissions(root)
            if not self.yes("Trust these settings and use TUA here?", default=False):
                return None
            name = load_project_config(root).name
            return init_project(self.db, self.paths, root, name).project

        self.say(f'First time here: TUA isn\'t set up for "{root.name}" yet.')
        if not self.yes("Set up TUA for this project now?", default=True):
            return None
        name = project_name_for(root, {p.name for p in self.db.list_projects()})
        allow = []
        test = suggest_test_command(root)
        if test and self.yes(f'Let the AI run "{test}" to test its own work?', default=True):
            allow.append(test)
        result = init_project(self.db, self.paths, root, name, allow=allow)
        if self.yes(
            "Save TUA's settings in Git (commit .tua/)? No keeps them on this laptop.",
            default=False,
        ):
            run_git(["add", "--", ".tua"], cwd=root)
            run_git(["commit", "-q", "-m", "Add TUA config", "--", ".tua"], cwd=root)
            self.say("Committed .tua/ (only that folder).")
        else:
            exclude = repo.common_dir() / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude.read_text() if exclude.exists() else ""
            if ".tua/" not in existing.split():
                with exclude.open("a") as handle:
                    handle.write(
                        ("" if existing.endswith("\n") or not existing else "\n") + ".tua/\n"
                    )
            self.say("Keeping .tua/ on this laptop only (listed in .git/info/exclude).")
        self.say(f'✓ TUA is ready for "{result.project.name}".')
        return result.project

    def show_permissions(self, root: Path) -> None:
        try:
            config = load_project_config(root)
        except TuaError as exc:
            self.say(f"  (cannot read the settings: {exc.message})")
            return
        allowed = ", ".join(CommandPolicy.render(c) for c in config.commands.allow) or "none"
        self.say(
            f"  provider: {config.default_provider}  model: {config.default_model or 'default'}"
        )
        self.say(f"  permission profile: {config.permission_profile}")
        self.say(f"  commands the AI may run: {allowed}")

    # ------------------------------------------------------------------------- tasks

    def read_prompt(self) -> str | None:
        line = self.ask("\nWhat should the AI do? (Enter on an empty line to quit)\n> ")
        while line.endswith("\\"):
            line = line[:-1] + "\n" + self.ask("... ")
        if not line.strip():
            return None
        if line.startswith("@"):
            path = Path(line[1:].strip()).expanduser()
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                self.say(f"Cannot read {path}: {exc.strerror}")
                return self.read_prompt()
            if len(text) > MAX_PROMPT_CHARS:
                self.say(f"{path} is longer than {MAX_PROMPT_CHARS} characters.")
                return self.read_prompt()
            return text
        return line

    def start(self, project: Project, prompt: str) -> Task | None:
        try:
            task = self.tasks.run(project.name, RunOptions(prompt=prompt))
        except DirtyRepositoryError as exc:
            self.say(f"\n{exc.message}")
            self.say(
                "Commit your own changes first (for example: git add -A && git commit -m "
                "'wip'), then ask again. TUA never touches them."
            )
            return None
        except TuaError as exc:
            self.say(f"\nCould not start: {exc.message}")
            if exc.hint:
                self.say(f"Hint: {exc.hint}")
            return None
        self.summarize(task)
        return task

    def summarize(self, task: Task) -> None:
        took = f" in {task.duration_seconds:.0f}s" if task.duration_seconds is not None else ""
        if task.status is TaskStatus.COMPLETED:
            self.say(f"\n✓ Done{took}.")
        else:
            self.say(f"\n✗ {task.status.value}{took}: {task.error_message}")
            if task.error_type == "rate_limited":
                self.say("You reached your plan's limit. Wait for it to reset, then try again.")
        if task.result_excerpt:
            text = task.result_excerpt
            self.say(text if len(text) <= 800 else text[:800] + " …")
        if Path(task.worktree_path).is_dir():
            try:
                stat = task_diff(task, self.paths).stat.strip()
            except TuaError:
                stat = ""
            self.say("Changes: " + (stat.splitlines()[-1].strip() if stat else "none"))

    def next_step(self, task: Task) -> None:
        completed = task.status is TaskStatus.COMPLETED
        has_worktree = Path(task.worktree_path).is_dir()
        if not has_worktree:
            return
        menu = "[m] merge  [d] diff  [r] review  [x] discard  [Enter] keep for later > "
        if not completed:
            menu = "[d] diff  [x] discard  [Enter] keep for later > "
        while True:
            choice = self.ask(f"\n{menu}").strip().lower()
            if choice == "":
                self.say(
                    f"Kept task {task.id}. Later: tua merge {task.id}  or  tua discard {task.id}"
                )
                return
            if choice == "d":
                try:
                    self.say(task_diff(task, self.paths).patch or "No changes.")
                except TuaError as exc:
                    self.say(exc.message)
            elif choice == "m" and completed:
                try:
                    self.review.merge(task.id, assume_yes=True)
                    return
                except TuaError as exc:
                    self.say(f"Not merged: {exc.message}")
                    if exc.hint:
                        self.say(f"Hint: {exc.hint}")
            elif choice == "r" and completed:
                try:
                    outcome = run_review(self.tasks, self.review, self.paths, task.id)
                    self.say("\nReview:\n" + (outcome.text or "(no review text)"))
                    self.say("A review is an extra opinion; check findings with tests.")
                except TuaError as exc:
                    self.say(f"Review failed: {exc.message}")
            elif choice == "x":
                try:
                    self.review.discard(task.id)
                    self.say("Discarded.")
                    return
                except TuaError as exc:
                    self.say(exc.message)
            else:
                self.say("Please choose one of the letters shown.")
