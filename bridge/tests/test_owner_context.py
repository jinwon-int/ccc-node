"""Owner-context delivery gate (bridge/core/owner_context.py) and its MatrixSpoolNotifier hook."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core import owner_context as oc
from telegram_bot.core.matrix.bot import MatrixSpoolNotifier

KST = oc.KST
H = 3600
# 2026-11-26 14:00 KST (afternoon) and 23:30 KST (night). All data below is made up.
DAY = dt.datetime(2026, 11, 26, 14, 0, tzinfo=KST).timestamp()
NIGHT = dt.datetime(2026, 11, 26, 23, 30, tzinfo=KST).timestamp()

# The 30-cell delivery grid from the design (rows: kind, columns: situation). The reference copy
# (iphone-agent-cast server/owner_context/test_owner_context.py) pins this same grid.
GRID = """
kind      driving sleep focus-work office free unknown
approval  hold    hold  hold       send   send send
ci        hold    hold  hold       send   send send
critical  send    send  send       send   send send
action    hold    hold  send       send   send send
notice    hold    hold  hold       send   send send
"""
TTL = {"carplay": 3 * H, "place": 24 * H, "focus": 10 * H, "sleep": 12 * H, "charger": 12 * H, "ping": 36 * H}
SITUATION_PAIRS = {
    "driving": ("carplay=on",),
    "sleep": ("focus=sleep",),
    "focus-work": ("focus=work",),
    "office": ("place=office",),
    "free": ("place=away",),
}
KIND_RECORDS = {
    "approval": {"event": "ApprovalAsk", "tier": "action"},
    "ci": {"event": "DetachedJob", "tier": "notice"},
    "critical": {"event": "health-alert", "tier": "critical"},
    "action": {"event": "health-alert", "tier": "action"},
    "notice": {"event": "Notification", "tier": "notice"},
}


def state_at(now: float, *pairs: str, ping: bool = True) -> dict:
    """The owner-context service's state.json shape with each ``signal=value`` received at ``now``."""
    signals = {}
    for pair in (("ping=seen",) if ping else ()) + pairs:
        name, value = pair.split("=")
        signals[name] = {"value": value, "at": now, "expires": now + TTL[name]}
    return {"schema": "owner-context.v1", "updated": now, "signals": signals}


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, KST).isoformat()


# --- decide / situation (pure) ---------------------------------------------------------------

def test_grid_30_cells() -> None:
    rows = [r.split() for r in GRID.strip().splitlines()]
    header, rows = rows[0][1:], rows[1:]
    assert len(rows) * len(header) == 30
    for kind, *cells in rows:
        for column, want in zip(header, cells):
            state = state_at(DAY, *SITUATION_PAIRS[column]) if column != "unknown" else None
            assert oc.situation(state, DAY) == column
            record = dict(KIND_RECORDS[kind], ts=iso(DAY), text="x")
            assert oc.decide(record, DAY, state) == want, (kind, column)


def test_situation_priority_and_unknown() -> None:
    assert oc.situation(state_at(DAY, "place=office", "focus=work", "carplay=on"), DAY) == "driving"
    assert oc.situation(state_at(DAY, "focus=work", "sleep=bedtime"), DAY) == "sleep"
    assert oc.situation(state_at(DAY, "place=office", "focus=work"), DAY) == "focus-work"
    assert oc.situation(state_at(DAY), DAY) == "unknown"  # only a ping
    assert oc.situation(state_at(DAY, "carplay=on", ping=False), DAY) == "unknown"
    assert oc.situation(state_at(DAY - 36 * H, "carplay=on"), DAY) == "unknown"  # stale ping
    assert oc.situation({"signals": "x"}, DAY) == "unknown"
    expired = state_at(DAY, "carplay=on")
    expired["signals"]["carplay"]["value"] = "expired"
    assert oc.situation(expired, DAY) == "unknown"


def test_sleep_guess_uses_the_sleep_column() -> None:
    state = state_at(NIGHT, "place=home", "charger=on")
    assert oc.situation(state, NIGHT) == "sleep-guess"
    assert oc.situation(state_at(DAY, "place=home", "charger=on"), DAY) == "free"
    assert oc.decide({"tier": "action", "ts": iso(NIGHT)}, NIGHT, state) == "hold"


