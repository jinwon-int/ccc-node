#!/usr/bin/env python3
"""Find timed-test deadlines that expired without anyone judging them.

The owner rule (2026-09-11) requires every future-ending test — canary,
observation window, A/B, trial — to carry an absolute KST end datetime in its
GitHub issue. The rule says *write it down*; it does not say *wake up then*.
Those are separate problems, and a 2026-09-21 fleet-wide sweep found eight
issues whose deadline had passed with no verdict, the oldest silent for nine
days (jinwon-int/ccc-node#1870).

This scanner closes that gap. It reads issues plus their comments, extracts
deadlines near timed-test keywords, and reports whatever expired without a
verdict.

Known false-positive shapes are *demoted, not dropped*. Recall is the whole
point: a missed finding went unnoticed for nine days, while a false positive
costs an operator seconds. Measured against the 2026-09-21 snapshot, the
scanner finds 12 issues of which 7 are real — recall 7/7, precision 7/12 —
and correctly stays quiet about ccc-node#1353, which was judged on time.
``--min-confidence high`` trades recall away for a shorter list.

Three modes:

``expired``
    Deadline is in the past and no verdict-shaped comment followed it — or
    preceded it, for a test run early and judged after its booking. A comment
    that books only a start ("실행 예약 … 22:00 KST") plus offsets ("T+24h")
    gets an inferred deadline of start + largest offset, at low confidence.

``relative``
    A timed test described only in relative terms ("며칠", "1~2주", "T+24h",
    "24시간 후") with no absolute datetime anywhere in the paragraph. These
    violate the rule at write time, so they can never be judged on schedule.
    Shapes that the 2026-10-27 audit measured as false positives (7 of 10) —
    "당분간" operations, effort estimates, external process durations, quoted
    rule text, judged checkboxes, outside authors — are demoted to low
    confidence with a reason code (#2544); see ``classify_relative_paragraph``.

``followup`` (opt-in, #2324)
    A verdict was posted, and the verdict comment or a later one promised a
    follow-up ("다음 확인", "후속", "재측정", "재확인", "next action",
    "follow-up") at an absolute KST datetime that has now passed with no
    comment at all after it. The other modes treat a verdict as the end of an
    issue, so a promised re-measure that never happened stayed invisible. A
    promise that is not written as a label, or that rests on a heading-only
    verdict, is demoted to low confidence. ``--include-closed-days N`` also
    fetches issues closed within the last N days, because a verdict is often
    followed by an immediate close (or a PR merge auto-close) that strands
    its follow-up.

Input is a JSONL dump (one issue object per line, ``--input``), a live ``gh``
query (``--repo``), or an operator-owned allowlist file (``--repos-file``).
The JSONL path keeps the scanner testable and lets an operator re-run a
judgement against a frozen snapshot; the allowlist is what the cron installer
wires up, so adding a repository never requires re-running the installer.

Exit codes: 0 clean, 1 findings exist (with ``--exit-nonzero-on-findings``),
2 unusable repo list, 3 not configured, 4 partial scan. The third one matters —
a scheduled scan whose repo list is missing or empty must not report a
reassuring "0건". So does the fourth: a repo that cannot be read (Issues
disabled, gh failure, timeout) is skipped and listed under ``repo_errors``
while every other repo is still scanned, and the run exits 4 even when the
findings flag would have said 1 (#2324).

Owner notification (``--notify {off,high,low}``, env
``CCC_TIMED_TEST_SCAN_NOTIFY``, default off; the cron installer renders
``high``). A log nobody reads is the failure this scanner hunts, and on
2026-09-25 its own cron log caught ccc-node#1913 expired-unjudged with nobody
looking. With notify enabled, findings at or above the chosen confidence are
summarised into the same owner-only bridge spool that agent-cron,
ccc-self-update.sh and ccc-pr-status-poll.sh already use
(``~/.claude/state/telegram-spool``, delivered by the bridge PushNotifier).
The scanner never touches a bot token. An identical finding set is not
re-sent daily: new findings notify at once, an unchanged set is re-sent as a
reminder after three calendar days. A notification failure is reported on
stderr and never changes the exit code.

CI does not count as a timed test (owner, 2026-09-11): runs finish in minutes
and GitHub shows the result, so ``gh-ci-wait`` covers them instead. This
scanner deliberately does not look at check runs.

Run standalone:
    python3 scripts/timed_test_deadline_scan.py --repo jinwon-int/ccc-node
    python3 scripts/timed_test_deadline_scan.py --input issues.jsonl --json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

KST = dt.timezone(dt.timedelta(hours=9))

# Reserved so a scheduled run can tell "nothing to report" (0) apart from
# "nobody told me what to scan" (3). --exit-nonzero-on-findings owns 1.
EXIT_NOT_CONFIGURED = 3
# At least one repo could not be read (Issues disabled, gh failure, timeout,
# unparsable output). The remaining repos are still scanned and reported, but
# the result is known to be incomplete, so this outranks the findings code 1
# (#2324: a single Issues-disabled repo used to abort the whole run).
EXIT_PARTIAL_SCAN = 4
# Failures isolated per repo. Anything else is a scanner bug and should crash.
REPO_FETCH_ERRORS = (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError)

# Paragraph must mention a timed test before a date in it counts as a deadline.
#
# "deadline" must stand alone: as part of an identifier or path it is this
# scanner's own name (`timed_test_deadline_scan.py`,
# `timed-test-deadline-scan.repos`), and any issue that quotes the command
# next to a timestamp — ccc-node#1873 did — became a high-confidence finding.
#
# "Expected end time" is the field the fleet's own future-ending-test-issue-
# tracking template writes. Without it ccc-node#2109's real booking
# (2026-10-12 07:44 KST) was invisible and an unrelated claim ETA won (#2150).
DEADLINE_KEYWORD = re.compile(
    r"(종료\s*(일시|시각|예정|시점)"
    r"|관측\s*종료|테스트\s*종료|검증\s*종료|예정\s*종료"
    r"|(?<![\w./-])deadline(?![\w./-])|observation\s+window|planned\s+end"
    r"|expected\s+end(?:\s+time)?|(?<![\w./-])end\s+time(?![\w./-]))",
    re.IGNORECASE,
)

# 2026-09-17 07:32 / 2026.09.17 / 2026년 9월 17일 07:32 / 2026-09-28 07:36:11 UTC
#
# Group 6 captures an explicit UTC marker. Without it a completion report's
# "테스트 종료: 2026-09-28 07:36:11 UTC (16:36 KST)" was read as 07:36 *KST*,
# nine hours early (a2a-nexus#2256). "UTC+09:00" is an offset annotation, not
# a UTC clock reading, so a following sign excludes it.
DEADLINE_DATE = re.compile(
    r"(20\d{2})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})일?"
    r"(?:\s*(?:T|\s)\s*(\d{1,2}):(\d{2})(?::\d{2})?"
    r"(?:\s*(UTC|GMT)(?![+-]\d)\b|(Z)\b)?)?"
)
UTC_TO_KST = dt.timedelta(hours=9)

# A comment that reads like somebody actually rendered a judgement.
#
# Deliberately narrow. An earlier draft accepted the bare word "완료", which
# appears in almost any progress update, and that silently swallowed three of
# the eight real findings in the 2026-09-21 sweep (ccc-node#1692, #1353 and
# a2a-nexus#1597 all had post-deadline comments saying some *other* thing was
# complete). Words that also appear in the *scheduling* comment — "검증 종료",
# "관측 종료" — are excluded for the same reason: they cannot distinguish a
# booking from a verdict.
#
# "close/closed" is absent on purpose: `fail-closed` is everywhere in this
# fleet's prose and matched a2a-nexus#1597 straight out of the results.
#
# The qualified completions ("검증 완료", "실제 종료", "결과 갱신") were added
# after the first production run (#1873): ccc-node#1648 was judged with the
# heading "## ✅ 배포 검증 완료 — 실제 종료 일시·결과 갱신" and kept being
# reported at high confidence. Bare "완료" and "✅" stay out — both lead
# ordinary progress updates ("yukson 연결 완료") that say nothing about the
# test — and the whole regex still only sees the heading window.
VERDICT = re.compile(
    r"(판정|결과\s*보고|종료\s*보고|합격|불합격|연장|재관측"
    r"|실제\s*종료|결과\s*갱신|(검증|관측|테스트|카나리)\s*완료)",
    re.IGNORECASE,
)

# A verdict that lands *before* the deadline — the test was run early and
# judged on the spot — counts only when it follows the booking and its heading
# states a conclusion. "연장"/"재관측" are excluded: posted early they announce
# more waiting, not a result (and an extension carries its own new deadline).
# a2a-nexus#1597 booked 2026-09-26 12:00, ran early and was judged
# "## 재카나리 판정 — 불합격" then "## 재카나리 r2 — 합격" the evening before,
# yet was reported expired-unjudged because only post-deadline comments were
# read. A verdict posted before the booking still never counts.
#
# The pre-deadline window spans days of progress updates, so it is held to a
# stricter test than the post-deadline one: only the *first line* is read, and
# "판정" must not be a table, a criterion, a pending state or an interim call,
# nor a topic ("판정은 종료 후") — a real one then still says 합격/불합격.
# Matching the 200-char window instead cleared ccc-node#1528 on
# "## 오너 결정 반영 — TM-3353 판정표 후속", which judged nothing.
EARLY_VERDICT = re.compile(
    r"((?<!중간\s)(?<!중간)판정(?!\s*(표|기준|대기|예정|보류|일정|요청|전)|은|는)"
    r"|결과\s*보고|종료\s*보고|합격|불합격"
    r"|실제\s*종료|결과\s*갱신|(검증|관측|테스트|카나리)\s*완료)",
    re.IGNORECASE,
)

# A verdict announces itself in the heading. Searching the whole comment body
# instead picked up "판정" from a table header (`| 항목 | 값 | 판정 |`,
# ccc-node#1692) and from mid-sentence prose ("불충분 판정", a2a-nexus#1597),
# hiding both real findings. Real verdicts led with "## 관측 종료 판정"
# (ccc-node#1353), so the heading window is where to look.
VERDICT_HEADER_CHARS = 200


def _first_line(body: str) -> str:
    for line in (body or "").splitlines():
        if line.strip():
            return line.strip()[:VERDICT_HEADER_CHARS]
    return ""


# The deadline paragraph itself already reports a finished test.
SETTLED = re.compile(
    r"(완료|✅|통과|합격|불합격|종료\s*보고|결과\s*갱신|실제\s*종료|판정)",
)

# How much of a comment counts as its "header" when deciding whether the whole
# comment is a completion report. A "## 테스트 종료 보고" heading sits well
# inside this window, while unrelated later prose does not leak in.
SETTLED_HEADER_CHARS = 150

# Demotion reasons that mean "this mention records a deadline, it does not
# book one". Such hits never move the early-verdict window (#2149): a verdict
# restating the deadline it beat, a report inside a completion comment, or a
# time that had already passed when the comment was posted.
NOT_A_BOOKING = frozenset({"already-settled", "already-settled-comment", "past-at-posting"})

# Relative-only durations the owner rule forbids.
#
# Hour offsets ("T+24h", "24시간 후", "48h 관측") are relative too: with no
# absolute datetime in the paragraph nobody can schedule the judgement.
# a2a-nexus#1504's "B8: 관측 타이머 T+1/6/12/24h" went unjudged for two days
# and this mode never saw it (#2252).
RELATIVE_DURATION = re.compile(
    r"(며칠|몇\s*일|수일|여러\s*날|1~2주|한두\s*주|몇\s*주|수주간|당분간"
    r"|a\s+few\s+days|couple\s+of\s+(days|weeks)|in\s+a\s+week"
    r"|(?<![A-Za-z])T\s*\+\s*\d+(?:\s*h)?(?:\s*[/,·]\s*\d+(?:\s*h)?)*\s*h(?![A-Za-z])"
    r"|(?<![\d.])\d+\s*시간\s*(?:후|뒤|관측)"
    r"|(?<![\d.])\d+\s*h\s*관측)",
    re.IGNORECASE,
)

# --- relative mode false positives (#2544) -----------------------------------
#
# The 2026-10-27 fleet audit ran relative mode over 42 repos: 10 findings, 7
# of them false positives. With `--notify high` that is a high-confidence ping
# every three days for nothing, which is how a real finding gets ignored. The
# shapes below are each grounded in one of those seven and are *demoted, not
# dropped* — the low findings stay in the log. The two real ones must stay
# high: a2a-nexus#2395 "소크: 실제 T2 태스크 수일분" and family-messenger#404
# "- [ ] 관찰 기간: 공명에서 며칠 운영".
RELATIVE_KIND = "relative-duration-only"
# Reason codes (Finding.weak_reason), one per measured shape.
REL_EXTERNAL_AUTHOR = "external-author"  # esp32-iphone-agent#77
REL_QUOTED_TERM = "quoted-term"  # family-messenger#179 "'며칠' 식 문구"
REL_CHECKBOX_JUDGED = "checkbox-verdict-recorded"  # family-messenger#167
REL_OPERATIONAL = "operational-wording"  # esp32-iphone-agent#74/#60, ccc-node#1864
REL_PLANNING = "planning-estimate"  # family-messenger#271 "스파이크(1~2주)"
REL_PROCESS = "process-duration"  # family-messenger#338 "수일 소요"

# Accounts whose words are the fleet's own (skills/external-github-content:
# jinon86, seoseo-ai and the fleet dependency bots). GitHub renders an app as
# `app/<name>` or `<name>[bot]`; both normalize to the bare name. Anyone else
# cannot book a fleet timed test, so their relative wording is conversation.
FLEET_AUTHORS = frozenset({"jinon86", "seoseo-ai", "dependabot", "jinwon-deps-bot", "github-actions"})

# Words that make a relative duration a genuine observation window. Rules that
# judge the *wording* (operational / planning / process) never fire when one
# of these is in the paragraph: "관찰 기간: … 며칠 운영" is a schedule-less
# observation because it says 관찰, and "소크 … 수일분" because it says 소크.
OBSERVATION_WORD = re.compile(
    r"(관찰|관측|소크|카나리|모니터링|지켜\s*보|파일럿|시범\s*운영"
    r"|soak|canary|monitor|observ|pilot|burn[\s-]?in)",
    re.IGNORECASE,
)
# A relative term quoted as a word, not used: "'며칠' 식 문구를 남기지 않게",
# a nag citing the booking ("실제 T2 태스크 수일분"). The span must open and
# close on a non-space character so the gap *between* two quotations is never
# read as one, and a single quote must not sit inside a word (don't / it's).
QUOTE_SPAN = re.compile(
    r'"\S(?:[^"\n]{0,78}\S)?"'
    r"|“[^”\n]{1,80}”|‘[^’\n]{1,80}’|「[^」\n]{1,80}」|『[^』\n]{1,80}』"
    r"|(?<![\w'])'\S(?:[^'\n]{0,78}\S)?'(?![\w'])"
)
# Markdown task-list item ("- [ ] …", "* [x] …", "1. [ ] …").
CHECKBOX_LINE = re.compile(r"^(?:[-*+]|\d+[.)])\s+\[[ xX]\]")
# "당분간" is "for the time being": a standing arrangement, not a test window.
OPERATIONAL_TERM = re.compile(r"^당분간$")
# A work-effort estimate attached to a plan item: "스파이크(1~2주)", or the
# duration alone in brackets right after an item name ("**D. 푸시**(1~2주)").
PLANNING_NOUN = re.compile(
    r"(스파이크|spike|구현|개발|작업|공수|일정|마일스톤|milestone|estimate|effort)",
    re.IGNORECASE,
)
PLANNING_WINDOW_CHARS = 15
# The time an outside procedure takes: "신분증 … 수일 소요", "며칠 걸림", or a
# review/issuance step just before the term ("심사(통상 수일, …)",
# family-messenger#584). "승인" is deliberately absent: "승인 후 며칠 운영"
# can be a real trial.
PROCESS_SUFFIX = re.compile(
    r"\s*(?:정도\s*)?(소요|걸(?:림|린다|립니다|려요|려서|리는)|takes?\b|to\s+complete)",
    re.IGNORECASE,
)
PROCESS_NOUN = re.compile(r"(심사|발급|배송|(?<![\w-])review)", re.IGNORECASE)

# Anchor + offset bookings (#2252). a2a-nexus#1504 booked Phase 7-B as
# "실행 예약: … OnCalendar=2026-10-09 22:00:00 KST" with "B8: 관측 타이머
# T+1/6/12/24h" a few lines below. No end-word was written, so DEADLINE_KEYWORD
# never fired, and the run's failure sat unjudged for about two days. Within
# one comment, a start anchor plus offsets infer a deadline of anchor + the
# largest offset. The inference is a guess, so it is always low confidence and
# never used when the same comment already states an explicit end.
ANCHOR_KEYWORD = re.compile(
    r"(실행\s*(?:예약|일시|시각|예정)|시작|OnCalendar|start(?:s|ed)?\s+at|run\s+at)",
    re.IGNORECASE,
)
# Each pattern yields one number (or a "1/6/12/24" list) in group 1 and is
# scaled by its unit. "T+5m" is deliberately absent: a minutes offset never
# outlasts the hour offsets it accompanies.
_HOUR = dt.timedelta(hours=1)
_DAY = dt.timedelta(days=1)
OFFSET_PATTERNS: tuple[tuple[re.Pattern[str], dt.timedelta], ...] = (
    (
        re.compile(
            r"(?<![A-Za-z])T\s*\+\s*(\d+(?:\s*h)?(?:\s*[/,·]\s*\d+(?:\s*h)?)*)\s*h(?![A-Za-z])",
            re.IGNORECASE,
        ),
        _HOUR,
    ),
    (re.compile(r"(?<![A-Za-z])T\s*\+\s*(\d+)\s*d(?![A-Za-z])", re.IGNORECASE), _DAY),
    (re.compile(r"(?<![\w+])\+\s*(\d+)\s*h(?![A-Za-z])", re.IGNORECASE), _HOUR),
    (re.compile(r"(?<![\d.])(\d+)\s*시간\s*(?:후|뒤|관측)"), _HOUR),
    (re.compile(r"(?:실행|시작)\s*후\s*(\d+)\s*시간"), _HOUR),
    (re.compile(r"(?<![\d.])(\d+)\s*h\s*관측", re.IGNORECASE), _HOUR),
    # "9일 후" right after "10월" is a date, not an offset.
    (re.compile(r"(?<![\d.월])(?<!월\s)(\d+)\s*일\s*(?:후|뒤|관측)"), _DAY),
    (re.compile(r"(?:실행|시작)\s*후\s*(\d+)\s*일"), _DAY),
)
# The inferred hit's own weak_reason when nothing more specific applies.
ANCHOR_OFFSET = "anchor-offset"

ISSUE_REFERENCE = re.compile(r"(?:#|/(?:issues|pull)/)(\d{1,7})\b")

# Inline code is quotation, not a booking. ccc-node#1873 reproduced another
# issue's deadline row as `| 배포 검증 종료 | **2026-09-12 06:00 KST** |` and
# was reported at high confidence for it. The keyword test therefore ignores
# code spans; the dates are still read from the full paragraph so a real
# booking that formats only its timestamp as code keeps its deadline.
INLINE_CODE = re.compile(r"`[^`\n]*`")

# An issue-claim comment (DOC-3508) carries the claimant's PR ETA, not a timed
# test's end. ccc-node#2109's claim read "예상 PR: 2026-10-05 09:30 KST. 실제
# 활성화 후 7일 창의 시작·종료 시각과 지표를 별도 기록합니다." — a deadline
# keyword and a date in one paragraph, unrelated to each other — and was
# reported at high confidence (#2150). Demote, keep.
CLAIM_MARKER = re.compile(r"<!--\s*issue-claim:v1\b")
CLAIM_ETA = re.compile(r"(예상\s*PR|PR\s*ETA)\s*\**\s*[:：]", re.IGNORECASE)

# A result report that states its own verdict on a line of its own. The
# heading checks (VERDICT, EARLY_VERDICT) only read the top of a comment, and
# ccc-node#2083's report put "판정: 통과(정적 근거)" at the end, under a heading
# that also stated the test end three minutes after posting — so the report
# booked itself and its own verdict could never follow the booking (#2150).
# The verdict word must end the label ("판정: 합격 기준은 …" is a booking).
# The English closing template of future-ending-test-issue-tracking writes
# "**Verdict**: FAIL" (ccc-node#2130, judged 23 min after its deadline and
# still reported expired-unjudged), so "Verdict" is a label too, and
# INCOMPLETE is a verdict — the observation was closed with a decision.
SELF_VERDICT_LINE = re.compile(
    r"^[\s>*_-]*(?:판정|verdict)\s*\**\s*[:：]\s*\**\s*(통과|합격|불합격|PASS|FAIL|INCOMPLETE)\**"
    r"(?=\s*(?:[(（.,·—–-]|$))",
    re.IGNORECASE | re.MULTILINE,
)


# --- followup mode (#2324 (2)) ------------------------------------------------
#
# A verdict ends the expired-mode question, but verdicts routinely promise more:
# a re-measure, a re-canary, a conditional pass to re-check, a post-rollback
# look. The 2026-10-15 deadline audit found 21 issues whose verdict promised a
# follow-up that never happened, 12 of them already closed. Only promises with
# an absolute datetime are scanned here; the KST/UTC handling and the
# false-positive guards are the same ones the deadline scan uses.
FOLLOWUP_KIND = "followup-overdue"
FOLLOWUP_KEYWORD = re.compile(
    r"(후속|다음\s*확인|재측정|재확인|재점검|재카나리"
    r"|(?<![\w./-])next\s+(?:action|check)s?(?![\w./-])"
    r"|(?<![\w./-])follow[\s-]?ups?(?![\w./-]))",
    re.IGNORECASE,
)
# The promise is written as a label — "다음 확인: …", "- **후속 재측정**: …",
# "| Next action | …", "Follow-up — …", "다음 확인 2026-10-14 09:00" — rather
# than mentioned in passing ("후속 PR에서 …"). Plain "-" is not a separator
# (it is inside every date), and the gap before the separator may not hold a
# digit, so the colon of a clock time ("09:00") never passes for one.
FOLLOWUP_LABEL = re.compile(
    r"^[\s>*_#|-]*(?:\d+[.)]\s*)?\**\s*" + FOLLOWUP_KEYWORD.pattern
    + r"(?:[^:：|\n\d]{0,20}?\**\s*(?:[:：|]|\s[—–]\s)|\**\s+(?=20\d{2}[-./년]))",
    re.IGNORECASE,
)
# Demotion reasons specific to follow-up promises.
FOLLOWUP_MENTION = "followup-mention"
VERDICT_HEADING_ONLY = "verdict-heading-only"


@dataclass(frozen=True)
class Hit:
    """One deadline candidate found in an issue body or comment.

    ``weak_reason`` is set when the paragraph matches a known false-positive
    shape. Such hits are *demoted, not dropped*: a missed finding stayed
    silent for nine days in the sweep that motivated this scanner, while a
    false positive costs an operator about thirty seconds. Recall wins.
    """

    deadline: dt.datetime
    paragraph: str
    source_url: str
    posted_at: dt.datetime | None
    had_explicit_time: bool
    weak_reason: str | None = None

    @property
    def confidence(self) -> str:
        return "low" if self.weak_reason else "high"


@dataclass
class Finding:
    """An issue that needs an operator's attention."""

    repo: str
    number: int
    title: str
    url: str
    kind: str
    deadline: dt.datetime | None = None
    days_overdue: int | None = None
    paragraph: str = ""
    source_url: str = ""
    comments_after: int = 0
    last_comment_at: dt.datetime | None = None
    confidence: str = "high"
    weak_reason: str | None = None
    dropped: list[str] = field(default_factory=list)
    # Only followup findings carry it (closed issues are in scope there); the
    # other kinds keep their JSON shape unchanged.
    issue_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "repo": self.repo,
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "kind": self.kind,
            "confidence": self.confidence,
            "weak_reason": self.weak_reason,
            "deadline_kst": self.deadline.strftime("%Y-%m-%d %H:%M") if self.deadline else None,
            "days_overdue": self.days_overdue,
            "paragraph": self.paragraph,
            "source_url": self.source_url,
            "comments_after": self.comments_after,
            "last_comment_kst": (
                self.last_comment_at.strftime("%Y-%m-%d %H:%M") if self.last_comment_at else None
            ),
            "dropped_candidates": self.dropped,
        }
        if self.kind == FOLLOWUP_KIND:
            data["issue_state"] = self.issue_state
        return data


