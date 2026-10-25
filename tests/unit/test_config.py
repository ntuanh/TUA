from __future__ import annotations

from pathlib import Path

import pytest

from tua.config import (
    CommandPolicy,
    load_global_config,
    parse_command,
    parse_project_config,
)
from tua.errors import ConfigError
from tua.paths import TuaPaths

VALID = """\
schema_version: 1
name: sample-project
repository: "."
default_provider: claude-code
default_model: "sonnet"
timeout_seconds: 600
max_turns: 20
permission_profile: safe
skills:
  - .tua/skills/a.md
eval_dir: evals
commands:
  allow: ["python -m pytest", "ruff check"]
  deny: ["python -m pytest --pdb"]
"""


def parse(text: str, root: Path) -> object:
    return parse_project_config(text.encode(), root=root, config_path=root / "project.yaml")


def test_valid_config(tmp_path: Path) -> None:
    config = parse_project_config(VALID.encode(), root=tmp_path, config_path=tmp_path / "p.yaml")
    assert config.name == "sample-project"
    assert config.root == tmp_path.resolve()
    assert config.default_model == "sonnet"
    assert config.timeout_seconds == 600
    assert config.max_turns == 20
    assert config.skills == (".tua/skills/a.md",)
    assert config.eval_dir == "evals"
    assert config.commands.allow == (("python", "-m", "pytest"), ("ruff", "check"))
    assert config.fingerprint.startswith("sha256:")


def test_defaults(tmp_path: Path) -> None:
    config = parse_project_config(
        b"schema_version: 1\nname: x\n", root=tmp_path, config_path=tmp_path / "p.yaml"
    )
    assert config.default_provider == "claude-code"
    assert config.permission_profile == "safe"
    assert config.timeout_seconds == 1800
    assert config.max_turns is None
    assert config.skills == ()
    assert config.commands == CommandPolicy(allow=(), deny=())


def test_fingerprint_changes_with_content(tmp_path: Path) -> None:
    a = parse_project_config(VALID.encode(), root=tmp_path, config_path=tmp_path / "p.yaml")
    b = parse_project_config(
        (VALID + "# comment\n").encode(), root=tmp_path, config_path=tmp_path / "p.yaml"
    )
    assert a.fingerprint != b.fingerprint


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (("name: sample-project", "name: Bad Name"), "name"),
        (("schema_version: 1", "schema_version: 2"), "schema_version"),
        (("schema_version: 1\n", ""), "schema_version"),
        (('repository: "."', 'repository: "sub"'), "repository"),
        (('repository: "."', 'repository: "../x"'), "repository"),
        (("default_provider: claude-code", "default_provider: other"), "default_provider"),
        (('default_model: "sonnet"', 'default_model: "a b"'), "default_model"),
        (("timeout_seconds: 600", "timeout_seconds: 0"), "timeout_seconds"),
        (("timeout_seconds: 600", "timeout_seconds: true"), "timeout_seconds"),
        (("timeout_seconds: 600", 'timeout_seconds: "600"'), "timeout_seconds"),
        (("max_turns: 20", "max_turns: 100000"), "max_turns"),
        (("permission_profile: safe", "permission_profile: yolo"), "permission_profile"),
        (("  - .tua/skills/a.md", "  - /etc/passwd"), "skills"),
        (("  - .tua/skills/a.md", "  - ../../x.md"), "skills"),
        (("  - .tua/skills/a.md", "  - a.md\n  - ./a.md"), "more than once"),
        (("eval_dir: evals", "eval_dir: /tmp"), "eval_dir"),
        (("eval_dir: evals", "eval_dir: evals\nunexpected: 1"), "unknown key"),
    ],
)
def test_invalid_values_are_rejected(
    tmp_path: Path, replacement: tuple[str, str], message: str
) -> None:
    old, new = replacement
    assert old in VALID
    with pytest.raises(ConfigError, match=message):
        parse(VALID.replace(old, new), tmp_path)


@pytest.mark.parametrize(
    "text", ["", "- a\n- b\n", "just text", "a: [unclosed", "1: x\nschema_version: 1"]
)
def test_non_mapping_or_invalid_yaml(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        parse(text, tmp_path)


@pytest.mark.parametrize(
    "entry",
    ["pytest; rm -rf /", "pytest && x", "pytest | tee", "echo $(id)", "pytest *", "a`b`", ""],
)
def test_command_entries_reject_shell_syntax(tmp_path: Path, entry: str) -> None:
    with pytest.raises(ConfigError):
        parse_command(entry, source=tmp_path, key="commands.allow")


@pytest.mark.parametrize(
    "allow", ["bash", "sh -c", "git", "git push", "git push origin", "curl", "sudo make", "env"]
)
def test_overly_broad_or_denied_allow_entries(tmp_path: Path, allow: str) -> None:
    text = f"schema_version: 1\nname: x\ncommands:\n  allow: ['{allow}']\n"
    with pytest.raises(ConfigError):
        parse(text, tmp_path)


def test_allow_entry_that_is_also_denied(tmp_path: Path) -> None:
    text = "schema_version: 1\nname: x\ncommands:\n  allow: ['make test']\n  deny: ['make']\n"
    with pytest.raises(ConfigError, match="also denied"):
        parse(text, tmp_path)


def test_commands_must_be_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="commands"):
        parse("schema_version: 1\nname: x\ncommands: [pytest]\n", tmp_path)


def test_global_config_absent_means_no_skills(paths: TuaPaths) -> None:
    config = load_global_config(paths)
    assert config.skills == ()
    assert config.path is None


def test_global_config_validates(paths: TuaPaths) -> None:
    paths.config_dir.mkdir(parents=True)
    paths.global_config_path.write_text("schema_version: 1\nskills: [rules.md]\n")
    assert load_global_config(paths).skills == ("rules.md",)
    paths.global_config_path.write_text("schema_version: 1\nskills: [../escape.md]\n")
    with pytest.raises(ConfigError):
        load_global_config(paths)
    paths.global_config_path.write_text("schema_version: 1\nextra: true\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_global_config(paths)


def test_oversized_config_is_rejected(tmp_path: Path) -> None:
    from tua.config import read_limited

    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * 300_000)
    with pytest.raises(ConfigError, match="limit"):
        read_limited(big, what="config")
