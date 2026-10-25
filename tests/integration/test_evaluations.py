"""Eval runner end to end with the fake CLI: no real provider quota is ever used."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import FakeClaude, git
from tests.integration.test_review import ScriptedConfirmer
from tua.db import Database
from tua.errors import AbortedError, EvalError
from tua.evaluations import EvalService
from tua.models import TaskStatus
from tua.paths import TuaPaths
from tua.projects import init_project
from tua.review import ReviewService
from tua.tasks import TaskService

PY = sys.executable

CHECK = "import pathlib, sys\nsys.exit(0 if pathlib.Path('hello.txt').exists() else 1)\n"
MEASURE = "import pathlib\nprint(len(pathlib.Path('hello.txt').read_text()))\n"


def suite_yaml(
    *,
    runs: int = 2,
    max_runs: int = 10,
    max_seconds: int = 600,
    timeout: int = 60,
    extra_case: bool = False,
    pin: dict[str, str] | None = None,
) -> str:
    text = f"""\
schema_version: 1
suite_id: greet
project: demo
budget:
  max_runs: {max_runs}
  max_total_seconds: {max_seconds}
cases:
  - id: hello
    prompt: "Create hello.txt"
    checks: ["{PY} scripts/check.py"]
    metric:
      name: size
      command: "{PY} scripts/measure.py"
      direction: lower_is_better
    runs: {runs}
    timeout_seconds: {timeout}
"""
    if extra_case:
        text += f"""\
  - id: second
    prompt: "Create hello.txt again"
    checks: ["{PY} scripts/check.py"]
    timeout_seconds: {timeout}
