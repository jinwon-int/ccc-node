"""Tier delivery policy for the fleet alert queue (#2413 stage 2): info digest, action re-notify.

Stage 1 gave every alert a tier and an id. This module decides *when* a row
leaves the queue, as pure functions over the outbox plus one ``tick`` that the
Matrix sender calls before each delivery round. Nothing here talks to Matrix
or 「한데」; it only creates and marks rows, so both channels see the same
policy through the queue (the 「한데」 mirror copies every non-``info`` insert).

Policy (owner decision 2026-10-27, ccc-node#2413):

``info``      held, then one **digest** per day at ``DIGEST_HOUR_KST`` (09:00) —
              a single derived row ``tier=digest`` grouping every held info alert by
              event with its content line and key detail lines (within
              ``DIGEST_MAX_BYTES``); the held rows are marked delivered with
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
import re
import time
from collections import Counter
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
DIGEST_LINE_CHARS = 120     # bytes of an item's head line (``#id label summary``)
DIGEST_DETAIL_CHARS = 100   # bytes of one detail line under an item
DIGEST_DETAIL_LINES = 2     # detail lines per item at full budget (attention lines first)
DIGEST_GROUP_ITEMS = 3      # items shown per event group; older plain ones fold into "… 외 N건"
#: The 「한데」 mirror caps a notice at 8000 bytes (fleet_alerts_sender_hande.MAX_TEXT_BYTES); stay well below.
DIGEST_MAX_BYTES = 7000
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

def kst_day(now: float) -> str:
    return _dt.datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d")


def digest_due(now: float, last_day: Optional[str]) -> bool:
    """True from 09:00 KST until a digest row has been made for that KST day."""
    local = _dt.datetime.fromtimestamp(now, KST)
    return local.hour >= DIGEST_HOUR_KST and last_day != local.strftime("%Y-%m-%d")


# ---- digest ------------------------------------------------------------------

def held_info_rows(root: os.PathLike | str) -> list[tuple[str, str, str, str, float, str]]:
    """``(txn, alert_id, node, body, created, event)`` of undelivered info rows, oldest first."""
    with outbox.database(root) as conn:
        return conn.execute(
            "SELECT txn, alert_id, node, body, created, event FROM alerts"
            " WHERE delivered IS NULL AND tier='info' ORDER BY created"
        ).fetchall()


# A ccc-node spool alert's first line is only a header ("[노드A] #h3g9l4 🔔 ccc-node [AgentCronRun] · node-a");
# the content starts on line 2. The digest used to show the header alone, i.e. who sent what kind, not what.
_HEADER_RE = re.compile(r"^\[(?P<label>[^\]]+)\] #(?P<aid>[0-9a-z]+) 🔔 \S+ \[[^\]]+\](?: · \S+)?\s*$")
# Bookkeeping lines that carry no meaning in a digest (agent-cron run ids, bare timestamps/URLs, empty stream heads).
_NOISE_RE = re.compile(
    r"^(?:scheduledAt|runId|exitCode|startedAt|finishedAt|attempt)=\S*$|^(?:stdout|stderr):\s*$"
    r"|^https?://\S+$|^\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:?\d\d)?$"
)
# Explicit problem signals only: status symbols, upper-case verdict words, failure statuses and non-zero failure
# counts. Plain words like "실패"/"fail" also appear in issue titles and would reorder lists for nothing.
_ATTENTION_RE = re.compile(
    r"[⚠✗❌🚨]|\b(?:DOWN|DEGRADED|UNVERIFIED|FAIL(?:ED|URE)?|ERROR)\b|status=(?:fail\w*|error)\b"
    r"|\bfail(?:ed|ures?)=[1-9]|실패\s*[1-9]"
)


def is_attention(line: str) -> bool:
    """True for a line that reports a problem; ``failed=0`` / ``실패 0`` style zero counts are not one."""
    return bool(_ATTENTION_RE.search(line))


def _trim(line: str, limit: int) -> str:
    data = line.encode("utf-8")
    return line if len(data) <= limit else data[: limit - 3].decode("utf-8", "ignore") + "…"


def digest_item(alert_id: str, body: str) -> tuple[str, list[str], bool]:
    """``(head, detail_lines, attention)`` for one held alert.

    head = ``#id label <first content line>`` for a ccc-node header body, else the first line as-is
    (it already starts with ``[label] #id``). Details are the remaining content lines with attention
    lines first; nothing is invented and the caller trims and limits them.
    """
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    if not lines:
        return (f"#{alert_id}", [], False)
    m = _HEADER_RE.match(lines[0])
    content = [ln for ln in (lines[1:] if m else lines) if not _NOISE_RE.search(ln)]
    if m:
        head = f"#{m['aid']} {m['label']}" + (f" {content[0]}" if content else "")
    else:
        head = content[0] if content else lines[0]
    # A detail line that would *display* the same as the summary or an earlier line adds nothing: exact repeats
    # (one line per worker lane) and a cut-off preview followed by its full text both collapse to one.
    def shown(ln: str) -> str:
        return _trim(ln, DIGEST_DETAIL_CHARS)

    seen = {shown(content[0])} if content else set()
    rest = []
    for ln in content[1:]:
        if shown(ln) not in seen:
            seen.add(shown(ln))
            rest.append(ln)
    details = [ln for ln in rest if is_attention(ln)] + [ln for ln in rest if not is_attention(ln)]
    return head, details, any(is_attention(ln) for ln in content)


def _render_digest(day: str, groups, total: int, items: int, details: int) -> tuple[list[str], bool]:
    lines = [f"📋 플릿 알림 다이제스트 {day} — info {total}건 · {len(groups)}종"]
    omitted = False
    for event, members in groups:
        nodes = Counter(node or "?" for _r, node, *_ in members)
        by_node = " (" + "·".join(f"{n} {c}" for n, c in nodes.most_common()) + ")" if len(nodes) > 1 else ""
        lines.append(f"■ {event or '기타'} {len(members)}건{by_node}")
        # attention items first, then the latest plain ones; shown in time order
        ranked = sorted(members, key=lambda m: (not m[4], -m[2]))[:items]
        for _r, _node, created, head, attention, dlines in sorted(ranked, key=lambda m: m[2]):
            hhmm = _dt.datetime.fromtimestamp(created, KST).strftime("%H:%M")
            lines.append(f"• {hhmm} {_trim(head, DIGEST_LINE_CHARS)}")
            lines.extend(f"   └ {_trim(d, DIGEST_DETAIL_CHARS)}" for d in dlines[:details])
            omitted = omitted or len(dlines) > details
        if len(members) > len(ranked):
            lines.append(f"  … 외 {len(members) - len(ranked)}건")
            omitted = True
    return lines, omitted


def build_digest(rows, now: float) -> str:
    """One message for the day's held info rows, grouped by event, within ``DIGEST_MAX_BYTES``.

    Groups with an attention item come first, then by first arrival. Each item shows its content line
    and up to ``DIGEST_DETAIL_LINES`` detail lines; over budget, detail lines go first, then items, then
    whole trailing groups (noted at the end).
    """
    day = kst_day(now)
    grouped: dict[str, list] = {}
    for row in rows:
        _txn, alert_id, node, body, created = row[:5]
        event = row[5] if len(row) > 5 else ""
        head, dlines, attention = digest_item(alert_id, body)
        grouped.setdefault(event or "", []).append((alert_id, node, created, head, attention, dlines))
    groups = sorted(grouped.items(), key=lambda g: (not any(m[4] for m in g[1]), min(m[2] for m in g[1])))
    note = "… 일부 생략 — 개별 알림 메타데이터는 fleet_alerts_receipt.py로 조회"
    for items, details in ((DIGEST_GROUP_ITEMS, DIGEST_DETAIL_LINES), (DIGEST_GROUP_ITEMS, 1),
                           (DIGEST_GROUP_ITEMS, 0), (1, 0)):
        lines, omitted = _render_digest(day, groups, len(rows), items, details)
        text = "\n".join(lines + ([note] if omitted and details < DIGEST_DETAIL_LINES else []))
        if len(text.encode("utf-8")) <= DIGEST_MAX_BYTES:
            return text
    # Still too long at one item per group: drop whole groups from the end.
    kept = list(groups)
    while kept:
        kept.pop()
        lines, _ = _render_digest(day, kept, len(rows), 1, 0)
        lines[0] = f"📋 플릿 알림 다이제스트 {day} — info {len(rows)}건 · {len(groups)}종"
        dropped = groups[len(kept):]
        tail = f"… 외 {len(dropped)}종 {sum(len(m) for _e, m in dropped)}건 — fleet_alerts_receipt.py로 조회"
        text = "\n".join(lines + [tail])
        if len(text.encode("utf-8")) <= DIGEST_MAX_BYTES:
            return text
    return _trim(lines[0], DIGEST_MAX_BYTES)


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
        for held_txn, *_rest in rows:
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
