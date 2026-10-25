"""Review operations on finished tasks: diff, merge, and discard.

Merge strategy (one strategy, documented in docs/task-lifecycle.md): TUA snapshots the
task worktree into a tree object, creates a single squash commit whose parent is the
recorded base commit (``git commit-tree``, using the repository's configured identity),
and fast-forwards the target branch to it with
``git merge --ff-only --no-overwrite-ignore``. The merge is refused unless the target
checkout is on the recorded branch, clean, and still at the recorded base commit, so it
can never produce conflicts in, or overwrite files of, the user's checkout. Nothing is
ever pushed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tua.db import Database
from tua.errors import AbortedError, MergeRefusedError, RepositoryError, TuaError
from tua.logging_utils import log_event
from tua.models import ACTIVE_STATES, FORCE_MERGE_STATES, Task, TaskStatus
from tua.paths import TuaPaths
from tua.process import deferred_interrupts
from tua.repository import Repository, run_git
from tua.tasks import TaskService
from tua.worktrees import (
    GITLINK_MODE,
    changed_entries,
    check_task_worktree,
    delete_task_branch,
    diff,
    remove_task_worktree,
    snapshot_tree,
)


class Confirmer(Protocol):
    def confirm(self, prompt: str) -> bool: ...

    def confirm_typed(self, prompt: str, expected: str) -> bool: ...


@dataclass(frozen=True)
class TaskDiff:
    source: str  # "worktree", "merged-commit", or "unavailable"
    base: str
    tree: str | None
    stat: str
    patch: str
    names: str

    @property
    def has_changes(self) -> bool:
        return bool(self.names.strip())


def _tree_of(repo_root: Path, rev: str) -> str:
    return run_git(["rev-parse", f"{rev}^{{tree}}"], cwd=repo_root).stdout.strip()


def task_diff(task: Task, paths: TuaPaths) -> TaskDiff:
    """Diff a task's result against its base commit, from the worktree when it exists."""
    repo_root = Path(task.source_repo)
    if task.status is TaskStatus.MERGED and task.merged_commit:
        rev = task.merged_commit
        return TaskDiff(
            source="merged-commit",
            base=task.base_commit,
            tree=_tree_of(repo_root, rev),
            stat=diff(repo_root, task.base_commit, rev, mode="stat"),
            patch=diff(repo_root, task.base_commit, rev, mode="patch"),
            names=diff(repo_root, task.base_commit, rev, mode="names"),
        )
    worktree = Path(task.worktree_path)
    if not worktree.is_dir():
        return TaskDiff("unavailable", task.base_commit, None, "", "", "")
    repo = Repository.open(repo_root)
    check_task_worktree(repo, worktree)
    tree = snapshot_tree(worktree, paths.tmp_dir)
    return TaskDiff(
        source="worktree",
        base=task.base_commit,
        tree=tree,
        stat=diff(repo.root, task.base_commit, tree, mode="stat"),
        patch=diff(repo.root, task.base_commit, tree, mode="patch"),
        names=diff(repo.root, task.base_commit, tree, mode="names"),
    )


def _commit_message(task: Task, message: str | None) -> str:
    if message and message.strip():
        text = message.strip()
    else:
        first_line = task.prompt.strip().splitlines()[0]
        subject = f"tua: {first_line}"
        text = subject if len(subject) <= 72 else subject[:71] + "…"
        text += (
            f"\n\nSquash-merged from TUA task {task.id} "
            f"(base {task.base_commit[:12]}, task status {task.status.value})."
        )
    return f"{text}\n\nTUA-Task-ID: {task.id}\n"


