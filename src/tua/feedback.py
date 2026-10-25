"""Task-bound feedback (``tua good`` / ``tua bad``).

Feedback is an append-only record bound to the exact task and its skill/model
fingerprints. It never changes the task's lifecycle state, never edits skill files, and
never claims a root cause; use ``tua eval`` to test whether a skill change helps.
"""

from __future__ import annotations

from tua.db import Database
from tua.errors import SecretDetectedError, TuaError
from tua.logging_utils import detect_secrets, log_event
from tua.models import ACTIVE_STATES, Feedback
from tua.tasks import TaskService

MAX_TEXT = 2000


def _clean(text: str | None, what: str) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_TEXT:
        raise TuaError(f"{what} is longer than {MAX_TEXT} characters")
    if detect_secrets(text):
        raise SecretDetectedError(f"{what} appears to contain a secret; not stored")
    return text


def record_feedback(
    db: Database,
    tasks: TaskService,
    task_id: str,
    rating: str,
    *,
    reason: str | None = None,
    note: str | None = None,
) -> Feedback:
    if rating not in ("good", "bad"):
        raise TuaError(f"invalid rating {rating!r}")
    task = tasks.get(task_id)
    if task.status in ACTIVE_STATES:
        raise TuaError(f"task {task.id} is still {task.status.value}; rate it once it finishes")
    reason = _clean(reason, "reason")
    note = _clean(note, "note")
    if rating == "bad" and reason is None:
        raise TuaError("bad feedback needs --reason")
    feedback = db.insert_feedback(
        {
            "task_id": task.id,
            "project_id": task.project_id,
            "rating": rating,
            "reason": reason,
            "note": note,
            "task_status": task.status.value,
            "source_skill_hash": task.source_skill_hash,
            "compiled_skill_hash": task.compiled_skill_hash,
            "provider": task.provider,
            "model_alias": task.model_alias,
            "actual_models": ",".join(task.actual_models) if task.actual_models else None,
            "cli_version": task.cli_version,
        }
    )
    log_event("feedback.recorded", task_id=task.id, rating=rating, feedback_id=feedback.id)
    return feedback
