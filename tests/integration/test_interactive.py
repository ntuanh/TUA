"""Interactive mode (`tua` with no arguments), driven by scripted answers."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import FakeClaude, git
from tua.db import Database
from tua.interactive import Session, project_name_for, suggest_test_command
from tua.models import TaskStatus
from tua.paths import TuaPaths
from tua.tasks import TaskService


class Script:
    """Feeds answers to the session and records every question and message."""

    def __init__(self, *answers: str) -> None:
        self.answers: Iterator[str] = iter(answers)
        self.questions: list[str] = []
        self.output: list[str] = []

    def ask(self, question: str) -> str:
        self.questions.append(question)
        try:
            return next(self.answers)
        except StopIteration:
            raise EOFError from None

    def say(self, text: str) -> None:
        self.output.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.output + self.questions)


def session(db: Database, paths: TuaPaths, service: TaskService, script: Script) -> Session:
    return Session(db, paths, service, ask=script.ask, say=script.say)


def commits(repo: Path) -> int:
    return int(git(repo, "rev-list", "--count", "HEAD").strip())


def test_first_time_setup_then_task_then_merge(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path, fake_claude: FakeClaude
) -> None:
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "-q", "-m", "py")
    before = commits(repo)
    script = Script("", "", "", "Create hello.txt", "m", "")
    assert session(db, paths, service, script).run(repo) == 0

    assert "Set up TUA for this project now? [Y/n]" in script.questions[0]
    assert "to test its own work?" in script.questions[1]
    (project,) = db.list_projects()
    assert project.name == "repo"
    config = (repo / ".tua" / "project.yaml").read_text()
    assert "-m pytest" in config  # the suggested test command was allowed
    assert ".tua/" in (repo / ".git" / "info" / "exclude").read_text()
    assert (repo / "hello.txt").read_text() == "hello from fake claude\n"  # merged
    assert commits(repo) == before + 1  # only the merge commit; .tua stayed local
    assert git(repo, "status", "--porcelain") == ""
    assert "✓ Done" in script.text


def test_second_time_goes_straight_to_the_prompt(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path, fake_claude: FakeClaude
) -> None:
    assert session(db, paths, service, Script("", "", "")).run(repo) == 0  # setup, then quit
    script = Script("Create hello.txt", "", "")  # task, keep for later, quit
    assert session(db, paths, service, script).run(repo) == 0
    assert script.questions[0].strip().startswith("What should the AI do?")
    (task,) = db.list_tasks()
    assert task.status is TaskStatus.COMPLETED  # kept for later, not merged
    assert f"tua merge {task.id}" in script.text


def test_subfolder_works_and_project_names_are_unique(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path
) -> None:
    (repo / "src").mkdir()
    assert session(db, paths, service, Script("", "", "")).run(repo / "src") == 0
    assert db.list_projects()[0].root_path == str(repo.resolve())
    assert project_name_for(Path("/x/My Project!"), set()) == "my-project"
    assert project_name_for(Path("/x/repo"), {"repo", "repo-2"}) == "repo-3"
    assert project_name_for(Path("/x/..."), set()) == "project"


def test_commit_option_commits_only_tua_files(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path
) -> None:
    (repo / "notes.txt").write_text("mine\n")
    git(repo, "add", "notes.txt")  # the user's own staged work
    script = Script("", "y", "")
    assert session(db, paths, service, script).run(repo) == 0
    changed = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert changed == [".tua/project.yaml", ".tua/skills/project-defaults.md"]
    assert git(repo, "diff", "--cached", "--name-only").split() == ["notes.txt"]  # untouched


def test_settings_from_the_repository_must_be_trusted(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path
) -> None:
    (repo / ".tua").mkdir()
    (repo / ".tua" / "project.yaml").write_text(
        "schema_version: 1\nname: shared\ncommands:\n  allow: ['make test']\n"
    )
    git(repo, "add", ".tua")
    git(repo, "commit", "-q", "-m", "someone else's tua config")
    script = Script("n")
    assert session(db, paths, service, script).run(repo) == 1
    assert "commands the AI may run: make test" in script.text
    assert db.list_projects() == []
    assert session(db, paths, service, Script("y", "")).run(repo) == 0
    assert db.list_projects()[0].name == "shared"


def test_changed_settings_need_approval(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path
) -> None:
    assert session(db, paths, service, Script("", "", "")).run(repo) == 0
    config = repo / ".tua" / "project.yaml"
    config.write_text(config.read_text().replace("allow: []", "allow: ['make test']"))
    declined = Script("n")
    assert session(db, paths, service, declined).run(repo) == 1
    assert "have changed" in declined.text
    assert session(db, paths, service, Script("y", "")).run(repo) == 0


def test_dirty_checkout_can_be_cancelled_or_committed(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path, fake_claude: FakeClaude
) -> None:
    assert session(db, paths, service, Script("", "", "")).run(repo) == 0
    (repo / "README.md").write_text("my unfinished edit\n")
    cancelled = Script("Create hello.txt", "", "")
    assert session(db, paths, service, cancelled).run(repo) == 0
    assert "[c] commit them" in cancelled.text
    assert "[i] hide" not in cancelled.text  # modified tracked files cannot be hidden
    assert "Your files are untouched" in cancelled.text
    assert (repo / "README.md").read_text() == "my unfinished edit\n"
    assert db.list_tasks() == []

    before = commits(repo)
    committed = Script("Create hello.txt", "c", "", "")
    assert session(db, paths, service, committed).run(repo) == 0
    assert commits(repo) == before + 1
    assert "Work in progress" in git(repo, "log", "-1", "--format=%s")
    assert len(db.list_tasks()) == 1  # the task ran after the commit


def test_untracked_build_output_can_be_hidden(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path, fake_claude: FakeClaude
) -> None:
    assert session(db, paths, service, Script("", "", "")).run(repo) == 0
    build = repo / "sw" / "drivers" / "out" / "SOC"  # the fixture already ignores build/
    build.mkdir(parents=True)
    (build / "app.elf").write_text("binary")
    (build / "app.dis").write_text("listing")
    script = Script("Create hello.txt", "i", "", "")
    assert session(db, paths, service, script).run(repo) == 0
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert "/sw/drivers/out/" in exclude.splitlines()
    assert git(repo, "status", "--porcelain") == ""
    assert (build / "app.elf").exists()  # hidden, never deleted
    assert len(db.list_tasks()) == 1


def test_secret_files_are_never_offered_for_commit(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path
) -> None:
    assert session(db, paths, service, Script("", "", "")).run(repo) == 0
    (repo / ".env").write_text("TOKEN=x\n")
    script = Script("Create hello.txt", "c", "")
    assert session(db, paths, service, script).run(repo) == 0
    assert "look like secrets: .env" in script.text
    assert "[c] commit them" not in script.text
    assert git(repo, "log", "--format=%s").count("Work in progress") == 0


def test_multiline_and_file_prompts(
    db: Database,
    paths: TuaPaths,
    service: TaskService,
    repo: Path,
    tmp_path: Path,
    fake_claude: FakeClaude,
) -> None:
    assert session(db, paths, service, Script("", "", "")).run(repo) == 0
    request = tmp_path / "task.md"
    request.write_text("Create hello.txt\nfrom a file\n")
    script = Script("first line \\", "second line", "", f"@{request}", "", "")
    assert session(db, paths, service, script).run(repo) == 0
    prompts = [c["prompt"] for c in fake_claude.calls() if c["prompt"]]
    assert prompts == ["first line \nsecond line", "Create hello.txt\nfrom a file\n"]


def test_failed_task_menu_has_no_merge(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path, fake_claude: FakeClaude
) -> None:
    assert session(db, paths, service, Script("", "", "")).run(repo) == 0
    fake_claude.configure(mode="provider_error")
    script = Script("Create hello.txt", "m", "", "")
    assert session(db, paths, service, script).run(repo) == 0
    assert "✗ failed" in script.text
    assert "Please choose one of the letters shown." in script.text
    assert "[m] merge" not in script.questions[-2]


def test_empty_plain_folder(
    db: Database, paths: TuaPaths, service: TaskService, tmp_path: Path
) -> None:
    plain = tmp_path / "work" / "empty"
    plain.mkdir(parents=True)
    script = Script()
    assert session(db, paths, service, script).run(plain) == 1
    assert "no files to save" in script.text
    assert not (plain / ".git").exists()


def test_plain_folder_becomes_a_git_project_safely(
    db: Database, paths: TuaPaths, service: TaskService, tmp_path: Path, fake_claude: FakeClaude
) -> None:
    folder = tmp_path / "work" / "plainproj"
    folder.mkdir(parents=True)
    (folder / "main.c").write_text("int main(void) { return 0; }\n")
    (folder / ".env").write_text("API_TOKEN=do-not-commit\n")
    with (folder / "huge.bin").open("wb") as handle:  # sparse: large size, no disk use
        handle.truncate(60 * 1024 * 1024)
    nested = folder / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    # "n": do not switch to the vendored project; then accept creating the repository.
    script = Script("n", "", "", "", "Create hello.txt", "m", "")
    assert session(db, paths, service, script).run(folder) == 0

    assert "contains one: vendor/lib/. Use it?" in script.text
    assert "It would save 1 files" in script.text
    assert "huge.bin" in script.text
    assert ".env" in script.text
    assert "vendor/lib" in script.text
    committed = git(folder, "ls-files").split()
    assert "main.c" in committed
    assert ".gitignore" in committed
    assert ".env" not in committed
    assert "huge.bin" not in committed
    assert not any(path.startswith("vendor/") for path in committed)
    ignore = (folder / ".gitignore").read_text().splitlines()
    assert "/huge.bin" in ignore
    assert "/vendor/lib/" in ignore
    assert ".env" in ignore
    assert (folder / ".env").read_text() == "API_TOKEN=do-not-commit\n"  # untouched
    assert (folder / "hello.txt").exists()  # the task ran and was merged


def test_folder_containing_git_projects(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path, tmp_path: Path
) -> None:
    outer = tmp_path / "workspace"
    outer.mkdir()
    (outer / "docs").mkdir()
    repo.rename(outer / "fx1")
    one = Script("", "", "", "")
    assert session(db, paths, service, one).run(outer) == 0
    assert "contains one: fx1/. Use it?" in one.text
    assert db.list_projects()[0].root_path == str((outer / "fx1").resolve())
    assert not (outer / ".git").exists()

    second = outer / "other"
    second.mkdir()
    git(second, "init", "-q")
    (second / "f.txt").write_text("x")
    git(second, "add", ".")
    git(second, "commit", "-q", "-m", "init")
    many = Script("2", "", "", "")
    assert session(db, paths, service, many).run(outer) == 0
    assert "[1] fx1/" in many.text
    assert "[2] other/" in many.text
    assert {p.name for p in db.list_projects()} == {"fx1", "other"}


def test_refuses_home_and_oversized_folders(
    db: Database,
    paths: TuaPaths,
    service: TaskService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = Path.home()
    (home / "notes.txt").write_text("x")
    script = Script()
    assert session(db, paths, service, script).run(home) == 1
    assert "your home folder" in script.text
    assert not (home / ".git").exists()

    import tua.gitsetup as gitsetup

    monkeypatch.setattr(gitsetup, "MAX_INITIAL_FILES", 2)
    folder = tmp_path / "work" / "big"
    folder.mkdir(parents=True)
    for i in range(3):
        (folder / f"f{i}.txt").write_text("x")
    big = Script()
    assert session(db, paths, service, big).run(folder) == 1
    assert "too much to add automatically" in big.text
    assert not (folder / ".git").exists()


def test_missing_git_identity_is_explained(
    db: Database,
    paths: TuaPaths,
    service: TaskService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    folder = tmp_path / "work" / "noid"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("x")
    script = Script("")
    assert session(db, paths, service, script).run(folder) == 1
    assert "user.name is not set" in script.text
    assert not (folder / ".git").exists()


def test_declining_setup_changes_nothing(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path
) -> None:
    assert session(db, paths, service, Script("n")).run(repo) == 1
    assert not (repo / ".tua").exists()
    assert db.list_projects() == []


def test_suggested_test_commands(tmp_path: Path) -> None:
    assert suggest_test_command(tmp_path) is None
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert suggest_test_command(tmp_path) == "npm test"
    (tmp_path / "pyproject.toml").write_text("")
    command = suggest_test_command(tmp_path)
    assert command is not None
    assert command.endswith("-m pytest")


def test_no_arguments_without_a_terminal_prints_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tua"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert result.returncode == 2
    assert "interactive mode" in result.stdout