class ReviewService:
    def __init__(
        self,
        db: Database,
        paths: TuaPaths,
        tasks: TaskService,
        confirmer: Confirmer,
        out: Callable[[str], None],
    ) -> None:
        self.db = db
        self.paths = paths
        self.tasks = tasks
        self.confirmer = confirmer
        self.out = out

    # ----------------------------------------------------------------------- merge

    def merge(
        self,
        task_id: str,
        *,
        force: bool = False,
        message: str | None = None,
        assume_yes: bool = False,
    ) -> Task:
        """Squash-merge a task after review and confirmation. Never pushes."""
        try:
            return self._merge(task_id, force=force, message=message, assume_yes=assume_yes)
        except (MergeRefusedError, RepositoryError) as exc:
            # An unknown task id has nothing to record the refusal against.
            with contextlib.suppress(TuaError):
                self.db.add_event(task_id, "merge_refused", {"reason": exc.message[:500]})
            raise

    def _merge(
        self,
        task_id: str,
        *,
        force: bool,
        message: str | None,
        assume_yes: bool,
    ) -> Task:
        task = self.tasks.get(task_id)
        if task.status is TaskStatus.MERGED:
            raise MergeRefusedError(f"task {task.id} is already merged as {task.merged_commit}")
        if task.status is TaskStatus.DISCARDED:
            raise MergeRefusedError(f"task {task.id} was discarded")
        if task.status in ACTIVE_STATES:
            raise MergeRefusedError(f"task {task.id} is still {task.status.value}")
        forced = task.status in FORCE_MERGE_STATES
        if forced and not force:
            raise MergeRefusedError(
                f"task {task.id} is '{task.status.value}', not 'completed'; merging it "
                "requires --force",
                hint=f"Inspect it with `tua diff {task.id}`. If you still want it, run "
                f"`tua merge {task.id} --force` (asks for a second confirmation).",
            )

        repo = self._target(task)
        worktree = Path(task.worktree_path)
        self._check_worktree(repo, worktree)
        tree = snapshot_tree(worktree, self.paths.tmp_dir)
        if tree == _tree_of(repo.root, task.base_commit):
            raise MergeRefusedError(
                f"task {task.id} made no changes; there is nothing to merge",
                hint=f"Remove it with `tua discard {task.id}`.",
            )
        gitlinks = [
            path
            for _status, mode, path in changed_entries(repo.root, task.base_commit, tree)
            if mode == GITLINK_MODE
        ]
        if gitlinks:
            raise MergeRefusedError(
                "the task adds or changes embedded Git repositories/submodules: "
                + ", ".join(gitlinks),
                hint="Review and merge these changes manually from the task worktree.",
            )

        if forced:
            self.out(
                "\n" + "!" * 72 + f"\nWARNING: task {task.id} ended as '{task.status.value}'"
                f" ({task.error_type or 'unknown error'}).\nIts changes may be incomplete or "
                "broken. Review the full diff below carefully.\n" + "!" * 72
            )
        self.out(
            f"Task {task.id}: merge into '{task.target_branch}' of {repo.root} "
            f"as one commit on top of {task.base_commit[:12]}.\n"
        )
        self.out(diff(repo.root, task.base_commit, tree, mode="patch"))

        if not (assume_yes and not forced) and not self.confirmer.confirm(
            f"Merge task {task.id} into '{task.target_branch}'?"
        ):
            raise AbortedError("merge cancelled; nothing was changed")
        if forced and not self.confirmer.confirm_typed(
            f"Force-merging a '{task.status.value}' task. Type the task ID to confirm:", task.id
        ):
            raise AbortedError("force-merge not confirmed; nothing was changed")

        # Re-check everything: the user may have changed things while reviewing.
        self._check_target_state(repo, task)
        if snapshot_tree(worktree, self.paths.tmp_dir) != tree:
            raise MergeRefusedError(
                "the task worktree changed during review; nothing was merged",
                hint=f"Run `tua merge {task.id}` again to review the current content.",
            )

        commit = run_git(
            ["commit-tree", tree, "-p", task.base_commit, "-F", "-"],
            cwd=repo.root,
            input_text=_commit_message(task, message),
        ).stdout.strip()
        self.db.add_event(task.id, "merge_started", {"commit": commit, "forced": forced})
        with deferred_interrupts():
            result = run_git(
                ["merge", "--ff-only", "--no-overwrite-ignore", commit],
                cwd=repo.root,
                check=False,
                new_session=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                self.db.add_event(
                    task.id, "merge_failed", {"commit": commit, "detail": detail[:500]}
                )
                raise MergeRefusedError(
                    f"git refused to fast-forward '{task.target_branch}'; nothing was changed:\n"
                    f"{detail}",
                    hint="Typical cause: an untracked or ignored file in your checkout would "
                    "be overwritten. Move it away and retry.",
                )
            if repo.head_commit() != commit:
                raise RepositoryError(
                    f"after merging, HEAD is not {commit}; inspect {repo.root} manually"
                )
            task = self.db.transition(
                task.id,
                TaskStatus.MERGED,
                fields={"merged_commit": commit},
                metadata={"commit": commit, "forced": forced},
                force=forced,
            )
        log_event("task.merged", task_id=task.id, commit=commit, forced=forced)
        self.out(
            f"Merged task {task.id} into '{task.target_branch}' as {commit[:12]} (not pushed)."
        )
        return self._cleanup(task, repo, expected_tree=tree)

    def _target(self, task: Task) -> Repository:
        root = Path(task.source_repo)
        try:
            repo = Repository.open(root)
        except RepositoryError as exc:
            raise MergeRefusedError(f"source repository is unavailable: {exc.message}") from exc
        if repo.root != root.resolve():
            raise MergeRefusedError(f"{root} no longer is the recorded repository root")
        repo.resolve_commit(task.base_commit)
        self._check_target_state(repo, task)
        return repo

    @staticmethod
    def _check_target_state(repo: Repository, task: Task) -> None:
        operation = repo.in_progress_operation()
        if operation:
            raise MergeRefusedError(f"{repo.root} has a {operation} in progress; finish it first")
        branch = repo.current_branch()
        if branch != task.target_branch:
            raise MergeRefusedError(
                f"{repo.root} is on '{branch}', but task {task.id} targets '{task.target_branch}'",
                hint=f"Run `git switch {task.target_branch}` yourself, then retry.",
            )
        repo.ensure_clean(purpose="merge into it")
        head = repo.head_commit()
        if head != task.base_commit:
            raise MergeRefusedError(
                f"'{task.target_branch}' has moved from the task's base {task.base_commit[:12]} "
                f"to {head[:12]}; TUA only merges onto an unchanged base",
                hint=f"The task branch {task.task_branch} is kept. Merge or rebase it yourself "
                f"with Git, or discard it with `tua discard {task.id}` and run the task again.",
            )

    @staticmethod
    def _check_worktree(repo: Repository, worktree: Path) -> None:
        if not worktree.is_dir():
            raise MergeRefusedError(f"the task worktree {worktree} no longer exists")
        wt = check_task_worktree(repo, worktree)
        operation = wt.in_progress_operation()
        if operation:
            raise MergeRefusedError(f"the task worktree has a {operation} in progress")
        unmerged = wt.unmerged_paths()
        if unmerged:
            raise MergeRefusedError(
                "the task worktree has unresolved conflicts: " + ", ".join(unmerged[:10]),
                hint="Resolve them in the worktree yourself, or discard the task.",
            )

    # --------------------------------------------------------------------- discard

    def discard(self, task_id: str, *, assume_yes: bool = False, reason: str | None = None) -> Task:
        task = self.tasks.get(task_id)
        if task.status is TaskStatus.DISCARDED:
            raise TuaError(f"task {task.id} is already discarded")
        if task.status in ACTIVE_STATES:
            raise TuaError(
                f"task {task.id} is still {task.status.value}",
                hint="Cancel it with Ctrl+C in the terminal running it, then discard it.",
            )
        repo = self._repo_for_cleanup(task)
        worktree = Path(task.worktree_path)

        if task.status is TaskStatus.MERGED:
            if task.cleanup_status == "done":
                raise TuaError(f"task {task.id} is merged and already cleaned up")
            if not assume_yes and not self.confirmer.confirm(
                f"Remove the leftover worktree and branch of merged task {task.id}?"
            ):
                raise AbortedError("nothing was removed")
            return self._cleanup(task, repo, expected_tree=None)

        automated = assume_yes and reason is not None  # eval/review cleanup: no preview
        if not automated:
            self.out(f"Discarding task {task.id} ({task.status.value}) removes:")
            self.out(f"  worktree {worktree}" + ("" if worktree.exists() else " (already gone)"))
            self.out(f"  branch   {task.task_branch}")
        if worktree.is_dir() and not automated:
            try:
                stat = task_diff(task, self.paths).stat or "  (none)\n"
            except TuaError as exc:
                stat = f"  (could not compute the diff: {exc.message})\n"
            self.out("Unmerged changes that will be lost:\n" + stat)
        if not assume_yes and not self.confirmer.confirm(f"Discard task {task.id}?"):
            raise AbortedError("discard cancelled; nothing was removed")
        try:
            remove_task_worktree(repo, self.paths, worktree)
            delete_task_branch(repo, task.id)
        except TuaError as exc:
            self.db.add_event(task.id, "discard_failed", {"error": exc.message})
            raise TuaError(
                f"could not discard task {task.id}: {exc.message}",
                hint="The task keeps its current state; fix the problem and retry.",
            ) from exc
        task = self.db.transition(
            task.id,
            TaskStatus.DISCARDED,
            fields={"cleanup_status": "done", "cleanup_error": None},
            metadata={"reason": reason} if reason else None,
        )
        log_event("task.discarded", task_id=task.id)
        return task

    def _repo_for_cleanup(self, task: Task) -> Repository:
        try:
            return Repository.open(Path(task.source_repo))
        except RepositoryError as exc:
            raise TuaError(
                f"cannot clean up task {task.id}: {exc.message}",
                hint=f"If the repository moved, remove {task.worktree_path} yourself.",
            ) from exc

    def _cleanup(self, task: Task, repo: Repository, *, expected_tree: str | None) -> Task:
        """Remove a merged task's worktree and branch; record (never raise) failures."""
        worktree = Path(task.worktree_path)
        try:
            if (
                expected_tree is not None
                and worktree.is_dir()
                and snapshot_tree(worktree, self.paths.tmp_dir) != expected_tree
            ):
                raise TuaError("the worktree changed after the merge; keeping it")
            remove_task_worktree(repo, self.paths, worktree)
            delete_task_branch(repo, task.id)
        except TuaError as exc:
            self.db.update_task(task.id, {"cleanup_status": "failed", "cleanup_error": exc.message})
            self.db.add_event(task.id, "cleanup_failed", {"error": exc.message})
            self.out(
                f"warning: cleanup failed: {exc.message}\n"
                f"The merge itself succeeded. Retry with `tua discard {task.id}`, or run:\n"
                f"  git -C {repo.root} worktree remove --force {worktree}\n"
                f"  git -C {repo.root} branch -D {task.task_branch}"
            )
            return self.db.get_task(task.id)
        task = self.db.update_task(task.id, {"cleanup_status": "done", "cleanup_error": None})
        self.db.add_event(task.id, "cleanup_done")
        return task
