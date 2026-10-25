from __future__ import annotations

from pathlib import Path

import pytest

from tua.config import CommandPolicy
from tua.errors import EvalError
from tua.evaluations import RunRecord, aggregate, check_commands, parse_suite

VALID = """\
schema_version: 1
suite_id: greet
project: demo
budget:
  max_runs: 4
  max_total_seconds: 600
cases:
  - id: hello
    prompt: "Create hello.txt"
    setup: ["python -m pip --version"]
    checks: ["python -m pytest -q"]
    metric:
      name: size
      command: "python scripts/measure.py"
      direction: lower_is_better
    runs: 2
    timeout_seconds: 60
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "suite.yaml"
    path.write_text(text)
    return path


def test_valid_suite(tmp_path: Path) -> None:
    suite = parse_suite(write(tmp_path, VALID), default_timeout=1800)
    assert suite.suite_id == "greet"
    assert suite.project == "demo"
    assert suite.planned_runs == 2
    assert suite.max_runs == 4
    (case,) = suite.cases
    assert case.checks[0].argv == ("python", "-m", "pytest", "-q")
    assert case.metric
    assert case.metric.direction == "lower_is_better"
    assert suite.baseline is None
    assert suite.fingerprint.startswith("sha256:")


def test_fingerprint_ignores_budget_and_pin_but_not_cases(tmp_path: Path) -> None:
    base = parse_suite(write(tmp_path, VALID), default_timeout=1).fingerprint
    budget = VALID.replace("max_runs: 4", "max_runs: 9")
    pinned = VALID + "baseline:\n  id: bl-1\n"
    runs = VALID.replace("runs: 2", "runs: 3")
    prompt = VALID.replace("Create hello.txt", "Create hi.txt")
    assert parse_suite(write(tmp_path, budget), default_timeout=1).fingerprint == base
    assert parse_suite(write(tmp_path, pinned), default_timeout=1).fingerprint == base
    assert parse_suite(write(tmp_path, runs), default_timeout=1).fingerprint == base
    assert parse_suite(write(tmp_path, prompt), default_timeout=1).fingerprint != base


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema_version: 1", "schema_version: 2", "schema_version"),
        ("suite_id: greet", "suite_id: Greet!", "suite_id"),
        ("  max_runs: 4\n", "", "max_runs"),
        ("budget:\n  max_runs: 4\n  max_total_seconds: 600\n", "", "budget"),
        ("max_total_seconds: 600", "max_total_seconds: 0", "max_total_seconds"),
        ("runs: 2", "runs: 50", "runs"),
        ("direction: lower_is_better", "direction: sideways", "direction"),
        ('checks: ["python -m pytest -q"]', 'checks: ["pytest && rm -rf /"]', "shell syntax"),
        ('checks: ["python -m pytest -q"]', 'checks: ["pytest tests/*.py"]', "shell syntax"),
        ('checks: ["python -m pytest -q"]', "checks: [42]", "non-empty strings"),
        ("    runs: 2\n", "    runs: 2\n    retries: 3\n", "unknown key"),
        ("cases:\n", "cases: []\nextra:\n", "unknown key"),
    ],
)
def test_invalid_suites(tmp_path: Path, old: str, new: str, message: str) -> None:
    assert old in VALID
    with pytest.raises(EvalError, match=message):
        parse_suite(write(tmp_path, VALID.replace(old, new)), default_timeout=1)


def test_duplicate_case_ids_and_empty_cases(tmp_path: Path) -> None:
    dup = VALID + "  - id: hello\n    prompt: again\n"
    with pytest.raises(EvalError, match="duplicate case id"):
        parse_suite(write(tmp_path, dup), default_timeout=1)
    empty = VALID.split("cases:")[0] + "cases: []\n"
    with pytest.raises(EvalError, match="at least one case"):
        parse_suite(write(tmp_path, empty), default_timeout=1)


def test_prompt_with_secret_is_rejected(tmp_path: Path) -> None:
    token = "sk-" + "ant-" + "K" * 30
    with pytest.raises(EvalError, match="secret"):
        parse_suite(
            write(tmp_path, VALID.replace("Create hello.txt", f"use {token}")), default_timeout=1
        )


def test_commands_must_be_allowlisted(tmp_path: Path) -> None:
    suite = parse_suite(write(tmp_path, VALID), default_timeout=1)
    policy = CommandPolicy(allow=(("python", "-m", "pytest"),), deny=())
    with pytest.raises(EvalError) as info:
        check_commands(suite, policy)
    assert "python -m pip --version" in info.value.message
    assert "python scripts/measure.py" in info.value.message
    full = CommandPolicy(
        allow=(
            ("python", "-m", "pytest"),
            ("python", "-m", "pip"),
            ("python", "scripts/measure.py"),
        ),
        deny=(),
    )
    assert len(check_commands(suite, full)) == 3
    denied = CommandPolicy(allow=full.allow, deny=(("python", "-m", "pip"),))
    with pytest.raises(EvalError, match="denied prefix"):
        check_commands(suite, denied)


def test_aggregate_counts_and_metrics(tmp_path: Path) -> None:
    suite = parse_suite(write(tmp_path, VALID), default_timeout=1)
    case = suite.cases[0]
    runs = [
        RunRecord("hello", 0, "c", "passed", metric_value=2.0, duration_seconds=1.0),
        RunRecord("hello", 1, "c", "checks_failed", metric_value=4.0, duration_seconds=3.0),
        RunRecord("hello", 2, "c", "skipped_budget"),
    ]
    summary = aggregate(case, runs)
    assert summary["runs_executed"] == 2
    assert summary["runs_skipped"] == 1
    assert summary["runs_passed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["metric"]["mean"] == 3.0
    assert summary["mean_duration_seconds"] == 2.0
