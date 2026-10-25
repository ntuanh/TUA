"""SQLite persistence: schema migrations, transactions, and all SQL used by TUA.

The schema version lives in ``PRAGMA user_version``. Migrations only ever add; TUA never
drops or recreates a database. A database created by a newer TUA, a corrupt file, or an
unrelated SQLite file is refused with an actionable error instead of being overwritten.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from tua.errors import ProjectNotFoundError, StateError, TaskNotFoundError
from tua.models import (
    Feedback,
    Project,
    Task,
    TaskEvent,
    TaskStatus,
    check_transition,
    utc_now,
)
from tua.paths import create_private_file, ensure_private_dir

SCHEMA_VERSION = 4

_STATUSES = ", ".join(f"'{s.value}'" for s in TaskStatus)
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _columns(fields: Mapping[str, Any]) -> list[str]:
    """Column names are internal, but validate them anyway before building SQL."""
    names = list(fields)
    for name in names:
        if not _IDENTIFIER.match(name):
            raise ValueError(f"invalid column name {name!r}")
    return names


_MIGRATION_1 = [
    """
    CREATE TABLE projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        root_path TEXT NOT NULL UNIQUE,
        config_path TEXT NOT NULL,
        config_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        status TEXT NOT NULL CHECK (status IN ({_STATUSES})),
        prompt TEXT NOT NULL,
        prompt_sha256 TEXT NOT NULL,
        source_repo TEXT NOT NULL,
        target_branch TEXT NOT NULL,
        base_commit TEXT NOT NULL,
        task_branch TEXT NOT NULL UNIQUE,
        worktree_path TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL,
        model_alias TEXT,
        actual_models TEXT,
        cli_path TEXT,
        cli_version TEXT,
        permission_profile TEXT NOT NULL,
        settings_path TEXT,
        config_fingerprint TEXT NOT NULL,
        source_skill_hash TEXT,
        compiled_skill_hash TEXT,
        compiled_skill_path TEXT,
        timeout_seconds INTEGER NOT NULL,
        max_turns INTEGER,
        eval_run_id TEXT REFERENCES eval_runs(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        duration_seconds REAL,
        exit_code INTEGER,
        error_type TEXT,
        error_message TEXT,
        stdout_path TEXT,
        stderr_path TEXT,
        result_excerpt TEXT,
        session_id TEXT,
        num_turns INTEGER,
        cost_usd REAL,
        usage_json TEXT,
        permission_denials INTEGER,
        runner_pid INTEGER,
        runner_host TEXT,
        runner_start TEXT,
        provider_pid INTEGER,
        provider_start TEXT,
        merged_commit TEXT,
        cleanup_status TEXT CHECK (cleanup_status IS NULL OR cleanup_status IN ('done', 'failed')),
        cleanup_error TEXT
    )
    """,
    "CREATE INDEX idx_tasks_project_created ON tasks(project_id, created_at)",
    "CREATE INDEX idx_tasks_status ON tasks(status)",
    "CREATE INDEX idx_tasks_eval_run ON tasks(eval_run_id)",
    """
    CREATE TRIGGER tasks_identity_immutable
    BEFORE UPDATE OF id, project_id, prompt, prompt_sha256, source_repo, target_branch,
        base_commit, task_branch, worktree_path, provider, created_at ON tasks
    BEGIN
        SELECT RAISE(ABORT, 'task identity columns are immutable');
    END
    """,
    """
    CREATE TABLE task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        event TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        created_at TEXT NOT NULL,
        metadata TEXT
    )
    """,
    "CREATE INDEX idx_task_events_task ON task_events(task_id, id)",
    """
    CREATE TRIGGER task_events_append_only_update BEFORE UPDATE ON task_events
    BEGIN SELECT RAISE(ABORT, 'task events are append-only'); END
    """,
    """
    CREATE TRIGGER task_events_append_only_delete BEFORE DELETE ON task_events
    BEGIN SELECT RAISE(ABORT, 'task events are append-only'); END
    """,
    """
    CREATE TABLE feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        project_id INTEGER NOT NULL REFERENCES projects(id),
        rating TEXT NOT NULL CHECK (rating IN ('good', 'bad')),
        reason TEXT,
        note TEXT,
        task_status TEXT NOT NULL,
        source_skill_hash TEXT,
        compiled_skill_hash TEXT,
        provider TEXT NOT NULL,
        model_alias TEXT,
        actual_models TEXT,
        cli_version TEXT,
        created_at TEXT NOT NULL,
        CHECK (rating = 'good' OR length(trim(coalesce(reason, ''))) > 0)
    )
    """,
    "CREATE INDEX idx_feedback_task ON feedback(task_id)",
    """
    CREATE TRIGGER feedback_append_only_update BEFORE UPDATE ON feedback
    BEGIN SELECT RAISE(ABORT, 'feedback is append-only'); END
    """,
    """
    CREATE TRIGGER feedback_append_only_delete BEFORE DELETE ON feedback
    BEGIN SELECT RAISE(ABORT, 'feedback is append-only'); END
    """,
    """
    CREATE TABLE eval_runs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('baseline', 'candidate')),
        project_id INTEGER NOT NULL REFERENCES projects(id),
        suite_id TEXT NOT NULL,
        suite_fingerprint TEXT NOT NULL,
        runner_version INTEGER NOT NULL,
        eval_file TEXT NOT NULL,
        baseline_id TEXT REFERENCES eval_baselines(id),
        source_skill_hash TEXT NOT NULL,
        compiled_skill_hash TEXT NOT NULL,
        provider TEXT NOT NULL,
        model_alias TEXT,
        cli_version TEXT,
        max_runs INTEGER NOT NULL,
        max_total_seconds INTEGER NOT NULL,
        runs_planned INTEGER NOT NULL,
        runs_started INTEGER NOT NULL DEFAULT 0,
        runs_skipped INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL
            CHECK (status IN ('running', 'complete', 'incomplete', 'cancelled', 'failed')),
        started_at TEXT NOT NULL,
        ended_at TEXT,
        elapsed_seconds REAL,
        summary_json TEXT,
        artifact_dir TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_eval_runs_suite ON eval_runs(project_id, suite_id, started_at)",
    """
    CREATE TABLE eval_case_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        eval_run_id TEXT NOT NULL REFERENCES eval_runs(id),
        case_id TEXT NOT NULL,
        run_index INTEGER NOT NULL,
        task_id TEXT REFERENCES tasks(id),
        repo_commit TEXT NOT NULL,
        status TEXT NOT NULL,
        task_status TEXT,
        checks_passed INTEGER,
        checks_total INTEGER,
        metric_name TEXT,
        metric_value REAL,
        duration_seconds REAL,
        detail TEXT,
        artifact_dir TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (eval_run_id, case_id, run_index)
    )
    """,
    """
    CREATE TABLE eval_baselines (
        id TEXT PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        suite_id TEXT NOT NULL,
        eval_run_id TEXT NOT NULL UNIQUE REFERENCES eval_runs(id),
        suite_fingerprint TEXT NOT NULL,
        repo_commit TEXT NOT NULL,
        source_skill_hash TEXT NOT NULL,
        compiled_skill_hash TEXT NOT NULL,
        manifest_path TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        superseded_at TEXT,
        superseded_by TEXT REFERENCES eval_baselines(id)
    )
    """,
    """
    CREATE UNIQUE INDEX idx_eval_baselines_active
    ON eval_baselines(project_id, suite_id) WHERE superseded_at IS NULL
    """,
    """
    CREATE TRIGGER eval_baselines_immutable
    BEFORE UPDATE OF id, project_id, suite_id, eval_run_id, suite_fingerprint, repo_commit,
        source_skill_hash, compiled_skill_hash, manifest_path, manifest_sha256, created_at
    ON eval_baselines
    BEGIN SELECT RAISE(ABORT, 'eval baselines are immutable'); END
    """,
    """
    CREATE TRIGGER eval_baselines_no_delete BEFORE DELETE ON eval_baselines
    BEGIN SELECT RAISE(ABORT, 'eval baselines are immutable'); END
    """,
]

