#!/usr/bin/env python3
"""Offline tests for fleet_merge_metrics aggregation (no network, no gh)."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_merge_metrics as m  # noqa: E402

T0 = dt.datetime(2026, 10, 1, 0, 0, tzinfo=dt.timezone.utc)


def ts(minutes: float) -> str:
    return (T0 + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(n: int, created: float, merged: float, enqueued: float | None = None, removals: int = 1) -> dict:
    items = []
    if enqueued is not None:
        items.append({"__typename": "AddedToMergeQueueEvent", "createdAt": ts(enqueued)})
        items += [{"__typename": "RemovedFromMergeQueueEvent", "createdAt": ts(merged)}] * removals
    return {"number": n, "createdAt": ts(created), "mergedAt": ts(merged), "author": {"login": "jinon86"},
            "timelineItems": {"nodes": items}}


def run(event: str, start: float, end: float, conclusion: str = "success", head: str = "main") -> dict:
    return {"event": event, "conclusion": conclusion, "run_started_at": ts(start), "updated_at": ts(end), "head_branch": head}


class PrSummaryTests(unittest.TestCase):
    def test_cycle_and_queue_wait(self):
        prs = [pr(1, 0, 40, enqueued=30), pr(2, 0, 20, enqueued=12), pr(3, 0, 10)]  # #3 merged without the queue
        s = m.summarize_prs(prs, days=10)
        self.assertEqual(s["merged_prs"], 3)
        self.assertEqual(s["merged_per_day"], 0.3)
        self.assertEqual(s["cycle_min_median"], 20.0)
        self.assertEqual(s["queued_prs"], 2)
        self.assertEqual(s["queue_wait_min_median"], 9.0)  # (10 + 8) / 2
        self.assertEqual(s["queue_removals"], 2)
        self.assertEqual(s["queue_evictions_est"], 0, "landing emits one removal per queued PR → no evictions")

    def test_eviction_estimate_counts_extra_removals(self):
        s = m.summarize_prs([pr(1, 0, 30, enqueued=5, removals=3)], days=1)
        self.assertEqual(s["queue_evictions_est"], 2)

    def test_last_enqueue_wins(self):
        p = pr(1, 0, 30, enqueued=5)
        p["timelineItems"]["nodes"].insert(0, {"__typename": "AddedToMergeQueueEvent", "createdAt": ts(20)})
        self.assertEqual(m.summarize_prs([p], days=1)["queue_wait_min_median"], 10.0)


class RunSummaryTests(unittest.TestCase):
    def test_by_event_and_failures(self):
        runs = [run("pull_request", 0, 2), run("pull_request", 0, 4, "failure"), run("merge_group", 0, 8),
                run("push", 0, 1), run("dynamic", 0, 1), {"event": "push", "conclusion": "success"}]  # last: no timestamps
        s = m.summarize_runs(runs)
        self.assertEqual(s["runs_total"], 6)
        self.assertFalse(s["runs_capped"])
        pr_ev = s["by_event"]["pull_request"]
        self.assertEqual((pr_ev["runs"], pr_ev["failed"], pr_ev["fail_rate"], pr_ev["median_min"]), (2, 1, 0.5, 3.0))
        self.assertEqual(s["by_event"]["other"]["runs"], 1)
        self.assertEqual(s["ci_minutes_total"], 16.0)

    def test_cap_flag(self):
        s = m.summarize_runs([run("push", 0, 1)] * m.RUNS_LIST_CAP)
        self.assertTrue(s["runs_capped"])


class GroupSummaryTests(unittest.TestCase):
    def test_groups_by_speculative_sha(self):
        g1, g2 = "gh-readonly-queue/main/pr-10-aaaa1111", "gh-readonly-queue/main/pr-12-bbbb2222"
        runs = [run("merge_group", 0, 5, head=g1), run("merge_group", 1, 7, head=g1),  # two workflows, one group
                run("merge_group", 10, 13, "failure", head=g2), run("pull_request", 0, 1, head="feat/x"),
                run("merge_group", 0, 1, head="weird-ref")]
        s = m.summarize_groups(runs, merged_prs=3)
        self.assertEqual(s["groups"], 2)
        self.assertEqual(s["effective_batch"], 1.5)
        self.assertEqual(s["group_wall_min_median"], 5.0)  # walls: 7 (0→7) and 3 → median 5
        self.assertEqual(s["groups_with_failed_run"], 1)

    def test_no_groups(self):
        s = m.summarize_groups([run("pull_request", 0, 1)], merged_prs=4)
        self.assertEqual((s["groups"], s["effective_batch"], s["group_wall_min_median"]), (0, None, None))


class RenderTests(unittest.TestCase):
    def test_markdown_has_rows_and_error_line(self):
        ok = m.summarize("r1", [pr(1, 0, 40, enqueued=30)], [run("merge_group", 0, 5, head="gh-readonly-queue/main/pr-1-cafe0001")], 30, T0)
        md = m.render_markdown([ok, {"repo": "r2", "error": "HTTP 502"}], 30)
        self.assertIn("| r1 | 1 |", md)
        self.assertIn("| r2 | 오류 |", md)
        self.assertIn("**합계** | **1**", md)

    def test_pct(self):
        self.assertEqual(m.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9), 9)
        self.assertIsNone(m.pct([], 0.5))


if __name__ == "__main__":
    unittest.main()
