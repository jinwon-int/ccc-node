"""Provider-neutral contracts for agent runtimes, sessions, and events.

This module intentionally contains no provider adapters.  It defines the seam a
future runtime can implement without exposing provider SDK objects to bridge
callers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol, TypeAlias, runtime_checkable

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


class ApprovalDecision(str, Enum):
    """An explicit response to a provider-neutral approval request."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class TextDeltaEvent:
    """A non-empty increment of user-visible assistant text."""

    text: str
    kind: Literal["text_delta"] = "text_delta"

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("text delta must not be empty")


@dataclass(frozen=True, slots=True)
class ReasoningDeltaEvent:
    """A non-empty increment of provider-normalized reasoning text."""

    text: str
    kind: Literal["reasoning_delta"] = "reasoning_delta"

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("reasoning delta must not be empty")


@dataclass(frozen=True, slots=True)
class ApprovalRequestEvent:
    """A request that cannot proceed without an explicit approval decision."""

    request_id: str
    action: str
    arguments: Mapping[str, JsonValue]
    description: str
    kind: Literal["approval_request"] = "approval_request"

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("approval request id must not be empty")
        if not self.action:
            raise ValueError("approval action must not be empty")
        if not self.description:
            raise ValueError("approval description must not be empty")


@dataclass(frozen=True, slots=True)
class CompletionEvent:
    """The provider has finished generating the current turn."""

    stop_reason: str
    kind: Literal["completion"] = "completion"

    def __post_init__(self) -> None:
        if not self.stop_reason:
            raise ValueError("completion stop reason must not be empty")


@dataclass(frozen=True, slots=True)
class ResultEvent:
    """The normalized result of a successfully completed turn."""

    result: JsonValue
    kind: Literal["result"] = "result"


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    """A provider-normalized terminal runtime error."""

    code: str
    message: str
    retryable: bool = False
    kind: Literal["error"] = "error"

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("error code must not be empty")
        if not self.message:
            raise ValueError("error message must not be empty")


AgentEvent: TypeAlias = (
    TextDeltaEvent
    | ReasoningDeltaEvent
    | ApprovalRequestEvent
    | CompletionEvent
    | ResultEvent
    | ErrorEvent
)
ApprovalHandler: TypeAlias = Callable[[ApprovalRequestEvent], Awaitable[ApprovalDecision]]


async def deny_approval(_request: ApprovalRequestEvent) -> ApprovalDecision:
    """Fail-closed approval handler used whenever a caller supplies none."""

    return ApprovalDecision.DENY


@dataclass(frozen=True, slots=True)
class SessionRequest:
    """Provider-neutral inputs for starting or resuming an agent session."""

    working_directory: str
    session_id: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if not self.working_directory:
            raise ValueError("working directory must not be empty")
        if self.session_id == "":
            raise ValueError("session id must not be empty")
        if self.model == "":
            raise ValueError("model must not be empty")


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """A model offered by an agent runtime."""

    id: str
    display_name: str

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("model id must not be empty")
        if not self.display_name:
            raise ValueError("model display name must not be empty")


@runtime_checkable
class AgentSession(Protocol):
    """A live provider-neutral agent session."""

    @property
    def session_id(self) -> str:
        """Stable identifier for this session."""
        ...

    async def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        """Send one turn and return its normalized event stream.

        Omitting ``approval_handler`` is always fail-closed: every approval
        request receives ``ApprovalDecision.DENY``.
        """
        ...

    async def interrupt(self) -> None:
        """Interrupt in-flight work for this session."""
        ...


@runtime_checkable
class AgentRuntime(Protocol):
    """Factory and model-discovery contract implemented by agent providers."""

    async def start_or_resume(self, request: SessionRequest) -> AgentSession:
        """Start a new session or resume ``request.session_id`` when provided."""
        ...

    async def list_models(self) -> Sequence[ModelInfo]:
        """Return models available from this runtime."""
        ...
