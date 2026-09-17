#!/usr/bin/env python3
"""#1628 lane: recording whether an approved intake PR is still open.

An `approve` verdict ends the automated pipeline — signed receipt, verdict
comment, stop. Promotion itself is hand work with no tooling, so the ledger
carries no trace of it and "approved an hour ago" reads identically to
"approved and forgotten". `_sweep_intake_states` supplies the one missing
fact, the intake PR's own state, so that the offline doctor can age it.

These tests pin the join (a verdict has no PR of its own — it is attributed
through the dispatch row's `pr_url`, exactly as _process_verdicts does it),
the FIFO order under a bounded window, and the three ways this pass must
refuse to write: dry-run, an unreadable state, and an already-terminal PR.

The schema pin at the bottom is the point of the file. The B2 substitute gate
shipped dead for weeks because its fixtures invented a row shape production
never emitted (#1767) — green suite, dead feature. So the row this pass writes
is asserted field by field against what ccc_doctor actually reads back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_intake_state_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)


def iso(*, days_ago: int) -> str:
    """A timestamp in the exact format _utc_now writes to the ledger."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def config(collect_window: int = 32) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        node="seoseo", repo="jinwon-int/fleet-skills", collect_window=collect_window
    )


def dispatch_row(task: str, pr: str) -> dict:
    """An `a2a-dispatch` row with the keys the promoter actually writes.

    Pinned to production: the real row carries `pr_url` and `dispatched_task`
    and has NO `pr` field — that absence is the whole reason the join below
    has to go through the dispatch at all.
    """
    return {
        "kind": "a2a-dispatch",
        "dispatched_task": task,
        "task_id": task,
        "round_id": f"skills_intake_review-pr{pr}-auto-abcd1234-x",
        "reviewer_node": "yukson",
        "broker_id": "seoseo",
        "head_sha": "a" * 40,
        "transport_id": "t",
        "pr_url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}",
        "ts": iso(days_ago=40),
    }


def verdict_row(task: str, verdict: str, *, days_ago: int) -> dict:
    """An `a2a-verdict` row as the promoter writes it — keyed by task, no PR."""
    return {
        "kind": "a2a-verdict",
        "task_id": task,
        "verdict": verdict,
        "status": "consumed",
        "findings": 0,
        "head_sha": "a" * 40,
        "ts": iso(days_ago=days_ago),
    }


def approved(pr: str, *, days_ago: int, task: str | None = None) -> list[dict]:
    task = task or f"skills_intake_review-pr{pr}-dungae-x"
    return [dispatch_row(task, pr), verdict_row(task, "approve", days_ago=days_ago)]


class ApproveLineageJoinTests(unittest.TestCase):
    def test_verdict_is_attributed_through_the_dispatch_pr_url(self) -> None:
        self.assertEqual(
            promotion._approve_lineage_prs(approved("140", days_ago=10)),
            {"140": iso(days_ago=10)},
        )

    def test_non_approve_verdicts_are_ignored(self) -> None:
        rows = [dispatch_row("t", "1"), verdict_row("t", "revise", days_ago=10)]
        self.assertEqual(promotion._approve_lineage_prs(rows), {})
        rows = [dispatch_row("t", "1"), verdict_row("t", "reject", days_ago=10)]
        self.assertEqual(promotion._approve_lineage_prs(rows), {})

    def test_verdict_without_a_dispatch_is_dropped(self) -> None:
        """No dispatch means no PR to attribute it to — never guess one."""
        rows = [verdict_row("orphan", "approve", days_ago=10)]
        self.assertEqual(promotion._approve_lineage_prs(rows), {})

    def test_dispatch_without_a_parsable_pr_url_is_dropped(self) -> None:
        row = dispatch_row("t", "1")
        row["pr_url"] = "https://github.com/jinwon-int/fleet-skills/issues/1"
        rows = [row, verdict_row("t", "approve", days_ago=10)]
        self.assertEqual(promotion._approve_lineage_prs(rows), {})

    def test_earliest_approval_wins_for_a_relitigated_lineage(self) -> None:
        """Approved, revised, approved again is still one promotion, owed since
        the first approval — the clock must not restart."""
        rows = approved("140", days_ago=30, task="round1")
        rows += [dispatch_row("round2", "140"),
                 verdict_row("round2", "approve", days_ago=2)]
        self.assertEqual(
            promotion._approve_lineage_prs(rows), {"140": iso(days_ago=30)}
        )


