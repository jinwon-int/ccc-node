#!/usr/bin/env python3
"""Direct unit tests for timed_test_deadline_scan.

Every fixture below is reduced from a real issue in the 2026-09-21 sweep that
motivated the scanner (jinwon-int/ccc-node#1870), so a regression here means
the scanner would once again miss — or once again invent — a finding that
actually happened.

Run standalone: python3 scripts/timed_test_deadline_scan_test.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_marked_utc_is_converted_to_kst(self) -> None:
        # a2a-nexus#2256: "07:36:11 UTC (16:36 KST)" was read as 07:36 KST.
        self.assertEqual(
            scanner._dates_in("테스트 종료: 2026-09-28 07:36:11 UTC (16:36 KST)"),
            [(dt.datetime(2026, 9, 28, 16, 36), True)],
        )
        self.assertEqual(
            scanner._dates_in("deadline 2026-09-28T07:36:11Z"),
            [(dt.datetime(2026, 9, 28, 16, 36), True)],
        )

    def test_utc_and_kst_renderings_of_one_instant_agree(self) -> None:
        self.assertEqual(
            {stamp for stamp, _ in scanner._dates_in(
                "종료일시: 2026-09-15 23:44 UTC (2026-09-16 08:44 KST)"
            )},
            {dt.datetime(2026, 9, 16, 8, 44)},
        )

    def test_utc_offset_annotation_is_not_a_utc_clock(self) -> None:
        # a2a-nexus#1597 wrote "09:30 KST (Asia/Seoul, UTC+09:00)"; a bare
        # "UTC+09:00" right after the time must not shift it either.
        self.assertEqual(
            scanner._dates_in("종료 예정: 2026-09-14 09:30 UTC+09:00"),
            [(dt.datetime(2026, 9, 14, 9, 30), True)],
        )

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

    def test_time_already_past_when_posted_is_a_record_not_a_booking(self) -> None:
        # a2a-nexus#2256: the result report posted at 16:44 KST stated
        # "테스트 종료: 07:36:11 UTC (16:36 KST)" under a neutral heading and
        # was a daily high-confidence finding.
        issue = _issue(
            number=2256,
            repo="jinwon-int/a2a-nexus",
            comments=[
                _comment(
                    "## 후속 PR #2286 착지 — 검증 종료 · worker · 2026-09-28\n\n"
                    "- **착지**: #2286 → main `7a105b66`\n"
                    "- **테스트 종료**: 2026-09-28 07:36:11 UTC (16:36 KST). 전체 게이트 실행.",
                    "2026-09-28T07:44:22Z",
                )
            ],
        )
        hits, _ = scanner.collect_hits(issue)
        record = [h for h in hits if h.deadline == dt.datetime(2026, 9, 28, 16, 36)]
        self.assertEqual(len(record), 1)
        self.assertEqual(record[0].weak_reason, "past-at-posting")
        now = dt.datetime(2026, 9, 29, 9, 20)
        self.assertEqual(scanner.scan([issue], now, "expired", "high"), [])

    def test_future_booking_is_not_demoted_as_past(self) -> None:
        issue = _issue(
            comments=[_comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-16T22:23:42Z")]
        )
        hits, _ = scanner.collect_hits(issue)
        self.assertEqual([h.weak_reason for h in hits], [None])

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

    def test_completion_report_heading_is_a_verdict(self) -> None:
        # ccc-node#1648 was judged with this heading on 2026-09-12 08:22 KST
        # and was still reported at high confidence on the first production
        # run (#1873): none of the original tokens appear in it.
        body = "## ✅ 배포 검증 완료 — 실제 종료 일시·결과 갱신 (soonwook 세션)"
        self.assertIsNotNone(scanner.VERDICT.search(body[: scanner.VERDICT_HEADER_CHARS]))

    def test_bare_completion_progress_heading_is_not_a_verdict(self) -> None:
        # Recall guard for the #1873 widening: an ordinary post-deadline
        # progress update must not clear the issue. Bare "완료"/"✅" once hid
        # three real findings and stay excluded.
        body = "## ✅ yukson piri skills 연결 완료 — 8/8 전 노드"
        self.assertIsNone(scanner.VERDICT.search(body[: scanner.VERDICT_HEADER_CHARS]))


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

    def test_early_verdict_after_the_booking_clears_the_issue(self) -> None:
        # a2a-nexus#1597: booked for 09-26 12:00 KST, run early the evening
        # before and judged twice before the window closed.
        issue = _issue(
            number=1597,
            repo="jinwon-int/a2a-nexus",
            comments=[
                _comment(
                    "## 재카나리 일정 확정 + 대상 정정 — **종료 일시 2026-09-26 12:00 KST**",
                    "2026-09-25T11:30:31Z",
                ),
                _comment(
                    "## 재카나리 착수 — 일정 앞당김\n\n"
                    "- 종료 예정: 늦어도 기존 종료 일시 **2026-09-26 12:00 KST**를 넘기지 않습니다.",
                    "2026-09-25T12:53:15Z",
                ),
                _comment("## 재카나리 판정 — **불합격 (runtime mismatch)**", "2026-09-25T12:56:18Z"),
                _comment("## 재카나리 r2 — **합격** · 대상 노드 `canary_passed` 승격", "2026-09-25T13:50:36Z"),
            ],
        )
        now = dt.datetime(2026, 9, 29, 9, 20)
        self.assertIsNone(scanner.judge_issue(issue, now, "expired"))

    def test_early_window_opens_at_the_last_restatement_of_the_booking(self) -> None:
        # A verdict between the first booking and a later re-affirmation of
        # the same deadline belongs to an earlier round, not this one.
        issue = _issue(
            comments=[
                _comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-10T00:00:00Z"),
                _comment("## 1차 판정 — 불합격", "2026-09-11T00:00:00Z"),
                _comment("재시험. 종료 예정은 그대로 2026-09-17 07:32 KST", "2026-09-12T00:00:00Z"),
            ]
        )
        self.assertIsNotNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_early_verdict_restating_the_deadline_does_not_move_the_window(self) -> None:
        # a2a-nexus#2315 (#2149): the verdict comment's first paragraph quoted
        # the booked end time it beat. That mention was demoted
        # (already-settled) but still counted as a booking, so booked_at
        # landed on the verdict itself, `posted > booked_at` failed and the
        # issue came back as expired-unjudged at high confidence.
        issue = _issue(
            number=2315,
            repo="jinwon-int/a2a-nexus",
            comments=[
                _comment(
                    "## 관측 예약\n\n| 항목 | 값 |\n|---|---|\n| 종료 일시 | **2026-10-08 09:00 KST** |",
                    "2026-10-05T02:10:00Z",
                ),
                _comment(
                    "## 관측 종료 판정 — 합격(PASS) · 조기 종료\n\n"
                    "예약 종료 시각(2026-10-08 09:00 KST)보다 앞당겨 끝냈다 — 필요한 관측 7건을 모두 확보했다.",
                    "2026-10-05T09:40:00Z",
                ),
            ],
        )
        now = dt.datetime(2026, 10, 8, 10, 0)
        self.assertIsNone(scanner.judge_issue(issue, now, "expired"))
        # Dropping the restatement must not hide a genuinely unjudged booking.
        unjudged = _issue(
            number=2315,
            repo="jinwon-int/a2a-nexus",
            comments=[issue["comments"][0], _comment("진행 중 — 관측 3/7", "2026-10-06T00:00:00Z")],
        )
        self.assertIsNotNone(scanner.judge_issue(unjudged, now, "expired"))

    def test_early_progress_headings_are_not_verdicts(self) -> None:
        # ccc-node#1528 was cleared by "판정표" when the 200-char window was
        # read before the deadline; interim and pending calls are not results.
        for heading in (
            "## 오너 결정 반영 (2026-09-13 09:5x KST) — TM-3353 판정표 후속",
            "## 중간 판정 — 관측 계속",
            "## 판정 기준 확정",
            "## 관측 중 — 결과는 아래 표, 판정은 종료 후",
            "## 기한 연장",
        ):
            with self.subTest(heading=heading):
                issue = _issue(
                    comments=[
                        _comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-10T00:00:00Z"),
                        _comment(heading, "2026-09-12T00:00:00Z"),
                    ]
                )
                self.assertIsNotNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_early_verdict_must_lead_the_comment(self) -> None:
        issue = _issue(
            comments=[
                _comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-10T00:00:00Z"),
                _comment("## 진행 상황\n\n지난 라운드는 합격이었다.", "2026-09-12T00:00:00Z"),
            ]
        )
        self.assertIsNotNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_issue_without_any_deadline_is_ignored(self) -> None:
        issue = _issue(body="이 이슈는 시한부 테스트가 아니다.")
        self.assertIsNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_date_without_timed_test_keyword_is_ignored(self) -> None:
        issue = _issue(body="2026-09-01 에 리팩터링을 시작했다.")
        self.assertIsNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_scanner_file_name_is_not_a_deadline_keyword(self) -> None:
        # ccc-node#1873 quoted the scan command next to its run time and the
        # "deadline" inside timed_test_deadline_scan.py matched the English
        # keyword, making the precision issue itself a high-confidence finding.
        issue = _issue(
            number=1873,
            body=(
                "실행: 2026-09-21 11:06 KST, `python3 scripts/timed_test_deadline_scan.py "
                "--repos-file ~/.claude/timed-test-deadline-scan.repos --mode expired` (22초)"
            ),
        )
        self.assertIsNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_quoted_booking_in_inline_code_is_not_a_deadline(self) -> None:
        # ccc-node#1873 quoted #1648's booking row inside backticks.
        issue = _issue(
            number=1873,
            body="- 예약 코멘트 표: `| 배포 검증 종료 | **2026-09-12 06:00 KST** |`",
        )
        self.assertIsNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_timestamp_alone_in_inline_code_still_counts(self) -> None:
        issue = _issue(body="검증 종료 예정: `2026-09-17 07:32 KST`")
        finding = scanner.judge_issue(issue, NOW, "expired")
        assert finding is not None
        self.assertEqual(finding.deadline, dt.datetime(2026, 9, 17, 7, 32))
        self.assertEqual(finding.confidence, "high")

    def test_english_deadline_word_still_counts(self) -> None:
        issue = _issue(body="Deadline: 2026-09-17 07:32 KST, judged by gwakga.")
        finding = scanner.judge_issue(issue, NOW, "expired")
        assert finding is not None
        self.assertEqual(finding.confidence, "high")

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

    def test_completion_report_after_deadline_clears_the_issue(self) -> None:
        # ccc-node#1648 end to end: booking table, then the completion report
        # two hours after the window closed. Before #1873 this was a daily
        # high-confidence finding.
        issue = _issue(
            number=1648,
            comments=[
                _comment("| 배포 검증 종료 | **2026-09-12 06:00 KST** |", "2026-09-11T00:00:00Z"),
                _comment(
                    "## ✅ 배포 검증 완료 — 실제 종료 일시·결과 갱신 (soonwook 세션)\n\n"
                    "| 배포 검증 종료 | 2026-09-12 06:00 KST | **2026-09-12 08:22 KST** |",
                    "2026-09-11T23:22:58Z",
                ),
            ],
        )
        self.assertIsNone(scanner.judge_issue(issue, NOW, "expired"))

    def test_bare_date_rule_citation_is_demoted(self) -> None:
        # ccc-node#1870 (the scanner's own tracking issue) cites the owner
        # rule by date and got itself reported at high confidence with a
        # 23:59 deadline nobody wrote. Still surfaced, but as low confidence.
        issue = _issue(
            number=1870,
            body="오너 규칙(2026-09-11)은 미래에 종료되는 테스트에 **종료 일시를 KST 절대시각으로** 적도록 요구한다.",
        )
        finding = scanner.judge_issue(issue, NOW, "expired")
        assert finding is not None
        self.assertEqual(finding.confidence, "low")
        self.assertEqual(finding.weak_reason, "date-only")
        self.assertEqual(finding.deadline, dt.datetime(2026, 9, 11, 23, 59))
        self.assertEqual(scanner.scan([issue], NOW, "expired", "high"), [])

    def test_explicit_datetime_outranks_a_later_bare_date(self) -> None:
        # A real booking with a time must not be displaced by a later bare
        # date citation in the same issue.
        issue = _issue(
            comments=[
                _comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-16T00:00:00Z"),
                _comment("참고: 종료 일시 규칙은 2026-09-18 개정본을 따른다.", "2026-09-16T01:00:00Z"),
            ]
        )
        finding = scanner.judge_issue(issue, NOW, "expired")
        assert finding is not None
        self.assertEqual(finding.deadline, dt.datetime(2026, 9, 17, 7, 32))
        self.assertEqual(finding.confidence, "high")


NOW_1006 = dt.datetime(2026, 10, 6, 9, 20)

# ccc-node#2109, reduced: a claim with a PR ETA, then the real booking in the
# future-ending-test-issue-tracking template.
CLAIM_2109 = (
    "<!-- issue-claim:v1 node=node-a session=ste100-provider-pilot-20261005 state=takeover prev=468c3987 -->\n"
    "착수: node-a (ste100-provider-pilot-20261005) — 오너 요청으로 A 보고 문체 규칙을 카나리 1노드에 "
    "시범 적용합니다. 예상 PR: 2026-10-05 09:30 KST. 실제 활성화 후 7일 창의 시작·종료 시각과 "
    "지표를 별도 기록합니다."
)
BOOKING_2109 = (
    "<!-- future-ending-test-issue-tracking: open -->\n"
    "**Test/task**: 카나리 노드 STE 보고 문체 7일 전후 비교\n"
    "**Tracking model**: clock-based\n"
    "**Start time**: 2026-10-05 07:44:53 KST\n"
    "**Expected end time**: 2026-10-12 07:44:53 KST\n"
    "**Next action on completion**: 관찰기가 최종 수치를 이 이슈에 기록."
)

# ccc-node#2083, reduced: a result report whose heading states the test end
# three minutes after posting and whose verdict sits at the end.
REPORT_2083 = (
    "**node-b DNS 수정 재부팅 지속성 — 정적 점검** (node-b, 테스트 종료 2026-10-05 10:40 UTC / 19:40 KST)\n\n"
    "재부팅은 하지 않았습니다. 부팅 때 다시 읽히는 설정만 확인했습니다.\n\n"
    "- `systemd-resolved` enabled\n- 현재 `getent hosts api.telegram.org` 정상\n\n"
    "판정: 통과(정적 근거). 실제 재부팅을 거쳐 본 것은 아님."
)


class FalsePositive2150Tests(unittest.TestCase):
    """ccc-node#2150: the two high-confidence false positives of 2026-10-06."""

    def test_expected_end_time_template_is_a_booking(self) -> None:
        issue = _issue(comments=[_comment(BOOKING_2109, "2026-10-04T22:45:43Z")])
        self.assertIsNone(scanner.judge_issue(issue, NOW_1006, "expired"))
        finding = scanner.judge_issue(issue, dt.datetime(2026, 10, 13, 9, 20), "expired")
        assert finding is not None
        self.assertEqual(finding.deadline, dt.datetime(2026, 10, 12, 7, 44))
        self.assertEqual(finding.confidence, "high")

    def test_claim_eta_alone_is_demoted(self) -> None:
        issue = _issue(number=2109, comments=[_comment(CLAIM_2109, "2026-10-04T22:30:50Z")])
        finding = scanner.judge_issue(issue, NOW_1006, "expired")
        assert finding is not None
        self.assertEqual(finding.confidence, "low")
        self.assertEqual(finding.weak_reason, "claim-eta")

    def test_pr_eta_outside_a_claim_comment_is_demoted(self) -> None:
        body = "진행 메모 — 예상 PR: 2026-10-05 09:30 KST, 관측 종료 시각은 활성화 후 따로 적음."
        issue = _issue(comments=[_comment(body, "2026-10-04T22:30:50Z")])
        finding = scanner.judge_issue(issue, NOW_1006, "expired")
        assert finding is not None
        self.assertEqual(finding.weak_reason, "claim-eta")

    def test_issue_2109_real_booking_wins_over_the_claim(self) -> None:
        issue = _issue(
            number=2109,
            comments=[
                _comment(CLAIM_2109, "2026-10-04T22:30:50Z"),
                _comment(BOOKING_2109, "2026-10-04T22:45:43Z"),
            ],
        )
        self.assertIsNone(scanner.judge_issue(issue, NOW_1006, "expired"))

    def test_issue_2083_report_with_its_own_verdict_is_judged(self) -> None:
        issue = _issue(number=2083, comments=[_comment(REPORT_2083, "2026-10-05T10:36:35Z")])
        self.assertIsNone(scanner.judge_issue(issue, NOW_1006, "expired"))

    def test_same_report_without_a_verdict_line_is_still_reported(self) -> None:
        body = REPORT_2083.replace("판정: 통과(정적 근거). ", "")
        issue = _issue(number=2083, comments=[_comment(body, "2026-10-05T10:36:35Z")])
        finding = scanner.judge_issue(issue, NOW_1006, "expired")
        assert finding is not None
        self.assertEqual(finding.confidence, "high")

    def test_verdict_line_in_a_later_body_clears_after_the_deadline(self) -> None:
        issue = _issue(
            comments=[
                _comment("| 종료 일시 | **2026-10-05 12:00 KST** |", "2026-09-29T04:13:00Z"),
                _comment("## 10-05 실행 결과\n\n- deleted 18 file(s)\n\n**판정**: 합격", "2026-10-05T23:51:00Z"),
            ]
        )
        self.assertIsNone(scanner.judge_issue(issue, NOW_1006, "expired"))

    def test_verdict_labels_that_book_or_assign_are_not_verdicts(self) -> None:
        for line in ("판정: 합격 기준은 deleted N≥1", "판정 주체: 담당 노드", "판정: 오너가 종료 후 결정"):
            with self.subTest(line=line):
                issue = _issue(
                    comments=[_comment(f"종료 일시: 2026-10-05 12:00 KST\n{line}", "2026-09-29T04:13:00Z")]
                )
                self.assertIsNotNone(scanner.judge_issue(issue, NOW_1006, "expired"))

    def test_verdict_line_before_the_booking_does_not_count(self) -> None:
        issue = _issue(
            comments=[
                _comment("판정: 통과", "2026-09-28T00:00:00Z"),
                _comment("종료 일시: 2026-10-05 12:00 KST", "2026-09-29T04:13:00Z"),
            ]
        )
        self.assertIsNotNone(scanner.judge_issue(issue, NOW_1006, "expired"))


