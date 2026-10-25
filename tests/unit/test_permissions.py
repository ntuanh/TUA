from __future__ import annotations

import json
from pathlib import Path

import pytest

from tua.config import CommandPolicy
from tua.errors import ConfigError
from tua.paths import TuaPaths
from tua.permissions import build_plan, command_allowed, write_settings

POLICY = CommandPolicy(allow=(("python", "-m", "pytest"),), deny=(("python", "-m", "pip"),))


def test_safe_profile(tmp_path: Path, paths: TuaPaths) -> None:
    wt = tmp_path / "wt"
    plan = build_plan("safe", worktree=wt, commands=POLICY, paths=paths)
    assert plan.permission_mode == "dontAsk"
    assert f"Edit(/{wt.resolve()}/**)" in plan.allow
    assert f"Read(/{wt.resolve()}/**)" in plan.allow
    assert plan.allow[0].startswith("Read(//")
    assert "Bash(python -m pytest)" in plan.allow
    assert "Bash(python -m pytest *)" in plan.allow
    for rule in (
        "WebFetch",
        "WebSearch",
        "Bash(git push *)",
        "Bash(git reset --hard *)",
        "Bash(git clean *)",
        "Bash(python -m pip *)",
        "Read(~/.ssh/**)",
        "Read(~/.claude/**)",
    ):
        assert rule in plan.deny
    assert "Bash(git push *)" in plan.cli_deny
    assert not any(rule.startswith("Bash(git push") for rule in plan.allow)


def test_read_only_profile(tmp_path: Path, paths: TuaPaths) -> None:
    plan = build_plan("read-only", worktree=tmp_path, commands=POLICY, paths=paths)
    assert {"Edit", "Write", "NotebookEdit", "Bash"} <= set(plan.deny)
    assert not any(r.startswith(("Edit(", "Bash(")) for r in plan.allow)


def test_unknown_profile(tmp_path: Path, paths: TuaPaths) -> None:
    with pytest.raises(ConfigError):
        build_plan("open", worktree=tmp_path, commands=POLICY, paths=paths)


def test_settings_file_is_valid_private_json(tmp_path: Path, paths: TuaPaths) -> None:
    plan = build_plan("safe", worktree=tmp_path, commands=POLICY, paths=paths)
    target = write_settings(plan, tmp_path / "s" / "settings.json")
    data = json.loads(target.read_text())
    assert set(data) == {"permissions"}
    assert set(data["permissions"]) == {"allow", "deny"}
    assert (target.stat().st_mode & 0o077) == 0


@pytest.mark.parametrize(
    ("argv", "allowed"),
    [
        (["python", "-m", "pytest"], True),
        (["python", "-m", "pytest", "-q", "tests"], True),
        (["python", "-m", "pip", "install", "x"], False),
        (["python", "-c", "print(1)"], False),
        (["git", "push"], False),
        (["pytest"], False),
    ],
)
def test_command_allowed(argv: list[str], allowed: bool) -> None:
    ok, reason = command_allowed(argv, POLICY)
    assert ok is allowed
    assert reason
