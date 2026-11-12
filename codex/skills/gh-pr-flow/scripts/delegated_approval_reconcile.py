#!/usr/bin/env python3
"""Delegated-approval shadow: collect node ledgers and reconcile them with GitHub. Reports, never decides.

Why this exists (a2a-nexus spec §10.2, ccc-node#2137 5-d slice 2)
-----------------------------------------------------------------
The shadow ledger (``delegated_approval_shadow.py``) is per node and only knows
what went through the recorder. A window verdict needs two more things:

``collect``    every node's ledger in one place, with the nodes that have no
               recorder or could not be reached named in the report.
``reconcile``  what GitHub shows for the same window: every PR closed in the
               recorded repositories, classified by the same path rules, with
               the outcome a person gave it. That is where a refusal shows up
               when nobody recorded an ask — a docs/test-only PR closed without
               merging, a "changes requested" review, a ``⛔ hold`` comment, or
               a revert within seven days.

Both are read-only towards the fleet: ``collect`` runs one ``cat`` per node,
``reconcile`` runs ``gh`` list/view calls. Neither writes a ledger row and
neither touches an approval.

What is kept (structured evidence only — principle 4)
-----------------------------------------------------
Counts, tiers, and ``owner/repo#N`` references. Titles are read only to find
the PR number a revert names; review and comment text is read only to test for
the ``CHANGES_REQUESTED`` state and the ``⛔ hold`` marker. None of it is
written to the report.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import delegated_approval_shadow as shadow  # noqa: E402

SCHEMA_COLLECT = "ccc.delegated-approval-collect.v1"
SCHEMA_RECONCILE = "ccc.delegated-approval-reconcile.v1"
NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
REPO_RE = re.compile(r"jinwon-int/[A-Za-z0-9_.-]+")
NO_LEDGER = "__NO_LEDGER__"
# One fixed remote command: print the ledger, or a marker when the node has none. The path is
# the recorder's default — its state dir under the home directory (same parts as
# ``shadow.DEFAULT_STATE_DIR``), then ``shadow.LEDGER_REL``.
_STATE_REL = "/".join((".claude", "state"))
REMOTE_CAT = (
    f'f="${{CCC_STATE_DIR:-$HOME/{_STATE_REL}}}/{shadow.LEDGER_REL.as_posix()}"; '
    f'if [ -f "$f" ]; then cat "$f"; else echo {NO_LEDGER}; fi'
)
PR_FIELDS = "number,title,author,additions,deletions,files,reviews,headRefOid,baseRefName,mergedAt,closedAt"
HOLD_MARKER = "⛔ hold"
REVERT_WINDOW_S = 7 * 86400
DEFAULT_BASES = ("main", "master")

Runner = Callable[[list], "subprocess.CompletedProcess"]


def _run(argv: list, timeout: int = 120) -> "subprocess.CompletedProcess":
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        return _dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---- collect ---------------------------------------------------------------

def fetch_ledger(
    node: str, *, self_node: str, ssh: list, timeout: int, state_dir: Optional[Path], runner: Runner,
) -> tuple[str, str]:
    """``(status, text)`` with status ``ok`` · ``no-ledger`` · ``unreachable``."""
    if node == self_node:
        path = shadow.ledger_path(state_dir)
        try:
            return "ok", path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "no-ledger", ""
        except OSError:
            return "unreachable", ""
    argv = [*ssh, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", node, REMOTE_CAT]
    try:
        done = runner(argv)
    except (OSError, subprocess.SubprocessError):
        return "unreachable", ""
    if done.returncode != 0:
        return "unreachable", ""
    if done.stdout.strip() == NO_LEDGER:
        return "no-ledger", ""
    return "ok", done.stdout


def _valid_lines(text: str) -> tuple[list[str], int]:
    """Lines that parse as a JSON object, and how many did not."""
    kept, dropped = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ok = isinstance(json.loads(line), dict)
        except ValueError:
            ok = False
        if ok:
            kept.append(line)
        else:
            dropped += 1
    return kept, dropped


def collect(
    nodes: Iterable[str], out_dir: Path, *, self_node: str = "", ssh: Optional[list] = None, timeout: int = 15,
    state_dir: Optional[Path] = None, runner: Runner = _run,
) -> dict:
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    report: dict = {"schema": SCHEMA_COLLECT, "out": str(out_dir), "nodes": {}, "ledgers": []}
    for node in nodes:
        status, text = fetch_ledger(node, self_node=self_node, ssh=ssh or ["ssh"], timeout=timeout,
                                    state_dir=state_dir, runner=runner)
        entry = {"status": status, "rows": 0, "dropped": 0}
        if status == "ok":
            lines, entry["dropped"] = _valid_lines(text)
            entry["rows"] = len(lines)
            target = out_dir / f"{node}.jsonl"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"))
            finally:
                os.close(fd)
            report["ledgers"].append(str(target))
        report["nodes"][node] = entry
    statuses = [entry["status"] for entry in report["nodes"].values()]
    report["complete"] = "unreachable" not in statuses
    report["without_recorder"] = sorted(n for n, e in report["nodes"].items() if e["status"] == "no-ledger")
    report["unreachable"] = sorted(n for n, e in report["nodes"].items() if e["status"] == "unreachable")
    report["rows"] = sum(entry["rows"] for entry in report["nodes"].values())
    return report


# ---- reconcile -------------------------------------------------------------

def spans(since: float, until: float, days: int) -> list[tuple[str, str]]:
    """The window cut into ``days``-sized pieces, so one search never returns a huge page."""
    step, out, start = max(1, days) * 86400, [], since
    while start < until:
        end = min(start + step, until)
        out.append((_iso(start), _iso(end)))
        start = end
    return out


def gh_json(argv: list, *, runner: Runner, retries: int = 3, sleep: Callable[[float], None] = time.sleep):
    """``gh … --json`` output, retried: the search API answers 502 now and then on a large page."""
    last = ""
    for attempt in range(retries):
        try:
            done = runner(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            last = type(exc).__name__
        else:
            if done.returncode == 0:
                try:
                    return json.loads(done.stdout or "[]")
                except ValueError:
                    last = "invalid-json"
            else:
                last = "gh-exit"
        if attempt + 1 < retries:
            sleep(2.0 * (attempt + 1))
    raise RuntimeError(last or "gh-failed")


def list_closed(repo: str, since: float, until: float, *, runner: Runner, span_days: int, sleep=time.sleep) -> list[dict]:
    """Every PR merged or closed-unmerged in ``[since, until)``; ``state`` is added to each."""
    found: dict = {}
    for state, query, stamp in (("merged", "merged:{a}..{b}", "mergedAt"), ("closed", "is:unmerged closed:{a}..{b}", "closedAt")):
        for a, b in spans(since, until, span_days):
            rows = gh_json(["gh", "pr", "list", "--repo", repo, "--state", state, "--search", query.format(a=a, b=b),
                            "--limit", "1000", "--json", PR_FIELDS], runner=runner, sleep=sleep)
            for pr in rows if isinstance(rows, list) else []:
                when = _epoch(pr.get(stamp))
                if when is None or not since <= when < until:  # search ranges are inclusive; the window is not
                    continue
                found[(state, pr.get("number"))] = {**pr, "state": state, "when": when}
    return list(found.values())


def _login(pr: dict) -> str:
    author = pr.get("author")
    return str(author.get("login") or "") if isinstance(author, dict) else str(author or "")


def _is_bot(pr: dict) -> bool:
    author = pr.get("author")
    login = _login(pr)
    return bool(isinstance(author, dict) and author.get("is_bot")) or login.startswith("app/") or login.endswith("[bot]")


def path_tier(pr: dict) -> str:
    """The tier the recorder's own rules give this PR from its paths and size alone."""
    paths = [str(f.get("path", "")) for f in (pr.get("files") or []) if isinstance(f, dict)]
    tier, _rule, _cats = shadow.classify(
        paths=paths, additions=int(pr.get("additions") or 0), deletions=int(pr.get("deletions") or 0),
        author=_login(pr), approver="", promotion=str(pr.get("baseRefName") or "main") not in DEFAULT_BASES,
        q2={"prior_count_24h": 0, "prior_diff_24h": 0},
    )
    return tier