# ccc-node#2130, reduced (node names generalized): the English tracking
# template booked a 48h window and closed it with "**Verdict**: FAIL" 23 minutes
# after the deadline.
BOOKING_2130 = (
    "<!-- future-ending-test-issue-tracking: open -->\n"
    "**Test/task**: Nunchi PR2132 natural-run acceptance\n"
    "**Tracking model**: clock-based\n"
    "**Start time**: 2026-10-04T19:02:15.323514+09:00 (KST)\n"
    "**Expected end time**: 2026-10-06T19:02:15.323514+09:00 (KST)\n"
    "**Next action on completion**: publish PASS/FAIL/INCOMPLETE."
)
CLOSE_2130 = (
    "<!-- future-ending-test-issue-tracking: closed -->\n"
    "**Test/task**: Nunchi PR2132 natural-run acceptance\n"
    "**Actual end time**: 2026-10-06T19:25:16.168598+09:00 (KST)\n"
    "**Verdict**: FAIL\n"
    "**Summary**: The bounded 48h read-only window ended."
)


class EnglishVerdictTests(unittest.TestCase):
    """Follow-up to #2150: the English closing template is a verdict."""

    def test_issue_2130_english_verdict_after_deadline_is_judged(self) -> None:
        issue = _issue(
            number=2130,
            comments=[
                _comment(BOOKING_2130, "2026-10-04T10:02:29Z"),
                _comment(CLOSE_2130, "2026-10-06T10:25:17Z"),
            ],
        )
        self.assertIsNone(scanner.judge_issue(issue, dt.datetime(2026, 10, 7, 9, 20), "expired"))

    def test_issue_2130_before_the_verdict_is_still_reported(self) -> None:
        issue = _issue(number=2130, comments=[_comment(BOOKING_2130, "2026-10-04T10:02:29Z")])
        finding = scanner.judge_issue(issue, dt.datetime(2026, 10, 6, 19, 20), "expired")
        assert finding is not None
        self.assertEqual(finding.deadline, dt.datetime(2026, 10, 6, 19, 2))
        self.assertEqual(finding.confidence, "high")

    def test_incomplete_and_pass_close_the_window(self) -> None:
        for word in ("INCOMPLETE", "PASS", "pass"):
            with self.subTest(word=word):
                close = CLOSE_2130.replace("**Verdict**: FAIL", f"**Verdict**: {word}")
                issue = _issue(
                    comments=[
                        _comment(BOOKING_2130, "2026-10-04T10:02:29Z"),
                        _comment(close, "2026-10-06T10:25:17Z"),
                    ]
                )
                self.assertIsNone(scanner.judge_issue(issue, dt.datetime(2026, 10, 7, 9, 20), "expired"))

    def test_verdict_before_the_booking_does_not_count(self) -> None:
        early = CLOSE_2130.replace("Nunchi PR2132 natural-run acceptance", "activation readiness").replace(
            "2026-10-06T19:25:16.168598", "2026-10-04T17:58:31.415481"
        )
        issue = _issue(
            comments=[
                _comment(early.replace("FAIL", "INCOMPLETE"), "2026-10-04T08:58:32Z"),
                _comment(BOOKING_2130, "2026-10-04T10:02:29Z"),
            ]
        )
        self.assertIsNotNone(scanner.judge_issue(issue, dt.datetime(2026, 10, 7, 9, 20), "expired"))

    def test_verdict_labels_that_book_are_not_verdicts(self) -> None:
        for line in ("**Verdict**: PASS criteria are listed below", "Verdict owner: operator", "**Verdict**: TBD"):
            with self.subTest(line=line):
                issue = _issue(comments=[_comment(BOOKING_2130 + "\n" + line, "2026-10-04T10:02:29Z")])
                self.assertIsNotNone(scanner.judge_issue(issue, dt.datetime(2026, 10, 7, 9, 20), "expired"))


