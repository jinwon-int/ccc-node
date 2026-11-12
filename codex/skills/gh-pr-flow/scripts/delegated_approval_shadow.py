#!/usr/bin/env python3
"""Delegated-approval SHADOW recorder (a2a-nexus#2317 Phase 1, ccc-node#2137). Records, never decides.

Why this exists
---------------
The fleet's 하부위임전결 spec (a2a-nexus ``docs/specs/delegated-approval-matrix.md``,
ratified 2026-10-22) says L1/L2 decisions may one day be delegated to a hub
agent while L3 stays with a person. Before anything is delegated, Phase 1
measures how the matrix *would* have classified the approvals a person
actually gave: the owner decided (2026-10-23) to put that recorder here, on
the path where cross-account PR approvals really happen
(``approve-via-relay.sh``). This module only appends a ledger line; it never
changes whether an approval is submitted.

What is recorded (structured evidence only — principle 4)
----------------------------------------------------------
repo, PR, exact head, author, approver, base, changed paths (classified), diff
totals, issue refs from the title, the matrix v0 tier the evidence maps to and
the rule that fired, the Q2 anti-split window counts, the kill-switch env
snapshot, and the human decision (``approve`` — the script runs only after a
fresh operator approval). PR bodies / review text are never read.

Tier rules (matrix v0, fail-closed)
-----------------------------------
L3  any path in the L3 set (CI workflows, required-checks, secrets/env/token
    files, release/publish/deploy paths, the matrix spec itself), a promotion
    branch base, self-approval, no file evidence, or a Q2 breach (same repo +
    issue: >=5 prior approvals in 24h, or cumulative diff > 800 lines).
L1  every path is docs or tests (docs/test-only PR merge).
L2  anything else (non-live repo code PR merge).

``summary`` prints agreement metrics for the Q5 review (shadow "delegate" =
L1/L2 vs the human approval; "escalate" = L3). Stdlib only; stdout is quiet in
``record`` mode because the caller's stdout carries the approval JSON.

Requests and refusals (spec §10.2 criterion 5-d, schema v2)
------------------------------------------------------------
An approval row exists only when a person approved, so a refusal left no trace
and "상향 오판" (the shadow said L1, the person refused) could not be counted.
Two more row kinds close that gap, still record-only:

``ask``       an agent asked the owner to approve this PR at this exact head
              (same structured evidence and tier as an approval row, no approver).
``decision``  a person's non-approval for that head: ``decline`` (chose another
              option), ``hold``, ``closed-unmerged``; ``reply-unclassified`` is a
              note that an answer arrived but could not be classified. No text
              is ever stored — only the class, the PR and the head.

Rows without ``kind`` are schema-v1 approvals. How each ask ended is *derived*
in ``summary`` rather than written, so it does not depend on write order: the
first approval or terminal decision on the same head wins; a later ask for the
same PR at another head means ``superseded``; no answer for 24 h means
``expired`` (not a refusal); otherwise ``open``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = "ccc.delegated-approval-shadow.v2"
MATRIX_VERSION = "v0"
# An ask nobody answered for this long counts as expired (Q4: a receipt is valid for 24h).
ASK_EXPIRE_S = 24 * 3600
# A person's non-approval that ends an ask, and the one note that does not.
TERMINAL_DECISIONS = ("decline", "hold", "closed-unmerged")
NOTE_DECISIONS = ("reply-unclassified",)
DECISION_SOURCES = ("reply", "github", "manual")
RESOLUTIONS = ("approve",) + TERMINAL_DECISIONS + ("superseded", "expired", "open")
Q2_WINDOW_S = 24 * 3600
Q2_MAX_COUNT = 5
Q2_MAX_DIFF = 800
DEFAULT_STATE_DIR = Path(os.environ.get("CCC_STATE_DIR") or Path.home() / ".claude" / "state")
LEDGER_REL = Path("delegated-approval") / "shadow.jsonl"

L3_PATH_RES = (
    re.compile(r"^\.github/workflows/"),
    re.compile(r"^\.github/required-checks\.json$"),
    re.compile(r"(^|/)secrets?(/|\.|$)"),
    re.compile(r"(^|/)\.env(\.|$)"),
    re.compile(r"(^|/)[^/]*(token|credential|api[_-]?key|password)[^/]*$", re.I),
    re.compile(r"^docs/specs/delegated-approval-matrix\.md$"),
    re.compile(r"(^|/)(release|publish)[^/]*\.(ya?ml|sh|py)$"),
    re.compile(r"(^|/)deploy/"),
)
DOC_RES = (
    re.compile(r"^docs/"), re.compile(r"\.md$"), re.compile(r"^changelog\.d/"), re.compile(r"^CHANGELOG"),
    re.compile(r"^README"), re.compile(r"^LICENSE"), re.compile(r"^pages/"), re.compile(r"\.rst$"),
)
TEST_RES = (
    re.compile(r"(_test\.py|\.test\.sh|\.test\.mjs|_test\.go|\.test\.ts|\.spec\.ts)$"),
    re.compile(r"(^|/)tests?/"), re.compile(r"(^|/)fixtures/"), re.compile(r"(^|/)test_[^/]+\.py$"),
)
# ``#N`` (this repo), ``repo#N`` (same owner) or ``owner/repo#N``. The lookbehind keeps a
# hyphenated name from being read from its last segment (``a2a-nexus#2317`` → ``a2a-nexus``).
ISSUE_RE = re.compile(r"(?<![\w/#.-])(?:([\w.-]+(?:/[\w.-]+)?))?#(\d{1,6})\b")


def classify_path(path: str) -> str:
    if any(r.search(path) for r in L3_PATH_RES):
        return "l3"
    if any(r.search(path) for r in DOC_RES):
        return "docs"
    if any(r.search(path) for r in TEST_RES):
        return "tests"
    return "code"


def issue_refs(title: str, repo: str) -> list[str]:
    """``owner/repo#N`` refs from the title: bare ``#N`` is this repo, ``repo#N`` shares its owner."""
    owner = repo.split("/", 1)[0]
    refs = set()
    for prefix, number in ISSUE_RE.findall(title or ""):
        if not prefix:
            target = repo
        elif "/" in prefix:
            target = prefix
        else:
            target = f"{owner}/{prefix}"
        refs.add(f"{target}#{number}")
    return sorted(refs)


