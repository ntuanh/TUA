"""End-to-end task lifecycle through the real adapter against a fake `claude` executable."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import FakeClaude, git
from tua.db import Database
from tua.errors import DirtyRepositoryError, RepositoryError, SecretDetectedError
from tua.models import ErrorType, TaskStatus
from tua.paths import TuaPaths
from tua.tasks import RunOptions, TaskService


def statuses(db: Database, task_id: str) -> list[str]:
    return [e.to_status for e in db.list_events(task_id) if e.to_status]


def test_successful_task(
    service: TaskService,
    db: Database,
    paths: TuaPaths,
    project: str,
    repo: Path,
    fake_claude: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")  # as if TUA itself ran inside Claude Code
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "parent-session-token")
    base = git(repo, "rev-parse", "HEAD").strip()
    task = service.run(project, RunOptions(prompt="Create hello.txt"))

    assert task.status is TaskStatus.COMPLETED
    assert statuses(db, task.id) == ["created", "preparing", "running", "completed"]
    assert task.base_commit == base
    assert task.target_branch == "main"
    assert task.cli_version == "2.1.301"
    assert task.source_skill_hash
    assert task.compiled_skill_hash
    assert task.actual_models == ("claude-fake-model-1",)
    assert task.session_id
    assert task.num_turns == 2
    assert task.exit_code == 0
    assert task.permission_denials == 0

    worktree = Path(task.worktree_path)
    assert worktree.parent == paths.worktrees_dir
    assert (worktree / "hello.txt").read_text() == "hello from fake claude\n"
    assert not (repo / "hello.txt").exists()  # the user's checkout is untouched
    assert git(repo, "status", "--porcelain") == ""
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() == f"tua/{task.id}"

    calls = [c for c in fake_claude.calls() if c["prompt"]]
    assert len(calls) == 1
    call = calls[0]
    assert Path(call["cwd"]) == worktree  # provider ran in the task worktree
    assert call["prompt"] == "Create hello.txt"  # via stdin, not argv
    assert "Create hello.txt" not in call["argv"]
    assert call["append_sha256"] == task.compiled_skill_hash  # exact fingerprinted payload
    assert not call["env_has_claudecode"]
    assert not call["env_has_messaging_token"]
    assert "--dangerously-skip-permissions" not in call["argv"]
    allow = call["settings"]["permissions"]["allow"]
    assert f"Edit(/{worktree.resolve()}/**)" in allow

    assert Path(task.stdout_path or "").stat().st_mode & 0o077 == 0
    assert Path(task.settings_path or "").stat().st_mode & 0o077 == 0


def test_dirty_checkout_is_refused_and_untouched(
    service: TaskService, db: Database, project: str, repo: Path, fake_claude: FakeClaude
) -> None:
    (repo / "README.md").write_text("modified\n")
    (repo / "notes.txt").write_text("untracked\n")
    before = git(repo, "status", "--porcelain")
    with pytest.raises(DirtyRepositoryError) as info:
        service.run(project, RunOptions(prompt="x"))
    assert "notes.txt" in info.value.message
    assert "README.md" in info.value.message
    assert "never stashes" in (info.value.hint or "")
    assert git(repo, "status", "--porcelain") == before
    assert (repo / "README.md").read_text() == "modified\n"
    assert db.list_tasks() == []
    assert fake_claude.calls() == []


def test_ignored_files_do_not_make_the_checkout_dirty(
    service: TaskService, project: str, repo: Path, fake_claude: FakeClaude
) -> None:
    (repo / "build").mkdir()
    (repo / "build" / "artifact.o").write_text("x")
    assert service.run(project, RunOptions(prompt="x")).status is TaskStatus.COMPLETED


def test_detached_head_is_refused(service: TaskService, project: str, repo: Path) -> None:
    git(repo, "checkout", "-q", "--detach")
    with pytest.raises(RepositoryError, match="detached HEAD"):
        service.run(project, RunOptions(prompt="x"))


def test_secret_in_prompt_is_refused(service: TaskService, db: Database, project: str) -> None:
    token = "gh" + "p_" + "B" * 36
    with pytest.raises(SecretDetectedError):
        service.run(project, RunOptions(prompt=f"use {token} to deploy"))
    assert db.list_tasks() == []


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("provider_error", ErrorType.PROVIDER_ERROR),
        ("auth_error", ErrorType.UNAUTHENTICATED),
        ("rate_limit", ErrorType.RATE_LIMITED),
        ("crash", ErrorType.PROVIDER_ERROR),
        ("invalid_output", ErrorType.INVALID_OUTPUT),
    ],
)
def test_provider_failures_keep_the_worktree(
    service: TaskService,
    db: Database,
    project: str,
    fake_claude: FakeClaude,
    mode: str,
    error_type: ErrorType,
) -> None:
    fake_claude.configure(mode=mode)
    task = service.run(project, RunOptions(prompt="x"))
    assert task.status is TaskStatus.FAILED
    assert task.error_type == error_type.value
    assert statuses(db, task.id) == ["created", "preparing", "running", "failed"]
    assert Path(task.worktree_path).is_dir()


def test_not_logged_in_fails_before_creating_a_worktree(
    service: TaskService, project: str, fake_claude: FakeClaude
) -> None:
    fake_claude.configure(mode="success", logged_in=False)
    task = service.run(project, RunOptions(prompt="x"))
    assert task.status is TaskStatus.FAILED
    assert task.error_type == ErrorType.UNAUTHENTICATED.value
    assert "claude auth login" in (task.error_message or "")
    assert not Path(task.worktree_path).exists()
    assert [c for c in fake_claude.calls() if c["prompt"]] == []


def test_unsupported_flag_fails_closed(
    service: TaskService, project: str, fake_claude: FakeClaude
) -> None:
    fake_claude.configure(mode="success", unsupported_flags=["--append-system-prompt-file"])
    task = service.run(project, RunOptions(prompt="x"))
    assert task.status is TaskStatus.FAILED
    assert task.error_type == ErrorType.CLI_UNSUPPORTED.value
    assert "--append-system-prompt-file" in (task.error_message or "")
    assert not Path(task.worktree_path).exists()


def test_missing_cli(service: TaskService, project: str) -> None:
    task = service.run(project, RunOptions(prompt="x"))  # no fake_claude fixture: not on PATH
    assert task.status is TaskStatus.FAILED
    assert task.error_type == ErrorType.CLI_MISSING.value


def test_timeout_keeps_worktree_and_kills_provider(
    service: TaskService, project: str, fake_claude: FakeClaude, tmp_path: Path
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    fake_claude.configure(mode="hang", grandchild_pid_file=str(pid_file))
    task = service.run(project, RunOptions(prompt="x", timeout_seconds=2))
    assert task.status is TaskStatus.TIMEOUT
    assert task.error_type == ErrorType.TIMEOUT.value
    assert Path(task.worktree_path).is_dir()
    grandchild = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        subprocess.run(["sleep", "0.05"], check=True)
    else:
        pytest.fail("provider grandchild survived the timeout")


def test_duplicate_task_id_is_retried(
    db: Database, paths: TuaPaths, project: str, fake_claude: FakeClaude
) -> None:
    from tua.adapters import create_adapter

    ids = iter(["20260101-000000-aaaa", "20260101-000000-aaaa", "20260101-000000-bbbb"])
    service = TaskService(
        db,
        paths,
        lambda provider: create_adapter(provider, scratch_dir=paths.tmp_dir),
        id_factory=lambda: next(ids),
    )
    first = service.run(project, RunOptions(prompt="one"))
    second = service.run(project, RunOptions(prompt="two"))
    assert (first.id, second.id) == ("20260101-000000-aaaa", "20260101-000000-bbbb")


def test_failed_worktree_creation_is_recorded(
    service: TaskService,
    db: Database,
    paths: TuaPaths,
    project: str,
    repo: Path,
    fake_claude: FakeClaude,
) -> None:
    from tua.adapters import create_adapter

    git(repo, "branch", "tua/20260101-000000-cccc")  # collides with the task branch
    service = TaskService(
        db,
        paths,
        lambda provider: create_adapter(provider, scratch_dir=paths.tmp_dir),
        id_factory=lambda: "20260101-000000-cccc",
    )
    task = service.run(project, RunOptions(prompt="x"))
    assert task.status is TaskStatus.FAILED
    assert task.error_type == ErrorType.WORKTREE_ERROR.value
    assert "already exists" in (task.error_message or "")
    # The pre-existing branch is the user's: it must not have been touched.
    assert git(repo, "rev-parse", "tua/20260101-000000-cccc") == git(repo, "rev-parse", "HEAD")


def test_interrupted_runner_is_reconciled(
    service: TaskService, db: Database, project: str, fake_claude: FakeClaude
) -> None:
    task = service.run(project, RunOptions(prompt="x"))
    # Simulate a TUA process that died while the task was running: a fresh task stuck in
    # 'running' whose runner PID no longer exists.
    stuck = db.insert_task(
        {
            **{
                k: getattr(task, k)
                for k in (
                    "project_id",
                    "prompt",
                    "prompt_sha256",
                    "source_repo",
                    "target_branch",
                    "base_commit",
                    "provider",
                    "permission_profile",
                    "config_fingerprint",
                    "timeout_seconds",
                )
            },
            "id": "20260101-000000-dddd",
            "task_branch": "tua/20260101-000000-dddd",
            "worktree_path": task.worktree_path + "-stuck",
            "runner_pid": 2**22 + 4321,
            "runner_host": None,
        }
    )
    db.transition(stuck.id, TaskStatus.RUNNING)
    reconciled = service.get(stuck.id)
    assert reconciled.status is TaskStatus.FAILED
    assert reconciled.error_type == ErrorType.INTERRUPTED.value
    # A live runner (this process) is never reconciled.
    live = db.insert_task(
        {
            **{
                k: getattr(task, k)
                for k in (
                    "project_id",
                    "prompt",
                    "prompt_sha256",
                    "source_repo",
                    "target_branch",
                    "base_commit",
                    "provider",
                    "permission_profile",
                    "config_fingerprint",
                    "timeout_seconds",
                )
            },
            "id": "20260101-000000-eeee",
            "task_branch": "tua/20260101-000000-eeee",
            "worktree_path": task.worktree_path + "-live",
            "runner_pid": os.getpid(),
        }
    )
    assert service.get(live.id).status is TaskStatus.PREPARING


def test_orphaned_provider_blocks_reconciliation(
    service: TaskService, db: Database, project: str, fake_claude: FakeClaude
) -> None:
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        stuck = db.insert_task(
            {
                "id": "20260101-000000-ffff",
                "project_id": 1,
                "prompt": "x",
                "prompt_sha256": "sha256:x",
                "source_repo": "/r",
                "target_branch": "main",
                "base_commit": "a" * 40,
                "task_branch": "tua/20260101-000000-ffff",
                "worktree_path": "/nonexistent/ffff",
                "provider": "claude-code",
                "permission_profile": "safe",
                "config_fingerprint": "sha256:c",
                "timeout_seconds": 10,
                "runner_pid": 2**22 + 4322,
            }
        )
        db.transition(stuck.id, TaskStatus.RUNNING, fields={"provider_pid": sleeper.pid})
        task = service.get(stuck.id)
        assert task.status is TaskStatus.RUNNING
        assert service.orphaned_provider(task)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert service.get("20260101-000000-ffff").status is TaskStatus.FAILED