# a2a-nexus#1504 Phase 7-B, reduced (#2252): the booking names only a start
# ("실행 예약 … OnCalendar=2026-10-09 22:00:00 KST") and offsets ("T+1/6/12/24h"),
# never an end. Its heading says "합격" (the pre-checks passed) and an earlier
# paragraph carries an unrelated OnCalendar (the reboot-hold expiry, 10-11).
BOOKING_1504 = (
    "## Phase 7-B 사전 조회 합격 + 실행 예약 (2026-10-08 18:30 KST · node-a)\n\n"
    "**사전 점검(읽기 전용) — 모두 통과**\n"
    "| B1 / 게이트 4 | shadow snapshot **pass**: 44.2h 창, unexplained 0/0 |\n\n"
    "**재부팅 보류 재설정**: 해제는 transient 타이머 `a2a-p7b-reboot-hold-expire`"
    "(`OnCalendar=2026-10-11 22:00 KST`)가 자동으로 합니다.\n\n"
    "**실행 예약**: `a2a-p7b-execute.timer`가 `OnCalendar=2026-10-09 22:00:00 KST`에 "
    "`run-b.sh execute`를 실행합니다.\n"
    "- **승인 창 고정**: 2026-10-09 21:59~23:00 KST 밖에서는 실행을 거부합니다.\n"
    "- **B5**: shadow 삭제 + `RATE=on` 1줄 + 930초 드레인\n"
    "- **B8**: `OnCalendar` 관측 타이머 T+1/6/12/24h + T+5m 즉시 관측\n"
    "- 시작·중단·롤백·완료는 텔레그램 spool로 알립니다."
)
BOOKING_1504_AT = "2026-10-08T09:31:05Z"
# The verdict that was finally written by hand on 2026-10-11.
VERDICT_1504 = (
    "<!-- future-ending-test-issue-tracking: closed -->\n"
    "## Phase 7-B 실행 결과 — **INCOMPLETE (B1에서 중단, 변경 없음)** · 보고 누락 정정\n\n"
    "**Test/task**: T1 브로커 Phase 7-B — RATE=on 전환 + T+24h 관측\n"
    "**Actual end time**: 2026-10-09 22:00:04 KST (예약 22:00:00 → 4초 만에 중단)\n"
    "**Verdict**: INCOMPLETE\n"
)
NOW_1011 = dt.datetime(2026, 10, 11, 9, 1)


