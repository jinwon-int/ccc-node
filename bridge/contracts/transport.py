"""Channel-neutral turn transport contract (#2380 PR-1).

A chat frontend ("transport") admits one inbound message as a *job*, hands it to
a :class:`TurnRunner`, and gives that runner a :class:`TurnSink` so the running
turn can talk back to its conversation. The runner answers with a
:class:`TurnResult`. The Matrix frontend was the first to code against these
shapes; they lived in ``core/matrix/transport.py`` with duck-typed mirrors in
``core/matrix/bot.py`` and ``core/grok_matrix_bot.py``. They are relocated
here, unchanged, so a second channel (the family messenger's native relay,
family-messenger#420) implements the same seam instead of a fourth copy.

Pure relocation, as ``contracts/agent_runtime.py`` was in #1756: no method, no
signature, no field and no default changed. Everything the Matrix modules used
to define keeps its old import path as a re-export.

This module must stay dependency-light: no ``nio``, no ``telegram``, no bridge
configuration. A channel's own types never appear in these signatures — a job
is an opaque ``Mapping``; room, sender and event identity are the transport's
business.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class TurnSink(Protocol):
    """What a running turn may do to its conversation while it runs.

    A transport binds one sink to one claimed job; the sink goes inert once that
    turn is no longer the active one, so a late callback cannot write into a
    conversation that has moved on.
    """

    async def typing(self) -> None:
        """Best-effort typing indicator; errors are swallowed."""
        ...

    async def interim(self, text: str) -> None:
        """Durable progress notice, delivered like any reply (outbox, retries)."""
        ...

    async def status(self, text: str | None) -> None:
        """Progress bubble: created once per turn, edited in place, cleared when done.

        ``None`` clears it. Channels without editable messages may implement
        this as a no-op; the runner must not depend on it being visible.
        """
        ...

    async def approval(self, description: str, arguments: Any) -> bool:
        """Ask the conversation; ``True`` only when an explicit approval arrives in time.

        Fail closed: a timeout, a cancelled turn, or a channel that cannot ask
        all answer ``False``.
        """
        ...


@dataclass(frozen=True)
class TurnResult:
    """What a runner hands back for one job.

    ``streamed`` says the text already reached the conversation incrementally
    (the transport then only closes the turn). ``status`` is ``"complete"`` or
    ``"uncertain"``; an uncertain turn is never re-run automatically.
    """

    text: str
    session_id: str | None
    streamed: bool = False
    status: str = "complete"  # or "uncertain"


class TurnRunner(Protocol):
    """``run``/``cancel`` are required. Optional hooks, looked up with ``getattr``:
    ``stop_idle(job)``, and ``turn_closed(job)`` once a turn's reply row is
    queued and ``delivered(job)`` after an outbox row went out — both run in
    the background and fail open.
    """

    async def run(
        self,
        job: Mapping[str, Any],
        *,
        sink: TurnSink,
        session_id: str | None,
        room_kind: str,
    ) -> TurnResult: ...

    async def cancel(self, job: Mapping[str, Any]) -> bool: ...


class TransportPort(Protocol):
    """The transport as the frontend sees it: lifecycle plus two room-level calls.

    ``room_id`` is the channel's conversation identifier (a Matrix room id, a
    relay room name, ...); ``room_kind`` classifies it for policy
    (``"direct"``, ``"family"``, ...). ``msgtype`` keeps the Matrix spelling
    for the quiet-notice distinction (``m.text`` vs ``m.notice``); a channel
    without that distinction ignores it.
    """

    async def open(self, initialize: bool = False) -> None: ...

    async def run(self) -> None: ...

    async def close(self) -> None: ...

    def enqueue_notice(
        self, room_id: str, text: str, *, key: str | None = None, msgtype: str = "m.text"
    ) -> None: ...

    def room_kind(self, room_id: str) -> str: ...


__all__ = ["TransportPort", "TurnResult", "TurnRunner", "TurnSink"]
