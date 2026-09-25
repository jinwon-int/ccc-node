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

# Inline code is quotation, not a booking. ccc-node#1873 reproduced another
# issue's deadline row as `| 배포 검증 종료 | **2026-09-12 06:00 KST** |` and
# was reported at high confidence for it. The keyword test therefore ignores
# code spans; the dates are still read from the full paragraph so a real
# booking that formats only its timestamp as code keeps its deadline.
INLINE_CODE = re.compile(r"`[^`\n]*`")


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
            if not DEADLINE_KEYWORD.search(INLINE_CODE.sub("", paragraph)):
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
        if finding.deadline is not None:
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
    lines.append(f"신뢰도 {level} 이상 · 전체 목록: timed-test-deadline-scan.cron.log")
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


def notify_owner(findings: Sequence[Finding], now: dt.datetime, mode: str, level: str) -> str:
    """Spool an owner notice when warranted; return a one-line outcome for the log.

    The dedup state is only advanced after the spool file exists, so a failed
    write is retried on the next run instead of being marked as sent.
    """
    candidates = notify_candidates(findings, level)
    current = [finding_fingerprint(f) for f in candidates]
    state_path = notify_state_path(mode)
    state = load_notify_state(state_path)
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
    parser.add_argument(
        "--notify",
        choices=NOTIFY_LEVELS,
        default=None,
        help=f"owner notice via the bridge push spool for findings at this confidence "
        f"or above (default: ${NOTIFY_ENV}, else off). Unchanged sets are re-sent "
        f"only every {NOTIFY_REMIND_AFTER_DAYS} days.",
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
    sys.stdout.flush()

    level = resolve_notify_level(args.notify)
    if level != "off":
        # Never let the notice mask the scan: whatever happens here, the exit
        # code below still reports what the scan found.
        try:
            print(notify_owner(findings, now, args.mode, level), file=sys.stderr)
        except Exception as exc:  # any failure is non-fatal by contract
            print(f"notify failed (scan result unaffected): {type(exc).__name__}: {exc}", file=sys.stderr)

    if findings and args.exit_nonzero_on_findings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
