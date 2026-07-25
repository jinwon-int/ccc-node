"""Typed in-memory lifecycle for one provider-neutral bridge request.

The lifecycle is intentionally synchronous and event-loop confined: every
method completes without an ``await``, so competing asyncio tasks observe a
single compare-and-set winner. Durable ``TaskLedger`` writes are projections of
this state and remain best-effort; they are not a second transition authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RequestPhase(str, Enum):
    WAITING_FOR_TURN = "waiting-for-turn"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"


TERMINAL_PHASES = frozenset(
    {
        RequestPhase.COMPLETED,
        RequestPhase.FAILED,
        RequestPhase.CANCELED,
        RequestPhase.TIMEOUT,
        RequestPhase.INTERRUPTED,
    }
)


@dataclass(frozen=True, slots=True)
class ApprovalLease:
    """Capability held by the sole active approval callback."""

    sequence: int


class TerminalAttemptKind(str, Enum):
    WON = "won"
    ALREADY_SAME = "already-same"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class TerminalAttempt:
    """Result of a non-throwing first-terminal-wins transition."""

    kind: TerminalAttemptKind
    phase: RequestPhase
    cause: str

    @property
    def won(self) -> bool:
        return self.kind is TerminalAttemptKind.WON


class RequestLifecycle:
    """Single in-memory authority for a request's lifecycle."""

    __slots__ = (
        "_active_approval_lease",
        "_approval_sequence",
        "_finalization_started",
        "_phase",
        "_terminal_cause",
    )

    def __init__(self) -> None:
        self._phase = RequestPhase.WAITING_FOR_TURN
        self._terminal_cause: str | None = None
        self._approval_sequence = 0
        self._active_approval_lease: ApprovalLease | None = None
        self._finalization_started = False

    @property
    def phase(self) -> RequestPhase:
        return self._phase

    @property
    def terminal_outcome(self) -> RequestPhase | None:
        return self._phase if self._phase in TERMINAL_PHASES else None

    @property
    def terminal_cause(self) -> str | None:
        return self._terminal_cause

    @property
    def is_terminal(self) -> bool:
        return self._phase in TERMINAL_PHASES

    @property
    def is_waiting_for_turn(self) -> bool:
        return self._phase is RequestPhase.WAITING_FOR_TURN

    @property
    def is_input_required(self) -> bool:
        return self._phase is RequestPhase.INPUT_REQUIRED

    def admit(self) -> bool:
        """Move the sole waiting request into provider-owned work."""

        if self._phase is not RequestPhase.WAITING_FOR_TURN:
            return False
        self._phase = RequestPhase.WORKING
        return True

    def begin_approval(self) -> ApprovalLease | None:
        """Acquire the only approval callback lease for this request."""

        if self._phase is not RequestPhase.WORKING:
            return None
        self._approval_sequence += 1
        lease = ApprovalLease(self._approval_sequence)
        self._active_approval_lease = lease
        self._phase = RequestPhase.INPUT_REQUIRED
        return lease

    def end_approval(self, lease: ApprovalLease) -> bool:
        """Resume work only for the current, still-live approval lease."""

        if self._phase is not RequestPhase.INPUT_REQUIRED or self._active_approval_lease != lease:
            return False
        self._active_approval_lease = None
        self._phase = RequestPhase.WORKING
        return True

    def try_terminal(self, phase: RequestPhase, *, cause: str) -> TerminalAttempt:
        """Set the first terminal result without throwing on normal races."""

        if phase not in TERMINAL_PHASES:
            raise ValueError(f"non-terminal request phase: {phase.value}")
        if not cause:
            raise ValueError("terminal cause must not be empty")
        if self._phase in TERMINAL_PHASES:
            kind = (
                TerminalAttemptKind.ALREADY_SAME
                if self._phase is phase
                else TerminalAttemptKind.LOST
            )
            return TerminalAttempt(
                kind,
                self._phase,
                self._terminal_cause or "unknown",
            )
        self._phase = phase
        self._terminal_cause = cause
        self._active_approval_lease = None
        return TerminalAttempt(TerminalAttemptKind.WON, phase, cause)

    def begin_finalization(self) -> bool:
        """Give cleanup ownership to exactly one terminal observer."""

        if not self.is_terminal or self._finalization_started:
            return False
        self._finalization_started = True
        return True


__all__ = [
    "ApprovalLease",
    "RequestLifecycle",
    "RequestPhase",
    "TERMINAL_PHASES",
    "TerminalAttempt",
    "TerminalAttemptKind",
]
