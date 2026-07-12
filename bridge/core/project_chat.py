"""
Project Chat Handler - Integrates Telegram with Claude Code SDK.
"""

import os
import re
import time
import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,  # noqa: F401
    ResultMessage,
    StreamEvent,  # noqa: F401
    TextBlock,  # noqa: F401
    ToolUseBlock,  # noqa: F401
    PermissionResultAllow,
    PermissionResultDeny,
)

from telegram_bot.utils.config import config
from telegram_bot.core.task_ledger import (
    CANCELED as TASK_CANCELED,
    INPUT_REQUIRED as TASK_INPUT_REQUIRED,
    WORKING as TASK_WORKING,
    TaskLedger,
    ledger_path_for,
)
from telegram_bot.core.heartbeat import (
    compose_heartbeat_text,
    has_recent_visible_progress,
    should_update_heartbeat,
)
from telegram_bot.utils.duration_log import (
    append_duration_sample,
    default_duration_log_path,
    forecast_samples,
    remaining_ms,
)

logger = logging.getLogger(__name__)


from telegram_bot.core.tool_policy import (  # noqa: E402
    BASH_DISABLED,
    EXECUTION_OWNER_OPERATOR,
    EXECUTION_STRICT_PROJECT,
    effective_bash_policy,
    missing_callback_requires_denial,
    resolve_bash_policy,
    resolve_execution_profile,
    sdk_permission_options,
    strict_bash_sandbox_settings,
)

PROCESS_TIMEOUT = int(os.getenv("CLAUDE_PROCESS_TIMEOUT", "21600"))

# Compatibility knobs for legacy direct-construction tests. Production always
# injects Settings and therefore never reads these module values.
EXECUTION_PROFILE = EXECUTION_STRICT_PROJECT
BASH_POLICY = "auto-approve"


# Pure SDK-stream / text helpers live in core/sdk_text.py (error classification,
# stream-delta extraction, AskUserQuestion formatting, numbered-option detection).
# Re-exported here so existing call sites and
# `from telegram_bot.core.project_chat import _is_...` imports (tests) keep working.
from telegram_bot.core.sdk_text import (  # noqa: E402,F401
    RESTART_INTERRUPT_NOTICE,
    TASK_TERMINATED_NOTICE,
    CANCEL_REASON_WINDOW_S,
    describe_cancel_reason,
    _is_shutdown_signal_error,
    _is_retryable_sdk_error,
    _format_ask_user_question,
    _extract_stream_text_delta,
    _detect_numbered_options,
)


from telegram_bot.core.project_chat_types import (  # noqa: E402,F401
    ChatResponse,
    PermissionCallback,
    StatusCallback,
    TypingCallback,
    UnsolicitedCallback,
    _PendingRequest,
    _UserStreamState,
)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid integer env %s=%r; using %s", name, raw, default)
        return default


TYPING_INTERVAL = 4  # Telegram typing status expires after ~5s
TYPING_MAX_NO_PROGRESS_SECONDS = _env_int("CCC_TYPING_MAX_NO_PROGRESS_SECONDS", 600)

from telegram_bot.core.project_chat_history import ProjectChatHistoryMixin  # noqa: E402
from telegram_bot.core.project_chat_process import ProjectChatProcessMixin  # noqa: E402
from telegram_bot.core.project_chat_reader import ProjectChatReaderMixin  # noqa: E402
from telegram_bot.core.project_chat_state import ProjectChatStateMixin  # noqa: E402


