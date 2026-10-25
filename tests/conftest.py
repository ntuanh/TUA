"""Shared fixtures. Every test runs with isolated HOME, XDG, and Git configuration, and with
any real ``claude`` executable removed from PATH so no test can use a real account."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tua.adapters.claude_code import ClaudeCodeAdapter
from tua.db import Database
from tua.logging_utils import LOGGER_NAME
from tua.paths import TuaPaths
from tua.projects import init_project
from tua.tasks import TaskService

FIXTURES = Path(__file__).parent / "fixtures"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / "xdg" / sub))
    gitconfig = home / ".gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = TUA Test\n\temail = tua-test@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)
    safe_path = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and not (Path(entry) / "claude").exists()
    ]
    monkeypatch.setenv("PATH", os.pathsep.join(safe_path))


@pytest.fixture(autouse=True)
def reset_tua_logging() -> Iterator[None]:
    yield
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


@pytest.fixture
def paths() -> TuaPaths:
    return TuaPaths.from_env()


@pytest.fixture
def db(paths: TuaPaths) -> Iterator[Database]:
    database = Database.open(paths.db_path)
    yield database
    database.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("# demo\n")
    (root / ".gitignore").write_text("build/\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root


@dataclass
class FakeClaude:
    control_path: Path
    log_path: Path
    bin_dir: Path

    def configure(self, **settings: Any) -> None:
        self.control_path.write_text(json.dumps(settings))

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n" + (FIXTURES / "fake_claude.py").read_text())
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    fake = FakeClaude(tmp_path / "fake-claude.json", tmp_path / "fake-claude.jsonl", bin_dir)
    monkeypatch.setenv("FAKE_CLAUDE_CONTROL", str(fake.control_path))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(fake.log_path))
    fake.configure(mode="success")
    return fake


@pytest.fixture
def project(db: Database, paths: TuaPaths, repo: Path) -> str:
    """A registered project whose .tua/ config is committed (so the checkout is clean)."""
    init_project(db, paths, repo, "demo")
    git(repo, "add", ".tua")
    git(repo, "commit", "-q", "-m", "add tua config")
    return "demo"


@pytest.fixture
def service(db: Database, paths: TuaPaths) -> TaskService:
    def factory() -> ClaudeCodeAdapter:
        return ClaudeCodeAdapter(scratch_dir=paths.tmp_dir, grace_seconds=2.0)

    return TaskService(db, paths, factory)