class AnchorOffset2252Tests(unittest.TestCase):
    """ccc-node#2252: an absolute start plus relative offsets is a deadline."""

    def test_issue_1504_booking_infers_anchor_plus_largest_offset(self) -> None:
        issue = _issue(
            number=1504,
            repo="jinwon-int/a2a-nexus",
            comments=[_comment(BOOKING_1504, BOOKING_1504_AT)],
        )
        finding = scanner.judge_issue(issue, NOW_1011, "expired")
        assert finding is not None
        self.assertEqual(finding.kind, "expired-unjudged")
        # Nearest preceding anchor (10-09 22:00), not the reboot-hold one (10-11).
        self.assertEqual(finding.deadline, dt.datetime(2026, 10, 10, 22, 0))
        self.assertEqual(finding.confidence, "low")
        self.assertEqual(finding.weak_reason, "anchor-offset")
        self.assertEqual(len(scanner.scan([issue], NOW_1011, "expired", "low")), 1)
        self.assertEqual(scanner.scan([issue], NOW_1011, "expired", "high"), [])

    def test_issue_1504_is_left_alone_before_the_inferred_deadline(self) -> None:
        issue = _issue(number=1504, comments=[_comment(BOOKING_1504, BOOKING_1504_AT)])
        self.assertIsNone(scanner.judge_issue(issue, dt.datetime(2026, 10, 10, 21, 59), "expired"))

    def test_issue_1504_verdict_after_the_inferred_deadline_clears_it(self) -> None:
        issue = _issue(
            number=1504,
            comments=[
                _comment(BOOKING_1504, BOOKING_1504_AT),
                _comment(VERDICT_1504, "2026-10-11T00:09:15Z"),
            ],
        )
        self.assertIsNone(scanner.judge_issue(issue, dt.datetime(2026, 10, 11, 9, 20), "expired"))

    def test_judged_explicit_window_does_not_hide_a_later_inferred_one(self) -> None:
        # The live a2a-nexus#1504 thread also booked "보류 종료 일시:
        # 2026-10-10 18:00 KST" (high) and carried Phase 7 verdicts after it.
        # Preferring only that strong deadline cleared the issue and hid the
        # 7-B window closing at 22:00.
        hold = "**보류 종료 일시: 2026-10-10 18:00 KST** — 관측창이 먼저 끝나면 그때 해제합니다."
        comments = [
            _comment(hold, "2026-09-26T02:17:39Z"),
            _comment("## Phase 7-A 관측 판정 — 합격", "2026-10-01T00:00:00Z"),
            _comment(BOOKING_1504, BOOKING_1504_AT),
        ]
        issue = _issue(number=1504, comments=comments)
        finding = scanner.judge_issue(issue, NOW_1011, "expired")
        assert finding is not None
        self.assertEqual(finding.deadline, dt.datetime(2026, 10, 10, 22, 0))
        self.assertEqual(finding.weak_reason, "anchor-offset")
        # An unjudged explicit window still wins and keeps its high confidence.
        # (The 7-B heading's "사전 조회 합격" would read as an early verdict.)
        plain = _comment(BOOKING_1504.replace("사전 조회 합격 + ", ""), BOOKING_1504_AT)
        issue = _issue(number=1504, comments=[comments[0], plain])
        finding = scanner.judge_issue(issue, NOW_1011, "expired")
        assert finding is not None
        self.assertEqual(finding.deadline, dt.datetime(2026, 10, 10, 18, 0))
        self.assertEqual(finding.confidence, "high")

    def test_offset_forms_take_the_largest(self) -> None:
        anchor = dt.datetime(2026, 10, 9, 22, 0)
        for line, offset in (
            ("관측 T+24h", dt.timedelta(hours=24)),
            ("관측 T+1h/6h/48h", dt.timedelta(hours=48)),
            ("48시간 후 판정", dt.timedelta(hours=48)),
            ("12시간 뒤 확인, +36h 재확인", dt.timedelta(hours=36)),
            ("72h 관측", dt.timedelta(hours=72)),
            ("3일 관측", dt.timedelta(days=3)),
            ("실행 후 6시간 관측", dt.timedelta(hours=6)),
        ):
            with self.subTest(line=line):
                body = f"실행 예약: 2026-10-09 22:00 KST\n- {line}"
                hits, _ = scanner.collect_hits(_issue(comments=[_comment(body, BOOKING_1504_AT)]))
                self.assertEqual([hit.deadline for hit in hits], [anchor + offset])

    def test_anchor_and_offset_in_one_paragraph(self) -> None:
        body = "Canary starts at 2026-10-09 22:00 KST and is read again at T+24h."
        hits, _ = scanner.collect_hits(_issue(comments=[_comment(body, BOOKING_1504_AT)]))
        self.assertEqual([hit.deadline for hit in hits], [dt.datetime(2026, 10, 10, 22, 0)])

    def test_korean_date_followed_by_hu_is_not_an_offset(self) -> None:
        body = "실행 예약: 2026년 10월 9일 22:00 KST. 2026년 10월 9일 후속 점검은 별도."
        hits, _ = scanner.collect_hits(_issue(comments=[_comment(body, BOOKING_1504_AT)]))
        self.assertEqual(hits, [])

    def test_anchor_without_offset_or_offset_without_anchor_is_ignored(self) -> None:
        for body in ("실행 예약: OnCalendar=2026-10-09 22:00 KST", "관측 타이머 T+1/6/12/24h"):
            with self.subTest(body=body):
                hits, _ = scanner.collect_hits(_issue(comments=[_comment(body, BOOKING_1504_AT)]))
                self.assertEqual(hits, [])

    def test_explicit_end_in_the_same_comment_is_not_double_detected(self) -> None:
        # ② of #2252: the explicit end is the booking; T+48h beside it must
        # not add a second, later deadline that outlives the verdict.
        body = (
            "실행 예약: OnCalendar=2026-10-09 22:00 KST\n"
            "- 관측 타이머 T+1/6/12/24h, 필요하면 +48h 추가 관측\n"
            "| 관측 종료 일시 | **2026-10-10 22:00 KST** |"
        )
        issue = _issue(
            comments=[
                _comment(body, BOOKING_1504_AT),
                _comment("## 관측 종료 판정 — 합격", "2026-10-10T14:30:00Z"),
            ]
        )
        hits, dropped = scanner.collect_hits(issue)
        self.assertEqual([hit.deadline for hit in hits], [dt.datetime(2026, 10, 10, 22, 0)])
        self.assertEqual(hits[0].confidence, "high")
        self.assertFalse(any(item.startswith("anchor-offset") for item in dropped))
        self.assertIsNone(scanner.judge_issue(issue, NOW_1011, "expired"))

    def test_early_verdict_restating_the_offsets_does_not_move_the_window(self) -> None:
        # ③ of #2252, the #2149 trap: an early verdict that recounts the
        # plan's anchor and offsets must not become the booking itself.
        for start in ("22:00:00", "22:05"):
            with self.subTest(start=start):
                verdict = (
                    "## Phase 7-B 판정 — 불합격 (B1 실패, 변경 없음)\n\n"
                    f"실행 예약대로 OnCalendar=2026-10-09 {start} KST에 시작했으나 B1에서 중단.\n"
                    "- B8 관측 타이머 T+1/6/12/24h는 시작되지 않음."
                )
                issue = _issue(
                    number=1504,
                    comments=[
                        _comment(BOOKING_1504, BOOKING_1504_AT),
                        _comment(verdict, "2026-10-09T23:30:00Z"),
                    ],
                )
                self.assertIsNone(scanner.judge_issue(issue, NOW_1011, "expired"))

    def test_progress_note_restating_the_offsets_keeps_the_finding(self) -> None:
        # Dropping restatements as bookings must not hide a real miss.
        note = "진행 메모: 실행 예약 OnCalendar=2026-10-09 22:00 KST 확인, T+24h 관측 대기 중."
        issue = _issue(
            number=1504,
            comments=[
                _comment(BOOKING_1504, BOOKING_1504_AT),
                _comment(note, "2026-10-09T23:30:00Z"),
            ],
        )
        finding = scanner.judge_issue(issue, NOW_1011, "expired")
        assert finding is not None
        self.assertEqual(finding.deadline, dt.datetime(2026, 10, 10, 22, 0))


class RelativeModeTests(unittest.TestCase):
    def test_hour_offsets_without_absolute_time_are_reported(self) -> None:
        # #2252: a2a-nexus#1504's B8 line has offsets and no date.
        for line in (
            "- **B8**: `OnCalendar` 관측 타이머 T+1/6/12/24h + T+5m 즉시 관측",
            "배포 후 24시간 관측한 뒤 판정",
            "48시간 후 결과 확인",
            "24h·48h 관측 후 정리",
            "관측은 T+72h까지",
        ):
            with self.subTest(line=line):
                issue = _issue(comments=[_comment(line, BOOKING_1504_AT)])
                finding = scanner.judge_issue(issue, NOW, "relative")
                assert finding is not None
                self.assertEqual(finding.kind, "relative-duration-only")

    def test_hour_offsets_next_to_an_absolute_time_are_compliant(self) -> None:
        issue = _issue(comments=[_comment("2026-10-09 22:00 KST 시작, T+24h 관측", BOOKING_1504_AT)])
        self.assertIsNone(scanner.judge_issue(issue, NOW, "relative"))

    def test_non_offset_hours_are_not_relative_durations(self) -> None:
        for line in ("shadow snapshot 44.2h 창", "UTC+9h 기준 표기", "평균 처리 3시간"):
            with self.subTest(line=line):
                issue = _issue(comments=[_comment(line, BOOKING_1504_AT)])
                self.assertIsNone(scanner.judge_issue(issue, NOW, "relative"))

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


