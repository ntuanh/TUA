"""``tua review``: a read-only task in which one model reviews another task's changes.

The review runs as an ordinary TUA task, so it is recorded with its own ID, fingerprints,
model metadata, and event log, and is linked to the reviewed task through ``review_of``.
It always uses the ``read-only`` profile: the reviewer can read the code but cannot edit
files or run commands. Its worktree is a snapshot of the reviewed task's result, and the
diff is included in the prompt. A model review is an extra opinion, not a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tua.errors import TuaError
from tua.models import ACTIVE_STATES, Task, TaskStatus
from tua.paths import TuaPaths
from tua.repository import Repository, run_git
from tua.review import ReviewService, task_diff
from tua.tasks import RunOptions, TaskService
from tua.worktrees import changed_entries, check_task_worktree, diff, snapshot_tree

DIFF_LIMIT = 60_000
PROMPT_LIMIT = 10_000

REVIEW_PROMPT = """\
You are reviewing changes that another AI coding agent made for TUA task {task_id}.
The current working directory contains the code AFTER those changes. You cannot edit
files or run commands; read files when you need more context.

The original task was:
<<<TASK
{task_prompt}
TASK>>>

The changes, as a diff against the base commit {base}:
```diff
{diff}
```
{truncated}
Review the changes for:
1. Correctness bugs and behaviour that contradicts the original task.
2. Missing or weak tests, and edge cases the change does not handle.
3. Risky or unnecessary changes.

For each finding give the file and line, a severity (high, medium, low), and a concrete
explanation. Label each finding CONFIRMED if you verified it by reading the code, or
SUSPECTED otherwise. Do not invent problems: if the change looks correct, say so plainly.
{focus}"""


@dataclass(frozen=True)
class ReviewOutcome:
    task: Task
    # True when the review task completed; its status may later read 'discarded' because
    # the read-only worktree copy is cleaned up automatically.
    completed: bool
    text: str | None
    diff_truncated: bool
    # Files the reviewer changed in its read-only worktree copy; the copy is then kept.
    changed_files: tuple[str, ...] = ()
    # Why the worktree copy could not be checked or removed; it is then kept.
    worktree_error: str | None = None


def worktree_notice(outcome: ReviewOutcome, *, kept_on_request: bool = False) -> str | None:
    """Why a review's worktree copy still exists and how to remove it safely."""
    review = outcome.task
    worktree = Path(review.worktree_path)
    where = f"\n  worktree: {worktree}" if worktree.is_dir() else ""
    if outcome.changed_files:
        shown = ", ".join(outcome.changed_files[:10])
        if len(outcome.changed_files) > 10:
            shown += f" (and {len(outcome.changed_files) - 10} more)"
        return (
            f"\nWarning: the read-only review changed files in its worktree: {shown}{where}\n"
            "A review should not change files; check that the provider respected TUA's "
            "permissions. Nothing was merged or deleted.\n"
            f"Inspect the changes with `tua diff {review.id}`, then remove them with "
            f"`tua discard {review.id}`."
        )
    if outcome.worktree_error:
        # With --keep-worktree only the change check ran; otherwise removal may have failed.
        action = (
            "check the review's worktree copy for changes"
            if kept_on_request
            else "remove the review's worktree copy"
        )
        return (
            f"\nWarning: could not {action}: {outcome.worktree_error}"
            f"{where}\nNothing was deleted. Remove it with `tua discard {review.id}` once "
            "the problem is fixed."
        )
    if not outcome.completed:
        if not worktree.is_dir():
            return None  # it failed before its worktree was created
        return (
            f"\nThe failed review's worktree copy was kept for inspection:{where}\n"
            f"Remove it with `tua discard {review.id}` when you are done."
        )
    if kept_on_request:
        return (
            f"\nThe review's worktree copy was kept:{where}\n"
            f"Remove it with `tua discard {review.id}` when you are done."
        )
    return None


def result_text(paths: TuaPaths, task: Task) -> str | None:
    path = paths.task_dir(task.id) / "result.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return task.result_excerpt


