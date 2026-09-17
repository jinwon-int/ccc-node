#!/usr/bin/env python3
"""A lineage is decided by its LATEST verdict, never by any verdict it has had.

Field case, 2026-09-17. The deferral backfill recorded every historical
`revise_author_offline` skip without asking whether that lineage had since
been decided. A deferral does not expire, so six of the sixteen the substitute
gate then called due were already settled — five `approve`, one `reject`:

    #81 broker-result-validation
      2026-08-31T03:35:44Z  revise verdict
      2026-08-31T03:35:48Z  revise dispatch skipped (author offline)
      2026-09-03 / 09-04    revise verdict x2
      2026-09-10T13:35:42Z  approve            <- lineage decided
      2026-09-16T23:29:04Z  deferral row written, backdated to 08-31

Dispatching a revise there pushes a new head onto the intake branch and
invalidates the very `approve` verdict bound to the old one. On a `reject` it
bypasses the owner decision `policies/REVIEW.md` reserves. The gate was
stopped by hand before it first fired; these tests are what keeps it stopped.

The reverse order matters too and is not hypothetical: intake #26 recorded
`approve` on 2026-08-30 and `revise` on 2026-09-03. Anything that answers "is
this approved?" by finding an approve verdict says yes to both #26 and #81,
and is wrong about #26.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import sys
import types
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "promotion_lineage_state_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

TREE = "a" * 64
HEAD = "b" * 40


def iso(*, days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def dispatch_row(task: str, pr: str) -> dict:
    return {"kind": "a2a-dispatch", "dispatched_task": task,
            "pr_url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}",
            "ts": iso(days_ago=40)}


def verdict_row(task: str, verdict: str, *, days_ago: int) -> dict:
    return {"kind": "a2a-verdict", "task_id": task, "verdict": verdict,
            "ts": iso(days_ago=days_ago)}


def history(pr: str, *pairs: tuple[str, int]) -> list[dict]:
    """One lineage's review history as (verdict, days_ago) rounds."""
    rows: list[dict] = []
    for index, (verdict, days_ago) in enumerate(pairs):
        task = f"skills_intake_review-pr{pr}-gwakga-r{index}"
        rows += [dispatch_row(task, pr), verdict_row(task, verdict, days_ago=days_ago)]
    return rows


def deferral_row(pr: str, node: str, name: str, *, skipped_days_ago: int) -> dict:
    """An author-offline deferral as _record_deferred_revise writes it."""
    return {"kind": "a2a-revise-deferred", "code": "revise_author_offline",
            "pr": pr, "node": node, "name": name, "head_sha": HEAD,
            "findings": [{"severity": "minor", "note": "n"}],
            "dispatched_task": f"skills_intake_review-pr{pr}-gwakga-r0",
            "ts": iso(days_ago=1), "skipped_at": iso(days_ago=skipped_days_ago)}


class LatestVerdictTests(unittest.TestCase):
    def test_last_verdict_wins(self) -> None:
        rows = history("81", ("revise", 17), ("revise", 14), ("approve", 7))
        self.assertEqual(promotion._latest_verdicts_by_pr(rows), {"81": "approve"})

    def test_approve_then_revise_is_revise(self) -> None:
        """Intake #26's real order."""
        rows = history("26", ("approve", 18), ("revise", 14))
        self.assertEqual(promotion._latest_verdicts_by_pr(rows), {"26": "revise"})

    def test_timestamp_order_beats_row_order(self) -> None:
        """Ledger append order is not verdict order after a backfill."""
        rows = history("81", ("approve", 7)) + history("81", ("revise", 17))
        self.assertEqual(promotion._latest_verdicts_by_pr(rows), {"81": "approve"})

    def test_verdict_without_a_dispatch_is_dropped(self) -> None:
        self.assertEqual(
            promotion._latest_verdicts_by_pr(
                [verdict_row("orphan", "approve", days_ago=1)]), {})


class ResolvedLineageTests(unittest.TestCase):
    def test_approve_and_reject_are_terminal(self) -> None:
        rows = history("81", ("approve", 7)) + history("90", ("reject", 7))
        self.assertEqual(promotion._resolved_lineage_prs(rows), {"81", "90"})

    def test_revise_is_not_terminal(self) -> None:
        rows = history("26", ("approve", 18), ("revise", 14))
        self.assertEqual(promotion._resolved_lineage_prs(rows), set())


