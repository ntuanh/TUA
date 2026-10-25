"""Task worktrees: creation, snapshots, diffs, and validated removal.

A task's result is the full content of its worktree (tracked changes, new untracked
files, and any commits the provider made), excluding ignored files. TUA captures it as a
Git tree through a *temporary* index, so inspecting a task never modifies the worktree,
its index, or its branch.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from tua.errors import GitCommandError, WorktreeError
from tua.paths import TuaPaths, ensure_inside, ensure_private_dir
from tua.repository import Repository, run_git

TASK_BRANCH_PREFIX = "tua/"
_TASK_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4,12}$")
GITLINK_MODE = "160000"


def task_branch_name(task_id: str) -> str:
    return f"{TASK_BRANCH_PREFIX}{task_id}"


def validate_task_id(task_id: str) -> str:
    if not _TASK_ID_RE.match(task_id):
        raise WorktreeError(f"invalid task id {task_id!r}")
    return task_id


def create_worktree(repo: Repository, *, task_id: str, path: Path, base_commit: str) -> None:
    """Create branch ``tua/<task_id>`` at ``base_commit`` checked out at ``path``."""
    branch = task_branch_name(validate_task_id(task_id))
    if path.exists():
        raise WorktreeError(f"worktree path {path} already exists; refusing to reuse it")
    if repo.branch_commit(branch) is not None:
        raise WorktreeError(f"branch {branch} already exists; refusing to reuse it")
    ensure_private_dir(path.parent)
    try:
        repo.git("worktree", "add", "-b", branch, str(path), base_commit)
    except GitCommandError as exc:
        raise WorktreeError(f"could not create worktree for task {task_id}: {exc.message}") from exc
    worktree = Repository(path)
    head = worktree.head_commit()
    if head != base_commit:
        raise WorktreeError(f"worktree HEAD {head} does not match base commit {base_commit}")


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    head: str | None
    branch: str | None
    prunable: bool


def list_worktrees(repo: Repository) -> list[WorktreeInfo]:
    out = repo.git("worktree", "list", "--porcelain").stdout
    infos: list[WorktreeInfo] = []
    for block in out.strip().split("\n\n"):
        attrs: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            attrs[key] = value
        if "worktree" not in attrs:
            continue
        branch = attrs.get("branch")
        infos.append(
            WorktreeInfo(
                path=Path(attrs["worktree"]),
                head=attrs.get("HEAD"),
                branch=branch.removeprefix("refs/heads/") if branch else None,
                prunable="prunable" in attrs,
            )
        )
    return infos


def find_worktree(repo: Repository, path: Path) -> WorktreeInfo | None:
    target = path.resolve(strict=False)
    for info in list_worktrees(repo):
        if info.path.resolve(strict=False) == target:
            return info
    return None


def check_task_worktree(repo: Repository, path: Path) -> Repository:
    """Ensure ``path`` is a live worktree of ``repo`` (same object store)."""
    if not path.is_dir():
        raise WorktreeError(f"task worktree {path} does not exist")
    worktree = Repository.open(path)
    if worktree.common_dir() != repo.common_dir():
        raise WorktreeError(f"{path} is not a worktree of {repo.root}")
    return worktree


def snapshot_tree(worktree: Path, tmp_dir: Path) -> str:
    """Write the worktree's current content (excluding ignored files) as a tree object."""
    ensure_private_dir(tmp_dir)
    index = tmp_dir / f"index-{uuid.uuid4().hex}"
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        run_git(["read-tree", "HEAD"], cwd=worktree, env=env)
        run_git(["add", "--all", "--", "."], cwd=worktree, env=env)
        return run_git(["write-tree"], cwd=worktree, env=env).stdout.strip()
    finally:
        with contextlib.suppress(FileNotFoundError):
            index.unlink()


def diff(repo_root: Path, base: str, tree: str, *, mode: str = "patch") -> str:
    """Render a diff between two tree-ish objects without external diff drivers."""
    flags = {
        "patch": ["--patch", "--stat"],
        "stat": ["--stat"],
        "names": ["--name-status"],
    }[mode]
    args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", *flags, base, tree]
    return run_git(args, cwd=repo_root).stdout


def changed_entries(repo_root: Path, base: str, tree: str) -> list[tuple[str, str, str]]:
    """(status, new_mode, path) for each change between ``base`` and ``tree``."""
    out = run_git(["diff", "--raw", "-z", "--no-renames", base, tree], cwd=repo_root).stdout
    fields = out.split("\x00")
    entries: list[tuple[str, str, str]] = []
    i = 0
    while i + 1 < len(fields):
        meta, path = fields[i], fields[i + 1]
        i += 2
        if not meta.startswith(":"):
            continue
        parts = meta[1:].split()
        entries.append((parts[4], parts[1], path))
    return entries


def remove_task_worktree(repo: Repository, paths: TuaPaths, path: Path) -> None:
    """Remove a TUA-owned worktree. Only paths under TUA's worktree root are accepted."""
    ensure_inside(path, paths.worktrees_dir, what="worktree")
    info = find_worktree(repo, path)
    if not path.exists():
        if info is not None:
            # Directory already gone: drop Git's stale metadata for this worktree only.
            repo.git("worktree", "remove", str(path))
        return
    if info is None:
        raise WorktreeError(
            f"{path} exists but is not a registered worktree of {repo.root}; leaving it alone"
        )
    try:
        # --force is required because task changes are uncommitted; the caller has
        # already merged them or the user confirmed discarding them.
        repo.git("worktree", "remove", "--force", str(path))
    except GitCommandError as exc:
        raise WorktreeError(f"could not remove worktree {path}: {exc.message}") from exc


def delete_task_branch(repo: Repository, task_id: str) -> None:
    branch = task_branch_name(validate_task_id(task_id))
    if repo.branch_commit(branch) is None:
        return
    try:
        repo.git("branch", "-D", branch)
    except GitCommandError as exc:
        raise WorktreeError(f"could not delete branch {branch}: {exc.message}") from exc