def parse_github_timestamp(raw: str) -> dt.datetime:
    """Convert a GitHub ISO-8601 Z timestamp into naive KST.

    Everything downstream compares against KST wall-clock, matching how the
    owner rule is written, so the conversion happens once here.
    """
    stamp = dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(KST).replace(tzinfo=None)


def _iter_paragraphs(text: str) -> Iterator[str]:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            yield stripped


def _dates_in(paragraph: str) -> list[tuple[dt.datetime, bool]]:
    """Return (datetime, had_explicit_time) for every date in a paragraph.

    A bare date means the window closes at end of day, so it gets 23:59 —
    being generous here avoids flagging something as overdue on its last day.
    """
    found: list[tuple[dt.datetime, bool]] = []
    for match in DEADLINE_DATE.finditer(paragraph):
        year, month, day = (int(match.group(i)) for i in (1, 2, 3))
        explicit = match.group(4) is not None
        hour = int(match.group(4)) if explicit else 23
        minute = int(match.group(5)) if explicit else 59
        try:
            stamp = dt.datetime(year, month, day, hour, minute)
        except ValueError:
            continue  # 2026-13-45 and friends
        if match.group(6) or match.group(7):
            stamp += UTC_TO_KST  # everything downstream is naive KST
        found.append((stamp, explicit))
    return found