class ApproveLineageTests(unittest.TestCase):
    def test_revise_then_approve_counts_from_the_first_approve(self) -> None:
        """Re-approving after a revision round must not reset a clock that has
        already been running — the promotion has been owed since day one."""
        rows = history("81", ("revise", 20), ("approve", 9), ("approve", 2))
        self.assertEqual(promotion._approve_lineage_prs(rows), {"81": iso(days_ago=9)})

    def test_approve_then_revise_is_not_approved(self) -> None:
        rows = history("26", ("approve", 18), ("revise", 14))
        self.assertEqual(promotion._approve_lineage_prs(rows), {})

    def test_rejected_lineage_is_not_approved(self) -> None:
        rows = history("90", ("revise", 20), ("reject", 7))
        self.assertEqual(promotion._approve_lineage_prs(rows), {})


class DeferredSweepFilterTests(unittest.TestCase):
    def due(self, rows):
        return [r["pr"] for r in
                promotion._deferred_revise_due(rows, datetime.now(timezone.utc))]

    def test_open_lineage_is_still_due(self) -> None:
        rows = history("70", ("revise", 17)) + [
            deferral_row("70", "gwakga", "s1", skipped_days_ago=17)]
        self.assertEqual(self.due(rows), ["70"])

    def test_approved_lineage_is_not_retried(self) -> None:
        rows = history("81", ("revise", 17), ("approve", 7)) + [
            deferral_row("81", "gwakga", "broker-result-validation",
                         skipped_days_ago=17)]
        self.assertEqual(self.due(rows), [])

    def test_rejected_lineage_is_not_retried(self) -> None:
        """policies/REVIEW.md reserves a reject for an owner decision."""
        rows = history("90", ("revise", 17), ("reject", 7)) + [
            deferral_row("90", "gwakga", "conflicted-pr-superceding-commit-check",
                         skipped_days_ago=17)]
        self.assertEqual(self.due(rows), [])


class SubstituteGateFilterTests(unittest.TestCase):
    def config(self, days: int = 7):
        return types.SimpleNamespace(revise_substitute_after_days=days)

    def test_open_lineage_is_due(self) -> None:
        rows = history("70", ("revise", 17)) + [
            deferral_row("70", "gwakga", "s1", skipped_days_ago=17)]
        self.assertTrue(
            promotion._revise_substitute_due(self.config(), rows, "gwakga", "s1"))

    def test_approved_lineage_is_never_due(self) -> None:
        """The five that would have been re-worked on 2026-09-17."""
        rows = history("81", ("revise", 17), ("approve", 7)) + [
            deferral_row("81", "gwakga", "broker-result-validation",
                         skipped_days_ago=17)]
        self.assertFalse(
            promotion._revise_substitute_due(
                self.config(), rows, "gwakga", "broker-result-validation"))

    def test_rejected_lineage_is_never_due(self) -> None:
        rows = history("90", ("revise", 17), ("reject", 7)) + [
            deferral_row("90", "gwakga", "conflicted-pr-superceding-commit-check",
                         skipped_days_ago=17)]
        self.assertFalse(
            promotion._revise_substitute_due(
                self.config(), rows, "gwakga",
                "conflicted-pr-superceding-commit-check"))

    def test_one_resolved_lineage_does_not_mask_an_open_one(self) -> None:
        """Both belong to gwakga; only the undecided one may fire."""
        rows = (history("81", ("revise", 17), ("approve", 7))
                + history("70", ("revise", 17))
                + [deferral_row("81", "gwakga", "resolved", skipped_days_ago=17),
                   deferral_row("70", "gwakga", "open", skipped_days_ago=17)])
        self.assertFalse(
            promotion._revise_substitute_due(self.config(), rows, "gwakga", "resolved"))
        self.assertTrue(
            promotion._revise_substitute_due(self.config(), rows, "gwakga", "open"))

    def test_still_off_when_threshold_is_zero(self) -> None:
        rows = history("70", ("revise", 17)) + [
            deferral_row("70", "gwakga", "s1", skipped_days_ago=17)]
        self.assertFalse(
            promotion._revise_substitute_due(self.config(0), rows, "gwakga", "s1"))


if __name__ == "__main__":
    unittest.main(verbosity=0)
