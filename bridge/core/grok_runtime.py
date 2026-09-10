"""Experimental fixed-conversation Grok runtime; provider activation is separate.

Host tools/policies remain external. No approval is synthesized or auto-granted.
Local interruption retires the handle and retains the remote unknown outcome.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from .agent_runtime import (
    AgentEvent, ApprovalHandler, CompletionEvent, ErrorEvent, MessageCompletedEvent,
    ModelInfo, ResultEvent, SessionRequest, TextDeltaEvent, deny_approval,
)
from .grok_journal import GrokJournal
from .grok_protocol import (
    AcceptedPrompt, Baseline, MAX_PROMPT, ProtocolError, _text, accepted_prompt,
    bound_reply, capture_baseline, check_host, check_idle,
)


class GrokTransport(Protocol):
    destination: str
    agent_id: str

    async def call(self, operation: str, arguments: Any = None) -> Any: ...


class GrokRuntime:
    def __init__(self, journal: GrokJournal, transport: GrokTransport):
        self.journal, self.transport = journal, transport
        self._session: GrokSession | None = None
        self._check_binding()

    def _check_binding(self) -> None:
        if (self.transport.destination != self.journal.binding.destination
                or self.transport.agent_id != self.journal.binding.agent_id):
            raise ProtocolError("grok_transport_binding_mismatch")

    async def list_models(self) -> Sequence[ModelInfo]:
        return ()  # existing Bot model is externally configured, not a model API

    async def start_or_resume(self, request: SessionRequest) -> GrokSession:
        self._check_binding()
        binding = self.journal.binding
        # Initial attachment uses the explicit persisted binding's session ID.
        # Never interpret /new or an arbitrary local UUID as a fresh Bot chat.
        if request.session_id != binding.session_id or request.working_directory != binding.working_directory:
            raise ProtocolError("grok_fixed_conversation_required")
        if any(getattr(request, key) is not None for key in (
                "model", "effort", "approval_policy", "approvals_reviewer", "sandbox_policy", "memory_environment")):
            raise ProtocolError("grok_session_option_unsupported")
        with self.journal.claim() as claim:
            claim.load()  # never initializes missing state on open
        await self._call("status")
        if self._session is None or self._session.closed:
            self._session = GrokSession(self)
        return self._session

    async def _call(self, operation: str, arguments: Any = None) -> Any:
        self._check_binding()
        result = await self.transport.call(operation, arguments)
        self._check_binding()
        if operation == "status":
            check_host(result)
        return result


class GrokSession:
    POLL_SECONDS = 2.0
    TURN_SECONDS = 180.0

    def __init__(self, runtime: GrokRuntime):
        self.runtime = runtime
        self.closed = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[dict[str, Any]] | None = None
        self._interrupted = False

    @property
    def session_id(self) -> str:
        return self.runtime.journal.binding.session_id

    async def interrupt(self) -> None:
        if self._task is not None:
            self._interrupted = True
            self.closed = True
            self._task.cancel()  # never invokes host-global interrupt

    async def close(self) -> None:
        self.closed = True
        await self.interrupt()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _execute(self, message: str) -> dict[str, Any]:
        runtime = self.runtime
        binding = runtime.journal.binding
        async with asyncio.timeout(self.TURN_SECONDS):
            # Nonblocking process claim is the local operation lease. It does
            # not lock the host/app, and it never auto-expires/requeues sends.
            with runtime.journal.claim() as claim:
                current, count, _ = claim.load()
                await runtime._call("status")
                if current is not None and current["stage"] == "complete" and current["prompt"] == message:
                    check_idle(await runtime._call("health"), binding.agent_id)
                    claim.load()
                    return current  # conservative identical-last-input replay
                if current is None or current["stage"] == "complete":
                    if count > 94:
                        raise ProtocolError("grok_history_limit")
                    check_idle(await runtime._call("health"), binding.agent_id)
                    baseline = capture_baseline(await runtime._call("tail"))
                    check_idle(await runtime._call("health"), binding.agent_id)
                    current = claim.attempt(message, baseline)
                    # The attempted record is durable even if killed before
                    # send. Reopening only queries this nonce; never resends.
                    reply = await runtime._call("send", {"nonce": current["nonce"], "prompt": message})
                    if not isinstance(reply, dict) or reply.get("accepted") is not True:
                        raise ProtocolError("grok_send_uncertain")
                elif current["prompt"] != message:
                    raise ProtocolError("grok_pending_input_mismatch")
                if current["stage"] == "attempted":
                    accepted = accepted_prompt(await runtime._call("acceptance", {"nonce": current["nonce"]}),
                                               binding.agent_id, current["nonce"], message)
                    current = claim.accept(current, accepted)
                accepted = AcceptedPrompt(**current["accepted"])
                baseline_value = current["baseline"]
                baseline = Baseline(baseline_value["last_id"], tuple(baseline_value["request_ids"]))
                while True:
                    health = await runtime._call("health")
                    if (isinstance(health, dict) and health.get("ok") is True
                            and health.get("activeAgentId") == binding.agent_id
                            and health.get("isBusy") is True
                            and health.get("busyOnlyAwaitingApproval") is False):
                        await asyncio.sleep(self.POLL_SECONDS)
                        continue
                    check_idle(health, binding.agent_id)
                    page = await runtime._call("tail")
                    # Recheck after the transcript fetch; this is observation,
                    # not distributed CAS. Interference denies the full range.
                    health = await runtime._call("health")
                    result = bound_reply(accepted, message, baseline, page, health)
                    return claim.complete(current, result)

    async def send_turn(self, message: str, *, approval_handler: ApprovalHandler = deny_approval) -> AsyncIterator[AgentEvent]:
        del approval_handler  # unsupported; no decision is sent to the host
        async with self._lock:
            if self.closed:
                yield ErrorEvent("grok_session_retired", "Grok session is retired; explicit reopen required.")
                return
            self._interrupted = False
            try:
                _text(message, MAX_PROMPT, "invalid_prompt")
                self._task = asyncio.create_task(self._execute(message))
                result = await self._task
                if self.closed:
                    raise ProtocolError("grok_session_retired")
                events: list[AgentEvent] = []
                for text in result["reply"]["texts"]:
                    events.extend((TextDeltaEvent(text), MessageCompletedEvent()))
                events.extend((ResultEvent({"text": "\n".join(result["reply"]["texts"])}), CompletionEvent("end_turn")))
                for event in events:
                    if self._interrupted:
                        raise asyncio.CancelledError
                    if self.closed:
                        raise ProtocolError("grok_session_retired")
                    yield event
            except asyncio.CancelledError:
                self.closed = True
                if not self._interrupted:
                    raise
                yield ErrorEvent("grok_interrupted_outcome_unknown", "Local wait cancelled; remote outcome retained, not stopped.")
            except Exception:
                self.closed = True
                yield ErrorEvent("grok_outcome_unknown", "Grok operation denied or uncertain; retained intent requires reconciliation.")
            finally:
                self._task = None
