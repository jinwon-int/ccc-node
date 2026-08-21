#!/usr/bin/env python3
"""NL time-reference → as_of instant estimation (#871 remaining slice).

Slice 1 built the explicit `as_of` retrieval mode; this module is the
auto-expansion the issue deferred: estimate a time reference from the natural
-language query itself and hand the caller an instant to retrieve "as of".

Design rules (conservative by contract):
- Fire ONLY on explicit absolute dates or strong relative markers
  (어제/N일 전/지난주/작년/yesterday/N days ago/last week/...). Casual time
  words that carry no retrieval intent (오늘/today, 이번 주말의 날씨 같은 비시점
  명사구 없이는) must not flip the mode.
- A period mention resolves to the END of the period: "지난주 시점" asks
  "what was true by the end of last week", and slice 1's boundary rule
  (valid_from inclusive / valid_until exclusive) does the rest.
- Absolute beats relative when both appear; two different absolute dates are
  ambiguous and yield no expansion rather than a coin flip.
- Anything unparseable or ambiguous returns a None instant with a reason —
  callers degrade to current mode with a body-free signal, never dropping or
  hiding facts (the issue's fail-safe contract).

Stdlib only; no LLM, no network.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Absolute dates
# ---------------------------------------------------------------------------

# 2026-03-15 / 2026/3/15 / 2026.3.15 — 한글이 붙어도 매칭되도록 숫자 경계만 검사
_ISO_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
# 2026년 3월 15일 / 2026년 3월
_KO_YMD = re.compile(r"((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_KO_YM = re.compile(r"((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월(?!\s*\d)")
# 3월 15일
_KO_MD = re.compile(r"(?<![\d년월])(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# bare month-day like 3/15 (guarded: not part of a longer number run)
_NUM_MD = re.compile(r"(?<![\d/.-])(\d{1,2})/(\d{1,2})(?![\d/.-])")

_MONTH_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
# in March 2025 / in 2025
_EN_MONTH_YEAR = re.compile(
    r"\bin\s+(" + "|".join(_MONTH_EN) + r")\s+((?:19|20)\d{2})\b", re.IGNORECASE)
_EN_YEAR = re.compile(r"\bin\s+((?:19|20)\d{2})\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Relative / period markers
# ---------------------------------------------------------------------------

_KO_REL = [
    (re.compile(r"그저께|그제"), "days", 2, "ko:그제"),
    (re.compile(r"어제"), "days", 1, "ko:어제"),
    (re.compile(r"(\d+)\s*일\s*전"), "days-n", None, "ko:N일전"),
    (re.compile(r"(\d+)\s*주\s*전"), "weeks-n", None, "ko:N주전"),
    (re.compile(r"(\d+)\s*개월\s*전"), "months-n", None, "ko:N개월전"),
    (re.compile(r"(\d+)\s*년\s*전"), "years-n", None, "ko:N년전"),
    (re.compile(r"지난주|저번주"), "last-week", None, "ko:지난주"),
    (re.compile(r"지난달|저번\s*달"), "last-month", None, "ko:지난달"),
    (re.compile(r"작년"), "last-year", None, "ko:작년"),
]

_EN_REL = [
    (re.compile(r"\byesterday\b", re.IGNORECASE), "days", 1, "en:yesterday"),
    (re.compile(r"\b(\d+)\s*days?\s+ago\b", re.IGNORECASE), "days-n", None, "en:Ndaysago"),
    (re.compile(r"\b(\d+)\s*weeks?\s+ago\b", re.IGNORECASE), "weeks-n", None, "en:Nweeksago"),
    (re.compile(r"\b(\d+)\s*months?\s+ago\b", re.IGNORECASE), "months-n", None, "en:Nmonthsago"),
    (re.compile(r"\b(\d+)\s*years?\s+ago\b", re.IGNORECASE), "years-n", None, "en:Nyearsago"),
    (re.compile(r"\blast\s+week\b", re.IGNORECASE), "last-week", None, "en:lastweek"),
    (re.compile(r"\blast\s+month\b", re.IGNORECASE), "last-month", None, "en:lastmonth"),
    (re.compile(r"\blast\s+year\b", re.IGNORECASE), "last-year", None, "en:lastyear"),
]

_MONTH_LENGTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _end_of_day(dt):
    eod = dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return eod if eod.tzinfo is not None else eod.astimezone()


def _add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, _MONTH_LENGTH[month - 1])
    return dt.replace(year=year, month=month, day=day)


def _resolve_relative(kind, n, rule, now):
    """Every period resolves to its END instant (docstring rule)."""
    if kind == "days":
        return _end_of_day(now - timedelta(days=n)), rule
    if kind == "days-n":
        return _end_of_day(now - timedelta(days=n)), rule
    if kind == "weeks-n":
        return _end_of_day(now - timedelta(weeks=n)), rule
    if kind == "months-n":
        return _end_of_day(_add_months(now, -n)), rule
    if kind == "years-n":
        return _end_of_day(_add_months(now, -12 * n)), rule
    if kind == "last-week":
        # End of the previous ISO week (Sunday 23:59:59 local).
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return (monday - timedelta(seconds=1)), rule
    if kind == "last-month":
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return (first - timedelta(seconds=1)), rule
    if kind == "last-year":
        first = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return (first - timedelta(seconds=1)), rule
    return None, None


def _absolute_candidates(query, now):
    """(dt, rule) for every absolute date mention; month-only → month END."""
    out = []
    for m in _ISO_DATE.finditer(query):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        out.append((_end_of_day(datetime(y, mo, d)), "abs:iso-date"))
    for m in _KO_YMD.finditer(query):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        out.append((_end_of_day(datetime(y, mo, d)), "abs:ko-ymd"))
    for m in _KO_YM.finditer(query):
        y, mo = int(m.group(1)), int(m.group(2))
        end = (_add_months(datetime(y, mo, 1), 1) - timedelta(seconds=1)).astimezone()
        out.append((end, "abs:ko-ym"))
    for m in _KO_MD.finditer(query):
        mo, d = int(m.group(1)), int(m.group(2))
        out.append((_end_of_day(datetime(now.year, mo, d)), "abs:ko-md"))
    for m in _NUM_MD.finditer(query):
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            out.append((_end_of_day(datetime(now.year, mo, d)), "abs:num-md"))
    for m in _EN_MONTH_YEAR.finditer(query):
        mo, y = _MONTH_EN[m.group(1).lower()], int(m.group(2))
        end = (_add_months(datetime(y, mo, 1), 1) - timedelta(seconds=1)).astimezone()
        out.append((end, "abs:en-month-year"))
    for m in _EN_YEAR.finditer(query):
        y = int(m.group(1))
        out.append((_end_of_day(datetime(y, 12, 31)), "abs:en-year"))
    return out


def estimate_as_of(query, now=None):
    """Estimate an as_of instant from a natural-language query.

    Returns a dict:
      {"ts": <epoch>, "iso": <local iso seconds>, "rule": <matched rule>}
    or, when no safe expansion exists:
      {"ts": None, "reason": "no-time-reference"|"ambiguous-absolute-dates"|...}
    """
    now = now or datetime.now().astimezone()
    query = (query or "").strip()
    if not query:
        return {"ts": None, "reason": "empty-query"}

    absolutes = _absolute_candidates(query, now)
    if absolutes:
        distinct = {(dt.year, dt.month, dt.day) for dt, _rule in absolutes}
        if len(distinct) > 1:
            return {"ts": None, "reason": "ambiguous-absolute-dates"}
        dt, rule = absolutes[0]
        return {"ts": dt.timestamp(), "iso": dt.isoformat(timespec="seconds"), "rule": rule}

    for pattern, kind, fixed_n, rule in _KO_REL + _EN_REL:
        m = pattern.search(query)
        if not m:
            continue
        if kind.endswith("-n"):
            n = int(m.group(1))
            if n > 3650:
                return {"ts": None, "reason": "relative-out-of-range"}
        else:
            n = fixed_n or 0
        dt, rule = _resolve_relative(kind, n, rule, now)
        if dt is not None:
            return {"ts": dt.timestamp(), "iso": dt.isoformat(timespec="seconds"), "rule": rule}
    return {"ts": None, "reason": "no-time-reference"}


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(estimate_as_of(" ".join(sys.argv[1:])), ensure_ascii=False))
