"""One-line, human-readable reason for a failed 「한데」 turn (#2813).

The native bot prints a ``result`` frame's ``reason`` verbatim inside
``⚠️ 요청(#N)을 끝까지 확인하지 못했습니다(<reason>). …``. Before #2813 the
bridge sent the bare status (``error``), so a provider usage limit and a
Cloudflare block looked the same in the family room although the bridge had
logged the real cause. :func:`failure_reason` turns that cause into a short
Korean headline plus a sanitized snippet of the original text.

Pure and dependency-light: the reason travels to a room other people read, so
it is one line, control characters are gone, credential-looking spans are
masked (shared :mod:`telegram_bot.utils.redaction` set plus long opaque runs)
and the whole string is capped at :data:`MAX_REASON_CHARS`.
"""

from __future__ import annotations

import re
import unicodedata

from telegram_bot.core.sdk_text import is_edge_block_message
from telegram_bot.utils.redaction import REDACTION_MARKER, redact_credentials

MAX_REASON_CHARS = 160
ELLIPSIS = "…"

EDGE_BLOCK_REASON = "Claude API 일시 차단(Cloudflare 403)"
USAGE_LIMIT_HEADLINE = "사용 한도 초과"
AUTH_HEADLINE = "인증 실패"
RATE_LIMIT_HEADLINE = "요청 과다·일시 과부하"
OTHER_HEADLINE = "오류"

# Provider plan/usage caps: Claude Code ("You've reached your Fable limit",
# "You've hit your limit · resets 3pm", "Claude AI usage limit reached"),
# Codex ("You've hit your usage limit"), API quota/credit wording.
_USAGE_LIMIT_RE = re.compile(
    r"\b(?:reached|hit|exceeded)\s+your\b[^.\n]{0,60}?\blimit"
    r"|\busage[ _-]?limit"
    r"|\bexceeded\s+your\b[^.\n]{0,60}?\bquota"
    r"|\binsufficient[ _-]quota\b"
    r"|\bquota\s+exceeded\b"
    r"|\bcredit\s+balance\s+is\s+too\s+low\b"
    r"|\bout\s+of\s+(?:extra\s+)?usage\b",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"\bfailed\s+to\s+authenticate\b"
    r"|\binvalid[ _-]?(?:x-)?api[ _-]?key\b"
    r"|\bauthentication[ _]error\b"
    r"|\bunauthori[sz]ed\b"
    r"|\b401\b"
    r"|\boauth\s+token\s+(?:has\s+)?expired\b",
    re.IGNORECASE,
)
_RATE_LIMIT_RE = re.compile(
    r"\b429\b|\b529\b|\brate[ _-]?limit|\boverloaded|\btoo\s+many\s+requests\b",
    re.IGNORECASE,
)
# Opaque runs the shared set does not name (provider keys of unknown shape,
# hex digests, base64 blobs): 40+ token characters containing a digit. Words,
# URLs (``/`` and ``.`` break the run) and request ids stay readable.
_OPAQUE_RUN_RE = re.compile(r"(?<![A-Za-z0-9_+=-])(?=[A-Za-z_+=-]{0,80}[0-9])[A-Za-z0-9_+=-]{40,}")
_SPACES_RE = re.compile(r"\s+")
# Terminal colour/cursor sequences (CSI and OSC): dropped whole, so no ``[31m``
# residue is left once the ESC itself is gone.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")


def _printable(text: str) -> str:
    """ANSI sequences go; control and format characters (bidi overrides) become spaces."""

    text = _ANSI_RE.sub("", text)
    return "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in text)


def _mask(text: str) -> str:
    return _OPAQUE_RUN_RE.sub(REDACTION_MARKER, redact_credentials(text))


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = _SPACES_RE.sub(" ", _printable(line)).strip()
        if line:
            return line
    return ""


def _cap(text: str, limit: int = MAX_REASON_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(ELLIPSIS)].rstrip() + ELLIPSIS


def _with_snippet(headline: str, snippet: str) -> str:
    return _cap(f"{headline} — {snippet}" if snippet else headline)


def failure_reason(status: str | None, detail: str | None) -> str:
    """Reason for a turn that ended ``status`` with the cause text ``detail``.

    No usable ``detail`` keeps the old behaviour: the status itself
    (``"error"``/``"uncertain"``), or ``"error"`` when that is empty too.
    """

    fallback = str(status or "").strip() or "error"
    raw = str(detail or "")
    if not raw.strip():
        return fallback
    masked = _mask(raw)
    snippet = _first_line(masked)
    if not snippet:
        return fallback
    if is_edge_block_message(raw):
        return EDGE_BLOCK_REASON
    if _USAGE_LIMIT_RE.search(raw):
        return _with_snippet(USAGE_LIMIT_HEADLINE, snippet)
    if _AUTH_RE.search(raw):
        return _with_snippet(AUTH_HEADLINE, snippet)
    if _RATE_LIMIT_RE.search(raw):
        return _with_snippet(RATE_LIMIT_HEADLINE, snippet)
    return _with_snippet(OTHER_HEADLINE, snippet)


__all__ = ["MAX_REASON_CHARS", "failure_reason"]