class SweepTests(unittest.TestCase):
    def sweep(self, rows, *, states=None, dry_run=False, window=32):
        """Run the sweep with the ledger and gh reads stubbed out."""
        written: list[dict] = []
        states = states or {}
        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_append_ledger",
                          side_effect=lambda _c, row: written.append(row)), \
             patch.object(promotion, "_pr_state",
                          side_effect=lambda _c, pr: states.get(pr, "OPEN")):
            out = promotion._sweep_intake_states(config(window), dry_run=dry_run)
        return out, written

    def test_open_pr_is_recorded(self) -> None:
        out, written = self.sweep(approved("140", days_ago=10))
        self.assertEqual([r["outcome"] for r in out], ["intake-state-recorded"])
        self.assertEqual(written[0]["pr"], "140")
        self.assertEqual(written[0]["state"], "OPEN")

    def test_dry_run_writes_nothing(self) -> None:
        out, written = self.sweep(approved("140", days_ago=10), dry_run=True)
        self.assertEqual(written, [])
        self.assertEqual([r["outcome"] for r in out], ["would-poll-intake-state"])

    def test_unreadable_state_writes_nothing(self) -> None:
        """A transient gh failure must not mark a live PR terminal."""
        out, written = self.sweep(approved("140", days_ago=10), states={"140": None})
        self.assertEqual(written, [])
        self.assertEqual([r["outcome"] for r in out], ["intake-state-unreadable"])

    def test_terminal_is_sticky(self) -> None:
        """Once CLOSED is on record the PR is never polled again."""
        rows = approved("140", days_ago=10)
        rows.append({"kind": "a2a-intake-state", "pr": "140", "state": "CLOSED",
                     "approved_at": iso(days_ago=10), "ts": iso(days_ago=1)})
        out, written = self.sweep(rows)
        self.assertEqual(out, [])
        self.assertEqual(written, [])

    def test_merged_is_terminal_too(self) -> None:
        rows = approved("140", days_ago=10)
        rows.append({"kind": "a2a-intake-state", "pr": "140", "state": "MERGED",
                     "approved_at": iso(days_ago=10), "ts": iso(days_ago=1)})
        out, _ = self.sweep(rows)
        self.assertEqual(out, [])

    def test_recorded_open_is_repolled(self) -> None:
        """OPEN is not terminal: it must be refreshed until it stops being OPEN."""
        rows = approved("140", days_ago=10)
        rows.append({"kind": "a2a-intake-state", "pr": "140", "state": "OPEN",
                     "approved_at": iso(days_ago=10), "ts": iso(days_ago=5)})
        out, written = self.sweep(rows, states={"140": "CLOSED"})
        self.assertEqual([r["outcome"] for r in out], ["intake-state-recorded"])
        self.assertEqual(written[0]["state"], "CLOSED")

    def test_window_bounds_the_polls(self) -> None:
        rows: list[dict] = []
        for n in range(10):
            rows += approved(str(100 + n), days_ago=n + 1)
        out, written = self.sweep(rows, window=3)
        self.assertEqual(len(out), 3)
        self.assertEqual(len(written), 3)

    def test_window_drains_oldest_approvals_first(self) -> None:
        """FIFO for the #1394 reason: a window smaller than the backlog must
        drain the longest-owed promotions, not the newest ones."""
        rows: list[dict] = []
        for n in range(5):
            rows += approved(str(100 + n), days_ago=n + 1)
        out, _ = self.sweep(rows, window=2)
        self.assertEqual([r["pr"] for r in out], ["104", "103"])


