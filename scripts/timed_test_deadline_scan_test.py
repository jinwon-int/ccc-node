#!/usr/bin/env python3
"""Direct unit tests for timed_test_deadline_scan.

Every fixture below is reduced from a real issue in the 2026-09-21 sweep that
motivated the scanner (jinwon-int/ccc-node#1870), so a regression here means
the scanner would once again miss — or once again invent — a finding that
actually happened.

Run standalone: python3 scripts/timed_test_deadline_scan_test.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import timed_test_deadline_scan as scanner

NOW = dt.datetime(2026, 9, 21, 1, 52)


def _comment(body: str, created_at: str, url: str = "https://example/c/1") -> dict:
    return {"body": body, "createdAt": created_at, "url": url}


def _issue(
    number: int = 1527,
    body: str = "",
    comments: list[dict] | None = None,
    repo: str = "jinwon-int/ccc-node",
    title: str = "example",
) -> dict:
    return {
        "repo": repo,
        "number": number,
        "title": title,
        "url": f"https://github.com/{repo}/issues/{number}",
        "body": body,
        "comments": comments or [],
    }


class TimestampTests(unittest.TestCase):
    def test_github_utc_timestamps_become_naive_kst(self) -> None:
        # 2026-09-16T22:13:39Z is 2026-09-17 07:13 KST — the daegyo activation
        # attempt that failed while its deadline was written in KST.
        self.assertEqual(
            scanner.parse_github_timestamp("2026-09-16T22:13:39Z"),
            dt.datetime(2026, 9, 17, 7, 13, 39),
        )


class DateExtractionTests(unittest.TestCase):
    def test_explicit_time_is_preserved(self) -> None:
        found = scanner._dates_in("검증 종료 예정: **2026-09-17 07:32 KST**")
        self.assertEqual(found, [(dt.datetime(2026, 9, 17, 7, 32), True)])

    def test_bare_date_closes_at_end_of_day(self) -> None:
        # Generous on purpose: a test whose window is "2026-09-21" should not
        # be reported overdue at 09:00 that same morning.
        found = scanner._dates_in("관측 종료 2026-09-21")
        self.assertEqual(found, [(dt.datetime(2026, 9, 21, 23, 59), False)])

    def test_impossible_dates_are_skipped(self) -> None:
        self.assertEqual(scanner._dates_in("종료 일시 2026-13-45"), [])

    def test_korean_date_notation_is_read(self) -> None:
        found = scanner._dates_in("종료 예정 2026년 9월 17일 07:32")
        self.assertEqual(found, [(dt.datetime(2026, 9, 17, 7, 32), True)])


class FalsePositiveClassificationTests(unittest.TestCase):
    def test_roadmap_quoting_another_issue_is_demoted(self) -> None:
        # ccc-node#1528 tracks other issues' deadlines in a table.
        paragraph = "| [#1353](x) | 예정 종료는 2026-09-14 21:41 KST, 곽가 확인 태스크 등록 |"
        self.assertEqual(
            scanner.classify_false_positive(paragraph, issue_number=1528),
            "cross-reference",
        )

    def test_issue_quoting_its_own_number_stays_strong(self) -> None:
        paragraph = "#1527 검증 종료 예정: 2026-09-17 07:32 KST"
        self.assertIsNone(scanner.classify_false_positive(paragraph, issue_number=1527))

    def test_completion_report_paragraph_is_demoted(self) -> None:
        # ccc-node#1648 reported its own deployment verification as done.
        paragraph = "| 배포 검증 종료 | **2026-09-12 08:22 KST** — 검증 완료 |"
        self.assertEqual(
            scanner.classify_false_positive(paragraph, issue_number=1648),
            "already-settled",
        )

    def test_completion_heading_demotes_a_neutral_paragraph(self) -> None:
        paragraph = "- 종료 예정: **2026-09-14 09:30 KST**"
        self.assertEqual(
            scanner.classify_false_positive(
                paragraph, issue_number=1597, comment_header="## 테스트 종료 보고 — 실측 완료"
            ),
            "already-settled-comment",
        )

    def test_demoted_hits_are_kept_not_discarded(self) -> None:
        issue = _issue(
            number=161,
            comments=[
                _comment(
                    "테스트 종료일시: 2026-09-12 00:06 UTC — apply 검증 완료 기준",
                    "2026-09-12T00:07:00Z",
                )
            ],
        )
        hits, dropped = scanner.collect_hits(issue)
        self.assertEqual(len(hits), 1, "a demoted hit must survive for the report")
        self.assertEqual(hits[0].confidence, "low")
        self.assertTrue(dropped)


class VerdictDetectionTests(unittest.TestCase):
    """The narrow VERDICT regex is what makes recall 7/7; guard it closely."""

    def test_fail_closed_prose_is_not_a_verdict(self) -> None:
        # a2a-nexus#1597 was wrongly cleared by `fail-closed` matching "closed".
        body = 'the "Implementation scheduling fails closed on the profile" contract'
        self.assertIsNone(scanner.VERDICT.search(body))

    def test_table_header_judgement_column_is_not_a_verdict(self) -> None:
        # ccc-node#1692 has `| 항목 | 값 | 판정 |` deep inside a comment.
        body = "### 결과\n" + "x" * 300 + "\n| 항목 | 값 | 판정 |"
        self.assertIsNone(scanner.VERDICT.search(body[: scanner.VERDICT_HEADER_CHARS]))

    def test_verdict_heading_is_recognised(self) -> None:
        # ccc-node#1353 was judged on 2026-09-18 and must stay unreported.
        body = "## 관측 종료 판정 (2026-09-18) — **종료(정착)** 권고"
        self.assertIsNotNone(scanner.VERDICT.search(body[: scanner.VERDICT_HEADER_CHARS]))


class ExpiredModeTests(unittest.TestCase):
    def test_expired_without_verdict_is_reported(self) -> None:
        issue = _issue(
            comments=[
                _comment(
                    "검증 종료 예정: **2026-09-17 07:32 KST**. 성공 신호는 serving head 일치.",
                    "2026-09-16T22:23:42Z",
                )
            ]
        )
        finding = scanner.judge_issue(issue, NOW, "expired")
        assert finding is not None
        self.assertEqual(finding.kind, "expired-unjudged")
        self.assertEqual(finding.deadline, dt.datetime(2026, 9, 17, 7, 32))
        self.assertEqual(finding.days_overdue, 3)
        self.assertEqual(finding.confidence, "high")

    def test_future_deadline_is_left_alone(self) -> None:
        issue = _issue(
            comments=[_comment("종료 예정: 2026-10-01 09:00 KST", "2026-09-20T00:00:00Z")]
        )
        self.assertIsNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_verdict_after_deadline_clears_the_issue(self) -> None:
        issue = _issue(
            number=1353,
            comments=[
                _comment("| **종료 일시** | **2026-09-14 21:41 KST** |", "2026-09-11T00:00:00Z"),
                _comment("## 관측 종료 판정 (2026-09-18) — 종료(정착) 권고", "2026-09-18T14:40:00Z"),
            ],
        )
        self.assertIsNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_verdict_before_deadline_does_not_count(self) -> None:
        issue = _issue(
            comments=[
                _comment("## 판정 완료", "2026-09-10T00:00:00Z"),
                _comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-16T22:23:42Z"),
            ]
        )
        self.assertIsNotNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_issue_without_any_deadline_is_ignored(self) -> None:
        issue = _issue(body="이 이슈는 시한부 테스트가 아니다.")
        self.assertIsNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_date_without_timed_test_keyword_is_ignored(self) -> None:
        issue = _issue(body="2026-09-01 에 리팩터링을 시작했다.")
        self.assertIsNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_latest_deadline_wins_when_a_window_was_extended(self) -> None:
        issue = _issue(
            comments=[
                _comment("검증 종료 예정: 2026-09-14 09:30 KST", "2026-09-13T00:00:00Z"),
                _comment("검증 종료 예정: 2026-09-19 06:00 KST", "2026-09-14T00:00:00Z"),
            ]
        )
        finding = scanner.judge_issue(issue, NOW, "expired")
        assert finding is not None
        self.assertEqual(finding.deadline, dt.datetime(2026, 9, 19, 6, 0))

    def test_min_confidence_high_filters_demoted_findings(self) -> None:
        issues = [
            _issue(
                number=1527,
                comments=[_comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-16T22:23:42Z")],
            ),
            _issue(
                number=1528,
                comments=[
                    _comment("| [#1353](x) | 예정 종료 2026-09-14 21:41 KST |", "2026-09-13T00:00:00Z")
                ],
            ),
        ]
        self.assertEqual(len(scanner.scan(issues, NOW, "expired", "low")), 2)
        strong = scanner.scan(issues, NOW, "expired", "high")
        self.assertEqual([f.number for f in strong], [1527])


class RelativeModeTests(unittest.TestCase):
    def test_relative_duration_without_absolute_time_is_reported(self) -> None:
        # family-messenger#104: "며칠 관찰 후 완전 삭제" — unschedulable.
        issue = _issue(
            number=104,
            comments=[_comment("**잔여**: 며칠 관찰 후 완전 삭제 별도 승인", "2026-09-17T05:29:33Z")],
        )
        finding = scanner.judge_issue(issue, NOW, "relative")
        assert finding is not None
        self.assertEqual(finding.kind, "relative-duration-only")

    def test_relative_phrase_with_an_absolute_time_is_compliant(self) -> None:
        issue = _issue(
            comments=[
                _comment("며칠 관찰한다 — 종료 2026-09-25 09:00 KST", "2026-09-17T05:29:33Z")
            ]
        )
        self.assertIsNone(scanner.judge_issue(issue, NOW, "relative"))


class CliTests(unittest.TestCase):
    def test_jsonl_input_round_trips_through_main(self) -> None:
        issue = _issue(
            comments=[_comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-16T22:23:42Z")]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "issues.jsonl"
            path.write_text(json.dumps(issue, ensure_ascii=False) + "\n", encoding="utf-8")
            code = scanner.main(
                ["--input", str(path), "--now", "2026-09-21T01:52", "--json"]
            )
        self.assertEqual(code, 0)

    def test_exit_nonzero_flag_signals_findings_for_cron(self) -> None:
        issue = _issue(
            comments=[_comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-16T22:23:42Z")]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "issues.jsonl"
            path.write_text(json.dumps(issue, ensure_ascii=False) + "\n", encoding="utf-8")
            code = scanner.main(
                [
                    "--input",
                    str(path),
                    "--now",
                    "2026-09-21T01:52",
                    "--json",
                    "--exit-nonzero-on-findings",
                ]
            )
        self.assertEqual(code, 1)

    def test_clean_scan_exits_zero_even_with_the_flag(self) -> None:
        issue = _issue(body="시한부 테스트 없음")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "issues.jsonl"
            path.write_text(json.dumps(issue, ensure_ascii=False) + "\n", encoding="utf-8")
            code = scanner.main(
                [
                    "--input",
                    str(path),
                    "--now",
                    "2026-09-21T01:52",
                    "--exit-nonzero-on-findings",
                ]
            )
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
