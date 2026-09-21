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

Two modes:

``expired``
    Deadline is in the past and no verdict-shaped comment followed it.

``relative``
    A timed test described only in relative terms ("며칠", "1~2주") with no
    absolute datetime anywhere in the paragraph. These violate the rule at
    write time, so they can never be judged on schedule.

Input is a JSONL dump (one issue object per line, ``--input``), a live ``gh``
query (``--repo``), or an operator-owned allowlist file (``--repos-file``).
The JSONL path keeps the scanner testable and lets an operator re-run a
judgement against a frozen snapshot; the allowlist is what the cron installer
wires up, so adding a repository never requires re-running the installer.

Exit codes: 0 clean, 1 findings exist (with ``--exit-nonzero-on-findings``),
3 not configured. The third one matters — a scheduled scan whose repo list is
missing or empty must not report a reassuring "0건".

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
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

KST = dt.timezone(dt.timedelta(hours=9))

# Reserved so a scheduled run can tell "nothing to report" (0) apart from
# "nobody told me what to scan" (3). --exit-nonzero-on-findings owns 1.
EXIT_NOT_CONFIGURED = 3

# Paragraph must mention a timed test before a date in it counts as a deadline.
#
# "deadline" must stand alone: as part of an identifier or path it is this
# scanner's own name (`timed_test_deadline_scan.py`,
# `timed-test-deadline-scan.repos`), and any issue that quotes the command
# next to a timestamp — ccc-node#1873 did — became a high-confidence finding.
DEADLINE_KEYWORD = re.compile(
    r"(종료\s*(일시|시각|예정|시점)"
    r"|관측\s*종료|테스트\s*종료|검증\s*종료|예정\s*종료"
    r"|(?<![\w./-])deadline(?![\w./-])|observation\s+window|planned\s+end)",
    re.IGNORECASE,
)

# 2026-09-17 07:32 / 2026.09.17 / 2026년 9월 17일 07:32
DEADLINE_DATE = re.compile(
    r"(20\d{2})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})일?"
    r"(?:\s*(?:T|\s)\s*(\d{1,2}):(\d{2}))?"
)

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

# A verdict announces itself in the heading. Searching the whole comment body
# instead picked up "판정" from a table header (`| 항목 | 값 | 판정 |`,
# ccc-node#1692) and from mid-sentence prose ("불충분 판정", a2a-nexus#1597),
# hiding both real findings. Real verdicts led with "## 관측 종료 판정"
# (ccc-node#1353), so the heading window is where to look.
VERDICT_HEADER_CHARS = 200

# The deadline paragraph itself already reports a finished test.
SETTLED = re.compile(
    r"(완료|✅|통과|합격|불합격|종료\s*보고|결과\s*갱신|실제\s*종료|판정)",
)

# How much of a comment counts as its "header" when deciding whether the whole
# comment is a completion report. A "## 테스트 종료 보고" heading sits well
# inside this window, while unrelated later prose does not leak in.
SETTLED_HEADER_CHARS = 150

# Relative-only durations the owner rule forbids.
RELATIVE_DURATION = re.compile(
    r"(며칠|몇\s*일|수일|여러\s*날|1~2주|한두\s*주|몇\s*주|수주간|당분간"
    r"|a\s+few\s+days|couple\s+of\s+(days|weeks)|in\s+a\s+week)",
)

