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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
        # Guards counter mutations so admission+charge is one atomic step even
        # if a caller ever moves meter calls off the event-loop thread.
        self._lock = threading.Lock()
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

    # -- helpers -------------------------------------------------------------

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
        with self._lock:
            alerts = self._apply_record(provider, mode, added)
        for alert in alerts:
            self._emit(alert)
        return alerts

    def _apply_record(
        self, provider: str, mode: str, added: Mapping[str, int]
    ) -> tuple[UsageAlert, ...]:
        """Apply one pre-validated record under the meter lock."""

        if not any(added.values()):
            return ()
        day = self.current_day()
        bucket = self._bucket(day, provider, mode)
        for key, value in added.items():
            bucket[key] = min(bucket[key] + value, _MAX_COUNT)
        alerts = self._collect_alerts(provider, day)
        self._prune(day)
        self._save()
        return alerts

    def reserve_autonomous_spend(
        self,
        provider: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        requests: int = 0,
    ) -> BudgetDecision:
        """Atomically admit-and-charge one autonomous attempt.

        Admission (is the provider still under its daily cap?) and the
        conservative charge happen under one lock with no await point in
        between, so concurrent autonomous attempts cannot all observe the
        pre-spend counter and overrun the cap together. A blocked decision
        charges nothing.
        """

        self._validate_provider(provider)
        added = {
            "input_tokens": _clamped_count(input_tokens),
            "output_tokens": _clamped_count(output_tokens),
            "requests": _clamped_count(requests),
        }
        with self._lock:
            day = self.current_day()
            budget = self._budgets.get(provider, 0)
            used = self.used_tokens(provider, day)
            if budget > 0 and used >= budget:
                return BudgetDecision(provider, day, "blocked", False, used, budget)
            alerts = self._apply_record(provider, MODE_AUTONOMOUS, added)
            state: BudgetState = "ok"
            if budget > 0 and used >= self._warn_threshold(budget):
                state = "warn"
            decision = BudgetDecision(provider, day, state, True, used, budget)
        for alert in alerts:
            self._emit(alert)
        return decision

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
        with self._lock:
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
]
