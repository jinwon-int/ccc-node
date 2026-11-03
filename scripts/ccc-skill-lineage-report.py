#!/usr/bin/env python3
"""Read-only skill intake *lineage* report over the promotion ledger (#2641).

#2239 measured review quality as the share of ``a2a-verdict`` rows that said
``revise``. The second verdict (2026-11-02) showed why that number cannot be a
quality target: 48 of the 56 weekly revises were FIRST reviews and 35 of the 41
approves were REPUBLISHED PRs. The pipeline converges as
``revise -> a2a-revise-result -> republish -> approve``, so the verdict-level
revise share counts *rounds*, not outcomes, and "<40%" is unreachable by design.

This report regroups the same ledger rows by lineage — the chain of intake PRs
that carry one skill from its first review to a decision — and answers the two
questions #2641 replaces the old target with:

  * final approve rate   — lineages whose LATEST consumed verdict is ``approve``
  * mean rounds          — consumed verdicts per lineage

A lineage is the connected component of PR numbers under two link kinds the
ledger already writes: ``a2a-intake-superseded`` (``pr`` -> ``superseded_by``)
and ``a2a-revise-result`` with ``status=republished`` (``pr`` -> ``new_pr_url``).
Verdicts attach to a PR through the review task id
``skills_intake_review-pr<N>-<node>-<UTC stamp>``; ``<node>`` is the author
node the lineage is attributed to. Only ``status=consumed`` verdicts count as
rounds; ``malformed`` / ``infra_failed`` rows are reported separately, exactly
as the #2239 verdict comments did.

Cohort: a lineage belongs to the window when its FIRST consumed verdict falls in
``[--since, --until)``. Its outcome is read from every ledger row, so a cohort
that was still in flight at ``--until`` is judged by what happened afterwards
(pass a truncated ledger to get an as-of view). Outcomes:

  approved     latest consumed verdict is ``approve``
  rejected     latest consumed verdict is ``reject``
  dropped      latest verdict is ``revise`` and the newest revise-result after it
               is terminal (``invalid`` / ``drop-recommended`` / ``publish-failed``)
  in_progress  latest verdict is ``revise`` and nothing terminal has happened yet
               (includes ``failed`` / ``infra_failed`` revise results, which the
               dispatcher retries, and open arbitrations)

The old verdict-level revise share is still printed (``legacy_revise_ratio``)
so the two definitions can be compared on the same window. The script never
writes anything; it only reads the ledger and prints.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO

DEFAULT_LEDGER = Path.home() / ".claude" / "state" / "skill-promotion" / "ledger.jsonl"
ENV_LEDGER = "CCC_SKILL_PROMOTION_LEDGER"

# Current ids end in the UTC stamp; the 2026-08-30..09-10 generation appended a
# two-digit sequence and sometimes a ``-t<n>r<n>`` retry suffix
# (``…-ccc-node-20260831T011835Z01-t2r2``). Node slugs may contain ``-``, so the
# node group is greedy and the stamp anchors the split.
REVIEW_TASK_RE = re.compile(r"^skills_intake_review-pr(\d+)-(.+)-(\d{8}T\d{6}Z)\d*(?:-t\d+r\d+)?$")
PR_URL_RE = re.compile(r"/pull/(\d+)/?$")

CONSUMED = "consumed"
# Revise results after which no republish will follow without an operator.
TERMINAL_REVISE_RESULTS = frozenset({"invalid", "drop-recommended", "publish-failed"})
OUTCOMES = ("approved", "rejected", "dropped", "in_progress")


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(ts: datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def pr_from_url(url: Any) -> int | None:
    if not isinstance(url, str):
        return None
    match = PR_URL_RE.search(url.strip())
    return int(match.group(1)) if match else None


def pr_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, item: int) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: int) -> int:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Smaller PR number roots the component: deterministic and, since
            # intake PRs are opened in order, usually the original PR.
            lo, hi = sorted((ra, rb))
            self.parent[hi] = lo


def read_rows(stream: Iterable[str]) -> tuple[list[dict[str, Any]], int]:
    """Parse JSONL rows; return (rows, skipped_line_count). Never raises on a bad line."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def build_lineages(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Group ledger rows into lineages. Pure; no window applied yet."""
    uf = _UnionFind()
    verdicts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    revise_results: dict[int, list[dict[str, Any]]] = defaultdict(list)
    arbitrations: dict[int, list[dict[str, Any]]] = defaultdict(list)
    unparsed_verdict_tasks = 0

    for row in rows:
        kind = row.get("kind")
        ts = parse_ts(row.get("ts"))
        if kind == "a2a-verdict":
            task_id = row.get("task_id")
            match = REVIEW_TASK_RE.match(task_id) if isinstance(task_id, str) else None
            if not match or ts is None:
                unparsed_verdict_tasks += 1
                continue
            pr = int(match.group(1))
            uf.add(pr)
            verdicts[pr].append(
                {
                    "pr": pr,
                    "node": match.group(2),
                    "ts": ts,
                    "verdict": row.get("verdict"),
                    "status": row.get("status"),
                    "head_sha": row.get("head_sha"),
                }
            )
        elif kind == "a2a-intake-superseded":
            old, new = pr_number(row.get("pr")), pr_number(row.get("superseded_by"))
            if old is not None and new is not None:
                uf.union(old, new)
        elif kind == "a2a-revise-result":
            old = pr_number(row.get("pr"))
            if old is None:
                continue
            uf.add(old)
            new = pr_from_url(row.get("new_pr_url"))
            if row.get("status") == "republished" and new is not None:
                uf.union(old, new)
            revise_results[old].append(
                {"pr": old, "ts": ts, "status": row.get("status"), "code": row.get("code"), "new_pr": new}
            )
        elif kind == "a2a-arbitration":
            old = pr_number(row.get("pr"))
            if old is not None:
                uf.add(old)
                arbitrations[old].append({"pr": old, "ts": ts, "trigger": row.get("trigger")})

    members: dict[int, list[int]] = defaultdict(list)
    for pr in list(uf.parent):
        members[uf.find(pr)].append(pr)

    lineages: list[dict[str, Any]] = []
    for root, prs in members.items():
        prs.sort()
        consumed = sorted(
            (v for pr in prs for v in verdicts.get(pr, []) if v["status"] == CONSUMED),
            key=lambda v: (v["ts"], v["pr"]),
        )
        excluded = Counter(
            v["status"] for pr in prs for v in verdicts.get(pr, []) if v["status"] != CONSUMED
        )
        results = sorted(
            (r for pr in prs for r in revise_results.get(pr, []) if r["ts"] is not None),
            key=lambda r: r["ts"],
        )
        arbs = [a for pr in prs for a in arbitrations.get(pr, [])]
        entry: dict[str, Any] = {
            "root_pr": min(prs),
            "prs": prs,
            "rounds": len(consumed),
            "excluded": dict(excluded),
            "arbitrated": bool(arbs),
            "arbitration_triggers": sorted({str(a["trigger"]) for a in arbs if a.get("trigger")}),
        }
        if consumed:
            first, last = consumed[0], consumed[-1]
            entry.update(
                {
                    "node": first["node"],
                    "first_ts": first["ts"],
                    "final_ts": last["ts"],
                    "first_verdict": first["verdict"],
                    "final_verdict": last["verdict"],
                    "final_pr": last["pr"],
                    "outcome": _outcome(last, results),
                }
            )
        else:
            entry.update(
                {
                    "node": None,
                    "first_ts": None,
                    "final_ts": None,
                    "first_verdict": None,
                    "final_verdict": None,
                    "final_pr": None,
                    "outcome": None,
                }
            )
        lineages.append(entry)

    lineages.sort(key=lambda e: (e["first_ts"] or datetime.max.replace(tzinfo=timezone.utc), e["root_pr"]))
    return {
        "lineages": lineages,
        "verdicts": [v for vs in verdicts.values() for v in vs],
        "unparsed_verdict_tasks": unparsed_verdict_tasks,
    }