ISSUE_REFERENCE = re.compile(r"(?:#|/(?:issues|pull)/)(\d{1,7})\b")


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

    def to_dict(self) -> dict[str, Any]:
        return {
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
            found.append((dt.datetime(year, month, day, hour, minute), explicit))
        except ValueError:
            continue  # 2026-13-45 and friends
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
        for paragraph in _iter_paragraphs(text):
            if not DEADLINE_KEYWORD.search(paragraph):
                continue
            dates = _dates_in(paragraph)
            if not dates:
                continue
            reason = classify_false_positive(paragraph, number, header)
            # A paragraph may render the same instant twice (KST and UTC).
            # Keep the latest, which is the KST rendering when both appear.
            deadline, explicit = max(dates, key=lambda item: item[0])
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
    return hits, dropped


def find_relative_only(issue: dict[str, Any]) -> list[str]:
    """Paragraphs promising a timed test with no absolute datetime at all.

    family-messenger#104 said "며칠 관찰 후 완전 삭제" and went unjudged for
    four days: with no absolute end time, no schedule could exist.
    """
    offenders: list[str] = []
    bodies = [issue.get("body") or ""]
    bodies += [(comment.get("body") or "") for comment in (issue.get("comments") or [])]
    for text in bodies:
        for paragraph in _iter_paragraphs(text):
            if not RELATIVE_DURATION.search(paragraph):
                continue
            if _dates_in(paragraph):
                continue  # an absolute datetime is right there; rule satisfied
            offenders.append(paragraph[:160])
    return offenders


def judge_issue(issue: dict[str, Any], now: dt.datetime, mode: str) -> Finding | None:
    """Decide whether one issue needs attention under the given mode."""
    repo = str(issue.get("repo", "")) or "?"
    number = int(issue.get("number", 0))
    title = str(issue.get("title", ""))
    url = str(issue.get("url", ""))
    comments = issue.get("comments") or []
    comment_times = [parse_github_timestamp(c["createdAt"]) for c in comments]

    if mode == "relative":
        offenders = find_relative_only(issue)
        if not offenders:
            return None
        return Finding(
            repo=repo,
            number=number,
            title=title,
            url=url,
            kind="relative-duration-only",
            paragraph=offenders[0],
            last_comment_at=max(comment_times, default=None),
        )

    hits, dropped = collect_hits(issue)
    if not hits:
        return None
    # Prefer a high-confidence deadline; fall back to a demoted one so an
    # issue whose every mention looks like a report is still surfaced, just
    # flagged as lower confidence.
    strong = [hit for hit in hits if hit.confidence == "high"]
    latest = max(strong or hits, key=lambda hit: hit.deadline)
    if latest.deadline >= now:
        return None  # still running; not our business

    after = [t for t in comment_times if t > latest.deadline]
    judged = [
        c
        for c, t in zip(comments, comment_times)
        if t > latest.deadline
        and VERDICT.search((c.get("body") or "")[:VERDICT_HEADER_CHARS])
    ]
    if judged:
        return None

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


def fetch_via_gh(repo: str, limit: int) -> list[dict[str, Any]]:
    """Pull open issues with comments through the gh CLI."""
    fields = "number,title,url,body,comments,createdAt,updatedAt"
    proc = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "open",
         "--limit", str(limit), "--json", fields],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh issue list failed for {repo}: {proc.stderr.strip()[:400]}")
    issues = json.loads(proc.stdout or "[]")
    for issue in issues:
        issue["repo"] = repo
    return issues


def format_report(findings: Sequence[Finding], now: dt.datetime, mode: str) -> str:
    if not findings:
        return f"[{now:%Y-%m-%d %H:%M} KST] {mode}: 해당 없음 — 기한 경과 미판정 0건"
    lines = [f"[{now:%Y-%m-%d %H:%M} KST] {mode}: {len(findings)}건"]
    for finding in findings:
        lines.append("")
        if finding.deadline is not None:
            mark = "" if finding.confidence == "high" else f" | 신뢰도 낮음({finding.weak_reason})"
            lines.append(
                f"### {finding.repo}#{finding.number} | 종료 {finding.deadline:%Y-%m-%d %H:%M}"
                f" | 경과 {finding.days_overdue}일{mark}"
            )
        else:
            lines.append(f"### {finding.repo}#{finding.number} | 절대시각 없음")
        lines.append(f"    {finding.title[:100]}")
        lines.append(f"    근거: {finding.paragraph[:150]}")
        if finding.last_comment_at:
            lines.append(f"    마지막 코멘트: {finding.last_comment_at:%Y-%m-%d %H:%M} KST")
        lines.append(f"    기한 후 코멘트 {finding.comments_after}건 / 판정성 0건")
        lines.append(f"    {finding.url}")
    return "\n".join(lines)


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
        choices=("expired", "relative"),
        default="expired",
        help="expired: deadline passed with no verdict. relative: no absolute datetime at all.",
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = (
        dt.datetime.strptime(args.now, "%Y-%m-%dT%H:%M")
        if args.now
        else dt.datetime.now(KST).replace(tzinfo=None)
    )

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
        issues = []
        for repo in repos:
            issues.extend(fetch_via_gh(repo, args.limit))

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
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_report(findings, now, args.mode))

    if findings and args.exit_nonzero_on_findings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