def has_hold_comment(repo: str, number: int, *, runner: Runner, sleep=time.sleep) -> bool:
    data = gh_json(["gh", "pr", "view", str(number), "--repo", repo, "--json", "comments"], runner=runner, sleep=sleep)
    comments = data.get("comments") if isinstance(data, dict) else None
    return any(str(c.get("body") or "").lstrip().startswith(HOLD_MARKER) for c in (comments or []) if isinstance(c, dict))


def reverted_prs(repo: str, since: float, merged_at: dict, *, runner: Runner, sleep=time.sleep) -> set:
    """Numbers in ``merged_at`` that a merged "Revert …" PR names within seven days of their merge."""
    day = _iso(since)[:10]
    rows = gh_json(["gh", "pr", "list", "--repo", repo, "--state", "merged", "--search", f"Revert in:title created:>={day}",
                    "--limit", "200", "--json", "number,title,createdAt"], runner=runner, sleep=sleep)
    hit: set = set()
    for pr in rows if isinstance(rows, list) else []:
        created = _epoch(pr.get("createdAt"))
        for ref in re.findall(r"#(\d{1,7})\b", str(pr.get("title") or "")):
            original = merged_at.get(int(ref))
            if original is not None and created is not None and 0 <= created - original <= REVERT_WINDOW_S:
                hit.add(int(ref))
    return hit


