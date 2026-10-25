from __future__ import annotations

import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from tua.db import SCHEMA_VERSION, Database
from tua.errors import InvalidTransitionError, StateError
from tua.models import TaskStatus


def task_fields(task_id: str = "20260101-000000-abcd", project_id: int = 1) -> dict[str, Any]:
    return {
        "id": task_id,
        "project_id": project_id,
        "prompt": "do it",
        "prompt_sha256": "sha256:x",
        "source_repo": "/r",
        "target_branch": "main",
        "base_commit": "a" * 40,
        "task_branch": f"tua/{task_id}",
        "worktree_path": f"/w/{task_id}",
        "provider": "claude-code",
        "permission_profile": "safe",
        "config_fingerprint": "sha256:c",
        "timeout_seconds": 10,
    }


@pytest.fixture
def project_db(db: Database) -> Database:
    db.upsert_project(
        name="p", root_path="/r", config_path="/r/.tua/project.yaml", config_fingerprint="sha256:c"
    )
    return db


def test_new_database_is_private_and_versioned(db: Database) -> None:
    assert db.schema_version() == SCHEMA_VERSION
    assert stat.S_IMODE(db.path.stat().st_mode) == 0o600
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "projects",
        "tasks",
        "task_events",
        "feedback",
        "eval_runs",
        "eval_case_runs",
        "eval_baselines",
    } <= tables


def test_reopen_is_idempotent(db: Database) -> None:
    db.upsert_project(name="p", root_path="/r", config_path="/c", config_fingerprint="f")
    db.close()
    again = Database.open(db.path)
    assert [p.name for p in again.list_projects()] == ["p"]
    again.close()


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "tua.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(StateError, match="schema version"):
        Database.open(path)


def test_corrupt_file_is_refused_and_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "tua.db"
    garbage = b"this is not a sqlite database at all" * 100
    path.write_bytes(garbage)
    with pytest.raises(StateError, match="corrupt") as info:
        Database.open(path)
    assert info.value.hint
    assert "mv" in info.value.hint
    assert path.read_bytes() == garbage


def test_unrelated_sqlite_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "tua.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    with pytest.raises(StateError, match="unrecognized"):
        Database.open(path)


def test_project_name_and_root_uniqueness(db: Database) -> None:
    db.upsert_project(name="a", root_path="/r1", config_path="/c", config_fingerprint="f1")
    updated = db.upsert_project(
        name="a", root_path="/r1", config_path="/c", config_fingerprint="f2"
    )
    assert updated.config_fingerprint == "f2"
    with pytest.raises(StateError, match="already registered"):
        db.upsert_project(name="a", root_path="/r2", config_path="/c", config_fingerprint="f")
    with pytest.raises(StateError, match="already registered"):
        db.upsert_project(name="b", root_path="/r1", config_path="/c", config_fingerprint="f")


def test_insert_task_records_created_and_preparing(project_db: Database) -> None:
    task = project_db.insert_task(task_fields())
    assert task.status is TaskStatus.PREPARING
    events = project_db.list_events(task.id)
    assert [(e.event, e.from_status, e.to_status) for e in events] == [
        ("created", None, "created"),
        ("transition", "created", "preparing"),
    ]


def test_duplicate_task_id_raises_integrity_error(project_db: Database) -> None:
    project_db.insert_task(task_fields())
    with pytest.raises(sqlite3.IntegrityError):
        project_db.insert_task(task_fields())


def test_transition_validation_and_rollback(project_db: Database) -> None:
    task = project_db.insert_task(task_fields())
    with pytest.raises(InvalidTransitionError):
        project_db.transition(task.id, TaskStatus.COMPLETED)
    assert project_db.get_task(task.id).status is TaskStatus.PREPARING
    project_db.transition(task.id, TaskStatus.RUNNING)
    done = project_db.transition(task.id, TaskStatus.FAILED, fields={"error_type": "x"})
    assert done.status is TaskStatus.FAILED
    assert done.error_type == "x"
    with pytest.raises(InvalidTransitionError, match="--force"):
        project_db.transition(task.id, TaskStatus.MERGED)
    project_db.transition(task.id, TaskStatus.MERGED, force=True)


def test_failed_transition_does_not_write_partial_state(project_db: Database) -> None:
    task = project_db.insert_task(task_fields())
    with pytest.raises(sqlite3.OperationalError):
        project_db.transition(task.id, TaskStatus.RUNNING, fields={"no_such_column": 1})
    assert project_db.get_task(task.id).status is TaskStatus.PREPARING
    assert len(project_db.list_events(task.id)) == 2


def test_status_cannot_be_changed_without_transition(project_db: Database) -> None:
    task = project_db.insert_task(task_fields())
    with pytest.raises(ValueError, match="transition"):
        project_db.update_task(task.id, {"status": "completed"})


def test_events_and_identity_are_immutable(project_db: Database) -> None:
    task = project_db.insert_task(task_fields())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        project_db.conn.execute("DELETE FROM task_events")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        project_db.conn.execute("UPDATE task_events SET event = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        project_db.conn.execute("UPDATE tasks SET base_commit = 'b' WHERE id = ?", (task.id,))


def test_feedback_constraints(project_db: Database) -> None:
    task = project_db.insert_task(task_fields())
    base = {
        "task_id": task.id,
        "project_id": 1,
        "task_status": "preparing",
        "provider": "claude-code",
    }
    project_db.insert_feedback({**base, "rating": "good"})
    with pytest.raises(sqlite3.IntegrityError):
        project_db.insert_feedback({**base, "rating": "bad", "reason": "  "})
    with pytest.raises(sqlite3.IntegrityError):
        project_db.insert_feedback({**base, "rating": "meh"})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        project_db.conn.execute("DELETE FROM feedback")


def test_column_names_are_validated(project_db: Database) -> None:
    task = project_db.insert_task(task_fields())
    with pytest.raises(ValueError, match="invalid column"):
        project_db.update_task(task.id, {"error_type = 'x'; --": 1})
