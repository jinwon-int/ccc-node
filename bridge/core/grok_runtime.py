"""Experimental fixed-conversation Grok runtime; provider activation is separate.

Host tools/policies remain external. No approval is synthesized or auto-granted.
Local interruption retires the handle and retains the remote unknown outcome.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from .agent_runtime import (
    AgentEvent, ApprovalHandler, CompletionEvent, ErrorEvent, MessageCompletedEvent,
    ModelInfo, ResultEvent, SessionRequest, TextDeltaEvent, deny_approval,
)
from .grok_journal import GrokJournal
from .grok_protocol import (
    AcceptedPrompt, Baseline, HOST_VERSION, MAX_PROMPT, ProtocolError, _text, accepted_prompt,
    bound_reply, capture_baseline, check_host, check_idle,
)
from .turn_stall import register_turn_liveness

logger = logging.getLogger(__name__)


def _record_status(outcome: Any) -> str | None:
    """The acceptance record's ``status`` string, or None; never the record body."""
    if not isinstance(outcome, dict) or not isinstance(outcome.get("record"), dict):
        return None
    status = outcome["record"].get("status")
    return status if isinstance(status, str) else None


class GrokTransport(Protocol):
    destination: str
    agent_id: str

    async def call(self, operation: str, arguments: Any = None) -> Any: ...


class GrokTurnLiveness:
    """Turn-liveness source over a :class:`GrokRuntime`'s session (#1741).

    Last activity is the last completed host RPC (status/health/tail/send
    all ride ``GrokRuntime._call``). The engine is a remote host Bot with
    no locally observable process: no transport can prove it dead from
    here, so the verdict fails closed to "unknown" unless the transport
    itself exposes an honest ``engine_verdict`` — a hung Grok turn is
    logged by the stall probe but never auto-recovered on a guess.
    """

    def __init__(self, runtime: GrokRuntime) -> None:
        self._runtime = runtime

    def _session(self, session_id: str) -> GrokSession | None:
        session = self._runtime._session
        if session is None or session.closed:
            return None
        if session.session_id != session_id:
            return None
        return session

    def last_activity(self, session_id: str) -> float | None:
        if self._session(session_id) is None:
            return None
        return self._runtime._last_activity

    def engine_verdict(self, session_id: str) -> str:
        if self._session(session_id) is None:
            return "unknown"
        getter = getattr(self._runtime.transport, "engine_verdict", None)
        if not callable(getter):
            return "unknown"
        try:
            verdict = getter()
        except Exception:
            return "unknown"
        return verdict if verdict in ("alive", "dead", "unknown") else "unknown"


class GrokRuntime:
    def __init__(
        self,
        journal: GrokJournal,
        transport: GrokTransport,
        *,
        qualified_hosts: frozenset[str] | None = frozenset({HOST_VERSION}),
    ):
        self.journal, self.transport = journal, transport
        # Accepted host ids for ``status`` (None = capability-only); the id
        # the host actually reports is recorded on every journal revision.
        self.qualified_hosts = qualified_hosts
        self._session: GrokSession | None = None
        self._last_activity: float | None = None
        self._check_binding()
        # Provider-agnostic turn-stall coverage (#1741): one registry slot
        # per provider, so a newer GrokRuntime replaces this registration.
        register_turn_liveness("grok", GrokTurnLiveness(self))

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
        # A completed host RPC is the turn's last-activity signal (#1741).
        self._last_activity = time.monotonic()
        if operation == "status":
            self.journal.host_version = check_host(result, self.qualified_hosts)
        return result


class GrokSession:
    POLL_SECONDS = 2.0
    TURN_SECONDS = 180.0
    # Host f7045c4 persists the acceptance record asynchronously: a lookup
    # right after ``send`` can still be ``not-found`` (observed 2026-09-22,
    # record stamped 328 ms before the bridge gave up). Poll briefly before
    # treating the operation as uncertain; a rejection is final immediately.
    ACCEPTANCE_SECONDS = 15.0
    ACCEPTANCE_POLL_SECONDS = 0.5

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
                        # Host f7045c4 no longer answers ``{"accepted": true}`` on
                        # the proxied path although it accepts and runs the
                        # prompt. The durable acceptance record below is the
                        # authority; never resend, never invent a nonce.
                        logger.warning("Grok send response unconfirmed (keys=%s); consulting acceptance record",
                                       sorted(reply) if isinstance(reply, dict) else type(reply).__name__)
                elif current["prompt"] != message:
                    raise ProtocolError("grok_pending_input_mismatch")
                if current["stage"] == "attempted":
                    accepted = await self._await_acceptance(current["nonce"], message)
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
                    try:
                        result = bound_reply(accepted, message, baseline, page, health)
                    except ProtocolError as exc:
                        if str(exc) != "reply_pending":
                            raise
                        # Host f7045c4 keeps ``isBusy: false`` while the Bot is
                        # still writing; the echo (or a streaming row) is in
                        # the tail but no complete text yet. Wait, bounded by
                        # the surrounding TURN_SECONDS timeout.
                        await asyncio.sleep(self.POLL_SECONDS)
                        continue
                    return claim.complete(current, result)

    async def _await_acceptance(self, nonce: str, message: str) -> AcceptedPrompt:
        """Look up the acceptance record for ``nonce``, tolerating a short persistence lag.

        Only ``acceptance_uncertain`` (record not yet visible) is retried, and
        only until ``ACCEPTANCE_SECONDS``; a rejected or mismatched record
        raises at once. The nonce is never resent.
        """
        runtime = self.runtime
        binding = runtime.journal.binding
        deadline = time.monotonic() + self.ACCEPTANCE_SECONDS
        while True:
            outcome = await runtime._call("acceptance", {"nonce": nonce})
            try:
                return accepted_prompt(outcome, binding.agent_id, nonce, message)
            except ProtocolError as exc:
                # Transient: record not visible yet, or visible with the host's
                # ``pending`` status (GrokBotSendStatus.PENDING on f7045c4)
                # before it flips to ``accepted``. Rejected/mismatched is final.
                transient = str(exc) == "acceptance_uncertain" or (
                    str(exc) == "acceptance_not_accepted" and _record_status(outcome) == "pending")
                if not transient or time.monotonic() >= deadline:
                    raise
            await asyncio.sleep(self.ACCEPTANCE_POLL_SECONDS)

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
            except Exception as exc:
                self.closed = True
                # Categorical ProtocolError codes carry no body; other types
                # reveal only their class name. Without this line the failing
                # gate could only be found by reproducing the turn by hand.
                logger.warning("Grok operation denied or uncertain (%s); intent retained",
                               exc if isinstance(exc, ProtocolError) else type(exc).__name__)
                yield ErrorEvent("grok_outcome_unknown", "Grok operation denied or uncertain; retained intent requires reconciliation.")
            finally:
                self._task = None
