"""Durable node-local model-usage metering and budget caps (#388).

Records body-free spend counters (tokens and request counts — never prompts,
responses, or credentials) per KST day × provider × mode, where mode is
``interactive`` (a user-visible Telegram turn) or ``autonomous`` (background
spend such as distill extraction). A configurable per-provider daily token
budget adds a warn threshold (early alarm) and an enforce threshold that
blocks **autonomous** spend only — interactive user requests are never
blocked by design.

The meter itself must never take down the conversation path: persistence
failures degrade to in-memory counting with a logged warning, and alert-sink
failures are swallowed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Literal

from .usage import UsageSnapshot

logger = logging.getLogger(__name__)

MODE_INTERACTIVE = "interactive"
MODE_AUTONOMOUS = "autonomous"
_MODES = (MODE_INTERACTIVE, MODE_AUTONOMOUS)

# KST day buckets match the repo-wide reporting convention (fixed offset, no
# DST, deterministic in tests).
_KST = timezone(timedelta(hours=9))

_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MAX_COUNT = 10**12
_DEFAULT_RETENTION_DAYS = 35
_COUNTER_KEYS = ("input_tokens", "output_tokens", "requests")

BudgetState = Literal["ok", "warn", "blocked"]
AlertKind = Literal["warn", "enforce"]


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """Body-free outcome of a budget check for one provider's current day."""

    provider: str
    day: str
    state: BudgetState
    allowed: bool
    used_tokens: int
    budget_tokens: int

    def reason(self) -> str:
        if self.budget_tokens <= 0:
            return f"{self.provider} daily token budget disabled"
        return (
            f"{self.provider} used {self.used_tokens} of {self.budget_tokens} "
            f"budget tokens on {self.day} ({self.state})"
        )


@dataclass(frozen=True, slots=True)
class UsageReservation:
    """Opaque handle for one admitted (or rejected) autonomous reservation.

    It pins the accounting day and the exact charged dimensions at admission
    time, so a later refund always unwinds the same bucket even across a
    KST midnight rollover.
    """

    provider: str
    day: str
    input_tokens: int
    output_tokens: int
    requests: int
    decision: BudgetDecision

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    @property
    def state(self) -> BudgetState:
        return self.decision.state

    def reason(self) -> str:
        return self.decision.reason()


@dataclass(frozen=True, slots=True)
class UsageAlert:
    """One first-time budget threshold crossing for a provider-day."""

    provider: str
    day: str
    kind: AlertKind
    used_tokens: int
    budget_tokens: int

    def render(self) -> str:
        percent = min(999, round(self.used_tokens * 100 / self.budget_tokens))
        marker = "🛑 enforce" if self.kind == "enforce" else "⚠️ warn"
        detail = (
            "autonomous spend is now blocked"
            if self.kind == "enforce"
            else "approaching the daily cap"
        )
        return (
            f"{marker}: {self.provider} used {self.used_tokens} of "
            f"{self.budget_tokens} daily budget tokens ({percent}%) on "
            f"{self.day} — {detail}"
        )


def _clamped_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, _MAX_COUNT))


