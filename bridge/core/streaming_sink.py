"""Transport-neutral streaming sink contract (Matrix frontend groundwork, #1780).

``ProjectChatHandler.process_message`` drives draft/tool progress through five
duck-typed methods that ``core/streaming.py`` ``StreamingMessageHandler``
(Telegram) happens to implement. This module names that contract so a second
frontend can inject its own sink instead of the handler constructing the
Telegram one from a ``bot`` object. Behaviour for Telegram callers is
unchanged: when no sink is injected the legacy ``bot``-based construction still
runs.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StreamingSinkPort(Protocol):
    """Draft/progress sink consumed by ``process_message``.

    Every method returns ``True`` when something visible was delivered so the
    handler can decide whether the final reply was already streamed.
    """

    async def update_if_needed(self, new_text_chunk: str) -> bool: ...

    async def add_tool_call(self, name: str, input: dict[str, Any]) -> bool: ...

    async def finalize_segment(self) -> bool: ...

    async def finalize_all(self) -> bool: ...

    async def cancel(self) -> bool: ...


_REQUIRED = ("update_if_needed", "add_tool_call", "finalize_segment", "finalize_all", "cancel")


def validate_streaming_sink(sink: Any) -> Any:
    """Fail fast on a partial sink instead of crashing mid-turn.

    A sink missing a method would surface as an ``AttributeError`` only when
    the corresponding runtime event arrives (possibly after tool execution
    started), so check the whole contract before the turn begins.
    """

    missing = [name for name in _REQUIRED if not callable(getattr(sink, name, None))]
    if missing:
        raise TypeError("streaming sink lacks required methods: " + ", ".join(missing))
    return sink
