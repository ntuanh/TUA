"""Codex tasks end to end through the real adapter against a fake `codex` executable."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeClaude, FakeCodex, git
from tests.integration.test_review import ScriptedConfirmer
from tua.db import Database
from tua.errors import ConfigError
from tua.models import ErrorType, TaskStatus
from tua.paths import TuaPaths
from tua.peer_review import run_review
from tua.review import ReviewService
from tua.tasks import RunOptions, TaskService


def prompted(fake: FakeCodex) -> list[dict[str, object]]:
    return [c for c in fake.calls() if c["prompt"]]


def test_codex_task_completes_in_its_worktree(
    service: TaskService,
    db: Database,
    paths: TuaPaths,
    project: str,
    repo: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    task = service.run(
        project,
        RunOptions(
            prompt="Create hello.txt", provider="codex", model="gpt-6-luna", effort="medium"
        ),
    )
    assert task.status is TaskStatus.COMPLETED, task.error_message
    assert task.provider == "codex"
    assert task.cli_version == "0.164.0"
    assert task.model_alias == "gpt-6-luna"
    assert task.effort == "medium"
    assert task.settings_path is None  # Codex uses its own sandbox, not Claude settings
    assert task.session_id == "11111111-1111-4111-8111-111111111111"
    assert task.actual_models is None  # not reported by Codex
    worktree = Path(task.worktree_path)
    assert (worktree / "hello.txt").read_text() == "hello from fake codex\n"
    assert not (repo / "hello.txt").exists()
    (call,) = prompted(fake_codex)
    assert Path(str(call["workdir"])) == worktree
    assert Path(str(call["cwd"])) == worktree
    assert call["sandbox"] == "workspace-write"
    assert call["instructions_sha256"] == task.compiled_skill_hash  # exact payload
    assert call["prompt"] == "Create hello.txt"
    assert not call["env_has_claudecode"]
    result = (paths.task_dir(task.id) / "result.md").read_text()
    assert result == "Done."


def test_codex_read_only_profile(service: TaskService, project: str, fake_codex: FakeCodex) -> None:
    task = service.run(
        project, RunOptions(prompt="Explain", provider="codex", permission_profile="read-only")
    )
    assert task.status is TaskStatus.COMPLETED
    (call,) = prompted(fake_codex)
    assert call["sandbox"] == "read-only"
    assert not (Path(task.worktree_path) / "hello.txt").exists()


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("model_error", ErrorType.PROVIDER_ERROR),
        ("rate_limit", ErrorType.RATE_LIMITED),
        ("crash", ErrorType.PROVIDER_ERROR),
        ("invalid_output", ErrorType.INVALID_OUTPUT),
    ],
)
def test_codex_failures_keep_worktree(
    service: TaskService, project: str, fake_codex: FakeCodex, mode: str, error_type: ErrorType
) -> None:
    fake_codex.configure(mode=mode)
    task = service.run(project, RunOptions(prompt="x", provider="codex"))
    assert task.status is TaskStatus.FAILED
    assert task.error_type == error_type.value
    assert Path(task.worktree_path).is_dir()


def test_codex_model_error_message_is_readable(
    service: TaskService, project: str, fake_codex: FakeCodex
) -> None:
    fake_codex.configure(mode="model_error")
    task = service.run(project, RunOptions(prompt="x", provider="codex", model="gpt-x"))
    assert "not supported when using Codex with a ChatGPT account" in (task.error_message or "")


def test_codex_preflight_failures(
    service: TaskService, project: str, fake_codex: FakeCodex
) -> None:
    fake_codex.configure(logged_in=False)
    task = service.run(project, RunOptions(prompt="x", provider="codex"))
    assert task.error_type == ErrorType.UNAUTHENTICATED.value
    assert "codex login" in (task.error_message or "")
    assert not Path(task.worktree_path).exists()

    fake_codex.configure(unsupported_flags=["--ignore-rules"])
    task = service.run(project, RunOptions(prompt="x", provider="codex"))
    assert task.error_type == ErrorType.CLI_UNSUPPORTED.value
    assert "--ignore-rules" in (task.error_message or "")
    assert prompted(fake_codex) == []


def test_codex_timeout(service: TaskService, project: str, fake_codex: FakeCodex) -> None:
    fake_codex.configure(mode="hang")
    task = service.run(project, RunOptions(prompt="x", provider="codex", timeout_seconds=2))
    assert task.status is TaskStatus.TIMEOUT
    assert Path(task.worktree_path).is_dir()


def test_codex_missing(service: TaskService, project: str) -> None:
    task = service.run(project, RunOptions(prompt="x", provider="codex"))
    assert task.error_type == ErrorType.CLI_MISSING.value


def test_effort_is_validated_per_provider(service: TaskService, project: str) -> None:
    with pytest.raises(ConfigError, match="not valid for codex"):
        service.run(project, RunOptions(prompt="x", provider="codex", effort="max"))
    with pytest.raises(ConfigError, match="not valid for claude-code"):
        service.run(project, RunOptions(prompt="x", effort="minimal"))


def test_claude_effort_flag_is_passed(
    service: TaskService, project: str, fake_claude: FakeClaude
) -> None:
    task = service.run(project, RunOptions(prompt="x", effort="high"))
    assert task.effort == "high"
    assert task.status is TaskStatus.COMPLETED, task.error_message
    argv = next(c for c in fake_claude.calls() if c["prompt"])["argv"]
    assert argv[argv.index("--effort") + 1] == "high"


def test_cross_provider_review(
    service: TaskService,
    db: Database,
    paths: TuaPaths,
    project: str,
    repo: Path,
    fake_claude: FakeClaude,
    fake_codex: FakeCodex,
) -> None:
    target = service.run(project, RunOptions(prompt="Create hello.txt"))  # Claude writes
    assert target.provider == "claude-code"
    fake_codex.configure(mode="success", result="CONFIRMED low: hello.txt lacks a newline test.")
    review_service = ReviewService(db, paths, service, ScriptedConfirmer(), lambda _t: None)
    outcome = run_review(
        service, review_service, paths, target.id, provider="codex", model="gpt-6-luna"
    )
    assert outcome.completed
    assert outcome.task.provider == "codex"
    assert outcome.task.permission_profile == "read-only"
    assert "CONFIRMED low" in (outcome.text or "")
    (call,) = prompted(fake_codex)
    assert call["sandbox"] == "read-only"
    assert "+hello from fake claude" in str(call["prompt"])
    assert git(repo, "status", "--porcelain") == ""


def test_project_default_provider_codex(
    db: Database, paths: TuaPaths, service: TaskService, repo: Path, fake_codex: FakeCodex
) -> None:
    from tua.projects import init_project

    init_project(db, paths, repo, "cx")
    config = repo / ".tua" / "project.yaml"
    text = config.read_text().replace("default_provider: claude-code", "default_provider: codex")
    config.write_text(
        text.replace("# default_model: sonnet", "default_model: gpt-6-luna\neffort: medium")
    )
    git(repo, "add", ".tua")
    git(repo, "commit", "-q", "-m", "codex config")
    init_project(db, paths, repo, "cx")
    task = service.run("cx", RunOptions(prompt="Create hello.txt"))
    assert task.provider == "codex"
    assert task.effort == "medium"
    assert task.model_alias == "gpt-6-luna"
    assert task.status is TaskStatus.COMPLETED
