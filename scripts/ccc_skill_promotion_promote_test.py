#!/usr/bin/env python3
"""#1628 lane: staging approved candidates as a draft `approved/*` PR.

Promotion is three parts of hand work — build the sanitized `approved/*` PR,
merge it, close the intake PR. `promote` automates the mechanical part of the
first: branch from current `main`, copy the reviewed tree, write
`approval.json`, group the batch. Measured on the 32 promoted by hand on
2026-09-17, 28 needed nothing beyond exactly that.

The two parts that are NOT mechanical are handed to a human through the draft,
and these tests pin that they are never guessed:

- audience. The verdict schema carries none and the provider does not imply
  one (29 `claude`-provider candidates went to `shared`, 2 to `claude`), so
  everything stages as `shared` and the PR body says so.
- generalization. `_promote_identity_hits` annotates surviving fleet
  identities. Provider and audience names are NOT identities: flagging them
  marked `piri-lane-routing-check` for the word "piri", which is the skill's
  entire subject.

They also pin the safety envelope: `promote` never merges, never marks a PR
ready, and never promotes content other than the exact tree the `approve`
verdict was bound to.
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
    "promotion_promote_test", Path(__file__).with_name("ccc-skill-promotion.py")
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


def dispatch_row(task: str, pr: str) -> dict:
    return {
        "kind": "a2a-dispatch",
        "dispatched_task": task,
        "pr_url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}",
        "ts": iso(days_ago=40),
    }


def verdict_row(task: str, verdict: str, *, days_ago: int) -> dict:
    return {"kind": "a2a-verdict", "task_id": task, "verdict": verdict,
            "ts": iso(days_ago=days_ago)}


def publish_row(pr: str, tree: str, name: str = "s1", node: str = "gwakga") -> dict:
    """The publish row as _collect_unlocked appends it — note: no `kind`."""
    return {
        "ts": iso(days_ago=40), "outcome": "pr-opened",
        "branch": f"skill-intake/{node}/{name}-claude-{tree[:12]}",
        "name": name, "node": node, "provider": "claude", "source": node,
        "state": "OPEN", "tree_sha256": tree,
        "url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}",
    }


def lineage(pr: str, tree: str, *, verdict: str = "approve", name: str = "s1") -> list:
    task = f"skills_intake_review-pr{pr}-gwakga-x"
    return [dispatch_row(task, pr), verdict_row(task, verdict, days_ago=9),
            publish_row(pr, tree, name=name)]


class IdentityScannerTests(unittest.TestCase):
    NODES = ["gwakga", "seoseo", "dungae", "gongyung"]

    def test_worker_node_name_is_flagged(self) -> None:
        """The strict class: zero hits across all 75 promoted skills, so one
        is a real leak (`gongyung` was hand-edited out of #165)."""
        hits = promotion._promote_identity_hits("run it on gongyung", self.NODES)
        self.assertEqual(
            hits, [{"line": 1, "kind": "node-name", "match": "gongyung"}]
        )

    def test_provider_and_audience_names_are_not_identities(self) -> None:
        """`piri`/`danso`/`claude`/`codex` are legitimate vocabulary — flagging
        them marked piri-lane-routing-check for naming its own subject."""
        text = "Verify that a queued piri draft installs to its own danso root"
        self.assertEqual(promotion._promote_identity_hits(text, self.NODES), [])

    def test_org_repo_slug_is_flagged(self) -> None:
        hits = promotion._promote_identity_hits("see jinwon-int/ccc-node#1257", [])
        self.assertEqual([h["match"] for h in hits], ["jinwon-int/ccc-node"])

    def test_product_names_are_flagged(self) -> None:
        for text, want in (("a Hermes fleet", "Hermes"),
                           ("team ccc-node instances", "ccc-node"),
                           ("the a2a-nexus checkout", "a2a-nexus")):
            hits = promotion._promote_identity_hits(text, [])
            self.assertEqual([h["match"] for h in hits], [want], text)

    def test_line_numbers_are_one_based(self) -> None:
        hits = promotion._promote_identity_hits("clean\nclean\non gwakga", self.NODES)
        self.assertEqual(hits[0]["line"], 3)

    def test_clean_text_yields_nothing(self) -> None:
        self.assertEqual(
            promotion._promote_identity_hits("Generalized procedure.", self.NODES), []
        )

    def test_empty_node_list_still_scans_products(self) -> None:
        """A keyring this pass cannot read must narrow the scan, not disable it."""
        hits = promotion._promote_identity_hits("on gwakga via Hermes", [])
        self.assertEqual([h["match"] for h in hits], ["Hermes"])


class PromotableTests(unittest.TestCase):
    def cfg(self):
        return types.SimpleNamespace(node="seoseo", collect_nodes=())

    def test_approved_and_unpromoted_is_promotable(self) -> None:
        out = promotion._promotable(self.cfg(), lineage("140", TREE_A), {})
        self.assertEqual([r["pr"] for r in out], ["140"])
        self.assertEqual(out[0]["tree_sha256"], TREE_A)

    def test_already_promoted_tree_is_excluded(self) -> None:
        out = promotion._promotable(
            self.cfg(), lineage("140", TREE_A), {TREE_A: "approved/shared/s1"}
        )
        self.assertEqual(out, [])

    def test_a_different_promoted_tree_does_not_exclude(self) -> None:
        out = promotion._promotable(
            self.cfg(), lineage("140", TREE_A), {TREE_B: "approved/shared/s1"}
        )
        self.assertEqual([r["pr"] for r in out], ["140"])

    def test_non_approve_verdicts_are_excluded(self) -> None:
        for verdict in ("revise", "reject"):
            out = promotion._promotable(
                self.cfg(), lineage("140", TREE_A, verdict=verdict), {}
            )
            self.assertEqual(out, [], verdict)

    def test_lineage_without_a_publish_row_is_excluded(self) -> None:
        """No publish row means no candidate tree and no intake branch — there
        is nothing to copy, and guessing one would promote unreviewed content."""
        task = "skills_intake_review-pr140-gwakga-x"
        rows = [dispatch_row(task, "140"), verdict_row(task, "approve", days_ago=9)]
        self.assertEqual(promotion._promotable(self.cfg(), rows, {}), [])

    def test_oldest_approval_comes_first(self) -> None:
        rows = lineage("140", TREE_A, name="s1") + lineage("99", TREE_B, name="s2")
        rows[1]["ts"] = iso(days_ago=30)
        out = promotion._promotable(self.cfg(), rows, {})
        self.assertEqual({r["pr"] for r in out}, {"140", "99"})

    def test_closed_intake_is_not_promotable(self) -> None:
        rows = lineage("140", TREE_A) + [{
            "kind": "a2a-intake-state", "pr": "140", "state": "CLOSED",
            "ts": iso(days_ago=1),
        }]
        self.assertEqual(promotion._promotable(self.cfg(), rows, {}), [])


class PromoteFlowTests(unittest.TestCase):
    def run_promote(self, rows, *, promoted=None, dry_run=False, limit=8):
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            if args[:2] == ["gh", "pr"]:
                return types.SimpleNamespace(
                    stdout=b"https://github.com/jinwon-int/fleet-skills/pull/999\n")
            return types.SimpleNamespace(stdout=b"")

        cfg = types.SimpleNamespace(
            node="seoseo", collect_nodes=(), repo="jinwon-int/fleet-skills",
            base="main", remote="https://example.invalid/fleet-skills.git",
            promotion_state_dir=Path("/tmp"))
        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_promoted_source_trees",
                          return_value=promoted or {}), \
             patch.object(promotion, "_promote_worker_nodes", return_value=["gwakga"]), \
             patch.object(promotion, "_promote_stage",
                          side_effect=lambda work, item, *, audience: (
                              work / "approved" / audience / item["name"],
                              "on gwakga")), \
             patch.object(promotion, "_append_ledger", lambda *_a, **_k: None), \
             patch.object(promotion, "_run", side_effect=fake_run):
            out = promotion._promote(cfg, dry_run=dry_run, limit=limit)
        return out, calls

    def test_nothing_to_promote_touches_github(self) -> None:
        out, calls = self.run_promote(
            lineage("140", TREE_A), promoted={TREE_A: "approved/shared/s1"})
        self.assertEqual(out["outcome"], "nothing-to-promote")
        self.assertEqual(calls, [])

    def test_dry_run_opens_no_pr(self) -> None:
        out, calls = self.run_promote(lineage("140", TREE_A), dry_run=True)
        self.assertEqual(calls, [])
        self.assertEqual([r["outcome"] for r in out["staged"]], ["would-stage"])
        self.assertEqual(out["staged"][0]["audience"], "shared")

    def test_audience_is_always_the_default(self) -> None:
        """Nothing about the candidate may pick an audience — the PR asks."""
        out, _ = self.run_promote(lineage("140", TREE_A))
        self.assertEqual(out["staged"][0]["audience"], "shared")

    def test_pr_is_opened_as_a_draft(self) -> None:
        _, calls = self.run_promote(lineage("140", TREE_A))
        create = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(create), 1)
        self.assertIn("--draft", create[0])

    def test_nothing_is_merged_or_marked_ready(self) -> None:
        _, calls = self.run_promote(lineage("140", TREE_A))
        for call in calls:
            self.assertNotIn("merge", call)
            self.assertNotIn("ready", call)
            self.assertNotIn("close", call)

    def test_identity_hits_reach_the_pr_body(self) -> None:
        _, calls = self.run_promote(lineage("140", TREE_A))
        body = [c for c in calls if c[:3] == ["gh", "pr", "create"]][0][-1]
        self.assertIn("gwakga", body)
        self.assertIn("node-name", body)
        self.assertIn("shared", body)

    def test_limit_bounds_the_batch(self) -> None:
        rows: list[dict] = []
        for n in range(6):
            rows += lineage(str(100 + n), f"{n}" * 64, name=f"s{n}")
        out, _ = self.run_promote(rows, limit=2)
        self.assertEqual(len(out["staged"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=0)
