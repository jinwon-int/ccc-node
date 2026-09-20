#!/usr/bin/env python3
"""Opt-in auto-drain: supersede-close older intake PRs, merge mechanical promote PRs.

Default OFF — a publisher that has not opted in must behave exactly as before.
Reject lineages stay open (policies/REVIEW.md). Identity-hit promote batches
stay draft for a human.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_auto_drain_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

TREE_A = "a" * 64
TREE_B = "b" * 64


def iso(*, days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def publish_row(pr: str, tree: str, name: str = "s1", node: str = "gwakga") -> dict:
    return {
        "ts": iso(days_ago=40),
        "outcome": "pr-opened",
        "branch": f"skill-intake/{node}/{name}-claude-{tree[:12]}",
        "name": name,
        "node": node,
        "provider": "claude",
        "source": node,
        "state": "OPEN",
        "tree_sha256": tree,
        "url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}",
    }


def dispatch_row(task: str, pr: str) -> dict:
    return {
        "kind": "a2a-dispatch",
        "dispatched_task": task,
        "pr_url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}",
        "ts": iso(days_ago=40),
    }


def verdict_row(task: str, verdict: str) -> dict:
    return {
        "kind": "a2a-verdict",
        "task_id": task,
        "verdict": verdict,
        "ts": iso(days_ago=9),
    }


def state_row(pr: str, state: str) -> dict:
    return {"ts": iso(days_ago=1), "kind": "a2a-intake-state", "pr": pr, "state": state}


class SupersedeSweepTests(unittest.TestCase):
    def sweep(self, rows, *, dry_run=False, enabled=True, window=32, close_fails=False):
        written: list[dict] = []
        closed: list[str] = []

        def fake_run(args, **kwargs):
            if args[:3] == ["gh", "pr", "close"]:
                if close_fails:
                    raise promotion.PromotionError("close_denied")
                closed.append(args[3])
            return types.SimpleNamespace(stdout=b"{}")

        cfg = types.SimpleNamespace(
            node="seoseo",
            repo="jinwon-int/fleet-skills",
            collect_window=window,
            supersede_autoclose_enabled=enabled,
        )
        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_append_ledger",
                          side_effect=lambda _c, row: written.append(row)), \
             patch.object(promotion, "_run", side_effect=fake_run):
            out = promotion._sweep_superseded_intakes(cfg, dry_run=dry_run)
        return out, written, closed

    def test_disabled_by_default_does_nothing(self) -> None:
        rows = [publish_row("10", TREE_A), publish_row("20", TREE_B)]
        out, written, closed = self.sweep(rows, enabled=False)
        self.assertEqual((out, written, closed), ([], [], []))

    def test_newer_tree_closes_the_older_pr(self) -> None:
        rows = [publish_row("10", TREE_A), publish_row("20", TREE_B)]
        out, written, closed = self.sweep(rows)
        self.assertEqual(closed, ["10"])
        self.assertEqual([r["outcome"] for r in out], ["intake-superseded-closed"])
        self.assertEqual(out[0]["superseded_by"], "20")
        self.assertEqual(written[0]["kind"], "a2a-intake-superseded")
        self.assertEqual(written[1]["state"], "CLOSED")

    def test_single_pr_lineage_is_left_alone(self) -> None:
        out, _, closed = self.sweep([publish_row("10", TREE_A)])
        self.assertEqual((out, closed), ([], []))

    def test_already_closed_old_pr_is_skipped(self) -> None:
        rows = [
            publish_row("10", TREE_A),
            publish_row("20", TREE_B),
            state_row("10", "CLOSED"),
        ]
        out, _, closed = self.sweep(rows)
        self.assertEqual((out, closed), ([], []))

    def test_reject_lineage_is_never_closed(self) -> None:
        task = "skills_intake_review-pr10-gwakga-x"
        rows = [
            publish_row("10", TREE_A),
            publish_row("20", TREE_B),
            dispatch_row(task, "10"),
            verdict_row(task, "reject"),
        ]
        out, _, closed = self.sweep(rows)
        self.assertEqual((out, closed), ([], []))

    def test_different_names_are_not_one_lineage(self) -> None:
        rows = [
            publish_row("10", TREE_A, name="alpha"),
            publish_row("20", TREE_B, name="beta"),
        ]
        out, _, closed = self.sweep(rows)
        self.assertEqual((out, closed), ([], []))

    def test_dry_run_closes_nothing(self) -> None:
        rows = [publish_row("10", TREE_A), publish_row("20", TREE_B)]
        out, written, closed = self.sweep(rows, dry_run=True)
        self.assertEqual((written, closed), ([], []))
        self.assertEqual([r["outcome"] for r in out], ["would-close-superseded-intake"])

    def test_close_failure_writes_no_ledger_row(self) -> None:
        rows = [publish_row("10", TREE_A), publish_row("20", TREE_B)]
        out, written, closed = self.sweep(rows, close_fails=True)
        self.assertEqual(written, [])
        self.assertEqual([r["outcome"] for r in out], ["close-failed"])

    def test_window_bounds_the_closes(self) -> None:
        rows = []
        for n in range(6):
            rows.append(publish_row(str(10 + n), f"{n}" * 64))
        out, _, closed = self.sweep(rows, window=2)
        self.assertEqual(len(closed), 2)


class PromoteMergeSweepTests(unittest.TestCase):
    def sweep(self, rows, *, view=None, dry_run=False, enabled=True, merge_fails=False):
        written: list[dict] = []
        calls: list[list[str]] = []
        payload = view or {
            "isDraft": True,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
            "state": "OPEN",
        }

        def fake_run(args, **kwargs):
            calls.append(list(args))
            if args[:3] == ["gh", "pr", "view"]:
                return types.SimpleNamespace(stdout=json.dumps(payload).encode())
            if args[:3] == ["gh", "pr", "merge"] and merge_fails:
                raise promotion.PromotionError("merge_denied")
            return types.SimpleNamespace(stdout=b"")

        cfg = types.SimpleNamespace(
            node="seoseo",
            repo="jinwon-int/fleet-skills",
            collect_window=32,
            auto_merge_promote_enabled=enabled,
        )
        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_append_ledger",
                          side_effect=lambda _c, row: written.append(row)), \
             patch.object(promotion, "_run", side_effect=fake_run):
            out = promotion._sweep_promote_merge(cfg, dry_run=dry_run)
        return out, written, calls

    def open_promote(self, *, hits=None):
        return {
            "kind": "a2a-promote-pr",
            "pr": "999",
            "url": "https://github.com/jinwon-int/fleet-skills/pull/999",
            "state": "OPEN",
            "identity_hits": hits or [],
        }

    def test_disabled_by_default_does_nothing(self) -> None:
        out, written, calls = self.sweep([self.open_promote()], enabled=False)
        self.assertEqual((out, written, calls), ([], [], []))

    def test_identity_hits_stay_draft(self) -> None:
        hits = [{"name": "s1", "hits": [{"line": 1, "kind": "node-name", "match": "gwakga"}]}]
        out, _, calls = self.sweep([self.open_promote(hits=hits)])
        self.assertEqual([r["outcome"] for r in out], ["needs-human-identity"])
        self.assertEqual(calls, [])

    def test_green_draft_is_marked_ready_and_merged(self) -> None:
        out, written, calls = self.sweep([self.open_promote()])
        self.assertEqual([r["outcome"] for r in out], ["promote-pr-merged"])
        self.assertEqual(written[0]["state"], "MERGED")
        verbs = [c[2] for c in calls if c[:2] == ["gh", "pr"]]
        self.assertEqual(verbs, ["view", "ready", "merge"])
        self.assertIn("--squash", calls[-1])
        self.assertNotIn("--admin", calls[-1])

    def test_pending_checks_do_not_merge(self) -> None:
        out, written, _ = self.sweep(
            [self.open_promote()],
            view={
                "isDraft": False,
                "mergeable": "UNKNOWN",
                "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [{"conclusion": "", "status": "IN_PROGRESS"}],
                "state": "OPEN",
            },
        )
        self.assertEqual([r["outcome"] for r in out], ["waiting-checks"])
        self.assertEqual(written, [])

    def test_failed_checks_do_not_merge(self) -> None:
        out, written, _ = self.sweep(
            [self.open_promote()],
            view={
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "UNSTABLE",
                "statusCheckRollup": [{"conclusion": "FAILURE", "status": "COMPLETED"}],
                "state": "OPEN",
            },
        )
        self.assertEqual([r["outcome"] for r in out], ["promote-pr-checks-failed"])
        self.assertEqual(written, [])

    def test_dry_run_merges_nothing(self) -> None:
        out, written, calls = self.sweep([self.open_promote()], dry_run=True)
        self.assertEqual((written, calls), ([], []))
        self.assertEqual([r["outcome"] for r in out], ["would-merge-promote-pr"])

    def test_latest_row_wins_so_merged_is_not_retried(self) -> None:
        rows = [
            self.open_promote(),
            {**self.open_promote(), "state": "MERGED"},
        ]
        out, _, calls = self.sweep(rows)
        self.assertEqual((out, calls), ([], []))

    def test_merge_failure_writes_no_merged_row(self) -> None:
        out, written, _ = self.sweep([self.open_promote()], merge_fails=True)
        self.assertEqual(written, [])
        self.assertEqual([r["outcome"] for r in out], ["merge-failed"])


if __name__ == "__main__":
    unittest.main(verbosity=0)
