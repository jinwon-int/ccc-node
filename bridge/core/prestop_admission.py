"""In-process pre-stop protocol foundation; NOT a durable restart authority.

All work must be admitted before dispatch and held through final delivery.
No production caller uses this module yet. See docs/prestop-admission.md.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable


class AdmissionError(RuntimeError):
    """Refused transition; callers must not stop or mutate the service."""


@dataclass(frozen=True)
class StopEvidence:
    attempt: str
    process_identity: str
    generation: str
    deadline: float


class PrestopAdmission:
    """Serialize admission, completion and attempt ownership under one lock.

    Identity must include process start identity, not just PID. Generation is
    the serving source/artifact/dependency identity supplied by the integrator.
    The clock must be monotonic. This object cannot be restored after a crash.
    """

    def __init__(
        self,
        process_identity: str,
        generation: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not process_identity or not generation:
            raise ValueError("serving identity required")
        self._identity = process_identity
        self._generation = generation
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "open"
        self._work: set[str] = set()
        self._evidence: StopEvidence | None = None
        self._last_time = -math.inf

    def _now(self) -> float:
        try:
            value = self._clock()
            valid = math.isfinite(value) and value >= self._last_time
        except Exception as exc:
            self._state = "failed"
            raise AdmissionError("monotonic clock unavailable") from exc
        if not valid:
            self._state = "failed"
            raise AdmissionError("invalid monotonic clock")
        self._last_time = value
        return value

    def admit(self) -> str:
        """Return a one-use work token, or refuse before dispatch."""
        with self._lock:
            if self._state != "open":
                raise AdmissionError("admission closed")
            token = uuid.uuid4().hex
            self._work.add(token)
            return token

    def close(self, *, timeout: float) -> StopEvidence:
        """Close admission atomically with creating a fresh attempt identity."""
        with self._lock:
            if self._state != "open":
                raise AdmissionError("attempt already active")
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("positive finite timeout required")
            deadline = self._now() + timeout
            if not math.isfinite(deadline):
                raise ValueError("finite deadline required")
            self._evidence = StopEvidence(
                uuid.uuid4().hex, self._identity, self._generation, deadline
            )
            self._state = "draining"
            return self._evidence

    def finish(self, token: str, *, delivered: bool) -> None:
        """Release work only after its final delivery outcome is known.

        Keep the token during retryable delivery errors. A terminal failure
        poisons the current drain (or closes admission if no drain exists).
        Duplicate/unknown completions are rejected, never counted twice.
        """
        with self._lock:
            if type(delivered) is not bool:
                raise ValueError("delivery outcome must be boolean")
            if token not in self._work:
                raise AdmissionError("unknown or completed work")
            self._work.remove(token)
            if not delivered:
                self._state = "failed"

    def _check(self, evidence: StopEvidence) -> None:
        if self._evidence is None or evidence != self._evidence:
            raise AdmissionError("stale or foreign attempt")
        if self._state != "draining":
            raise AdmissionError("attempt not draining")
        if self._now() >= evidence.deadline:
            self._state = "failed"
            raise AdmissionError("drain deadline expired")

    def ready(self, evidence: StopEvidence) -> bool:
        """Observation only: never authorizes shutdown or source mutation."""
        with self._lock:
            self._check(evidence)
            return not self._work

    def commit(self, evidence: StopEvidence) -> None:
        """Consume permission once, rechecking identity, deadline and work.

        This is only an in-memory transition. An external controller MUST NOT
        treat it as durable admission acknowledgement (see integration contract).
        """
        with self._lock:
            self._check(evidence)
            if self._work:
                raise AdmissionError("work or delivery outstanding")
            self._state = "committed"

    def cancel(self, evidence: StopEvidence) -> None:
        """Explicit owner abort before commit; never called automatically.

        Failed attempts are latched closed; this API cannot reconcile them.
        Pending tokens survive abort and belong to any subsequent drain.
        """
        with self._lock:
            if self._evidence is None or evidence != self._evidence:
                raise AdmissionError("stale or foreign attempt")
            if self._state != "draining":
                raise AdmissionError("attempt cannot reopen admission")
            self._check(evidence)
            self._evidence = None
            self._state = "open"
