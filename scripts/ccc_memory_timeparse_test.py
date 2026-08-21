#!/usr/bin/env python3
"""Unit tests for ccc_memory_timeparse.py (#871 remaining slice).

Run directly (prints a PASS=<n> FAIL=<n> tally for the harness suite
contract). The `now` anchor is fixed so every expectation is deterministic.
"""
from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD_PATH = ROOT / "scripts" / "ccc_memory_timeparse.py"

spec = importlib.util.spec_from_file_location("ccc_memory_timeparse", MOD_PATH)
MOD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MOD)
estimate_as_of = MOD.estimate_as_of

# Fixed anchor: Friday 2026-08-21 15:30:00 local (naive == local for the anchor;
# the estimator only needs a consistent tz-aware-ish base).
NOW = datetime(2026, 8, 21, 15, 30, 0).astimezone()


def iso(y, mo, d, h=23, mi=59, s=59):
    return datetime(y, mo, d, h, mi, s).astimezone().isoformat(timespec="seconds")


CASES_HIT = [
    # (query, expected iso, rule prefix)
    ("2026-03-15에 결정한 거", iso(2026, 3, 15), "abs:iso-date"),
    ("2026/3/5 회의 내용", iso(2026, 3, 5), "abs:iso-date"),
    ("2026년 3월 15일에 뭐라 했지", iso(2026, 3, 15), "abs:ko-ymd"),
    ("2026년 3월 기록", iso(2026, 3, 31), "abs:ko-ym"),
    ("3월 15일의 메모", iso(2026, 3, 15), "abs:ko-md"),
    ("on 2026-03-15 we decided", iso(2026, 3, 15), "abs:iso-date"),
    ("in March 2025", iso(2025, 3, 31), "abs:en-month-year"),
    ("in 2025", iso(2025, 12, 31), "abs:en-year"),
    ("어제 말한 거", iso(2026, 8, 20), "ko:어제"),
    ("그제 논의", iso(2026, 8, 19), "ko:그제"),
    ("3일 전 기록", iso(2026, 8, 18), "ko:N일전"),
    ("2주 전 결정", iso(2026, 8, 7), "ko:N주전"),
    ("1개월 전 상태", iso(2026, 7, 21), "ko:N개월전"),
    ("1년 전 오늘", iso(2025, 8, 21), "ko:N년전"),
    ("지난주에 뭐 했지", iso(2026, 8, 16), "ko:지난주"),   # prev ISO week Sunday
    ("지난달 결산", iso(2026, 7, 31), "ko:지난달"),
    ("작년 이맘때", iso(2025, 12, 31), "ko:작년"),
    ("what happened yesterday", iso(2026, 8, 20), "en:yesterday"),
    ("3 days ago", iso(2026, 8, 18), "en:Ndaysago"),
    ("2 weeks ago", iso(2026, 8, 7), "en:Nweeksago"),
    ("last week summary", iso(2026, 8, 16), "en:lastweek"),
    ("last month report", iso(2026, 7, 31), "en:lastmonth"),
    ("last year baseline", iso(2025, 12, 31), "en:lastyear"),
    # absolute beats relative when both appear
    ("지난주에 2026-03-15 문서를 봤다", iso(2026, 3, 15), "abs:iso-date"),
]

CASES_MISS = [
    # (query, expected reason)
    ("", "empty-query"),
    ("   ", "empty-query"),
    ("지금 상태 알려줘", "no-time-reference"),
    ("오늘 할 일", "no-time-reference"),
    ("이번 주 계획", "no-time-reference"),   # current week ≈ current mode
    ("머지는 항상 스쿼시", "no-time-reference"),
    ("2026-03-15와 2026-04-01 둘 다", "ambiguous-absolute-dates"),
    ("99999일 전", "relative-out-of-range"),
]


class TestEstimateAsOf(unittest.TestCase):
    def test_hit_cases(self):
        for query, expected_iso, rule in CASES_HIT:
            with self.subTest(query=query):
                got = estimate_as_of(query, NOW)
                self.assertIsNotNone(got.get("ts"), f"expected expansion for {query!r}")
                self.assertEqual(got["iso"], expected_iso)
                self.assertEqual(got["rule"], rule)

    def test_miss_cases(self):
        for query, reason in CASES_MISS:
            with self.subTest(query=query):
                got = estimate_as_of(query, NOW)
                self.assertIsNone(got.get("ts"), f"expected no expansion for {query!r}")
                self.assertEqual(got.get("reason"), reason)

    def test_cli_shape(self):
        got = estimate_as_of("어제", NOW)
        self.assertEqual(set(got), {"ts", "iso", "rule"})
        got = estimate_as_of("평범한 질의", NOW)
        self.assertEqual(set(got), {"ts", "reason"})


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestEstimateAsOf)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    failed = len(result.failures) + len(result.errors)
    # subtests count individually for the harness tally contract.
    total = len(CASES_HIT) + len(CASES_MISS) + 2
    print(f"PASS={total - failed} FAIL={failed}")
    raise SystemExit(1 if failed else 0)