def row_kind(row: dict) -> str:
    """``approve`` (also every schema-v1 row), ``ask`` or ``decision``."""
    return str(row.get("kind") or "approve")


def q2_window(ledger: Iterable[dict], repo: str, refs: list[str], now: float) -> dict:
    """Prior approvals in the last 24h that share this repo and any issue ref."""
    count, diff = 0, 0
    if refs:
        for row in ledger:
            if row_kind(row) != "approve":  # asks and decisions are not approvals
                continue
            if row.get("repo") != repo or now - float(row.get("ts_epoch") or 0) > Q2_WINDOW_S:
                continue
            if set(row.get("issue_refs") or []) & set(refs):
                count += 1
                diff += int(row.get("additions") or 0) + int(row.get("deletions") or 0)
    return {"prior_count_24h": count, "prior_diff_24h": diff}


def classify(
    *, paths: list[str], additions: int, deletions: int, author: str, approver: str,
    promotion: bool, q2: dict,
) -> tuple[str, str, dict]:
    """``(tier, rule_id, categories)``. Fail-closed: anything unclear is L3."""
    cats = {"docs": 0, "tests": 0, "code": 0, "l3": 0}
    for p in paths:
        cats[classify_path(p)] += 1
    if author and approver and author == approver:
        return "L3", f"{MATRIX_VERSION}.L3.self-approval", cats
    if not paths:
        return "L3", f"{MATRIX_VERSION}.L3.no-evidence", cats
    if cats["l3"]:
        return "L3", f"{MATRIX_VERSION}.L3.path", cats
    if promotion:
        return "L3", f"{MATRIX_VERSION}.L3.promotion-branch", cats
    if q2.get("prior_count_24h", 0) >= Q2_MAX_COUNT:
        return "L3", f"{MATRIX_VERSION}.L3.q2-count", cats
    if q2.get("prior_diff_24h", 0) + additions + deletions > Q2_MAX_DIFF:
        return "L3", f"{MATRIX_VERSION}.L3.q2-diff", cats
    if cats["code"] == 0:
        return "L1", f"{MATRIX_VERSION}.L1.docs-test-only", cats
    return "L2", f"{MATRIX_VERSION}.L2.code", cats


