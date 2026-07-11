"""Shared type aliases and dataclasses for telegram_bot.core.project_chat."""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from claude_agent_sdk import ClaudeSDKClient


# Callback type: async (chat_id, user_id, tool_name, tool_input) -> bool | PermissionResult
PermissionCallback = Callable[[int, int, str, Dict[str, Any]], Awaitable]
# Callback type: async () -> Any, sends typing action
TypingCallback = Callable[[], Awaitable[Any]]
# Callback type: async (text, message_id) -> message_id | None. When text is
# None the existing heartbeat message should be deleted/cleared.
StatusCallback = Callable[[Optional[str], Optional[int]], Awaitable[Optional[int]]]


# Callback type: async (text, session_id) -> None. Delivers SDK messages that
# arrive on a live stream after its Telegram request queue has drained.
UnsolicitedCallback = Callable[[str, Optional[str]], Awaitable[None]]


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
    sent_session_id: str = "default"
    last_typing_at: float = 0.0
    started_at: float = 0.0
    heartbeat_last_update_at: float = 0.0
    heartbeat_message_id: Optional[int] = None
    # Wall-clock of the last SDK event the reader loop saw for this request.
    # Drives heartbeat stall detection: when it goes silent for too long the
    # request is stuck (bridge restart / hung stream) and its heartbeat is
    # removed instead of ticking up forever. 0 until the first event; the
    # stall check falls back to started_at.
    last_event_at: float = 0.0
    current_tool_label: Optional[str] = None
    last_visible_progress_at: float = 0.0
    awaiting_permission: bool = False
    heartbeat_forecast_loaded: bool = False
    # Duration samples the per-tick remaining-time ETA conditions on (loaded
    # once per request; the estimate itself is recomputed every heartbeat).
    heartbeat_forecast_samples: List[int] = field(default_factory=list)
    last_assistant_texts: List[str] = field(default_factory=list)
    synthetic_response: Optional[str] = None
    streaming_handler: Optional[Any] = None  # StreamingMessageHandler instance
    # Set once any partial StreamEvent text delta has driven the live draft, so
    # the complete AssistantMessage that follows is NOT re-fed to the streaming
    # handler (which would double the text). Stays False when partial streaming
    # is off / no deltas arrive, preserving the whole-block fallback path.
    streamed_via_partials: bool = False


@dataclass
class _UserStreamState:
    client: ClaudeSDKClient
    model: Optional[str]
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: Deque[_PendingRequest] = field(default_factory=deque)
    reader_task: Optional[asyncio.Task] = None
    typing_task: Optional[asyncio.Task] = None
    last_session_id: Optional[str] = None
    # Last SDK/stream error text + monotonic timestamp. Recorded by the reader so
    # a subsequent disconnect can surface the real cause (usage limit, auth,
    # network) instead of the opaque "Task has been terminated." notice.
    last_error: Optional[str] = None
    last_error_ts: float = 0.0
    # Route for SDK AssistantMessage/ResultMessage pairs that arrive after the
    # request FIFO has drained (for example background task notifications).
    unsolicited_callback: Optional[UnsolicitedCallback] = None
    unsolicited_assistant_texts: List[str] = field(default_factory=list)
    # Once a turn-bearing frame arrives without a pending Telegram request,
    # keep ownership through its terminal ResultMessage. A new Telegram request
    # may be enqueued between those frames and must not steal the autonomous
    # turn's result.
    unsolicited_inflight: bool = False
