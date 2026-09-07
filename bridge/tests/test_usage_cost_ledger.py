"""Tests for the additive per-turn cost ledger (``/usage`` spend report)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.usage import ModelUsage, UsageSnapshot, delta_from_snapshots
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


# --- #1205 D-3: cumulative SDK snapshots -> per-turn deltas -------------------


def _snap(cost, *, in_tok=0, out_tok=0, models=()):
    return UsageSnapshot(
        provider="claude",
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_cost_usd=cost,
        models=tuple(models),
    )


def _mu(model, cost, *, in_tok=0, out_tok=0):
    return ModelUsage(model=model, cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok)


def test_delta_no_previous_returns_current() -> None:
    cur = _snap(0.55, in_tok=100, models=(_mu("glm-5.2[1m]", 0.55, in_tok=100),))
    assert delta_from_snapshots(None, cur) is cur


def test_delta_subtracts_previous_cumulative() -> None:
    prev = _snap(0.55, in_tok=100, models=(_mu("glm-5.2[1m]", 0.55, in_tok=100),))
    cur = _snap(1.09, in_tok=250, models=(_mu("glm-5.2[1m]", 1.09, in_tok=250),))
    delta = delta_from_snapshots(prev, cur)
    assert delta.total_cost_usd == pytest.approx(0.54)
    assert delta.input_tokens == 150
    assert delta.models[0].cost_usd == pytest.approx(0.54)
    assert delta.models[0].input_tokens == 150


def test_delta_reset_backwards_total_takes_current() -> None:
    # ConversationResetMessage zeroes the running totals: a current snapshot
    # BELOW the previous one is the fresh conversation's true delta.
    prev = _snap(5.16, in_tok=900, models=(_mu("glm-5.2[1m]", 5.16, in_tok=900),))
    cur = _snap(1.85, in_tok=200, models=(_mu("glm-5.2[1m]", 1.85, in_tok=200),))
    delta = delta_from_snapshots(prev, cur)
    assert delta is cur


def test_delta_new_model_contributes_full_totals() -> None:
    prev = _snap(1.00, models=(_mu("glm-5.2[1m]", 1.00, in_tok=100),))
    cur = _snap(
        1.80,
        models=(
            _mu("glm-5.2[1m]", 1.20, in_tok=120),
            _mu("k3", 0.60, in_tok=50),
        ),
    )
    delta = delta_from_snapshots(prev, cur)
    by_model = {m.model: m for m in delta.models}
    assert by_model["glm-5.2[1m]"].cost_usd == pytest.approx(0.20)
    assert by_model["k3"].cost_usd == pytest.approx(0.60)
    assert delta.total_cost_usd == pytest.approx(0.80)


def test_ledger_records_session_id(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    ledger.record_snapshot(_snapshot(cost=0.10), session_id="sess-abc")
    ledger.record_snapshot(_snapshot(cost=0.05), session_id=None)
    import json

    rows = [
        json.loads(line)
        for line in (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["sid"] == "sess-abc"
    assert "sid" not in rows[1]


# ---- windowed tail read ----------------------------------------------------
# The ledger is append-only and retained for the life of the node, so
# aggregate() walks it backwards and stops at the window edge instead of
# reading the whole spend history for a fixed 7-day answer.


def test_aggregate_stops_reading_at_the_window_edge(tmp_path: Path) -> None:
    from telegram_bot.core import usage_cost_ledger as mod

    base = 1_700_000_000.0
    now = [base - 400 * 86400]
    path = tmp_path / "ledger.jsonl"
    ledger = CostLedger(path, clock=lambda: now[0])

    # A year of history outside the window, then two rows inside it.
    for day in range(400, 9, -1):  # all strictly older than the 7-day window
        now[0] = base - day * 86400
        ledger.record_snapshot(_snapshot(cost=0.01))
    now[0] = base - 1 * 86400
    ledger.record_snapshot(_snapshot(cost=2.0))
    now[0] = base
    ledger.record_snapshot(_snapshot(cost=3.0))

    read_lines = 0
    real_iter = mod._iter_lines_newest_first

    def counting_iter(p):
        nonlocal read_lines
        for line in real_iter(p):
            read_lines += 1
            yield line

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod, "_iter_lines_newest_first", counting_iter)
        agg = ledger.aggregate(days=7)

    assert len(agg) == 1
    assert agg[0].cost_usd == pytest.approx(5.0)
    assert agg[0].turns == 2
    # 2 in-window rows + the short run of old rows that ends the scan —
    # bounded by the window, not by the ~390-row file.
    expected = 2 + mod._WINDOW_EDGE_TOLERANCE_ROWS
    assert read_lines == expected, (
        f"scan did not stop near the window edge ({read_lines} lines)"
    )


def test_tail_read_spans_block_boundaries(tmp_path: Path) -> None:
    """Rows must not be lost or duplicated where a read block splits a line."""
    from telegram_bot.core import usage_cost_ledger as mod

    base = 1_700_000_000.0
    now = [base]
    path = tmp_path / "ledger.jsonl"
    ledger = CostLedger(path, clock=lambda: now[0])
    turns = 900
    for i in range(turns):
        now[0] = base - i          # all well inside the window
        ledger.record_snapshot(_snapshot(cost=0.01))

    assert path.stat().st_size > mod._TAIL_BLOCK_BYTES, "fixture must span blocks"

    lines = path.read_text(encoding="utf-8").splitlines()
    backwards = [raw.decode("utf-8") for raw in mod._iter_lines_newest_first(path)]
    assert backwards == list(reversed(lines))

    agg = ledger.aggregate(days=DEFAULT_DAYS)
    assert agg[0].turns == turns
    assert agg[0].cost_usd == pytest.approx(0.01 * turns)


def test_tail_read_handles_a_file_without_a_trailing_newline(tmp_path: Path) -> None:
    from telegram_bot.core import usage_cost_ledger as mod

    path = tmp_path / "ledger.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}', encoding="utf-8")
    assert [raw.decode() for raw in mod._iter_lines_newest_first(path)] == [
        '{"b": 2}',
        '{"a": 1}',
    ]


def test_tail_read_on_an_empty_file_yields_nothing(tmp_path: Path) -> None:
    from telegram_bot.core import usage_cost_ledger as mod

    path = tmp_path / "ledger.jsonl"
    path.write_text("", encoding="utf-8")
    assert list(mod._iter_lines_newest_first(path)) == []


def test_a_backwards_clock_step_does_not_truncate_the_window(tmp_path: Path) -> None:
    """One row stamped behind its neighbours (an NTP step, or a second writer
    on the same project ledger) must not end the scan and undercount spend."""
    base = 1_700_000_000.0
    now = [base]
    path = tmp_path / "ledger.jsonl"
    ledger = CostLedger(path, clock=lambda: now[0])

    now[0] = base - 2 * 86400
    ledger.record_snapshot(_snapshot(cost=1.0))
    now[0] = base - 30 * 86400      # single out-of-order row, well outside 7d
    ledger.record_snapshot(_snapshot(cost=0.5))
    now[0] = base - 1 * 86400
    ledger.record_snapshot(_snapshot(cost=2.0))

    agg = ledger.aggregate(days=7)
    assert agg[0].turns == 2
    assert agg[0].cost_usd == pytest.approx(3.0)