"""
    if pin:
        text += "baseline:\n" + "".join(f'  {k}: "{v}"\n' for k, v in pin.items())
    return text


class Env:
    def __init__(self, db: Database, paths: TuaPaths, repo: Path, service: TaskService) -> None:
        self.db, self.paths, self.repo, self.service = db, paths, repo, service
        self.confirmer = ScriptedConfirmer()
        self.output: list[str] = []
        review = ReviewService(db, paths, service, self.confirmer, self.output.append)
        self.evals = EvalService(db, paths, service, review, self.confirmer, self.output.append)
        self.suite = repo / ".tua" / "evals" / "greet.yaml"

    def write_suite(self, text: str) -> Path:
        self.suite.write_text(text)
        return self.suite

    def pin_of(self, baseline_id: str) -> dict[str, str]:
        row = self.db.fetch_one("SELECT * FROM eval_baselines WHERE id = ?", (baseline_id,))
        assert row is not None
        return {
            "id": row["id"],
            "suite_fingerprint": row["suite_fingerprint"],
            "source_skill_hash": row["source_skill_hash"],
            "compiled_skill_hash": row["compiled_skill_hash"],
        }

    def baselines(self) -> list[dict[str, object]]:
        return [
            dict(r) for r in self.db.fetch_all("SELECT * FROM eval_baselines ORDER BY created_at")
        ]


@pytest.fixture
def env(
    db: Database, paths: TuaPaths, repo: Path, service: TaskService, fake_claude: FakeClaude
) -> Env:
    init_project(db, paths, repo, "demo")
    config = repo / ".tua" / "project.yaml"
    config.write_text(
        config.read_text().replace(
            "allow: []", f"allow: ['{PY} scripts/check.py', '{PY} scripts/measure.py']"
        )
    )
    (repo / "scripts").mkdir()
    (repo / "scripts" / "check.py").write_text(CHECK)
    (repo / "scripts" / "measure.py").write_text(MEASURE)
    (repo / ".tua" / "evals").mkdir()
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "eval setup")
    init_project(db, paths, repo, "demo")  # re-register the edited config
    fake_claude.configure(mode="success", write_files={"hello.txt": "hello\n"})
    return Env(db, paths, repo, service)


def provider_calls(fake: FakeClaude) -> int:
    return len([c for c in fake.calls() if c["prompt"]])


def test_baseline_then_candidate_without_rerunning_baseline(
    env: Env, fake_claude: FakeClaude
) -> None:
    suite = env.write_suite(suite_yaml())
    report = env.evals.baseline(suite, assume_yes=True)
    assert report.status == "complete"
    assert report.exit_code == 0
    assert provider_calls(fake_claude) == 2
    (stored,) = env.baselines()
    manifest_path = Path(str(stored["manifest_path"]))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["coverage"] == "complete"
    assert manifest["cases"][0]["runs_passed"] == 2
    assert manifest["cases"][0]["metric"]["mean"] == 6.0
    assert manifest["source_skill_hash"] == stored["source_skill_hash"]
    assert manifest["case_commits"]["hello"] == git(env.repo, "rev-parse", "HEAD").strip()
    assert manifest["runs"][0]["task_id"]
    assert (manifest_path.stat().st_mode & 0o777) == 0o400
    # Completed eval tasks are discarded after measurement by default.
    for run in manifest["runs"]:
        assert env.db.get_task(run["task_id"]).status is TaskStatus.DISCARDED

    env.write_suite(suite_yaml(pin=env.pin_of(str(stored["id"]))))
    candidate = env.evals.run(suite, assume_yes=True)
    assert candidate.status == "complete"
    assert provider_calls(fake_claude) == 4  # only the 2 candidate runs were added
    text = candidate.render()
    assert f"pinned baseline {stored['id']}" in text
    assert "pass 2/2 -> 2/2" in text
    assert "size mean 6.0 -> 6.0" in text
    assert "identical to the baseline" in text
    assert "statistical significance" in text
    kinds = [r["kind"] for r in env.db.fetch_all("SELECT kind FROM eval_runs ORDER BY started_at")]
    assert kinds == ["baseline", "candidate"]


def test_baseline_is_not_overwritten_and_replacement_keeps_history(env: Env) -> None:
    suite = env.write_suite(suite_yaml(runs=1))
    env.evals.baseline(suite, assume_yes=True)
    with pytest.raises(EvalError, match="refusing to overwrite"):
        env.evals.baseline(suite, assume_yes=True)
    env.confirmer.answers = [False]
    with pytest.raises(AbortedError):
        env.evals.baseline(suite, replace=True, assume_yes=True)
    env.confirmer.answers = [True]
    env.evals.baseline(suite, replace=True, assume_yes=True)
    old, new = env.baselines()
    assert old["superseded_by"] == new["id"]
    assert old["superseded_at"]
    assert new["superseded_at"] is None
    assert Path(str(old["manifest_path"])).exists()
    with pytest.raises(Exception, match="immutable"):
        env.db.conn.execute("UPDATE eval_baselines SET compiled_skill_hash = 'x'")
    with pytest.raises(Exception, match="immutable"):
        env.db.conn.execute("DELETE FROM eval_baselines")


def test_candidate_requires_a_valid_pin(env: Env, fake_claude: FakeClaude) -> None:
    suite = env.write_suite(suite_yaml(runs=1))
    with pytest.raises(EvalError, match="does not pin a baseline") as info:
        env.evals.run(suite, assume_yes=True)
    assert "eval baseline" in (info.value.hint or "")
    env.evals.baseline(suite, assume_yes=True)
    (stored,) = env.baselines()
    pin = env.pin_of(str(stored["id"]))
    calls = provider_calls(fake_claude)

    env.write_suite(suite_yaml(runs=1, pin={**pin, "compiled_skill_hash": "sha256:" + "0" * 64}))
    with pytest.raises(EvalError, match="does not match"):
        env.evals.run(suite, assume_yes=True)
    env.write_suite(suite_yaml(runs=1, pin={**pin, "id": "bl-missing"}))
    with pytest.raises(EvalError, match="does not exist"):
        env.evals.run(suite, assume_yes=True)

    changed = suite_yaml(runs=1, pin=pin).replace("Create hello.txt", "Create other.txt")
    env.write_suite(changed)
    with pytest.raises(EvalError, match="changed since baseline"):
        env.evals.run(suite, assume_yes=True)

    manifest = Path(str(stored["manifest_path"]))
    manifest.chmod(0o600)
    manifest.write_text(manifest.read_text().replace('"complete"', '"tampered"'))
    env.write_suite(suite_yaml(runs=1, pin=pin))
    with pytest.raises(EvalError, match="modified"):
        env.evals.run(suite, assume_yes=True)
    assert provider_calls(fake_claude) == calls  # nothing ran for any refused candidate


def test_disallowed_command_is_refused_before_anything_runs(
    env: Env, fake_claude: FakeClaude
) -> None:
    suite = env.write_suite(
        suite_yaml().replace(f"{PY} scripts/check.py", f"{PY} scripts/other.py")
    )
    with pytest.raises(EvalError, match="does not allow"):
        env.evals.baseline(suite, assume_yes=True)
    assert provider_calls(fake_claude) == 0
    assert env.db.fetch_all("SELECT * FROM eval_runs") == []


def test_suite_outside_eval_dir_is_refused(env: Env, tmp_path: Path) -> None:
    outside = tmp_path / "suite.yaml"
    outside.write_text(suite_yaml())
    with pytest.raises(EvalError, match="outside the project's eval_dir"):
        env.evals.baseline(outside, assume_yes=True)


def test_declined_confirmation_runs_nothing(env: Env, fake_claude: FakeClaude) -> None:
    suite = env.write_suite(suite_yaml())
    env.confirmer.answers = [False]
    with pytest.raises(AbortedError):
        env.evals.baseline(suite)
    assert provider_calls(fake_claude) == 0
    assert any("scripts/check.py" in line for line in env.output)  # commands were displayed


def test_run_count_budget_stops_before_exceeding(env: Env, fake_claude: FakeClaude) -> None:
    suite = env.write_suite(suite_yaml(runs=2, extra_case=True, max_runs=2))
    report = env.evals.baseline(suite, assume_yes=True)
    assert provider_calls(fake_claude) == 2
    assert report.status == "incomplete"
    assert report.exit_code == 1
    statuses = [(r.case_id, r.status) for r in report.records]
    assert statuses == [("hello", "passed"), ("hello", "passed"), ("second", "skipped_budget")]
    assert "2/3 planned run(s) executed" in report.render()
    run = env.db.fetch_one("SELECT * FROM eval_runs")
    assert run is not None
    assert run["runs_started"] == 2
    assert run["runs_skipped"] == 1
    # CLI-style override can only be set explicitly; here it lowers the budget further.
    env.confirmer.answers = [True]
    again = env.evals.baseline(suite, replace=True, assume_yes=True, max_runs=1)
    assert sum(1 for r in again.records if r.status == "passed") == 1


def test_wall_clock_budget_never_starts_a_run_that_could_exceed_it(
    env: Env, fake_claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SteppingClock:
        extra = 0.0

        def __call__(self) -> float:
            return time.monotonic() + self.extra

    clock = SteppingClock()
    env.evals.clock = clock
    save = env.evals._save_record

    def save_and_advance(*args: object) -> None:
        save(*args)  # type: ignore[arg-type]
        clock.extra += 200  # pretend every run took 200 seconds

    monkeypatch.setattr(env.evals, "_save_record", save_and_advance)
    suite = env.write_suite(suite_yaml(runs=2, timeout=300, max_seconds=400))
    report = env.evals.baseline(suite, assume_yes=True)
    # Run 1 fits (0 + 300 + overhead <= 400). After it, 200s are used and a second run
    # could take up to 300s more, so it is never started.
    assert [r.status for r in report.records] == ["passed", "skipped_budget"]
    assert "max_total_seconds" in (report.records[1].detail or "")
    tight = env.write_suite(suite_yaml(runs=1, timeout=300, max_seconds=100))
    env.confirmer.answers = [True]
    none = env.evals.baseline(tight, replace=True, assume_yes=True)
    assert [r.status for r in none.records] == ["skipped_budget"]
    assert provider_calls(fake_claude) == 1
    # A baseline without a single executed run would be meaningless, so none is stored.
    assert "No baseline was stored" in none.render()
    assert len(env.baselines()) == 1


def test_failed_runs_keep_worktrees_and_failed_checks_are_recorded(
    env: Env, fake_claude: FakeClaude
) -> None:
    fake_claude.configure(mode="provider_error")
    suite = env.write_suite(suite_yaml(runs=1))
    report = env.evals.baseline(suite, assume_yes=True)
    (record,) = report.records
    assert record.status == "provider_failed"
    assert record.task_id is not None
    task = env.db.get_task(record.task_id)
    assert task.status is TaskStatus.FAILED
    assert Path(task.worktree_path).is_dir()

    fake_claude.configure(mode="success", write_files={"other.txt": "x"})  # check will fail
    env.confirmer.answers = [True]
    report = env.evals.baseline(suite, replace=True, assume_yes=True, keep_worktrees=True)
    (record,) = report.records
    assert record.status == "checks_failed"
    assert record.checks_passed == 0
    assert record.task_id is not None
    kept = env.db.get_task(record.task_id)
    assert kept.status is TaskStatus.COMPLETED
    assert Path(kept.worktree_path).is_dir()
    logs = list(Path(env.paths.evals_dir).rglob("check-1.log"))
    assert logs
    assert "exit: 1" in logs[-1].read_text()


def test_ctrl_c_cancels_the_eval_and_stores_no_baseline(
    env: Env, fake_claude: FakeClaude, tmp_path: Path
) -> None:
    started = tmp_path / "started"
    fake_claude.configure(mode="hang", started_file=str(started))
    suite = env.write_suite(suite_yaml(runs=2))

    def interrupt() -> None:
        end = time.monotonic() + 20
        while not started.exists() and time.monotonic() < end:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=interrupt)
    thread.start()
    report = env.evals.baseline(suite, assume_yes=True)
    thread.join()
    assert report.status == "cancelled"
    assert report.exit_code == 130
    assert [r.status for r in report.records] == ["cancelled", "skipped_cancelled"]
    assert env.baselines() == []
    assert "No baseline was stored" in report.render()
    task_id = report.records[0].task_id
    assert task_id is not None
    task = env.db.get_task(task_id)
    assert task.status is TaskStatus.CANCELLED
    assert Path(task.worktree_path).is_dir()
