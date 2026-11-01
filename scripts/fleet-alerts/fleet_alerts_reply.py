"""Owner replies to fleet alerts — ``ok <id>`` / ``hold <id>`` → reply receipt (#2413 stage 3, family-messenger#460).

Both alert rooms can carry a reply: the Matrix sender sees owner messages in
the 🔔 플릿 알림 room (``MatrixTransport.admit_event`` already admits only the
owner's verified, pinned devices), and the 「한데」 lane sees ``turn`` frames
whose ``sender`` is ``@<device>:hande`` (MLS-authenticated by the relay bot;
only devices of the owner actor count). Parsing is **deterministic** — a
regular expression, no model — so text inside an alert body can never be
mistaken for a reply, and nothing here executes anything: a reply only
writes a ``reply`` receipt (which stops the 15-minute re-notify) and queues a
short acknowledgement row (internal tier ``ack``) so the owner sees it landed
in both rooms.

Outcomes of :func:`record_reply`: ``recorded`` · ``already`` (a reply exists;
the first one stands) · ``unknown`` (no such alert id) · ``not-actionable``
(``info``/digest/derived rows do not take replies).
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_alerts_outbox as outbox  # noqa: E402

REPLY_RE = re.compile(r"^\s*(ok|hold)\s+#?([0-9a-z]{6})\s*$", re.IGNORECASE)
ACK_TIER = "ack"
REPLY_CHANNEL = "reply"
ACTIONABLE_TIERS = ("action", "critical")
WHO_MAX = 64
KST_LABEL = "Asia/Seoul"


def parse_reply(text: object) -> Optional[tuple[str, str]]:
    """``(verb, alert_id)`` for an exact ``ok <id>`` / ``hold <id>`` line, else None."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    m = REPLY_RE.match(text)
    if not m:
        return None
    return m.group(1).lower(), m.group(2).lower()


def _who(value: object) -> str:
    s = re.sub(r"[^A-Za-z0-9@:._-]", "", str(value or ""))[:WHO_MAX]
    return s or "unknown"


def _ack_text(verb: str, alert_id: str, channel: str, who: str, now: float, outcome: str) -> str:
    from zoneinfo import ZoneInfo
    hhmm = _dt.datetime.fromtimestamp(now, ZoneInfo(KST_LABEL)).strftime("%H:%M")
    if outcome == "recorded":
        mark = "✅" if verb == "ok" else "⏸"
        tail = "재통지 중지" if verb == "ok" else "보류, 재통지 중지"
        return f"{mark} #{alert_id} {verb} 접수 {hhmm} — {who} ({channel}) · {tail}"
    if outcome == "already":
        return f"ℹ️ #{alert_id}: 이미 회신이 기록돼 있어 첫 회신을 유지합니다 ({who}, {channel})"
    if outcome == "not-actionable":
        return f"ℹ️ #{alert_id}: 회신 대상이 아닌 알림입니다(info/notice/다이제스트/파생)"
    return f"❓ #{alert_id}: 알 수 없는 알림 번호입니다 ({who}, {channel})"


def record_reply(
    queue: os.PathLike | str, verb: str, alert_id: str, *, channel: str, who: object, now: Optional[float] = None,
    ack: bool = True,
) -> dict:
    """Record one reply. Returns ``{"outcome", "alert_id", "txn", "ack_txn"}``; never raises on bad input."""
    now = time.time() if now is None else now
    who_s = _who(who)
    verb = verb.lower()
    if verb not in ("ok", "hold") or not re.fullmatch(r"[0-9a-z]{6}", alert_id or ""):
        return {"outcome": "invalid", "alert_id": alert_id, "txn": None, "ack_txn": None}
    info = outbox.lookup(queue, alert_id)
    if info is None:
        outcome, txn = "unknown", None
    elif info["tier"] not in ACTIONABLE_TIERS:
        outcome, txn = "not-actionable", info["txn"]
    elif any(r["channel"] == REPLY_CHANNEL for r in info["receipts"]):
        outcome, txn = "already", info["txn"]
    else:
        outcome, txn = "recorded", info["txn"]
        outbox.receipt(queue, txn, REPLY_CHANNEL, "reply", f"{verb}:{channel}:{who_s}")
    ack_txn = None
    if ack:
        text = _ack_text(verb, alert_id, channel, who_s, now, outcome)
        ack_txn, _ = outbox.enqueue(
            queue, text, node="relay", event="FleetAlertReply", tier=ACK_TIER,
            body_with_id=lambda aid, t=text: f"#{aid} {t}",
        )
    return {"outcome": outcome, "alert_id": alert_id, "txn": txn, "ack_txn": ack_txn}


def handle_text(queue: os.PathLike | str, text: object, *, channel: str, who: object, now: Optional[float] = None) -> Optional[dict]:
    """Parse + record in one step; None when ``text`` is not a reply line (the common case)."""
    parsed = parse_reply(text)
    if parsed is None:
        return None
    verb, alert_id = parsed
    return record_reply(queue, verb, alert_id, channel=channel, who=who, now=now)


def hande_sender_device(sender: object) -> Optional[str]:
    """``@owner-web-87576b:hande`` → ``owner-web-87576b`` (None for anything else)."""
    m = re.fullmatch(r"@([A-Za-z0-9_-]{1,64}):hande", str(sender or ""))
    return m.group(1) if m else None


def actor_of(device: str) -> str:
    """The relay's rule (DEVICES-V4 G-H3): actor = device id up to the first hyphen."""
    return device.split("-", 1)[0]
