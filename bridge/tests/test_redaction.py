"""Canonical credential-redaction pattern coverage (bridge/utils/redaction).

This module is the single pattern set every persisting path shares, so a shape
missing here leaks everywhere at once. The fixtures below are synthesized, not
real credentials.
"""

from __future__ import annotations

import pytest

from telegram_bot.utils.redaction import (
    REDACTION_MARKER,
    contains_credential,
    redact_credentials,
)


# Fixtures are assembled at runtime rather than written as whole literals:
# a contiguous provider-shaped string in a committed file trips GitHub push
# protection and other scanners, which cannot tell a test vector from a live
# credential. Splitting the prefix from the body keeps the shape under test
# without ever storing something that reads as a real token.
_SLACK_BODY = "-123456789012-1234567890123-" + "a" * 24
SLACK_BOT = "xox" + "b" + _SLACK_BODY
SLACK_USER = "xox" + "p" + _SLACK_BODY
GOOGLE_KEY = "AIza" + "Sy" + "A" * 33
AWS_TEMPORARY = "ASIA" + "IOSFODNN7EXAMPLE"
AWS_LONG_LIVED = "AKIA" + "IOSFODNN7EXAMPLE"
SLACK_WEBHOOK = "https://hooks.slack.com/services/" + "T00000000/B00000000/" + "a" * 10

LEAKY = [
    ("bearer", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"),
    ("jwt", "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u"),
    ("telegram", "TELEGRAM_BOT_TOKEN=1234567890:" + "AAF-" + "b" * 28),
    ("github pat", "ghp_" + "c" * 36),
    ("github fine-grained", "github_pat_" + "d" * 36),
    ("openai-style", "sk-" + "e" * 36),
    ("aws long-lived", AWS_LONG_LIVED),
    ("aws temporary", AWS_TEMPORARY),
    ("slack bot", SLACK_BOT),
    ("slack user", SLACK_USER),
    ("slack webhook", SLACK_WEBHOOK),
    ("google api key", GOOGLE_KEY),
    ("keyed secret", "api_key=abcdefghijklmnop"),
    ("private key", "-----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY-----"),
]


@pytest.mark.parametrize("label,text", LEAKY, ids=[label for label, _ in LEAKY])
def test_credential_shapes_are_detected_and_redacted(label: str, text: str) -> None:
    assert contains_credential(text), f"{label} not detected"
    redacted = redact_credentials(text)
    assert REDACTION_MARKER in redacted, f"{label} not redacted"


@pytest.mark.parametrize("secret", [AWS_TEMPORARY, SLACK_BOT, GOOGLE_KEY])
def test_secret_body_does_not_survive_redaction(secret: str) -> None:
    assert secret not in redact_credentials(f"leaked value: {secret} <-")


ORDINARY = [
    "the deploy finished at 12:34 with 0 errors",
    "see docs/memory.md for the AIza prefix discussion",
    "commit 8c77426 fix(scripts): take the drift detector from the canonical ref",
    "AKIA is the long-lived AWS access-key prefix",
]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_prose_is_left_alone(text: str) -> None:
    assert not contains_credential(text)
    assert redact_credentials(text) == text


def test_non_string_input_is_returned_unchanged() -> None:
    assert redact_credentials(None) is None  # type: ignore[arg-type]
    assert contains_credential(None) is False  # type: ignore[arg-type]
