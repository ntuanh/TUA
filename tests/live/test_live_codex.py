"""Opt-in smoke test against the real Codex CLI. It uses ChatGPT plan quota.

Run with:  TUA_LIVE_CODEX=1 python -m pytest tests/live/test_live_codex.py -v -s
Optional:  TUA_LIVE_CODEX_MODEL=<model> (default: gpt-6-luna), TUA_LIVE_CODEX_EFFORT (medium)

It proves that the compiled skills reach the model through ``developer_instructions``,
that the task can write inside its worktree, and that Codex's sandbox blocks a write
outside it.
"""

from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import git
from tua.adapters import create_adapter
from tua.db import Database
from tua.models import TaskStatus
from tua.paths import TuaPaths
from tua.projects import init_project
from tua.tasks import RunOptions, TaskService

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("TUA_LIVE_CODEX") != "1",
        reason="set TUA_LIVE_CODEX=1 to run against the real Codex CLI (uses quota)",
    ),
]

PROMPT = """\
This is an automated smoke test of a tool integration. Do exactly these steps, in order:
1. Create a file named smoke.txt in the current directory whose entire content is the TUA \
smoke marker defined in your developer instructions (only the marker).
2. Run the shell command `touch ../tua-sandbox-probe.txt` once. It is expected to fail \
because of the sandbox; do not retry it or work around it.
Then reply with one short sentence saying which steps succeeded.
"""


@pytest.fixture
def home_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[TuaPaths]:
    """Keep TUA state under $HOME, as in real use. Codex's workspace-write sandbox also
    allows writes to /tmp, so a worktree under /tmp could not show the sandbox boundary."""
    root = Path.home() / ".cache" / f"tua-live-codex-{secrets.token_hex(4)}"
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
    try:
        yield TuaPaths.from_env()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_live_codex_task(tmp_path: Path, home_state: TuaPaths) -> None:
    paths = home_state
    db = Database.open(paths.db_path)
    marker = f"TUA-CODEX-{secrets.token_hex(6)}"
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# live codex smoke test\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    init_project(db, paths, repo, "livecodex")
    (repo / ".tua" / "skills" / "project-defaults.md").write_text(
        "---\nschema_version: 1\n---\n\n"
        f"## must: smoke-marker\nThe TUA smoke marker for this task is `{marker}`.\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "tua config")
    init_project(db, paths, repo, "livecodex")

    service = TaskService(
        db,
        paths,
        lambda provider: create_adapter(provider, scratch_dir=paths.tmp_dir),
        on_progress=print,
    )
    task = service.run(
        "livecodex",
        RunOptions(
            prompt=PROMPT,
            provider="codex",
            model=os.environ.get("TUA_LIVE_CODEX_MODEL", "gpt-6-luna"),
            effort=os.environ.get("TUA_LIVE_CODEX_EFFORT", "medium"),
            timeout_seconds=300,
        ),
    )
    print(f"\nstatus={task.status.value} error={task.error_type}: {task.error_message}")
    print(f"result={task.result_excerpt}")
    assert task.status is TaskStatus.COMPLETED, task.error_message
    worktree = Path(task.worktree_path)
    assert (worktree / "smoke.txt").read_text().strip().strip("`") == marker
    probe = worktree.parent / "tua-sandbox-probe.txt"
    print(f"outside-worktree probe at {probe} exists: {probe.exists()}")
    assert not probe.exists()
    assert task.cli_version
    assert task.session_id
    print(f"cli={task.cli_version} model={task.model_alias} effort={task.effort}")
    db.close()