def _referenced_issues(paragraph: str) -> set[int]:
    return {int(number) for number in ISSUE_REFERENCE.findall(paragraph)}


def classify_false_positive(
    paragraph: str, issue_number: int, comment_header: str = ""
) -> str | None:
    """Name the false-positive shape this paragraph matches, if any.

    The 2026-09-21 sweep produced 14 raw hits for 8 real findings. Every one
    of the six false positives was one of these two shapes, so both filters
    are grounded in measured data rather than guessed:

    ``cross-reference``
        A roadmap tracking table or a child issue quoting *another* issue's
        deadline (ccc-node#1528, #1823, #1824).

    ``already-settled``
        The paragraph — or the comment it sits in — is itself the completion
        report (ccc-node#1648, a2a-nexus#2065, family-messenger#92). The
        heading carries the signal more often than the deadline line does,
        which is why ``comment_header`` is checked too.
    """
    referenced = _referenced_issues(paragraph)
    if referenced and issue_number not in referenced:
        return "cross-reference"
    if SETTLED.search(paragraph):
        return "already-settled"
    if comment_header and SETTLED.search(comment_header):
        return "already-settled-comment"
    return None


def _anchor_in(paragraph: str) -> dt.datetime | None:
    """The absolute start time a paragraph schedules, if it schedules one.

    The first datetime at or after the anchor keyword wins ("실행 예약: …
    OnCalendar=2026-10-09 22:00:00 KST"); a bare date is not an anchor.
    """
    keyword = ANCHOR_KEYWORD.search(paragraph)
    if keyword is None:
        return None
    for start in (keyword.start(), 0):
        for match in DEADLINE_DATE.finditer(paragraph, start):
            dates = _dates_in(match.group(0))
            if dates and dates[0][1]:
                return dates[0][0]
    return None