def test_info_untiered_and_critical_are_never_held() -> None:
    state = state_at(DAY, "carplay=on")
    for record in ({"event": "SelfUpdate"}, {"tier": "info"}, {"tier": "bogus"},
                   {"event": "ApprovalAsk", "tier": "critical"}, {"tier": "CRITICAL"}):
        assert oc.decide(record, DAY, state) == "send", record
    assert oc.decide({"event": "ApprovalAsk"}, DAY, state) == "hold"


def test_max_hold_boundaries() -> None:
    for pair, limit in (("carplay=on", 3 * H), ("focus=work", 4 * H)):
        state = state_at(DAY + limit - 60, pair)
        record = {"tier": "notice", "ts": iso(DAY)}
        assert oc.decide(record, DAY + limit - 1, state) == "hold", pair
        assert oc.decide(record, DAY + limit, state) == "send", pair
    state = state_at(NIGHT, "focus=sleep")
    eight = dt.datetime(2026, 11, 27, 8, 0, tzinfo=KST).timestamp()
    assert oc.decide({"tier": "action", "ts": iso(NIGHT)}, eight - 1, state) == "hold"
    assert oc.decide({"tier": "action", "ts": iso(NIGHT)}, eight, state) == "send"


def test_record_time_prefers_ts_then_created() -> None:
    assert oc.record_time({"ts": "2026-11-26T05:00:00Z"}) == DAY
    assert oc.record_time({"ts": "2026-11-26T14:00:00+0900"}) == DAY
    assert oc.record_time({"ts": "2026-11-26T05:00:00"}, created=1.0) == 1.0  # no offset
    assert oc.record_time({"ts": "T"}, created=2.0) == 2.0
    assert oc.record_time({}) is None


def test_broken_input_is_send() -> None:
    state = state_at(DAY, "carplay=on")
    assert oc.decide("not a dict", DAY, state) == "send"
    assert oc.decide({"tier": "action"}, "not a time", state) == "send"
    assert oc.decide({"tier": "action"}, DAY, {"signals": {"ping": "x"}}) == "send"


# --- gate ---------------------------------------------------------------------------------------

def _gate(tmp_path: Path, mode: str, state: dict | None, now: float) -> oc.OwnerContextGate:
    sf = tmp_path / "ctx" / "state.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    if state is not None:
        sf.write_text(json.dumps(state), encoding="utf-8")
    return oc.OwnerContextGate(sf, mode, tmp_path / "log" / "decisions.jsonl", clock=lambda: now)


def _spool(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / "spool" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _log(gate: oc.OwnerContextGate) -> list[dict]:
    assert gate.log_path is not None
    if not gate.log_path.exists():
        return []
    return [json.loads(x) for x in gate.log_path.read_text().splitlines()]


def test_gate_from_env_is_off_by_default() -> None:
    assert oc.gate_from_env({}) is None
    assert oc.gate_from_env({"CCC_OWNER_CONTEXT_FILE": "  "}) is None
    gate = oc.gate_from_env({"CCC_OWNER_CONTEXT_FILE": "/x/state.json"})
    assert gate is not None and gate.mode == "shadow"
    assert oc.gate_from_env({"CCC_OWNER_CONTEXT_FILE": "/x", "CCC_OWNER_CONTEXT_MODE": "hold"}).mode == "hold"
    assert oc.gate_from_env({"CCC_OWNER_CONTEXT_FILE": "/x", "CCC_OWNER_CONTEXT_MODE": "yes"}).mode == "shadow"


def test_shadow_mode_never_holds_and_logs_once(tmp_path: Path) -> None:
    gate = _gate(tmp_path, "shadow", state_at(DAY, "carplay=on"), DAY)
    data = {"event": "ApprovalAsk", "tier": "action", "ts": iso(DAY), "text": "승인 요청 본문"}
    p = _spool(tmp_path, "a.json", data)
    assert gate.should_hold(p, data) is False
    assert gate.should_hold(p, data) is False
    log = _log(gate)
    assert [(e["action"], e["record"], e["situation"]) for e in log] == [("would-hold", "a.json", "driving")]
    assert "승인 요청 본문" not in gate.log_path.read_text()  # never the text
    assert stat.S_IMODE(os.stat(gate.log_path).st_mode) == 0o600


def test_hold_mode_holds_then_releases_when_the_situation_ends(tmp_path: Path) -> None:
    now = [DAY]
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps(state_at(DAY, "carplay=on")))
    gate = oc.OwnerContextGate(sf, "hold", tmp_path / "decisions.jsonl", clock=lambda: now[0])
    data = {"tier": "notice", "ts": iso(DAY), "text": "x"}
    p = _spool(tmp_path, "a.json", data)
    assert gate.should_hold(p, data) is True
    assert gate.holding("a.json")
    assert gate.should_hold(p, data) is True  # still driving: one 'held' line only
    now[0] = DAY + 120
    sf.write_text(json.dumps(state_at(DAY + 60, "place=home")))  # arrived home
    os.utime(sf, ns=(int(now[0] * 1e9), int(now[0] * 1e9)))  # new mtime -> cache refresh
    assert gate.should_hold(p, data) is False
    assert not gate.holding("a.json")
    assert [e["action"] for e in _log(gate)] == ["held", "released"]


