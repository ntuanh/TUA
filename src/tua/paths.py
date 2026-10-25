"""XDG-compatible local storage locations, private file helpers, and safe path resolution."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tua.errors import UnsafePathError

APP_NAME = "tua"
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _xdg_base(env_var: str, fallback: Path) -> Path:
    # The XDG spec says relative paths in these variables are invalid and must be ignored.
    value = os.environ.get(env_var, "")
    if value and os.path.isabs(value):
        return Path(value)
    return fallback


@dataclass(frozen=True)
class TuaPaths:
    """Directories TUA owns. All of them live outside any source repository."""

    config_dir: Path
    state_dir: Path
    cache_dir: Path

    @classmethod
    def from_env(cls) -> TuaPaths:
        home = Path.home()
        return cls(
            config_dir=_xdg_base("XDG_CONFIG_HOME", home / ".config") / APP_NAME,
            state_dir=_xdg_base("XDG_STATE_HOME", home / ".local" / "state") / APP_NAME,
            cache_dir=_xdg_base("XDG_CACHE_HOME", home / ".cache") / APP_NAME,
        )

    @property
    def db_path(self) -> Path:
        return self.state_dir / "tua.db"

    @property
    def tasks_dir(self) -> Path:
        return self.state_dir / "tasks"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    @property
    def compiled_dir(self) -> Path:
        return self.state_dir / "compiled"

    @property
    def evals_dir(self) -> Path:
        return self.state_dir / "evals"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.cache_dir / "tmp"

    @property
    def global_config_path(self) -> Path:
        return self.config_dir / "config.yaml"

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id

    def worktree_path(self, task_id: str) -> Path:
        return self.worktrees_dir / task_id


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and restrict the leaf directory to the current user."""
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    current = stat.S_IMODE(path.stat().st_mode)
    if current & 0o077:
        path.chmod(PRIVATE_DIR_MODE)
    return path


def write_private_file(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` with mode 0600."""
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), PRIVATE_FILE_MODE)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def create_private_file(path: Path) -> None:
    """Create an empty file with mode 0600 if it does not exist yet."""
    ensure_private_dir(path.parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
    except FileExistsError:
        return
    os.close(fd)


def is_private(path: Path) -> bool:
    """Return True when group and others have no access to ``path``."""
    return not stat.S_IMODE(path.stat().st_mode) & 0o077


def resolve_within(root: Path, relative: str, *, what: str) -> Path:
    """Resolve a configured relative path and ensure it stays inside ``root``.

    Absolute paths, NUL bytes, empty values, ``..`` traversal, and symlinks that point
    outside ``root`` are all rejected.
    """
    if not isinstance(relative, str) or not relative.strip():
        raise UnsafePathError(f"{what} must be a non-empty relative path")
    if "\x00" in relative:
        raise UnsafePathError(f"{what} contains a NUL byte")
    if os.path.isabs(relative) or relative.startswith("~"):
        raise UnsafePathError(f"{what} must be relative to {root}, got absolute path {relative!r}")
    root_resolved = root.resolve(strict=True)
    candidate = (root_resolved / relative).resolve(strict=False)
    if not candidate.is_relative_to(root_resolved):
        raise UnsafePathError(f"{what} {relative!r} resolves outside {root_resolved}")
    return candidate


def ensure_inside(path: Path, root: Path, *, what: str) -> Path:
    """Ensure an absolute path is inside ``root`` before a destructive operation."""
    resolved = path.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise UnsafePathError(f"refusing to touch {what} {path}: it is not inside {root}")
    return resolved
