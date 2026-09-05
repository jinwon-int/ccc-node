"""Shared structural ports for the ``TelegramBot`` mixins (#1484, precursor to #896).

Each mixin used to re-declare its own private ``Protocol`` for the objects
``TelegramBot.__init__`` injects (``settings``, ``session_manager``,
``project_chat``, ``clock``), and the copies drifted: ``get_session`` was typed
``key: Any`` in one mixin and ``user_id: int`` in another, ``bot_data_dir`` was
``Path`` here and ``Path | None`` there, and two stubs described session-manager
methods that no longer exist. This module is the single home for those ports.

Split rationale: there is one Protocol per *injected collaborator*, not one
per mixin. The concrete objects are ``utils.config.Settings``,
``session.manager.SessionManager``, ``core.project_chat.ProjectChatHandler``
and the ``time`` module, so grouping by collaborator makes it impossible for
the same member to be declared twice with different shapes. The config port
is the largest because ``Settings`` is one object; it is sectioned by concern
so that #896 can slice it further if a mixin ever moves out of the bot.

Every signature below is derived from the concrete implementation, not from
the old stubs. Bound-method ports (``ReplySmartFn`` etc.) describe methods the
composed ``TelegramBot`` provides from *another* mixin and are shared where
two mixins previously duplicated them.
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
)

from telegram import Message, Update

from telegram_bot.core.agent_runtime import (
    JsonValue as AgentJsonValue,
    ModelInfo,
    SessionHistory,
    SessionSummary,
)
from telegram_bot.core.project_chat_types import (
    AgentApprovalCallback,
    ChatResponse,
    InterimMessageCallback,
    PermissionCallback,
    StatusCallback,
    TypingCallback,
)
from telegram_bot.core.usage import UsageSnapshot
from telegram_bot.core.usage_meter import MODE_INTERACTIVE, UsageMeter

# ``core.session_scope.storage_key`` returns the bare ``user_id`` for DMs and an
# ``"actor:chat"`` string for group / shared scopes. ``SessionManager`` still
# annotates its parameter ``user_id: int`` for historical reasons; this alias is
# the honest shape of what the bot actually passes.
ConversationKey = int | str


class BotConfigPort(Protocol):
    """Attributes of the validated ``Settings`` object the bot mixins read directly.

    Members are typed exactly as their ``Field`` declarations in
    ``utils/config.py`` and the ``settings_*`` sections it composes. Optional
    attributes read through ``getattr(self._config, ..., default)`` are not
    required to appear here.
    """

    # --- paths / identity -------------------------------------------------
    @property
    def bot_data_dir(self) -> Path: ...

    @property
    def project_root(self) -> Path: ...

    @property
    def telegram_bot_token(self) -> str: ...

    @property
    def claude_cli_path(self) -> Optional[Path]: ...

    @property
    def claude_settings_path(self) -> Path: ...

    @property
    def piri_cli_path(self) -> str: ...

    # --- access control ----------------------------------------------------
    @property
    def allowed_user_ids(self) -> Sequence[int]: ...

    @property
    def require_allowlist(self) -> bool: ...

    @property
    def execution_profile(self) -> str: ...

    @property
    def bash_policy(self) -> str: ...

    # --- lifecycle / restart ----------------------------------------------
    @property
    def restart_service_unit(self) -> str: ...

    @property
    def restart_delay_seconds(self) -> int: ...

    @property
    def heartbeat_store_path(self) -> Optional[Path]: ...

    @property
    def heartbeat_delete_on_done(self) -> bool: ...

    # --- delivery / rendering ---------------------------------------------
    @property
    def bridge_memory_mode(self) -> Literal["off", "curated", "audience-scoped"]: ...

    @property
    def telegram_max_bubble_chars(self) -> int: ...

    @property
    def enable_readable_renderer(self) -> bool: ...

    @property
    def enable_loose_spacing(self) -> bool: ...

    @property
    def spacing_lines(self) -> int: ...

    @property
    def enable_entity_renderer(self) -> bool: ...

    @property
    def enable_option_buttons(self) -> bool: ...

    # --- inbound media -----------------------------------------------------
    @property
    def max_document_size_mb(self) -> int: ...

    @property
    def image_context_guard(self) -> bool: ...

    @property
    def telegram_max_image_bytes(self) -> int: ...

    @property
    def telegram_max_image_pixels(self) -> int: ...

    # --- voice -----------------------------------------------------------------
    @property
    def transcription_provider(self) -> str: ...

    @property
    def openai_api_key(self) -> Optional[str]: ...

    @property
    def openai_base_url(self) -> Optional[str]: ...

    @property
    def whisper_model(self) -> str: ...

    @property
    def max_voice_duration(self) -> int: ...

    @property
    def ffmpeg_path(self) -> Optional[str]: ...

    @property
    def voice_reply_persona(self) -> str: ...

    @property
    def volcengine_app_id(self) -> Optional[str]: ...

    @property
    def volcengine_token(self) -> Optional[str]: ...

    @property
    def volcengine_access_key(self) -> Optional[str]: ...

    @property
    def volcengine_secret_access_key(self) -> Optional[str]: ...

    @property
    def volcengine_tos_bucket_name(self) -> Optional[str]: ...

    @property
    def volcengine_tos_endpoint(self) -> str: ...

    @property
    def volcengine_tos_region(self) -> str: ...

    @property
    def volcengine_tos_signed_url_ttl_seconds(self) -> int: ...

    @property
    def volcengine_cluster(self) -> str: ...

    @property
    def volcengine_resource_id(self) -> str: ...

    @property
    def volcengine_model_name(self) -> str: ...

    @property
    def volcengine_submit_endpoint(self) -> str: ...

    @property
    def volcengine_query_endpoint(self) -> str: ...

    @property
    def volcengine_timeout_seconds(self) -> float: ...

    @property
    def volcengine_max_retries(self) -> int: ...

    @property
    def volcengine_initial_backoff(self) -> float: ...

    @property
    def volcengine_poll_interval_seconds(self) -> float: ...

    @property
    def volcengine_max_poll_seconds(self) -> float: ...


class SessionManagerPort(Protocol):
    """``session.manager.SessionManager`` surface used by the bot mixins."""

    def validate_storage_path(self) -> None: ...

    def initialize(self) -> None: ...

    async def get_session(self, key: ConversationKey) -> dict[str, Any]: ...

    async def patch_session(
        self,
        key: ConversationKey,
        *,
        updates: Optional[Mapping[str, Any]] = None,
        remove_fields: Iterable[str] = (),
    ) -> None: ...

    async def patch_session_if(
        self,
        key: ConversationKey,
        *,
        expected: Mapping[str, Any],
        updates: Optional[Mapping[str, Any]] = None,
        remove_fields: Iterable[str] = (),
    ) -> bool: ...


class ProjectChatPort(Protocol):
    """``core.project_chat.ProjectChatHandler`` surface used by the bot mixins."""

    @property
    def conversations_dir(self) -> Path: ...

    @property
    def usage_meter(self) -> Optional[UsageMeter]: ...

    async def process_message(
        self,
        user_message: str,
        user_id: int,
        chat_id: int,
        message_id: Optional[int] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        approval_policy: Optional[str] = None,
        approvals_reviewer: Optional[str] = None,
        sandbox_policy: Optional[Mapping[str, AgentJsonValue]] = None,
        new_session: bool = False,
        permission_callback: Optional[PermissionCallback] = None,
        approval_callback: Optional[AgentApprovalCallback] = None,
        typing_callback: Optional[TypingCallback] = None,
        status_callback: Optional[StatusCallback] = None,
        bot: Optional[Any] = None,
        notification_bot: Optional[Any] = None,
        interim_message_callback: Optional[InterimMessageCallback] = None,
        sensitive_log_event: Optional[str] = None,
        usage_mode: str = MODE_INTERACTIVE,
    ) -> ChatResponse: ...

    async def get_usage(
        self, user_id: int, chat_id: int, session_id: str | None
    ) -> UsageSnapshot: ...

    async def list_runtime_models(self) -> Sequence[ModelInfo]: ...

    async def list_runtime_sessions(
        self, *, limit: int = 10
    ) -> Sequence[SessionSummary]: ...

    async def read_runtime_session(
        self, session_id: str, *, limit: int = 5
    ) -> SessionHistory: ...

    # Transcript scans below are synchronous file reads; callers on the event
    # loop must offload them with ``asyncio.to_thread`` (#1479).
    def list_sessions(self, limit: int = 10) -> list[tuple[str, str, float]]: ...

    def get_recent_messages(
        self, session_id: str, limit: int = 5
    ) -> list[dict[str, Any]]: ...

    def get_conversation_history(
        self, session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]: ...

    def get_session_last_assistant_message(
        self, session_id: str, max_chars: int = 300
    ) -> Optional[str]: ...

    async def stop(self, user_id: int, chat_id: Optional[int] = None) -> bool: ...

    async def clear_user_stream(
        self, user_id: int, chat_id: Optional[int] = None
    ) -> None: ...

    def clear_pending_permissions(
        self, user_id: int, chat_id: Optional[int] = None
    ) -> None: ...

    async def cancel_user_streaming(
        self, user_id: int, chat_id: Optional[int] = None
    ) -> bool: ...

    def invalidate_agent_approvals(
        self, user_id: int, chat_id: Optional[int] = None
    ) -> None: ...

    def is_agent_approval_active(
        self, user_id: int, chat_id: int, generation: int
    ) -> bool: ...

    def workload_snapshot(self, now: float) -> tuple[int, float]: ...

    def foreground_workload_snapshot(self, now: float) -> tuple[int, float]: ...

    def begin_drain(self) -> bool: ...

    def waiting_for_turn_snapshot(self) -> int: ...

    async def enforce_session_resource_limits(
        self, *, now: float | None = None
    ) -> dict[str, int | float]: ...

    def set_async_completion_sender(self, sender: Any) -> None: ...

    async def close(self) -> None: ...


class ClockPort(Protocol):
    """Monotonic-enough wall clock; the default is the ``time`` module."""

    def time(self) -> float: ...


# --- bound-method ports provided by sibling mixins on the composed bot ------


class ReplySmartFn(Protocol):
    """``BotDeliveryMixin._reply_smart`` as seen by the command and voice mixins."""

    def __call__(
        self,
        message: Message,
        content: str,
        parse_mode: str = "Markdown",
        force_options: bool = False,
        streamed: bool = False,
        user_id: Optional[int] = None,
    ) -> Awaitable[None]: ...


class ProcessUserMessageTextFn(Protocol):
    """``TelegramBot._process_user_message_text`` as seen by delivery and voice."""

    def __call__(
        self,
        update: Update,
        user_id: int,
        text: str,
        message_source: str = "text",
        voice_input_preview: Optional[str] = None,
        sensitive_log_event: Optional[str] = None,
    ) -> Awaitable[None]: ...


class EnqueueUserTaskFn(Protocol):
    """``BotFollowupQueueMixin._enqueue_user_task`` (durable follow-up admission).

    This is the only implementation on the composed bot: ``TelegramBot``'s MRO
    puts the follow-up queue mixin first, so no other mixin may define a method
    of this name (#1484).
    """

    def __call__(
        self,
        user_id: Any,
        run_task: Any,
        on_overflow: Any,
    ) -> Awaitable[bool]: ...


class ClearUserQueueFn(Protocol):
    """``BotFollowupQueueMixin._clear_user_queue`` -> (volatile, durable, receipts)."""

    def __call__(self, user_id: Any) -> Awaitable[tuple[int, int, int]]: ...
