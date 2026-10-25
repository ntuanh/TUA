"""diff / merge / discard / feedback against real repositories and worktrees."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.conftest import FakeClaude, git
from tua.db import Database
from tua.errors import AbortedError, DirtyRepositoryError, MergeRefusedError, TuaError
from tua.feedback import record_feedback
from tua.models import Task, TaskStatus
from tua.paths import TuaPaths
from tua.review import ReviewService, task_diff
from tua.tasks import RunOptions, TaskService


@dataclass
class ScriptedConfirmer:
    answers: list[bool] = field(default_factory=list)
    typed: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    def confirm(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else False

    def confirm_typed(self, prompt: str, expected: str) -> bool:
        self.prompts.append(prompt)
        return bool(self.typed) and self.typed.pop(0) == expected


@dataclass
class Harness:
    service: TaskService
    review: ReviewService
    confirmer: ScriptedConfirmer
    output: list[str]
    db: Database
    repo: Path

    def run(self, **control: object) -> Task:
        return self.service.run("demo", RunOptions(prompt="Add greeting files"))


@pytest.fixture
def h(
    service: TaskService,
    db: Database,
    paths: TuaPaths,
    project: str,
    repo: Path,
    fake_claude: FakeClaude,
) -> Harness:
    confirmer = ScriptedConfirmer()
    output: list[str] = []
    review = ReviewService(db, paths, service, confirmer, output.append)
    fake_claude.configure(
        mode="success", write_files={"hello.txt": "hello\n", "src/new_module.py": "X = 1\n"}
    )
    return Harness(service, review, confirmer, output, db, repo)


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def test_diff_includes_new_untracked_files_without_touching_the_worktree(
    h: Harness, paths: TuaPaths
) -> None:
    task = h.run()
    worktree = Path(task.worktree_path)
    before = git(worktree, "status", "--porcelain")
    result = task_diff(task, paths)
    assert result.source == "worktree"
    assert result.has_changes
    assert "hello.txt" in result.names
    assert "src/new_module.py" in result.names
    assert "+hello" in result.patch
    assert git(worktree, "status", "--porcelain") == before  # real index untouched


def test_merge_creates_one_local_commit_and_cleans_up(h: Harness, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(h.repo, "remote", "add", "origin", str(remote))
    git(h.repo, "push", "-q", "origin", "main")
    remote_before = git(remote, "rev-parse", "main")

    task = h.run()
    base = head(h.repo)
    h.confirmer.answers = [True]
    merged = h.review.merge(task.id)

    assert merged.status is TaskStatus.MERGED
    assert merged.cleanup_status == "done"
    assert head(h.repo) == merged.merged_commit
    assert git(h.repo, "rev-parse", "HEAD^").strip() == base  # exactly one new commit
    assert (h.repo / "hello.txt").read_text() == "hello\n"
    assert git(h.repo, "status", "--porcelain") == ""
    assert git(remote, "rev-parse", "main") == remote_before  # never pushed
    author = git(h.repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip()
    assert author == "TUA Test <tua-test@example.invalid>|TUA Test <tua-test@example.invalid>"
    message = git(h.repo, "log", "-1", "--format=%B")
    assert f"TUA-Task-ID: {task.id}" in message
    assert "co-authored-by" not in message.lower()
    assert not Path(task.worktree_path).exists()
    assert git(h.repo, "branch", "--list", f"tua/{task.id}") == ""
    assert any("Merge task" in p for p in h.confirmer.prompts)
    assert any("+hello" in chunk for chunk in h.output)  # the diff was shown first


def test_custom_commit_message(h: Harness) -> None:
    task = h.run()
    h.review.merge(task.id, message="feat: greet", assume_yes=True)
    message = git(h.repo, "log", "-1", "--format=%B")
    assert message.startswith("feat: greet")
    assert f"TUA-Task-ID: {task.id}" in message


def test_declined_merge_changes_nothing(h: Harness) -> None:
    task = h.run()
    before = head(h.repo)
    h.confirmer.answers = [False]
    with pytest.raises(AbortedError):
        h.review.merge(task.id)
    assert head(h.repo) == before
    assert h.db.get_task(task.id).status is TaskStatus.COMPLETED
    assert Path(task.worktree_path).is_dir()


def test_dirty_target_is_refused(h: Harness) -> None:
    task = h.run()
    (h.repo / "README.md").write_text("local edit\n")
    with pytest.raises(DirtyRepositoryError):
        h.review.merge(task.id, assume_yes=True)
    assert (h.repo / "README.md").read_text() == "local edit\n"
    assert h.db.get_task(task.id).status is TaskStatus.COMPLETED
    assert any(e.event == "merge_refused" for e in h.db.list_events(task.id))


def test_target_that_advanced_is_refused(h: Harness) -> None:
    task = h.run()
    (h.repo / "other.txt").write_text("upstream change\n")
    git(h.repo, "add", "other.txt")
    git(h.repo, "commit", "-q", "-m", "advance")
    advanced = head(h.repo)
    with pytest.raises(MergeRefusedError, match="has moved") as info:
        h.review.merge(task.id, assume_yes=True)
    assert task.task_branch in (info.value.hint or "")
    assert head(h.repo) == advanced
    assert Path(task.worktree_path).is_dir()


def test_wrong_branch_is_refused(h: Harness) -> None:
    task = h.run()
    git(h.repo, "switch", "-q", "-c", "feature")
    with pytest.raises(MergeRefusedError, match="targets 'main'"):
        h.review.merge(task.id, assume_yes=True)


def test_ignored_file_in_target_is_never_overwritten(h: Harness, fake_claude: FakeClaude) -> None:
    # The task un-ignores build/ and adds build/keep.txt, where the user has an ignored file.
    fake_claude.configure(
        mode="success", write_files={".gitignore": "", "build/keep.txt": "task\n"}
    )
    task = h.run()
    (h.repo / "build").mkdir()
    (h.repo / "build" / "keep.txt").write_text("USER DATA\n")
    before = head(h.repo)
    with pytest.raises(MergeRefusedError, match="refused to fast-forward"):
        h.review.merge(task.id, assume_yes=True)
    assert (h.repo / "build" / "keep.txt").read_text() == "USER DATA\n"
    assert head(h.repo) == before
    assert h.db.get_task(task.id).status is TaskStatus.COMPLETED
    assert Path(task.worktree_path).is_dir()


def test_unresolved_conflict_in_worktree_is_refused(h: Harness) -> None:
    task = h.run()
    worktree = Path(task.worktree_path)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "task work")
    git(worktree, "switch", "-q", "-c", "side", "HEAD~1")
    (worktree / "hello.txt").write_text("conflicting\n")
    git(worktree, "add", "hello.txt")
    git(worktree, "commit", "-q", "-m", "side")
    with pytest.raises(subprocess.CalledProcessError):  # the merge stops with a conflict
        git(worktree, "merge", f"tua/{task.id}")
    with pytest.raises(MergeRefusedError, match=r"merge in progress|unresolved conflicts"):
        h.review.merge(task.id, assume_yes=True)


def test_task_without_changes_is_not_merged(h: Harness, fake_claude: FakeClaude) -> None:
    fake_claude.configure(mode="success", write_files={})
    task = h.run()
    with pytest.raises(MergeRefusedError, match="no changes"):
        h.review.merge(task.id, assume_yes=True)


def test_embedded_repository_is_refused(h: Harness, fake_claude: FakeClaude) -> None:
    task = h.run()
    nested = Path(task.worktree_path) / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    (nested / "f").write_text("x")
    git(nested, "add", "f")
    git(nested, "commit", "-q", "-m", "nested")
    with pytest.raises(MergeRefusedError, match="embedded Git repositories"):
        h.review.merge(task.id, assume_yes=True)


def test_failed_task_requires_force_and_double_confirmation(
    h: Harness, fake_claude: FakeClaude
) -> None:
    fake_claude.configure(mode="provider_error")
    task = h.run()
    (Path(task.worktree_path) / "partial.txt").write_text("partial\n")
    before = head(h.repo)

    with pytest.raises(MergeRefusedError, match="--force"):
        h.review.merge(task.id)
    # --yes never satisfies the confirmations of a force-merge.
    h.confirmer.answers, h.confirmer.typed = [False], []
    with pytest.raises(AbortedError):
        h.review.merge(task.id, force=True, assume_yes=True)
    h.confirmer.answers, h.confirmer.typed = [True], ["wrong-id"]
    with pytest.raises(AbortedError, match="not confirmed"):
        h.review.merge(task.id, force=True)
    assert head(h.repo) == before

    h.confirmer.answers, h.confirmer.typed = [True], [task.id]
    merged = h.review.merge(task.id, force=True)
    assert merged.status is TaskStatus.MERGED
    assert any("WARNING" in chunk for chunk in h.output)
    assert (h.repo / "partial.txt").exists()


def test_force_does_not_bypass_safety_checks(h: Harness, fake_claude: FakeClaude) -> None:
    fake_claude.configure(mode="provider_error")
    task = h.run()
    (Path(task.worktree_path) / "partial.txt").write_text("partial\n")
    (h.repo / "README.md").write_text("dirty\n")
    h.confirmer.answers, h.confirmer.typed = [True], [task.id]
    with pytest.raises(DirtyRepositoryError):
        h.review.merge(task.id, force=True)


def test_discard_removes_only_task_resources(h: Harness) -> None:
    git(h.repo, "branch", "keep-me")
    task = h.run()
    h.confirmer.answers = [True]
    discarded = h.review.discard(task.id)
    assert discarded.status is TaskStatus.DISCARDED
    assert not Path(task.worktree_path).exists()
    assert git(h.repo, "branch", "--list", f"tua/{task.id}") == ""
    assert git(h.repo, "branch", "--list", "keep-me").strip() == "keep-me"
    with pytest.raises(TuaError, match="already discarded"):
        h.review.discard(task.id, assume_yes=True)
    with pytest.raises(MergeRefusedError, match="discarded"):
        h.review.merge(task.id, assume_yes=True)


def test_declined_discard_keeps_everything(h: Harness) -> None:
    task = h.run()
    h.confirmer.answers = [False]
    with pytest.raises(AbortedError):
        h.review.discard(task.id)
    assert Path(task.worktree_path).is_dir()
    assert h.db.get_task(task.id).status is TaskStatus.COMPLETED


def test_discard_works_for_failed_and_timed_out_tasks(h: Harness, fake_claude: FakeClaude) -> None:
    fake_claude.configure(mode="crash")
    failed = h.run()
    assert h.review.discard(failed.id, assume_yes=True).status is TaskStatus.DISCARDED


def test_discard_when_worktree_was_deleted_manually(h: Harness) -> None:
    import shutil

    task = h.run()
    shutil.rmtree(task.worktree_path)
    assert h.review.discard(task.id, assume_yes=True).status is TaskStatus.DISCARDED
    assert "tua/" not in git(h.repo, "worktree", "list")


def test_cleanup_failure_is_recorded_and_retryable(h: Harness) -> None:
    task = h.run()
    git(h.repo, "worktree", "lock", task.worktree_path)  # makes `worktree remove --force` fail
    merged = h.review.merge(task.id, assume_yes=True)
    assert merged.status is TaskStatus.MERGED
    assert merged.cleanup_status == "failed"
    assert merged.cleanup_error
    assert Path(task.worktree_path).is_dir()
    assert any(e.event == "cleanup_failed" for e in h.db.list_events(task.id))
    assert any("cleanup failed" in chunk for chunk in h.output)

    git(h.repo, "worktree", "unlock", task.worktree_path)
    retried = h.review.discard(task.id, assume_yes=True)
    assert retried.status is TaskStatus.MERGED
    assert retried.cleanup_status == "done"
    assert not Path(task.worktree_path).exists()


def test_discard_failure_keeps_state(h: Harness) -> None:
    task = h.run()
    git(h.repo, "worktree", "lock", task.worktree_path)
    with pytest.raises(TuaError, match="could not discard"):
        h.review.discard(task.id, assume_yes=True)
    assert h.db.get_task(task.id).status is TaskStatus.COMPLETED
    assert Path(task.worktree_path).is_dir()


def test_diff_of_merged_task_uses_the_commit(h: Harness, paths: TuaPaths) -> None:
    task = h.run()
    merged = h.review.merge(task.id, assume_yes=True)
    result = task_diff(merged, paths)
    assert result.source == "merged-commit"
    assert "hello.txt" in result.names


# ------------------------------------------------------------------------- feedback


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + path.read_bytes())
    return digest.hexdigest()


def test_feedback_is_bound_and_changes_nothing_else(h: Harness) -> None:
    task = h.run()
    skills_before = _hash_tree(h.repo / ".tua")
    good = record_feedback(h.db, h.service, task.id, "good", note="nice")
    bad = record_feedback(h.db, h.service, task.id, "bad", reason="missed edge case")
    after = h.db.get_task(task.id)
    assert after.status is TaskStatus.COMPLETED
    assert after.updated_at == task.updated_at
    assert _hash_tree(h.repo / ".tua") == skills_before
    for item in (good, bad):
        assert item.task_id == task.id
        assert item.task_status == "completed"
        assert item.source_skill_hash == task.source_skill_hash
        assert item.compiled_skill_hash == task.compiled_skill_hash
        assert item.cli_version == task.cli_version
        assert item.actual_models == "claude-fake-model-1"
    assert [f.rating for f in h.db.list_feedback(task.id)] == ["good", "bad"]


def test_feedback_validation(h: Harness) -> None:
    task = h.run()
    with pytest.raises(TuaError, match="--reason"):
        record_feedback(h.db, h.service, task.id, "bad", reason="  ")
    with pytest.raises(TuaError):
        record_feedback(h.db, h.service, task.id, "bad", reason="key " + "gh" + "p_" + "C" * 36)
    with pytest.raises(TuaError, match="no task"):
        record_feedback(h.db, h.service, "20990101-000000-0000", "good")
