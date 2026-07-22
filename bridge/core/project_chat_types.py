"""Shared type aliases and dataclasses for telegram_bot.core.project_chat."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from telegram_bot.core.agent_runtime import ApprovalDecision, ApprovalRequestEvent


# Callback type: async (chat_id, user_id, tool_name, tool_input) -> bool | PermissionResult
PermissionCallback = Callable[[int, int, str, Dict[str, Any]], Awaitable]
# Explicit provider-neutral approval bridge. The generation is owned by
# ProjectChat and lets the Telegram side reject stale buttons fail-closed.
AgentApprovalCallback = Callable[
    [int, int, ApprovalRequestEvent, int], Awaitable[ApprovalDecision]
]
# Callback type: async () -> Any, sends typing action
TypingCallback = Callable[[], Awaitable[Any]]
# Callback type: async (text, message_id) -> message_id | None. When text is
# None the existing heartbeat message should be deleted/cleared.
StatusCallback = Callable[[Optional[str], Optional[int]], Awaitable[Optional[int]]]
# Callback for a completed assistant message that is known to be followed by
# more work in the same turn. Raising keeps the message on the final fallback.
InterimMessageCallback = Callable[[str], Awaitable[None]]


@dataclass
class AgentSessionEntry:
    """One provider-neutral agent session plus the parameters it was started
    with.

    The parameters travel with the session so a staleness check compares one
    object instead of six parallel dicts, and eviction is a single ``pop`` that
    cannot forget a field (the old parallel-dict form dropped only four of the
    six maps in one call site).
    """

    session: Any
    model: Optional[str] = None
    effort: Optional[str] = None
    approval_policy: Optional[str] = None
    approvals_reviewer: Optional[str] = None
    sandbox_policy: Optional[Mapping[str, Any]] = None


@dataclass
class ChatResponse:
    """Response from processing a message"""

    content: str
    success: bool = True
    error: Optional[str] = None
    session_id: Optional[str] = None
    has_options: bool = False
    streamed: bool = False  # Whether message was already sent via streaming


@dataclass
class _PendingRequest:
    user_id: int
    chat_id: int
    model: Optional[str]
    requested_session_id: Optional[str]
    permission_callback: Optional[PermissionCallback]
    typing_callback: Optional[TypingCallback]
    future: asyncio.Future
    status_callback: Optional[StatusCallback] = None
    # Persistent task-ledger record id (None when the ledger is unavailable).
    # Terminal transitions (completed/failed/canceled/timeout/interrupted) go
    # through the ledger so no status indicator can outlive its task record.
    task_id: Optional[str] = None
    # Usage-meter mode this request's spend is recorded under (#388/#364):
    # "interactive" for user turns (never budget-blocked), "autonomous" for
    # bridge-initiated turns such as the dead-session wakeup.
    usage_mode: str = "interactive"
    last_typing_at: float = 0.0
    started_at: float = 0.0
    heartbeat_last_update_at: float = 0.0
    heartbeat_message_id: Optional[int] = None
    # Wall-clock of the last runtime event seen for this request. Drives
    # heartbeat stall detection: when it goes silent for too long the request
    # is stuck (bridge restart / hung stream) and its heartbeat is removed
    # instead of ticking up forever. 0 until the first event; the stall check
    # falls back to started_at.
    last_event_at: float = 0.0
    # True until the runtime yields its first normalized event. This separates
    # a request blocked behind a leaked turn lock/provider admission from work
    # that the provider has actually started (#625).
    waiting_for_turn: bool = True
    # Wall-clock of the newest assistant TextBlock / ToolUseBlock (#411 C).
    # A terminal-event stall is only releasable when answer text is the latest
    # meaningful activity: last_text_at > last_tool_at means no tool started
    # after the text, so prolonged silence implies the terminal event vanished
    # rather than a long-running tool.
    last_text_at: float = 0.0
    last_tool_at: float = 0.0
    current_tool_label: Optional[str] = None
    last_visible_progress_at: float = 0.0
    awaiting_permission: bool = False
    heartbeat_forecast_loaded: bool = False
    # Duration samples the per-tick remaining-time ETA conditions on (loaded
    # once per request; the estimate itself is recomputed every heartbeat).
    heartbeat_forecast_samples: List[int] = field(default_factory=list)
    streaming_handler: Optional[Any] = None  # StreamingMessageHandler instance
