"""Pure SDK-stream / text classification helpers for the project chat handler.

Side-effect-free functions extracted from ``core/project_chat.py``: classifying
SDK errors (shutdown-signal / retryable), pulling text deltas out of raw
streaming events, degrading ``AskUserQuestion`` to plain text, and detecting
numbered-option prompts. They depend on no handler state, so they can be unit
tested directly. ``project_chat`` re-imports these names, so existing call sites
and ``from telegram_bot.core.project_chat import _is_shutdown_signal_error``
imports keep working unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Error signatures for a claude subprocess that was killed by a service
# restart/shutdown signal rather than failing on its own. When systemd restarts
# ``ccc-telegram-bridge.service`` it delivers SIGTERM to the whole control group,
# so an in-flight claude child dies with exit 143 (=128+SIGTERM) or 137
# (=128+SIGKILL). That is not a real answer failure — the user message should be
# resent after the restart instead of surfacing a scary "❌ Error: ... exit 143".
_SHUTDOWN_SIGNAL_PATTERNS = (
    "exit code -15",
    "exit code -9",
    "exit code 143",
    "exit code 137",
    "exit status 143",
    "exit status 137",
    "exited with code 143",
    "exited with code 137",
    "exited with status 143",
    "exited with status 137",
    "sigterm",
    "sigkill",
    "stopped by signal",
    "terminated by signal",
)

# User-facing notice shown when an in-flight answer is interrupted by a bridge
# restart, in place of the raw subprocess error string.
RESTART_INTERRUPT_NOTICE = (
    "⏳ The bridge restarted while answering, so the last reply was interrupted. "
    "Please resend your message."
)


def _is_shutdown_signal_error(error_msg: str) -> bool:
    """True when ``error_msg`` reflects a claude subprocess killed by a
    service restart/shutdown signal (SIGTERM=143, SIGKILL=137).

    Used to (1) treat the error as retryable and (2) replace the raw error text
    with a friendly "restarted, please resend" notice rather than leaking the
    exit code to the user.
    """
    low = error_msg.lower()
    return any(pattern in low for pattern in _SHUTDOWN_SIGNAL_PATTERNS)


def _is_retryable_sdk_error(error: Exception) -> bool:
    """Check if the SDK error is transient and worth retrying.

    Returns True for network/timeout errors, False for permanent errors like
    configuration issues, permission errors, or code bugs.
    """
    error_type = type(error).__name__
    error_msg = str(error)

    # A subprocess killed by a restart/shutdown signal is always worth retrying.
    if _is_shutdown_signal_error(error_msg):
        return True

    # Permanent errors that should NOT be retried
    NON_RETRYABLE_PATTERNS = [
        "Invalid token",
        "Permission denied",
        "No such file",
        "Configuration error",
        "AttributeError",
        "KeyError",
        "ValueError",
        "TypeError",
    ]

    # Check if it's a permanent error
    if any(pattern in error_msg for pattern in NON_RETRYABLE_PATTERNS):
        return False

    # Retry all timeout and connection errors by default
    RETRYABLE_TYPES = [
        "TimeoutError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "BrokenPipeError",
        "OSError",
    ]

    if error_type in RETRYABLE_TYPES:
        return True

    # Also retry if error message contains common transient error patterns
    RETRYABLE_MSG_PATTERNS = [
        "timeout",
        "connection",
        "refused",
        "unreachable",
        "exit code -15",  # SIGTERM
        "exit code -9",  # SIGKILL
    ]

    return any(pattern in error_msg.lower() for pattern in RETRYABLE_MSG_PATTERNS)


def _format_ask_user_question(tool_input: dict):
    """Degrade AskUserQuestion to plain text for bot delivery.

    Returns (formatted_text: str, image_paths: list[str]).
    Extracts question text (which may include post content and image file paths
    as plain text) and numbered options so the bot's _extract_options can build
    an inline keyboard. Images are delivered separately via Read tool interception.
    """
    lines: list = []

    for q in tool_input.get("questions", []):
        question = q.get("question", "")
        if question:
            lines.append(question)

        options = q.get("options", [])

        if options:
            lines.append("")
        for i, opt in enumerate(options, 1):
            label = opt.get("label", "")
            desc = opt.get("description", "")
            lines.append(f"{i}. {label}" + (f" - {desc}" if desc else ""))

    return "\n".join(lines), []


def _extract_stream_text_delta(event: Any) -> Optional[str]:
    """Pull the incremental text from a raw Anthropic streaming event.

    Returns the delta text for ``content_block_delta`` events carrying a
    ``text_delta`` (the per-token text increments), else None. Tool-argument
    deltas (``input_json_delta``), block start/stop, and message-level events
    are ignored — only assistant-visible text drives the live draft.
    """
    if not isinstance(event, dict):
        return None
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) and text else None


def _detect_numbered_options(text: str) -> bool:
    """Detect if text contains numbered options format (e.g., "1. Option A").

    Returns True if the text appears to contain a question with numbered choices
    (at least two numbered items).
    """
    # Look for pattern: number followed by period and text, appearing multiple
    # times. Must have at least 2 numbered items to be considered options.
    pattern = r"^\s*\d+\.\s+.+$"
    matches = re.findall(pattern, text, re.MULTILINE)
    return len(matches) >= 2
