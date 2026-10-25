"""Git repository inspection and a single, safe way to run Git commands.

TUA never runs ``git reset --hard``, ``git clean``, ``git stash``, ``git push``, or any
other command that could discard or publish the user's work.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tua.errors import DirtyRepositoryError, GitCommandError, RepositoryError
from tua.logging_utils import excerpt
from tua.process import child_env

GIT_TIMEOUT_SECONDS = 300
MIN_GIT_VERSION = (2, 25)


def run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
    new_session: bool = False,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd`` with a scrubbed environment and a timeout."""
    argv = ["git", *args]
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=child_env({"LC_ALL": "C", **(env or {})}),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            start_new_session=new_session,
        )
    except FileNotFoundError as exc:
        raise RepositoryError(
            "git is not installed or not on PATH", hint="Install Git 2.25 or newer."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(f"`git {' '.join(args)}` timed out after {timeout:.0f}s") from exc
    if check and result.returncode != 0:
        detail = excerpt(result.stderr.strip() or result.stdout.strip(), 1500) or "no output"
        raise GitCommandError(f"`git {' '.join(args)}` failed (exit {result.returncode}): {detail}")
    return result


def git_version() -> tuple[int, ...] | None:
    try:
        out = run_git(["--version"], cwd=Path.cwd()).stdout
    except RepositoryError:
        return None
    parts = out.strip().split()
    if len(parts) < 3:
        return None
    numbers = []
    for piece in parts[2].split("."):
        if not piece.isdigit():
            break
        numbers.append(int(piece))
    return tuple(numbers) or None


@dataclass(frozen=True)
class StatusEntry:
    code: str
    path: str

    def render(self) -> str:
        return f"{self.code} {self.path}"


def parse_porcelain_z(output: str) -> list[StatusEntry]:
    """Parse ``git status --porcelain=v1 -z`` output (rename entries carry two paths)."""
    entries: list[StatusEntry] = []
    items = output.split("\x00")
    i = 0
    while i < len(items):
        item = items[i]
        i += 1
        if not item:
            continue
        code, path = item[:2], item[3:]
        if code[0] in "RC":
            i += 1  # skip the original path of a rename/copy
        entries.append(StatusEntry(code=code, path=path))
    return entries


class Repository:
    """A Git working tree identified by its top-level directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def open(cls, path: Path, *, require_toplevel: bool = True) -> Repository:
        if not path.is_dir():
            raise RepositoryError(f"{path} is not a directory")
        result = run_git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
        if result.returncode != 0:
            raise RepositoryError(
                f"{path} is not inside a Git repository",
                hint="Run `git init` there first, or pick the repository root.",
            )
        toplevel = Path(result.stdout.strip()).resolve()
        if require_toplevel and toplevel != path.resolve():
            raise RepositoryError(
                f"{path} is inside the repository {toplevel} but is not its root",
                hint=f"Use {toplevel} instead.",
            )
        return cls(toplevel)

    def git(
        self, *args: str, check: bool = True, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return run_git(list(args), cwd=self.root, check=check, env=env)

    def _rev_parse_path(self, flag: str) -> Path:
        # Output may be relative to the working directory on older Git versions.
        out = self.git("rev-parse", flag).stdout.strip()
        return (self.root / out).resolve()

    def common_dir(self) -> Path:
        return self._rev_parse_path("--git-common-dir")

    def git_dir(self) -> Path:
        return self._rev_parse_path("--git-dir")

    def current_branch(self) -> str:
        result = self.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if result.returncode != 0:
            raise RepositoryError(
                f"{self.root} has a detached HEAD",
                hint="Check out the branch you want task results merged into, then retry.",
            )
        return result.stdout.strip()

    def head_commit(self) -> str:
        result = self.git("rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
        if result.returncode != 0:
            raise RepositoryError(
                f"{self.root} has no commits yet",
                hint="Create an initial commit; tasks start from a committed revision.",
            )
        return result.stdout.strip()

    def resolve_commit(self, rev: str) -> str:
        if not rev or rev.startswith("-"):
            raise RepositoryError(f"invalid revision {rev!r}")
        result = self.git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False)
        if result.returncode != 0:
            raise RepositoryError(f"revision {rev!r} does not name a commit in {self.root}")
        return result.stdout.strip()

    def branch_commit(self, branch: str) -> str | None:
        result = self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def status(self) -> list[StatusEntry]:
        out = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        return parse_porcelain_z(out)

    def ensure_clean(self, *, purpose: str) -> None:
        """Refuse to continue when tracked changes or untracked files exist."""
        entries = self.status()
        if not entries:
            return
        shown = "\n".join(f"  {e.render()}" for e in entries[:10])
        more = f"\n  … and {len(entries) - 10} more" if len(entries) > 10 else ""
        raise DirtyRepositoryError(
            f"{self.root} has uncommitted or untracked changes; refusing to {purpose}:\n"
            f"{shown}{more}",
            hint="Commit them, or stash them yourself with `git stash -u`, then retry. "
            "TUA never stashes, resets, or deletes your changes.",
        )

    def in_progress_operation(self) -> str | None:
        """Name of an unfinished merge/rebase/cherry-pick/revert/bisect, if any."""
        git_dir = self.git_dir()
        markers = {
            "MERGE_HEAD": "merge",
            "CHERRY_PICK_HEAD": "cherry-pick",
            "REVERT_HEAD": "revert",
            "rebase-merge": "rebase",
            "rebase-apply": "rebase or am",
            "BISECT_LOG": "bisect",
        }
        for marker, name in markers.items():
            if (git_dir / marker).exists():
                return name
        return None

    def unmerged_paths(self) -> list[str]:
        out = self.git("diff", "--name-only", "--diff-filter=U", "-z").stdout
        return [p for p in out.split("\x00") if p]