class ReposFileTests(unittest.TestCase):
    """An unconfigured scheduled scan must never look like a clean scan.

    This tool exists because a silent failure passed for success for nine
    days. A cron job whose repo list vanished reporting "0건" would be that
    same bug, so every unconfigured shape gets its own exit code.
    """

    def _write(self, directory: str, text: str) -> Path:
        path = Path(directory) / "scan.repos"
        path.write_text(text, encoding="utf-8")
        return path

    def test_repos_are_parsed_with_comments_and_blanks_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                directory,
                "# fleet repos\n\njinwon-int/ccc-node\n"
                "jinwon-int/a2a-nexus  # trailing note\n\n",
            )
            self.assertEqual(
                scanner.load_repos_file(path),
                ["jinwon-int/ccc-node", "jinwon-int/a2a-nexus"],
            )

    def test_missing_file_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(scanner.NotConfigured):
                scanner.load_repos_file(Path(directory) / "absent.repos")

    def test_empty_file_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "")
            with self.assertRaises(scanner.NotConfigured):
                scanner.load_repos_file(path)

    def test_comment_only_file_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "# TODO: list repos here\n\n")
            with self.assertRaises(scanner.NotConfigured):
                scanner.load_repos_file(path)

    def test_malformed_entry_raises_instead_of_being_skipped(self) -> None:
        # Skipping would scan a shorter list than the operator wrote — the
        # exact class of quiet shortfall this scanner hunts.
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "jinwon-int/ccc-node\nnot-a-repo\n")
            with self.assertRaises(ValueError):
                scanner.load_repos_file(path)

    def test_cli_exits_3_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "# nothing yet\n")
            code = scanner.main(["--repos-file", str(path), "--now", "2026-09-21T01:52"])
        self.assertEqual(code, scanner.EXIT_NOT_CONFIGURED)

    def test_cli_exits_2_on_unusable_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "garbage\n")
            code = scanner.main(["--repos-file", str(path), "--now", "2026-09-21T01:52"])
        self.assertEqual(code, 2)

    def test_not_configured_code_is_distinct_from_findings_code(self) -> None:
        self.assertNotIn(scanner.EXIT_NOT_CONFIGURED, (0, 1))


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


