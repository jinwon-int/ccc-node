#!/usr/bin/env python3
"""fleet_merge_metrics.py — merge-pipeline metrics for jinwon-int repos (read-only `gh api`).

Why (ccc-node #2302, Wiki DOC-3932): the fleet's bottleneck is not git but the
merge pipeline — merge queue serialisation, CI retries and approval waits. This
measures it so changes (e.g. merge-queue batching) are judged by numbers, not
by feel.

Per repo over a window of N days:
  - merged PRs, per day, cycle time (created → merged) median / p90
  - merge queue: queued PRs, wait (last ADDED_TO_MERGE_QUEUE → merged) median / p90,
    removal events (landing also emits one, so evictions ≈ removals − queued)
  - merge-group batching: groups (speculative `gh-readonly-queue/<base>/pr-N-<sha>`
    refs, grouped by sha), effective batch = merged PRs / groups, group wall time
  - workflow runs by event (pull_request / merge_group / push / …): count,
    minutes, failure rate, duration median / p90

Caveats printed with the data: `/actions/runs` lists at most 1,000 runs per
query, so run counts and minutes are lower bounds on busy repos; GraphQL may
return 502 on very large repos (reported per repo, other repos continue).

Usage:
  fleet_merge_metrics.py [--repos a,b,c] [--days 30] [--out-dir DIR] [--markdown-only]
Outputs `merge-metrics-<KST stamp>.json` and `.md` in --out-dir (default cwd) and
prints the Markdown table. Needs an authenticated `gh` with read access.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import sys
from collections import defaultdict

OWNER = "jinwon-int"
DEFAULT_REPOS = ["ccc-node", "family-messenger", "a2a-nexus", "fleet-skills", "seoyoon-family-wiki", "wiki-agent", "piri"]
RUNS_LIST_CAP = 1000
QUEUE_REF = re.compile(r"gh-readonly-queue/[^/]+/pr-(\d+)-([0-9a-f]+)")
KST = dt.timezone(dt.timedelta(hours=9))

PR_QUERY = """
query($o:String!,$r:String!,$after:String){repository(owner:$o,name:$r){pullRequests(states:MERGED,first:50,after:$after,orderBy:{field:UPDATED_AT,direction:DESC}){
  pageInfo{hasNextPage endCursor}
  nodes{number createdAt mergedAt author{login}
    timelineItems(itemTypes:[ADDED_TO_MERGE_QUEUE_EVENT,REMOVED_FROM_MERGE_QUEUE_EVENT],first:20){nodes{__typename ... on AddedToMergeQueueEvent{createdAt} ... on RemovedFromMergeQueueEvent{createdAt}}}}}}}"""


def iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {r.stderr.strip()[:300]}")
    return r.stdout


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    return xs[max(0, min(len(xs) - 1, round((len(xs) - 1) * p)))]


def _r1(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


# ---------------------------------------------------------------- fetch (network)

def fetch_merged_prs(repo: str, since: dt.datetime) -> list[dict]:
    """Merged PRs with mergedAt >= since. UPDATED_AT order can interleave older merges,
    so paging stops only once a whole page is older than the window."""
    out: list[dict] = []
    after = None
    while True:
        args = ["api", "graphql", "-f", f"query={PR_QUERY}", "-F", f"o={OWNER}", "-F", f"r={repo}"]
        if after:
            args += ["-F", f"after={after}"]
        page = json.loads(gh(*args))["data"]["repository"]["pullRequests"]
        nodes = page["nodes"]
        out.extend(n for n in nodes if iso(n["mergedAt"]) >= since)
        if not page["pageInfo"]["hasNextPage"] or (nodes and all(iso(n["mergedAt"]) < since for n in nodes)):
            return out
        after = page["pageInfo"]["endCursor"]


def fetch_runs(repo: str, since: dt.datetime) -> list[dict]:
    raw = gh("api", "--paginate", f"repos/{OWNER}/{repo}/actions/runs?per_page=100&created=>={since.date().isoformat()}",
             "--jq", ".workflow_runs[] | {event,conclusion,run_started_at,updated_at,head_branch}")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


# ---------------------------------------------------------------- pure aggregation

def summarize_prs(prs: list[dict], days: int) -> dict:
    cycle = [(iso(p["mergedAt"]) - iso(p["createdAt"])).total_seconds() / 60 for p in prs]
    waits: list[float] = []
    removals = queued = 0
    for p in prs:
        items = p.get("timelineItems", {}).get("nodes", [])
        adds = sorted(iso(e["createdAt"]) for e in items if e["__typename"] == "AddedToMergeQueueEvent")
        removals += sum(1 for e in items if e["__typename"] == "RemovedFromMergeQueueEvent")
        if adds:
            queued += 1
            waits.append((iso(p["mergedAt"]) - adds[-1]).total_seconds() / 60)
    return {
        "merged_prs": len(prs), "merged_per_day": round(len(prs) / days, 2) if days else None,
        "authors": sorted({(p.get("author") or {}).get("login", "?") for p in prs}),
        "cycle_min_median": _r1(statistics.median(cycle)) if cycle else None, "cycle_min_p90": _r1(pct(cycle, 0.9)),
        "queued_prs": queued, "queue_wait_min_median": _r1(statistics.median(waits)) if waits else None,
        "queue_wait_min_p90": _r1(pct(waits, 0.9)), "queue_removals": removals,
        "queue_evictions_est": max(0, removals - queued),
    }


def summarize_runs(runs: list[dict]) -> dict:
    by_ev: dict[str, dict] = defaultdict(lambda: {"runs": 0, "minutes": 0.0, "failed": 0, "durations": []})
    for r in runs:
        if not r.get("run_started_at") or not r.get("updated_at"):
            continue
        d = max(0.0, (iso(r["updated_at"]) - iso(r["run_started_at"])).total_seconds() / 60)
        ev = r["event"] if r["event"] in ("pull_request", "merge_group", "push", "workflow_dispatch", "schedule") else "other"
        b = by_ev[ev]
        b["runs"] += 1
        b["minutes"] += d
        b["durations"].append(d)
        b["failed"] += r.get("conclusion") in ("failure", "timed_out", "cancelled")
    summary = {}
    for ev, b in by_ev.items():
        summary[ev] = {"runs": b["runs"], "minutes": round(b["minutes"], 1), "failed": b["failed"],
                       "fail_rate": round(b["failed"] / b["runs"], 3) if b["runs"] else None,
                       "median_min": _r1(statistics.median(b["durations"])) if b["durations"] else None,
                       "p90_min": _r1(pct(b["durations"], 0.9))}
    return {"runs_total": len(runs), "runs_capped": len(runs) >= RUNS_LIST_CAP,
            "ci_minutes_total": round(sum(b["minutes"] for b in by_ev.values()), 1), "by_event": summary}


def summarize_groups(runs: list[dict], merged_prs: int) -> dict:
    """Merge-queue groups from merge_group runs' head refs (grouped by the speculative sha)."""
    groups: dict[str, dict] = defaultdict(lambda: {"runs": 0, "failed": 0, "start": None, "end": None})
    for r in runs:
        if r.get("event") != "merge_group":
            continue
        m = QUEUE_REF.search(r.get("head_branch") or "")
        if not m:
            continue
        g = groups[m.group(2)]
        g["runs"] += 1
        g["failed"] += r.get("conclusion") in ("failure", "timed_out", "cancelled")
        s, e = r.get("run_started_at"), r.get("updated_at")
        if s and (g["start"] is None or s < g["start"]):
            g["start"] = s
        if e and (g["end"] is None or e > g["end"]):
            g["end"] = e
    walls = [(iso(g["end"]) - iso(g["start"])).total_seconds() / 60 for g in groups.values() if g["start"] and g["end"]]
    return {"groups": len(groups), "effective_batch": round(merged_prs / len(groups), 2) if groups else None,
            "group_wall_min_median": _r1(statistics.median(walls)) if walls else None,
            "group_wall_min_p90": _r1(pct(walls, 0.9)),
            "groups_with_failed_run": sum(1 for g in groups.values() if g["failed"])}