class LedgerSchemaPinTests(unittest.TestCase):
    """The row this pass writes is the doctor's only input. Pin it here so a
    field rename cannot silently blind check_skill_promotion_unpromoted."""

    def written_row(self) -> dict:
        written: list[dict] = []
        with patch.object(promotion, "_ledger_rows",
                          return_value=approved("140", days_ago=9)), \
             patch.object(promotion, "_append_ledger",
                          side_effect=lambda _c, row: written.append(row)), \
             patch.object(promotion, "_pr_state", return_value="OPEN"):
            promotion._sweep_intake_states(config(), dry_run=False)
        return written[0]

    def test_row_has_exactly_the_fields_the_doctor_reads(self) -> None:
        self.assertEqual(
            set(self.written_row()), {"ts", "kind", "pr", "state", "approved_at"}
        )

    def test_kind_matches_the_doctor_filter(self) -> None:
        self.assertEqual(self.written_row()["kind"], "a2a-intake-state")

    def test_approved_at_is_the_verdict_stamp_not_the_write_time(self) -> None:
        """The doctor ages `approved_at`. Writing the poll time here would
        restart a months-old stall at zero on every collect."""
        self.assertEqual(self.written_row()["approved_at"], iso(days_ago=9))

    def test_pr_is_a_string_key(self) -> None:
        """The doctor keys its dict on `pr`; an int would split the identity."""
        self.assertIsInstance(self.written_row()["pr"], str)

    def test_stamps_parse_with_the_doctor_age_helper(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ccc_doctor import _iso_age_days

        self.assertEqual(_iso_age_days(self.written_row()["approved_at"]), 9)


def publish_row(pr: str, tree: str, name: str = "s1", node: str = "gwakga") -> dict:
    """A publish row exactly as _collect_unlocked appends it.

    Pinned deliberately: it is the outcome dict plus a timestamp, so it has NO
    `kind` key. `_candidate_trees_by_pr` matches on that absence, which only
    stays correct while this fixture keeps mirroring the producer.
    """
    return {
        "ts": iso(days_ago=40),
        "outcome": "pr-opened",
        "branch": f"skill-intake/{node}/{name}-claude-{tree[:12]}",
        "draft": "true",
        "name": name,
        "node": node,
        "provider": "claude",
        "source": node,
        "state": "OPEN",
        "transport_id": f"{node}-claude-{name}-{tree[:12]}",
        "tree_sha256": tree,
        "url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}",
    }


def state_row(pr: str, state: str, approved_at: str) -> dict:
    return {"ts": approved_at, "kind": "a2a-intake-state", "pr": pr,
            "state": state, "approved_at": approved_at}


TREE_A = "a" * 64
TREE_B = "b" * 64


class CandidateTreeJoinTests(unittest.TestCase):
    def test_publish_row_supplies_the_tree_hash(self) -> None:
        self.assertEqual(
            promotion._candidate_trees_by_pr([publish_row("140", TREE_A)]),
            {"140": TREE_A},
        )

    def test_kinded_rows_are_ignored(self) -> None:
        """Only the kind-less publish row carries a candidate tree."""
        row = dict(publish_row("140", TREE_A), kind="a2a-dispatch")
        self.assertEqual(promotion._candidate_trees_by_pr([row]), {})

    def test_short_hash_is_rejected(self) -> None:
        self.assertEqual(
            promotion._candidate_trees_by_pr([publish_row("140", "abc")]), {}
        )


class PromotedIntakeSweepTests(unittest.TestCase):
    def sweep(self, rows, *, promoted=None, dry_run=False, enabled=True, window=32,
              close_fails=False):
        written: list[dict] = []
        closed: list[str] = []

        def fake_run(args, **kwargs):
            if args[:3] == ["gh", "pr", "close"]:
                if close_fails:
                    raise promotion.PromotionError("close_denied")
                closed.append(args[3])
            return types.SimpleNamespace(stdout=b"{}")

        cfg = types.SimpleNamespace(
            node="seoseo", repo="jinwon-int/fleet-skills", collect_window=window,
            promote_autoclose_enabled=enabled)
        with patch.object(promotion, "_ledger_rows", return_value=rows),              patch.object(promotion, "_append_ledger",
                          side_effect=lambda _c, row: written.append(row)),              patch.object(promotion, "_promoted_source_trees",
                          return_value=promoted or {}),              patch.object(promotion, "_run", side_effect=fake_run):
            out = promotion._sweep_promoted_intakes(cfg, dry_run=dry_run)
        return out, written, closed

    def ledger(self, pr="140", tree=TREE_A, state="OPEN"):
        return (approved(pr, days_ago=9) + [publish_row(pr, tree),
                state_row(pr, state, iso(days_ago=9))])

    def test_disabled_by_default_does_nothing(self) -> None:
        out, written, closed = self.sweep(
            self.ledger(), promoted={TREE_A: "approved/shared/s1"}, enabled=False)
        self.assertEqual((out, written, closed), ([], [], []))

    def test_exact_tree_match_closes_the_intake(self) -> None:
        out, written, closed = self.sweep(
            self.ledger(), promoted={TREE_A: "approved/shared/s1"})
        self.assertEqual(closed, ["140"])
        self.assertEqual([r["outcome"] for r in out], ["intake-closed"])
        kinds = [r["kind"] for r in written]
        self.assertEqual(kinds, ["a2a-intake-promoted", "a2a-intake-state"])
        self.assertEqual(written[1]["state"], "CLOSED")

    def test_unpromoted_lineage_is_left_open(self) -> None:
        """Approved but not yet promoted is the normal state, not a leftover."""
        out, written, closed = self.sweep(self.ledger(), promoted={})
        self.assertEqual(closed, [])
        self.assertEqual(written, [])
        self.assertEqual([r["outcome"] for r in out], ["awaiting-promotion"])

    def test_a_different_tree_never_closes(self) -> None:
        """Same name, different content must not be read as promoted."""
        out, _, closed = self.sweep(
            self.ledger(), promoted={TREE_B: "approved/shared/s1"})
        self.assertEqual(closed, [])
        self.assertEqual([r["outcome"] for r in out], ["awaiting-promotion"])

    def test_lineage_without_an_approve_verdict_is_skipped(self) -> None:
        """A revise or reject lineage is an owner decision point, not a leftover."""
        rows = [dispatch_row("t", "140"), verdict_row("t", "reject", days_ago=9),
                publish_row("140", TREE_A), state_row("140", "OPEN", iso(days_ago=9))]
        out, _, closed = self.sweep(rows, promoted={TREE_A: "approved/shared/s1"})
        self.assertEqual((out, closed), ([], []))

    def test_already_closed_pr_is_skipped(self) -> None:
        out, _, closed = self.sweep(
            self.ledger(state="CLOSED"), promoted={TREE_A: "approved/shared/s1"})
        self.assertEqual((out, closed), ([], []))

    def test_dry_run_closes_nothing(self) -> None:
        out, written, closed = self.sweep(
            self.ledger(), promoted={TREE_A: "approved/shared/s1"}, dry_run=True)
        self.assertEqual((written, closed), ([], []))
        self.assertEqual([r["outcome"] for r in out], ["would-close-promoted-intake"])

    def test_close_failure_writes_no_ledger_row(self) -> None:
        """A refused close must not leave the ledger claiming it happened."""
        out, written, closed = self.sweep(
            self.ledger(), promoted={TREE_A: "approved/shared/s1"}, close_fails=True)
        self.assertEqual(written, [])
        self.assertEqual([r["outcome"] for r in out], ["close-failed"])

    def test_window_bounds_the_closes(self) -> None:
        rows: list[dict] = []
        promoted = {}
        for n in range(6):
            tree = f"{n}" * 64
            pr = str(100 + n)
            rows += approved(pr, days_ago=9 + n, task=f"t{n}")
            rows += [publish_row(pr, tree, name=f"s{n}"),
                     state_row(pr, "OPEN", iso(days_ago=9 + n))]
            promoted[tree] = f"approved/shared/s{n}"
        out, _, closed = self.sweep(rows, promoted=promoted, window=2)
        self.assertEqual(len(closed), 2)


if __name__ == "__main__":
    unittest.main(verbosity=0)
