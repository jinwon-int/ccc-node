"""Settlement, shared finite allowances and trustworthy native usage evidence."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from telegram_bot.core.usage_meter import UsageMeter
from telegram_bot.memory.danso_backend import _run
from telegram_bot.memory.distill_types import DistillExtractionAccounting


def test_settlement_is_shared_exactly_once_and_pins_admission_day(tmp_path):
    path = tmp_path / "usage.json"
    first = UsageMeter(path, budgets={"danso": 1000}, clock=lambda: 1784170800)
    handle = first.reserve_autonomous_spend("danso", input_tokens=900, requests=1)
    second = UsageMeter(path, budgets={"danso": 1000}, clock=lambda: 1784257200)
    assert second.settle_reservation(handle, input_tokens=100, output_tokens=20, requests=1)
    assert not first.settle_reservation(handle, input_tokens=1, output_tokens=0, requests=1)
    first.refund_reservation(handle)
    data = json.loads(path.read_text())["days"]
    assert data["2026-07-16"]["danso"]["autonomous"]["input_tokens"] == 100
    assert "2026-07-17" not in data
    assert UsageMeter(path).used_tokens("danso", "2026-07-16") == 120


def test_failed_settlement_write_cannot_discount_another_process_twice(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    first = UsageMeter(path)
    handle = first.reserve_autonomous_spend("danso", input_tokens=900, requests=1)
    with monkeypatch.context() as patch:
        patch.setattr(first, "_save", lambda: None)
        first.settle_reservation(handle, input_tokens=100, output_tokens=20, requests=1)
    second = UsageMeter(path)
    second.settle_reservation(handle, input_tokens=100, output_tokens=20, requests=1)
    first.record("danso", "autonomous", input_tokens=50, requests=1)
    assert UsageMeter(path).used_tokens("danso") == 170


def test_recovery_cannot_exhaust_routine_or_exceed_aggregate_and_settles_in_own_mode(tmp_path):
    path = tmp_path / "usage.json"
    def meter():
        return UsageMeter(path, budgets={"danso": 1000}, recovery_reserve_percent=25)
    def recovery(_):
        return meter().reserve_autonomous_spend("danso", input_tokens=100, requests=1, purpose="recovery")
    with ThreadPoolExecutor(max_workers=8) as pool:
        handles = list(pool.map(recovery, range(8)))
    assert sum(h.allowed for h in handles) == 2
    assert meter().reserve_autonomous_spend("danso", input_tokens=750, requests=1).allowed
    assert not meter().reserve_autonomous_spend("danso", input_tokens=1).allowed
    selected = next(h for h in handles if h.allowed)
    meter().settle_reservation(selected, input_tokens=10, output_tokens=10, requests=1)
    assert meter().used_tokens("danso") == 870
    assert recovery(0).allowed
    assert not recovery(0).allowed
    assert not UsageMeter(tmp_path / "disabled").reserve_autonomous_spend(
        "danso", input_tokens=1, purpose="recovery").allowed


def test_actual_overage_is_not_hidden_by_reservation(tmp_path):
    meter = UsageMeter(tmp_path / "usage", budgets={"danso": 100})
    handle = meter.reserve_autonomous_spend("danso", input_tokens=90, requests=1)
    meter.settle_reservation(handle, input_tokens=200, output_tokens=20, requests=2)
    assert meter.used_tokens("danso") == 220
    assert not meter.check_autonomous_spend("danso").allowed
    with pytest.raises(ValueError):
        meter.settle_reservation(handle, input_tokens=True, output_tokens=0, requests=1)


def test_no_autonomous_admission_without_durable_shared_charge(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    meter = UsageMeter(path, budgets={"danso": 1000})
    monkeypatch.setattr(meter, "_save", lambda: None)
    assert not meter.reserve_autonomous_spend("danso", input_tokens=100, requests=1).allowed


@pytest.mark.parametrize("damage", ["symlink", "corrupt", "lock_symlink"])
def test_unsafe_or_corrupt_meter_is_preserved_and_blocks_admission(tmp_path, damage):
    path = tmp_path / "usage.json"
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("do not touch")
    if damage == "symlink":
        path.symlink_to(sentinel)
    elif damage == "corrupt":
        path.write_text("{broken historical ledger")
    else:
        (tmp_path / "usage.json.lock").symlink_to(sentinel)
    assert not UsageMeter(path).reserve_autonomous_spend("danso", input_tokens=1).allowed
    assert sentinel.read_text() == "do not touch"
    if damage == "corrupt":
        assert path.read_text() == "{broken historical ledger"


@pytest.mark.parametrize("raw", [
    '{broken ledger', '{"version":1,"days":"broken history"}',
    '{"version":1,"days":{"2026-10-04":{"danso":{"autonomous":{"requests":1,"input_tokens":-1,"output_tokens":0}}}}}',
])
def test_interactive_telemetry_cannot_erase_damage_or_reopen_admission(tmp_path, raw):
    path = tmp_path / "usage.json"
    path.write_text(raw)
    meter = UsageMeter(path, budgets={"danso": 1000})
    meter.record("danso", "interactive", input_tokens=10, requests=1)
    assert path.read_text() == raw
    assert not meter.reserve_autonomous_spend("danso", input_tokens=1).allowed
    assert not meter.check_autonomous_spend("danso").allowed
    assert path.read_text() == raw


@pytest.mark.parametrize("diagnostic", ["valid", "missing", "mismatch", "duplicate"])
def test_native_usage_is_only_from_matching_cli_diagnostics(tmp_path, diagnostic):
    counts = dict(requests=1, inputTokens=12, outputTokens=8,
                  cacheReadTokens=5, cacheWriteTokens=3, totalTokens=28)
    lines = ["DANSO_USAGE=" + json.dumps(counts), "PIRI_USAGE=" + json.dumps(counts)]
    if diagnostic == "missing":
        lines = []
    elif diagnostic == "mismatch":
        lines[-1] = 'PIRI_USAGE={}'
    elif diagnostic == "duplicate":
        lines.append(lines[0])
    script = tmp_path / "native"
    script.write_text("#!/usr/bin/python3\nimport sys\nprint('{\"usage\":0}')\n"
                      + f"print({chr(10).join(lines)!r}, file=sys.stderr)\n")
    script.chmod(0o700)
    result = asyncio.run(_run([str(script)], {}, tmp_path, 5))
    assert result.usage == (dict(input_tokens=20, output_tokens=8, requests=1)
                            if diagnostic == "valid" else None)


def test_accounting_roundtrip_distinguishes_unknown_from_actual_zero():
    legacy = DistillExtractionAccounting("provider-default", 100, 30, 900)
    assert DistillExtractionAccounting.from_dict(legacy.to_dict()).actual_requests is None
    actual = DistillExtractionAccounting("provider-default", 100, 30, 900, 0, 0, 1)
    assert DistillExtractionAccounting.from_dict(actual.to_dict()) == actual
    with pytest.raises(ValueError):
        DistillExtractionAccounting("provider-default", 100, 30, 900, 1, None, None)
