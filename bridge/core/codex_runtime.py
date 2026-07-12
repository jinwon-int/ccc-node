"""Provider-neutral runtime adapter for the Codex app-server protocol."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

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
    SessionRequest,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    deny_approval,
)
from .codex_app_server import (
    CodexAppServerClient,
    CodexNotification,
    CodexServerRequest,
    JsonValue,
    ServerRequestHandler,
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
    ) -> JsonValue: ...

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> JsonValue: ...

    async def list_models(self, *, include_hidden: bool = False) -> JsonValue: ...

    async def next_notification(self) -> CodexNotification: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[ServerRequestHandler], AppServerClient]


def _default_client_factory(handler: ServerRequestHandler) -> AppServerClient:
    return CodexAppServerClient(server_request_handler=handler)


@dataclass(slots=True)
class _ActiveTurn:
    queue: asyncio.Queue[AgentEvent]
    approval_handler: ApprovalHandler
    turn_id: str | None = None
    finished: bool = False
    pending_notifications: list[CodexNotification] = field(default_factory=list)


class CodexSession:
    """One provider-neutral session backed by a Codex thread."""

    def __init__(
        self,
        runtime: CodexRuntime,
        thread_id: str,
        model: str | None,
        turn_lock: asyncio.Lock,
    ) -> None:
        self._runtime = runtime
        self._thread_id = thread_id
        self._model = model
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
                    )
                    active.turn_id = self._runtime._turn_id(result)
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

    def __init__(self, *, client_factory: ClientFactory = _default_client_factory) -> None:
        self._client = client_factory(self._handle_server_request)
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
        return CodexSession(self, thread_id, request.model, turn_lock)

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
            models.append(ModelInfo(id=model_id, display_name=display_name))
        return tuple(models)

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
        if (
            active is None
            or active.finished
            or not isinstance(turn_id, str)
            or active.turn_id != turn_id
        ):
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
        except (asyncio.CancelledError, Exception):
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