def _largest_offset(paragraph: str) -> dt.timedelta | None:
    """The longest relative offset in a paragraph ("T+1/6/12/24h" → 24h)."""
    largest: dt.timedelta | None = None
    for pattern, unit in OFFSET_PATTERNS:
        for match in pattern.finditer(paragraph):
            for number in re.findall(r"\d+", match.group(1)):
                offset = unit * int(number)
                if offset and (largest is None or offset > largest):
                    largest = offset
    return largest


def _infer_anchor_offset(
    text: str,
) -> tuple[dt.datetime, dt.datetime, dt.timedelta, str, str] | None:
    """Infer anchor + largest offset within one comment (#2252).

    Each offset pairs with the nearest anchor stated at or before it — the
    offsets in a plan hang off the step just scheduled, and a2a-nexus#1504's
    booking also carried an unrelated earlier ``OnCalendar`` (the reboot-hold
    expiry) that must not be the anchor. An offset with no anchor above it
    falls back to the first anchor below. Returns (deadline, anchor, offset,
    anchor paragraph, offset paragraph) for the latest inferred deadline.
    """
    anchors: list[tuple[int, dt.datetime, str]] = []
    offsets: list[tuple[int, dt.timedelta, str]] = []
    for index, paragraph in enumerate(_iter_paragraphs(text)):
        anchor = _anchor_in(paragraph)
        if anchor is not None:
            anchors.append((index, anchor, paragraph))
        offset = _largest_offset(paragraph)
        if offset is not None:
            offsets.append((index, offset, paragraph))
    if not anchors or not offsets:
        return None
    best: tuple[dt.datetime, dt.datetime, dt.timedelta, str, str] | None = None
    for index, offset, offset_paragraph in offsets:
        above = [item for item in anchors if item[0] <= index]
        _, anchor, anchor_paragraph = above[-1] if above else anchors[0]
        candidate = (anchor + offset, anchor, offset, anchor_paragraph, offset_paragraph)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best


