#!/usr/bin/env python3
"""The auto-revision republish must leave the same publish row `_collect` does.

Field case, 2026-09-11 → 2026-09-21. `_republish_revised_candidate` pushed the
revised tree through `_publish` and recorded only an `a2a-revise-result`
row. Every lineage consumer, however, recognises a published candidate by the
kind-less publish row (`url` + 64-char `tree_sha256`):

    _candidate_trees_by_pr    -> _promotable       (auto-promote never saw them)
    _publish_lineage_groups   -> supersede sweep   (`superseded_intakes=[]` forever)
    _sweep_promoted_intakes                        (never closed after promotion)

33 republished intake PRs sat invisible beside their superseded originals.
These tests pin the row the republish path now writes, the one-off backfill
that repairs an existing ledger, and that both feed the consumers.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_republish_row_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

OLD_TREE = "a" * 64
NEW_TREE = "b" * 64
HEAD = "c" * 40
REPO = "https://github.com/jinwon-int/fleet-skills"


def publish_row(pr: str, tree: str) -> dict:
    """A publish row as `_collect` writes it (no `kind`)."""
    return {"ts": "2026-09-20T14:03:00Z", "outcome": "pr-opened",
            "branch": f"skill-intake/dungae/xdist-claude-{tree[:12]}",
            "url": f"{REPO}/pull/{pr}", "source": "remote", "node": "dungae",
            "provider": "claude", "name": "xdist", "tree_sha256": tree,
            "transport_id": f"dungae-claude-xdist-{tree[:12]}"}


def republished_row(old_pr: str, new_pr: str, tree: str, **overrides: object) -> dict:
    """An `a2a-revise-result status=republished` row as the gate writes it."""
    row = {"ts": "2026-09-21T14:08:00Z", "kind": "a2a-revise-result",
           "task_id": f"skills_intake_revise-pr{old_pr}-dungae-x", "status": "republished",
           "node": "dungae", "provider": "claude", "name": "xdist", "pr": old_pr,
           "head_sha": HEAD, "new_tree_sha256": tree,
           "new_transport_id": f"dungae-claude-xdist-{tree[:12]}",
           "new_branch": f"skill-intake/dungae/xdist-claude-{tree[:12]}",
           "new_pr_url": f"{REPO}/pull/{new_pr}"}
    row.update(overrides)
    return row


class PublishRowShape(unittest.TestCase):
    def test_row_is_kind_less_with_url_and_tree(self) -> None:
        row = promotion._publish_row(
            {"outcome": "pr-opened", "branch": "b", "url": f"{REPO}/pull/284"},
            source="revise", node="dungae", provider="claude", name="xdist",
            tree=NEW_TREE, transport_id="dungae-claude-xdist-" + NEW_TREE[:12],
        )
        self.assertNotIn("kind", row)
        self.assertEqual(row["url"], f"{REPO}/pull/284")
        self.assertEqual(row["tree_sha256"], NEW_TREE)
        self.assertEqual(promotion._candidate_trees_by_pr([row]), {"284": NEW_TREE})


class RepublishWritesPublishRow(unittest.TestCase):
    def run_republish(self, outcome: dict) -> list[dict]:
        captured: list[dict] = []
        candidate = types.SimpleNamespace(
            node="dungae", provider="claude", name="xdist", tree_sha256=NEW_TREE
        )
        config = types.SimpleNamespace(supersede_autoclose_enabled=False)
        with patch.object(promotion, "_pr_skill_files", return_value=[]), \
             patch.object(promotion, "_candidate_from_revised_files", return_value=candidate), \
             patch.object(promotion, "_publish", return_value=outcome), \
             patch.object(promotion, "_append_ledger", side_effect=lambda _c, r: captured.append(r)), \
             patch.object(promotion, "_comment_once"), \
             patch.object(promotion, "_dispatch_intake_review"):
            result = promotion._republish_revised_candidate(
                config, [], revised=[("SKILL.md", b"x")], task_id="t", pr="273",
                head=HEAD, node="dungae", name="xdist", provider="claude", tree=OLD_TREE,
            )
        self.assertEqual(result["outcome"], "republished")
        return captured

    def test_pr_opened_records_the_publish_row_before_the_revise_result(self) -> None:
        rows = self.run_republish(
            {"outcome": "pr-opened", "branch": "skill-intake/dungae/xdist-claude-" + NEW_TREE[:12],
             "url": f"{REPO}/pull/284"}
        )
        kinds = [row.get("kind") for row in rows]
        self.assertEqual(kinds, [None, "a2a-revise-result"])
        publish = rows[0]
        self.assertEqual(publish["url"], f"{REPO}/pull/284")
        self.assertEqual(publish["tree_sha256"], NEW_TREE)
        self.assertEqual(publish["source"], "revise")
        self.assertEqual(publish["transport_id"], "dungae-claude-xdist-" + NEW_TREE[:12])
        self.assertEqual(promotion._candidate_trees_by_pr(rows), {"284": NEW_TREE})

    def test_existing_pr_also_records(self) -> None:
        rows = self.run_republish(
            {"outcome": "existing-pr", "branch": "b", "url": f"{REPO}/pull/284"}
        )
        self.assertIsNone(rows[0].get("kind"))

    def test_would_open_does_not_record(self) -> None:
        rows = self.run_republish({"outcome": "would-open-private-intake-pr", "branch": "b"})
        self.assertEqual([row.get("kind") for row in rows], ["a2a-revise-result"])


class Backfill(unittest.TestCase):
    def test_republished_without_row_is_backfilled_once(self) -> None:
        rows = [publish_row("273", OLD_TREE), republished_row("273", "284", NEW_TREE)]
        pending = promotion._republished_without_publish_row(rows)
        self.assertEqual(len(pending), 1)
        row = pending[0]
        self.assertIsNone(row.get("kind"))
        self.assertEqual(row["url"], f"{REPO}/pull/284")
        self.assertEqual(row["tree_sha256"], NEW_TREE)
        self.assertEqual(row["source"], "revise-backfill")
        self.assertEqual(row["branch"], "skill-intake/dungae/xdist-claude-" + NEW_TREE[:12])
        # idempotent: once recorded, nothing is pending
        self.assertEqual(promotion._republished_without_publish_row(rows + pending), [])

    def test_fixed_path_ledger_needs_no_backfill(self) -> None:
        """A ledger written by the fixed republish path already has the row."""
        rows = [publish_row("273", OLD_TREE), publish_row("284", NEW_TREE),
                republished_row("273", "284", NEW_TREE)]
        self.assertEqual(promotion._republished_without_publish_row(rows), [])

    def test_duplicate_revise_results_yield_one_row(self) -> None:
        rows = [republished_row("273", "284", NEW_TREE), republished_row("273", "284", NEW_TREE)]
        self.assertEqual(len(promotion._republished_without_publish_row(rows)), 1)

    def test_malformed_rows_are_skipped(self) -> None:
        rows = [
            republished_row("273", "284", "short"),
            republished_row("273", "285", NEW_TREE, new_pr_url="not-a-url"),
            republished_row("273", "286", NEW_TREE, new_branch=""),
            republished_row("273", "287", NEW_TREE, node=None),
            republished_row("273", "288", NEW_TREE, status="publish-failed"),
        ]
        self.assertEqual(promotion._republished_without_publish_row(rows), [])

    def test_backfilled_rows_feed_the_lineage_consumers(self) -> None:
        rows = [publish_row("273", OLD_TREE), republished_row("273", "284", NEW_TREE)]
        before = promotion._publish_lineage_groups(rows)
        self.assertEqual([len(v) for v in before.values()], [1])
        rows += promotion._republished_without_publish_row(rows)
        groups = promotion._publish_lineage_groups(rows)
        items = groups[("dungae", "xdist", "claude")]
        self.assertEqual(sorted(item["pr"] for item in items), ["273", "284"])
        self.assertEqual(promotion._candidate_trees_by_pr(rows)["284"], NEW_TREE)

    def test_backfill_command_is_read_only_under_dry_run(self) -> None:
        rows = [republished_row("273", "284", NEW_TREE)]
        appended: list[dict] = []
        config = types.SimpleNamespace()
        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_append_ledger", side_effect=lambda _c, r: appended.append(r)):
            dry = promotion._backfill_republished(config, dry_run=True)
            self.assertEqual(dry["would_record"], [{"pr": "284", "node": "dungae", "name": "xdist"}])
            self.assertEqual(appended, [])
            wet = promotion._backfill_republished(config, dry_run=False)
        self.assertEqual(wet["recorded"], [{"pr": "284", "node": "dungae", "name": "xdist"}])
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0]["url"], f"{REPO}/pull/284")


if __name__ == "__main__":
    unittest.main()
