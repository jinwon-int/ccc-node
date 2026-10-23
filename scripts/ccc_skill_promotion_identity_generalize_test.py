#!/usr/bin/env python3
"""#2435 S4: a fleet-identity hit no longer turns a promote batch into a human draft.

Field case 2026-10-21: promote batches fleet-skills #548/#551/#552 (7-8
approved candidates each) sat as `needs-human-identity` drafts because one
candidate's `SKILL.md` named the fleet on three lines. With the arbitration
switch at `true`, these tests pin:

- `_promote` holds a flagged candidate out of the batch (ledger
  `a2a-identity-hold`), never reuses an open batch that carries hits, and the
  held candidate takes no batch slot;
- `_sweep_identity_holds` dispatches one `identity` generalization revise per
  lineage (outside the round limit and the final round), waits while it runs
  or once it republished, and otherwise closes the intake PR as
  `archived:identity-unresolved`;
- #2476: an owner reopen after that close (`collect` records the PR OPEN
  again) grants exactly one more generalization round — rounds before the
  reopen are spent, the dispatch gate sees only rounds after it, and the new
  round's outcome settles the hold again; a PR closed and not seen open
  again is left alone;
- `_arbitration_state` ignores identity rounds.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_identity_generalize_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

NOW = 1_800_000_000.0
HEAD = "a" * 40
TREE = "b" * 64
PR = "538"
NODE, NAME = "nodea", "skill-x"
URL = f"https://github.com/jinwon-int/fleet-skills/pull/{PR}"
HITS = [{"line": 2, "kind": "node-name", "match": "nodea"}, {"line": 6, "kind": "product", "match": "Product"}]


def iso(offset_sec: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW + offset_sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hold(pr: str = PR, age: float = 3600) -> dict:
    return {"kind": "a2a-identity-hold", "pr": pr, "node": NODE, "name": NAME, "tree_sha256": TREE,
            "hits": HITS, "ts": iso(-age)}


def review(pr: str = PR) -> dict:
    return {"kind": "a2a-dispatch", "pr_url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}",
            "head_sha": HEAD, "dispatched_task": f"t-{pr}", "transport_id": f"{NODE}-claude-{NAME}-x",
            "node": NODE, "name": NAME, "reviewer_node": "r1"}


def identity_round(task: str, pr: str = PR) -> dict:
    return {"kind": "a2a-revise-dispatch", "task_id": task, "pr": pr, "head_sha": HEAD, "node": NODE,
            "name": NAME, "identity": True, "round": 1}


def config(mode: str = "true") -> types.SimpleNamespace:
    return types.SimpleNamespace(arbitration_mode=mode, dispatch_enabled=True, repo="jinwon-int/fleet-skills",
                                 revise_round_limit=2, revise_daily_cap=6)


class SweepIdentityHolds(unittest.TestCase):
    def sweep(self, rows: list[dict], *, mode: str = "true", dispatch: dict | None = None):
        written: list[dict] = []
        closed: list[list[str]] = []
        calls: list[dict] = []

        def fake_dispatch(_config, row, _rows, findings, reviewer, **kw):
            calls.append({"row": row, "findings": findings, "reviewer": reviewer, **kw})
            return dispatch or {"outcome": "revise-dispatched", "pr": PR}

        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_dispatch_intake_revise", side_effect=fake_dispatch), \
             patch.object(promotion, "_append_ledger", side_effect=lambda c, r: written.append(r)), \
             patch.object(promotion, "_run", side_effect=lambda argv, **k: closed.append(list(argv))):
            out = promotion._sweep_identity_holds(config(mode), dry_run=False, now=NOW)
        return out, written, closed, calls

    def test_off_unless_true(self) -> None:
        for mode in ("off", "dry-run"):
            self.assertEqual(self.sweep([hold(), review()], mode=mode), ([], [], [], []))

    def test_dispatches_one_identity_round_with_line_findings(self) -> None:
        out, _, closed, calls = self.sweep([hold(), review()])
        self.assertEqual(out[0]["outcome"], "identity-revise-dispatched")
        self.assertTrue(calls[0]["identity"])
        self.assertEqual(calls[0]["row"]["dispatched_task"], f"t-{PR}")
        self.assertEqual(len(calls[0]["findings"]), 2)
        self.assertIn("line 2", calls[0]["findings"][0]["note"])
        self.assertEqual(closed, [])

    def test_running_or_republished_round_waits(self) -> None:
        rows = [hold(), review(), identity_round("v1")]
        self.assertEqual(self.sweep(rows)[0], [])
        rows.append({"kind": "a2a-revise-result", "task_id": "v1", "status": "republished"})
        self.assertEqual(self.sweep(rows)[0], [])

    def test_infra_failed_round_is_dispatched_again_not_closed(self) -> None:
        # fleet-skills#538: the round died on a reviser session limit; S1 consumes
        # it as infra_failed and the hold is served again, never closed for it.
        rows = [hold(), review(), identity_round("v1"),
                {"kind": "a2a-revise-result", "task_id": "v1", "status": "infra_failed"}]
        out, _, closed, calls = self.sweep(rows)
        self.assertEqual((out[0]["outcome"], closed, len(calls)), ("identity-revise-dispatched", [], 1))
        retried = rows + [identity_round("v2")]
        self.assertEqual(self.sweep(retried)[0], [])  # the retry is running: wait

    def test_failed_round_closes_unresolved(self) -> None:
        rows = [hold(), review(), identity_round("v1"),
                {"kind": "a2a-revise-result", "task_id": "v1", "status": "invalid"}]
        out, written, closed, calls = self.sweep(rows)
        self.assertEqual(out[0]["reason"], "identity-unresolved")
        self.assertEqual(written[0]["lifecycle"], "archived:identity-unresolved")
        self.assertEqual(closed[0][:3], ["gh", "pr", "close"])
        self.assertIn("#2435 S4", closed[0][-1])
        self.assertEqual(calls, [])

    def test_hit_surviving_the_lineage_round_closes(self) -> None:
        rows = [identity_round("v0", pr="500"), hold(), review()]
        out, _, closed, calls = self.sweep(rows)
        self.assertEqual(out[0]["reason"], "identity-unresolved")
        self.assertEqual((len(closed), calls), (1, []))

    def test_undeliverable_waits_for_grace_then_closes(self) -> None:
        skipped = {"outcome": "revise-skipped", "code": "revise_author_offline"}
        out, _, closed, _ = self.sweep([hold(age=3600), review()], dispatch=skipped)
        self.assertEqual((out[0]["outcome"], closed), ("identity-revise-skipped", []))
        out, written, closed, _ = self.sweep([hold(age=49 * 3600), review()], dispatch=skipped)
        self.assertEqual(out[0]["reason"], "identity-undeliverable")
        self.assertEqual(written[0]["lifecycle"], "archived:identity-unresolved")

    def test_terminal_or_arbitration_closed_is_left_alone(self) -> None:
        closed_state = {"kind": "a2a-intake-state", "pr": PR, "state": "CLOSED"}
        self.assertEqual(self.sweep([hold(), review(), closed_state])[0], [])
        closed = {"kind": "a2a-arbitration-closed", "pr": PR, "head_sha": HEAD}
        # closed by arbitration, never recorded open again: nothing to serve
        self.assertEqual(self.sweep([hold(), review(), closed])[0], [])
        self.assertEqual(self.sweep([hold(), review(), closed, closed_state])[0], [])

    def owner_reopened(self) -> list[dict]:
        # fleet-skills#538 (ccc-node#2476): the identity round failed, arbitration
        # closed the PR, the owner reopened it and `collect` recorded it OPEN again.
        return [hold(), review(), identity_round("v1"),
                {"kind": "a2a-revise-result", "task_id": "v1", "status": "failed"},
                {"kind": "a2a-arbitration-closed", "pr": PR, "head_sha": HEAD, "reason": "identity-unresolved"},
                {"kind": "a2a-intake-state", "pr": PR, "state": "CLOSED"},
                {"kind": "a2a-intake-state", "pr": PR, "state": "OPEN"}]

    def test_owner_reopen_grants_one_more_identity_round(self) -> None:
        rows = self.owner_reopened()
        out, _, closed, calls = self.sweep(rows)
        self.assertEqual((out[0]["outcome"], closed, len(calls)), ("identity-revise-dispatched", [], 1))
        self.assertTrue(calls[0]["identity"])
        # the dispatch gate only sees rounds after the reopen row, so the spent
        # round "v1" no longer trips `revise_identity_already`
        self.assertEqual(calls[0]["lineage_from"], len(rows))
        self.assertEqual(promotion._owner_reopen_index(rows, PR), len(rows) - 1)

    def test_reopened_round_settles_the_hold_again(self) -> None:
        rows = self.owner_reopened() + [identity_round("v2")]
        self.assertEqual(self.sweep(rows)[0], [])  # the granted round is running: wait
        rows.append({"kind": "a2a-revise-result", "task_id": "v2", "status": "republished"})
        self.assertEqual(self.sweep(rows)[0], [])  # superseded by the republished tree
        failed = self.owner_reopened() + [identity_round("v2"),
                                          {"kind": "a2a-revise-result", "task_id": "v2", "status": "failed"}]
        out, written, closed, calls = self.sweep(failed)
        self.assertEqual((out[0]["reason"], written[0]["lifecycle"], len(closed), calls),
                         ("identity-unresolved", "archived:identity-unresolved", 1, []))
        # ...and a second reopen grants one round again, never more than one per reopen
        again = failed + [{"kind": "a2a-arbitration-closed", "pr": PR, "head_sha": HEAD},
                          {"kind": "a2a-intake-state", "pr": PR, "state": "CLOSED"},
                          {"kind": "a2a-intake-state", "pr": PR, "state": "OPEN"}]
        self.assertEqual(self.sweep(again)[0][0]["outcome"], "identity-revise-dispatched")


class IdentityRoundGate(unittest.TestCase):
    def gate(self, lineage: list[dict]):
        with patch.object(promotion, "_comment_once", return_value=True):
            return promotion._revise_round_gate(config(), [], lineage, round_no=len(lineage) + 1,
                                                pr=PR, head=HEAD, final=False, identity=True)

    def test_identity_round_ignores_limit_and_final_once(self) -> None:
        full = [{"node": NODE}, {"node": NODE}, {"node": NODE, "final": True}]
        self.assertIsNone(self.gate(full))
        self.assertEqual(self.gate(full + [{"identity": True}])["code"], "revise_identity_already")

    def test_lineage_from_hides_spent_rounds_from_the_gate(self) -> None:
        # #2476: `_dispatch_intake_revise` builds the lineage from `lineage_from`
        # on, so an identity round before an owner reopen is not "already".
        spent = {"kind": "a2a-revise-dispatch", "task_id": "v1", "node": NODE, "name": NAME, "identity": True}
        rows = [spent, {"kind": "a2a-intake-state", "pr": PR, "state": "OPEN"}]
        seen: list[list[dict]] = []

        def gate(_config, _rows, lineage, **kw):
            seen.append(list(lineage))
            return {"outcome": "revise-skipped", "code": "stop-here"}

        target = (NODE, NAME, TREE[:12], PR, "claude", HEAD, URL)
        with patch.dict("os.environ", {"A2A_EDGE_SECRET": "x"}), \
             patch.object(promotion, "_revise_dispatch_target", return_value=target), \
             patch.object(promotion, "_revise_round_gate", side_effect=gate):
            promotion._dispatch_intake_revise(config(), review(), rows, [], "r", dry_run=False, identity=True)
            promotion._dispatch_intake_revise(config(), review(), rows, [], "r", dry_run=False, identity=True,
                                              lineage_from=len(rows))
        self.assertEqual([len(lineage) for lineage in seen], [1, 0])


class ArbitrationIgnoresIdentityRounds(unittest.TestCase):
    def test_failed_identity_round_is_not_an_arbitration_trigger(self) -> None:
        rows = [{**review(), "dispatched_task": "t1"},
                {"kind": "a2a-verdict", "task_id": "t1", "status": "consumed", "verdict": "approve", "ts": iso(-60)},
                identity_round("v1"), {"kind": "a2a-revise-result", "task_id": "v1", "status": "failed"}]
        self.assertEqual(promotion._arbitration_state(rows, PR, HEAD, NODE, NAME, now=NOW)[0], None)


class PromoteHoldsFlaggedCandidates(unittest.TestCase):
    def test_stage_batch_unstages_flagged_and_records_hold(self) -> None:
        with TemporaryDirectory() as tmp:
            work = Path(tmp)
            items = [{"pr": PR, "name": NAME, "node": NODE, "tree_sha256": TREE},
                     {"pr": "540", "name": "skill-y", "node": "nodeb", "tree_sha256": "c" * 64}]

            def stage(_work, item, *, audience):
                target = work / "approved" / audience / item["name"]
                target.mkdir(parents=True)
                text = "---\nname: x\n---\nrun on nodea\n" if item["pr"] == PR else "---\nname: y\n---\ngeneric\n"
                return target, text

            staged: list[dict] = []
            errors: list[dict] = []
            held: list[dict] = []
            written: list[dict] = []
            rows: list[dict] = []
            with patch.object(promotion, "_promote_stage", side_effect=stage), \
                 patch.object(promotion, "_run", return_value=None), \
                 patch.object(promotion, "_append_ledger", side_effect=lambda c, r: written.append(r)):
                promotion._promote_stage_batch(config(), rows, work, items, ["nodea", "nodeb"], generalize=True,
                                               staged=staged, errors=errors, held=held)
            self.assertEqual([row["pr"] for row in staged], ["540"])
            self.assertEqual(held, [{"pr": PR, "name": NAME, "hits": 1}])
            self.assertEqual(written[0]["kind"], "a2a-identity-hold")
            self.assertFalse((work / "approved" / "shared" / NAME).exists())
            self.assertTrue((work / "approved" / "shared" / "skill-y").exists())

    def preview(self, rows: list[dict], open_rows: list[dict], *, mode: str = "true"):
        pending = [{"pr": PR, "tree_sha256": TREE, "name": NAME}, {"pr": "540", "tree_sha256": "c" * 64, "name": "y"}]
        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_promoted_source_trees", return_value={}), \
             patch.object(promotion, "_promotable", return_value=pending), \
             patch.object(promotion, "_open_promote_prs", return_value=(open_rows, [])):
            return promotion._promote(config(mode), dry_run=True, limit=8)

    def test_held_candidate_takes_no_slot_and_flagged_batch_is_not_reused(self) -> None:
        flagged_batch = {"pr": "552", "identity_hits": [{"name": NAME}],
                         "candidates": [f"540:{'c' * 64}"]}
        out = self.preview([hold()], [flagged_batch])
        self.assertEqual([item["pr"] for item in out["staged"]], ["540"])  # rebuilt without the held one
        self.assertEqual(out["would_supersede"], ["552"])
        off = self.preview([hold()], [], mode="off")
        self.assertEqual(len(off["staged"]), 2)  # switch off: unchanged behaviour


if __name__ == "__main__":
    unittest.main()