def _format_offset(offset: dt.timedelta) -> str:
    hours = int(offset.total_seconds() // 3600)
    return f"{hours // 24}d" if hours % 24 == 0 and hours >= 48 else f"{hours}h"


def collect_hits(issue: dict[str, Any]) -> tuple[list[Hit], list[str]]:
    """Extract deadline candidates, separating keepers from filtered ones."""
    number = int(issue.get("number", 0))
    url = issue.get("url", "")
    sources: list[tuple[str, str, dt.datetime | None]] = [(issue.get("body") or "", url, None)]
    for comment in issue.get("comments") or []:
        posted = parse_github_timestamp(comment["createdAt"])
        sources.append((comment.get("body") or "", comment.get("url", url), posted))

    hits: list[Hit] = []
    dropped: list[str] = []
    for text, source_url, posted in sources:
        # The issue body is not a completion report no matter what it says,
        # so only comments contribute a header signal.
        header = text[:SETTLED_HEADER_CHARS] if posted is not None else ""
        is_claim = posted is not None and bool(CLAIM_MARKER.search(text))
        explicit_hits = len(hits)
        for paragraph in _iter_paragraphs(text):
            if not DEADLINE_KEYWORD.search(INLINE_CODE.sub("", paragraph)):
                continue
            dates = _dates_in(paragraph)
            if not dates:
                continue
            reason = classify_false_positive(paragraph, number, header)
            if reason is None and (is_claim or CLAIM_ETA.search(paragraph)):
                reason = "claim-eta"
            # A paragraph may render the same instant twice (KST and UTC);
            # _dates_in already normalised marked UTC, so both agree. Keep the
            # latest, which is also the KST rendering of an unmarked UTC pair.
            deadline, explicit = max(dates, key=lambda item: item[0])
            # A booking always precedes its deadline. A comment stating a time
            # that had already passed when it was posted is a record of what
            # happened — "테스트 종료: 07:36:11 UTC" inside the result report
            # posted eight minutes later (a2a-nexus#2256) — or a nag citing a
            # missed deadline whose original booking is scored on its own.
            # Demote, keep.
            if reason is None and posted is not None and deadline <= posted:
                reason = "past-at-posting"
            # The owner rule asks for an absolute KST *datetime*. A bare date
            # next to a deadline keyword is more often a citation than a
            # booking — "오너 규칙(2026-09-11)은 … 종료 일시를 …" got the
            # scanner's own tracking issue (ccc-node#1870) reported at high
            # confidence on its first production run (#1873). Demote, keep.
            if reason is None and not explicit:
                reason = "date-only"
            if reason:
                dropped.append(f"{reason}: {paragraph[:90]}")
            hits.append(
                Hit(
                    deadline=deadline,
                    paragraph=paragraph,
                    source_url=source_url,
                    posted_at=posted,
                    had_explicit_time=explicit,
                    weak_reason=reason,
                )
            )
        # An explicit end in this comment is the booking; an anchor + offset
        # beside it would only double-detect the same window (#2252).
        if len(hits) == explicit_hits:
            inferred = _infer_anchor_offset(text)
            if inferred is not None:
                hit = _inferred_hit(inferred, number, header, source_url, posted)
                dropped.append(f"{hit.weak_reason}: {hit.paragraph[:90]}")
                hits.append(hit)
    return hits, dropped


def _inferred_hit(
    inferred: tuple[dt.datetime, dt.datetime, dt.timedelta, str, str],
    issue_number: int,
    header: str,
    source_url: str,
    posted: dt.datetime | None,
) -> Hit:
    """Turn an anchor + offset inference into a (always low-confidence) Hit.

    A comment whose anchor still lay ahead when it was posted is scheduling
    something, whatever else its heading reports — a2a-nexus#1504's booking
    was titled "사전 조회 합격 + 실행 예약". One whose anchor had already
    passed and that reads like a report is restating the plan it judged, and
    must not become a booking that moves the early-verdict window (#2149).
    """
    deadline, anchor, offset, anchor_paragraph, offset_paragraph = inferred
    joined = anchor_paragraph
    if offset_paragraph != anchor_paragraph:
        joined += " " + offset_paragraph
    reason = classify_false_positive(joined, issue_number, header)
    anchor_passed = posted is not None and anchor <= posted
    if reason in ("already-settled", "already-settled-comment") and not anchor_passed:
        reason = None
    if reason is None and posted is not None and deadline <= posted:
        reason = "past-at-posting"
    summary = f"[추론] {anchor:%Y-%m-%d %H:%M} + {_format_offset(offset)} ← {anchor_paragraph[:80]}"
    if offset_paragraph != anchor_paragraph:
        summary += f" / {offset_paragraph[:80]}"
    return Hit(
        deadline=deadline,
        paragraph=summary,
        source_url=source_url,
        posted_at=posted,
        had_explicit_time=True,
        weak_reason=reason or ANCHOR_OFFSET,
    )


def _author_login(item: dict[str, Any]) -> str | None:
    """The GitHub login of an issue or comment, or None when the dump has none."""
    author = item.get("author")
    if isinstance(author, dict):
        author = author.get("login")
    return str(author) if author else None


def is_fleet_author(login: str | None) -> bool:
    """Whether a login is a fleet account. Unknown (no author data) counts as fleet:
    a JSONL dump without authors must not demote everything."""
    if not login:
        return True
    name = login.strip().lower()
    if name.startswith("app/"):
        name = name[len("app/"):]
    if name.endswith("[bot]"):
        name = name[: -len("[bot]")]
    return name in FLEET_AUTHORS


def _iter_paragraphs_fenced(text: str) -> Iterator[tuple[str, bool]]:
    """Like _iter_paragraphs, plus whether the line sits inside a ``` / ~~~ fence.

    A fenced block is quotation: family-messenger#167's nag reproduced the
    acceptance criterion "(며칠 관측 필요 — 즉시 판정 불가)" inside one.
    """
    fenced = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if stripped:
            yield stripped, fenced


def _is_quoted(paragraph: str, start: int, end: int) -> bool:
    for pattern in (QUOTE_SPAN, INLINE_CODE):
        for span in pattern.finditer(paragraph):
            if span.start() < start and end < span.end():
                return True
    return False


def _wording_reason(paragraph: str, match: re.Match[str]) -> str | None:
    """Operational / planning / process wording for one unquoted relative term."""
    if OPERATIONAL_TERM.match(match.group(0)):
        return REL_OPERATIONAL
    before = paragraph[: match.start()].rstrip(" *_")
    after = paragraph[match.end():].lstrip(" *_")
    if before.endswith(("(", "（")) and after.startswith((")", "）")):
        return REL_PLANNING
    if PLANNING_NOUN.search(paragraph[max(0, match.start() - PLANNING_WINDOW_CHARS): match.start()]):
        return REL_PLANNING
    if PROCESS_SUFFIX.match(paragraph, match.end()):
        return REL_PROCESS
    if PROCESS_NOUN.search(paragraph[max(0, match.start() - PLANNING_WINDOW_CHARS): match.start()]):
        return REL_PROCESS
    return None


def classify_relative_paragraph(
    paragraph: str,
    *,
    author: str | None = None,
    in_code_block: bool = False,
    verdict_after: bool = False,
) -> str | None:
    """Name the false-positive shape of one relative-only paragraph, if any (#2544).

    Checked in order; the first that applies is the reason code:

    ``external-author``
        Written by a non-fleet account (``FLEET_AUTHORS``); outside
        conversation cannot book a fleet timed test (esp32-iphone-agent#77).
    ``quoted-term``
        Every relative term is quoted ("…", '…', inline code, or a fenced
        block): rule text or a citation, not a booking (family-messenger#179).
    ``checkbox-verdict-recorded``
        A task-list item, and a verdict comment (strict, as in followup mode)
        was posted after it (family-messenger#167, PASS recorded).

    The remaining three judge the wording, so they never apply when the
    paragraph names an observation (``OBSERVATION_WORD``), and every unquoted
    term must be explained by one of them:

    ``operational-wording``  "당분간" — a standing arrangement.
    ``planning-estimate``    an effort estimate: a planning noun just before
                             the term ("스파이크(1~2주)") or the term alone in
                             brackets after a plan item.
    ``process-duration``     how long an outside procedure takes ("수일 소요").
    """
    if author is not None and not is_fleet_author(author):
        return REL_EXTERNAL_AUTHOR
    matches = list(RELATIVE_DURATION.finditer(paragraph))
    if not matches:
        return None
    if in_code_block:
        return REL_QUOTED_TERM
    unquoted = [m for m in matches if not _is_quoted(paragraph, m.start(), m.end())]
    if not unquoted:
        return REL_QUOTED_TERM
    if verdict_after and CHECKBOX_LINE.match(paragraph):
        return REL_CHECKBOX_JUDGED
    if OBSERVATION_WORD.search(paragraph):
        return None
    reasons = [_wording_reason(paragraph, m) for m in unquoted]
    if all(reasons):
        return reasons[0]
    return None


def relative_offenders(issue: dict[str, Any]) -> list[tuple[str, str | None]]:
    """(paragraph, weak_reason) for every relative-only paragraph, in order."""
    sources: list[tuple[str, str | None, dt.datetime | None]] = [
        (issue.get("body") or "", _author_login(issue), None)
    ]
    for comment in issue.get("comments") or []:
        sources.append(
            (comment.get("body") or "", _author_login(comment),
             parse_github_timestamp(comment["createdAt"]))
        )
    verdict_times = [
        posted for text, _, posted in sources
        if posted is not None and _verdict_strength(text) == "strict"
    ]
    offenders: list[tuple[str, str | None]] = []
    for text, author, posted in sources:
        verdict_after = any(posted is None or when > posted for when in verdict_times)
        for paragraph, fenced in _iter_paragraphs_fenced(text):
            if not RELATIVE_DURATION.search(paragraph):
                continue
            if _dates_in(paragraph):
                continue  # an absolute datetime is right there; rule satisfied
            reason = classify_relative_paragraph(
                paragraph, author=author, in_code_block=fenced, verdict_after=verdict_after
            )
            offenders.append((paragraph[:160], reason))
    return offenders


def find_relative_only(issue: dict[str, Any]) -> list[str]:
    """Paragraphs promising a timed test with no absolute datetime at all.

    family-messenger#104 said "며칠 관찰 후 완전 삭제" and went unjudged for
    four days: with no absolute end time, no schedule could exist. Demoted
    shapes are included; ``relative_offenders`` carries their reasons.
    """
    return [paragraph for paragraph, _ in relative_offenders(issue)]


def _is_judged(
    latest: Hit,
    hits: Sequence[Hit],
    comments: Sequence[dict[str, Any]],
    comment_times: Sequence[dt.datetime],
) -> bool:
    """Whether a verdict-shaped comment settles the window ending at ``latest``."""
    # The early-verdict window opens at the *last* comment that stated this
    # deadline, so a re-affirmed booking resets it. The issue body (posted_at
    # None) predates every comment. Only hits that can be a booking count:
    # a verdict or completion report that restates the deadline it beat
    # ("예약 종료 시각(10-08 09:00 KST)보다 앞당겨 끝냈다", a2a-nexus#2315)
    # is a record of the booking, not a new one — counting it moved
    # booked_at onto the verdict itself and the verdict was ignored (#2149).
    booked_at = max(
        (
            hit.posted_at
            for hit in hits
            if hit.deadline == latest.deadline and hit.posted_at and hit.weak_reason not in NOT_A_BOOKING
        ),
        default=None,
    )
    for comment, posted in zip(comments, comment_times):
        body = comment.get("body") or ""
        head = body[:VERDICT_HEADER_CHARS]
        self_verdict = bool(SELF_VERDICT_LINE.search(body))
        if posted > latest.deadline:
            if VERDICT.search(head) or self_verdict:
                return True
        elif (booked_at is None or posted > booked_at) and EARLY_VERDICT.search(_first_line(body)):
            return True
        elif self_verdict and (booked_at is None or posted >= booked_at):
            # A report that booked and judged in one comment (ccc-node#2083).
            return True
    return False


def _verdict_strength(body: str) -> str | None:
    """How firmly a comment reads as a verdict: ``strict``, ``heading`` or None.

    ``strict`` is the early-verdict test (a conclusion on the first line) or a
    self-verdict label line. ``heading`` is the looser expired-mode window,
    which also accepts "판정" anywhere in the first 200 characters — enough to
    open the follow-up window, not enough to trust what it promises.
    """
    if EARLY_VERDICT.search(_first_line(body)) or SELF_VERDICT_LINE.search(body):
        return "strict"
    if VERDICT.search(body[:VERDICT_HEADER_CHARS]):
        return "heading"
    return None


def collect_followups(issue: dict[str, Any]) -> tuple[list[Hit], list[str]]:
    """Follow-up promises with a datetime, in the first verdict and after it.

    The issue body is never a verdict, and comments before the first verdict
    are bookings, which expired mode already judges. Demotion mirrors
    collect_hits: cross-reference, already-settled, claim-eta,
    past-at-posting and date-only, plus ``followup-mention`` (keyword not
    written as a label) and ``verdict-heading-only`` (no strict verdict yet).
    """
    number = int(issue.get("number", 0))
    url = issue.get("url", "")
    hits: list[Hit] = []
    dropped: list[str] = []
    verdict: str | None = None
    for comment in issue.get("comments") or []:
        body = comment.get("body") or ""
        posted = parse_github_timestamp(comment["createdAt"])
        strength = _verdict_strength(body)
        if strength == "strict" or (strength and verdict is None):
            verdict = strength
        if verdict is None:
            continue
        is_claim = bool(CLAIM_MARKER.search(body))
        for paragraph in _iter_paragraphs(body):
            bare = INLINE_CODE.sub("", paragraph)
            if not FOLLOWUP_KEYWORD.search(bare):
                continue
            dates = _dates_in(paragraph)
            if not dates:
                continue
            due, explicit = max(dates, key=lambda item: item[0])
            # No comment-header check: the verdict comment's heading always
            # reads as settled, which is the point of reading it here.
            reason = classify_false_positive(paragraph, number)
            if reason is None and (is_claim or CLAIM_ETA.search(paragraph)):
                reason = "claim-eta"
            if reason is None and due <= posted:
                reason = "past-at-posting"
            if reason is None and not explicit:
                reason = "date-only"
            if reason is None and not FOLLOWUP_LABEL.search(bare):
                reason = FOLLOWUP_MENTION
            if reason is None and verdict != "strict":
                reason = VERDICT_HEADING_ONLY
            if reason:
                dropped.append(f"{reason}: {paragraph[:90]}")
            hits.append(
                Hit(
                    deadline=due,
                    paragraph=paragraph,
                    source_url=comment.get("url", url),
                    posted_at=posted,
                    had_explicit_time=explicit,
                    weak_reason=reason,
                )
            )
    return hits, dropped


def _judge_followup(
    issue: dict[str, Any],
    now: dt.datetime,
    comment_times: Sequence[dt.datetime],
) -> Finding | None:
    """Report a promised follow-up whose time passed with nothing after it.

    Any comment after the promised time counts as evidence of action — the
    scanner cannot tell a result from a nag, and over-reporting a closed
    question is the worse failure for a low-trust first version. A promise
    is also cleared when a later comment re-schedules it to a later time.
    """
    hits, dropped = collect_followups(issue)
    last_comment = max(comment_times, default=None)
    overdue: list[Hit] = []
    for hit in hits:
        if hit.deadline >= now:
            continue  # not due yet
        if last_comment is not None and last_comment > hit.deadline:
            continue  # somebody wrote something after the promised time
        rescheduled = any(
            other.posted_at is not None
            and hit.posted_at is not None
            and other.posted_at > hit.posted_at
            and other.deadline > hit.deadline
            and other.weak_reason not in NOT_A_BOOKING
            for other in hits
        )
        if rescheduled:
            continue
        overdue.append(hit)
    if not overdue:
        return None
    strong = [hit for hit in overdue if hit.confidence == "high"]
    hit = max(strong or overdue, key=lambda item: item.deadline)
    state = issue.get("state")
    repo = str(issue.get("repo", "")) or "?"
    return Finding(
        repo=repo,
        number=int(issue.get("number", 0)),
        title=str(issue.get("title", "")),
        url=str(issue.get("url", "")),
        kind=FOLLOWUP_KIND,
        deadline=hit.deadline,
        days_overdue=(now - hit.deadline).days,
        paragraph=hit.paragraph[:200],
        source_url=hit.source_url,
        comments_after=0,
        last_comment_at=last_comment,
        confidence=hit.confidence,
        weak_reason=hit.weak_reason,
        dropped=dropped,
        issue_state=str(state).lower() if state else None,
    )


def judge_issue(issue: dict[str, Any], now: dt.datetime, mode: str) -> Finding | None:
    """Decide whether one issue needs attention under the given mode."""
    repo = str(issue.get("repo", "")) or "?"
    number = int(issue.get("number", 0))
    title = str(issue.get("title", ""))
    url = str(issue.get("url", ""))
    comments = issue.get("comments") or []
    comment_times = [parse_github_timestamp(c["createdAt"]) for c in comments]

    if mode == "followup":
        return _judge_followup(issue, now, comment_times)

    if mode == "relative":
        offenders = relative_offenders(issue)
        if not offenders:
            return None
        # One undemoted paragraph keeps the issue high; otherwise report the
        # first demoted one at low confidence (#2544).
        undemoted = [offender for offender in offenders if offender[1] is None]
        evidence, demoted_as = undemoted[0] if undemoted else offenders[0]
        return Finding(
            repo=repo,
            number=number,
            title=title,
            url=url,
            kind=RELATIVE_KIND,
            paragraph=evidence,
            last_comment_at=max(comment_times, default=None),
            confidence="low" if demoted_as else "high",
            weak_reason=demoted_as,
            dropped=[f"{why}: {text[:90]}" for text, why in offenders if why],
        )

    hits, dropped = collect_hits(issue)
    if not hits:
        return None
    # Prefer a high-confidence deadline; fall back to a demoted one so an
    # issue whose every mention looks like a report is still surfaced, just
    # flagged as lower confidence.
    strong = [hit for hit in hits if hit.confidence == "high"]
    latest = max(strong or hits, key=lambda hit: hit.deadline)
    candidates = [latest]
    # An anchor + offset inference is a separate window, never a restatement
    # of an explicit end (the same comment never yields both). When it runs
    # past the strongest explicit deadline, judge it on its own too:
    # a2a-nexus#1504's earlier "보류 종료 일시: 2026-10-10 18:00 KST" was
    # cleared by the Phase 7 verdicts around it, and preferring it alone hid
    # the Phase 7-B window that closed four hours later (#2252).
    inferred = [
        hit for hit in hits if hit.weak_reason == ANCHOR_OFFSET and hit.deadline > latest.deadline
    ]
    if strong and inferred:
        candidates.append(max(inferred, key=lambda hit: hit.deadline))
    for candidate in candidates:
        if candidate.deadline >= now:
            continue  # still running; not our business
        if not _is_judged(candidate, hits, comments, comment_times):
            latest = candidate
            break
    else:
        return None

    after = [t for t in comment_times if t > latest.deadline]
    return Finding(
        repo=repo,
        number=number,
        title=title,
        url=url,
        kind="expired-unjudged",
        deadline=latest.deadline,
        days_overdue=(now - latest.deadline).days,
        paragraph=latest.paragraph[:200],
        source_url=latest.source_url,
        comments_after=len(after),
        last_comment_at=max(comment_times, default=None),
        confidence=latest.confidence,
        weak_reason=latest.weak_reason,
        dropped=dropped,
    )


def scan(
    issues: Iterable[dict[str, Any]],
    now: dt.datetime,
    mode: str,
    min_confidence: str = "low",
) -> list[Finding]:
    findings = [f for f in (judge_issue(issue, now, mode) for issue in issues) if f is not None]
    if min_confidence == "high":
        findings = [f for f in findings if f.confidence == "high"]
    findings.sort(key=lambda f: (f.deadline or now))
    return findings


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                issues.append(json.loads(line))
    return issues


class NotConfigured(RuntimeError):
    """The repo list exists in form but selects nothing to scan.

    Kept distinct from "scanned and found nothing" on purpose. This scanner
    exists because a failure that looks like success stayed invisible for nine
    days; an unconfigured scanner reporting a clean "0건" would be exactly that
    bug wearing this tool's face. Callers map it to its own exit code.
    """


REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def load_repos_file(path: Path) -> list[str]:
    """Read an operator-owned ``owner/name`` per line allowlist.

    ``#`` comments and blank lines are ignored, and any trailing fields on a
    line are tolerated so the format can grow without breaking older installs.
    A malformed entry is a hard error rather than a skip — silently scanning a
    shorter list than the operator wrote is the failure mode this tool exists
    to catch.
    """
    if not path.exists():
        raise NotConfigured(f"repo list not found: {path}")
    repos: list[str] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = line.split()[0]
        if not REPO_NAME.match(name):
            raise ValueError(f"{path}:{lineno}: not an owner/name repo: {name!r}")
        repos.append(name)
    if not repos:
        raise NotConfigured(f"repo list is empty: {path}")
    return repos


def fetch_via_gh(
    repo: str, limit: int, closed_since: dt.date | None = None
) -> list[dict[str, Any]]:
    """Pull open issues with comments through the gh CLI.

    With ``closed_since`` (followup mode's ``--include-closed-days``), issues
    closed on or after that date are fetched too, by a second ``gh`` query
    using GitHub search (``closed:>=YYYY-MM-DD``, a UTC date). Without it the
    query is exactly the open-only one the other modes always ran.
    """
    # author: relative mode demotes non-fleet wording (#2544); comments
    # already carry their author.
    fields = "number,title,url,body,comments,createdAt,updatedAt,author"
    queries: list[tuple[str, list[str]]] = [("open", [])]
    if closed_since is not None:
        fields += ",state,closedAt"
        queries.append(("closed", ["--search", f"closed:>={closed_since:%Y-%m-%d}"]))
    issues: list[dict[str, Any]] = []
    seen: set[int] = set()
    for state, extra in queries:
        proc = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", state,
             "--limit", str(limit), *extra, "--json", fields],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            which = "" if state == "open" else f" ({state})"
            raise RuntimeError(
                f"gh issue list failed for {repo}{which}: {proc.stderr.strip()[:400]}"
            )
        for issue in json.loads(proc.stdout or "[]"):
            number = issue.get("number")
            if number in seen:
                continue  # closed between the two queries
            seen.add(number)
            issue["repo"] = repo
            issues.append(issue)
    return issues