def _reconcile_repo(repo: str, since: float, until: float, *, approved: set, asked: set, runner: Runner,
                    span_days: int, check_comments: bool, sleep) -> dict:
    """One repository's counts. Raises on a gh failure so a half-read repository is never added to the totals."""
    by_tier = {"L1": 0, "L2": 0, "L3": 0}
    closed = {"merged": 0, "unmerged": 0}
    l1 = {"total": 0, "merged": 0, "closed_unmerged": 0, "changes_requested": 0, "hold_comment": 0, "reverted": 0,
          "asked": 0, "clean": 0, "refused": [], "reverted_prs": []}
    cover = {"merged": 0, "in_ledger": 0, "outside_ledger": 0, "outside_by_bot": 0, "l1_outside_ledger": 0}
    prs = list_closed(repo, since, until, runner=runner, span_days=span_days, sleep=sleep)
    l1_merged_at: dict = {}
    l1_prs: list = []
    for pr in prs:
        tier, merged, key = path_tier(pr), pr["state"] == "merged", (repo, pr.get("number"))
        by_tier[tier] += 1
        closed["merged" if merged else "unmerged"] += 1
        if merged:
            cover["merged"] += 1
            if key in approved:
                cover["in_ledger"] += 1
            else:
                cover["outside_ledger"] += 1
                cover["outside_by_bot"] += 1 if _is_bot(pr) else 0
                cover["l1_outside_ledger"] += 1 if tier == "L1" else 0
        if tier == "L1":
            l1_prs.append((pr, merged, key))
            if merged:
                l1_merged_at[int(pr["number"])] = pr["when"]
    reverted = reverted_prs(repo, since, l1_merged_at, runner=runner, sleep=sleep) if l1_merged_at else set()
    for pr, merged, key in l1_prs:
        changes = any(isinstance(rv, dict) and rv.get("state") == "CHANGES_REQUESTED" for rv in pr.get("reviews") or [])
        hold = has_hold_comment(repo, int(pr["number"]), runner=runner, sleep=sleep) if check_comments else False
        l1["total"] += 1
        l1["merged" if merged else "closed_unmerged"] += 1
        l1["changes_requested"] += 1 if changes else 0
        l1["hold_comment"] += 1 if hold else 0
        l1["asked"] += 1 if key in asked else 0
        if not merged or changes or hold:
            l1["refused"].append(f"{repo}#{pr['number']}")
        else:
            l1["clean"] += 1
        if int(pr["number"]) in reverted:
            l1["reverted"] += 1
            l1["reverted_prs"].append(f"{repo}#{pr['number']}")
    return {"by_tier": by_tier, "closed": closed, "l1": l1, "coverage": cover}


def _add(total: dict, part: dict) -> None:
    for key, value in part.items():
        if isinstance(value, list):
            total.setdefault(key, []).extend(value)
        else:
            total[key] = total.get(key, 0) + value


