"""Tier delivery policy for the fleet alert queue (#2413 stage 2): info digest, action re-notify.

Stage 1 gave every alert a tier and an id. This module decides *when* a row
leaves the queue, as pure functions over the outbox plus one ``tick`` that the
Matrix sender calls before each delivery round. Nothing here talks to Matrix
or 「한데」; it only creates and marks rows, so both channels see the same
policy through the queue (the 「한데」 mirror copies every non-``info`` insert).

Policy (owner decision 2026-10-27, ccc-node#2413):

``info``      held, then one **digest** per day at ``DIGEST_HOUR_KST`` (09:00) —
              a single derived row ``tier=digest`` listing ``#id [node] first line``
              of every held info alert; the held rows are marked delivered with
              a ``digested`` receipt pointing at the digest id.
``action``    delivered at once; **re-notified once** ``RENOTIFY_AFTER_S`` (15 min)
              after its strong Matrix receipt unless a ``reply`` receipt (the
              responder stage, ``ok/hold <id>``) arrived — a derived row
              ``tier=renotify`` that quotes the original id.
``critical``  delivered at once, no digest; re-notified like ``action``.

Derived tiers (``digest``, ``renotify``) are internal: ``normalize_tier``
never produces them from a payload, so a node cannot inject one.
Fail-closed shape: a policy error never blocks ``action``/``critical``
delivery — ``tick`` catches and logs, and ``pending`` only ever *excludes*
``info`` rows.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from typing import Optional
from zoneinfo import ZoneInfo

import fleet_alerts_outbox as outbox

KST = ZoneInfo("Asia/Seoul")
DIGEST_HOUR_KST = 9
DIGEST_TIER = "digest"
RENOTIFY_TIER = "renotify"
DERIVED_TIERS = (DIGEST_TIER, RENOTIFY_TIER)
IMMEDIATE_TIERS = ("action", "critical")
RENOTIFY_AFTER_S = 15 * 60
RENOTIFY_LINE_CHARS = 160
DIGEST_LINE_CHARS = 120
DIGEST_MAX_LINES = 60
META_DIGEST_DAY = "digest_last_day"  # KST date of the last digest row, YYYY-MM-DD


# ---- meta -----------------------------------------------------------------

def _meta_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def meta_get(root: os.PathLike | str, key: str) -> Optional[str]:
    with outbox.database(root) as conn:
        _meta_table(conn)
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(root: os.PathLike | str, key: str, value: str) -> None:
    with outbox.database(root) as conn:
        _meta_table(conn)
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, value))


# ---- helpers ---------------------------------------------------------------

def first_line(body: str, limit: int) -> str:
    line = (body or "").strip().splitlines()[0] if (body or "").strip() else ""
    data = line.encode("utf-8")
    if len(data) <= limit:
        return line
    return data[: limit - 1].decode("utf-8", "ignore") + "…"


def kst_day(now: float) -> str:
    return _dt.datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d")


def digest_due(now: float, last_day: Optional[str]) -> bool:
    """True from 09:00 KST until a digest row has been made for that KST day."""
    local = _dt.datetime.fromtimestamp(now, KST)
    return local.hour >= DIGEST_HOUR_KST and last_day != local.strftime("%Y-%m-%d")


# ---- digest ------------------------------------------------------------------

def held_info_rows(root: os.PathLike | str) -> list[tuple[str, str, str, str, float]]:
    """``(txn, alert_id, node, body, created)`` of undelivered info rows, oldest first."""
    with outbox.database(root) as conn:
        return conn.execute(
            "SELECT txn, alert_id, node, body, created FROM alerts"
            " WHERE delivered IS NULL AND tier='info' ORDER BY created"
        ).fetchall()


def build_digest(rows, now: float) -> str:
    day = kst_day(now)
    lines = [f"📋 플릿 알림 다이제스트 {day} — info {len(rows)}건"]
    for txn, alert_id, node, body, created in rows[:DIGEST_MAX_LINES]:
        hhmm = _dt.datetime.fromtimestamp(created, KST).strftime("%H:%M")
        # The stored body already starts with "[label] #id " — keep its first line, trimmed.
        lines.append(f"• {hhmm} {first_line(body, DIGEST_LINE_CHARS)}")
    if len(rows) > DIGEST_MAX_LINES:
        lines.append(f"… 외 {len(rows) - DIGEST_MAX_LINES}건 (fleet_alerts_receipt.py로 조회)")
    return "\n".join(lines)


def make_digest(root: os.PathLike | str, now: Optional[float] = None) -> Optional[str]:
    """Create today's digest row from the held info rows; returns its txn (None when nothing to do).

    Always records the KST day, so an empty day does not re-check every second.
    """
    now = time.time() if now is None else now
    rows = held_info_rows(root)
    if not rows:
        meta_set(root, META_DIGEST_DAY, kst_day(now))
        return None
    text = build_digest(rows, now)
    txn, _ = outbox.enqueue(
        root, text, node="relay", event="FleetAlertDigest", tier=DIGEST_TIER,
        body_with_id=lambda aid: f"#{aid} {text}",
    )
    digest_id = outbox.alert_id_of(root, txn) or txn
    with outbox.database(root) as conn:
        for held_txn, _aid, _node, _body, _created in rows:
            conn.execute(
                "UPDATE alerts SET delivered=?, body='' WHERE txn=? AND delivered IS NULL", (now, held_txn)
            )
            conn.execute(
                "INSERT OR REPLACE INTO receipts(txn,channel,kind,ref,at) VALUES (?,?,?,?,?)",
                (held_txn, "digest", "digested", digest_id, now),
            )
    meta_set(root, META_DIGEST_DAY, kst_day(now))
    logging.info("fleet alert digest created txn=%s rows=%d", txn, len(rows))
    return txn


# ---- re-notify -------------------------------------------------------------

def due_renotify(root: os.PathLike | str, now: float):
    """Delivered action/critical rows older than RENOTIFY_AFTER_S with no reply and no re-notify yet."""
    with outbox.database(root) as conn:
        return conn.execute(
            "SELECT a.txn, a.alert_id, a.node, a.tier, r.ref, a.delivered FROM alerts a"
            " JOIN receipts r ON r.txn=a.txn AND r.channel='matrix' AND r.kind='matrix-event'"
            " WHERE a.tier IN ('action','critical') AND a.delivered IS NOT NULL AND a.delivered <= ?"
            " AND NOT EXISTS (SELECT 1 FROM receipts x WHERE x.txn=a.txn AND x.kind IN ('reply','renotify'))"
            " ORDER BY a.delivered",
            (now - RENOTIFY_AFTER_S,),
        ).fetchall()


def make_renotify(root: os.PathLike | str, now: Optional[float] = None) -> list[str]:
    """One derived ``renotify`` row per due alert; marks the original so it happens once."""
    now = time.time() if now is None else now
    made = []
    for txn, alert_id, node, tier, _event_ref, delivered in due_renotify(root, now):
        mins = int((now - delivered) // 60)
        label = "🚨" if tier == "critical" else "⏰"
        text = (f"{label} 재통지 #{alert_id} ({tier}, {mins}분 전 알림, 회신 없음) — "
                f"`ok {alert_id}` 또는 `hold {alert_id}`로 답하면 재통지가 멈춥니다. 상세: fleet_alerts_receipt.py {alert_id}")
        new_txn, _ = outbox.enqueue(
            root, text, node=node, event="FleetAlertRenotify", tier=RENOTIFY_TIER,
            body_with_id=lambda aid, t=text: f"#{aid} {t}",
        )
        outbox.receipt(root, txn, "renotify", "renotify", outbox.alert_id_of(root, new_txn) or new_txn)
        made.append(new_txn)
    if made:
        logging.info("fleet alert re-notify rows=%d", len(made))
    return made


# ---- tick --------------------------------------------------------------------

def tick(root: os.PathLike | str, now: Optional[float] = None) -> dict:
    """Run the policy once (cheap when nothing is due). Never raises."""
    now = time.time() if now is None else now
    out = {"digest": None, "renotify": 0}
    try:
        if digest_due(now, meta_get(root, META_DIGEST_DAY)):
            out["digest"] = make_digest(root, now)
    except Exception as exc:  # policy must never block action/critical delivery
        logging.warning("fleet alert digest skipped reason=%s", type(exc).__name__)
    try:
        out["renotify"] = len(make_renotify(root, now))
    except Exception as exc:
        logging.warning("fleet alert re-notify skipped reason=%s", type(exc).__name__)
    return out


def pending_deliverable(root: os.PathLike | str) -> Optional[tuple[str, str]]:
    """Oldest pending row that is not a held ``info`` row (action/critical/digest/renotify)."""
    with outbox.database(root) as conn:
        return conn.execute(
            "SELECT txn,body FROM alerts WHERE delivered IS NULL AND tier<>'info' ORDER BY created LIMIT 1"
        ).fetchone()
