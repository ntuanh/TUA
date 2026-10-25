"""Fake secrets are assembled at runtime so this file contains no literal credential."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tua.logging_utils import (
    LOGGER_NAME,
    REDACTED,
    detect_secrets,
    excerpt,
    log_event,
    redact,
    setup_logging,
)

GH = "gh" + "p_" + "A1b2C3d4" * 5
GH_PAT = "github" + "_pat_" + "x" * 40
ANT = "sk-" + "ant-" + "api03-" + "Z" * 30
AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
SLACK = "xo" + "xb-" + "1234567890-abcdef"
KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END " + "OPENSSH PRIVATE KEY-----"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + ".abcdefghijklmnop"


@pytest.mark.parametrize("secret", [GH, GH_PAT, ANT, AWS, SLACK, KEY, JWT])
def test_known_formats_are_redacted_and_detected(secret: str) -> None:
    text = f"before {secret} after"
    redacted = redact(text)
    assert secret not in redacted
    assert REDACTED in redacted
    assert redacted.startswith("before ")
    assert detect_secrets(text)


PASSWORD_KEY = "pass" + "word"
API_KEY = "api" + "_key"
URL_WITH_CREDENTIALS = "https://" + "user:" + "hunter2hunter2" + "@example.invalid/repo.git"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Authorization: Bearer " + "abcdefghijklmnopqrstuvwxyz123", "Bearer "),
        (URL_WITH_CREDENTIALS, "https://"),
        (PASSWORD_KEY + "=" + "correcthorsebattery", PASSWORD_KEY + "="),
        ('{"' + API_KEY + '": "' + "abcdefgh12345" + '"}', '"' + API_KEY + '": '),
    ],
)
def test_context_preserving_redaction(text: str, kept: str) -> None:
    redacted = redact(text)
    assert kept in redacted
    assert REDACTED in redacted
    assert "hunter2" not in redacted
    assert "correcthorse" not in redacted


def test_plain_text_is_untouched() -> None:
    text = "Add a password field to the login form and write tests."
    assert redact(text) == text
    assert detect_secrets(text) == []


def test_generic_assignments_do_not_block_prompts() -> None:
    # Only high-confidence formats are used to refuse input.
    assignment = PASSWORD_KEY + "=" + "abcdefgh"
    assert detect_secrets(assignment) == []
    assert detect_secrets(assignment, high_confidence_only=False) == ["secret_assignment"]


def test_excerpt_truncates_and_redacts() -> None:
    assert excerpt(None) is None
    long = "x" * 3000 + GH
    result = excerpt(long, 100)
    assert result is not None
    assert result.endswith("[truncated]")
    assert len(result) < 130
    assert excerpt(f"t {GH}") == f"t {REDACTED}"


def test_structured_log_is_redacted_and_private(tmp_path: Path) -> None:
    setup_logging(tmp_path / "logs")
    try:
        log_event("probe", detail=f"token {GH}", task_id="t1")
        for handler in logging.getLogger(LOGGER_NAME).handlers:
            handler.flush()
        log_file = tmp_path / "logs" / "tua.log"
        content = log_file.read_text()
        assert GH not in content
        record = json.loads(content.strip().splitlines()[-1])
        assert record["event"] == "probe"
        assert record["task_id"] == "t1"
        assert (log_file.stat().st_mode & 0o077) == 0
    finally:
        logger = logging.getLogger(LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
