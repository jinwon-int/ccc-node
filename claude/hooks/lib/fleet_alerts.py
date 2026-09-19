#!/usr/bin/env python3
"""Surface unread fleet alert issues at SessionStart.

Why this exists
---------------
`wiki-log-rotate` failed every day from 2026-09-14 to 09-18 and its alarm
worked perfectly the whole time: issue #5069 was opened at 05:11 on the first
failure and collected a comment on every subsequent one. Author on all five:
`github-actions`. Human responses: zero.

`pages/log.md` grew past its 200KB lint limit with 124 un-rotated entries, and
every node's wiki PR started hitting rebase conflicts in the generated routing
files because `wiki-manifest-refresh` was down for the same reason. That is
four days of compounding damage behind a signal that was firing correctly.

So the gap this closes is **not detection**. It is delivery: a GitHub issue in
a repo nobody opens is a dead channel, and making the alarm more precise would
only have put a better message into it. The fleet's read path is the
SessionStart snapshot -- every node, every session -- so that is where an
unacknowledged alert belongs.

"Unacknowledged" is the load-bearing word. An alert with a human comment is
being handled and must not nag; an alert whose only voices are bots is the
#5069 case exactly. The refresher records `last_human_comment_at` so this
module can tell those apart without reading issue bodies.

Like the promises and detached-job blocks this is the read side of a durable
record, shaped for a hook: stdlib only, fail-open, and **silent when nothing
is unacknowledged** so the common case leaves the loader's output
byte-identical.

Deliberately not here
---------------------
No network. The refresher (`refresh-memory.sh`) writes the cache out-of-band;
a `gh` call on the SessionStart path would put GitHub's availability in front
of every session start on every node. A stale cache is a tolerable failure and
a hung session start is not.

Usage::

    python3 fleet_alerts.py [<cache path>] [--max-bytes N]

With no argument the path is resolved from ``CCC_FLEET_ALERTS_CACHE``, then the
default under the memory cache dir.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sys
from typing import Any, Dict, List

MAX_ROWS = 6
DEFAULT_MAX_BYTES = 1200
# An alert younger than this has not had a chance to be seen yet; nagging about
# it on the first session after it fires would train the reader to skip the
# block, which is the failure mode this module exists to fix.
MIN_AGE_HOURS = 6.0


# Options whose NEXT argv element is a value, not a positional path. Without
# this the positional scan claims `--max-bytes`'s argument as the cache path,
# which is how the first end-to-end run of this module came back silent while
# the same cache rendered fine from the command line.
_VALUE_OPTS = {"--max-bytes"}


def _positionals(argv: List[str]) -> List[str]:
    out: List[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in _VALUE_OPTS:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        out.append(arg)
    return out


def cache_path(argv: List[str]) -> str:
    positional = _positionals(argv)
    if positional:
        return positional[0]
    env = os.environ.get("CCC_FLEET_ALERTS_CACHE")
    if env:
        return env
    base = os.environ.get("CCC_MEMORY_CACHE_DIR") or os.path.join(
        os.environ.get("HOME") or "/root", ".claude", "hooks", "cache"
    )
    return os.path.join(base, "fleet-alerts.json")


def _max_bytes(argv: List[str]) -> int:
    for i, arg in enumerate(argv):
        if arg == "--max-bytes" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return DEFAULT_MAX_BYTES
    return DEFAULT_MAX_BYTES


def load_alerts(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    alerts = payload.get("alerts") if isinstance(payload, dict) else payload
    return [rec for rec in (alerts or []) if isinstance(rec, dict)]


def _parsed(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_hours(value: Any, *, now: datetime) -> float | None:
    parsed = _parsed(value)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 3600.0


def unacknowledged(
    alerts: List[Dict[str, Any]], *, now: datetime | None = None
) -> List[Dict[str, Any]]:
    """Alerts old enough to report and with no human voice on them.

    A human comment means someone is on it. Bot comments mean the opposite --
    the lane is still failing and still unread, which is the whole point.
    """

    now = now or datetime.now(timezone.utc)
    out = []
    for rec in alerts:
        if str(rec.get("state") or "open").lower() != "open":
            continue
        age = _age_hours(rec.get("created_at"), now=now)
        if age is None or age < MIN_AGE_HOURS:
            continue
        if _parsed(rec.get("last_human_comment_at")) is not None:
            continue
        rec = dict(rec)
        rec["_age_hours"] = age
        out.append(rec)
    return sorted(out, key=lambda r: -float(r.get("_age_hours") or 0))


def _row(rec: Dict[str, Any]) -> str:
    repo = str(rec.get("repo") or "").strip()
    number = rec.get("number")
    # Compared against None rather than tested for truthiness so issue 0 still
    # renders as an identity instead of a bare repo name.
    head = f"{repo}#{number}" if repo and number is not None else repo or "?"
    title = " ".join(str(rec.get("title") or "").split())[:90]
    days = int(float(rec.get("_age_hours") or 0) // 24)
    age = f"{days}일" if days >= 1 else "<1일"
    row = f"- {head} ({age} 경과"
    bots = rec.get("bot_comments")
    if isinstance(bots, int) and bots > 0:
        row += f", 봇 코멘트 {bots}건"
    row += ")"
    if title:
        row += f" — {title}"
    return row


def render(alerts: List[Dict[str, Any]], *, now: datetime | None = None) -> str:
    """Return the block body, or '' when nothing is unacknowledged."""
    rows = unacknowledged(alerts, now=now)
    if not rows:
        return ""
    lines = [
        "아래 경보는 봇만 말하고 있습니다 — 사람이 응답한 기록이 없습니다."
        " 운영자에게 보고하거나 직접 확인할 것:",
    ]
    for rec in rows[:MAX_ROWS]:
        lines.append(_row(rec))
    hidden = len(rows) - MAX_ROWS
    if hidden > 0:
        lines.append(f"- …외 {hidden}건")
    return "\n".join(lines)


def limit_bytes(text: str, cap: int) -> str:
    """Truncate on a UTF-8 boundary, mirroring memory_render.limit-bytes."""
    if cap <= 0:
        return text
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    marker = "\n…(truncated)"
    room = max(0, cap - len(marker.encode("utf-8")))
    return raw[:room].decode("utf-8", "ignore") + marker


def main(argv: List[str]) -> int:
    try:
        body = render(load_alerts(cache_path(argv)))
    except Exception:  # noqa: BLE001 - fail open, never break the loader
        return 0
    if body:
        sys.stdout.write(limit_bytes(body, _max_bytes(argv)))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