def _snapshot(task: Task, repo: Repository, paths: TuaPaths) -> str:
    """A commit holding the reviewed task's result, on top of its base commit."""
    if task.status is TaskStatus.MERGED and task.merged_commit:
        return task.merged_commit
    worktree = Path(task.worktree_path)
    if not worktree.is_dir():
        raise TuaError(f"task {task.id} has no worktree any more; there is nothing to review")
    check_task_worktree(repo, worktree)
    tree = snapshot_tree(worktree, paths.tmp_dir)
    base_tree = run_git(["rev-parse", f"{task.base_commit}^{{tree}}"], cwd=repo.root).stdout
    if tree == base_tree.strip():
        raise TuaError(f"task {task.id} made no changes; there is nothing to review")
    return run_git(
        ["commit-tree", tree, "-p", task.base_commit, "-F", "-"],
        cwd=repo.root,
        input_text=f"TUA review snapshot of task {task.id}\n",
    ).stdout.strip()


def run_review(
    tasks: TaskService,
    review_service: ReviewService,
    paths: TuaPaths,
    task_id: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    focus: str | None = None,
    timeout_seconds: int | None = None,
    keep_worktree: bool = False,
) -> ReviewOutcome:
    target = tasks.get(task_id)
    if target.review_of:
        raise TuaError(f"task {target.id} is itself a review of {target.review_of}")
    if target.status in ACTIVE_STATES:
        raise TuaError(f"task {target.id} is still {target.status.value}; review it once it ends")
    if target.status is TaskStatus.DISCARDED:
        raise TuaError(f"task {target.id} was discarded; there is nothing to review")

    repo = Repository.open(Path(target.source_repo))
    snapshot = _snapshot(target, repo, paths)
    changes = diff(repo.root, target.base_commit, snapshot, mode="patch")
    truncated = len(changes) > DIFF_LIMIT
    task_prompt = target.prompt
    if len(task_prompt) > PROMPT_LIMIT:
        task_prompt = task_prompt[:PROMPT_LIMIT] + "\n[… prompt truncated]"
    prompt = REVIEW_PROMPT.format(
        task_id=target.id,
        task_prompt=task_prompt,
        base=target.base_commit[:12],
        diff=changes[:DIFF_LIMIT],
        truncated=(
            f"(The diff was truncated to {DIFF_LIMIT} characters; read the files for the rest.)\n"
            if truncated
            else ""
        ),
        focus=f"\nPay particular attention to: {focus.strip()}\n"
        if focus and focus.strip()
        else "",
    )
    project = tasks.db.get_project_by_id(target.project_id)
    review = tasks.run(
        project.name,
        RunOptions(
            prompt=prompt,
            model=model,
            provider=provider,
            effort=effort,
            timeout_seconds=timeout_seconds,
            base_commit=snapshot,
            require_clean=False,
            permission_profile="read-only",
            review_of=target.id,
        ),
    )
    completed = review.status is TaskStatus.COMPLETED
    text = result_text(paths, review) if completed else None
    changed: tuple[str, ...] = ()
    worktree_error: str | None = None
    if completed:
        # A read-only review leaves nothing to merge; remove its worktree copy, but only
        # when it really contains no changes. Changes mean the provider wrote despite the
        # read-only profile: they are kept for the user to inspect.
        try:
            review_diff = task_diff(review, paths)
            if review_diff.tree:  # exact paths; a rename lists both its old and new path
                entries = changed_entries(repo.root, review_diff.base, review_diff.tree)
                changed = tuple(path for _status, _mode, path in entries)
            if not changed and not keep_worktree:
                review = review_service.discard(review.id, assume_yes=True, reason="review cleanup")
        except TuaError as exc:
            worktree_error = exc.message
            review = tasks.get(review.id)
    return ReviewOutcome(
        task=review,
        completed=completed,
        text=text,
        diff_truncated=truncated,
        changed_files=changed,
        worktree_error=worktree_error,
    )