def format_report(findings: Sequence[Finding], now: dt.datetime, mode: str) -> str:
    if not findings:
        if mode == "followup":
            return f"[{now:%Y-%m-%d %H:%M} KST] {mode}: 해당 없음 — 판정 후 후속 기한 경과 0건"
        return f"[{now:%Y-%m-%d %H:%M} KST] {mode}: 해당 없음 — 기한 경과 미판정 0건"
    lines = [f"[{now:%Y-%m-%d %H:%M} KST] {mode}: {len(findings)}건"]
    for finding in findings:
        lines.append("")
        if finding.kind == FOLLOWUP_KIND and finding.deadline is not None:
            mark = "" if finding.confidence == "high" else f" | 신뢰도 낮음({finding.weak_reason})"
            closed = " | 닫힌 이슈" if finding.issue_state == "closed" else ""
            lines.append(
                f"### {finding.repo}#{finding.number} | 후속 기한 {finding.deadline:%Y-%m-%d %H:%M}"
                f" | 경과 {finding.days_overdue}일{closed}{mark}"
            )
            lines.append(f"    {finding.title[:100]}")
            lines.append(f"    근거: {finding.paragraph[:150]}")
            if finding.last_comment_at:
                lines.append(f"    마지막 코멘트: {finding.last_comment_at:%Y-%m-%d %H:%M} KST")
            lines.append("    후속 기한 후 코멘트 0건")
            lines.append(f"    {finding.url}")
            continue
        if finding.deadline is not None:
            mark = "" if finding.confidence == "high" else f" | 신뢰도 낮음({finding.weak_reason})"
            lines.append(
                f"### {finding.repo}#{finding.number} | 종료 {finding.deadline:%Y-%m-%d %H:%M}"
                f" | 경과 {finding.days_overdue}일{mark}"
            )
        else:
            mark = "" if finding.confidence == "high" else f" | 신뢰도 낮음({finding.weak_reason})"
            lines.append(f"### {finding.repo}#{finding.number} | 절대시각 없음{mark}")
        lines.append(f"    {finding.title[:100]}")
        lines.append(f"    근거: {finding.paragraph[:150]}")
        if finding.last_comment_at:
            lines.append(f"    마지막 코멘트: {finding.last_comment_at:%Y-%m-%d %H:%M} KST")
        lines.append(f"    기한 후 코멘트 {finding.comments_after}건 / 판정성 0건")
        lines.append(f"    {finding.url}")
    return "\n".join(lines)