def test_gate_fails_open(tmp_path: Path) -> None:
    data = {"tier": "action", "ts": iso(DAY), "text": "x"}
    p = _spool(tmp_path, "a.json", data)
    assert _gate(tmp_path / "missing", "hold", None, DAY).should_hold(p, data) is False
    broken = _gate(tmp_path / "broken", "hold", None, DAY)
    broken.state_file.write_text("{broken")
    assert broken.should_hold(p, data) is False
    gate = _gate(tmp_path / "ok", "hold", state_at(DAY, "carplay=on"), DAY)
    gate._state = None  # type: ignore[assignment]  # an internal error must not raise
    assert gate.should_hold(p, data) is False


def test_gate_skips_the_state_file_for_records_it_never_holds(tmp_path: Path) -> None:
    gate = _gate(tmp_path, "hold", state_at(DAY, "carplay=on"), DAY)
    calls = []
    real = gate._state
    gate._state = lambda: calls.append(1) or real()  # type: ignore[assignment]
    for data in ({"event": "SelfUpdate", "text": "x"}, {"tier": "critical", "text": "x"}):
        assert gate.should_hold(_spool(tmp_path, "b.json", data), data) is False
    assert calls == []


def test_decision_log_keeps_14_days(tmp_path: Path) -> None:
    gate = _gate(tmp_path, "shadow", state_at(DAY, "carplay=on"), DAY)
    gate.log_path.parent.mkdir(parents=True, exist_ok=True)
    old = json.dumps({"at": iso(DAY - 15 * 24 * H), "action": "would-hold"})
    recent = json.dumps({"at": iso(DAY - 13 * 24 * H), "action": "would-hold"})
    gate.log_path.write_text(old + "\n" + recent + "\n")
    data = {"tier": "notice", "ts": iso(DAY), "text": "x"}
    gate.should_hold(_spool(tmp_path, "a.json", data), data)
    assert [e["at"] for e in _log(gate)] == [iso(DAY - 13 * 24 * H), iso(DAY)[:19] + "+09:00"]


# --- MatrixSpoolNotifier hook -----------------------------------------------------------------

DIRECT = "!direct:hs"


def _notifier(tmp_path: Path, gate: oc.OwnerContextGate | None):
    settings = SimpleNamespace(push_enabled=True, push_spool_dir=str(tmp_path / "spool"),
                               push_poll_interval=0.01, push_max_per_minute=10)
    enqueued: list[tuple[str, str]] = []

    class _T:
        policy = SimpleNamespace(rooms={DIRECT: "direct"})

        def enqueue_notice(self, room: str, text: str) -> None:
            enqueued.append((room, text))

    notifier = MatrixSpoolNotifier(settings, _T())
    notifier._owner_context = gate
    (tmp_path / "spool" / "sent").mkdir(parents=True, exist_ok=True)
    return notifier, enqueued


