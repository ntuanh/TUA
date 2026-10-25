"""Let ``tua`` start from any folder, always with the user's agreement.

* find Git projects inside a folder that is not one itself;
* turn a plain folder into a Git project, after computing exactly what would be committed
  (large files, nested projects, and secret-like files are left out);
* hide untracked leftovers (such as build output) from Git on this machine only.

Nothing here runs without an explicit yes from the user; see ``tua.interactive``.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from tua.errors import TuaError
from tua.repository import Repository, run_git

LARGE_FILE_BYTES = 50 * 1024 * 1024
MAX_INITIAL_BYTES = 300 * 1024 * 1024
MAX_INITIAL_FILES = 20_000
SKIP_SCAN_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".cache"}
)
BUILD_DIR_NAMES = frozenset(
    {"build", "out", "dist", "bin", "obj", "target", "__pycache__", ".pytest_cache"}
)
# Generated files: left out quietly.
GENERATED_IGNORE = (
    "# Added by TUA when this folder became a Git project.",
    "__pycache__/",
    "*.py[cod]",
    ".venv/",
    "venv/",
    "node_modules/",
    ".DS_Store",
    "*.o",
    "*.obj",
)
# Secret-like files: always left out, and listed so the user sees it.
SECRET_IGNORE = (
    ".env",
    ".env.*",
    "!.env.example",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".netrc",
    ".git-credentials",
)
SECRET_NAME_RE = re.compile(
    r"(^\.env($|\.)|\.pem$|\.key$|^id_(rsa|ed25519|ecdsa)|^\.netrc$|^\.git-credentials$|"
    r"credential|secret)",
    re.IGNORECASE,
)


def looks_secret(path: str) -> bool:
    name = PurePosixPath(path).name
    return bool(SECRET_NAME_RE.search(name)) and name != ".env.example"


def escape_pattern(path: str) -> str:
    """Escape a literal path for use in a gitignore file, anchored at the root."""
    escaped = re.sub(r"([*?\[\\])", r"\\\1", path)
    if escaped.startswith(("#", "!")):
        escaped = "\\" + escaped
    return "/" + escaped


def find_child_repos(folder: Path, *, max_depth: int = 2, limit: int = 20) -> list[Path]:
    """Git projects inside ``folder`` (not descending into them), nearest first."""
    found: list[Path] = []
    frontier = [(folder, 0)]
    while frontier and len(found) < limit:
        current, depth = frontier.pop(0)
        try:
            children = sorted(p for p in current.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            continue
        for child in children:
            if child.name in SKIP_SCAN_DIRS:
                continue
            if (child / ".git").exists():
                found.append(child)
            elif depth + 1 < max_depth:
                frontier.append((child, depth + 1))
    return found[:limit]


def unsafe_location(folder: Path) -> str | None:
    """Why ``folder`` must never become a Git project automatically, if it must not."""
    resolved = folder.resolve()
    if resolved == Path.home().resolve():
        return "it is your home folder"
    if len(resolved.parts) <= 2:
        return "it is a top-level system folder"
    return None


@dataclass
class RepoPlan:
    folder: Path
    files: list[str] = field(default_factory=list)
    total_bytes: int = 0
    large_files: list[str] = field(default_factory=list)
    secret_files: list[str] = field(default_factory=list)
    nested_repos: list[str] = field(default_factory=list)
    ignore_lines: list[str] = field(default_factory=list)

    @property
    def too_big(self) -> bool:
        return self.total_bytes > MAX_INITIAL_BYTES or len(self.files) > MAX_INITIAL_FILES


def plan_new_repo(folder: Path) -> RepoPlan:
    """Compute exactly which files a new repository would commit, without changing anything."""
    plan = RepoPlan(folder=folder)
    plan.nested_repos = [
        p.relative_to(folder).as_posix() for p in find_child_repos(folder, max_depth=6)
    ]
    planned = [*GENERATED_IGNORE, *(escape_pattern(n) + "/" for n in plan.nested_repos)]
    with tempfile.TemporaryDirectory(prefix="tua-plan-") as tmp:
        git_dir = Path(tmp) / "plan.git"
        exclude = Path(tmp) / "exclude"
        exclude.write_text("\n".join(planned) + "\n")
        run_git(["init", "-q", "--bare", str(git_dir)], cwd=Path(tmp))
        out = run_git(
            [
                f"--git-dir={git_dir}",
                f"--work-tree={folder}",
                "ls-files",
                "-z",
                "--others",
                "--exclude-standard",
                f"--exclude-from={exclude}",
            ],
            cwd=folder,
        ).stdout
    for rel in sorted(p for p in out.split("\x00") if p):
        if rel.endswith("/"):  # an embedded repository Git would add as a link
            plan.nested_repos.append(rel.rstrip("/"))
            planned.append(escape_pattern(rel.rstrip("/")) + "/")
            continue
        try:
            size = os.lstat(folder / rel).st_size
        except OSError:
            continue
        if looks_secret(rel):
            plan.secret_files.append(rel)
            planned.append(escape_pattern(rel))
        elif size > LARGE_FILE_BYTES:
            plan.large_files.append(rel)
            planned.append(escape_pattern(rel))
        else:
            plan.files.append(rel)
            plan.total_bytes += size
    plan.ignore_lines = [*planned, *SECRET_IGNORE]
    return plan


def require_identity(folder: Path) -> None:
    for key in ("user.name", "user.email"):
        if not run_git(["config", "--get", key], cwd=folder, check=False).stdout.strip():
            raise TuaError(
                f"Git does not know who you are ({key} is not set), so it cannot commit",
                hint='Run: git config --global user.name "Your Name" && '
                'git config --global user.email "you@example.com"',
            )


def create_repo(plan: RepoPlan) -> Repository:
    """Run ``git init``, write the planned .gitignore lines, and make the first commit."""
    folder = plan.folder
    require_identity(folder)
    run_git(["init", "-q"], cwd=folder)
    gitignore = folder / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    new_lines = [line for line in plan.ignore_lines if line not in existing]
    if new_lines:
        prefix = "\n" if existing and existing[-1] != "" else ""
        with gitignore.open("a") as handle:
            handle.write(prefix + "\n".join(new_lines) + "\n")
    run_git(["add", "-A"], cwd=folder)
    run_git(["commit", "-q", "-m", "Initial commit"], cwd=folder)
    repo = Repository.open(folder)
    leftover = repo.status()
    if leftover:
        raise TuaError(
            "the new Git project still has uncommitted files: "
            + ", ".join(e.path for e in leftover[:5])
        )
    return repo


def ignore_patterns_for(paths: list[str], tracked: frozenset[str] = frozenset()) -> list[str]:
    """Patterns that hide ``paths``: a whole build-like folder when nothing in it is tracked
    (so hiding it cannot affect committed files), otherwise each file exactly."""
    patterns: list[str] = []
    for path in paths:
        parts = PurePosixPath(path).parts
        pattern = escape_pattern(path)
        for i, part in enumerate(parts[:-1]):
            if part in BUILD_DIR_NAMES:
                folder = "/".join(parts[: i + 1]) + "/"
                if not any(t.startswith(folder) for t in tracked):
                    pattern = escape_pattern(folder.rstrip("/")) + "/"
                break
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns


def add_local_excludes(repo: Repository, patterns: list[str]) -> list[str]:
    """Append ``patterns`` to .git/info/exclude (this machine only); return those added."""
    exclude = repo.common_dir() / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    current = set(existing.splitlines())
    added = [p for p in patterns if p not in current]
    if added:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a") as handle:
            handle.write(prefix + "\n".join(added) + "\n")
    return added
