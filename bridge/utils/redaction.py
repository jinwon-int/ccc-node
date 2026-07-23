"""Shared credential redaction (#645).

One canonical credential/secret pattern set for the whole bridge, promoted from
the memory-distill extractor (the most complete set — it is the only one that
covers the Telegram-bot-token shape and full ``BEGIN…END PRIVATE KEY`` blocks),
plus the ``AKIA`` AWS-key shape. Prefer importing from here over redefining a
per-module copy so redaction stays consistent everywhere.

``redact_credentials`` substitutes matches with a marker (for text that must be
persisted); ``contains_credential`` only reports presence (for warnings that must
never store the raw value).
"""

from __future__ import annotations

import re
from typing import Final

REDACTION_MARKER: Final = "[REDACTED_CREDENTIAL]"

CREDENTIAL_PATTERNS: Final = (
    re.compile(
        r"(?:\bauthorization\s*:\s*)?\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",
        re.IGNORECASE,
    ),
    re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_-]{20,}\b"),  # telegram bot token
    re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"secret|password)\s*[:=]\s*[^\s,;]{12,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
)


def contains_credential(value: str) -> bool:
    """True if the text carries a credential-like value (warning use)."""
    return isinstance(value, str) and any(
        pattern.search(value) for pattern in CREDENTIAL_PATTERNS
    )


def redact_credentials(value: str) -> str:
    """Replace credential-like spans with ``REDACTION_MARKER``."""
    if not isinstance(value, str):
        return value
    redacted = value
    for pattern in CREDENTIAL_PATTERNS:
        redacted = pattern.sub(REDACTION_MARKER, redacted)
    return redacted


__all__ = ["CREDENTIAL_PATTERNS", "REDACTION_MARKER", "contains_credential", "redact_credentials"]
