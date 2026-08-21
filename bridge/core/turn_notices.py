"""Pure turn-level notice/text composition extracted from ``bot.py`` (#896).

One pure-move slice of the ``_process_user_message_text`` hotspot (#348):
the user-facing strings a message turn can emit — the busy notice, the
session-start reason and banner, and the history-injection prompt — are
composed here as directly unit-tested functions, following the established
``core/`` helper pattern (``ui.py``, ``media.py``, ``sdk_text.py``). The
orchestrator keeps thin delegators so behavior and call sites are unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from telegram_bot.core.heartbeat import format_duration

_PROVIDER_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "crush": "Crush",
    "piri": "Piri",
}

HISTORY_SNIPPET_CHARS = 400
HISTORY_ROLE_LABELS = {"user": "사용자", "assistant": "어시스턴트"}


def busy_notice_text(busy_seconds: float) -> str:
    """The still-working notice shown when a turn is already in flight."""
    return (
        "⏳ Still working on the previous message "
        f"({format_duration(busy_seconds)} elapsed). "
        "I will handle this message after it finishes."
    )


def session_start_reason(
    *,
    new_session: bool,
    auto_new_session: bool,
    stale_session_id: Optional[str],
) -> str:
    """Why a fresh stream is starting; precedence: auto > /new > stale > none."""
    if auto_new_session:
        return "automatic reset"
    if new_session:
        return "/new requested"
    if stale_session_id:
        return "previous session was not resumable"
    return "no active session"


def session_start_notice_text(
    *,
    reason: str,
    model: Optional[str],
    provider: str = "claude",
    previous_session_id: Optional[str] = None,
) -> str:
    """The session-start banner.

    Banner model label: explicit /model choice first, then the operator
    display label, then the env-routed model (Claude/crush paths only), so
    the notice reflects the real backend instead of a bare "default".
    """
    provider_label = _PROVIDER_LABELS.get(provider, provider.title())
    display_model = model or os.environ.get("CCC_MODEL_LABEL", "").strip()
    if not display_model and provider == "claude":
        display_model = os.environ.get("ANTHROPIC_MODEL", "").strip()
    if not display_model and provider == "crush":
        display_model = os.environ.get("CCC_CRUSH_MODEL", "").strip()
    if not display_model:
        display_model = "default"
    lines = [
        f"◐ CCC session started ({reason}). Conversation history is on a fresh {provider_label} stream.",
        "Use /resume to browse and restore a previous session.",
        "",
        f"◆ Model: {display_model}",
        f"◆ Provider: {provider_label}",
        "◆ Context: new stream",
    ]
    if previous_session_id:
        lines.append(f"◆ Previous session: {previous_session_id[:8]}… (not resumed)")
    return "\n".join(lines)


def compose_history_injection(recent: List[Dict[str, Any]], text: str) -> str:
    """Prepend recent exchanges to ``text`` when a session could not resume.

    Each message is flattened to one ``label: snippet`` line (snippet capped
    at ``HISTORY_SNIPPET_CHARS`` with newlines collapsed) so the injected
    block stays bounded regardless of transcript size. With no messages the
    original text is returned unchanged.
    """
    if not recent:
        return text
    lines = []
    for message in recent:
        label = HISTORY_ROLE_LABELS.get(
            str(message.get("role")), HISTORY_ROLE_LABELS["assistant"]
        )
        snippet = str(message.get("content") or "")[:HISTORY_SNIPPET_CHARS].replace(
            "\n", " "
        )
        lines.append(f"{label}: {snippet}")
    history_block = "\n".join(lines)
    return (
        "[이전 대화 맥락 — 세션 전환으로 자동 주입됨]\n"
        f"{history_block}\n\n"
        f"[현재 메시지]\n{text}"
    )


__all__ = [
    "HISTORY_ROLE_LABELS",
    "HISTORY_SNIPPET_CHARS",
    "busy_notice_text",
    "compose_history_injection",
    "session_start_notice_text",
    "session_start_reason",
]