def summarize(repo: str, prs: list[dict], runs: list[dict], days: int, since: dt.datetime) -> dict:
    out = {"repo": repo, "days": days, "since": since.isoformat()}
    out.update(summarize_prs(prs, days))
    out.update(summarize_runs(runs))
    out.update(summarize_groups(runs, out["merged_prs"]))
    return out


def render_markdown(results: list[dict], days: int) -> str:
    head = (f"| 레포 | 머지 PR/{days}일 | /일 | PR 사이클 중앙/p90(분) | 큐 PR | 큐 대기 중앙/p90(분) | 큐 그룹 | 실효 배치 "
            "| 그룹 wall 중앙/p90(분) | 축출≈ | CI run | CI 분 | PR run 실패율 |")
    lines = [head, "|---|---:|---:|---|---:|---|---:|---:|---|---:|---:|---:|---:|"]
    tot_prs = tot_min = 0.0
    for r in results:
        if "error" in r:
            lines.append(f"| {r['repo']} | 오류 | | {r['error'][:60]} | | | | | | | | | |")
            continue
        tot_prs += r["merged_prs"]
        tot_min += r["ci_minutes_total"]
        pr_ev = r["by_event"].get("pull_request", {})
        cap = "≥" if r["runs_capped"] else ""
        lines.append(
            f"| {r['repo']} | {r['merged_prs']} | {r['merged_per_day']} | {r['cycle_min_median']}/{r['cycle_min_p90']} "
            f"| {r['queued_prs']} | {r['queue_wait_min_median']}/{r['queue_wait_min_p90']} | {r['groups']} | {r['effective_batch']} "
            f"| {r['group_wall_min_median']}/{r['group_wall_min_p90']} | {r['queue_evictions_est']} | {cap}{r['runs_total']} "
            f"| {cap}{r['ci_minutes_total']} | {pr_ev.get('fail_rate')} |")
    lines.append(f"| **합계** | **{int(tot_prs)}** | **{round(tot_prs / days, 1) if days else '-'}** | | | | | | | | | **≥{round(tot_min)}** | |")
    lines.append("")
    lines.append(f"측정 {dt.datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')} · `/actions/runs` 1,000건 상한(≥ 표시) · 축출≈제거 이벤트−큐 PR(랜딩도 제거 이벤트를 남김)")
    return "\n".join(lines)


