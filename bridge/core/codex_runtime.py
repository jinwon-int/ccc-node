"""Provider-neutral runtime adapter for the Codex app-server protocol."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from typing import Literal, Protocol, cast

from .agent_runtime import (
    AgentEvent,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequestEvent,
    CompletionEvent,
    ErrorEvent,
    JsonValue as AgentJsonValue,
    ModelInfo,
    ReasoningDeltaEvent,
    ResultEvent,
    SessionHistory,
    SessionHistoryMessage,
    SessionRequest,
    SessionSummary,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    deny_approval,
)
from .codex_app_server import (
    CodexAppServerClient,
    CodexNotification,
    CodexServerRequest,
    CodexThread,
    CodexThreadListPage,
    JsonValue,
    ServerRequestHandler,
)
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    TranscriptBounds,
    TranscriptMessage,
)


class AppServerClient(Protocol):
    async def start(self) -> JsonValue: ...

    async def thread_start(self, *, cwd: str, model: str | None = None) -> JsonValue: ...

    async def thread_resume(
        self,
        thread_id: str,
        *,
        cwd: str | None = None,
        model: str | None = None,
    ) -> JsonValue: ...

    async def turn_start(
        self,
        thread_id: str,
        input_items: Sequence[Mapping[str, JsonValue]],
        *,
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue: ...

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> JsonValue: ...

    async def list_models(self, *, include_hidden: bool = False) -> JsonValue: ...

    async def thread_list(
        self, *, limit: int = 20, cursor: str | None = None
    ) -> CodexThreadListPage: ...

    async def thread_read(
        self, thread_id: str, *, include_turns: bool = True
    ) -> CodexThread | None: ...

    async def next_notification(self) -> CodexNotification: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[ServerRequestHandler], AppServerClient]


@dataclass(slots=True)
class _ActiveTurn:
    queue: asyncio.Queue[AgentEvent]
    approval_handler: ApprovalHandler
    turn_id: str | None = None
    turn_ready: asyncio.Event = field(default_factory=asyncio.Event)
    finished: bool = False
    pending_notifications: list[CodexNotification] = field(default_factory=list)


class CodexSession:
    """One provider-neutral session backed by a Codex thread."""

    def __init__(
        self,
        runtime: CodexRuntime,
        thread_id: str,
        model: str | None,
        effort: str | None,
        approval_policy: str | None,
        approvals_reviewer: str | None,
        sandbox_policy: Mapping[str, AgentJsonValue] | None,
        turn_lock: asyncio.Lock,
    ) -> None:
        self._runtime = runtime
        self._thread_id = thread_id
        self._model = model
        self._effort = effort
        self._approval_policy = approval_policy
        self._approvals_reviewer = approvals_reviewer
        self._sandbox_policy = cast(Mapping[str, JsonValue] | None, sandbox_policy)
        self._turn_lock = turn_lock

    @property
    def session_id(self) -> str:
        return self._thread_id

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def events() -> AsyncIterator[AgentEvent]:
            async with self._turn_lock:
                active = _ActiveTurn(asyncio.Queue(), approval_handler)
                self._runtime._active_turns[self._thread_id] = active
                try:
                    result = await self._runtime._client.turn_start(
                        self._thread_id,
                        [{"type": "text", "text": message}],
                        model=self._model,
                        effort=self._effort,
                        approval_policy=self._approval_policy,
                        approvals_reviewer=self._approvals_reviewer,
                        sandbox_policy=self._sandbox_policy,
                    )
                    returned_turn_id = self._runtime._turn_id(result)
                    if active.turn_id is not None and active.turn_id != returned_turn_id:
                        raise RuntimeError("Codex approval turn does not match turn/start response")
                    active.turn_id = returned_turn_id
                    active.turn_ready.set()
                    self._runtime._flush_pending_notifications(active)
                    while True:
                        event = await active.queue.get()
                        yield event
                        if isinstance(event, (CompletionEvent, ErrorEvent)):
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    yield ErrorEvent(
                        code="codex_runtime_error",
                        message=str(exc) or "Codex runtime request failed",
                    )
                finally:
                    active.finished = True
                    active.turn_ready.set()
                    if self._runtime._active_turns.get(self._thread_id) is active:
                        self._runtime._active_turns.pop(self._thread_id, None)

        return events()

    async def interrupt(self) -> None:
        active = self._runtime._active_turns.get(self._thread_id)
        if active is None or active.turn_id is None or active.finished:
            return
        await self._runtime._client.turn_interrupt(self._thread_id, active.turn_id)


class CodexRuntime:
    """Own a shared Codex app-server client and its notification dispatcher."""

    def __init__(
        self,
        *,
        cli_path: str = "codex",
        client_factory: ClientFactory | None = None,
    ) -> None:
        if not cli_path.strip():
            raise ValueError("Codex CLI path must not be empty")
        factory = client_factory or (
            lambda handler: CodexAppServerClient(
                executable=cli_path,
                server_request_handler=handler,
            )
        )
        self._client = factory(self._handle_server_request)
        self._start_lock = asyncio.Lock()
        self._started = False
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._active_turns: dict[str, _ActiveTurn] = {}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("Codex runtime is closed")
            if self._started:
                return
            await self._client.start()
            self._dispatcher_task = asyncio.create_task(self._dispatch_notifications())
            self._started = True

    async def start_or_resume(self, request: SessionRequest) -> CodexSession:
        await self._ensure_started()
        if request.session_id is None:
            result = await self._client.thread_start(
                cwd=request.working_directory,
                model=request.model,
            )
        else:
            result = await self._client.thread_resume(
                request.session_id,
                cwd=request.working_directory,
                model=request.model,
            )
        thread_id = self._thread_id(result)
        turn_lock = self._thread_locks.setdefault(thread_id, asyncio.Lock())
        return CodexSession(
            self,
            thread_id,
            request.model,
            request.effort,
            request.approval_policy,
            request.approvals_reviewer,
            request.sandbox_policy,
            turn_lock,
        )

    async def list_models(self) -> Sequence[ModelInfo]:
        await self._ensure_started()
        result = await self._client.list_models()
        if not isinstance(result, Mapping):
            return ()
        data = result.get("data")
        if not isinstance(data, (list, tuple)):
            return ()
        models: list[ModelInfo] = []
        for value in data:
            if not isinstance(value, Mapping):
                continue
            model_id = value.get("id")
            display_name = value.get("displayName", value.get("name"))
            if not isinstance(model_id, str) or not model_id:
                continue
            if not isinstance(display_name, str) or not display_name:
                continue
            raw_efforts = value.get("supportedReasoningEfforts")
            efforts: list[str] = []
            if isinstance(raw_efforts, (list, tuple)):
                for option in raw_efforts:
                    if not isinstance(option, Mapping):
                        continue
                    effort = option.get("reasoningEffort")
                    if isinstance(effort, str) and effort and effort not in efforts:
                        efforts.append(effort)
            default_effort = value.get("defaultReasoningEffort")
            if not isinstance(default_effort, str) or not default_effort:
                default_effort = None
            models.append(
                ModelInfo(
                    id=model_id,
                    display_name=display_name,
                    default_reasoning_effort=default_effort,
                    supported_reasoning_efforts=tuple(efforts),
                    is_default=value.get("isDefault") is True,
                )
            )
        return tuple(models)

    @property
    def supports_session_browsing(self) -> bool:
        return True

    async def list_sessions(
        self,
        *,
        limit: int = 10,
        max_pages: int = 5,
    ) -> Sequence[SessionSummary]:
        """Return a bounded list of app-server threads."""

        if limit <= 0 or max_pages <= 0:
            return ()
        await self._ensure_started()
        bounded_limit = min(limit, 100)
        bounded_pages = min(max_pages, 5)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        summaries: list[SessionSummary] = []
        for _ in range(bounded_pages):
            page = await self._client.thread_list(
                limit=min(20, bounded_limit - len(summaries)),
                cursor=cursor,
            )
            for value in page.data:
                if len(summaries) >= bounded_limit:
                    break
                summaries.append(
                    SessionSummary(
                        id=value.id,
                        title=value.title,
                        preview=value.preview,
                        updated_at=value.updated_at,
                        cwd=value.cwd,
                        model=value.model,
                    )
                )
            if len(summaries) >= bounded_limit or page.next_cursor is None:
                break
            if page.next_cursor in seen_cursors:
                break
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        return tuple(summaries)

    async def read_session(self, session_id: str, *, limit: int = 5) -> SessionHistory:
        """Return only bounded user and assistant text from ``thread/read``."""

        if not session_id:
            raise ValueError("session id must not be empty")
        if limit <= 0:
            return SessionHistory(session_id, ())
        await self._ensure_started()
        thread = await self._client.thread_read(session_id, include_turns=True)
        if thread is None:
            return SessionHistory(session_id, ())
        messages: list[SessionHistoryMessage] = []
        for turn in thread.turns[-100:]:
            timestamp = self._history_timestamp(turn.get("createdAt"))
            items = turn.get("items")
            if not isinstance(items, (list, tuple)):
                continue
            for item in items[:200]:
                if not isinstance(item, Mapping):
                    continue
                item_type = item.get("type")
                role: Literal["user", "assistant"]
                text: object
                if item_type == "userMessage":
                    text = self._user_message_text(item.get("content"))
                    role = "user"
                elif item_type == "agentMessage":
                    text = item.get("text")
                    role = "assistant"
                else:
                    continue
                if not isinstance(text, str):
                    continue
                bounded_text = text[:2000].strip()
                if not bounded_text:
                    continue
                item_timestamp = self._history_timestamp(item.get("timestamp")) or timestamp
                messages.append(
                    SessionHistoryMessage(role, bounded_text, item_timestamp)
                )
        return SessionHistory(session_id, tuple(messages[-min(limit, 50):]))

    async def read_session_snapshot(
        self,
        session_id: str,
        *,
        bounds: TranscriptBounds | None = None,
        now: datetime | None = None,
    ) -> CodexTranscriptSnapshot:
        """Return a strict, read-only and byte-bounded Codex transcript snapshot."""

        if not session_id:
            raise ValueError("session id must not be empty")
        limits = bounds or TranscriptBounds()
        captured = now or datetime.now(timezone.utc)
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        captured = captured.astimezone(timezone.utc)
        await self._ensure_started()
        thread = await self._client.thread_read(session_id, include_turns=True)
        thread_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        if thread is None:
            return CodexTranscriptSnapshot(
                thread_hash=thread_hash,
                last_turn_id=None,
                messages=(),
                byte_count=0,
                truncated=False,
                captured_at=self._format_snapshot_time(captured),
            )

        newest_messages, last_turn_id, structural_truncation = (
            self._snapshot_candidates(thread, limits, captured)
        )
        messages, byte_count, byte_truncation = self._bound_snapshot_messages(
            newest_messages, limits
        )
        return CodexTranscriptSnapshot(
            thread_hash=thread_hash,
            last_turn_id=last_turn_id,
            messages=messages,
            byte_count=byte_count,
            truncated=structural_truncation or byte_truncation,
            captured_at=self._format_snapshot_time(captured),
        )

    @classmethod
    def _snapshot_candidates(
        cls,
        thread: CodexThread,
        limits: TranscriptBounds,
        captured: datetime,
    ) -> tuple[list[TranscriptMessage], str | None, bool]:
        turns = tuple(thread.turns)
        selected_turns = turns[-limits.max_turns :]
        truncated = len(turns) > limits.max_turns
        last_turn_id: str | None = None
        if selected_turns:
            raw_last_turn_id = selected_turns[-1].get("id")
            if isinstance(raw_last_turn_id, str) and raw_last_turn_id:
                last_turn_id = raw_last_turn_id

        newest_messages: list[TranscriptMessage] = []
        items_seen = 0
        for turn in reversed(selected_turns):
            turn_timestamp = cls._history_timestamp(turn.get("createdAt"))
            items = turn.get("items")
            if not isinstance(items, (list, tuple)):
                continue
            for item in reversed(items):
                if items_seen >= limits.max_items:
                    return newest_messages, last_turn_id, True
                items_seen += 1
                message, excluded_by_age = cls._snapshot_item(
                    item, turn_timestamp, captured, limits.max_age_seconds
                )
                truncated = truncated or excluded_by_age
                if message is None:
                    continue
                newest_messages.append(message)
                if len(newest_messages) >= limits.max_messages:
                    return newest_messages, last_turn_id, True
        return newest_messages, last_turn_id, truncated

    @classmethod
    def _snapshot_item(
        cls,
        item: object,
        turn_timestamp: str | None,
        captured: datetime,
        max_age_seconds: int,
    ) -> tuple[TranscriptMessage | None, bool]:
        if not isinstance(item, Mapping):
            return None, False
        item_type = item.get("type")
        role: Literal["user", "assistant"]
        text: object
        if item_type == "userMessage":
            text = cls._user_message_text(item.get("content"))
            role = "user"
        elif item_type == "agentMessage":
            text = item.get("text")
            role = "assistant"
        else:
            return None, False
        if not isinstance(text, str) or not (text := text.strip()):
            return None, False
        timestamp = cls._history_timestamp(item.get("timestamp")) or turn_timestamp
        parsed_timestamp = cls._parse_snapshot_time(timestamp)
        if parsed_timestamp is None:
            return None, True
        if (captured - parsed_timestamp).total_seconds() > max_age_seconds:
            return None, True
        return TranscriptMessage(role, text, timestamp), False

    @classmethod
    def _bound_snapshot_messages(
        cls,
        newest_messages: list[TranscriptMessage],
        limits: TranscriptBounds,
    ) -> tuple[tuple[TranscriptMessage, ...], int, bool]:
        bounded_newest: list[TranscriptMessage] = []
        remaining_bytes = limits.max_bytes
        truncated = False
        for message in newest_messages:
            if remaining_bytes <= 0:
                truncated = True
                break
            allowed_bytes = min(remaining_bytes, limits.max_message_bytes)
            bounded_text, was_truncated = cls._truncate_utf8(
                message.text, allowed_bytes
            )
            truncated = truncated or was_truncated
            if not bounded_text:
                continue
            bounded_newest.append(
                TranscriptMessage(message.role, bounded_text, message.timestamp)
            )
            remaining_bytes -= len(bounded_text.encode("utf-8"))
        messages = tuple(reversed(bounded_newest))
        byte_count = sum(len(message.text.encode("utf-8")) for message in messages)
        return messages, byte_count, truncated

    @staticmethod
    def _truncate_utf8(value: str, maximum_bytes: int) -> tuple[str, bool]:
        payload = value.encode("utf-8")
        if len(payload) <= maximum_bytes:
            return value, False
        return payload[:maximum_bytes].decode("utf-8", errors="ignore").strip(), True

    @staticmethod
    def _parse_snapshot_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _format_snapshot_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _user_message_text(content: JsonValue) -> str | None:
        if isinstance(content, str):
            return content[:2000]
        if not isinstance(content, (list, tuple)):
            return None
        parts: list[str] = []
        for block in content[:50]:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") not in {"text", "input_text"}:
                continue
            text = block.get("text")
            if isinstance(text, str):
                bounded_text = text[:2000].strip()
                if bounded_text:
                    parts.append(bounded_text)
        return "\n".join(parts) or None

    @staticmethod
    def _history_timestamp(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_active_turns("codex_runtime_closed", "Codex runtime closed")
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            await asyncio.gather(self._dispatcher_task, return_exceptions=True)
        await self._client.close()

    async def _dispatch_notifications(self) -> None:
        try:
            while True:
                notification = await self._client.next_notification()
                try:
                    self._route_notification(notification)
                except (TypeError, ValueError):
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_active_turns("codex_connection_failed", str(exc))

    def _route_notification(self, notification: CodexNotification) -> None:
        params = notification.params
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        active = self._active_turns.get(thread_id)
        if active is None or active.finished:
            return
        if active.turn_id is None:
            active.pending_notifications.append(notification)
            return
        turn_id = self._notification_turn_id(params)
        if active.turn_id is not None and turn_id is not None and active.turn_id != turn_id:
            return

        event: AgentEvent | None = None
        if notification.method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                event = TextDeltaEvent(delta)
        elif notification.method in {
            "item/reasoning/textDelta",
            "item/reasoning/summaryTextDelta",
        }:
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                event = ReasoningDeltaEvent(delta)
        elif notification.method in {"item/started", "item/completed"}:
            event = self._tool_event(notification.method, params)
        elif notification.method == "turn/completed":
            self._complete_turn(active, params)
            return
        if event is not None:
            active.queue.put_nowait(event)

    def _flush_pending_notifications(self, active: _ActiveTurn) -> None:
        pending = tuple(active.pending_notifications)
        active.pending_notifications.clear()
        for notification in pending:
            self._route_notification(notification)

    @staticmethod
    def _tool_event(method: str, params: Mapping[str, JsonValue]) -> AgentEvent | None:
        item = params.get("item")
        if not isinstance(item, Mapping):
            return None
        item_id = item.get("id")
        tool_name = item.get("type")
        if not isinstance(item_id, str) or not item_id:
            return None
        if not isinstance(tool_name, str) or not tool_name:
            return None
        if tool_name in {
            "agentMessage",
            "userMessage",
            "reasoning",
            "plan",
            "enteredReviewMode",
            "exitedReviewMode",
        }:
            return None
        snapshot = cast(Mapping[str, AgentJsonValue], item)
        if method == "item/started":
            return ToolStartedEvent(item_id, tool_name, snapshot)
        status = item.get("status")
        exit_code = item.get("exitCode")
        success = status in {"completed", "success"} and exit_code in {None, 0}
        return ToolCompletedEvent(item_id, tool_name, snapshot, success)

    @staticmethod
    def _complete_turn(active: _ActiveTurn, params: Mapping[str, JsonValue]) -> None:
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            return
        status = turn.get("status")
        if status in {"completed", "success"}:
            active.queue.put_nowait(ResultEvent(cast(AgentJsonValue, turn)))
            active.queue.put_nowait(CompletionEvent("end_turn"))
        elif status in {"interrupted", "cancelled"}:
            active.queue.put_nowait(ErrorEvent("interrupted", "Codex turn was interrupted"))
        else:
            error = turn.get("error")
            message = (str(error) if error is not None else "") or "Codex turn failed"
            active.queue.put_nowait(ErrorEvent("codex_turn_failed", message))
        active.finished = True

    def _fail_active_turns(self, code: str, message: str) -> None:
        normalized_message = message or "Codex connection failed"
        for active in tuple(self._active_turns.values()):
            if not active.finished:
                active.queue.put_nowait(ErrorEvent(code, normalized_message))

    async def _handle_server_request(
        self,
        request: CodexServerRequest,
    ) -> Mapping[str, JsonValue]:
        approval_methods = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }
        if request.method not in approval_methods:
            return self._unsupported_server_request()

        thread_id = request.params.get("threadId")
        turn_id = request.params.get("turnId")
        active: _ActiveTurn | None = None
        if isinstance(thread_id, str):
            active = self._active_turns.get(thread_id)
        if active is None or active.finished or not isinstance(turn_id, str):
            return self._approval_response(request.method, ApprovalDecision.DENY, request.params)
        if active.turn_id is None:
            # A server request task can be scheduled before turn/start returns.
            # Wait for the exact returned turn ID before exposing approval UI or
            # sending an allow decision. A missing response remains fail-closed.
            try:
                await asyncio.wait_for(active.turn_ready.wait(), timeout=5.0)
            except TimeoutError:
                return self._approval_response(
                    request.method, ApprovalDecision.DENY, request.params
                )
        if active.finished or active.turn_id != turn_id:
            return self._approval_response(request.method, ApprovalDecision.DENY, request.params)

        approval = ApprovalRequestEvent(
            request_id=str(request.id),
            action=request.method,
            arguments=cast(Mapping[str, AgentJsonValue], request.params),
            description=f"Codex requests approval for {request.method.rsplit('/', 1)[0]}",
        )
        active.queue.put_nowait(approval)
        try:
            decision = await active.approval_handler(approval)
        except asyncio.CancelledError:
            raise
        except Exception:
            decision = ApprovalDecision.DENY
        if active.finished or self._active_turns.get(cast(str, thread_id)) is not active:
            decision = ApprovalDecision.DENY
        return self._approval_response(request.method, decision, request.params)

    @staticmethod
    def _approval_response(
        method: str,
        decision: ApprovalDecision,
        params: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        if method == "item/permissions/requestApproval":
            permissions = params.get("permissions")
            allowed = permissions if decision is ApprovalDecision.ALLOW else {}
            if not isinstance(allowed, Mapping):
                allowed = {}
            return {"result": {"permissions": dict(allowed), "scope": "turn"}}
        provider_decision = "accept" if decision is ApprovalDecision.ALLOW else "decline"
        return {"result": {"decision": provider_decision}}

    @staticmethod
    def _unsupported_server_request() -> Mapping[str, JsonValue]:
        return {
            "error": {
                "code": -32601,
                "message": "Client does not support server request",
            }
        }

    @staticmethod
    def _thread_id(result: JsonValue) -> str:
        if not isinstance(result, Mapping):
            raise RuntimeError("Codex thread response is malformed")
        thread = result.get("thread")
        if not isinstance(thread, Mapping):
            raise RuntimeError("Codex thread response is missing thread")
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("Codex thread response has invalid thread id")
        return thread_id

    @staticmethod
    def _turn_id(result: JsonValue) -> str:
        if not isinstance(result, Mapping):
            raise RuntimeError("Codex turn response is malformed")
        turn = result.get("turn")
        if not isinstance(turn, Mapping):
            raise RuntimeError("Codex turn response is missing turn")
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise RuntimeError("Codex turn response has invalid turn id")
        return turn_id

    @staticmethod
    def _notification_turn_id(params: Mapping[str, JsonValue]) -> str | None:
        turn_id = params.get("turnId")
        if isinstance(turn_id, str):
            return turn_id
        turn = params.get("turn")
        if isinstance(turn, Mapping):
            nested_id = turn.get("id")
            if isinstance(nested_id, str):
                return nested_id
        return None
