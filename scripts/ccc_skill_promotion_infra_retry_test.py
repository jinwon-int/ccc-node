#!/usr/bin/env python3
"""#2435 S1: infrastructure failures are retried on another node, not handed to a human.

Field cases (2026-10-07..20): node-c/node-d Claude OAuth expiry, node-e
Claude Code 2.1.278 model incompatibility and the node-f T1 lane's glm
balance exhaustion all failed review/revise tasks before any result existed.
The publisher recorded each one as `malformed` / `failed` with "no automatic
retry; human judgment continues", and the intake PRs waited for an operator
(#406 #407 #413 #415 #416 #420 #518 #522 #532 #534). These tests pin:

- the classifier reads only the broker error text of a result-less failure;
- a classified review failure is consumed as `infra_failed` (once per head),
  leaves the head owed to `_missing_dispatch_prs`, does not trip the dispatch
  idempotency guard, and disqualifies the failed reviewer for the retry;
- a classified revise failure is consumed as `infra_failed` and re-dispatched
  immediately with the same findings, excluding the failed node, without
  using up the round limit, the per-head guard or the lineage's one substitute;
- the second infrastructure failure falls back to the old terminal record.
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
    "promotion_infra_retry_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

HEAD = "a" * 40
TRANSPORT = "nodea-claude-skill-x-0123456789ab"
OAUTH = (
    "a2a-intent-dispatcher: routing intent=skills_intake_review\n"
    "skills-intake-review-handler: prompt built: 38692 bytes\n"
    "skills-intake-review-handler: review agent run failed: out[Failed to authenticate: "
    "OAuth session expired and could not be refreshed] err[]"
)
BALANCE = (
    'reviser agent run failed: out[] err[429: {"code":"1113","message":"Insufficient balance '
    'or no resource package. Please recharge."}]'
)
CLI = "review agent run failed: out[API Error: 400 Claude Code 2.1.278 does not support claude-opus-5-5]"


def failed_task(message: str, *, code: str = "handler_exit_nonzero", payload: object = None) -> dict:
    task: dict = {"status": "failed", "error": {"code": code, "message": message,
                                                "details": {"excerpt": message.replace("\n", " ")}}}
    if payload is not None:
        task["payload"] = payload
    return task


class Classifier(unittest.TestCase):
    def test_field_messages(self) -> None:
        self.assertEqual(promotion._infra_failure_class(failed_task(OAUTH)), "auth_expired")
        self.assertEqual(promotion._infra_failure_class(failed_task(CLI)), "cli_incompatible")
        self.assertEqual(promotion._infra_failure_class(failed_task("x", code="handler_timeout")), "timeout")
        # fleet-skills#538 (2026-10-21): Claude Code quota message, no 429.
        self.assertEqual(promotion._infra_failure_class(failed_task(
            "reviser agent run failed (rc=1): out[You've hit your session limit · resets 10:30am (UTC) ] err[]")),
            "rate_limited")

    def test_balance_wins_over_plain_429(self) -> None:
        self.assertEqual(promotion._infra_failure_class(failed_task(BALANCE)), "balance_exhausted")
        self.assertEqual(promotion._infra_failure_class(failed_task("HTTP 429 rate limit")), "rate_limited")

    def test_results_and_unknown_errors_are_not_infra(self) -> None:
        negative = failed_task(OAUTH)
        negative["negativeVerdictEvidence"] = {"result": {"output": {"verdict": "revise", "findings": []}}}
        self.assertIsNone(promotion._infra_failure_class(negative))
        self.assertIsNone(promotion._infra_failure_class({"status": "succeeded", "result": {"output": {}}}))
        self.assertIsNone(promotion._infra_failure_class(failed_task("KeyError: 'skillFiles'")))
        self.assertIsNone(promotion._infra_failure_class({"status": "failed"}))
        self.assertIsNone(promotion._infra_failure_class({"status": "running", "error": {"message": OAUTH}}))


def dispatch_row(task: str, reviewer: str, head: str = HEAD) -> dict:
    return {"kind": "a2a-dispatch", "transport_id": TRANSPORT, "head_sha": head, "dispatched_task": task,
            "task_id": task, "reviewer_node": reviewer,
            "pr_url": "https://github.com/jinwon-int/fleet-skills/pull/534"}


def infra_verdict(task: str) -> dict:
    return {"kind": "a2a-verdict", "task_id": task, "status": "infra_failed", "head_sha": HEAD}


class MissingDispatchOwesInfraHeads(unittest.TestCase):
    PR = {"number": "534", "head": HEAD, "created_at": 0.0,
          "branch": "skill-intake/nodea/skill-x-claude-0123456789ab",
          "url": "https://github.com/jinwon-int/fleet-skills/pull/534"}

    def owed(self, rows: list[dict]) -> list:
        return promotion._missing_dispatch_prs(rows, [dict(self.PR)], now=10_000_000.0)

    def test_infra_failed_round_leaves_head_owed(self) -> None:
        self.assertEqual(len(self.owed([dispatch_row("t1", "rev1"), infra_verdict("t1")])), 1)

    def test_second_real_round_settles_head(self) -> None:
        rows = [dispatch_row("t1", "rev1"), infra_verdict("t1"), dispatch_row("t2", "rev2")]
        self.assertEqual(self.owed(rows), [])

    def test_plain_dispatch_still_settles(self) -> None:
        self.assertEqual(self.owed([dispatch_row("t1", "rev1")]), [])


class DispatchGuard(unittest.TestCase):
    def attempt(self, rows: list[dict]) -> tuple[dict, list[set]]:
        captured: list[set] = []

        def target(_config, _node, _secret, *, disqualified):
            captured.append(set(disqualified))
            raise promotion.PromotionError("dispatch_no_reviewer_online")

        with TemporaryDirectory() as tmp:
            Path(tmp, "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            config = types.SimpleNamespace(promotion_state_dir=Path(tmp), dispatch_ci_wait_sec=0,
                                           a2a_nexus_dir=Path(tmp))
            with patch.dict("os.environ", {"A2A_EDGE_SECRET": "x"}), \
                 patch.object(promotion, "_branch_head_sha", return_value=HEAD), \
                 patch.object(promotion, "_wait_for_skills_check", return_value=True), \
                 patch.object(promotion, "_worker_procedure_from_docs", return_value=""), \
                 patch.object(promotion, "_lineage_revisers", return_value=set()), \
                 patch.object(promotion, "_dispatch_target_worker", side_effect=target):
                result = promotion._dispatch_intake_review_attempt(
                    config, types.SimpleNamespace(node="nodea", name="skill-x"),
                    {"url": "https://github.com/jinwon-int/fleet-skills/pull/534",
                     "branch": "skill-intake/nodea/skill-x-claude-0123456789ab"},
                    transport_id=TRANSPORT,
                )
        return result, captured

    def test_infra_failed_round_is_not_dispatch_already_and_reviewer_is_excluded(self) -> None:
        result, captured = self.attempt([dispatch_row("t1", "node-f"), infra_verdict("t1")])
        self.assertEqual(result.get("code"), "dispatch_no_reviewer_online")
        self.assertEqual(captured, [{"node-f"}])

    def test_real_round_still_blocks_redispatch(self) -> None:
        result, captured = self.attempt([dispatch_row("t1", "node-f")])
        self.assertEqual(result.get("code"), "dispatch_already")
        self.assertEqual(captured, [])


class VerdictCollection(unittest.TestCase):
    def run_collect(self, rows: list[dict], task: dict) -> tuple[list, list, list]:
        written: list[dict] = []
        comments: list[str] = []
        config = types.SimpleNamespace(collect_window=8)
        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_retry_intake_receipts", return_value=[]), \
             patch.object(promotion, "_broker_task_gated", return_value=(task, "ok")), \
             patch.object(promotion, "_append_ledger", side_effect=lambda c, r: written.append(r)), \
             patch.object(promotion, "_comment_once",
                          side_effect=lambda *a, **k: comments.append(k.get("marker", ""))):
            processed = promotion._process_verdicts(config, dry_run=False)
        return processed, written, comments

    def test_first_infra_failure_is_consumed_as_infra_failed(self) -> None:
        processed, written, comments = self.run_collect([dispatch_row("t1", "node-f")], failed_task(BALANCE))
        self.assertEqual(processed[-1]["outcome"], "verdict-infra-failed")
        self.assertEqual(written[-1]["status"], "infra_failed")
        self.assertEqual(written[-1]["reason"], "balance_exhausted")
        self.assertEqual(written[-1]["reviewer_node"], "node-f")
        self.assertEqual(comments, ["verdict-infra-failed:t1"])

    def test_second_infra_failure_falls_back_to_malformed(self) -> None:
        rows = [dispatch_row("t1", "node-c"), infra_verdict("t1"), dispatch_row("t2", "node-d")]
        processed, written, _ = self.run_collect(rows, failed_task(OAUTH))
        self.assertEqual(processed[-1]["outcome"], "verdict-malformed")
        self.assertEqual(written[-1]["status"], "malformed")

    def test_non_infra_failure_is_still_malformed(self) -> None:
        processed, written, _ = self.run_collect([dispatch_row("t1", "node-c")], failed_task("KeyError: x"))
        self.assertEqual(processed[-1]["outcome"], "verdict-malformed")
        self.assertEqual(written[-1]["status"], "malformed")


def revise_dispatch(task: str, reviser: str, *, substitute: bool = False) -> dict:
    row = {"kind": "a2a-revise-dispatch", "task_id": task, "pr": "532", "head_sha": HEAD, "node": "node-h",
           "name": "skill-x", "provider": "codex", "tree_sha256": "b" * 64, "reviser_node": reviser,
           "round": 1}
    if substitute:
        row["substitute"] = True
    return row


def review_origin() -> dict:
    return {"kind": "a2a-dispatch", "transport_id": TRANSPORT, "head_sha": HEAD, "dispatched_task": "r1",
            "reviewer_node": "node-e", "pr_url": "https://github.com/jinwon-int/fleet-skills/pull/532"}


FINDINGS_PAYLOAD = {"payload": {"findings": [{"severity": "major", "area": "claims", "note": "fix it"}]}}


class ReviseCollection(unittest.TestCase):
    def run_collect(self, rows: list[dict], task: dict) -> tuple[list, list, list]:
        written: list[dict] = []
        calls: list[dict] = []
        config = types.SimpleNamespace(collect_window=8)

        def fake_dispatch(_config, origin, _rows, findings, reviewer, *, dry_run, exclude=frozenset(),
                          final=False, identity=False):
            calls.append({"origin": origin.get("dispatched_task"), "findings": findings,
                          "reviewer": reviewer, "exclude": set(exclude),
                          **({"final": True} if final else {}), **({"identity": True} if identity else {})})
            return {"outcome": "revise-dispatched", "reviser": "node-g", "task_id": "rv2"}

        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_broker_task_gated", return_value=(task, "ok")), \
             patch.object(promotion, "_append_ledger", side_effect=lambda c, r: written.append(r)), \
             patch.object(promotion, "_comment_once", return_value=None), \
             patch.object(promotion, "_dispatch_intake_revise", side_effect=fake_dispatch):
            results = promotion._consume_revise_results(config, dry_run=False)
        return results, written, calls

    def test_infra_failed_revise_is_redispatched_excluding_the_failed_node(self) -> None:
        rows = [review_origin(), revise_dispatch("rv1", "node-f", substitute=True)]
        task = failed_task(BALANCE, payload=FINDINGS_PAYLOAD["payload"])
        results, written, calls = self.run_collect(rows, task)
        self.assertEqual(results[-1]["outcome"], "revise-infra-failed")
        self.assertEqual(written[-1]["status"], "infra_failed")
        self.assertEqual(calls, [{"origin": "r1", "reviewer": "node-e", "exclude": {"node-f"},
                                  "findings": [{"severity": "major", "area": "claims", "note": "fix it"}]}])

    def test_identity_and_final_rounds_are_retried_as_the_same_kind(self) -> None:
        # #2435 S4 field case: the #538 generalization round failed on a session
        # limit and was re-dispatched as an ordinary round would have been.
        for flag in ("identity", "final"):
            with self.subTest(flag=flag):
                rows = [review_origin(), {**revise_dispatch("rv1", "node-f"), flag: True}]
                task = failed_task("You've hit your session limit", payload=FINDINGS_PAYLOAD["payload"])
                results, written, calls = self.run_collect(rows, task)
                self.assertEqual(written[-1]["status"], "infra_failed")
                self.assertTrue(calls[0].get(flag))

    def test_second_infra_failure_is_terminal_failed(self) -> None:
        rows = [review_origin(), revise_dispatch("rv1", "node-f"),
                {"kind": "a2a-revise-result", "task_id": "rv1", "status": "infra_failed"},
                revise_dispatch("rv2", "node-g")]
        task = failed_task(OAUTH, payload=FINDINGS_PAYLOAD["payload"])
        with patch.object(promotion, "_infra_failed_task_ids", wraps=promotion._infra_failed_task_ids):
            results, written, calls = self.run_collect(rows, task)
        self.assertEqual([r["outcome"] for r in results], ["revise-failed"])
        self.assertEqual(written[-1]["status"], "failed")
        self.assertEqual(calls, [])


class InfraRoundsDoNotConsumeBudgets(unittest.TestCase):
    def test_per_head_guard_ignores_infra_rounds(self) -> None:
        rows = [revise_dispatch("rv1", "node-f"),
                {"kind": "a2a-revise-result", "task_id": "rv1", "status": "infra_failed"}]
        self.assertFalse(promotion._revise_dispatched(rows, "532", HEAD))
        self.assertTrue(promotion._revise_dispatched([revise_dispatch("rv1", "node-f")], "532", HEAD))

    def test_one_substitute_rule_ignores_infra_rounds(self) -> None:
        sub = revise_dispatch("rv1", "node-f", substitute=True)
        self.assertTrue(promotion._revise_substitute_used([sub], "node-h", "skill-x"))
        rows = [sub, {"kind": "a2a-revise-result", "task_id": "rv1", "status": "infra_failed"}]
        self.assertFalse(promotion._revise_substitute_used(rows, "node-h", "skill-x"))

    def test_excluded_author_goes_to_an_independent_substitute(self) -> None:
        config = types.SimpleNamespace(remote_brokers=(), broker_url="https://p.example",
                                       revise_substitute_after_days=0, revise_non_worker_authors=())
        with patch.object(promotion, "_broker_online_worker_ids", return_value={"nodea", "rev", "sub1", "sub2"}), \
             patch.object(promotion, "_remote_online_worker_ids", return_value=set()), \
             patch.object(promotion, "_keyring_worker_ids", return_value=["nodea", "rev", "sub1", "sub2"]), \
             patch.object(promotion, "_broker_id", return_value="primary"):
            _rb, _bid, _url, reviser, substitute = promotion._resolve_revise_target(
                config, {}, [], "nodea", "skill-x", "rev", "0123456789ab", "secret",
                exclude=frozenset({"nodea", "sub1"}),
            )
        self.assertEqual((reviser, substitute), ("sub2", "sub2"))

    def test_without_exclusion_b2_off_author_offline_stays_skipped(self) -> None:
        config = types.SimpleNamespace(remote_brokers=(), broker_url="https://p.example",
                                       revise_substitute_after_days=0, revise_non_worker_authors=())
        with patch.object(promotion, "_broker_online_worker_ids", return_value={"rev", "sub1"}), \
             patch.object(promotion, "_remote_online_worker_ids", return_value=set()), \
             patch.object(promotion, "_keyring_worker_ids", return_value=["nodea", "rev", "sub1"]):
            with self.assertRaises(promotion.PromotionError) as raised:
                promotion._resolve_revise_target(
                    config, {}, [], "nodea", "skill-x", "rev", "0123456789ab", "secret")
        self.assertEqual(raised.exception.code, "revise_author_offline")


if __name__ == "__main__":
    unittest.main()
