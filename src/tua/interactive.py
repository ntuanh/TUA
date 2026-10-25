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
from tua.gitsetup import (
    LARGE_FILE_BYTES,
    add_local_excludes,
    create_repo,
    find_child_repos,
    ignore_patterns_for,
    looks_secret,
    plan_new_repo,
    unsafe_location,
)
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
        # Where the user started TUA, like `claude` in its working folder.
        self.context_dir: Path | None = None
        self.start_dir: str | None = None

    def yes(self, question: str, *, default: bool) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        answer = self.ask(f"{question} {hint} ").strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")

    # -------------------------------------------------------------------- entry point

    def run(self, cwd: Path) -> int:
        try:
            repo = self.find_repo(cwd)
            if repo is None:
                return 1
            project = self.project_for(repo)
        except (EOFError, KeyboardInterrupt):
            self.say("")
            return 0
        except TuaError as exc:
            self.say(f"Could not set up TUA: {exc.message}")
            if exc.hint:
                self.say(f"Hint: {exc.hint}")
            return 1
        if project is None:
            return 1
        _, config = load_registered(self.db, project.name)
        model = config.default_model or "default model"
        self.say(f"\nTUA · {project.name} · {config.default_provider} ({model})")
        if self.context_dir:
            self.say(
                f"The AI works on a copy of {project.root_path} and can also read (not change)"
            )
            self.say(f"everything in {self.context_dir}.")
        if self.start_dir:
            self.say(f"Focus: {self.start_dir}/")
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

    def find_repo(self, cwd: Path) -> Repository | None:
        """The Git project to work on: this folder's, one inside it, or a new one."""
        try:
            repo = Repository.open(cwd, require_toplevel=False)
        except RepositoryError:
            pass
        else:
            rel = cwd.resolve().relative_to(repo.root).as_posix()
            self.start_dir = None if rel == "." else rel
            return repo
        if unsafe_location(cwd) is None:
            self.context_dir = cwd.resolve()  # readable context around the project
        children = find_child_repos(cwd)
        known = [c for c in children if self.db.find_project_by_root(str(c.resolve()))]
        if len(children) == 1 and known:
            return Repository.open(children[0])  # chosen before; don't ask again
        if len(children) == 1:
            rel = children[0].relative_to(cwd).as_posix()
            if self.yes(
                f"This folder isn't a Git project, but it contains one: {rel}/. Use it?",
                default=True,
            ):
                return Repository.open(children[0])
        elif children:
            self.say("This folder isn't a Git project, but it contains these:")
            for number, child in enumerate(children, 1):
                self.say(f"  [{number}] {child.relative_to(cwd).as_posix()}/")
            answer = self.ask("Which one should TUA use? (number, or Enter for none) ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(children):
                return Repository.open(children[int(answer) - 1])
        return self.offer_new_repo(cwd)

    def offer_new_repo(self, folder: Path) -> Repository | None:
        reason = unsafe_location(folder)
        if reason:
            self.say(
                "This folder isn't a Git project, and TUA won't create one here because "
                f"{reason}. Open a terminal in a project folder and run `tua` there."
            )
            return None
        plan = plan_new_repo(folder)
        size = plan.total_bytes / (1024 * 1024)
        self.say("This folder isn't a Git project yet. TUA needs Git to work on a safe copy and")
        self.say(
            f"to let you undo anything. It would save {len(plan.files)} files ({size:.1f} MB) "
            "in a first commit."
        )
        for label, items in (
            ("left out, larger than 50 MB", plan.large_files),
            ("left out, look like secrets", plan.secret_files),
            ("left out, separate Git projects", plan.nested_repos),
        ):
            if items:
                more = f" and {len(items) - 5} more" if len(items) > 5 else ""
                self.say(f"  {label}: {', '.join(items[:5])}{more}")
        if plan.too_big:
            self.say(
                "That is too much to add automatically. Create the repository yourself with a "
                ".gitignore for generated and large files, then run `tua` again."
            )
            return None
        if not plan.files:
            self.say("There are no files to save here.")
            return None
        if not self.yes("Make this folder a Git project now?", default=True):
            return None
        self.context_dir = None  # the folder itself becomes the project
        repo = create_repo(plan)
        self.say("✓ Created a Git project with a first commit (left-out files are in .gitignore).")
        return repo

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
            add_local_excludes(repo, ["/.tua/"])
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

    def start(self, project: Project, prompt: str, *, retry: bool = True) -> Task | None:
        try:
            task = self.tasks.run(
                project.name,
                RunOptions(prompt=prompt, context_dir=self.context_dir, start_dir=self.start_dir),
            )
        except DirtyRepositoryError:
            if retry and self.tidy_up(Repository(Path(project.root_path))):
                return self.start(project, prompt, retry=False)
            self.say("Nothing was started. Your files are untouched.")
            return None
        except TuaError as exc:
            self.say(f"\nCould not start: {exc.message}")
            if exc.hint:
                self.say(f"Hint: {exc.hint}")
            return None
        self.summarize(task)
        return task

    def tidy_up(self, repo: Repository) -> bool:
        """Offer to commit or hide uncommitted files so a task can start. True if done."""
        entries = repo.status()
        if not entries:
            return True
        untracked = [e.path for e in entries if e.code == "??"]
        modified = [e for e in entries if e.code != "??"]
        self.say("\nYour project has files that are not committed yet. TUA starts from your last")
        self.say("commit, so they must be saved or hidden first:")
        for entry in entries[:8]:
            self.say(f"  {entry.render()}")
        if len(entries) > 8:
            self.say(f"  … and {len(entries) - 8} more")
        secrets = [e.path for e in entries if looks_secret(e.path)]
        large = []
        for path in untracked:
            try:
                if (repo.root / path).stat().st_size > LARGE_FILE_BYTES:
                    large.append(path)
            except OSError:
                pass
        can_commit = not secrets and not large
        options = []
        if can_commit:
            options.append("[c] commit them")
        if not modified:
            options.append("[i] hide them from Git on this laptop")
        options.append("[Enter] cancel")
        if secrets:
            self.say(
                "  (not offering to commit; these look like secrets: " + ", ".join(secrets) + ")"
            )
        if large:
            self.say("  (not offering to commit; larger than 50 MB: " + ", ".join(large) + ")")
        choice = self.ask("  ".join(options) + " > ").strip().lower()
        if choice == "c" and can_commit:
            run_git(["add", "-A"], cwd=repo.root)
            run_git(
                ["commit", "-q", "-m", "Work in progress (saved before a TUA task)"],
                cwd=repo.root,
            )
            self.say("✓ Committed your changes.")
            return not repo.status()
        if choice == "i" and not modified:
            tracked = frozenset(repo.git("ls-files", "-z").stdout.split("\x00"))
            added = add_local_excludes(repo, ignore_patterns_for(untracked, tracked))
            self.say("✓ Hidden on this laptop (.git/info/exclude): " + ", ".join(added))
            return not repo.status()
        return False

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