# --- owner notification ------------------------------------------------------
#
# Reuses the owner-only bridge spool that agent-cron (write_owner_spool),
# ccc-self-update.sh and ccc-pr-status-poll.sh already write: one small JSON
# file {ts, event, node, text, dedup} per notice in
# ${CCC_PUSH_SPOOL:-${CCC_STATE_DIR:-~/.claude/state}/telegram-spool}. The bridge
# PushNotifier (opt-in via CCC_PUSH_ENABLED) delivers it to the owner chat and
# archives it; a record without chatId can only ever reach the owner. #1821
# asked that its failure alarms and this scanner share one channel instead of
# each inventing one, and this is that channel.

NOTIFY_ENV = "CCC_TIMED_TEST_SCAN_NOTIFY"
NOTIFY_LEVELS = ("off", "high", "low")
NOTIFY_EVENT = "TimedTestDeadlineScan"
NOTIFY_MAX_FINDINGS = 10
NOTIFY_TITLE_CHARS = 60
# Calendar days, not a 72h timedelta: the cron fires at 09:20 daily and a few
# seconds of start-up jitter must not push a reminder back by a whole day.
NOTIFY_REMIND_AFTER_DAYS = 3
NOTIFY_STATE_SCHEMA = "ccc.timed-test-deadline-scan.notify-state.v1"
_NOTIFY_STAMP = "%Y-%m-%dT%H:%M"

# Only a canonical GitHub issue/PR URL is copied into a notice; anything else
# is rebuilt from repo#number so a crafted "url" field cannot smuggle text.
_GITHUB_ITEM_URL = re.compile(
    r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(?:issues|pull)/\d{1,7}$"
)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


class NotifyError(RuntimeError):
    """Owner notification could not be produced safely; the scan is unaffected."""


def _state_dir() -> Path:
    claude_dir = os.environ.get("CCC_CLAUDE_DIR") or str(Path.home() / ".claude")
    return Path(os.environ.get("CCC_STATE_DIR") or Path(claude_dir) / "state").expanduser()


def push_spool_dir() -> Path:
    """Same resolution order as the shell spool writers (self-update, pr-status-poll)."""
    raw = os.environ.get("CCC_PUSH_SPOOL")
    return Path(raw).expanduser() if raw else _state_dir() / "telegram-spool"


def notify_state_path(mode: str) -> Path:
    return _state_dir() / f"timed-test-deadline-scan.notify-{mode}.json"


def resolve_notify_level(cli_value: str | None) -> str:
    """CLI flag wins, then the env var, then off.

    An unrecognised env value warns and stays off rather than guessing: the
    installed cron always passes an explicit ``--notify``, so the env var only
    matters for hand runs, where a surprise message is the worse failure.
    """
    if cli_value:
        return cli_value
    raw = (os.environ.get(NOTIFY_ENV) or "").strip().lower()
    if not raw:
        return "off"
    if raw not in NOTIFY_LEVELS:
        print(f"notify: ignoring {NOTIFY_ENV}={raw[:20]!r} (expected off|high|low)", file=sys.stderr)
        return "off"
    return raw


def notify_candidates(findings: Sequence[Finding], level: str) -> list[Finding]:
    """Findings worth waking the owner for at the given level.

    ``high`` is the installed default: low-confidence hits are the demoted
    false-positive shapes, kept in the log for recall but not worth a ping.
    """
    if level == "off":
        return []
    if level == "high":
        return [f for f in findings if f.confidence == "high"]
    return list(findings)


def finding_fingerprint(finding: Finding) -> str:
    """repo#number plus the deadline — a re-booked deadline counts as new."""
    anchor = finding.deadline.strftime(_NOTIFY_STAMP) if finding.deadline else finding.kind
    return f"{finding.repo}#{finding.number}@{anchor}"


