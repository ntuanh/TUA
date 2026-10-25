"""Best-effort secret redaction and structured (JSON lines) logging.

Redaction is pattern based. It catches common credential formats but cannot detect every
secret, so TUA also avoids logging environment variables, full prompts, and authentication
output in the first place.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tua.models import utc_now
from tua.paths import create_private_file, ensure_private_dir

REDACTED = "[REDACTED]"

# (name, pattern, keep_group): when keep_group is set, that group is preserved and the
# rest of the match is replaced, so "Bearer <token>" becomes "Bearer [REDACTED]".
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], int | None]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        None,
    ),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{30,})"), None),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), None),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}"), None),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), None),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), None),
    ("jwt", re.compile(r"\beyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,}"), None),
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@"), 1),
    ("bearer_token", re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/]{16,}=*"), 1),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
            r"secret[_-]?key|password|passwd)['\"]?\s*[:=]\s*)['\"]?[^\s'\"]{6,}['\"]?"
        ),
        1,
    ),
]

# Formats precise enough to refuse sending text to a provider when they appear.
HIGH_CONFIDENCE_KINDS = frozenset(
    {
        "private_key",
        "github_token",
        "anthropic_key",
        "openai_key",
        "aws_access_key_id",
        "slack_token",
        "google_api_key",
        "jwt",
        "url_credentials",
    }
)


def _keep_prefix(group: int) -> Callable[[re.Match[str]], str]:
    def replace(match: re.Match[str]) -> str:
        return match.group(group) + REDACTED

    return replace


def redact(text: str) -> str:
    """Replace known secret formats in ``text`` with a placeholder."""
    for _name, pattern, keep in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED if keep is None else _keep_prefix(keep), text)
    return text


def detect_secrets(text: str, *, high_confidence_only: bool = True) -> list[str]:
    """Return the kinds of secret formats found in ``text`` (never the values)."""
    kinds = []
    for name, pattern, _keep in _SECRET_PATTERNS:
        if high_confidence_only and name not in HIGH_CONFIDENCE_KINDS:
            continue
        if pattern.search(text):
            kinds.append(name)
    return kinds


def excerpt(text: str | None, limit: int = 2000) -> str | None:
    """Redacted, length-limited excerpt for display or storage."""
    if text is None:
        return None
    cleaned = redact(text).strip()
    if len(cleaned) > limit:
        return cleaned[:limit] + " …[truncated]"
    return cleaned


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": utc_now(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str, sort_keys=True))


LOGGER_NAME = "tua"
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_BACKUPS = 3


def setup_logging(log_dir: Path) -> None:
    """Send TUA's structured log to a size-bounded, private file in the state directory."""
    logger = logging.getLogger(LOGGER_NAME)
    if any(getattr(h, "_tua_handler", False) for h in logger.handlers):
        return
    ensure_private_dir(log_dir)
    log_file = log_dir / "tua.log"
    create_private_file(log_file)
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
    )
    handler._tua_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_event(event: str, **fields: Any) -> None:
    """Write one structured event. Never pass environment variables or credentials here."""
    logging.getLogger(LOGGER_NAME).info(event, extra={"fields": fields})
