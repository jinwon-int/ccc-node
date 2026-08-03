"""Additive per-turn cost ledger for the ``/usage`` spend report.

Records each terminal Claude turn's per-model cost (USD) + token totals to an
append-only JSONL file, and aggregates a rolling window for ``/usage``.

This is intentionally separate from :class:`UsageMeter`. The meter enforces
budget reservation/refund invariants and tracks provider-level tokens/requests;
this ledger only needs cost + model granularity for display and never touches
the meter's state, so it cannot perturb budget math.

Resilience: append-only JSONL is safe under concurrent/crashed writers (one
self-contained line per turn; partial/corrupt trailing lines are skipped on
read). Every public method swallows exceptions — the ledger must never break
the conversation path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 7
_MAX_MODELS_PER_TURN = 16
_MAX_REPORT_MODELS = 8


@dataclass(frozen=True)
class CostAggregate:
    """Aggregated cost/token totals for one model over the report window."""

    model: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    turns: int


class CostLedger:
    """Append-only per-turn cost ledger backed by a JSONL file."""

    def __init__(
        self,
        path: Path | str,
        *,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._enabled = bool(enabled)
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> Path:
        return self._path

    def record_snapshot(self, snapshot: Any, *, provider: str = "claude") -> None:
        """Append one turn's per-model cost from a parsed ``UsageSnapshot``.

        Reads ``snapshot.models`` (an iterable of objects with ``model``,
        ``cost_usd``, ``input_tokens``, ``output_tokens``) and
        ``snapshot.total_cost_usd``. A turn with no cost is skipped (no row).
        """

        if not self._enabled:
            return
        try:
            rows = []
            for entry in (getattr(snapshot, "models", ()) or ())[:_MAX_MODELS_PER_TURN]:
                cost = getattr(entry, "cost_usd", None)
                if cost is None:
                    continue
                rows.append(
                    {
                        "model": (getattr(entry, "model", "") or "")[:80],
                        "cost_usd": round(float(cost), 6),
                        "in": int(getattr(entry, "input_tokens", 0) or 0),
                        "out": int(getattr(entry, "output_tokens", 0) or 0),
                    }
                )
            total = getattr(snapshot, "total_cost_usd", None)
            if not rows and total in (None, 0, 0.0):
                return
            line = {
                "ts": float(self._clock()),
                "provider": provider,
                "total_cost_usd": (
                    round(float(total), 6) if total not in (None,) else None
                ),
                "models": rows,
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            logger.debug("cost ledger append failed", exc_info=True)

    def aggregate(self, *, days: int = DEFAULT_DAYS) -> List[CostAggregate]:
        """Aggregate per-model cost/tokens over the trailing ``days`` window."""

        if not self._enabled or not self._path.exists():
            return []
        try:
            cutoff = float(self._clock()) - max(0, int(days)) * 86400
        except Exception:
            return []
        acc: dict[str, dict[str, float]] = {}
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    try:
                        record = json.loads(raw)
                    except Exception:
                        continue  # partial/corrupt trailing line
                    if not isinstance(record, dict):
                        continue
                    try:
                        if float(record.get("ts", 0)) < cutoff:
                            continue
                    except (TypeError, ValueError):
                        continue
                    for model_row in record.get("models", []) or []:
                        if not isinstance(model_row, dict):
                            continue
                        key = (model_row.get("model") or "unknown")[:80]
                        bucket = acc.setdefault(
                            key, {"cost_usd": 0.0, "in": 0, "out": 0, "turns": 0}
                        )
                        try:
                            bucket["cost_usd"] += float(model_row.get("cost_usd") or 0.0)
                        except (TypeError, ValueError):
                            pass
                        bucket["in"] += int(model_row.get("in") or 0)
                        bucket["out"] += int(model_row.get("out") or 0)
                        bucket["turns"] += 1
        except Exception:
            return []
        aggregated = [
            CostAggregate(
                model=key,
                cost_usd=round(values["cost_usd"], 4),
                input_tokens=int(values["in"]),
                output_tokens=int(values["out"]),
                turns=int(values["turns"]),
            )
            for key, values in acc.items()
        ]
        aggregated.sort(key=lambda item: item.cost_usd, reverse=True)
        return aggregated

    def render_report(self, *, days: int = DEFAULT_DAYS) -> str:
        """Render a Telegram-safe spend block; empty string when no data."""

        aggregated = self.aggregate(days=days)
        if not aggregated:
            return ""
        total = sum(item.cost_usd for item in aggregated)
        lines = [f"\U0001f4b0 Spend ({days}d): ${total:.4f}"]
        for item in aggregated[:_MAX_REPORT_MODELS]:
            lines.append(
                f"  {item.model} · ${item.cost_usd:.4f} · "
                f"in {item.input_tokens:,} · out {item.output_tokens:,} "
                f"· {item.turns} turns"
            )
        return "\n".join(lines)
