"""Project registry: initialize, register, and load projects with config fingerprint checks."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template

from tua.config import (
    NAME_RE,
    PROJECT_CONFIG_RELPATH,
    ProjectConfig,
    load_global_config,
    load_project_config,
    parse_command,
    parse_project_config,
    project_config_path,
)
from tua.db import Database
from tua.errors import ConfigError
from tua.models import Project
from tua.paths import TuaPaths
from tua.repository import Repository
from tua.skills import CompiledSkills, compile_project_skills

DEFAULT_SKILL_RELPATH = Path(".tua") / "skills" / "project-defaults.md"


def _template(name: str) -> str:
    return resources.files("tua.templates").joinpath(name).read_text(encoding="utf-8")


def _create_exclusive(path: Path, content: str) -> None:
    """Create a new file; never overwrite an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


@dataclass(frozen=True)
class InitResult:
    project: Project
    config: ProjectConfig
    compiled: CompiledSkills
    created_files: tuple[Path, ...]


def _render_config(name: str, allow: Sequence[str], root: Path, config_file: Path) -> str:
    """Render the template and validate the complete result before anything is written."""
    text = Template(_template("project.yaml")).substitute(name=name)
    if allow:
        for entry in allow:
            parse_command(entry, source=config_file, key="--allow")
        items = "".join(f"    - {json.dumps(entry)}\n" for entry in allow)
        text = text.replace("  allow: []\n", f"  allow:\n{items}", 1)
    parse_project_config(text.encode("utf-8"), root=root, config_path=config_file)
    return text


def init_project(
    db: Database, paths: TuaPaths, path: Path, name: str, *, allow: Sequence[str] = ()
) -> InitResult:
    """Create ``.tua/project.yaml`` if missing, validate it, and register the project.

    Existing files are never overwritten. Re-running init for an already registered
    project re-validates it and records the current config fingerprint. ``allow`` seeds
    ``commands.allow`` and is only accepted when the config file is being created.
    """
    if not NAME_RE.match(name):
        raise ConfigError(
            f"invalid project name {name!r}",
            hint="Use 1-64 lowercase letters, digits, '.', '_' or '-'.",
        )
    root = Repository.open(path.expanduser().resolve()).root
    config_file = project_config_path(root)
    created: list[Path] = []
    if config_file.exists() and allow:
        raise ConfigError(
            f"{config_file} already exists; --allow only applies when creating it",
            hint="Edit commands.allow in that file, then run `tua project init` again.",
        )
    if not config_file.exists():
        content = _render_config(name, allow, root, config_file)
        skill_file = root / DEFAULT_SKILL_RELPATH
        if not skill_file.exists():
            _create_exclusive(skill_file, _template("project-skill.md"))
            created.append(skill_file)
        _create_exclusive(config_file, content)
        created.append(config_file)

    config = load_project_config(root)
    if config.name != name:
        raise ConfigError(
            f"{config_file} declares name '{config.name}', but --name is '{name}'",
            hint=f"Use --name {config.name}, or edit the file.",
        )
    compiled = compile_project_skills(config, load_global_config(paths))
    project = db.upsert_project(
        name=config.name,
        root_path=str(root),
        config_path=str(config_file),
        config_fingerprint=config.fingerprint,
    )
    return InitResult(project, config, compiled, tuple(created))


def load_registered(
    db: Database, name: str, *, verify_fingerprint: bool = True
) -> tuple[Project, ProjectConfig]:
    """Load a registered project's config, refusing it if it changed since registration."""
    project = db.get_project(name)
    root = Path(project.root_path)
    if not root.is_dir():
        raise ConfigError(
            f"project '{name}' root {root} no longer exists",
            hint="Restore the repository or register it again from its new location.",
        )
    config = load_project_config(root)
    if config.name != project.name:
        raise ConfigError(
            f"{root / PROJECT_CONFIG_RELPATH} now declares name '{config.name}', but it is "
            f"registered as '{project.name}'"
        )
    if verify_fingerprint and config.fingerprint != project.config_fingerprint:
        raise ConfigError(
            f"{root / PROJECT_CONFIG_RELPATH} changed since project '{name}' was registered "
            f"(registered {project.config_fingerprint[:19]}…, now {config.fingerprint[:19]}…)",
            hint=f"Review it with `tua project show {name}`, then re-register with "
            f"`tua project init {root} --name {name}`.",
        )
    return project, config