# ---------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repos", default=",".join(DEFAULT_REPOS), help="comma-separated repo names under jinwon-int")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--markdown-only", action="store_true", help="print the table only; do not write files")
    args = ap.parse_args(argv)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).replace(microsecond=0)
    results = []
    for repo in [r.strip() for r in args.repos.split(",") if r.strip()]:
        try:
            prs = fetch_merged_prs(repo, since)
            runs = fetch_runs(repo, since)
            s = summarize(repo, prs, runs, args.days, since)
        except Exception as e:  # noqa: BLE001 — one repo's API failure must not hide the others
            s = {"repo": repo, "error": str(e)}
        results.append(s)
        print(f"[{dt.datetime.now(KST).strftime('%H:%M:%S')}] {repo}: {s.get('merged_prs', s.get('error'))} PRs, "
              f"{s.get('runs_total', '-')} runs, batch {s.get('effective_batch', '-')}", file=sys.stderr, flush=True)
    md = render_markdown(results, args.days)
    if not args.markdown_only:
        stamp = dt.datetime.now(KST).strftime("%Y%m%d-%H%M")
        with open(f"{args.out_dir}/merge-metrics-{stamp}.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        with open(f"{args.out_dir}/merge-metrics-{stamp}.md", "w", encoding="utf-8") as f:
            f.write(md + "\n")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