def _outcome(last: dict[str, Any], results: list[dict[str, Any]]) -> str:
    verdict = last["verdict"]
    if verdict == "approve":
        return "approved"
    if verdict == "reject":
        return "rejected"
    # revise (or an unexpected label): look at what the pipeline did next.
    later = [r for r in results if r["ts"] >= last["ts"]]
    if later and later[-1]["status"] in TERMINAL_REVISE_RESULTS:
        return "dropped"
    return "in_progress"


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _mean(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _bucket_metrics(entries: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(e["outcome"] for e in entries)
    decided = [e for e in entries if e["outcome"] in ("approved", "rejected", "dropped")]
    approved = outcomes.get("approved", 0)
    first_approve = sum(1 for e in entries if e["first_verdict"] == "approve")
    return {
        "lineages": len(entries),
        "outcomes": {k: outcomes.get(k, 0) for k in OUTCOMES},
        "decided": len(decided),
        "final_approve_rate_decided": _rate(approved, len(decided)),
        "final_approve_rate_all": _rate(approved, len(entries)),
        "first_review_approve_rate": _rate(first_approve, len(entries)),
        "mean_rounds_all": _mean([e["rounds"] for e in entries]),
        "mean_rounds_decided": _mean([e["rounds"] for e in decided]),
        "mean_rounds_approved": _mean([e["rounds"] for e in entries if e["outcome"] == "approved"]),
        "rounds_histogram": {str(k): v for k, v in sorted(Counter(e["rounds"] for e in entries).items())},
        "arbitrated": sum(1 for e in entries if e["arbitrated"]),
    }


def summarize(
    built: dict[str, Any],
    since: datetime,
    until: datetime,
    *,
    skipped_lines: int = 0,
) -> dict[str, Any]:
    lineages = built["lineages"]
    cohort = [e for e in lineages if e["first_ts"] is not None and since <= e["first_ts"] < until]
    no_consumed = [e for e in lineages if e["first_ts"] is None]

    window_verdicts = [v for v in built["verdicts"] if since <= v["ts"] < until]
    consumed = [v for v in window_verdicts if v["status"] == CONSUMED]
    verdict_counts = Counter(v["verdict"] for v in consumed)
    excluded_counts = Counter(v["status"] for v in window_verdicts if v["status"] != CONSUMED)

    by_node: dict[str, Any] = {}
    for node in sorted({e["node"] for e in cohort}):
        by_node[node] = _bucket_metrics([e for e in cohort if e["node"] == node])

    return {
        "window": {"since": iso(since), "until": iso(until)},
        "cohort_rule": "lineage first consumed verdict in [since, until); outcome from the whole ledger",
        "fleet": _bucket_metrics(cohort),
        "by_node": by_node,
        "legacy_verdict_window": {
            "consumed": len(consumed),
            "approve": verdict_counts.get("approve", 0),
            "revise": verdict_counts.get("revise", 0),
            "reject": verdict_counts.get("reject", 0),
            "legacy_revise_ratio": _rate(verdict_counts.get("revise", 0), len(consumed)),
            "excluded": dict(excluded_counts),
        },
        "ledger": {
            "lineages_total": len(lineages),
            "lineages_without_consumed_verdict": len(no_consumed),
            "unparsed_verdict_tasks": built["unparsed_verdict_tasks"],
            "skipped_lines": skipped_lines,
        },
        "lineages": [
            {
                **{k: v for k, v in e.items() if k not in ("first_ts", "final_ts")},
                "first_ts": iso(e["first_ts"]),
                "final_ts": iso(e["final_ts"]),
            }
            for e in cohort
        ],
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def render_text(summary: dict[str, Any], out: TextIO, *, show_lineages: bool) -> None:
    fleet = summary["fleet"]
    legacy = summary["legacy_verdict_window"]
    w = summary["window"]
    print(f"skill intake lineage report  window {w['since']} .. {w['until']}  (cohort = first consumed verdict)", file=out)
    print(
        f"lineages {fleet['lineages']}  decided {fleet['decided']}  "
        f"approved {fleet['outcomes']['approved']}  rejected {fleet['outcomes']['rejected']}  "
        f"dropped {fleet['outcomes']['dropped']}  in_progress {fleet['outcomes']['in_progress']}  "
        f"arbitrated {fleet['arbitrated']}",
        file=out,
    )
    print(
        f"final approve rate  decided {_pct(fleet['final_approve_rate_decided'])}  "
        f"all {_pct(fleet['final_approve_rate_all'])}   "
        f"first-review approve {_pct(fleet['first_review_approve_rate'])}",
        file=out,
    )
    print(
        f"mean rounds  all {_num(fleet['mean_rounds_all'])}  decided {_num(fleet['mean_rounds_decided'])}  "
        f"approved {_num(fleet['mean_rounds_approved'])}   histogram {fleet['rounds_histogram']}",
        file=out,
    )
    print(
        f"legacy verdict window  consumed {legacy['consumed']}  revise {legacy['revise']}  "
        f"approve {legacy['approve']}  reject {legacy['reject']}  "
        f"revise ratio {_pct(legacy['legacy_revise_ratio'])}  excluded {legacy['excluded']}",
        file=out,
    )
    if summary["by_node"]:
        print("", file=out)
        print(f"{'node':<12}{'lineages':>9}{'decided':>8}{'approved':>9}{'final%':>9}{'first%':>8}{'rounds':>8}{'in_prog':>8}", file=out)
        for node, m in summary["by_node"].items():
            print(
                f"{node:<12}{m['lineages']:>9}{m['decided']:>8}{m['outcomes']['approved']:>9}"
                f"{_pct(m['final_approve_rate_decided']):>9}{_pct(m['first_review_approve_rate']):>8}"
                f"{_num(m['mean_rounds_all']):>8}{m['outcomes']['in_progress']:>8}",
                file=out,
            )
    ledger = summary["ledger"]
    print("", file=out)
    print(
        f"ledger: lineages {ledger['lineages_total']}  without consumed verdict {ledger['lineages_without_consumed_verdict']}  "
        f"unparsed verdict task ids {ledger['unparsed_verdict_tasks']}  skipped lines {ledger['skipped_lines']}",
        file=out,
    )
    if show_lineages:
        print("", file=out)
        print(f"{'root':>6} {'node':<12}{'rounds':>6} {'first':<8}{'final':<8}{'outcome':<12}prs", file=out)
        for e in summary["lineages"]:
            prs = ",".join(str(p) for p in e["prs"])
            print(
                f"{e['root_pr']:>6} {e['node']:<12}{e['rounds']:>6} {e['first_verdict']:<8}{e['final_verdict']:<8}"
                f"{e['outcome']:<12}{prs}",
                file=out,
            )


def _open_ledger(path_arg: str | None) -> tuple[TextIO | None, str]:
    if path_arg == "-":
        return sys.stdin, "<stdin>"
    path = Path(path_arg or os.environ.get(ENV_LEDGER) or DEFAULT_LEDGER).expanduser()
    if not path.is_file():
        return None, str(path)
    return path.open("r", encoding="utf-8", errors="replace"), str(path)


def main(argv: list[str] | None = None, *, now: datetime | None = None, out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ledger", nargs="?", help=f"ledger.jsonl path ('-' = stdin; default ${ENV_LEDGER} or {DEFAULT_LEDGER})")
    parser.add_argument("--since", help="cohort window start, ISO-8601 UTC (default: --until minus --window-days)")
    parser.add_argument("--until", help="cohort window end, exclusive, ISO-8601 UTC (default: now)")
    parser.add_argument("--window-days", type=float, default=14.0, help="window length when --since is omitted (default 14)")
    parser.add_argument("--json", action="store_true", help="machine-readable output (includes per-lineage rows)")
    parser.add_argument("--lineages", action="store_true", help="text mode: also list every lineage in the cohort")
    args = parser.parse_args(argv)
    out = out or sys.stdout

    until = parse_ts(args.until) if args.until else (now or datetime.now(timezone.utc))
    if until is None:
        print(f"ccc-skill-lineage-report: --until is not an ISO-8601 timestamp: {args.until!r}", file=sys.stderr)
        return 64
    if args.since:
        since = parse_ts(args.since)
        if since is None:
            print(f"ccc-skill-lineage-report: --since is not an ISO-8601 timestamp: {args.since!r}", file=sys.stderr)
            return 64
    else:
        if args.window_days <= 0:
            print("ccc-skill-lineage-report: --window-days must be positive", file=sys.stderr)
            return 64
        since = until - timedelta(days=args.window_days)
    if since >= until:
        print("ccc-skill-lineage-report: --since must be before --until", file=sys.stderr)
        return 64

    stream, label = _open_ledger(args.ledger)
    if stream is None:
        print(f"ccc-skill-lineage-report: ledger not found: {label}", file=sys.stderr)
        return 66
    try:
        rows, skipped = read_rows(stream)
    finally:
        if stream is not sys.stdin:
            stream.close()

    summary = summarize(build_lineages(rows), since, until, skipped_lines=skipped)
    summary["ledger"]["path"] = label
    if args.json:
        json.dump(summary, out, ensure_ascii=False, indent=2, sort_keys=False)
        out.write("\n")
    else:
        render_text(summary, out, show_lineages=args.lineages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
