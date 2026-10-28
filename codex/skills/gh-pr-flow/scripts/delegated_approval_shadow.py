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
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = "ccc.delegated-approval-shadow.v1"
MATRIX_VERSION = "v0"
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


def q2_window(ledger: Iterable[dict], repo: str, refs: list[str], now: float) -> dict:
    """Prior approvals in the last 24h that share this repo and any issue ref."""
    count, diff = 0, 0
    if refs:
        for row in ledger:
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
        "schema": SCHEMA, "matrix": MATRIX_VERSION, "phase": 1,
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


def cmd_record(args: argparse.Namespace) -> int:
    if os.environ.get("CCC_DELEGATED_SHADOW", "1").lower() in ("0", "false", "off", "no"):
        return 0
    path = ledger_path(Path(args.state_dir) if args.state_dir else None)
    try:
        meta = json.loads(Path(args.pr_json_file).read_text(encoding="utf-8")) if args.pr_json_file else fetch_pr(args.repo, args.pr)
        row = build_row(
            repo=args.repo, pr=args.pr, head=args.head.lower(), approver=args.approver, meta=meta,
            promotion=bool(args.promotion), ledger=read_ledger(path), node=args.node,
        )
        append_ledger(path, row)
    except Exception as exc:  # shadow is best-effort; the approval already happened
        print(f"delegated-approval shadow: not recorded ({type(exc).__name__})", file=sys.stderr)
        return 0
    print(f"delegated-approval shadow: {row['shadow_tier']} {row['rule_id']} ({row['repo']}#{row['pr']})", file=sys.stderr)
    return 0


# ---- summary ---------------------------------------------------------------

def summarize(rows: list[dict], *, days: int, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    since = now - days * 86400
    sel = [r for r in rows if float(r.get("ts_epoch") or 0) >= since]
    by_tier = {"L1": 0, "L2": 0, "L3": 0}
    by_rule: dict[str, int] = {}
    for r in sel:
        by_tier[r.get("shadow_tier", "L3")] = by_tier.get(r.get("shadow_tier", "L3"), 0) + 1
        by_rule[r.get("rule_id", "?")] = by_rule.get(r.get("rule_id", "?"), 0) + 1
    total = len(sel)
    delegable = by_tier["L1"] + by_tier["L2"]
    return {
        "schema": "ccc.delegated-approval-shadow-summary.v1", "days": days, "total": total,
        "by_tier": by_tier, "by_rule": dict(sorted(by_rule.items())),
        # Q5 metrics: share of human-approved decisions the matrix would have delegated,
        # and the escalation (L3) rate. "상향 오판" needs a human label and is reviewed by hand.
        "agreement_rate": round(delegable / total, 4) if total else None,
        "escalation_rate": round(by_tier["L3"] / total, 4) if total else None,
        "repos": sorted({r.get("repo", "") for r in sel}),
        "first": min((r.get("ts") for r in sel), default=None), "last": max((r.get("ts") for r in sel), default=None),
    }


def cmd_summary(args: argparse.Namespace) -> int:
    path = ledger_path(Path(args.state_dir) if args.state_dir else None)
    print(json.dumps(summarize(read_ledger(path), days=args.days), ensure_ascii=False, sort_keys=True))
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
    s = sub.add_parser("summary", help="Q5 metrics over the last N days")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--state-dir")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"jinwon-int/[A-Za-z0-9_.-]+", getattr(args, "repo", "jinwon-int/x") or ""):
        print("delegated-approval shadow: repo must be jinwon-int/<repo>", file=sys.stderr)
        return 0 if args.cmd == "record" else 2
    return cmd_record(args) if args.cmd == "record" else cmd_summary(args)


if __name__ == "__main__":
    raise SystemExit(main())