def test_notifier_has_no_gate_without_the_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CCC_OWNER_CONTEXT_FILE", raising=False)
    settings = SimpleNamespace(push_enabled=True, push_spool_dir=str(tmp_path / "spool"))
    assert MatrixSpoolNotifier(settings, None)._owner_context is None
    monkeypatch.setenv("CCC_OWNER_CONTEXT_FILE", str(tmp_path / "state.json"))
    assert MatrixSpoolNotifier(settings, None)._owner_context is not None


@pytest.mark.anyio
async def test_drain_keeps_a_held_record_and_later_records_still_go(tmp_path: Path) -> None:
    now = dt.datetime.now(KST).timestamp()
    gate = _gate(tmp_path, "hold", state_at(now, "carplay=on"), now)
    notifier, enqueued = _notifier(tmp_path, gate)
    _spool(tmp_path, "1-approval.json", {"event": "ApprovalAsk", "tier": "action", "ts": iso(now), "text": "승인?"})
    _spool(tmp_path, "2-critical.json", {"event": "health-alert", "tier": "critical", "ts": iso(now), "text": "장애"})
    _spool(tmp_path, "3-info.json", {"event": "SelfUpdate", "ts": iso(now), "text": "업데이트"})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert [t.split("\n")[1] for _, t in enqueued] == ["장애", "업데이트"]
    assert [p.name for p in (tmp_path / "spool").glob("*.json")] == ["1-approval.json"]
    assert notifier.pending_count() == 1


@pytest.mark.anyio
async def test_drain_shadow_mode_delivers_everything(tmp_path: Path) -> None:
    now = dt.datetime.now(KST).timestamp()
    gate = _gate(tmp_path, "shadow", state_at(now, "carplay=on"), now)
    notifier, enqueued = _notifier(tmp_path, gate)
    _spool(tmp_path, "1-approval.json", {"event": "ApprovalAsk", "tier": "action", "ts": iso(now), "text": "승인?"})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert len(enqueued) == 1 and not list((tmp_path / "spool").glob("*.json"))
    assert [e["action"] for e in _log(gate)] == ["would-hold"]


@pytest.mark.anyio
async def test_drain_without_a_gate_is_unchanged(tmp_path: Path) -> None:
    notifier, enqueued = _notifier(tmp_path, None)
    _spool(tmp_path, "1-approval.json", {"event": "ApprovalAsk", "tier": "action", "text": "승인?"})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert len(enqueued) == 1


def test_relay_exit_drain_does_not_wait_for_held_records(tmp_path: Path) -> None:
    now = dt.datetime.now(KST).timestamp()
    gate = _gate(tmp_path, "hold", state_at(now, "carplay=on"), now)
    notifier, _ = _notifier(tmp_path, gate)
    notifier.relay_mode = True
    data = {"tier": "notice", "ts": iso(now), "text": "x"}
    p = _spool(tmp_path, "1.json", data)
    _spool(tmp_path, "2.json", {"text": "y"})
    assert notifier.pending_count(relay_only=True) == 2
    assert gate.should_hold(p, data) is True
    assert notifier.pending_count(relay_only=True) == 1


def test_owner_zone_falls_back_to_fixed_utc9_without_a_tz_database(monkeypatch):
    # Termux has no system zoneinfo and the bridge venv sees no global tzdata; importing this
    # module must not crash there (Termux bridges, 2026-11-27).
    def missing(key):
        raise oc.ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(oc, "ZoneInfo", missing)
    zone = oc._owner_zone()
    assert zone.utcoffset(None) == dt.timedelta(hours=9)
    for ts in (DAY, NIGHT, dt.datetime(2026, 7, 1, 3, 0, tzinfo=dt.timezone.utc).timestamp()):
        assert dt.datetime.fromtimestamp(ts, zone).utcoffset() == dt.timedelta(hours=9)
        assert dt.datetime.fromtimestamp(ts, zone).time() == dt.datetime.fromtimestamp(ts, KST).time()