class ProjectChatHandler(
    ProjectChatReaderMixin,
    ProjectChatProcessMixin,
    ProjectChatStateMixin,
    ProjectChatHistoryMixin,
):
    """
    Handles Telegram messages using per-conversation long-lived Claude SDK streams.

    Requests for the same Telegram conversation are serialized until the SDK
    returns a terminal ResultMessage. This preserves the bridge's pending FIFO
    even when Claude Code internally records queued prompts on a live stream.
    """

    def __init__(
        self,
        settings: Any = None,
        *,
        sdk_client_factory: Optional[Callable[..., Any]] = None,
        agent_runtime: Any = None,
        clock: Any = None,
    ):
        # ``settings=None`` is retained only for legacy unit-test adapters. The
        # production composition root always injects the validated Settings.
        compatibility_mode = settings is None
        self._config = config if compatibility_mode else settings
        root_value = getattr(
            self._config,
            "project_root",
            os.environ.get("PROJECT_ROOT", Path.cwd()),
        )
        self.project_root = Path(root_value).resolve()
        project_dir_name = str(self.project_root).replace("/", "-").replace("_", "-")
        self.conversations_dir = Path.home() / ".claude" / "projects" / project_dir_name
        profile = (
            EXECUTION_PROFILE
            if compatibility_mode
            else getattr(self._config, "execution_profile", EXECUTION_STRICT_PROJECT)
        )
        policy = (
            BASH_POLICY
            if compatibility_mode
            else getattr(self._config, "bash_policy", None)
        )
        if compatibility_mode:
            self._execution_profile = profile
        else:
            self._execution_profile = resolve_execution_profile(
                profile,
                allowed_user_ids=getattr(self._config, "allowed_user_ids", []),
                require_allowlist=getattr(self._config, "require_allowlist", True),
            )
        self._bash_policy = effective_bash_policy(
            resolve_bash_policy(policy),
            self._execution_profile,
        )
        self._sdk_client_factory = sdk_client_factory or ClaudeSDKClient
        provider = getattr(self._config, "agent_provider", "claude")
        if provider == "codex" and agent_runtime is None:
            raise ValueError("Codex ProjectChat requires an injected AgentRuntime")
        self._agent_runtime = agent_runtime if provider == "codex" else None
        self._agent_sessions: Dict[Tuple[int, int], Any] = {}
        self._agent_session_models: Dict[Tuple[int, int], Optional[str]] = {}
        self._agent_active_sessions: Dict[Tuple[int, int], Any] = {}
        self._agent_started_at: Dict[Tuple[int, int], float] = {}
        self._agent_runtime_closed = False
        self._agent_interrupt_timeout_seconds = 10.0
        self._clock = clock or time
        self._process_timeout_seconds = PROCESS_TIMEOUT
        self._typing_interval_seconds = TYPING_INTERVAL
        # Streams are scoped by Telegram conversation, not only user. A single
        # Telegram user may talk to the bridge in a private DM and a group at the
        # same time; sharing one Claude stream made pending ResultMessages race
        # and could swap answers between chats.
        self._streams: Dict[Tuple[int, int], _UserStreamState] = {}
        self._stream_init_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        self._conversation_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        logger.info(f"ProjectChatHandler initialized for {self.project_root}")

    @staticmethod
    def _stream_key(user_id: int, chat_id: int) -> Tuple[int, int]:
        return (user_id, chat_id)

    def _get_stream_init_lock(self, user_id: int, chat_id: int) -> asyncio.Lock:
        key = self._stream_key(user_id, chat_id)
        lock = self._stream_init_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._stream_init_locks[key] = lock
        return lock

    def _get_conversation_lock(self, user_id: int, chat_id: int) -> asyncio.Lock:
        key = self._stream_key(user_id, chat_id)
        lock = self._conversation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[key] = lock
        return lock

    async def _create_user_stream(
        self,
        user_id: int,
        model: Optional[str],
        unsolicited_callback: Optional[UnsolicitedCallback] = None,
    ) -> _UserStreamState:
        state_holder: Dict[str, _UserStreamState] = {}
        bash_policy = self._bash_policy

        async def can_use_tool(tool_name, tool_input, _context=None):
            logger.debug(
                f"can_use_tool called: tool_name={tool_name}, tool_input type={type(tool_input)}"
            )
            # AskUserQuestion: degrade to plain text instead of interactive dialog
            if tool_name == "AskUserQuestion" and isinstance(tool_input, dict):
                formatted, _ = _format_ask_user_question(tool_input)
                logger.debug(f"AskUserQuestion intercepted, formatted: {formatted[:200]}...")
                s = state_holder.get("state")
                if s and s.pending:
                    s.pending[0].synthetic_response = formatted
                    logger.debug(f"Set synthetic_response for user {user_id}")
                return PermissionResultDeny(
                    message=(
                        "AskUserQuestion tool is not available. "
                        "CRITICAL: You MUST output the question and numbered options to the user, then STOP and WAIT. "
                        "Do NOT continue execution. Do NOT make assumptions about the user's choice. "
                        "Output format:\n\n"
                        "[Question and context]\n\n"
                        "1. [First option]\n"
                        "2. [Second option]\n"
                        "3. [Third option]\n\n"
                        "After outputting the options, you MUST stop and wait for the user to respond with their choice."
                    )
                )
            state = state_holder.get("state")
            if not state or not state.pending:
                if missing_callback_requires_denial(tool_name, bash_policy):
                    logger.warning("bash_callback_state_missing user_id=%s", user_id)
                    return PermissionResultDeny(
                        message="Bash requires an active per-call permission callback."
                    )
                return PermissionResultAllow()
            req = state.pending[0]
            callback = req.permission_callback
            if not callback:
                if missing_callback_requires_denial(tool_name, bash_policy):
                    logger.warning("bash_permission_callback_missing user_id=%s", user_id)
                    return PermissionResultDeny(
                        message="Bash requires an active per-call permission callback."
                    )
                return PermissionResultAllow()

            req.awaiting_permission = True
            led = self._task_ledger
            if led and req.task_id:
                led.set_state(req.task_id, TASK_INPUT_REQUIRED)
            try:
                result = await callback(req.chat_id, user_id, tool_name, tool_input)
            finally:
                req.awaiting_permission = False
                if led and req.task_id:
                    led.set_state(req.task_id, TASK_WORKING)
            if isinstance(result, (PermissionResultAllow, PermissionResultDeny)):
                return result
            return PermissionResultAllow() if result else PermissionResultDeny()

        permission_options = sdk_permission_options(bash_policy)
        opts: Dict[str, Any] = {
            "cwd": str(self.project_root),
            "allowed_tools": permission_options["allowed_tools"],
            "disallowed_tools": permission_options["disallowed_tools"],
            "hooks": permission_options["hooks"],
            "system_prompt": (
                "\n\n## Important: User Questions and Choices\n\n"
                "The AskUserQuestion tool is NOT available in this environment. "
                "When you need to ask the user a question with multiple choice options:\n\n"
                "1. Output the question and context clearly\n"
                "2. List options with numbers (1., 2., 3., etc.)\n"
                "3. STOP and WAIT for the user's response\n"
                "4. Do NOT continue execution or make assumptions\n"
                "5. Do NOT try to use AskUserQuestion tool\n\n"
                "Example format:\n"
                "Question: Which option do you prefer?\n\n"
                "1. Option A - Description\n"
                "2. Option B - Description\n"
                "3. Option C - Description\n\n"
                "After outputting options, you MUST stop and wait for user input.\n\n"
                "## Important: Sending Images and Files\n\n"
                "When the user asks you to send/show/deliver an image or file:\n\n"
                "1. Do NOT use the Read tool to read or analyze the image/file content\n"
                "2. Simply output the file path in your response (absolute path preferred)\n"
                "3. The system will automatically detect file paths and send them as messages\n"
                "4. Supported image formats: .png, .jpg, .jpeg, .gif, .webp\n"
                "5. Other files will be sent as documents\n\n"
                "Example: When user says 'send me the generated image', just respond with:\n"
                "'Here is the image: /path/to/image.png' - the system will send it automatically.\n\n"
                "After generating an image (e.g., via a skill), ALWAYS include the output file path "
                "in your response so the system can send it to the user."
            ),
            "can_use_tool": can_use_tool,
            "permission_mode": "default",
            # Raise stream-json decode buffer from default 1MB to 10MB.
            # A single CLI->SDK JSON message (usually a large tool_result)
            # exceeding 1MB was raising:
            #   "Failed to decode JSON: JSON message exceeded maximum
            #    buffer size of 1048576 bytes"
            "max_buffer_size": 10 * 1024 * 1024,
            # Real token-level streaming: ask the SDK for partial message events
            # so the reader loop can update the Telegram draft from incremental
            # text deltas (true typewriter effect). The draft edit cadence stays
            # throttled by draft_update_min_chars / draft_update_interval.
            "include_partial_messages": bool(
                self._config.enable_streaming and self._config.enable_partial_streaming
            ),
        }
        if self._execution_profile == EXECUTION_OWNER_OPERATOR:
            # Owner-operated bridges intentionally retain host utility and the
            # normal Claude Code settings/context chain. Access control, not a
            # project-root sandbox, is the boundary for this explicit profile.
            opts["setting_sources"] = ["user", "project", "local"]
        else:
            # Every non-owner profile suppresses filesystem settings. Even when
            # Bash is disallowed, user/project/local settings can register host
            # shell hooks independently of the model-facing Bash tool.
            opts["setting_sources"] = []
            if self._execution_profile == EXECUTION_STRICT_PROJECT and bash_policy != BASH_DISABLED:
                # Strict-project uses the SDK OS sandbox as the Bash boundary.
                opts["sandbox"] = strict_bash_sandbox_settings(
                    self.project_root,
                    str(self._config.claude_cli_path) if self._config.claude_cli_path else None,
                )
        if self._config.claude_cli_path:
            opts["cli_path"] = str(self._config.claude_cli_path)
        if model:
            # Normalize model name: ensure at most one [1M] suffix
            # e.g., "claude-opus-4-7[1M][1m]" -> "claude-opus-4-7[1M]"
            # e.g., "opus" -> "opus" (alias, unchanged)
            normalized = re.sub(r"\[(?:1[mM])\]+", "", model)  # Remove all [1M]/[1m] suffixes
            if normalized != model:
                # Had suffix, add back single [1M]
                normalized = f"{normalized}[1m]"
                logger.info(f"Model normalized: {model!r} -> {normalized!r}")
            opts["model"] = normalized

        client = self._sdk_client_factory(options=ClaudeAgentOptions(**opts))
        await client.connect()
        state = _UserStreamState(
            client=client,
            model=model,
            unsolicited_callback=unsolicited_callback,
        )
        state_holder["state"] = state
        state.reader_task = asyncio.create_task(self._reader_loop(user_id, state))
        state.typing_task = asyncio.create_task(self._typing_keepalive_loop(user_id, state))
        return state

    async def _disconnect_stream_state(
        self, key: Any, state: _UserStreamState, cancel_message: Optional[str] = None
    ) -> bool:
        if isinstance(key, tuple):
            user_id, chat_id = key
        else:
            user_id, chat_id = key, "*"

        # Cancel typing keepalive task
        if state.typing_task and not state.typing_task.done():
            state.typing_task.cancel()
            try:
                await asyncio.wait_for(state.typing_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as e:
                logger.error(f"Error cancelling typing task for user {user_id} chat {chat_id}: {e}")

        # Cancel reader task first
        if state.reader_task and not state.reader_task.done():
            state.reader_task.cancel()
            try:
                await asyncio.wait_for(state.reader_task, timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Reader task for user {user_id} chat {chat_id} did not complete within timeout"
                )
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error cancelling reader task for user {user_id} chat {chat_id}: {e}")

        # Fail all pending requests.
        msg = cancel_message or TASK_TERMINATED_NOTICE
        # If this disconnect was triggered by a recent stream error (usage limit,
        # auth, network drop) rather than an explicit user /stop, surface the real
        # reason instead of the opaque "Task has been terminated." notice.
        if msg == TASK_TERMINATED_NOTICE and state.last_error:
            if (self._clock.monotonic() - state.last_error_ts) < CANCEL_REASON_WINDOW_S:
                reason = describe_cancel_reason(state.last_error)
                if reason:
                    msg = reason
            state.last_error = None
        while state.pending:
            req = state.pending.popleft()
            cleaned = await self._cleanup_heartbeat(req)
            self._ledger_finish(req, TASK_CANCELED, cleanup_done=cleaned)
            if not req.future.done():
                try:
                    req.future.set_result(
                        ChatResponse(
                            content=msg,
                            success=False,
                            error=msg,
                            session_id=state.last_session_id,
                        )
                    )
                except Exception as e:
                    logger.error(f"Error setting future result: {e}")

        # Disconnect client.  The SDK transport's close() waits up to 5s for a
        # graceful stdin-EOF shutdown then sends SIGTERM (another 5s) before
        # SIGKILL — 10s total.  Allow 15s so the subprocess is actually killed
        # rather than abandoned as an orphan when the outer timeout fires.
        try:
            await asyncio.wait_for(state.client.disconnect(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning(
                f"Client disconnect for user {user_id} chat {chat_id} timed out after 15s"
            )
        except Exception as e:
            logger.error(f"Error disconnecting client for user {user_id} chat {chat_id}: {e}")

        return True

    async def _disconnect_user_stream(
        self, user_id: int, chat_id: Optional[int] = None, cancel_message: Optional[str] = None
    ) -> bool:
        if chat_id is not None:
            key = self._stream_key(user_id, chat_id)
            state = self._streams.pop(key, None)
            if not state:
                return False
            return await self._disconnect_stream_state(key, state, cancel_message)

        matched = [
            (key, state)
            for key, state in list(self._streams.items())
            if (key[0] if isinstance(key, tuple) else key) == user_id
        ]
        if not matched:
            return False
        for key, state in matched:
            self._streams.pop(key, None)
            await self._disconnect_stream_state(key, state, cancel_message)
        return True

    async def _get_or_create_stream(
        self,
        user_id: int,
        chat_id: int,
        model: Optional[str],
        new_session: bool,
        unsolicited_callback: Optional[UnsolicitedCallback] = None,
    ) -> _UserStreamState:
        key = self._stream_key(user_id, chat_id)
        lock = self._get_stream_init_lock(user_id, chat_id)
        async with lock:
            state = self._streams.get(key)

            # Detect stale stream: reader task ended (e.g. after system sleep/wake)
            if state and state.reader_task is not None and state.reader_task.done():
                logger.warning(
                    f"Stale stream detected for user {user_id} chat {chat_id} (reader task exited), recreating"
                )
                await self._disconnect_user_stream(user_id, chat_id)
                state = None

            if state and (new_session or state.model != model):
                await self._disconnect_user_stream(user_id, chat_id)
                state = None

            if not state:
                state = await self._create_user_stream(user_id, model, unsolicited_callback)
                self._streams[key] = state
            elif unsolicited_callback is not None:
                # Refresh the route when Telegram supplies a new Bot instance,
                # while preserving the same long-lived SDK stream.
                state.unsolicited_callback = unsolicited_callback
            return state

    def workload_snapshot(self, now: float) -> tuple[int, float]:
        """Return ``(in_flight_count, oldest_request_age_seconds)``.

        Exposes bridge busyness so an external supervisor (the self-update
        procedure) can avoid restarting the bridge mid-request — a restart
        SIGTERM-kills the in-flight ``claude`` child (exit 143) and destroys
        the user's work. ``now`` must come from the event loop clock so it is
        comparable to ``_PendingRequest.started_at``.
        """
        count = len(self._agent_active_sessions)
        oldest_started: Optional[float] = None
        for started_at in self._agent_started_at.values():
            if oldest_started is None or started_at < oldest_started:
                oldest_started = started_at
        for state in list(self._streams.values()):
            for req in list(state.pending):
                if req.future.done():
                    continue
                count += 1
                if req.started_at > 0 and (
                    oldest_started is None or req.started_at < oldest_started
                ):
                    oldest_started = req.started_at
        oldest_age = (now - oldest_started) if oldest_started is not None else 0.0
        return count, max(0.0, oldest_age)

    @property
    def _task_ledger(self):
        """Lazy persistent task ledger; None when no data dir is configured."""
        cached = getattr(self, "_task_ledger_cache", None)
        if cached is not None:
            return cached or None  # False sentinel = resolved to unavailable
        path = ledger_path_for(
            getattr(config, "bot_data_dir", None),
            getattr(config, "task_ledger_path", None),
        )
        self._task_ledger_cache = TaskLedger(path) if path else False
        return self._task_ledger_cache or None

    def _ledger_create(self, user_id: int, chat_id: int):
        led = self._task_ledger
        return led.create(user_id, chat_id) if led else None

    def _ledger_finish(self, req: _PendingRequest, state: str, *, cleanup_done: bool) -> None:
        led = self._task_ledger
        if led and req.task_id:
            led.finish(req.task_id, state, cleanup_done=cleanup_done)

    async def _cleanup_heartbeat(self, req: _PendingRequest) -> bool:
        """Delete/clear the transient heartbeat message for a request.

        Returns True when there is nothing left to clean (no message, or the
        delete went through) — False when the delete failed, so the caller's
        terminal transition keeps a retryable op in the task ledger.
        """
        if not req.status_callback or req.heartbeat_message_id is None:
            return True
        cleaned = False
        try:
            cleaned = (await req.status_callback(None, req.heartbeat_message_id)) is None
        except Exception as e:
            logger.warning(
                "Heartbeat cleanup failed for user %s chat %s: %s",
                req.user_id,
                req.chat_id,
                type(e).__name__,
            )
        if cleaned:
            req.heartbeat_message_id = None
            led = self._task_ledger
            if led and req.task_id:
                led.set_status_message(req.task_id, None)
        return cleaned

    async def _maybe_update_heartbeat(self, req: _PendingRequest, now: float) -> None:
        """Send or edit a fail-open long-running task heartbeat."""
        if not getattr(config, "heartbeat_enabled", True):
            return
        if not req.status_callback or req.future.done():
            return

        # Stall guard: if the SDK stream has gone silent for too long the request
        # is stuck (e.g. a bridge restart left it in flight, or the stream hung)
        # and will never reach the terminal ResultMessage that deletes the
        # heartbeat. Remove the dangling "⏳ Working — Nm" line rather than let it
        # tick up forever as the last chat message. It reappears if activity
        # resumes (last_event_at advances on the next SDK event).
        stall_seconds = float(getattr(config, "heartbeat_stall_seconds", 0.0) or 0.0)
        if stall_seconds > 0:
            last_event = req.last_event_at or req.started_at
            if last_event > 0 and now - last_event >= stall_seconds:
                if req.heartbeat_message_id is not None:
                    await self._cleanup_heartbeat(req)
                return

        threshold = float(getattr(config, "heartbeat_threshold_seconds", 15.0))
        interval = float(getattr(config, "heartbeat_update_interval_seconds", 15.0))
        if not should_update_heartbeat(
            now=now,
            started_at=req.started_at,
            last_update_at=req.heartbeat_last_update_at,
            threshold_seconds=threshold,
            update_interval_seconds=interval,
        ):
            return

        if (
            getattr(config, "heartbeat_suppress_when_streaming_progress", True)
            and req.streaming_handler
            and getattr(req.streaming_handler, "drafts", None)
            and has_recent_visible_progress(
                now=now,
                last_visible_progress_at=req.last_visible_progress_at,
                window_seconds=threshold,
            )
        ):
            return

        self._load_heartbeat_forecast(req)
        # Recompute the ETA on every tick as a REMAINING-time estimate
        # conditioned on the samples still longer than the current elapsed time
        # (see duration_log.remaining_ms) — a fixed total-median forecast goes
        # stale and reads absurd once elapsed exceeds it.
        elapsed = now - req.started_at
        forecast_remaining_ms = (
            remaining_ms(
                req.heartbeat_forecast_samples,
                elapsed_ms=int(elapsed * 1000),
            )
            if req.heartbeat_forecast_samples
            else None
        )
        text = compose_heartbeat_text(
            elapsed_seconds=elapsed,
            current_tool=req.current_tool_label,
            forecast_seconds=(forecast_remaining_ms / 1000.0)
            if forecast_remaining_ms is not None
            else None,
        )
        try:
            previous_id = req.heartbeat_message_id
            message_id = await req.status_callback(text, req.heartbeat_message_id)
            req.heartbeat_message_id = message_id
            req.heartbeat_last_update_at = now
            # Register the projection in the task ledger so a terminal
            # transition (or a restart's reconciliation) can always clean it.
            if message_id != previous_id:
                led = self._task_ledger
                if led and req.task_id:
                    led.set_status_message(req.task_id, message_id)
        except Exception as e:
            logger.warning(
                "Heartbeat update failed for user %s chat %s: %s",
                req.user_id,
                req.chat_id,
                type(e).__name__,
            )

    def _duration_log_path(self) -> Path:
        path = getattr(self._config, "heartbeat_duration_log_path", None)
        if path is None:
            return default_duration_log_path(
                Path(getattr(self._config, "bot_data_dir", self.project_root / ".telegram_bot"))
            )
        return Path(path)

    def _load_heartbeat_forecast(self, req: _PendingRequest) -> None:
        """Load the duration samples the ETA conditions on (once per request).

        Only the sample list is cached — the remaining-time estimate itself is
        recomputed from it on every heartbeat tick so it tracks elapsed time.
        """
        if req.heartbeat_forecast_loaded:
            return
        req.heartbeat_forecast_loaded = True
        if not getattr(config, "heartbeat_forecast_enabled", False):
            return
        req.heartbeat_forecast_samples = forecast_samples(
            self._duration_log_path(),
            user_id=req.user_id,
            model=req.model,
            min_samples=int(getattr(config, "heartbeat_forecast_min_samples", 10)),
        )

    def _append_duration_log(self, req: _PendingRequest, msg: ResultMessage) -> None:
        """Record request duration metadata without prompt/response text."""
        if not getattr(config, "heartbeat_duration_log_enabled", False):
            return
        append_duration_sample(
            path=self._duration_log_path(),
            user_id=req.user_id,
            chat_id=req.chat_id,
            session_id=msg.session_id or req.requested_session_id,
            model=req.model,
            duration_ms=msg.duration_ms,
            success=not msg.is_error,
            max_lines=int(getattr(config, "heartbeat_duration_log_max_lines", 10000)),
        )

    def _should_refresh_typing(self, req: _PendingRequest, now: float) -> bool:
        """Return whether Telegram typing should still be asserted for a request."""
        if req.future.done() or req.awaiting_permission:
            return False
        # After any visible draft/tool progress, stop typing entirely. Telegram
        # draft edits do not clear typing; progress/heartbeat should represent
        # the work from here instead of reasserting a stale chat action.
        if req.last_visible_progress_at > 0:
            return False
        if (
            TYPING_MAX_NO_PROGRESS_SECONDS > 0
            and req.started_at > 0
            and now - req.started_at >= TYPING_MAX_NO_PROGRESS_SECONDS
        ):
            return False
        return True

    async def _typing_keepalive_loop(self, user_id: int, state: _UserStreamState) -> None:
        """Background task that sends typing actions at regular intervals.

        Keeps Telegram typing indicator alive during long tool calls when
        the SDK stream emits no messages.
        """
        try:
            while True:
                await asyncio.sleep(TYPING_INTERVAL)
                if not state.pending:
                    continue
                req = state.pending[0]
                # Once a request's response is finalized (future resolved) it is
                # about to be popped and delivered — stop refreshing the typing
                # indicator so it doesn't reassert "typing…" after the agent's
                # final message. Streamed replies edit drafts rather than sending
                # a new message, so they never clear typing on their own; a stray
                # keepalive here is exactly what leaves it stuck.
                if req.future.done():
                    continue
                now = asyncio.get_event_loop().time()
                if not self._should_refresh_typing(req, now):
                    await self._maybe_update_heartbeat(req, now)
                    continue
                if req.typing_callback and now - req.last_typing_at >= TYPING_INTERVAL:
                    # Re-check immediately before the network call to avoid a
                    # finalize/permission race reasserting typing after completion.
                    if not self._should_refresh_typing(req, now):
                        await self._maybe_update_heartbeat(req, now)
                        continue
                    req.last_typing_at = now
                    try:
                        await req.typing_callback()
                    except Exception:
                        pass
                await self._maybe_update_heartbeat(req, now)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Typing keepalive loop crashed for user {user_id}: {e}", exc_info=True)
