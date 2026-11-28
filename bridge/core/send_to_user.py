"""``send_to_user``: verbatim mid-turn delivery as a model tool (#2942).

On the current Claude generation, text the model writes between tool calls
comes back as a progress-update ``thinking`` block once it is longer than a
sentence or two, and that block carries a short summary, not the text (#2161
forwards it as an interim bubble). A body the owner must receive exactly as
written — text to paste elsewhere, a table, exact values — therefore arrived
as two or three summary lines, and resending did not help. The original text
is not returned by the API under any ``thinking.display`` value.

Tool *inputs* are never summarized. This module is the frontend-neutral half
of a one-tool in-process MCP server: it validates the message, hands it to the
frontend's ``send`` callable (which posts it into the conversation of the turn
being served, through the same path an interim bubble takes) and returns the
frontend's outcome, so the model learns whether — and how many bytes — went
out. There is no room or recipient argument: the destination is always the
conversation the final answer goes to, which is why the tool needs no
per-call approval.

Outcome shape (JSON text in the MCP result, ``is_error`` unless ``ok`` is
exactly ``True``)::

    {"ok": true, "bytes": 1621}
    {"ok": false, "error": "too_long", "bytes": 9100, "max_bytes": 8192}

Error codes: ``bad_arguments``, ``empty_message``, ``too_long``,
``no_active_turn``, ``interim_frames_off``, ``interim_limit``, ``send_failed``.
A message is never truncated and never dropped without an error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

SERVER = "bridge"
TOOL = "send_to_user"
# One interim bubble: the hande ``interim`` frame bound and the bot's reply limit.
DEFAULT_MAX_BYTES = 8192
MAX_RESULT_TEXT_BYTES = 4096
# Session-record field naming the provider session the tool was declared for.
SESSION_FIELD = "send_to_user_session"

DESCRIPTION = (
    "Send a message to the user in the current conversation right now, exactly as written. "
    "Use it only for content the user must receive verbatim before the turn ends: text they "
    "will copy and paste, exact values, a finished part of the deliverable. Text you write "
    "between tool calls may reach the user only as a short summary; this tool's message is "
    "never summarized. Do not use it for routine progress narration, and do not repeat the "
    "same content in your final answer. One message per call, about 8 KB of UTF-8 at most: "
    "split longer content yourself and number the parts. The result says whether the message "
    "was delivered and how many bytes went out; on an error nothing was sent."
)

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

Send = Callable[[str], Awaitable[Mapping[str, Any]]]


def qualified() -> str:
    return f"mcp__{SERVER}__{TOOL}"


def allowed_tool_names() -> list[str]:
    """SDK allow-list entry: the tool runs without a per-call approval."""

    return [qualified()]


def clean_text(text: str) -> str:
    """The transport-safe form of ``text``: ANSI escapes and control characters removed.

    The same two steps every delivered answer goes through; nothing else is
    changed — no stripping, no reflow.
    """

    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    return "".join(char for char in cleaned if ord(char) >= 32 or char in "\n\r\t")


def clean_message(arguments: Any, *, max_bytes: int) -> tuple[str | None, dict[str, Any] | None]:
    """``(message, None)`` for a sendable call, else ``(None, error outcome)``."""

    if not isinstance(arguments, Mapping) or set(arguments) != {"message"}:
        return None, {"ok": False, "error": "bad_arguments"}
    raw = arguments["message"]
    if not isinstance(raw, str):
        return None, {"ok": False, "error": "bad_arguments"}
    message = clean_text(raw)
    if not message.strip():
        return None, {"ok": False, "error": "empty_message"}
    size = len(message.encode("utf-8"))
    if size > max_bytes:
        return None, {"ok": False, "error": "too_long", "bytes": size, "max_bytes": max_bytes}
    return message, None


def outcome_result(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """MCP tool result for one outcome (the steward tools' shape)."""

    text = json.dumps(dict(outcome), sort_keys=True, ensure_ascii=True, separators=(", ", ": "))
    if len(text.encode("utf-8")) > MAX_RESULT_TEXT_BYTES:
        outcome = {"ok": False, "error": "send_failed"}
        text = json.dumps(outcome, sort_keys=True, ensure_ascii=True, separators=(", ", ": "))
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if outcome.get("ok") is not True:
        result["is_error"] = True
    return result


def make_handler(
    send: Send, max_bytes: Callable[[], int]
) -> Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]:
    """The tool body: validate, send through the frontend, report the outcome."""

    async def handler(arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            limit = int(max_bytes())
        except Exception:
            limit = DEFAULT_MAX_BYTES
        message, error = clean_message(arguments, max_bytes=limit)
        if message is None:
            return outcome_result(error or {"ok": False, "error": "bad_arguments"})
        try:
            outcome = await send(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the bridge never lets a tool crash the turn
            logger.warning("send_to_user failed: %s", type(exc).__name__)
            return outcome_result({"ok": False, "error": "send_failed"})
        if not isinstance(outcome, Mapping):
            return outcome_result({"ok": False, "error": "send_failed"})
        return outcome_result(outcome)

    return handler


def build_send_to_user_server(send: Send, *, max_bytes: Callable[[], int]) -> Any:
    """In-process Claude Agent SDK MCP server with the one tool.

    ``send(message)`` posts the cleaned message into the conversation of the
    turn being served and returns the outcome mapping. Returns ``None`` when
    the SDK is not importable (the frontend then runs without the tool).
    """

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool as sdk_tool
    except ImportError:
        logger.warning("send_to_user tool disabled: claude_agent_sdk not importable")
        return None

    handler = make_handler(send, max_bytes)
    tools = [sdk_tool(TOOL, DESCRIPTION, {"message": str})(handler)]
    return create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=tools)


def merge_request_extras(extras: dict[str, Any], server: Any) -> None:
    """Add the server and its allow-list entry to one turn's request extras, in place."""

    servers = dict(extras.get("mcp_servers") or {})
    servers[SERVER] = server
    extras["mcp_servers"] = servers
    allowed = [name for name in (extras.get("mcp_allowed_tools") or ()) if name != qualified()]
    extras["mcp_allowed_tools"] = [*allowed, qualified()]


def offered_for_session(session: Mapping[str, Any], session_id: str | None) -> bool:
    """Whether a turn on ``session_id`` may declare the tool.

    A tool must be in the tool list from the first request of a provider
    session: adding it to a session already under way edits the conversation
    prefix and invalidates its earlier thinking blocks. So the tool is declared
    on a fresh session (no id yet) and on the session it was declared for —
    never slipped into an older one.
    """

    if not session_id:
        return True
    return session.get(SESSION_FIELD) == session_id


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DESCRIPTION",
    "SERVER",
    "SESSION_FIELD",
    "TOOL",
    "allowed_tool_names",
    "build_send_to_user_server",
    "clean_message",
    "clean_text",
    "make_handler",
    "merge_request_extras",
    "offered_for_session",
    "outcome_result",
    "qualified",
]