# ---- ledger ----------------------------------------------------------------

def ledger_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or DEFAULT_STATE_DIR) / LEDGER_REL


def read_ledger(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return rows


def append_ledger(path: Path, row: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


# ---- record ----------------------------------------------------------------

def fetch_pr(repo: str, pr: int) -> dict:
    out = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "files,additions,deletions,title,author,baseRefName"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    return json.loads(out)


def build_row(
    *, repo: str, pr: int, head: str, approver: str, meta: dict, promotion: bool, ledger: list[dict],
    now: Optional[float] = None, node: Optional[str] = None,
) -> dict:
    now = time.time() if now is None else now
    paths = [str(f.get("path", "")) for f in (meta.get("files") or []) if isinstance(f, dict)]
    additions = int(meta.get("additions") or 0)
    deletions = int(meta.get("deletions") or 0)
    author = str((meta.get("author") or {}).get("login") or meta.get("author") or "")
    refs = issue_refs(str(meta.get("title") or ""), repo)
    q2 = q2_window(ledger, repo, refs, now)
    tier, rule, cats = classify(
        paths=paths, additions=additions, deletions=deletions, author=author, approver=approver,
        promotion=promotion, q2=q2,
    )
    return {
        "schema": SCHEMA, "kind": "approve", "matrix": MATRIX_VERSION, "phase": 1,
        "ts": _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "ts_epoch": now,
        "node": node or socket.gethostname(),
        "repo": repo, "pr": pr, "head": head, "base": str(meta.get("baseRefName") or ""),
        "author": author, "approver": approver,
        "files": len(paths), "categories": cats, "additions": additions, "deletions": deletions,
        "issue_refs": refs, "q2": q2,
        "shadow_tier": tier, "rule_id": rule,
        "shadow_would": "escalate" if tier == "L3" else "delegate",
        "human_decision": "approve",
        "agreement": "shadow-delegates-human-approved" if tier != "L3" else "shadow-escalates-human-approved",
        "env": {
            "A2A_DELEGATED_APPROVAL": os.environ.get("A2A_DELEGATED_APPROVAL", ""),
            "A2A_DELEGATED_APPROVAL_KILL": os.environ.get("A2A_DELEGATED_APPROVAL_KILL", ""),
        },
    }


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def has_ask(ledger: Iterable[dict], repo: str, pr: int, head: str) -> bool:
    return any(row_kind(r) == "ask" and r.get("repo") == repo and r.get("pr") == pr and r.get("head") == head
               for r in ledger)


def build_ask_row(
    *, repo: str, pr: int, head: str, meta: dict, promotion: bool, ledger: list[dict],
    session: Optional[str] = None, now: Optional[float] = None, node: Optional[str] = None,
) -> dict:
    """The evidence and tier an approval row would carry, taken when the owner is *asked*."""
    row = build_row(repo=repo, pr=pr, head=head, approver="", meta=meta, promotion=promotion,
                    ledger=ledger, now=now, node=node)
    for key in ("approver", "human_decision", "agreement"):
        row.pop(key, None)
    row["kind"] = "ask"
    # A session is only ever stored as a short digest: it groups rows, it does not identify a chat.
    row["session"] = hashlib.sha256(session.encode("utf-8")).hexdigest()[:12] if session else ""
    return row


def build_decision_row(
    *, repo: str, pr: int, head: str, decision: str, source: str, detail: str = "",
    now: Optional[float] = None, node: Optional[str] = None,
) -> dict:
    now = time.time() if now is None else now
    return {
        "schema": SCHEMA, "kind": "decision", "matrix": MATRIX_VERSION, "phase": 1,
        "ts": _iso(now), "ts_epoch": now, "node": node or socket.gethostname(),
        "repo": repo, "pr": pr, "head": head,
        "decision": decision, "source": source, "detail": detail,
    }


def _off() -> bool:
    return os.environ.get("CCC_DELEGATED_SHADOW", "1").lower() in ("0", "false", "off", "no")


def cmd_ask(args: argparse.Namespace) -> int:
    if _off():
        return 0
    path = ledger_path(Path(args.state_dir) if args.state_dir else None)
    head = args.head.lower()
    try:
        ledger = read_ledger(path)
        if resolve_asks(ledger, until=time.time()).get((args.repo, args.pr, head)) == "open":
            print(f"delegated-approval shadow: ask already open ({args.repo}#{args.pr})", file=sys.stderr)
            return 0
        meta = json.loads(Path(args.pr_json_file).read_text(encoding="utf-8")) if args.pr_json_file else fetch_pr(args.repo, args.pr)
        row = build_ask_row(repo=args.repo, pr=args.pr, head=head, meta=meta, promotion=bool(args.promotion),
                            ledger=ledger, session=args.session, node=args.node)
        append_ledger(path, row)
    except Exception as exc:  # best-effort, like record: asking the owner must never fail on this
        print(f"delegated-approval shadow: ask not recorded ({type(exc).__name__})", file=sys.stderr)
        return 0
    print(f"delegated-approval shadow: ask {row['shadow_tier']} {row['rule_id']} ({row['repo']}#{row['pr']})", file=sys.stderr)
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    if _off():
        return 0
    path = ledger_path(Path(args.state_dir) if args.state_dir else None)
    try:
        head = (args.head or "").lower()
        if not head:  # attach to the newest ask for this PR
            asks = [r for r in read_ledger(path)
                    if row_kind(r) == "ask" and r.get("repo") == args.repo and r.get("pr") == args.pr]
            if not asks:
                print(f"delegated-approval shadow: no ask for {args.repo}#{args.pr}; pass --head to record anyway",
                      file=sys.stderr)
                return 0
            head = str(max(asks, key=lambda r: float(r.get("ts_epoch") or 0)).get("head") or "")
        row = build_decision_row(repo=args.repo, pr=args.pr, head=head, decision=args.decision,
                                 source=args.source, detail=args.detail or "", node=args.node)
        append_ledger(path, row)
    except Exception as exc:
        print(f"delegated-approval shadow: decision not recorded ({type(exc).__name__})", file=sys.stderr)
        return 0
    print(f"delegated-approval shadow: {row['decision']} via {row['source']} ({row['repo']}#{row['pr']})", file=sys.stderr)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    if _off():
        return 0
    path = ledger_path(Path(args.state_dir) if args.state_dir else None)
    try:
        meta = json.loads(Path(args.pr_json_file).read_text(encoding="utf-8")) if args.pr_json_file else fetch_pr(args.repo, args.pr)
        ledger = read_ledger(path)
        row = build_row(
            repo=args.repo, pr=args.pr, head=args.head.lower(), approver=args.approver, meta=meta,
            promotion=bool(args.promotion), ledger=ledger, node=args.node,
        )
        # No ask on record for this head: the request was made without the recorder. Kept as a
        # field so the share of unrecorded requests is a number instead of a silent gap.
        row["implicit_ask"] = not has_ask(ledger, args.repo, args.pr, row["head"])
        append_ledger(path, row)
    except Exception as exc:  # shadow is best-effort; the approval already happened
        print(f"delegated-approval shadow: not recorded ({type(exc).__name__})", file=sys.stderr)
        return 0
    print(f"delegated-approval shadow: {row['shadow_tier']} {row['rule_id']} ({row['repo']}#{row['pr']})", file=sys.stderr)
    return 0


# ---- summary ---------------------------------------------------------------

def resolve_asks(rows: Iterable[dict], *, until: float) -> dict:
    """``{(repo, pr, head): resolution}`` for every ask before ``until`` (see the module docstring)."""
    seen = sorted((r for r in rows if float(r.get("ts_epoch") or 0) < until),
                  key=lambda r: float(r.get("ts_epoch") or 0))
    out: dict = {}
    for i, ask in enumerate(seen):
        key = (ask.get("repo"), ask.get("pr"), ask.get("head"))
        if row_kind(ask) != "ask" or key in out:  # the same ask seen on two nodes counts once
            continue
        resolution = None
        for later in seen[i + 1:]:
            if (later.get("repo"), later.get("pr")) != key[:2]:
                continue
            kind, same_head = row_kind(later), later.get("head") == key[2]
            if kind == "approve" and same_head:
                resolution = "approve"
            elif kind == "decision" and same_head and later.get("decision") in TERMINAL_DECISIONS:
                resolution = str(later["decision"])
            elif kind in ("ask", "approve") and not same_head:  # the PR moved on to another head
                resolution = "superseded"
            if resolution:
                break
        if resolution is None:
            resolution = "expired" if until - float(ask.get("ts_epoch") or 0) > ASK_EXPIRE_S else "open"
        out[key] = resolution
    return out


def _ask_metrics(rows: list[dict], *, since: float, until: float) -> dict:
    resolved = resolve_asks(rows, until=until)
    first: dict = {}
    for r in sorted(rows, key=lambda r: float(r.get("ts_epoch") or 0)):
        key = (r.get("repo"), r.get("pr"), r.get("head"))
        if row_kind(r) == "ask" and key in resolved and key not in first:
            first[key] = r
    asks = {k: r for k, r in first.items() if since <= float(r.get("ts_epoch") or 0) < until}

    def tally(keys: Iterable[tuple]) -> dict:
        counts = {name: 0 for name in RESOLUTIONS}
        for k in keys:
            counts[resolved[k]] += 1
        return counts

    l1 = [k for k, r in asks.items() if r.get("shadow_tier") == "L1"]
    refused = sorted(f"{k[0]}#{k[1]}" for k in l1 if resolved[k] in TERMINAL_DECISIONS)
    notes = sum(1 for r in rows if row_kind(r) == "decision" and r.get("decision") in NOTE_DECISIONS
                and since <= float(r.get("ts_epoch") or 0) < until)
    return {
        "total": len(asks), "by_resolution": tally(asks),
        "l1": {"total": len(l1), "by_resolution": tally(l1)},
        # Spec §10.2 5-c: an L1 ask a person refused. Superseded and expired asks are reported
        # above and reviewed by hand; they are not refusals.
        "upward_misjudgement": {"count": len(refused), "prs": refused},
        "reply_unclassified": notes,
    }


def summarize(
    rows: list[dict], *, days: int = 14, now: Optional[float] = None,
    since: Optional[float] = None, until: Optional[float] = None,
) -> dict:
    """Metrics over ``[since, until)``; without them, the last ``days`` days up to ``now``."""
    until = (time.time() if now is None else now) if until is None else until
    since = until - days * 86400 if since is None else since
    approvals = [r for r in rows if row_kind(r) == "approve"]
    sel = [r for r in approvals if since <= float(r.get("ts_epoch") or 0) < until]
    flagged = [r for r in sel if "implicit_ask" in r]
    implicit = sum(1 for r in flagged if r.get("implicit_ask"))
    by_tier = {"L1": 0, "L2": 0, "L3": 0}
    by_rule: dict[str, int] = {}
    for r in sel:
        by_tier[r.get("shadow_tier", "L3")] = by_tier.get(r.get("shadow_tier", "L3"), 0) + 1
        by_rule[r.get("rule_id", "?")] = by_rule.get(r.get("rule_id", "?"), 0) + 1
    total = len(sel)
    delegable = by_tier["L1"] + by_tier["L2"]
    return {
        "schema": "ccc.delegated-approval-shadow-summary.v2", "days": days, "total": total,
        "since": _iso(since), "until": _iso(until),
        "by_tier": by_tier, "by_rule": dict(sorted(by_rule.items())),
        # Q5 metrics: share of human-approved decisions the matrix would have delegated,
        # and the escalation (L3) rate. "상향 오판" needs a human label and is reviewed by hand.
        "agreement_rate": round(delegable / total, 4) if total else None,
        "escalation_rate": round(by_tier["L3"] / total, 4) if total else None,
        "repos": sorted({r.get("repo", "") for r in sel}),
        "first": min((r.get("ts") for r in sel), default=None), "last": max((r.get("ts") for r in sel), default=None),
        # Spec §10.2: how the requests ended (5-c) and how many approvals had no recorded request.
        "asks": _ask_metrics(rows, since=since, until=until),
        "implicit_ask": {"approvals": len(flagged), "implicit": implicit,
                         "rate": round(implicit / len(flagged), 4) if flagged else None},
    }


def parse_when(text: str) -> float:
    """ISO-8601 with an explicit offset (``2026-10-28T04:22:00+09:00``). A window edge is an absolute time."""
    value = _dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamp needs a UTC offset")
    return value.timestamp()


def cmd_summary(args: argparse.Namespace) -> int:
    try:
        since = parse_when(args.since) if args.since else None
        until = parse_when(args.until) if args.until else None
    except ValueError as exc:
        print(f"delegated-approval shadow: {exc}", file=sys.stderr)
        return 2
    paths = [Path(p) for p in args.ledger] if args.ledger else [ledger_path(Path(args.state_dir) if args.state_dir else None)]
    rows = [row for path in paths for row in read_ledger(path)]
    print(json.dumps(summarize(rows, days=args.days, since=since, until=until), ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="append one shadow judgement (quiet on stdout; never fails the caller)")
    r.add_argument("--repo", required=True)
    r.add_argument("--pr", required=True, type=int)
    r.add_argument("--head", required=True)
    r.add_argument("--approver", required=True)
    r.add_argument("--promotion", action="store_true", help="the approval targeted a promotion branch (--base)")
    r.add_argument("--pr-json-file", help="use this gh pr view JSON instead of calling gh (tests)")
    r.add_argument("--state-dir")
    r.add_argument("--node")
    a = sub.add_parser("ask", help="note that the owner was asked to approve this head (quiet on stdout; never fails)")
    a.add_argument("--repo", required=True)
    a.add_argument("--pr", required=True, type=int)
    a.add_argument("--head", required=True)
    a.add_argument("--promotion", action="store_true", help="the PR targets a promotion branch")
    a.add_argument("--session", help="grouping key; stored as a short digest only")
    a.add_argument("--pr-json-file", help="use this gh pr view JSON instead of calling gh (tests)")
    a.add_argument("--state-dir")
    a.add_argument("--node")
    d = sub.add_parser("decide", help="note a person's non-approval for an asked head (class only, no text)")
    d.add_argument("--repo", required=True)
    d.add_argument("--pr", required=True, type=int)
    d.add_argument("--head", help="default: the newest ask for this PR")
    d.add_argument("--decision", required=True, choices=TERMINAL_DECISIONS + NOTE_DECISIONS)
    d.add_argument("--source", required=True, choices=DECISION_SOURCES)
    d.add_argument("--detail", help="short class label such as other-option (letters, digits, hyphen)")
    d.add_argument("--state-dir")
    d.add_argument("--node")
    s = sub.add_parser("summary", help="Q5 metrics over a window (default: the last N days)")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--since", help="window start, ISO-8601 with offset (e.g. 2026-10-28T04:22:00+09:00)")
    s.add_argument("--until", help="window end (exclusive), ISO-8601 with offset")
    s.add_argument("--ledger", action="append", help="read this ledger file instead of the local one (repeatable)")
    s.add_argument("--state-dir")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"jinwon-int/[A-Za-z0-9_.-]+", getattr(args, "repo", "jinwon-int/x") or ""):
        print("delegated-approval shadow: repo must be jinwon-int/<repo>", file=sys.stderr)
        return 2 if args.cmd == "summary" else 0
    if args.cmd == "decide" and args.detail and not re.fullmatch(r"[a-z0-9-]{1,40}", args.detail):
        print("delegated-approval shadow: --detail is a class label, not text", file=sys.stderr)
        return 0
    return {"record": cmd_record, "ask": cmd_ask, "decide": cmd_decide, "summary": cmd_summary}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
