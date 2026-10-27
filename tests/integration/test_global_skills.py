"""A global skill (`~/.config/tua/config.yaml`) reaches each provider, end to end."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeClaude, FakeCodex
from tua.models import TaskStatus
from tua.paths import TuaPaths
from tua.tasks import RunOptions, TaskService


def write_global_skill(paths: TuaPaths, rule: str) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.global_config_path.write_text("schema_version: 1\nskills:\n  - global-rules.md\n")
    (paths.config_dir / "global-rules.md").write_text(
        "---\nschema_version: 1\ndescription: Global rules for every project.\n---\n\n"
        f"## must: global-marker\n{rule}\n"
    )


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_global_skill_reaches_the_provider(
    provider: str,
    service: TaskService,
    paths: TuaPaths,
    project: str,
    fake_claude: FakeClaude,
    fake_codex: FakeCodex,
) -> None:
    def sent_hash() -> object:
        if provider == "claude-code":
            return fake_claude.calls()[-1]["append_sha256"]
        return [c for c in fake_codex.calls() if c["prompt"]][-1]["instructions_sha256"]

    write_global_skill(paths, "GLOBAL-RULE-ONE: never edit the vendor folder.")
    first = service.run(project, RunOptions(prompt="Create hello.txt", provider=provider))
    assert first.status is TaskStatus.COMPLETED, first.error_message
    payload = Path(str(first.compiled_skill_path)).read_text()
    assert "GLOBAL-RULE-ONE" in payload
    assert "keep-changes-scoped" in payload  # the project's own skill is still included
    assert sent_hash() == first.compiled_skill_hash  # the provider got exactly this payload

    # Editing the global skill changes what the next task receives and its fingerprints.
    write_global_skill(paths, "GLOBAL-RULE-TWO: never edit generated files.")
    second = service.run(project, RunOptions(prompt="Create hello.txt", provider=provider))
    assert second.status is TaskStatus.COMPLETED, second.error_message
    payload = Path(str(second.compiled_skill_path)).read_text()
    assert "GLOBAL-RULE-TWO" in payload
    assert "GLOBAL-RULE-ONE" not in payload
    assert second.source_skill_hash != first.source_skill_hash
    assert second.compiled_skill_hash != first.compiled_skill_hash
    assert sent_hash() == second.compiled_skill_hash
