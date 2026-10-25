"""Starting TUA outside or below the project: read-only context folder and focus subfolder."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeClaude, git
from tests.integration.test_interactive import Script, session
from tua.config import CommandPolicy
from tua.db import Database
from tua.errors import ConfigError
from tua.paths import TuaPaths
from tua.permissions import build_plan
from tua.tasks import RunOptions, TaskService


def prompted(fake: FakeClaude) -> list[dict[str, object]]:
    return [c for c in fake.calls() if c["prompt"]]


@pytest.fixture
def workspace(repo: Path, tmp_path: Path) -> Path:
    """A folder like ~/dev/FX1: docs next to a Git project."""
    outer = tmp_path / "FX1"
    outer.mkdir()
    (outer / "docs").mkdir()
    (outer / "docs" / "spec.txt").write_text("the spec\n")
    repo.rename(outer / "fx1")
    return outer


def test_plan_makes_context_readable_but_not_editable(tmp_path: Path, paths: TuaPaths) -> None:
    context = tmp_path / "FX1"
    context.mkdir()
    plan = build_plan(
        "safe",
        worktree=tmp_path / "wt",
        commands=CommandPolicy((), ()),
        paths=paths,
        context_dir=context,
    )
    assert f"Read(/{context.resolve()}/**)" in plan.allow
    assert f"Edit(/{context.resolve()}/**)" in plan.deny
    assert f"Edit(/{(tmp_path / 'wt').resolve()}/**)" in plan.allow


def test_task_with_context_folder(
    db: Database, paths: TuaPaths, service: TaskService, workspace: Path, fake_claude: FakeClaude
) -> None:
    from tua.projects import init_project

    root = workspace / "fx1"
    init_project(db, paths, root, "fx1")
    git(root, "add", ".tua")
    git(root, "commit", "-q", "-m", "tua")
    task = service.run("fx1", RunOptions(prompt="Summarize the spec", context_dir=workspace))
    assert task.context_dir == str(workspace.resolve())
    assert task.prompt == "Summarize the spec"  # the stored request is the user's own
    call = prompted(fake_claude)[0]
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--add-dir") + 1] == str(workspace.resolve())
    assert f"started TUA in {workspace.resolve()}" in str(call["prompt"])
    assert str(call["prompt"]).endswith("Summarize the spec")
    settings = call["settings"]
    assert isinstance(settings, dict)
    assert f"Edit(/{workspace.resolve()}/**)" in settings["permissions"]["deny"]


def test_context_inside_the_project_is_dropped(
    service: TaskService, project: str, repo: Path, fake_claude: FakeClaude
) -> None:
    task = service.run(project, RunOptions(prompt="x", context_dir=repo, start_dir="src/"))
    assert task.context_dir is None
    assert task.start_dir == "src"
    prompt = str(prompted(fake_claude)[0]["prompt"])
    assert "subfolder 'src'" in prompt
    assert "--add-dir" not in prompted(fake_claude)[0]["argv"]  # type: ignore[operator]


def test_unsafe_context_folders_are_refused(
    service: TaskService, project: str, paths: TuaPaths, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match="refusing to let the provider read all of"):
        service.run(project, RunOptions(prompt="x", context_dir=Path.home()))
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ConfigError, match="contains TUA's own files"):
        service.run(project, RunOptions(prompt="x", context_dir=paths.state_dir.parent))


def test_interactive_in_a_workspace_folder(
    db: Database, paths: TuaPaths, service: TaskService, workspace: Path, fake_claude: FakeClaude
) -> None:
    first = Script("", "", "", "")  # use fx1/, set up, keep local, quit
    assert session(db, paths, service, first).run(workspace) == 0
    assert "contains one: fx1/. Use it?" in first.text
    assert "can also read (not change)" in first.text

    again = Script("Summarize docs/spec.txt", "", "")
    assert session(db, paths, service, again).run(workspace) == 0
    assert "Use it?" not in again.text  # remembered: no question the second time
    (task,) = db.list_tasks()
    assert task.context_dir == str(workspace.resolve())


def test_interactive_in_a_project_subfolder(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path, fake_claude: FakeClaude
) -> None:
    (repo / "sw" / "drivers").mkdir(parents=True)
    (repo / "sw" / "drivers" / "uart.c").write_text("/* uart */\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "drivers")
    script = Script("", "", "Fix the UART driver", "", "")
    assert session(db, paths, service, script).run(repo / "sw" / "drivers") == 0
    assert "Focus: sw/drivers/" in script.text
    (task,) = db.list_tasks()
    assert task.start_dir == "sw/drivers"
    assert task.context_dir is None