class UsageMeter:
    """Durable daily usage counters with warn/enforce token budgets."""

    def __init__(
        self,
        path: Path,
        *,
        budgets: Mapping[str, int] | None = None,
        warn_percent: int = 80,
        alert_sink: Callable[[str], None] | None = None,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not (1 <= warn_percent <= 99):
            raise ValueError("warn_percent must be within [1, 99]")
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        self._path = Path(path)
        self._warn_percent = warn_percent
        self._alert_sink = alert_sink
        self._retention_days = retention_days
        self._clock = clock
        # Thread lock for in-process atomicity; every mutation additionally
        # takes an exclusive interprocess file lock and re-reads the on-disk
        # state before applying its delta, so overlapping meter instances or
        # bridge processes merge spend instead of last-writer-wins losing it.
        self._lock = threading.Lock()
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        self._flock_warned = False
        # Signed counter deltas applied since the last successful save
        # (day -> provider -> mode -> key). While non-empty, every locked
        # mutation reloads the authoritative on-disk state and replays these
        # on top, so a transient save failure neither loses our spend nor
        # clobbers spend other writers persisted meanwhile.
        self._pending_deltas: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
        self._budgets: dict[str, int] = {}
        for provider, budget in dict(budgets or {}).items():
            self._validate_provider(provider)
            self._budgets[provider] = _clamped_count(budget)
        # Cumulative per-thread baselines for delta-based provider counters.
        self._thread_baselines: dict[str, tuple[int, int]] = {}
        self._days: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
        self._alerted: dict[str, dict[str, list[str]]] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError, ValueError):
            logger.warning(
                "Usage meter state at %s is unreadable; starting from empty counters",
                self._path,
            )
            return
        if not isinstance(raw, Mapping):
            return
        self._load_days(raw.get("days"))
        self._load_alerted(raw.get("alerted"))

    def _load_days(self, days: object) -> None:
        if not isinstance(days, Mapping):
            return
        for day, providers in days.items():
            if not self._is_day_key(day) or not isinstance(providers, Mapping):
                continue
            for provider, modes in providers.items():
                if not isinstance(provider, str) or not _PROVIDER_RE.match(provider):
                    continue
                if not isinstance(modes, Mapping):
                    continue
                for mode, counters in modes.items():
                    if mode not in _MODES or not isinstance(counters, Mapping):
                        continue
                    bucket = self._bucket(day, provider, mode)
                    for key in _COUNTER_KEYS:
                        bucket[key] = _clamped_count(counters.get(key))

    def _load_alerted(self, alerted: object) -> None:
        if not isinstance(alerted, Mapping):
            return
        for day, providers in alerted.items():
            if not self._is_day_key(day) or not isinstance(providers, Mapping):
                continue
            for provider, kinds in providers.items():
                if not isinstance(provider, str) or not _PROVIDER_RE.match(provider):
                    continue
                if not isinstance(kinds, list):
                    continue
                safe_kinds = [kind for kind in kinds if kind in ("warn", "enforce")]
                if safe_kinds:
                    self._alerted.setdefault(day, {})[provider] = safe_kinds

    def _save(self) -> None:
        payload = json.dumps(
            {"version": 1, "days": self._days, "alerted": self._alerted},
            ensure_ascii=True,
            sort_keys=True,
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self._path.name}.", dir=str(self._path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, self._path)
                self._pending_deltas.clear()
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError:
            logger.warning(
                "Usage meter state could not be persisted to %s; keeping "
                "in-memory counters",
                self._path,
            )

    @contextmanager
    def _locked_state(self, *, save: bool = True) -> Iterator[None]:
        """Hold the thread + interprocess lock around one reload/apply/write.

        The on-disk state is authoritative: it is re-read under the lock so
        the caller's delta lands on top of every other writer's spend. If the
        lock file cannot be used the meter degrades to thread-only locking
        (logged once) rather than breaking the turn.
        """

        with self._lock:
            handle = None
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    self._lock_path, os.O_CREAT | os.O_RDWR, 0o600
                )
                handle = os.fdopen(fd, "r+b")
                fcntl.flock(handle, fcntl.LOCK_EX)
            except OSError:
                if handle is not None:
                    handle.close()
                    handle = None
                if not self._flock_warned:
                    self._flock_warned = True
                    logger.warning(
                        "Usage meter interprocess lock unavailable at %s; "
                        "falling back to thread-level locking only",
                        self._lock_path,
                    )
            try:
                self._days.clear()
                self._alerted.clear()
                self._load()
                self._replay_pending_deltas()
                yield
                if save:
                    self._save()
            finally:
                if handle is not None:
                    handle.close()

    # -- helpers -------------------------------------------------------------

    def _note_pending_delta(
        self, day: str, provider: str, mode: str, changes: Mapping[str, int], sign: int
    ) -> None:
        bucket = (
            self._pending_deltas.setdefault(day, {})
            .setdefault(provider, {})
            .setdefault(mode, {key: 0 for key in _COUNTER_KEYS})
        )
        for key, value in changes.items():
            bucket[key] += sign * value

    def _replay_pending_deltas(self) -> None:
        for day, providers in self._pending_deltas.items():
            for provider, modes in providers.items():
                for mode, changes in modes.items():
                    bucket = self._bucket(day, provider, mode)
                    for key, value in changes.items():
                        bucket[key] = max(0, min(bucket[key] + value, _MAX_COUNT))

    @staticmethod
    def _is_day_key(value: object) -> bool:
        return isinstance(value, str) and bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value))

    @staticmethod
    def _validate_provider(provider: str) -> None:
        if not isinstance(provider, str) or not _PROVIDER_RE.match(provider):
            raise ValueError(f"invalid provider label: {provider!r}")

    def current_day(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=_KST).strftime("%Y-%m-%d")

    def _bucket(self, day: str, provider: str, mode: str) -> dict[str, int]:
        return (
            self._days.setdefault(day, {})
            .setdefault(provider, {})
            .setdefault(mode, {key: 0 for key in _COUNTER_KEYS})
        )

    def _prune(self, today: str) -> None:
        horizon = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=_KST) - timedelta(
            days=self._retention_days
        )
        for store in (self._days, self._alerted):
            for day in [key for key in store if key < horizon.strftime("%Y-%m-%d")]:
                del store[day]

    def used_tokens(self, provider: str, day: str | None = None) -> int:
        """Total input+output tokens recorded for one provider on one day."""

        target_day = day or self.current_day()
        providers = self._days.get(target_day, {})
        modes = providers.get(provider, {})
        return sum(
            _clamped_count(counters.get("input_tokens"))
            + _clamped_count(counters.get("output_tokens"))
            for counters in modes.values()
        )

    # -- recording -----------------------------------------------------------

    def record(
        self,
        provider: str,
        mode: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        requests: int = 0,
    ) -> tuple[UsageAlert, ...]:
        """Add one body-free usage record and return new threshold alerts."""

        self._validate_provider(provider)
        if mode not in _MODES:
            raise ValueError(f"invalid usage mode: {mode!r}")
        added = {
            "input_tokens": _clamped_count(input_tokens),
            "output_tokens": _clamped_count(output_tokens),
            "requests": _clamped_count(requests),
        }
        if not any(added.values()):
            return ()
        with self._locked_state():
            alerts = self._apply_record(provider, mode, added, self.current_day())
        for alert in alerts:
            self._emit(alert)
        return alerts

    def _apply_record(
        self, provider: str, mode: str, added: Mapping[str, int], day: str
    ) -> tuple[UsageAlert, ...]:
        """Apply one pre-validated record to ``day`` under the meter lock."""

        if not any(added.values()):
            return ()
        bucket = self._bucket(day, provider, mode)
        for key, value in added.items():
            bucket[key] = min(bucket[key] + value, _MAX_COUNT)
        self._note_pending_delta(day, provider, mode, added, 1)
        alerts = self._collect_alerts(provider, day)
        self._prune(day)
        return alerts

    def reserve_autonomous_spend(
        self,
        provider: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        requests: int = 0,
    ) -> UsageReservation:
        """Atomically admit-and-charge one autonomous attempt, prospectively.

        Admission requires the *whole* requested reservation to fit under the
        provider's daily cap (``used + reservation <= budget``), and the
        admission check and the charge happen under one lock (thread and
        interprocess) against freshly re-read on-disk state, pinned to one
        accounting day. Concurrent attempts cannot jointly overrun the cap, a
        single oversized attempt is rejected outright (a blocked reservation
        charges nothing), and the returned handle carries the captured day
        and exact dimensions so a refund always unwinds the same bucket.
        Callers must size the budget to fit at least one maximal attempt, or
        such work stays deferred by design.
        """

        self._validate_provider(provider)
        added = {
            "input_tokens": _clamped_count(input_tokens),
            "output_tokens": _clamped_count(output_tokens),
            "requests": _clamped_count(requests),
        }
        reserved_tokens = added["input_tokens"] + added["output_tokens"]
        with self._locked_state():
            day = self.current_day()
            budget = self._budgets.get(provider, 0)
            used = self.used_tokens(provider, day)
            if budget > 0 and used + reserved_tokens > budget:
                decision = BudgetDecision(provider, day, "blocked", False, used, budget)
                return UsageReservation(
                    provider, day, added["input_tokens"], added["output_tokens"],
                    added["requests"], decision,
                )
            alerts = self._apply_record(provider, MODE_AUTONOMOUS, added, day)
            state: BudgetState = "ok"
            if budget > 0 and used >= self._warn_threshold(budget):
                state = "warn"
            decision = BudgetDecision(provider, day, state, True, used, budget)
        for alert in alerts:
            self._emit(alert)
        return UsageReservation(
            provider, day, added["input_tokens"], added["output_tokens"],
            added["requests"], decision,
        )

    def refund_reservation(self, reservation: UsageReservation) -> None:
        """Unwind one admitted reservation whose attempt never started.

        Only for the caller that just reserved and then lost the work (for
        example a distill claim race or an already-finished job): the
        handle's exact dimensions are subtracted from the handle's own
        accounting day, clamped at zero — a refund after midnight cannot
        touch another day's spend. Blocked reservations charged nothing and
        refund as a no-op. Alerts already fired are intentionally not
        retracted, and crashes between reserve and refund leave the charge
        in place — both err toward over-counting, never under-counting.
        """

        if not reservation.allowed:
            return
        removed = {
            "input_tokens": _clamped_count(reservation.input_tokens),
            "output_tokens": _clamped_count(reservation.output_tokens),
            "requests": _clamped_count(reservation.requests),
        }
        if not any(removed.values()):
            return
        with self._locked_state():
            bucket = self._bucket(
                reservation.day, reservation.provider, MODE_AUTONOMOUS
            )
            applied = {
                key: min(bucket[key], value) for key, value in removed.items()
            }
            for key, value in applied.items():
                bucket[key] = bucket[key] - value
            self._note_pending_delta(
                reservation.day, reservation.provider, MODE_AUTONOMOUS, applied, -1
            )

    def record_codex_thread_usage(
        self,
        thread_id: str,
        previous: UsageSnapshot | None,
        current: UsageSnapshot,
    ) -> tuple[UsageAlert, ...]:
        """Record interactive Codex spend from cumulative per-thread totals.

        ``thread/tokenUsage/updated`` carries thread-lifetime totals, so the
        first observation of a thread (which may include history from before
        this process) only sets a baseline; later observations record the
        positive delta. A shrinking total re-baselines instead of recording.
        """

        if not thread_id:
            return ()
        current_totals = (
            _clamped_count(current.input_tokens),
            _clamped_count(current.output_tokens),
        )
        baseline = self._thread_baselines.get(thread_id)
        if baseline is None and previous is not None:
            baseline = (
                _clamped_count(previous.input_tokens),
                _clamped_count(previous.output_tokens),
            )
        self._thread_baselines[thread_id] = current_totals
        self._thread_baselines = dict(tuple(self._thread_baselines.items())[-256:])
        if baseline is None:
            return ()
        delta_input = current_totals[0] - baseline[0]
        delta_output = current_totals[1] - baseline[1]
        if delta_input < 0 or delta_output < 0:
            return ()
        return self.record(
            "codex",
            MODE_INTERACTIVE,
            input_tokens=delta_input,
            output_tokens=delta_output,
        )

    # -- budgets / alerts ------------------------------------------------------

    def _warn_threshold(self, budget: int) -> int:
        # Floor at one token so a tiny valid budget cannot warn at zero usage.
        return max(1, budget * self._warn_percent // 100)

    def _collect_alerts(self, provider: str, day: str) -> tuple[UsageAlert, ...]:
        budget = self._budgets.get(provider, 0)
        if budget <= 0:
            return ()
        used = self.used_tokens(provider, day)
        fired = self._alerted.setdefault(day, {}).setdefault(provider, [])
        alerts: list[UsageAlert] = []
        warn_at = self._warn_threshold(budget)
        if used >= warn_at and "warn" not in fired:
            fired.append("warn")
            alerts.append(UsageAlert(provider, day, "warn", used, budget))
        if used >= budget and "enforce" not in fired:
            fired.append("enforce")
            alerts.append(UsageAlert(provider, day, "enforce", used, budget))
        return tuple(alerts)

    def _emit(self, alert: UsageAlert) -> None:
        message = alert.render()
        logger.warning("Usage budget alert: %s", message)
        if self._alert_sink is None:
            return
        try:
            self._alert_sink(message)
        except Exception:
            logger.exception("Usage alert sink failed; alert already logged")

    def check_autonomous_spend(self, provider: str) -> BudgetDecision:
        """Gate autonomous spend only; interactive turns are never blocked."""

        self._validate_provider(provider)
        with self._locked_state(save=False):
            day = self.current_day()
            budget = self._budgets.get(provider, 0)
            used = self.used_tokens(provider, day)
        if budget <= 0:
            return BudgetDecision(provider, day, "ok", True, used, budget)
        if used >= budget:
            return BudgetDecision(provider, day, "blocked", False, used, budget)
        if used >= self._warn_threshold(budget):
            return BudgetDecision(provider, day, "warn", True, used, budget)
        return BudgetDecision(provider, day, "ok", True, used, budget)

    # -- reporting -------------------------------------------------------------

    def render_report(self, *, days: int = 7) -> str:
        """Render a compact body-free usage summary for the last ``days``."""

        if days <= 0:
            raise ValueError("days must be positive")
        today = datetime.strptime(self.current_day(), "%Y-%m-%d").replace(tzinfo=_KST)
        window = [
            (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(days)
        ]
        providers = sorted(
            {provider for day in window for provider in self._days.get(day, {})}
            | {provider for provider, budget in self._budgets.items() if budget > 0}
        )
        lines = [f"📊 Local usage meter (KST, last {days}d)"]
        if not providers:
            lines.append("no recorded usage")
            return "\n".join(lines)
        for provider in providers:
            totals = {mode: {key: 0 for key in _COUNTER_KEYS} for mode in _MODES}
            for day in window:
                modes = self._days.get(day, {}).get(provider, {})
                for mode in _MODES:
                    for key in _COUNTER_KEYS:
                        totals[mode][key] += _clamped_count(modes.get(mode, {}).get(key))
            today_key = window[0]
            decision = self.check_autonomous_spend(provider)
            lines.append(
                f"{provider} · today {self.used_tokens(provider, today_key)} tok · "
                f"{days}d interactive "
                f"{totals[MODE_INTERACTIVE]['input_tokens'] + totals[MODE_INTERACTIVE]['output_tokens']}"
                f" tok/{totals[MODE_INTERACTIVE]['requests']} req · autonomous "
                f"{totals[MODE_AUTONOMOUS]['input_tokens'] + totals[MODE_AUTONOMOUS]['output_tokens']}"
                f" tok/{totals[MODE_AUTONOMOUS]['requests']} req"
            )
            if decision.budget_tokens > 0:
                percent = min(
                    999, round(decision.used_tokens * 100 / decision.budget_tokens)
                )
                lines.append(
                    f"  budget {decision.used_tokens}/{decision.budget_tokens} tok "
                    f"({percent}%, {decision.state}; enforce blocks autonomous only)"
                )
        return "\n".join(lines)


__all__ = [
    "MODE_AUTONOMOUS",
    "MODE_INTERACTIVE",
    "BudgetDecision",
    "UsageAlert",
    "UsageMeter",
    "UsageReservation",
]
