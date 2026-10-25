"""Opt-in smoke test against the real Claude Code CLI. It uses account quota.

Run with:  TUA_LIVE_CLAUDE=1 python -m pytest tests/live -v -s
Optional:  TUA_LIVE_MODEL=<alias> (default: haiku)

It proves three things the fake CLI cannot:
1. the compiled skill payload reaches the model (a random marker exists only there);
2. TUA's generated settings file is loaded: an allowlisted command runs while a
   non-allowlisted one is denied;
3. the real JSON result is parsed into a completed task with recorded metadata.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import pytest

from tests.conftest import git
from tua.adapters.claude_code import ClaudeCodeAdapter
from tua.db import Database
from tua.models import TaskStatus
from tua.paths import TuaPaths
from tua.projects import init_project
from tua.tasks import RunOptions, TaskService

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("TUA_LIVE_CLAUDE") != "1",
        reason="set TUA_LIVE_CLAUDE=1 to run against the real Claude Code CLI (uses quota)",
    ),
]

PROMPT = """\
This is an automated smoke test of a tool integration. Do exactly these steps, in order:
1. Create a file named smoke.txt in the current directory whose entire content is the TUA \
smoke marker defined in your system instructions (only the marker).
2. Run the shell command `mkdir tua-allowed` on its own.
3. Run the shell command `touch tua-denied.txt` on its own. It may be blocked by \
permissions; if it is, do not retry it and do not create that file any other way.
Then reply with one short sentence saying which steps succeeded.
"""


def test_live_headless_task(tmp_path: Path, db: Database, paths: TuaPaths) -> None:
    marker = f"TUA-SMOKE-{secrets.token_hex(6)}"
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# live smoke test\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")

    init_project(db, paths, repo, "live")
    model = os.environ.get("TUA_LIVE_MODEL", "haiku")
    config = repo / ".tua" / "project.yaml"
    text = config.read_text()
    text = text.replace("# default_model: sonnet", f"default_model: {model}")
    text = text.replace("timeout_seconds: 1800", "timeout_seconds: 300")
    text = text.replace("# max_turns: 50", "max_turns: 10")
    text = text.replace("allow: []", "allow: ['mkdir tua-allowed']")
    config.write_text(text)
    (repo / ".tua" / "skills" / "project-defaults.md").write_text(
        "---\nschema_version: 1\n---\n\n"
        f"## must: smoke-marker\nThe TUA smoke marker for this task is `{marker}`.\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "tua config")
    init_project(db, paths, repo, "live")

    service = TaskService(
        db, paths, lambda: ClaudeCodeAdapter(scratch_dir=paths.tmp_dir), on_progress=print
    )
    task = service.run("live", RunOptions(prompt=PROMPT))
    print(f"\nstatus={task.status.value} error={task.error_type}: {task.error_message}")
    print(f"result={task.result_excerpt}")
    assert task.status is TaskStatus.COMPLETED, task.error_message
    assert marker not in PROMPT

    worktree = Path(task.worktree_path)
    # 1. The marker only existed in the appended system prompt file.
    assert (worktree / "smoke.txt").read_text().strip().strip("`") == marker
    # 2. Allowed by TUA's generated settings rule Bash(mkdir tua-allowed).
    assert (worktree / "tua-allowed").is_dir()
    # ...while a command outside the allowlist was denied (dontAsk fails closed).
    payload = json.loads(Path(task.stdout_path or "").read_text())
    denied = [d.get("tool_name") for d in payload.get("permission_denials", [])]
    print(f"permission denials: {denied}")
    print(f"tua-denied.txt exists: {(worktree / 'tua-denied.txt').exists()}")
    assert "Bash" in denied
    # 3. Metadata recorded from the real output, never guessed.
    assert task.cli_version
    assert task.compiled_skill_hash
    assert task.source_skill_hash
    assert task.actual_models
    assert task.exit_code == 0
    print(f"cli={task.cli_version} models={task.actual_models} turns={task.num_turns}")
    print("result keys:", sorted(payload))
    assert not (repo / "smoke.txt").exists()  # the user's checkout was untouched
