from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import git
from tua.db import Database
from tua.errors import ConfigError, RepositoryError, StateError
from tua.paths import TuaPaths
from tua.projects import init_project, load_registered


def test_init_creates_files_and_registers(db: Database, paths: TuaPaths, repo: Path) -> None:
    result = init_project(db, paths, repo, "demo")
    config_file = repo / ".tua" / "project.yaml"
    skill_file = repo / ".tua" / "skills" / "project-defaults.md"
    assert set(result.created_files) == {config_file, skill_file}
    assert "name: demo" in config_file.read_text()
    assert result.project.root_path == str(repo.resolve())
    assert result.project.config_fingerprint == result.config.fingerprint
    assert result.compiled.source_hash.startswith("sha256:")
    assert [p.name for p in db.list_projects()] == ["demo"]


def test_init_never_overwrites_existing_files(db: Database, paths: TuaPaths, repo: Path) -> None:
    (repo / ".tua").mkdir()
    custom = "schema_version: 1\nname: demo\n# my comment\n"
    (repo / ".tua" / "project.yaml").write_text(custom)
    result = init_project(db, paths, repo, "demo")
    assert result.created_files == ()
    assert (repo / ".tua" / "project.yaml").read_text() == custom


def test_init_name_mismatch(db: Database, paths: TuaPaths, repo: Path) -> None:
    init_project(db, paths, repo, "demo")
    with pytest.raises(ConfigError, match="declares name 'demo'"):
        init_project(db, paths, repo, "other")


def test_init_rejects_non_repo_and_subdirectory(
    db: Database, paths: TuaPaths, tmp_path: Path, repo: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RepositoryError, match="not inside a Git repository"):
        init_project(db, paths, plain, "x")
    (repo / "sub").mkdir()
    with pytest.raises(RepositoryError, match="not its root"):
        init_project(db, paths, repo / "sub", "x")


def test_init_rejects_invalid_name(db: Database, paths: TuaPaths, repo: Path) -> None:
    with pytest.raises(ConfigError, match="invalid project name"):
        init_project(db, paths, repo, "Bad Name")


def test_init_rejects_broken_skills(db: Database, paths: TuaPaths, repo: Path) -> None:
    init_project(db, paths, repo, "demo")
    (repo / ".tua" / "skills" / "project-defaults.md").write_text("no front matter\n")
    from tua.errors import SkillError

    with pytest.raises(SkillError):
        init_project(db, paths, repo, "demo")


def test_changed_config_is_refused_until_reregistered(
    db: Database, paths: TuaPaths, repo: Path
) -> None:
    init_project(db, paths, repo, "demo")
    config_file = repo / ".tua" / "project.yaml"
    config_file.write_text(config_file.read_text().replace("allow: []", "allow: ['make test']"))
    with pytest.raises(ConfigError, match="changed since"):
        load_registered(db, "demo")
    _, config = load_registered(db, "demo", verify_fingerprint=False)
    assert config.commands.allow == (("make", "test"),)
    init_project(db, paths, repo, "demo")  # explicit re-registration
    load_registered(db, "demo")


def test_same_root_cannot_be_registered_twice(
    db: Database, paths: TuaPaths, repo: Path, tmp_path: Path
) -> None:
    init_project(db, paths, repo, "demo")
    config_file = repo / ".tua" / "project.yaml"
    config_file.write_text(config_file.read_text().replace("name: demo", "name: demo2"))
    with pytest.raises(StateError, match="already registered"):
        init_project(db, paths, repo, "demo2")


def test_moved_repository_is_reported(
    db: Database, paths: TuaPaths, repo: Path, tmp_path: Path
) -> None:
    init_project(db, paths, repo, "demo")
    git(repo, "add", ".tua")
    repo.rename(tmp_path / "moved")
    with pytest.raises(ConfigError, match="no longer exists"):
        load_registered(db, "demo")
