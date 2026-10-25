"""The `tua` command line, in-process and as a real subprocess (for signal handling)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import FakeClaude, git
from tua import __version__
from tua.cli import main
from tua.db import Database
from tua.models import TaskStatus
from tua.paths import TuaPaths

SUBCOMMANDS = [
    ["doctor"],
    ["project", "init"],
    ["project", "list"],
    ["project", "show"],
    ["run"],
    ["status"],
    ["diff"],
    ["merge"],
    ["discard"],
    ["good"],
    ["bad"],
    ["eval", "baseline"],
    ["eval", "run"],
]


def tua(
    *args: str, input_text: str | None = None, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tua", *args],
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout,
    )


def test_version_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"tua {__version__}"
    result = tua("--help")
    assert result.returncode == 0
    for command in (
        "doctor",
        "project",
        "run",
        "status",
        "diff",
        "merge",
        "discard",
        "good",
        "bad",
        "eval",
    ):
        assert command in result.stdout


@pytest.mark.parametrize("command", SUBCOMMANDS)
def test_every_subcommand_has_help(command: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([*command, "--help"])
    assert exit_info.value.code == 0
    assert "usage: tua" in capsys.readouterr().out


def test_help_does_not_create_state(paths: TuaPaths) -> None:
    assert tua("--help").returncode == 0
    assert not paths.state_dir.exists()


def test_doctor_reports_without_leaking_account_details(fake_claude: FakeClaude) -> None:
    result = tua("doctor")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "claude-code 2.1.301" in result.stdout
    assert "codex: not installed (optional)" in result.stdout
    assert "headless flags accepted" in result.stdout
    assert "logged in" in result.stdout
    for canary in ("leak-canary@example.invalid", "Leak Canary Org"):
        assert canary not in result.stdout + result.stderr


def test_doctor_fails_actionably_without_claude() -> None:
    result = tua("doctor")
    assert result.returncode == 1
    assert "[FAIL] Claude Code CLI (`claude`) was not found" in result.stdout
    assert "hint:" in result.stdout


def test_doctor_detects_unsupported_cli(fake_claude: FakeClaude) -> None:
    fake_claude.configure(unsupported_flags=["--permission-prompts"])
    result = tua("doctor")
    assert result.returncode == 1
    assert "does not support --permission-prompts" in result.stdout


def test_errors_are_rendered_without_traceback() -> None:
    result = tua("status", "20990101-000000-0000")
    assert result.returncode == 1
    assert result.stderr.startswith("error: no task with id")
    assert "Traceback" not in result.stderr
    assert "hint:" in result.stderr


def test_full_workflow(repo: Path, fake_claude: FakeClaude, paths: TuaPaths) -> None:
    init = tua("project", "init", str(repo), "--name", "demo")
    assert init.returncode == 0, init.stderr
    assert "Registered project 'demo'" in init.stdout
    assert "commit them" in init.stdout

    dirty = tua("run", "Create hello.txt", "--project", "demo")
    assert dirty.returncode == 1
    assert "uncommitted or untracked" in dirty.stderr

    git(repo, "add", ".tua")
    git(repo, "commit", "-q", "-m", "tua config")
    assert "demo" in tua("project", "list").stdout
    show = tua("project", "show", "demo")
    assert show.returncode == 0
    assert "compiled_skill_hash" in show.stdout
    assert "dontAsk" in show.stdout

    run = tua("run", "Create hello.txt", "--project", "demo")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "completed" in run.stdout
    assert "tua merge" in run.stdout
    db = Database.open(paths.db_path)
    (task,) = db.list_tasks()
    db.close()

    listing = tua("status")
    assert task.id in listing.stdout
    assert "completed" in listing.stdout
    detail = tua("status", task.id)
    assert "compiled_skill_hash" in detail.stdout
    assert "Events:" in detail.stdout
    diff = tua("diff", task.id)
    assert "hello.txt" in diff.stdout
    assert "+hello from fake claude" in diff.stdout

    assert tua("good", task.id, "--note", "worked").returncode == 0
    assert tua("bad", task.id).returncode == 2  # --reason is required
    bad = tua("bad", task.id, "--reason", "too verbose")
    assert bad.returncode == 0
    assert "does not infer a root cause" in bad.stdout

    declined = tua("merge", task.id, input_text="n\n")
    assert declined.returncode == 1
    assert "nothing was changed" in declined.stderr
    assert not (repo / "hello.txt").exists()

    merged = tua("merge", task.id, "--yes")
    assert merged.returncode == 0, merged.stderr
    assert "not pushed" in merged.stdout
    assert (repo / "hello.txt").exists()
    assert "merged" in tua("status", task.id).stdout
    assert tua("discard", task.id, "--yes").returncode == 1  # already merged and cleaned


def _start_hanging_task(
    repo: Path, fake_claude: FakeClaude, tmp_path: Path
) -> tuple[subprocess.Popen[str], int]:
    assert tua("project", "init", str(repo), "--name", "demo").returncode == 0
    git(repo, "add", ".tua")
    git(repo, "commit", "-q", "-m", "tua config")
    started = tmp_path / "provider.pid"
    fake_claude.configure(mode="hang", started_file=str(started))
    proc = subprocess.Popen(
        [sys.executable, "-m", "tua", "run", "wait forever", "--project", "demo"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    end = time.monotonic() + 30
    while time.monotonic() < end and not (started.exists() and started.read_text()):
        time.sleep(0.05)
    assert started.exists(), "fake provider never started"
    return proc, int(started.read_text())


def _wait_dead(pid: int) -> None:
    end = time.monotonic() + 15
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} still alive")


@pytest.mark.parametrize(("sig", "name"), [(signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")])
def test_signal_cancels_task_and_keeps_worktree(
    repo: Path, fake_claude: FakeClaude, tmp_path: Path, paths: TuaPaths, sig: int, name: str
) -> None:
    proc, provider_pid = _start_hanging_task(repo, fake_claude, tmp_path)
    proc.send_signal(sig)
    stdout, stderr = proc.communicate(timeout=60)
    assert proc.returncode == 130, stdout + stderr
    assert "cancelled" in stdout
    _wait_dead(provider_pid)
    db = Database.open(paths.db_path)
    (task,) = db.list_tasks()
    events = db.list_events(task.id)
    db.close()
    assert task.status is TaskStatus.CANCELLED
    assert Path(task.worktree_path).is_dir()
    assert events[-1].metadata
    assert events[-1].metadata.get("signal") == name


def test_killed_runner_is_reconciled_by_status(
    repo: Path, fake_claude: FakeClaude, tmp_path: Path, paths: TuaPaths
) -> None:
    proc, provider_pid = _start_hanging_task(repo, fake_claude, tmp_path)
    proc.kill()  # SIGKILL: TUA cannot record anything
    proc.communicate(timeout=30)
    detail = tua("status")
    assert "running" in detail.stdout  # the orphaned provider is still alive
    os.killpg(provider_pid, signal.SIGKILL)
    _wait_dead(provider_pid)
    db = Database.open(paths.db_path)
    (task,) = db.list_tasks()
    db.close()
    after = tua("status", task.id)
    assert "failed" in after.stdout
    assert "interrupted" in after.stdout
    assert Path(task.worktree_path).is_dir()
    assert tua("discard", task.id, "--yes").returncode == 0


def test_eval_cli_end_to_end(repo: Path, fake_claude: FakeClaude, paths: TuaPaths) -> None:
    assert tua("project", "init", str(repo), "--name", "demo").returncode == 0
    (repo / ".tua" / "evals").mkdir()
    suite = repo / ".tua" / "evals" / "smoke.yaml"
    suite.write_text(
        "schema_version: 1\nsuite_id: smoke\nproject: demo\n"
        "budget:\n  max_runs: 2\n  max_total_seconds: 600\n"
        "cases:\n  - id: hello\n    prompt: Create hello.txt\n    runs: 1\n"
        "    timeout_seconds: 60\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "suite")
    baseline = tua("eval", "baseline", str(suite), "--yes")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    assert "Stored immutable baseline" in baseline.stdout
    pin_block = baseline.stdout[baseline.stdout.index("baseline:\n") :]
    unpinned = tua("eval", "run", str(suite), "--yes")
    assert unpinned.returncode == 1
    assert "does not pin a baseline" in unpinned.stderr
    suite.write_text(suite.read_text() + pin_block)
    candidate = tua("eval", "run", str(suite), "--yes")
    assert candidate.returncode == 0, candidate.stdout + candidate.stderr
    assert "Comparison with pinned baseline" in candidate.stdout
    db = Database.open(paths.db_path)
    rows = db.fetch_all("SELECT kind, status FROM eval_runs ORDER BY started_at")
    db.close()
    assert [tuple(r) for r in rows] == [("baseline", "complete"), ("candidate", "complete")]


def test_review_and_profile_cli(repo: Path, fake_claude: FakeClaude, paths: TuaPaths) -> None:
    init = tua("project", "init", str(repo), "--name", "demo", "--allow", "python -m pytest")
    assert init.returncode == 0, init.stderr
    assert "python -m pytest" in init.stdout
    git(repo, "add", ".tua")
    git(repo, "commit", "-q", "-m", "tua config")

    run = tua("run", "Create hello.txt", "--project", "demo")
    assert run.returncode == 0, run.stderr
    assert "API-equivalent estimate" in run.stdout
    db = Database.open(paths.db_path)
    (task,) = db.list_tasks()
    db.close()

    fake_claude.configure(mode="success", write_files={}, result="CONFIRMED low: no test.")
    review = tua("review", task.id, "--model", "sonnet")
    assert review.returncode == 0, review.stdout + review.stderr
    assert "CONFIRMED low: no test." in review.stdout
    assert "extra opinion, not a verdict" in review.stdout
    detail = tua("status", task.id)
    assert "Reviews:" in detail.stdout
    listing = tua("status")
    assert f"[review of {task.id}]" in listing.stdout

    read_only = tua("run", "Explain the code", "--project", "demo", "--profile", "read-only")
    assert read_only.returncode == 0, read_only.stderr
    assert tua("run", "x", "--project", "demo", "--profile", "admin").returncode == 2