def reconcile(
    repos: Iterable[str], since: float, until: float, *, ledger_rows: Optional[list] = None, runner: Runner = _run,
    span_days: int = 4, check_comments: bool = True, sleep: Callable[[float], None] = time.sleep,
) -> dict:
    ledger_rows = ledger_rows or []
    approved = {(r.get("repo"), r.get("pr")) for r in ledger_rows if shadow.row_kind(r) == "approve"}
    asked = {(r.get("repo"), r.get("pr")) for r in ledger_rows if shadow.row_kind(r) == "ask"}
    totals: dict = {
        "by_tier": {"L1": 0, "L2": 0, "L3": 0}, "closed": {"merged": 0, "unmerged": 0},
        "l1": {"total": 0, "merged": 0, "closed_unmerged": 0, "changes_requested": 0, "hold_comment": 0, "reverted": 0,
               "asked": 0, "clean": 0, "refused": [], "reverted_prs": []},
        "coverage": {"merged": 0, "in_ledger": 0, "outside_ledger": 0, "outside_by_bot": 0, "l1_outside_ledger": 0},
    }
    errors: list = []
    done_repos: list = []
    for repo in sorted(set(repos)):
        try:
            part = _reconcile_repo(repo, since, until, approved=approved, asked=asked, runner=runner,
                                   span_days=span_days, check_comments=check_comments, sleep=sleep)
        except RuntimeError as exc:
            errors.append({"repo": repo, "reason": str(exc)})
            continue
        for section in totals:
            _add(totals[section], part[section])
        done_repos.append(repo)
    l1 = totals["l1"]
    l1["refused"].sort()
    l1["reverted_prs"].sort()
    clean = l1.pop("clean")
    # Spec §10.2 5-b: docs/test-only PRs that were merged with no refusal signal, over all of them.
    l1["approved_as_asked_rate"] = round(clean / l1["total"], 4) if l1["total"] else None
    return {
        "schema": SCHEMA_RECONCILE, "since": _iso(since), "until": _iso(until),
        "repos": done_repos, "errors": errors, "complete": not errors,
        "closed": totals["closed"], "by_tier": totals["by_tier"], "l1": l1, "coverage": totals["coverage"],
        "comments_checked": bool(check_comments),
    }


# ---- CLI -------------------------------------------------------------------

def _read_ledgers(paths: Optional[list]) -> list:
    return [row for path in (paths or []) for row in shadow.read_ledger(Path(path))]


def cmd_collect(args: argparse.Namespace) -> int:
    nodes = list(args.node or []) or os.environ.get("CCC_FLEET_NODES", "").split()
    bad = [n for n in nodes if not NODE_RE.fullmatch(n)]
    if not nodes or bad:
        print("delegated-approval collect: pass --node NAME (repeatable) or set CCC_FLEET_NODES; "
              f"names are letters, digits, '.', '_' and '-'{' — rejected: ' + ', '.join(bad) if bad else ''}", file=sys.stderr)
        return 2
    report = collect(
        nodes, Path(args.out), self_node=args.self_node or os.environ.get("CCC_FLEET_SELF", ""),
        ssh=shlex.split(os.environ.get("CCC_FLEET_SSH", "ssh")), timeout=args.timeout,
        state_dir=Path(args.state_dir) if args.state_dir else None,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["complete"] else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    try:
        since, until = shadow.parse_when(args.since), shadow.parse_when(args.until)
    except ValueError as exc:
        print(f"delegated-approval reconcile: {exc}", file=sys.stderr)
        return 2
    rows = _read_ledgers(args.ledger)
    repos = list(args.repo or []) or sorted({str(r.get("repo")) for r in rows if r.get("repo")})
    bad = [r for r in repos if not REPO_RE.fullmatch(r)]
    if not repos or bad or until <= since:
        print("delegated-approval reconcile: need --repo jinwon-int/<repo> (or --ledger files to take repos from) "
              "and --until later than --since", file=sys.stderr)
        return 2
    report = reconcile(repos, since, until, ledger_rows=rows, span_days=args.span_days, check_comments=not args.no_comments)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["complete"] else 1


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="copy every node's shadow ledger into one directory (one read-only ssh per node)")
    c.add_argument("--out", required=True, help="directory for <node>.jsonl (0700, files 0600)")
    c.add_argument("--node", action="append", help="ssh target, repeatable (default: CCC_FLEET_NODES)")
    c.add_argument("--self-node", help="read this node's ledger locally (default: CCC_FLEET_SELF)")
    c.add_argument("--timeout", type=int, default=15)
    c.add_argument("--state-dir")
    r = sub.add_parser("reconcile", help="what GitHub shows for the window: closed PRs by tier, and L1 refusals")
    r.add_argument("--since", required=True, help="window start, ISO-8601 with offset")
    r.add_argument("--until", required=True, help="window end (exclusive), ISO-8601 with offset")
    r.add_argument("--repo", action="append", help="jinwon-int/<repo>, repeatable (default: the repos in --ledger)")
    r.add_argument("--ledger", action="append", help="collected ledger file, repeatable (coverage and default repos)")
    r.add_argument("--span-days", type=int, default=4, help="search the window in pieces this long")
    r.add_argument("--no-comments", action="store_true", help="skip the per-PR ⛔ hold comment check")
    args = ap.parse_args(argv)
    return cmd_collect(args) if args.cmd == "collect" else cmd_reconcile(args)


if __name__ == "__main__":
    raise SystemExit(main())
