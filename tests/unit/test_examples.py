"""The shipped examples and templates must stay valid against the real schemas."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from string import Template

from tua.config import CommandPolicy, parse_project_config
from tua.evaluations import check_commands, parse_suite
from tua.skills import SkillSource, builtin_source, compile_sources, parse_skill

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "examples"


def test_example_project_config_is_valid(tmp_path: Path) -> None:
    data = (EXAMPLES / "project.yaml").read_bytes()
    config = parse_project_config(data, root=tmp_path, config_path=EXAMPLES / "project.yaml")
    assert config.name == "sample-project"
    assert ("python", "-m", "pytest") in config.commands.allow


def test_example_eval_suite_is_valid_against_example_policy(tmp_path: Path) -> None:
    suite = parse_suite(EXAMPLES / "eval.yaml", default_timeout=1800)
    assert suite.project == "sample-project"
    config = parse_project_config(
        (EXAMPLES / "project.yaml").read_bytes(), root=tmp_path, config_path=tmp_path / "p.yaml"
    )
    assert len(check_commands(suite, config.commands)) == 3
    assert isinstance(config.commands, CommandPolicy)


def test_example_global_rules_and_templates_compile(tmp_path: Path) -> None:
    global_rules = SkillSource(
        "global", "global:global-rules.md", (EXAMPLES / "global-rules.md").read_bytes()
    )
    template_skill = resources.files("tua.templates").joinpath("project-skill.md").read_bytes()
    project = SkillSource("project", "project:.tua/skills/project-defaults.md", template_skill)
    compiled = compile_sources([builtin_source(), global_rules, project])
    assert len(compiled.must_rules) >= 6
    assert parse_skill(project)

    template = resources.files("tua.templates").joinpath("project.yaml").read_text()
    rendered = Template(template).substitute(name="demo").encode()
    config = parse_project_config(rendered, root=tmp_path, config_path=tmp_path / "p.yaml")
    assert config.name == "demo"
    assert config.commands.allow == ()
