"""Tests for the additive per-turn cost ledger (``/usage`` spend report)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.usage_cost_ledger import CostLedger, DEFAULT_DAYS


def _snapshot(model: str = "glm-5.2[1m]", cost: float = 0.14, *, total=None,
              in_tok: int = 100, out_tok: int = 5):
    return SimpleNamespace(
        models=(SimpleNamespace(model=model, cost_usd=cost,
                                input_tokens=in_tok, output_tokens=out_tok),),
        total_cost_usd=total if total is not None else cost,
    )


def test_records_and_aggregates_per_model(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "ledger.jsonl")

    ledger.record_snapshot(_snapshot(cost=0.10))
    ledger.record_snapshot(_snapshot(cost=0.20))

    agg = ledger.aggregate(days=DEFAULT_DAYS)
    assert len(agg) == 1
    assert agg[0].model == "glm-5.2[1m]"
    assert agg[0].cost_usd == pytest.approx(0.30)
    assert agg[0].input_tokens == 200
    assert agg[0].output_tokens == 10
    assert agg[0].turns == 2


def test_skips_turns_with_no_cost(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    no_cost = SimpleNamespace(
        models=(SimpleNamespace(model="glm-5.2[1m]", cost_usd=None,
                                input_tokens=50, output_tokens=2),),
        total_cost_usd=None,
    )
    ledger.record_snapshot(no_cost)
    ledger.record_snapshot(_snapshot(cost=0.05))

    agg = ledger.aggregate(days=DEFAULT_DAYS)
    assert len(agg) == 1
    assert agg[0].turns == 1
    assert agg[0].cost_usd == pytest.approx(0.05)


def test_window_cutoff_excludes_old_records(tmp_path: Path) -> None:
    base = 1_700_000_000.0
    now = [base]
    ledger = CostLedger(tmp_path / "ledger.jsonl", clock=lambda: now[0])

    now[0] = base - 10 * 86400  # 10 days ago
    ledger.record_snapshot(_snapshot(cost=1.0))
    now[0] = base - 1 * 86400   # 1 day ago
    ledger.record_snapshot(_snapshot(cost=2.0))
    now[0] = base               # now

    agg = ledger.aggregate(days=7)
    assert len(agg) == 1
    assert agg[0].cost_usd == pytest.approx(2.0)


def test_render_report_format(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    ledger.record_snapshot(_snapshot(model="glm-5.2[1m]", cost=0.1234))

    report = ledger.render_report(days=7)
    assert "Spend (7d): $0.1234" in report
    assert "glm-5.2[1m]" in report
    assert "turns" in report


def test_render_report_empty_when_no_data(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    assert ledger.render_report(days=7) == ""


def test_skips_corrupt_trailing_lines(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    ledger.record_snapshot(_snapshot(cost=0.5))
    # Append a partial/garbage line as if a prior write crashed mid-line.
    (tmp_path / "ledger.jsonl").open("a").write("{not json\n")

    agg = ledger.aggregate(days=DEFAULT_DAYS)
    assert len(agg) == 1
    assert agg[0].cost_usd == pytest.approx(0.5)


def test_disabled_ledger_is_noop(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "ledger.jsonl", enabled=False)
    ledger.record_snapshot(_snapshot(cost=9.0))
    assert ledger.aggregate(days=DEFAULT_DAYS) == []
    assert ledger.render_report(days=7) == ""


def test_never_raises_on_bad_snapshot(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    ledger.record_snapshot(None)            # type: ignore[arg-type]
    ledger.record_snapshot(SimpleNamespace())  # no models / total
    ledger.record_snapshot("garbage")       # type: ignore[arg-type]
    assert ledger.aggregate(days=DEFAULT_DAYS) == []
