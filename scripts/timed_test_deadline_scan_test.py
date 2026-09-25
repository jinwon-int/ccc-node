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


if __name__ == "__main__":
    unittest.main(verbosity=2)