class RepoFetchIsolationTests(unittest.TestCase):
    """#2324 (1): one unreadable repo must not silence every other repo.

    Reproduced 2026-10-15: a 45-repo list that included a repository with
    Issues disabled raised RuntimeError out of fetch_via_gh on that repo and
    the whole run printed a traceback — zero findings, no JSON, for all 45.
    """

    _EXPIRED = _issue(
        comments=[_comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-16T22:23:42Z")]
    )

    def _run(self, side_effect, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(scanner, "fetch_via_gh", side_effect=side_effect), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scanner.main(
                ["--repo", "o/disabled", "--repo", "o/live", "--now", "2026-09-21T01:52", *extra]
            )
        return code, out.getvalue(), err.getvalue()

    def _one_fails(self, exc: BaseException):
        live = dict(self._EXPIRED, repo="o/live")

        def fetch(repo: str, limit: int):
            if repo == "o/disabled":
                raise exc
            return [live]

        return fetch

    def test_one_failing_repo_does_not_abort_the_rest(self) -> None:
        exc = RuntimeError("gh issue list failed for o/disabled: the repository has disabled issues")
        code, out, err = self._run(self._one_fails(exc), "--json", "--exit-nonzero-on-findings")
        payload = json.loads(out)
        self.assertEqual(payload["finding_count"], 1)
        self.assertEqual([e["repo"] for e in payload["repo_errors"]], ["o/disabled"])
        self.assertIn("disabled issues", payload["repo_errors"][0]["error"])
        self.assertIn("o/disabled", err)
        # A partial scan outranks "findings": the list is known to be incomplete.
        self.assertEqual(code, scanner.EXIT_PARTIAL_SCAN)

    def test_partial_scan_without_the_findings_flag_is_still_nonzero(self) -> None:
        exc = RuntimeError("gh issue list failed for o/disabled: boom")
        code, out, _ = self._run(self._one_fails(exc))
        self.assertEqual(code, scanner.EXIT_PARTIAL_SCAN)
        self.assertIn("o/disabled", out)  # the text report names the skipped repo

    def test_timeouts_and_bad_json_are_isolated_too(self) -> None:
        for exc in (
            scanner.subprocess.TimeoutExpired(cmd="gh", timeout=300),
            json.JSONDecodeError("Expecting value", "", 0),
            OSError("gh: not found"),
        ):
            with self.subTest(exc=type(exc).__name__):
                code, out, _ = self._run(self._one_fails(exc), "--json")
                payload = json.loads(out)
                self.assertEqual(payload["finding_count"], 1)
                self.assertEqual(len(payload["repo_errors"]), 1)
                self.assertEqual(code, scanner.EXIT_PARTIAL_SCAN)

    def test_every_repo_failing_is_partial_not_clean(self) -> None:
        def fetch(repo: str, limit: int):
            raise RuntimeError(f"gh issue list failed for {repo}: offline")

        code, out, _ = self._run(fetch, "--json")
        payload = json.loads(out)
        self.assertEqual(payload["finding_count"], 0)
        self.assertEqual(len(payload["repo_errors"]), 2)
        self.assertEqual(code, scanner.EXIT_PARTIAL_SCAN)

    def test_clean_fetch_reports_empty_repo_errors_and_exits_zero(self) -> None:
        code, out, _ = self._run(lambda repo, limit: [], "--json", "--exit-nonzero-on-findings")
        self.assertEqual(json.loads(out)["repo_errors"], [])
        self.assertEqual(code, 0)

    def test_partial_scan_code_is_distinct_from_every_other_code(self) -> None:
        self.assertNotIn(scanner.EXIT_PARTIAL_SCAN, (0, 1, 2, scanner.EXIT_NOT_CONFIGURED))


_BOOKING = "검증 종료 예정: 2026-09-17 07:32 KST"
_BOOKED_AT = "2026-09-16T22:23:42Z"


def _high(number: int, title: str = "example", url: str | None = None) -> dict:
    issue = _issue(number=number, title=title, comments=[_comment(_BOOKING, _BOOKED_AT)])
    if url is not None:
        issue["url"] = url
    return issue


def _low(number: int = 1528) -> dict:
    # Roadmap row quoting another issue's deadline: demoted to low confidence.
    return _issue(
        number=number,
        comments=[_comment("| [#1353](x) | 예정 종료 2026-09-14 21:41 KST |", "2026-09-13T00:00:00Z")],
    )


class NotifyTests(unittest.TestCase):
    """Owner notice via the bridge push spool (#1870 잔여 2번).

    On 2026-09-25 the installed cron caught ccc-node#1913 expired-unjudged and
    only wrote it to a log nobody reads. These pin the notice path: it fires on
    high findings, stays quiet on demoted ones, does not repeat itself daily,
    stays short and redacted, lands owner-only (0600) in the spool the bridge
    already drains, and can never change the scan's exit code.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.home / ".claude" / "state"
        self.spool = self.state / "telegram-spool"
        env = {key: value for key, value in os.environ.items() if not key.startswith("CCC_")}
        env.update({"HOME": str(self.home), "CCC_NODE": "testnode"})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, issues: list[dict], now: str, *extra: str) -> tuple[int, str]:
        path = self.root / "issues.jsonl"
        path.write_text(
            "".join(json.dumps(issue, ensure_ascii=False) + "\n" for issue in issues),
            encoding="utf-8",
        )
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = scanner.main(
                ["--input", str(path), "--now", now, "--exit-nonzero-on-findings", *extra]
            )
        return code, err.getvalue()

    def _spooled(self) -> list[dict]:
        if not self.spool.is_dir():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.spool.glob("*.json"))]

    def _run_repos(self, fetched: dict[str, list[dict] | Exception], now: str) -> tuple[int, str]:
        def fetch(repo: str, limit: int):
            result = fetched[repo]
            if isinstance(result, Exception):
                raise result
            return result

        args = ["--now", now, "--notify", "high", "--exit-nonzero-on-findings"]
        for repo in fetched:
            args += ["--repo", repo]
        err = io.StringIO()
        with mock.patch.object(scanner, "fetch_via_gh", side_effect=fetch), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = scanner.main(args)
        return code, err.getvalue()

    def _state_fingerprints(self) -> list[str]:
        state = self.state / "timed-test-deadline-scan.notify-expired.json"
        return json.loads(state.read_text(encoding="utf-8"))["fingerprints"]

    def test_partial_scan_keeps_skipped_repo_fingerprints(self) -> None:
        # #2324: an unreadable repo is not "its findings were resolved". Dropping
        # them from the dedup set would re-announce them as new once it recovers.
        a = dict(_high(1913), repo="o/a")
        b = dict(_high(7), repo="o/b")
        self._run_repos({"o/a": [a], "o/b": [b]}, "2026-09-21T01:52")
        self.assertEqual(len(self._spooled()), 1)
        both = sorted(self._state_fingerprints())

        code, err = self._run_repos(
            {"o/a": [a], "o/b": RuntimeError("gh issue list failed for o/b: offline")},
            "2026-09-21T09:20",
        )
        self.assertEqual(code, scanner.EXIT_PARTIAL_SCAN)
        self.assertEqual(sorted(self._state_fingerprints()), both)
        self.assertEqual(len(self._spooled()), 1)  # nothing new to say

        _, err = self._run_repos({"o/a": [a], "o/b": [b]}, "2026-09-21T13:20")
        self.assertEqual(len(self._spooled()), 1)  # recovery is not a new finding
        self.assertNotIn("reason=new", err)

    def test_partial_scan_still_drops_resolved_findings_of_readable_repos(self) -> None:
        a = dict(_high(1913), repo="o/a")
        b = dict(_high(7), repo="o/b")
        self._run_repos({"o/a": [a], "o/b": [b]}, "2026-09-21T01:52")
        self._run_repos({"o/a": [], "o/b": RuntimeError("offline")}, "2026-09-21T09:20")
        self.assertEqual(self._state_fingerprints(), ["o/b#7@2026-09-17T07:32"])

    def test_high_finding_is_spooled_owner_only(self) -> None:
        code, err = self._run([_high(1913, title="관측 종료 판정 대기")], "2026-09-21T01:52", "--notify", "high")
        self.assertEqual(code, 1)
        self.assertIn("reason=new", err)
        records = self._spooled()
        self.assertEqual(len(records), 1)
        record = records[0]
        # The PushNotifier contract: {ts, event, node, text, dedup}; no chatId,
        # so the bridge can only deliver it to the resolved owner chat.
        self.assertEqual(set(record), {"ts", "event", "node", "text", "dedup"})
        self.assertEqual(record["event"], "TimedTestDeadlineScan")
        self.assertEqual(record["node"], "testnode")
        self.assertEqual(record["ts"], "2026-09-20T16:52:00Z")
        self.assertTrue(record["dedup"].startswith("TimedTestDeadlineScan:expired:new:"))
        text = record["text"]
        self.assertIn("기한 경과 미판정 1건", text)
        self.assertIn("jinwon-int/ccc-node#1913 · 종료 2026-09-17 07:32 KST · 경과 3일", text)
        self.assertIn("관측 종료 판정 대기", text)
        self.assertIn("https://github.com/jinwon-int/ccc-node/issues/1913", text)

    def test_spool_and_state_are_0600_under_home_state_dir(self) -> None:
        self._run([_high(1913)], "2026-09-21T01:52", "--notify", "high")
        files = list(self.spool.iterdir())
        self.assertEqual(len(files), 1, files)  # no .tmp leftovers either
        self.assertRegex(files[0].name, r"^2026-09-20T16-52-00Z-TimedTestDeadlineScan-\d+-[0-9a-f]{8}\.json$")
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        state = self.state / "timed-test-deadline-scan.notify-expired.json"
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(saved["fingerprints"], ["jinwon-int/ccc-node#1913@2026-09-17T07:32"])
        self.assertEqual(saved["notified_at"], "2026-09-21T01:52")

    def test_state_dir_and_spool_env_overrides_are_honoured(self) -> None:
        custom_state = self.root / "state"
        custom_spool = self.root / "spool"
        with mock.patch.dict(
            os.environ, {"CCC_STATE_DIR": str(custom_state), "CCC_PUSH_SPOOL": str(custom_spool)}
        ):
            self._run([_high(1913)], "2026-09-21T01:52", "--notify", "high")
        self.assertEqual(len(list(custom_spool.glob("*.json"))), 1)
        self.assertTrue((custom_state / "timed-test-deadline-scan.notify-expired.json").is_file())
        self.assertFalse(self.spool.exists())

    def test_low_only_findings_stay_quiet_at_high(self) -> None:
        code, err = self._run([_low()], "2026-09-21T01:52", "--notify", "high")
        self.assertEqual(code, 1)  # still a finding for the log and the exit code
        self.assertEqual(self._spooled(), [])
        self.assertIn("nothing at level=high", err)

    def test_low_level_includes_demoted_findings(self) -> None:
        self._run([_low()], "2026-09-21T01:52", "--notify", "low")
        self.assertEqual(len(self._spooled()), 1)

    def test_notify_is_off_unless_asked(self) -> None:
        code, err = self._run([_high(1913)], "2026-09-21T01:52")
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertFalse(self.spool.exists())
        self.assertFalse(self.state.exists())

    def test_env_sets_the_default_level_and_flag_wins(self) -> None:
        with mock.patch.dict(os.environ, {"CCC_TIMED_TEST_SCAN_NOTIFY": "high"}):
            self._run([_high(1913)], "2026-09-21T01:52")
            self.assertEqual(len(self._spooled()), 1)
            self._run([_high(1914)], "2026-09-21T01:52", "--notify", "off")
            self.assertEqual(len(self._spooled()), 1)

    def test_bogus_env_level_warns_and_stays_off(self) -> None:
        with mock.patch.dict(os.environ, {"CCC_TIMED_TEST_SCAN_NOTIFY": "loud"}):
            code, err = self._run([_high(1913)], "2026-09-21T01:52")
        self.assertEqual(code, 1)
        self.assertIn("ignoring CCC_TIMED_TEST_SCAN_NOTIFY", err)
        self.assertEqual(self._spooled(), [])

    def test_unchanged_set_is_not_resent_until_the_reminder(self) -> None:
        issues = [_high(1913)]
        self._run(issues, "2026-09-21T09:20", "--notify", "high")
        code, err = self._run(issues, "2026-09-22T09:20", "--notify", "high")
        self.assertEqual(code, 1)
        self.assertIn("unchanged set of 1", err)
        self._run(issues, "2026-09-23T09:20", "--notify", "high")
        self.assertEqual(len(self._spooled()), 1)
        # Three calendar days later, even if cron starts a little earlier.
        self._run(issues, "2026-09-24T09:19", "--notify", "high")
        records = self._spooled()
        self.assertEqual(len(records), 2)
        reminder = next(r for r in records if ":reminder:" in r["dedup"])
        self.assertIn("3일+ 미해소 재알림", reminder["text"])
        # The reminder restarts the clock.
        self._run(issues, "2026-09-25T09:20", "--notify", "high")
        self.assertEqual(len(self._spooled()), 2)

    def test_new_finding_notifies_immediately_and_is_marked(self) -> None:
        self._run([_high(1913)], "2026-09-21T09:20", "--notify", "high")
        self._run([_high(1913), _high(1999)], "2026-09-22T09:20", "--notify", "high")
        records = self._spooled()
        self.assertEqual(len(records), 2)
        latest = next(r for r in records if "미판정 2건 (신규 1건)" in r["text"])
        self.assertIn("🆕 jinwon-int/ccc-node#1999", latest["text"])
        self.assertIn("• jinwon-int/ccc-node#1913", latest["text"])

    def test_resolved_then_returning_finding_counts_as_new(self) -> None:
        self._run([_high(1913), _high(1999)], "2026-09-21T09:20", "--notify", "high")
        self._run([_high(1913)], "2026-09-22T09:20", "--notify", "high")  # 1999 judged
        self.assertEqual(len(self._spooled()), 1)
        self._run([_high(1913), _high(1999)], "2026-09-23T09:20", "--notify", "high")
        self.assertEqual(len(self._spooled()), 2)

    def test_clean_scan_clears_state_so_the_next_finding_notifies(self) -> None:
        self._run([_high(1913)], "2026-09-21T09:20", "--notify", "high")
        self._run([_issue(body="시한부 테스트 없음")], "2026-09-22T09:20", "--notify", "high")
        self._run([_high(1913)], "2026-09-23T09:20", "--notify", "high")
        self.assertEqual(len(self._spooled()), 2)

    def test_message_is_capped_short_and_redacted(self) -> None:
        redaction_probe_title = "토큰 누출 ghp_" + "A" * 36 + " 그리고 아주 긴 제목이 계속해서 이어집니다 " * 3
        issues = [_high(2000 + i, title=f"제목 {i}") for i in range(12)]
        issues.append(_high(2100, title=redaction_probe_title, url="https://evil.example/phish"))
        self._run(issues, "2026-09-21T01:52", "--notify", "high")
        (record,) = self._spooled()
        text = record["text"]
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("⏰ 시한부 테스트 기한 경과 미판정 13건"))
        finding_lines = [line for line in lines if line.startswith(("🆕 ", "• "))]
        self.assertEqual(len(finding_lines), 10)
        self.assertIn("외 3건", lines)
        # No body/paragraph excerpt ever reaches the notice.
        self.assertNotIn("검증 종료 예정", text)
        self.assertNotIn("ghp_", text)
        self.assertNotIn("evil.example", text)
        for line in lines:
            if line.startswith("  ") and not line.startswith("  https://"):
                self.assertLessEqual(len(line.strip()), scanner.NOTIFY_TITLE_CHARS)

    def test_capped_list_keeps_the_new_findings_visible(self) -> None:
        old = [_high(3000 + i) for i in range(12)]
        self._run(old, "2026-09-21T09:20", "--notify", "high")
        self._run([*old, _high(3999)], "2026-09-22T09:20", "--notify", "high")
        latest = next(r for r in self._spooled() if "미판정 13건 (신규 1건)" in r["text"])
        self.assertIn("🆕 jinwon-int/ccc-node#3999", latest["text"])

    def test_notify_failure_never_masks_the_scan_result(self) -> None:
        blocker = self.root / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CCC_PUSH_SPOOL": str(blocker / "spool")}):
            code, err = self._run([_high(1913)], "2026-09-21T01:52", "--notify", "high")
        self.assertEqual(code, 1)
        self.assertIn("notify failed (scan result unaffected)", err)
        # State is only advanced after a successful spool write, so the next
        # run retries instead of believing it already told the owner.
        self.assertFalse((self.state / "timed-test-deadline-scan.notify-expired.json").exists())
        self._run([_high(1913)], "2026-09-21T01:53", "--notify", "high")
        self.assertEqual(len(self._spooled()), 1)

    def test_clean_scan_exit_code_is_untouched_by_notify(self) -> None:
        code, _ = self._run([_issue(body="시한부 테스트 없음")], "2026-09-21T01:52", "--notify", "low")
        self.assertEqual(code, 0)
        self.assertEqual(self._spooled(), [])

    def test_corrupt_state_fails_toward_notifying(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "timed-test-deadline-scan.notify-expired.json").write_text("{nope", encoding="utf-8")
        self._run([_high(1913)], "2026-09-21T01:52", "--notify", "high")
        self.assertEqual(len(self._spooled()), 1)

    def test_relative_mode_uses_its_own_state_and_wording(self) -> None:
        issue = _issue(
            number=104,
            comments=[_comment("**잔여**: 며칠 관찰 후 완전 삭제 별도 승인", "2026-09-17T05:29:33Z")],
        )
        self._run([issue], "2026-09-21T01:52", "--mode", "relative", "--notify", "high")
        (record,) = self._spooled()
        self.assertIn("절대 종료시각 없음 1건", record["text"])
        self.assertIn("jinwon-int/ccc-node#104 · 절대시각 없음", record["text"])
        self.assertTrue((self.state / "timed-test-deadline-scan.notify-relative.json").is_file())


    def test_followup_mode_uses_its_own_state_and_wording(self) -> None:
        issue = _fu_issue([_comment(FU_VERDICT, FU_VERDICT_AT)])
        code, _ = self._run([issue], "2026-10-15T16:00", "--mode", "followup", "--notify", "high")
        self.assertEqual(code, 1)
        (record,) = self._spooled()
        self.assertIn("판정 후 후속 기한 경과 1건", record["text"])
        self.assertIn(f"{FU_REPO}#42 · 후속 2026-10-14 09:00 KST · 경과 1일", record["text"])
        self.assertTrue((self.state / "timed-test-deadline-scan.notify-followup.json").is_file())



# --- followup mode (#2324 (2)) ------------------------------------------------
# Synthesized fixtures: a verdict that promises a follow-up at an absolute KST
# time, then silence. Generic repo names only.
FU_REPO = "example-org/example-repo"
FU_NOW = dt.datetime(2026, 10, 15, 16, 0)
FU_VERDICT_AT = "2026-10-13T01:00:00Z"  # 2026-10-13 10:00 KST
FU_VERDICT = "## 재측정 판정 — 조건부 합격\n지표는 기준 안이다.\n다음 확인: 2026-10-14 09:00 KST"


def _fu_issue(comments: list[dict], **extra) -> dict:
    issue = _issue(number=42, repo=FU_REPO, title="followup fixture", comments=comments)
    issue.update(extra)
    return issue


class FollowupModeTests(unittest.TestCase):
    def test_overdue_followup_after_verdict_is_reported(self) -> None:
        issue = _fu_issue([_comment(FU_VERDICT, FU_VERDICT_AT, "https://example/c/9")])
        finding = scanner.judge_issue(issue, FU_NOW, "followup")
        assert finding is not None
        self.assertEqual(finding.kind, "followup-overdue")
        self.assertEqual(finding.deadline, dt.datetime(2026, 10, 14, 9, 0))
        self.assertEqual(finding.days_overdue, 1)
        self.assertEqual(finding.confidence, "high")
        self.assertEqual(finding.comments_after, 0)
        self.assertEqual(finding.source_url, "https://example/c/9")
        self.assertIn("다음 확인", finding.paragraph)

    def test_followup_in_a_later_comment_counts(self) -> None:
        issue = _fu_issue(
            [
                _comment("## 관측 종료 판정 — 합격", FU_VERDICT_AT),
                _comment("- **후속 재측정**: 2026-10-14 09:00 KST", "2026-10-13T03:00:00Z"),
            ]
        )
        finding = scanner.judge_issue(issue, FU_NOW, "followup")
        assert finding is not None
        self.assertEqual(finding.confidence, "high")

    def test_english_label_is_recognised(self) -> None:
        issue = _fu_issue(
            [_comment("**Verdict**: PASS\nNext action: 2026-10-14 09:00 KST re-measure", FU_VERDICT_AT)]
        )
        finding = scanner.judge_issue(issue, FU_NOW, "followup")
        assert finding is not None
        self.assertEqual(finding.confidence, "high")

    def test_comment_after_followup_time_clears_it(self) -> None:
        issue = _fu_issue(
            [
                _comment(FU_VERDICT, FU_VERDICT_AT),
                _comment("재측정 결과: 기준 안", "2026-10-14T01:30:00Z"),  # 10:30 KST
            ]
        )
        self.assertIsNone(scanner.judge_issue(issue, FU_NOW, "followup"))

    def test_comment_before_followup_time_is_not_evidence(self) -> None:
        issue = _fu_issue(
            [
                _comment(FU_VERDICT, FU_VERDICT_AT),
                _comment("참고 링크 추가", "2026-10-13T20:00:00Z"),  # 10-14 05:00 KST
            ]
        )
        self.assertIsNotNone(scanner.judge_issue(issue, FU_NOW, "followup"))

    def test_future_followup_is_not_reported(self) -> None:
        issue = _fu_issue([_comment(FU_VERDICT, FU_VERDICT_AT)])
        early = dt.datetime(2026, 10, 14, 8, 59)
        self.assertIsNone(scanner.judge_issue(issue, early, "followup"))

    def test_followup_before_any_verdict_is_ignored(self) -> None:
        # A booking's own plan is expired mode's business, not a promise made
        # by a verdict.
        issue = _fu_issue(
            [_comment("## 실행 계획\n다음 확인: 2026-10-14 09:00 KST", FU_VERDICT_AT)]
        )
        self.assertIsNone(scanner.judge_issue(issue, FU_NOW, "followup"))

    def test_issue_body_is_never_a_verdict(self) -> None:
        issue = _fu_issue([])
        issue["body"] = "## 판정 — 합격\n다음 확인: 2026-10-14 09:00 KST"
        self.assertIsNone(scanner.judge_issue(issue, FU_NOW, "followup"))

    def test_marked_utc_followup_is_converted_to_kst(self) -> None:
        issue = _fu_issue(
            [_comment("## 판정 — 합격\n다음 확인: 2026-10-14 00:00 UTC", FU_VERDICT_AT)]
        )
        finding = scanner.judge_issue(issue, FU_NOW, "followup")
        assert finding is not None
        self.assertEqual(finding.deadline, dt.datetime(2026, 10, 14, 9, 0))

    def test_rescheduled_followup_is_not_reported(self) -> None:
        issue = _fu_issue(
            [
                _comment(FU_VERDICT, FU_VERDICT_AT),
                _comment("다음 확인: 2026-10-20 09:00 KST 로 미룬다", "2026-10-13T11:00:00Z"),
            ]
        )
        self.assertIsNone(scanner.judge_issue(issue, FU_NOW, "followup"))

    def test_passing_mention_is_demoted(self) -> None:
        issue = _fu_issue(
            [_comment("## 판정 — 합격\n후속 PR에서 2026-10-14 09:00 KST 이후 확인한다", FU_VERDICT_AT)]
        )
        finding = scanner.judge_issue(issue, FU_NOW, "followup")
        assert finding is not None
        self.assertEqual(finding.confidence, "low")
        self.assertEqual(finding.weak_reason, "followup-mention")

    def test_cross_reference_and_date_only_are_demoted(self) -> None:
        for line, reason in (
            ("다음 확인: #7 에서 2026-10-14 09:00 KST", "cross-reference"),
            ("다음 확인: 2026-10-13", "date-only"),
        ):
            with self.subTest(line=line):
                issue = _fu_issue([_comment(f"## 판정 — 합격\n{line}", FU_VERDICT_AT)])
                finding = scanner.judge_issue(issue, FU_NOW, "followup")
                assert finding is not None
                self.assertEqual(finding.weak_reason, reason)

    def test_heading_only_verdict_is_demoted(self) -> None:
        issue = _fu_issue(
            [_comment("## 진행 메모\n판정: 보류\n다음 확인: 2026-10-14 09:00 KST", FU_VERDICT_AT)]
        )
        finding = scanner.judge_issue(issue, FU_NOW, "followup")
        assert finding is not None
        self.assertEqual(finding.weak_reason, "verdict-heading-only")

    def test_past_at_posting_record_is_not_a_promise(self) -> None:
        issue = _fu_issue(
            [_comment("## 판정 — 합격\n재측정: 2026-10-12 09:00 KST 에 했다", FU_VERDICT_AT)]
        )
        self.assertIsNone(scanner.judge_issue(issue, FU_NOW, "followup"))

    def test_min_confidence_high_drops_demoted_followups(self) -> None:
        issue = _fu_issue(
            [_comment("## 판정 — 합격\n후속 PR에서 2026-10-14 09:00 KST 이후 확인한다", FU_VERDICT_AT)]
        )
        self.assertEqual(scanner.scan([issue], FU_NOW, "followup", "high"), [])
        self.assertEqual(len(scanner.scan([issue], FU_NOW, "followup", "low")), 1)

    def test_closed_issue_state_is_reported(self) -> None:
        issue = _fu_issue([_comment(FU_VERDICT, FU_VERDICT_AT)], state="CLOSED")
        finding = scanner.judge_issue(issue, FU_NOW, "followup")
        assert finding is not None
        self.assertEqual(finding.to_dict()["issue_state"], "closed")
        report = scanner.format_report([finding], FU_NOW, "followup")
        self.assertIn("후속 기한 2026-10-14 09:00 | 경과 1일 | 닫힌 이슈", report)
        self.assertIn("후속 기한 후 코멘트 0건", report)

    def test_clean_followup_report_wording(self) -> None:
        self.assertEqual(
            scanner.format_report([], FU_NOW, "followup"),
            "[2026-10-15 16:00 KST] followup: 해당 없음 — 판정 후 후속 기한 경과 0건",
        )


class FollowupExistingModesTests(unittest.TestCase):
    """followup is opt-in: the other modes must not see it at all."""

    def test_followup_shape_is_invisible_to_existing_modes(self) -> None:
        issue = _fu_issue([_comment(FU_VERDICT, FU_VERDICT_AT)])
        self.assertIsNone(scanner.judge_issue(issue, FU_NOW, "expired"))
        self.assertIsNone(scanner.judge_issue(issue, FU_NOW, "relative"))

    def test_expired_output_shape_is_unchanged(self) -> None:
        issue = _issue(
            comments=[_comment("검증 종료 예정: 2026-09-17 07:32 KST", "2026-09-16T22:23:42Z")]
        )
        finding = scanner.judge_issue(issue, NOW, "expired")
        assert finding is not None
        self.assertNotIn("issue_state", finding.to_dict())
        self.assertEqual(
            scanner.format_report([finding], NOW, "expired").splitlines()[2],
            "### jinwon-int/ccc-node#1527 | 종료 2026-09-17 07:32 | 경과 3일",
        )
        self.assertEqual(
            scanner.format_report([], NOW, "expired"),
            "[2026-09-21 01:52 KST] expired: 해당 없음 — 기한 경과 미판정 0건",
        )

    def test_expired_json_has_no_followup_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "issues.jsonl"
            path.write_text(json.dumps(_issue(), ensure_ascii=False) + "\n", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                scanner.main(["--input", str(path), "--now", "2026-09-21T01:52", "--json"])
        self.assertNotIn("include_closed_days", json.loads(out.getvalue()))


class IncludeClosedDaysTests(unittest.TestCase):
    def _main(self, *args: str) -> tuple[int, list]:
        calls: list = []

        def fetch(*call_args):
            calls.append(call_args)
            return []

        with mock.patch.object(scanner, "fetch_via_gh", side_effect=fetch), \
                contextlib.redirect_stdout(io.StringIO()):
            code = scanner.main(["--repo", "o/r", "--now", "2026-10-15T16:00", *args])
        return code, calls

    def test_default_is_zero_and_open_only(self) -> None:
        self.assertEqual(scanner.build_parser().parse_args(["--repo", "o/r"]).include_closed_days, 0)
        _, calls = self._main("--mode", "followup")
        self.assertEqual(calls, [("o/r", 200)])

    def test_followup_mode_passes_the_closed_since_date(self) -> None:
        code, calls = self._main("--mode", "followup", "--include-closed-days", "45")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("o/r", 200, dt.date(2026, 8, 31))])

    def test_other_modes_reject_the_flag(self) -> None:
        for mode in ("expired", "relative"):
            with self.subTest(mode=mode), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self._main("--mode", mode, "--include-closed-days", "5")
                self.assertEqual(raised.exception.code, 2)

    def test_negative_days_are_rejected(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self._main("--mode", "followup", "--include-closed-days", "-1")
        self.assertEqual(raised.exception.code, 2)

    def _gh(self, outputs: list[list[dict]]):
        procs = [
            mock.Mock(returncode=0, stdout=json.dumps(out), stderr="") for out in outputs
        ]
        return mock.patch.object(scanner.subprocess, "run", side_effect=procs)

    def test_fetch_without_closed_since_runs_the_original_query(self) -> None:
        with self._gh([[{"number": 1}]]) as run:
            issues = scanner.fetch_via_gh("o/r", 50)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            ["gh", "issue", "list", "--repo", "o/r", "--state", "open", "--limit", "50",
             "--json", "number,title,url,body,comments,createdAt,updatedAt"],
        )
        self.assertEqual(issues, [{"number": 1, "repo": "o/r"}])

    def test_fetch_with_closed_since_adds_a_closed_query(self) -> None:
        with self._gh([[{"number": 1}], [{"number": 1}, {"number": 2, "state": "CLOSED"}]]) as run:
            issues = scanner.fetch_via_gh("o/r", 50, dt.date(2026, 8, 31))
        self.assertEqual(run.call_count, 2)
        closed_argv = run.call_args_list[1].args[0]
        self.assertIn("closed", closed_argv)
        self.assertIn("closed:>=2026-08-31", closed_argv)
        self.assertTrue(closed_argv[-1].endswith(",state,closedAt"))
        self.assertEqual([issue["number"] for issue in issues], [1, 2])

    def test_closed_query_failure_raises_for_isolation(self) -> None:
        procs = [
            mock.Mock(returncode=0, stdout="[]", stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="boom"),
        ]
        with mock.patch.object(scanner.subprocess, "run", side_effect=procs):
            with self.assertRaises(RuntimeError) as raised:
                scanner.fetch_via_gh("o/r", 50, dt.date(2026, 8, 31))
        self.assertIn("(closed)", str(raised.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