def load_notify_state(path: Path) -> dict[str, Any]:
    """Previously notified fingerprints; unreadable state means "never notified".

    Failing toward a duplicate notice is deliberate: a lost state file costing
    one repeated message is cheap, a state glitch silencing a finding is not.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("fingerprints"), list):
        return {}
    return data


def decide_notification(
    current: Sequence[str], state: dict[str, Any], now: dt.datetime
) -> tuple[str | None, set[str]]:
    """Return (reason, new_fingerprints); reason None means stay quiet.

    ``new``      at least one fingerprint was not in the last notified set.
    ``reminder`` the set is unchanged (or shrank) and the last notice is
                 NOTIFY_REMIND_AFTER_DAYS or more calendar days old.
    """
    if not current:
        return None, set()
    previous = {str(fp) for fp in state.get("fingerprints", [])}
    new = set(current) - previous
    if new:
        return "new", new
    try:
        last = dt.datetime.strptime(str(state.get("notified_at")), _NOTIFY_STAMP)
    except ValueError:
        return "reminder", set()
    if (now.date() - last.date()).days >= NOTIFY_REMIND_AFTER_DAYS:
        return "reminder", set()
    return None, set()


def _load_canonical_redaction() -> Any:
    """Load bridge/utils/redaction.py from this checkout, as agent-cron does."""
    source = Path(__file__).resolve().parents[1] / "bridge" / "utils" / "redaction.py"
    spec = importlib.util.spec_from_file_location("_ccc_timed_test_scan_redaction", source)
    if spec is None or spec.loader is None or not source.is_file():
        raise NotifyError("canonical redaction unavailable; notice suppressed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean_title(title: str, redaction: Any) -> str:
    text = " ".join(_CONTROL_CHARS.sub(" ", title or "").split())
    text = redaction.redact_credentials(text)
    if len(text) > NOTIFY_TITLE_CHARS:
        text = text[: NOTIFY_TITLE_CHARS - 1].rstrip() + "…"
    return text or "(제목 없음)"


def _safe_url(finding: Finding) -> str:
    if _GITHUB_ITEM_URL.match(finding.url or ""):
        return finding.url
    if REPO_NAME.match(finding.repo or ""):
        return f"https://github.com/{finding.repo}/issues/{int(finding.number)}"
    return ""


def build_notify_text(
    findings: Sequence[Finding],
    new: set[str],
    reason: str,
    level: str,
    mode: str,
    redaction: Any,
) -> str:
    """Short Korean owner notice: count, then repo#number / deadline / overdue /
    title / URL per finding. No paragraph or comment excerpts ever — those are
    issue prose and may quote anything."""
    if mode == "followup":
        what = "판정 후 후속 기한 경과"
    else:
        what = "기한 경과 미판정" if mode == "expired" else "절대 종료시각 없음"
    head = f"⏰ 시한부 테스트 {what} {len(findings)}건"
    if reason == "new":
        head += f" (신규 {len(new)}건)"
    else:
        head += f" ({NOTIFY_REMIND_AFTER_DAYS}일+ 미해소 재알림)"
    lines = [head]
    # New findings first so the cap never hides what triggered the notice.
    ordered = sorted(findings, key=lambda f: finding_fingerprint(f) not in new)
    for finding in ordered[:NOTIFY_MAX_FINDINGS]:
        repo = finding.repo if REPO_NAME.match(finding.repo or "") else "?"
        mark = "🆕" if finding_fingerprint(finding) in new else "•"
        if finding.kind == FOLLOWUP_KIND and finding.deadline is not None:
            lines.append(
                f"{mark} {repo}#{int(finding.number)} · 후속 {finding.deadline:%Y-%m-%d %H:%M} KST"
                f" · 경과 {finding.days_overdue}일"
            )
        elif finding.deadline is not None:
            lines.append(
                f"{mark} {repo}#{int(finding.number)} · 종료 {finding.deadline:%Y-%m-%d %H:%M} KST"
                f" · 경과 {finding.days_overdue}일"
            )
        else:
            lines.append(f"{mark} {repo}#{int(finding.number)} · 절대시각 없음")
        lines.append(f"  {_clean_title(finding.title, redaction)}")
        url = _safe_url(finding)
        if url:
            lines.append(f"  {url}")
    if len(ordered) > NOTIFY_MAX_FINDINGS:
        lines.append(f"외 {len(ordered) - NOTIFY_MAX_FINDINGS}건")
    # Followup (#2324) and relative (#2544) modes run from their own
    # installers and cron logs.
    log_name = {
        "followup": "timed-followup-scan.cron.log",
        "relative": "timed-relative-scan.cron.log",
    }.get(mode, "timed-test-deadline-scan.cron.log")
    lines.append(f"신뢰도 {level} 이상 · 전체 목록: {log_name}")
    text = redaction.redact_credentials("\n".join(lines))
    if redaction.contains_credential(text):
        raise NotifyError("credential-shaped text survived redaction; notice suppressed")
    return text


def write_owner_spool(text: str, dedup: str, now: dt.datetime) -> Path:
    """Queue one owner-only notice in the bridge push spool (0600, atomic).

    Written through the canonical secure-fs helper: a private ``.tmp`` sibling
    renamed into place, so the PushNotifier's ``*.json`` poll never reads a
    half-written file (it archives unparsable files as malformed, which would
    silently drop the notice).
    """
    import ccc_secure_fs  # lazy: the scan itself must not depend on it

    spool = push_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    stamp = now.replace(tzinfo=KST).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    node = os.environ.get("CCC_NODE") or socket.gethostname().split(".")[0] or "node"
    payload = {
        "ts": stamp,
        "event": NOTIFY_EVENT,
        "node": node,
        "text": text,
        "dedup": f"{NOTIFY_EVENT}:{dedup}",
    }
    name = f"{stamp.replace(':', '-')}-{NOTIFY_EVENT}-{os.getpid()}-{secrets.token_hex(4)}.json"
    path = spool / name
    ccc_secure_fs.atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", mode=0o600
    )
    return path


def _save_notify_state(path: Path, fingerprints: Sequence[str], notified_at: str | None) -> None:
    import ccc_secure_fs

    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "schema": NOTIFY_STATE_SCHEMA,
        "fingerprints": sorted(fingerprints),
        "notified_at": notified_at,
    }
    ccc_secure_fs.atomic_write_text(path, json.dumps(state, ensure_ascii=False) + "\n", mode=0o600)


def notify_owner(
    findings: Sequence[Finding],
    now: dt.datetime,
    mode: str,
    level: str,
    skipped_repos: Iterable[str] = (),
) -> str:
    """Spool an owner notice when warranted; return a one-line outcome for the log.

    The dedup state is only advanced after the spool file exists, so a failed
    write is retried on the next run instead of being marked as sent.

    On a partial scan, fingerprints already in the state for a repo that could
    not be read are carried over: an unreadable repo says nothing about whether
    its findings were resolved, and dropping them would re-announce them as new
    once the repo is readable again (#2324).
    """
    candidates = notify_candidates(findings, level)
    current = [finding_fingerprint(f) for f in candidates]
    state_path = notify_state_path(mode)
    state = load_notify_state(state_path)
    skipped = set(skipped_repos)
    if skipped:
        seen = set(current)
        current += [
            str(fp)
            for fp in state.get("fingerprints", [])
            if str(fp).split("#", 1)[0] in skipped and str(fp) not in seen
        ]
    reason, new = decide_notification(current, state, now)
    if reason is None:
        previous = sorted(str(fp) for fp in state.get("fingerprints", []))
        if previous != sorted(current):
            # Shrunk (or cleared): remember the smaller set so a finding that
            # comes back later counts as new again. Keep the notice clock.
            _save_notify_state(state_path, current, state.get("notified_at"))
        if not current:
            return f"notify: nothing at level={level}"
        return f"notify: unchanged set of {len(current)}, last sent {state.get('notified_at')}"
    text = build_notify_text(candidates, new, reason, level, mode, _load_canonical_redaction())
    digest = hashlib.sha256("\n".join(sorted(current)).encode("utf-8")).hexdigest()[:12]
    path = write_owner_spool(text, f"{mode}:{reason}:{digest}:{now:%Y%m%d}", now)
    _save_notify_state(state_path, current, now.strftime(_NOTIFY_STAMP))
    return f"notify: spooled reason={reason} findings={len(current)} new={len(new)} file={path.name}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="JSONL dump of issues (one object per line)")
    source.add_argument("--repo", action="append", help="owner/name; repeatable")
    source.add_argument(
        "--repos-file",
        type=Path,
        help="operator-owned allowlist, one owner/name per line (# comments ok). "
        "A missing or empty file exits 3 (not configured), never a clean 0 findings.",
    )
    parser.add_argument(
        "--mode",
        choices=("expired", "relative", "followup"),
        default="expired",
        help="expired: deadline passed with no verdict. relative: no absolute datetime at all. "
        "followup (opt-in): a follow-up promised at/after a verdict is past due with no "
        "comment after it.",
    )
    parser.add_argument(
        "--include-closed-days",
        type=int,
        default=0,
        metavar="N",
        help="followup mode only: also fetch issues closed within the last N days "
        "(default 0 = open issues only). Applies to --repo/--repos-file, not --input.",
    )
    parser.add_argument("--now", help="KST override for testing, e.g. 2026-09-21T01:52")
    parser.add_argument("--limit", type=int, default=200, help="max issues per repo for --repo")
    parser.add_argument(
        "--min-confidence",
        choices=("low", "high"),
        default="low",
        help="low (default) reports demoted hits too; high reports only clean ones",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--exit-nonzero-on-findings",
        action="store_true",
        help="exit 1 when findings exist, for cron/doctor wiring",
    )
    parser.add_argument(
        "--notify",
        choices=NOTIFY_LEVELS,
        default=None,
        help=f"owner notice via the bridge push spool for findings at this confidence "
        f"or above (default: ${NOTIFY_ENV}, else off). Unchanged sets are re-sent "
        f"only every {NOTIFY_REMIND_AFTER_DAYS} days.",
    )
    return parser


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.include_closed_days < 0:
        parser.error("--include-closed-days must be 0 or more")
    if args.include_closed_days and args.mode != "followup":
        parser.error("--include-closed-days is only used by --mode followup")
    return args


def _fetch_repos(
    repos: Sequence[str], limit: int, closed_since: dt.date | None
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Fetch every repo, isolating per-repo failures into ``repo_errors`` (#2324)."""
    issues: list[dict[str, Any]] = []
    repo_errors: list[dict[str, str]] = []
    for repo in repos:
        try:
            if closed_since is None:
                issues.extend(fetch_via_gh(repo, limit))
            else:
                issues.extend(fetch_via_gh(repo, limit, closed_since))
        except REPO_FETCH_ERRORS as exc:
            detail = " ".join(str(exc).split())[:400] or type(exc).__name__
            repo_errors.append({"repo": repo, "error": f"{type(exc).__name__}: {detail}"})
            print(f"repo skipped (scan continues): {repo}: {type(exc).__name__}: {detail}",
                  file=sys.stderr)
    return issues, repo_errors


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    now = (
        dt.datetime.strptime(args.now, "%Y-%m-%dT%H:%M")
        if args.now
        else dt.datetime.now(KST).replace(tzinfo=None)
    )

    repo_errors: list[dict[str, str]] = []
    if args.input:
        issues = load_jsonl(args.input)
    else:
        if args.repos_file:
            try:
                repos = load_repos_file(args.repos_file)
            except NotConfigured as exc:
                print(f"not configured: {exc}", file=sys.stderr)
                return EXIT_NOT_CONFIGURED
            except (ValueError, OSError) as exc:
                # A broken list is not "nothing to do" either; fail loudly
                # rather than scanning a silently shortened set.
                print(f"repo list unusable: {exc}", file=sys.stderr)
                return 2
        else:
            repos = list(args.repo or [])
        closed_since = (
            (now - dt.timedelta(days=args.include_closed_days)).date()
            if args.include_closed_days
            else None
        )
        issues, repo_errors = _fetch_repos(repos, args.limit, closed_since)

    findings = scan(issues, now, args.mode, args.min_confidence)

    if args.json:
        payload = {
            "schema": "ccc.timed-test-deadline-scan.v1",
            "scanned_at_kst": now.strftime("%Y-%m-%d %H:%M"),
            "mode": args.mode,
            "min_confidence": args.min_confidence,
            "issue_count": len(issues),
            "finding_count": len(findings),
            "findings": [f.to_dict() for f in findings],
            "repo_errors": repo_errors,
        }
        if args.mode == "followup":
            payload["include_closed_days"] = args.include_closed_days
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_report(findings, now, args.mode))
        if repo_errors:
            print(f"⚠ 부분 스캔 — 조회 실패 {len(repo_errors)}개 레포는 결과에 없음:")
            for err in repo_errors:
                print(f"  - {err['repo']}: {err['error']}")
    sys.stdout.flush()

    level = resolve_notify_level(args.notify)
    if level != "off":
        # Never let the notice mask the scan: whatever happens here, the exit
        # code below still reports what the scan found.
        try:
            skipped = [err["repo"] for err in repo_errors]
            print(notify_owner(findings, now, args.mode, level, skipped), file=sys.stderr)
        except Exception as exc:  # any failure is non-fatal by contract
            print(f"notify failed (scan result unaffected): {type(exc).__name__}: {exc}", file=sys.stderr)

    if repo_errors:
        return EXIT_PARTIAL_SCAN
    if findings and args.exit_nonzero_on_findings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
