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


if __name__ == "__main__":
    unittest.main(verbosity=2)
