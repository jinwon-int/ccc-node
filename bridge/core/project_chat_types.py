"""Shared type aliases and dataclasses for telegram_bot.core.project_chat."""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from claude_agent_sdk import ClaudeSDKClient
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
    last_assistant_texts: List[str] = field(default_factory=list)
    synthetic_response: Optional[str] = None
    streaming_handler: Optional[Any] = None  # StreamingMessageHandler instance
    interim_message_callback: Optional[InterimMessageCallback] = None
    # Claude emits one complete AssistantMessage per semantic message. Keep the
    # newest one pending until a later text/tool frame proves it is interim;
    # terminal text stays on the normal final-response path.
    pending_completed_message: Optional[str] = None
    retained_response_parts: List[str] = field(default_factory=list)
    delivered_interim_parts: List[str] = field(default_factory=list)
    interim_delivered: bool = False
    # Historical request-wide flag retained for compatibility/observability.
    streamed_via_partials: bool = False
    # Per-semantic-message form of the flag. Reset at each AssistantMessage so
    # a later message without partials can still use whole-block fallback.
    current_message_streamed_via_partials: bool = False
    # Unchanged image files may be read once per Telegram request. Claude Code
    # otherwise embeds the same base64 payload repeatedly and burns context.
    image_read_fingerprints: set[str] = field(default_factory=set)


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
    # Set when a terminal-event stall released the head request (#411 C). The
    # stream is being torn down; if its late ResultMessage still races in, the
    # reader swallows exactly one instead of double-delivering the same answer
    # through the unsolicited route.
    stall_swallow_result: bool = False
