#!/usr/bin/env python3
"""#2435 S2: A2A arbitration replaces the "human judgment" end states of the intake loop.

Owner decision 2026-10-21. Before this, an intake PR whose latest verdict was
`reject`, whose lineage hit the revise round limit, whose reviser recommended
dropping it, whose revision could go nowhere, or whose review/revise round
failed stayed open "for an owner decision" (13 such PRs on 2026-10-21). These
tests pin:

- `_arbitration_state` derives the unsettled trigger from the ledger alone and
  stays None while any review/revise round for the head is still running;
- `_sweep_arbitration` is inert when off, previews in dry-run, and in `true`
  mode dispatches one independent arbitration round per lineage (author, every
  prior reviewer and reviser excluded) or closes a lineage that is unsettled
  again after its arbitration or whose arbitration never arrived;
- a non-approve arbitration verdict is applied mechanically (reject closes;
  revise dispatches the one final round, an undeliverable one closes);
- the final round is allowed beyond the round limit exactly once per lineage;
- arbitration dispatch is idempotent only against other arbitration rounds and
  appends its publisher-authored appendix to the worker procedure.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_arbitration_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

HEAD = "a" * 40
NOW = 1_800_000_000.0
PR = "466"
URL = f"https://github.com/jinwon-int/fleet-skills/pull/{PR}"
NODE, NAME = "nodea", "skill-x"
TRANSPORT = "nodea-claude-skill-x-0123456789ab"


def iso(offset_sec: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW + offset_sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def review(task: str, reviewer: str, *, arbitration: bool = False, head: str = HEAD) -> dict:
    row = {"kind": "a2a-dispatch", "transport_id": TRANSPORT, "head_sha": head, "dispatched_task": task,
           "task_id": task, "reviewer_node": reviewer, "pr_url": URL, "node": NODE, "name": NAME,
           "ts": iso(-7200)}
    if arbitration:
        row["arbitration"] = True
    return row


def verdict(task: str, value: str | None, *, status: str = "consumed", age: float = 3600) -> dict:
    row = {"kind": "a2a-verdict", "task_id": task, "status": status, "ts": iso(-age), "head_sha": HEAD}
    if value is not None:
        row["verdict"] = value
    return row


def revise(task: str, reviser: str, *, final: bool = False) -> dict:
    row = {"kind": "a2a-revise-dispatch", "task_id": task, "pr": PR, "head_sha": HEAD, "node": NODE,
           "name": NAME, "reviser_node": reviser, "round": 1, "ts": iso(-1800)}
    if final:
        row["final"] = True
    return row


def revise_result(task: str, status: str) -> dict:
    return {"kind": "a2a-revise-result", "task_id": task, "status": status, "ts": iso(-600)}


def state(rows: list[dict]) -> tuple:
    return promotion._arbitration_state(rows, PR, HEAD, NODE, NAME, now=NOW)


class ArbitrationState(unittest.TestCase):
    def test_reject_is_unsettled(self) -> None:
        self.assertEqual(state([review("t1", "r1"), verdict("t1", "reject")]), ("reject", False))

    def test_running_review_or_revise_is_not_a_trigger(self) -> None:
        self.assertEqual(state([review("t1", "r1")]), (None, False))
        rows = [review("t1", "r1"), verdict("t1", "revise"), revise("v1", NODE)]
        self.assertEqual(state(rows), (None, False))

    def test_round_limit_marker_triggers_immediately(self) -> None:
        rows = [review("t1", "r1"), verdict("t1", "revise", age=60),
                {"kind": "a2a-revise-comment", "pr": PR, "head_sha": HEAD, "marker": "round-limit"}]
        self.assertEqual(state(rows), ("round_limit", False))

    def test_undelivered_revise_waits_for_the_stuck_window(self) -> None:
        self.assertEqual(state([review("t1", "r1"), verdict("t1", "revise", age=3600)]), (None, False))
        self.assertEqual(state([review("t1", "r1"), verdict("t1", "revise", age=25 * 3600)]),
                         ("revise_stuck", False))

    def test_revise_outcomes(self) -> None:
        base = [review("t1", "r1"), verdict("t1", "revise"), revise("v1", NODE)]
        self.assertEqual(state(base + [revise_result("v1", "drop-recommended")])[0], "drop_recommended")
        self.assertEqual(state(base + [revise_result("v1", "failed")])[0], "revise_failed")
        self.assertEqual(state(base + [revise_result("v1", "invalid")])[0], "revise_failed")
        self.assertEqual(state(base + [revise_result("v1", "republished")])[0], None)

    def test_failed_review_and_settled_states(self) -> None:
        self.assertEqual(state([review("t1", "r1"), verdict("t1", None, status="malformed")])[0], "review_failed")
        self.assertEqual(state([review("t1", "r1"), verdict("t1", "approve")])[0], None)
        self.assertEqual(state([])[0], None)

    def test_infra_failed_round_is_neither_running_nor_settled(self) -> None:
        rows = [review("t1", "r1"), verdict("t1", None, status="infra_failed")]
        self.assertEqual(state(rows)[0], None)  # owed to the missing-dispatch sweep

    def test_review_after_failed_revise_supersedes_it(self) -> None:
        # Field case 2026-10-21 (fleet-skills#532): revise_failed triggered the
        # arbitration, the arbiter approved, and the old revise_failed must not
        # be re-read as an unsettled-after-arbitration trigger.
        before = [review("t1", "r1"), verdict("t1", "revise"), revise("v1", NODE), revise_result("v1", "failed")]
        self.assertEqual(state(before), ("revise_failed", False))
        approved = before + [review("t2", "arb", arbitration=True), verdict("t2", "approve")]
        self.assertEqual(state(approved), (None, True))
        running = before + [review("t2", "arb", arbitration=True)]
        self.assertEqual(state(running), (None, True))
        rejected = before + [review("t2", "arb", arbitration=True), verdict("t2", "reject")]
        self.assertEqual(state(rejected), ("reject", True))
        final_failed = before + [review("t2", "arb", arbitration=True), verdict("t2", "revise"),
                                 revise("v2", "s", final=True), revise_result("v2", "invalid")]
        self.assertEqual(state(final_failed), ("revise_failed", True))

    def test_arbitrated_flag_is_lineage_wide(self) -> None:
        rows = [review("t0", "r0", arbitration=True, head="b" * 40), verdict("t0", "revise"),
                review("t1", "r1"), verdict("t1", "reject")]
        self.assertEqual(state(rows), ("reject", True))


OPEN_PR = {"number": PR, "url": URL, "head": HEAD, "created_at": 0.0,
           "branch": f"skill-intake/{NODE}/{NAME}-claude-0123456789ab"}


class SweepArbitration(unittest.TestCase):
    def run_sweep(self, rows: list[dict], *, mode: str = "true", dry_run: bool = False):
        written: list[dict] = []
        dispatched: list[dict] = []
        closed: list[list[str]] = []
        config = types.SimpleNamespace(arbitration_mode=mode, dispatch_enabled=True, repo="jinwon-int/fleet-skills")

        def fake_dispatch(_config, candidate, outcome, *, transport_id, arbitration=None):
            dispatched.append({"arbitration": arbitration, "transport_id": transport_id})
            return {"outcome": "dispatched"}

        def fake_run(argv, **_kw):
            closed.append(list(argv))
            return types.SimpleNamespace(stdout=b"")

        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_open_intake_prs", return_value=[dict(OPEN_PR)]), \
             patch.object(promotion, "_candidate_from_open_pr",
                          return_value=types.SimpleNamespace(node=NODE, name=NAME)), \
             patch.object(promotion, "_transport_id", return_value=TRANSPORT), \
             patch.object(promotion, "_append_ledger", side_effect=lambda c, r: written.append(r)), \
             patch.object(promotion, "_dispatch_intake_review", side_effect=fake_dispatch), \
             patch.object(promotion, "_run", side_effect=fake_run):
            out = promotion._sweep_arbitration(config, dry_run=dry_run, now=NOW)
        return out, written, dispatched, closed

    def test_off_is_inert(self) -> None:
        out, written, dispatched, closed = self.run_sweep([review("t1", "r1"), verdict("t1", "reject")], mode="off")
        self.assertEqual((out, written, dispatched, closed), ([], [], [], []))

    def test_dry_run_previews_only(self) -> None:
        out, written, dispatched, closed = self.run_sweep([review("t1", "r1"), verdict("t1", "reject")],
                                                          mode="dry-run")
        self.assertEqual(out, [{"outcome": "would-arbitrate", "pr": PR, "trigger": "reject"}])
        self.assertEqual((written, dispatched, closed), ([], [], []))

    def test_true_dispatches_one_independent_arbitration(self) -> None:
        older = {**revise("v0", "subx"), "pr": "401", "head_sha": "c" * 40}  # earlier PR of the lineage
        rows = [review("t0", "r0", head="c" * 40), verdict("t0", "revise"), older,
                revise_result("v0", "republished"), review("t1", "r1"), verdict("t1", "reject")]
        out, written, dispatched, closed = self.run_sweep(rows)
        self.assertEqual(out[0]["outcome"], "arbitration-dispatch")
        self.assertEqual(written[0]["kind"], "a2a-arbitration")
        arb = dispatched[0]["arbitration"]
        self.assertEqual(arb["trigger"], "reject")
        self.assertEqual(set(arb["exclude"]), {NODE, "r0", "r1", "subx"})
        self.assertIn("ARBITRATION ROUND", arb["appendix"])
        self.assertIn("`reject`", arb["appendix"])
        self.assertEqual(closed, [])

    def test_unsettled_again_after_arbitration_closes(self) -> None:
        rows = [review("t0", "r0", arbitration=True), verdict("t0", "revise"), revise("v0", "s", final=True),
                revise_result("v0", "failed")]
        out, written, dispatched, closed = self.run_sweep(rows)
        self.assertEqual(out[0]["outcome"], "arbitration-closed")
        self.assertEqual(out[0]["reason"], "post-arbitration-revise_failed")
        self.assertEqual(closed[0][:3], ["gh", "pr", "close"])
        self.assertEqual(written[0]["lifecycle"], "archived:arbiter-closed")
        self.assertEqual(written[1], {**written[1], "kind": "a2a-intake-state", "state": "CLOSED"})
        self.assertEqual(dispatched, [])

    def test_arbitration_never_dispatched_within_grace_closes_unavailable(self) -> None:
        rows = [review("t1", "r1"), verdict("t1", "reject"),
                {"kind": "a2a-arbitration", "pr": PR, "head_sha": HEAD, "ts": iso(-49 * 3600)}]
        out, written, _, closed = self.run_sweep(rows)
        self.assertEqual(out[0]["reason"], "arbiter-unavailable")
        self.assertEqual(written[0]["lifecycle"], "archived:arbiter-unavailable")
        self.assertEqual(len(closed), 1)


class ApplyArbitrationVerdict(unittest.TestCase):
    ROW = {**review("t9", "arb", arbitration=True)}

    def apply(self, value: str, revise_outcome: dict | None = None):
        closed: list[list[str]] = []
        written: list[dict] = []
        config = types.SimpleNamespace(repo="jinwon-int/fleet-skills")
        with patch.object(promotion, "_dispatch_intake_revise",
                          return_value=revise_outcome or {"outcome": "revise-skipped", "code": "x"}) as rev, \
             patch.object(promotion, "_pr_comment", return_value=None), \
             patch.object(promotion, "_append_ledger", side_effect=lambda c, r: written.append(r)), \
             patch.object(promotion, "_run", side_effect=lambda argv, **k: closed.append(list(argv))):
            out = promotion._apply_arbitration_verdict(config, [], dict(self.ROW), HEAD, "t9", value, [])
        return out, closed, written, rev

    def test_reject_closes(self) -> None:
        out, closed, written, rev = self.apply("reject")
        self.assertEqual(out["reason"], "arbiter-reject")
        self.assertEqual(len(closed), 1)
        rev.assert_not_called()

    def test_revise_dispatches_the_final_round(self) -> None:
        out, closed, _, rev = self.apply("revise", {"outcome": "revise-dispatched", "reviser": "s1"})
        self.assertEqual(out["outcome"], "arbitration-revise-dispatched")
        self.assertTrue(rev.call_args.kwargs["final"])
        self.assertEqual(closed, [])

    def test_undeliverable_final_round_closes(self) -> None:
        out, closed, _, _ = self.apply("revise", {"outcome": "revise-skipped", "code": "revise_author_offline"})
        self.assertEqual(out["reason"], "arbiter-revise-undeliverable")
        self.assertEqual(len(closed), 1)


class FinalRoundGate(unittest.TestCase):
    CONFIG = types.SimpleNamespace(revise_round_limit=2, revise_daily_cap=6, arbitration_mode="true")

    def gate(self, lineage: list[dict], *, final: bool):
        with patch.object(promotion, "_comment_once", return_value=True):
            return promotion._revise_round_gate(self.CONFIG, [], lineage, round_no=len(lineage) + 1,
                                                pr=PR, head=HEAD, final=final)

    def test_final_round_passes_the_limit_once(self) -> None:
        two = [revise("v1", NODE), revise("v2", NODE)]
        self.assertEqual(self.gate(two, final=False)["outcome"], "revise-handoff")
        self.assertIsNone(self.gate(two, final=True))
        self.assertEqual(self.gate(two + [revise("v3", "s", final=True)], final=True)["code"],
                         "revise_final_already")

    def test_no_ordinary_round_after_the_final_round(self) -> None:
        # Field case 2026-10-21 (fleet-skills#458 -> #546): a lineage whose only
        # round was the final one was still under the limit and got an ordinary
        # revise round after the post-final review said `revise`.
        with patch.object(promotion, "_comment_once", return_value=True) as comment:
            out = promotion._revise_round_gate(self.CONFIG, [], [revise("v1", "s", final=True)], round_no=2,
                                               pr=PR, head=HEAD, final=False)
        self.assertEqual(out["outcome"], "revise-handoff")
        self.assertEqual(comment.call_args.kwargs["marker"], "round-limit")
        self.assertIsNone(self.gate([revise("v1", NODE)], final=False))  # ordinary lineage unchanged


class ReopenAfterArbitrationClose(unittest.TestCase):
    def test_sweep_leaves_an_owner_reopened_head_alone(self) -> None:
        rows = [review("t1", "r1"), verdict("t1", "reject"),
                {"kind": "a2a-arbitration-closed", "pr": PR, "head_sha": HEAD, "reason": "arbiter-reject"}]
        out, written, dispatched, closed = SweepArbitration.run_sweep(SweepArbitration(), rows)
        self.assertEqual((out, written, dispatched, closed), ([], [], [], []))

    def poll(self, rows: list[dict], live: str):
        written: list[dict] = []
        polled: list[str] = []
        config = types.SimpleNamespace(collect_window=10)

        def pr_state(_config, pr):
            polled.append(pr)
            return live

        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_approve_lineage_prs", return_value={PR: "2026-10-21T05:01:16Z", "77": "x"}), \
             patch.object(promotion, "_pr_state", side_effect=pr_state), \
             patch.object(promotion, "_append_ledger", side_effect=lambda c, r: written.append(r)):
            out = promotion._sweep_intake_states(config, dry_run=False)
        return out, written, polled

    def test_arbiter_closed_pr_is_repolled_and_reopen_recorded(self) -> None:
        # Field case 2026-10-21 (fleet-skills#532): reopened after a wrong
        # arbitration close, but the sticky CLOSED kept it out of promotion.
        rows = [{"kind": "a2a-arbitration-closed", "pr": PR, "head_sha": HEAD},
                {"kind": "a2a-intake-state", "pr": PR, "state": "CLOSED"},
                {"kind": "a2a-intake-state", "pr": "77", "state": "CLOSED"}]
        out, written, polled = self.poll(rows, "OPEN")
        self.assertEqual(polled, [PR])  # an ordinary CLOSED stays sticky
        self.assertEqual(written[0]["state"], "OPEN")
        self.assertEqual(out, [{"outcome": "intake-state-recorded", "pr": PR, "state": "OPEN"}])
        out, written, polled = self.poll(rows, "CLOSED")
        self.assertEqual((polled, written, out), ([PR], [], []))  # unchanged: no ledger churn


class ArbitrationDispatchGuard(unittest.TestCase):
    def attempt(self, rows: list[dict]):
        captured: dict = {}

        def manifest(_config, _candidate, **kwargs):
            captured["procedure"] = kwargs["procedure"]
            raise promotion.PromotionError("stop_after_manifest")

        def target(_config, _node, _secret, *, disqualified):
            captured["disqualified"] = set(disqualified)
            return "arb", None

        with TemporaryDirectory() as tmp:
            Path(tmp, "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            Path(tmp, "scripts").mkdir()
            Path(tmp, "scripts", "a2a-dispatch-round.mjs").write_text("", encoding="utf-8")
            config = types.SimpleNamespace(promotion_state_dir=Path(tmp), dispatch_ci_wait_sec=0,
                                           a2a_nexus_dir=Path(tmp), broker_url="https://p.example")
            with patch.dict("os.environ", {"A2A_EDGE_SECRET": "x"}), \
                 patch.object(promotion, "_branch_head_sha", return_value=HEAD), \
                 patch.object(promotion, "_wait_for_skills_check", return_value=True), \
                 patch.object(promotion, "_worker_procedure_from_docs", return_value="PROCEDURE"), \
                 patch.object(promotion, "_lineage_revisers", return_value=set()), \
                 patch.object(promotion, "_dispatch_target_worker", side_effect=target), \
                 patch.object(promotion, "_broker_id", return_value="primary"), \
                 patch.object(promotion, "_inventory_snapshot", return_value=[]), \
                 patch.object(promotion, "_build_dispatch_manifest", side_effect=manifest):
                result = promotion._dispatch_intake_review_attempt(
                    config, types.SimpleNamespace(node=NODE, name=NAME),
                    {"url": URL, "branch": OPEN_PR["branch"]}, transport_id=TRANSPORT,
                    arbitration={"trigger": "reject", "exclude": ["r1", NODE], "appendix": "## ARB"},
                )
        return result, captured

    def test_reviewed_head_still_gets_its_arbitration_round(self) -> None:
        result, captured = self.attempt([review("t1", "r1")])
        self.assertEqual(result.get("code"), "stop_after_manifest")
        self.assertEqual(captured["disqualified"], {"r1", NODE})
        self.assertTrue(captured["procedure"].startswith("PROCEDURE"))
        self.assertIn("## ARB", captured["procedure"])

    def test_second_arbitration_round_is_blocked(self) -> None:
        result, captured = self.attempt([review("t1", "r1"), review("t2", "arb", arbitration=True)])
        self.assertEqual(result.get("code"), "dispatch_already")
        self.assertEqual(captured, {})


if __name__ == "__main__":
    unittest.main()
