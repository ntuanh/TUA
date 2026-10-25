"""`tua review`, per-task permission profiles, `project init --allow`, and migration 2."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.conftest import FakeClaude, git
from tests.integration.test_review import ScriptedConfirmer
from tua.db import MIGRATIONS, SCHEMA_VERSION, Database
from tua.errors import ConfigError, TuaError
from tua.models import TaskStatus
from tua.paths import TuaPaths
from tua.peer_review import run_review
from tua.permissions import narrow_profile
from tua.projects import init_project
from tua.review import ReviewService
from tua.tasks import RunOptions, TaskService


@pytest.fixture
def review_service(db: Database, paths: TuaPaths, service: TaskService) -> ReviewService:
    return ReviewService(db, paths, service, ScriptedConfirmer(), lambda _text: None)


def test_narrow_profile_only_restricts() -> None:
    assert narrow_profile("safe", None) == "safe"
    assert narrow_profile("safe", "read-only") == "read-only"
    assert narrow_profile("read-only", "read-only") == "read-only"
    with pytest.raises(ConfigError, match="widen"):
        narrow_profile("read-only", "safe")
    with pytest.raises(ConfigError, match="unknown"):
        narrow_profile("safe", "admin")


def test_run_with_read_only_profile(
    service: TaskService, project: str, fake_claude: FakeClaude
) -> None:
    task = service.run(project, RunOptions(prompt="look only", permission_profile="read-only"))
    assert task.permission_profile == "read-only"
    (call,) = [c for c in fake_claude.calls() if c["prompt"]]
    deny = call["settings"]["permissions"]["deny"]
    assert "Edit" in deny
    assert "Bash" in deny
    assert not any(rule.startswith("Edit(") for rule in call["settings"]["permissions"]["allow"])


def test_full_result_text_is_saved(
    service: TaskService, paths: TuaPaths, project: str, fake_claude: FakeClaude
) -> None:
    fake_claude.configure(mode="success", result="line one\n" + "x" * 5000)
    task = service.run(project, RunOptions(prompt="x"))
    saved = (paths.task_dir(task.id) / "result.md").read_text()
    assert saved.startswith("line one")
    assert len(saved) > 5000
    assert task.result_excerpt is not None
    assert len(task.result_excerpt) < 2100


def test_review_is_a_recorded_read_only_task(
    service: TaskService,
    review_service: ReviewService,
    db: Database,
    paths: TuaPaths,
    project: str,
    repo: Path,
    fake_claude: FakeClaude,
) -> None:
    fake_claude.configure(
        mode="success", write_files={"calc.py": "def add(a, b):\n    return a - b\n"}
    )
    target = service.run(project, RunOptions(prompt="Write add(a, b)"))
    fake_claude.configure(
        mode="success",
        write_files={},
        result="CONFIRMED high: calc.py:2 subtracts instead of adds.",
    )
    outcome = run_review(service, review_service, paths, target.id, model="sonnet", focus="math")
    review = outcome.task

    assert outcome.completed
    assert review.review_of == target.id
    assert review.permission_profile == "read-only"
    assert review.model_alias == "sonnet"
    assert "subtracts instead of adds" in (outcome.text or "")
    assert db.list_reviews(target.id)[0].id == review.id
    # Its worktree was a read-only copy of the reviewed result and has been removed.
    assert review.status is TaskStatus.DISCARDED
    assert not Path(review.worktree_path).exists()
    # The reviewed task itself is untouched and still mergeable.
    assert db.get_task(target.id).status is TaskStatus.COMPLETED
    assert (Path(target.worktree_path) / "calc.py").exists()
    assert git(repo, "status", "--porcelain") == ""

    call = [c for c in fake_claude.calls() if c["prompt"]][-1]
    assert "Write add(a, b)" in call["prompt"]  # original task prompt
    assert "+    return a - b" in call["prompt"]  # the diff
    assert "Pay particular attention to: math" in call["prompt"]
    assert "Do not invent problems" in call["prompt"]
    # The reviewer ran on a snapshot that contains the reviewed changes.
    assert (Path(call["cwd"]).name) == review.id
    assert review.base_commit != target.base_commit
    show = git(repo, "show", f"{review.base_commit}:calc.py")
    assert "return a - b" in show


def test_review_keep_worktree_and_merged_tasks(
    service: TaskService,
    review_service: ReviewService,
    paths: TuaPaths,
    project: str,
    fake_claude: FakeClaude,
) -> None:
    target = service.run(project, RunOptions(prompt="Create hello.txt"))
    merged = review_service.merge(target.id, assume_yes=True)
    fake_claude.configure(mode="success", write_files={}, result="Looks correct.")
    outcome = run_review(service, review_service, paths, merged.id, keep_worktree=True)
    assert outcome.task.base_commit == merged.merged_commit
    assert outcome.task.status is TaskStatus.COMPLETED
    assert Path(outcome.task.worktree_path).is_dir()


def test_review_refusals(
    service: TaskService,
    review_service: ReviewService,
    paths: TuaPaths,
    project: str,
    fake_claude: FakeClaude,
) -> None:
    fake_claude.configure(mode="success", write_files={})
    empty = service.run(project, RunOptions(prompt="do nothing"))
    with pytest.raises(TuaError, match="no changes"):
        run_review(service, review_service, paths, empty.id)
    review_service.discard(empty.id, assume_yes=True)
    with pytest.raises(TuaError, match="discarded"):
        run_review(service, review_service, paths, empty.id)

    fake_claude.configure(mode="success")
    target = service.run(project, RunOptions(prompt="Create hello.txt"))
    outcome = run_review(service, review_service, paths, target.id, keep_worktree=True)
    with pytest.raises(TuaError, match="itself a review"):
        run_review(service, review_service, paths, outcome.task.id)


def test_failed_review_is_reported_and_kept(
    service: TaskService,
    review_service: ReviewService,
    paths: TuaPaths,
    project: str,
    fake_claude: FakeClaude,
) -> None:
    target = service.run(project, RunOptions(prompt="Create hello.txt"))
    fake_claude.configure(mode="rate_limit")
    outcome = run_review(service, review_service, paths, target.id)
    assert outcome.task.status is TaskStatus.FAILED
    assert not outcome.completed
    assert outcome.task.error_type == "rate_limited"
    assert outcome.text is None
    assert Path(outcome.task.worktree_path).is_dir()


def test_project_init_allow(db: Database, paths: TuaPaths, repo: Path) -> None:
    with pytest.raises(ConfigError, match="shell syntax"):
        init_project(db, paths, repo, "demo", allow=["pytest; rm -rf /"])
    assert not (repo / ".tua" / "project.yaml").exists()  # nothing written on error
    with pytest.raises(ConfigError, match="always-denied"):
        init_project(db, paths, repo, "demo", allow=["git push"])
    assert not (repo / ".tua").exists()
    result = init_project(db, paths, repo, "demo", allow=["python -m pytest", "ruff check"])
    assert result.config.commands.allow == (("python", "-m", "pytest"), ("ruff", "check"))
    with pytest.raises(ConfigError, match="already exists"):
        init_project(db, paths, repo, "demo", allow=["make test"])


def test_migration_from_v1_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "tua.db"
    conn = sqlite3.connect(path)
    for statement in MIGRATIONS[1]:
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO projects (name, root_path, config_path, config_fingerprint, created_at,"
        " updated_at) VALUES ('p', '/r', '/c', 'f', 't', 't')"
    )
    conn.commit()
    conn.close()

    db = Database.open(path)
    assert db.schema_version() == SCHEMA_VERSION == 3
    assert [p.name for p in db.list_projects()] == ["p"]
    columns = {r[1] for r in db.conn.execute("PRAGMA table_info(tasks)")}
    assert "review_of" in columns
    db.close()