# v2: a task can be a read-only review of another task (`tua review`).
_MIGRATION_2 = [
    "ALTER TABLE tasks ADD COLUMN review_of TEXT REFERENCES tasks(id)",
    "CREATE INDEX idx_tasks_review_of ON tasks(review_of)",
]

# v3: tasks record the reasoning effort they ran with.
_MIGRATION_3 = ["ALTER TABLE tasks ADD COLUMN effort TEXT"]

# v4: where the user started TUA (read-only context folder and project subfolder).
_MIGRATION_4 = [
    "ALTER TABLE tasks ADD COLUMN context_dir TEXT",
    "ALTER TABLE tasks ADD COLUMN start_dir TEXT",
]

MIGRATIONS: dict[int, list[str]] = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
}

_CORRUPT_HINT = (
    "Move the file aside (for example `mv {path} {path}.broken`) and run the command again "
    "to start with an empty database. TUA never deletes it for you."
)


class Database:
    """A connection to TUA's state database. Use :meth:`open` to create one."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self.conn = conn
        self.path = path

    @classmethod
    def open(cls, path: Path) -> Database:
        ensure_private_dir(path.parent)
        create_private_file(path)
        try:
            conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
        except sqlite3.Error as exc:
            raise StateError(f"cannot open state database {path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        db = cls(conn, path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            db._migrate()
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise StateError(
                f"state database {path} is unreadable or corrupt: {exc}",
                hint=_CORRUPT_HINT.format(path=path),
            ) from exc
        except StateError:
            conn.close()
            raise
        return db

    def close(self) -> None:
        self.conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run statements atomically; any exception rolls everything back."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ----------------------------------------------------------------------------- schema

    def schema_version(self) -> int:
        row = self.conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def _migrate(self) -> None:
        version = self.schema_version()
        if version > SCHEMA_VERSION:
            raise StateError(
                f"state database {self.path} uses schema version {version}, but this TUA "
                f"supports up to {SCHEMA_VERSION}",
                hint="Upgrade TUA, or point XDG_STATE_HOME at a different directory.",
            )
        if version == 0:
            tables = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if tables:
                raise StateError(
                    f"{self.path} contains tables but no TUA schema version; refusing to "
                    "modify an unrecognized database",
                    hint=_CORRUPT_HINT.format(path=self.path),
                )
        for target in range(version + 1, SCHEMA_VERSION + 1):
            with self.transaction() as conn:
                for statement in MIGRATIONS[target]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {target}")

    def quick_check(self) -> str:
        row = self.conn.execute("PRAGMA quick_check").fetchone()
        return str(row[0])

    # ---------------------------------------------------------------------------- projects

    def upsert_project(
        self, *, name: str, root_path: str, config_path: str, config_fingerprint: str
    ) -> Project:
        now = utc_now()
        with self.transaction() as conn:
            by_name = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
            by_root = conn.execute(
                "SELECT * FROM projects WHERE root_path = ?", (root_path,)
            ).fetchone()
            if by_name is not None and by_name["root_path"] != root_path:
                raise StateError(
                    f"project name '{name}' is already registered for {by_name['root_path']}",
                    hint="Choose a different --name.",
                )
            if by_root is not None and by_root["name"] != name:
                raise StateError(
                    f"{root_path} is already registered as project '{by_root['name']}'",
                    hint=f"Use --name {by_root['name']} to re-register it.",
                )
            if by_name is None:
                conn.execute(
                    "INSERT INTO projects (name, root_path, config_path, config_fingerprint,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, root_path, config_path, config_fingerprint, now, now),
                )
            else:
                conn.execute(
                    "UPDATE projects SET config_path = ?, config_fingerprint = ?, updated_at = ?"
                    " WHERE id = ?",
                    (config_path, config_fingerprint, now, by_name["id"]),
                )
        return self.get_project(name)

    def get_project(self, name: str) -> Project:
        row = self.conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ProjectNotFoundError(
                f"no project named '{name}'",
                hint="List projects with `tua project list` or register one with "
                "`tua project init <PATH> --name <NAME>`.",
            )
        return Project.from_row(row)

    def find_project_by_root(self, root_path: str) -> Project | None:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE root_path = ?", (root_path,)
        ).fetchone()
        return Project.from_row(row) if row is not None else None

    def get_project_by_id(self, project_id: int) -> Project:
        row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise ProjectNotFoundError(f"no project with id {project_id}")
        return Project.from_row(row)

    def list_projects(self) -> list[Project]:
        rows = self.conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [Project.from_row(r) for r in rows]

    # ------------------------------------------------------------------------------- tasks

    def insert_task(self, fields: Mapping[str, Any]) -> Task:
        """Insert a task in ``created`` state and move it to ``preparing`` atomically."""
        now = utc_now()
        values = dict(fields)
        values.update(status=TaskStatus.CREATED.value, created_at=now, updated_at=now)
        columns = ", ".join(_columns(values))
        placeholders = ", ".join("?" for _ in values)
        with self.transaction() as conn:
            conn.execute(
                f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",  # noqa: S608
                tuple(values.values()),
            )
            self._add_event(conn, values["id"], "created", None, TaskStatus.CREATED, None)
            self._apply_transition(
                conn, values["id"], TaskStatus.CREATED, TaskStatus.PREPARING, {}, None
            )
        return self.get_task(values["id"])

    def get_task(self, task_id: str) -> Task:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError(
                f"no task with id '{task_id}'", hint="List tasks with `tua status`."
            )
        return Task.from_row(row)

    def list_tasks(
        self,
        *,
        project_id: int | None = None,
        statuses: Sequence[TaskStatus] | None = None,
        limit: int | None = None,
    ) -> list[Task]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if statuses:
            clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(s.value for s in statuses)
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [Task.from_row(r) for r in self.conn.execute(sql, params).fetchall()]

    def list_reviews(self, task_id: str) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE review_of = ? ORDER BY created_at, id", (task_id,)
        ).fetchall()
        return [Task.from_row(r) for r in rows]

    def update_task(self, task_id: str, fields: Mapping[str, Any]) -> Task:
        """Update non-status task fields (status changes must use :meth:`transition`)."""
        if "status" in fields:
            raise ValueError("use transition() to change a task's status")
        with self.transaction() as conn:
            self._update_fields(conn, task_id, fields)
        return self.get_task(task_id)

    def transition(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        fields: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> Task:
        """Validate and apply a state change plus its event in one transaction."""
        with self.transaction() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"no task with id '{task_id}'")
            current = TaskStatus(row["status"])
            check_transition(current, new_status, force=force)
            self._apply_transition(conn, task_id, current, new_status, fields or {}, metadata)
        return self.get_task(task_id)

    def add_event(
        self, task_id: str, event: str, metadata: Mapping[str, Any] | None = None
    ) -> None:
        """Record a non-transition event (for example a refused merge or failed cleanup)."""
        with self.transaction() as conn:
            self._add_event(conn, task_id, event, None, None, metadata)

    def list_events(self, task_id: str) -> list[TaskEvent]:
        rows = self.conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [TaskEvent.from_row(r) for r in rows]

    def _apply_transition(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        current: TaskStatus,
        new_status: TaskStatus,
        fields: Mapping[str, Any],
        metadata: Mapping[str, Any] | None,
    ) -> None:
        updates = dict(fields)
        updates["status"] = new_status.value
        assignments = ", ".join(f"{column} = ?" for column in _columns(updates))
        cursor = conn.execute(
            f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ? AND status = ?",  # noqa: S608
            (*updates.values(), utc_now(), task_id, current.value),
        )
        if cursor.rowcount != 1:
            raise StateError(f"task {task_id} changed concurrently; transition aborted")
        self._add_event(conn, task_id, "transition", current, new_status, metadata)

    @staticmethod
    def _update_fields(conn: sqlite3.Connection, task_id: str, fields: Mapping[str, Any]) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in _columns(fields))
        cursor = conn.execute(
            f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608
            (*fields.values(), utc_now(), task_id),
        )
        if cursor.rowcount != 1:
            raise TaskNotFoundError(f"no task with id '{task_id}'")

    @staticmethod
    def _add_event(
        conn: sqlite3.Connection,
        task_id: str,
        event: str,
        from_status: TaskStatus | None,
        to_status: TaskStatus | None,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        conn.execute(
            "INSERT INTO task_events (task_id, event, from_status, to_status, created_at,"
            " metadata) VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                event,
                from_status.value if from_status else None,
                to_status.value if to_status else None,
                utc_now(),
                json.dumps(dict(metadata), sort_keys=True, default=str) if metadata else None,
            ),
        )

    # ---------------------------------------------------------------------------- feedback

    def insert_feedback(self, fields: Mapping[str, Any]) -> Feedback:
        values = dict(fields)
        values["created_at"] = utc_now()
        columns = ", ".join(_columns(values))
        placeholders = ", ".join("?" for _ in values)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"INSERT INTO feedback ({columns}) VALUES ({placeholders})",  # noqa: S608
                tuple(values.values()),
            )
            feedback_id = cursor.lastrowid
        row = self.conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
        return Feedback.from_row(row)

    def list_feedback(self, task_id: str) -> list[Feedback]:
        rows = self.conn.execute(
            "SELECT * FROM feedback WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [Feedback.from_row(r) for r in rows]

    # ------------------------------------------------------------------------- evaluations

    def insert_row(self, conn: sqlite3.Connection, table: str, fields: Mapping[str, Any]) -> None:
        """Insert into one of the eval tables inside an open transaction."""
        if table not in {"eval_runs", "eval_case_runs", "eval_baselines"}:
            raise ValueError(f"unexpected table {table}")
        columns = ", ".join(_columns(fields))
        placeholders = ", ".join("?" for _ in fields)
        conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",  # noqa: S608
            tuple(fields.values()),
        )

    def update_eval_run(self, run_id: str, fields: Mapping[str, Any]) -> None:
        assignments = ", ".join(f"{column} = ?" for column in _columns(fields))
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE eval_runs SET {assignments} WHERE id = ?",  # noqa: S608
                (*fields.values(), run_id),
            )

    def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(sql, params).fetchone()
        return row
