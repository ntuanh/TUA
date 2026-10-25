"""A project's default model and effort reach only the project's own provider.

Model names are provider-specific (`sonnet` is a Claude Code alias; Codex with a ChatGPT
login rejects it), so switching provider for one task must not carry the project's model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.conftest import FakeClaude, FakeCodex, git
from tests.integration.test_review import ScriptedConfirmer
from tua.adapters import create_adapter
from tua.adapters.base import ProviderAdapter
from tua.db import Database
from tua.paths import TuaPaths
from tua.peer_review import run_review
from tua.projects import init_project
from tua.review import ReviewService
from tua.tasks import RunOptions, TaskService


def configure(
    db: Database,
    paths: TuaPaths,
    repo: Path,
    *,
    provider: str = "claude-code",
    model: str | None = None,
    effort: str | None = None,
) -> str:
    init_project(db, paths, repo, "routing")
    config = repo / ".tua" / "project.yaml"
    text = config.read_text().replace(
        "default_provider: claude-code", f"default_provider: {provider}"
    )
    settings = [f"default_model: {model}"] if model else []
    settings += [f"effort: {effort}"] if effort else []
    if settings:
        text = text.replace("# default_model: sonnet", "\n".join(settings))
    config.write_text(text)
    git(repo, "add", ".tua")
    git(repo, "commit", "-q", "-m", "tua config")
    init_project(db, paths, repo, "routing")
    return "routing"


def noting_service(db: Database, paths: TuaPaths, notes: list[str]) -> TaskService:
    def factory(provider: str) -> ProviderAdapter:
        return create_adapter(provider, scratch_dir=paths.tmp_dir, grace_seconds=2.0)

    return TaskService(db, paths, factory, on_progress=notes.append)


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def codex_call(fake: FakeCodex) -> dict[str, Any]:
    (call,) = [c for c in fake.calls() if c["prompt"]]
    return call


def claude_call(fake: FakeClaude) -> dict[str, Any]:
    (call,) = fake.calls()
    return call


def test_claude_default_model_is_not_passed_to_codex(
    db: Database, paths: TuaPaths, repo: Path, fake_codex: FakeCodex
) -> None:
    project = configure(db, paths, repo, model="sonnet", effort="high")
    notes: list[str] = []
    task = noting_service(db, paths, notes).run(
        project, RunOptions(prompt="Create hello.txt", provider="codex")
    )
    call = codex_call(fake_codex)
    assert flag(call["argv"], "--model") is None
    assert "model_reasoning_effort" not in call["config"]
    assert task.provider == "codex"
    assert task.model_alias is None
    assert task.effort is None
    assert any(
        "default_model 'sonnet' and effort 'high' are for claude-code" in n
        and "codex uses its CLI default" in n
        for n in notes
    ), notes


def test_codex_review_of_claude_task_ignores_claude_default_model(
    db: Database,
    paths: TuaPaths,
    repo: Path,
    fake_claude: FakeClaude,
    fake_codex: FakeCodex,
) -> None:
    project = configure(db, paths, repo, model="sonnet")
    service = noting_service(db, paths, [])
    target = service.run(project, RunOptions(prompt="Create hello.txt"))
    assert flag(claude_call(fake_claude)["argv"], "--model") == "sonnet"
    review_service = ReviewService(db, paths, service, ScriptedConfirmer(), lambda _t: None)
    outcome = run_review(service, review_service, paths, target.id, provider="codex")
    assert outcome.completed
    assert outcome.task.provider == "codex"
    assert outcome.task.model_alias is None
    assert flag(codex_call(fake_codex)["argv"], "--model") is None


def test_codex_project_uses_its_own_default_model(
    db: Database, paths: TuaPaths, repo: Path, fake_codex: FakeCodex
) -> None:
    project = configure(db, paths, repo, provider="codex", model="gpt-6-luna", effort="low")
    notes: list[str] = []
    task = noting_service(db, paths, notes).run(project, RunOptions(prompt="Create hello.txt"))
    call = codex_call(fake_codex)
    assert flag(call["argv"], "--model") == "gpt-6-luna"
    assert call["config"]["model_reasoning_effort"] == '"low"'
    assert task.model_alias == "gpt-6-luna"
    assert not any(n.startswith("Note: the project's") for n in notes)


def test_naming_the_projects_own_provider_keeps_its_default_model(
    db: Database, paths: TuaPaths, repo: Path, fake_claude: FakeClaude
) -> None:
    project = configure(db, paths, repo, model="sonnet")
    task = noting_service(db, paths, []).run(
        project, RunOptions(prompt="Create hello.txt", provider="claude-code")
    )
    assert flag(claude_call(fake_claude)["argv"], "--model") == "sonnet"
    assert task.model_alias == "sonnet"


def test_explicit_model_overrides_on_the_same_provider(
    db: Database, paths: TuaPaths, repo: Path, fake_claude: FakeClaude
) -> None:
    project = configure(db, paths, repo, model="sonnet")
    task = noting_service(db, paths, []).run(
        project, RunOptions(prompt="Create hello.txt", model="haiku")
    )
    assert flag(claude_call(fake_claude)["argv"], "--model") == "haiku"
    assert task.model_alias == "haiku"


def test_explicit_model_is_passed_when_switching_to_codex(
    db: Database, paths: TuaPaths, repo: Path, fake_codex: FakeCodex
) -> None:
    project = configure(db, paths, repo, model="sonnet", effort="high")
    notes: list[str] = []
    task = noting_service(db, paths, notes).run(
        project,
        RunOptions(prompt="Create hello.txt", provider="codex", model="gpt-6-luna"),
    )
    assert flag(codex_call(fake_codex)["argv"], "--model") == "gpt-6-luna"
    assert task.model_alias == "gpt-6-luna"
    # Only the setting that was not given explicitly is reported as skipped.
    assert any("effort 'high' is for claude-code" in n for n in notes), notes
    assert not any("default_model" in n for n in notes), notes


def test_explicit_model_is_passed_when_switching_to_claude(
    db: Database, paths: TuaPaths, repo: Path, fake_claude: FakeClaude
) -> None:
    project = configure(db, paths, repo, provider="codex", model="gpt-6-luna")
    task = noting_service(db, paths, []).run(
        project, RunOptions(prompt="Create hello.txt", provider="claude-code", model="sonnet")
    )
    assert flag(claude_call(fake_claude)["argv"], "--model") == "sonnet"
    assert task.model_alias == "sonnet"


def test_codex_default_model_is_not_passed_to_claude(
    db: Database, paths: TuaPaths, repo: Path, fake_claude: FakeClaude
) -> None:
    project = configure(db, paths, repo, provider="codex", model="gpt-6-luna")
    task = noting_service(db, paths, []).run(
        project, RunOptions(prompt="Create hello.txt", provider="claude-code")
    )
    assert flag(claude_call(fake_claude)["argv"], "--model") is None
    assert task.model_alias is None


def test_no_model_configured_uses_each_cli_default(
    db: Database, paths: TuaPaths, repo: Path, fake_claude: FakeClaude, fake_codex: FakeCodex
) -> None:
    project = configure(db, paths, repo)
    notes: list[str] = []
    service = noting_service(db, paths, notes)
    claude_task = service.run(project, RunOptions(prompt="Create hello.txt"))
    assert flag(claude_call(fake_claude)["argv"], "--model") is None
    assert claude_task.model_alias is None
    codex_task = service.run(project, RunOptions(prompt="Create hello.txt", provider="codex"))
    assert flag(codex_call(fake_codex)["argv"], "--model") is None
    assert codex_task.model_alias is None
    assert not any(n.startswith("Note: the project's") for n in notes)
