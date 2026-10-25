from __future__ import annotations

import stat
from pathlib import Path

import pytest

from tua.errors import UnsafePathError
from tua.paths import (
    TuaPaths,
    create_private_file,
    ensure_inside,
    ensure_private_dir,
    is_private,
    resolve_within,
    write_private_file,
)


def test_xdg_variables_are_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    paths = TuaPaths.from_env()
    assert paths.state_dir == tmp_path / "s" / "tua"
    assert paths.db_path == tmp_path / "s" / "tua" / "tua.db"


def test_relative_xdg_values_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    paths = TuaPaths.from_env()
    assert paths.config_dir == Path.home() / ".config" / "tua"


def test_private_dir_and_file_modes(tmp_path: Path) -> None:
    directory = ensure_private_dir(tmp_path / "a" / "b")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    target = directory / "f.txt"
    write_private_file(target, b"data")
    assert target.read_bytes() == b"data"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    write_private_file(target, b"new")  # atomic replace keeps the private mode
    assert target.read_bytes() == b"new"
    assert is_private(target)
    assert [p.name for p in directory.iterdir()] == ["f.txt"]  # no temp files left


def test_existing_world_readable_dir_is_tightened(tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    ensure_private_dir(loose)
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700


def test_create_private_file_never_truncates(tmp_path: Path) -> None:
    target = tmp_path / "x"
    create_private_file(target)
    target.write_text("keep")
    create_private_file(target)
    assert target.read_text() == "keep"


@pytest.mark.parametrize(
    "value",
    ["/etc/passwd", "../outside.md", "a/../../outside.md", "~/x.md", "", "   ", "a\x00b"],
)
def test_resolve_within_rejects_unsafe_values(tmp_path: Path, value: str) -> None:
    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path, value, what="skill")


def test_resolve_within_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("x")
    (root / "link.md").symlink_to(outside)
    with pytest.raises(UnsafePathError):
        resolve_within(root, "link.md", what="skill")


def test_resolve_within_accepts_nested_paths(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    assert resolve_within(tmp_path, "skills/./a.md", what="skill") == (
        tmp_path.resolve() / "skills" / "a.md"
    )


def test_ensure_inside_rejects_root_and_outside(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    root.mkdir()
    assert ensure_inside(root / "t1", root, what="worktree") == (root / "t1").resolve()
    with pytest.raises(UnsafePathError):
        ensure_inside(root, root, what="worktree")
    with pytest.raises(UnsafePathError):
        ensure_inside(tmp_path / "elsewhere", root, what="worktree")
    with pytest.raises(UnsafePathError):
        ensure_inside(root / ".." / "x", root, what="worktree")
