#!/usr/bin/env python3
"""A skipped intake review dispatch must be recorded and retried.

Field case, 2026-10-05 → 2026-10-07. The T1 edge secret was rotated on
2026-10-05 and the publisher's `~/.a2a-broker-edge.env` was not in the
consumer inventory. From the next nightly collect every `_dispatch_intake_review`
returned `dispatch_broker_unreachable`; that dict went into a cron log line
truncated at 500 bytes and nothing retried, so nine intake PRs (#405–#417)
opened and sat without a review round. These tests pin:

- the wrapper writes an `a2a-dispatch-skipped` ledger row for every skip and
  stays silent on success;
- `_missing_dispatch_prs` selects exactly the open intake PRs whose head has
  no dispatch/receipt/verdict row, oldest first, bounded, and leaves PRs the
  publish path just opened alone;
- `_intake_branch_parts` parses the intake branch lineage the sweep needs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_redispatch_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40
NOW = 1_800_000_000.0


def open_pr(number: str, branch: str, head: str, age_sec: float) -> dict:
    return {"number": number, "url": f"https://github.com/jinwon-int/fleet-skills/pull/{number}",
            "branch": branch, "head": head, "created_at": NOW - age_sec}


class IntakeBranchParts(unittest.TestCase):
    def test_parses_lineage(self) -> None:
        parts = promotion._intake_branch_parts(
            "skill-intake/nodea/ci-build-variant-coverage-validation-claude-d551fec013e2"
        )
        self.assertEqual(parts, {"node": "nodea", "name": "ci-build-variant-coverage-validation",
                                 "provider": "claude", "suffix": "d551fec013e2"})

    def test_rejects_foreign_branches(self) -> None:
        for branch in ("main", "promote/auto-20261006T101511Z", "skill-intake/nodea/x", "",
                       "skill-intake/nodea/name-unknownprovider-d551fec013e2"):
            self.assertIsNone(promotion._intake_branch_parts(branch), branch)


class MissingDispatchSelection(unittest.TestCase):
    def test_selects_only_unsettled_heads_oldest_first_and_bounded(self) -> None:
        rows = [
            {"kind": "a2a-dispatch", "head_sha": HEAD_A},
            {"kind": "a2a-receipt", "head_sha": HEAD_B},
        ]
        prs = [
            open_pr("1", "skill-intake/nodea/alpha-claude-" + "1" * 12, HEAD_A, 90_000),   # dispatched
            open_pr("2", "skill-intake/nodeb/beta-claude-" + "2" * 12, HEAD_B, 80_000),    # receipt
            open_pr("3", "skill-intake/nodec/gamma-danso-" + "3" * 12, HEAD_C, 70_000),     # owed
            open_pr("4", "skill-intake/noded/delta-claude-" + "4" * 12, "d" * 40, 60_000),  # owed
            open_pr("5", "skill-intake/nodee/eps-codex-" + "5" * 12, "e" * 40, 50_000),  # owed
            open_pr("6", "skill-intake/nodef/zeta-claude-" + "6" * 12, "f" * 40, 40_000),  # owed, beyond limit
            open_pr("7", "skill-intake/nodef/eta-claude-" + "7" * 12, "0" * 40, 30),       # just opened: left to publish path
            open_pr("8", "promote/auto-20261006T101511Z", "9" * 40, 90_000),               # not an intake branch
        ]
        owed = promotion._missing_dispatch_prs(rows, prs, now=NOW)
        self.assertEqual([pr["number"] for pr in owed], ["3", "4", "5"])

    def test_verdict_row_also_settles(self) -> None:
        rows = [{"kind": "a2a-verdict", "head_sha": HEAD_C}]
        prs = [open_pr("3", "skill-intake/nodec/gamma-danso-" + "3" * 12, HEAD_C, 70_000)]
        self.assertEqual(promotion._missing_dispatch_prs(rows, prs, now=NOW), [])

    def test_malformed_entries_are_ignored(self) -> None:
        prs = [{"number": "x"}, {"number": "y", "branch": "skill-intake/a/b-claude-" + "1" * 12, "head": None, "created_at": 1}]
        self.assertEqual(promotion._missing_dispatch_prs([], prs, now=NOW), [])


class SkipRowRecording(unittest.TestCase):
    def run_wrapper(self, attempt_result: dict) -> list[dict]:
        with TemporaryDirectory() as temp:
            state = Path(temp) / "skill-promotion"
            state.mkdir(mode=0o700)
            config = type("Cfg", (), {})()
            config.promotion_state_dir = state
            candidate = type("Cand", (), {})()
            candidate.node, candidate.provider, candidate.name, candidate.tree_sha256 = "nodea", "claude", "alpha", "a" * 64
            with patch.object(promotion, "_dispatch_intake_review_attempt", return_value=attempt_result):
                result = promotion._dispatch_intake_review(
                    config, candidate,
                    {"url": "https://github.com/jinwon-int/fleet-skills/pull/417", "branch": "skill-intake/nodea/alpha-claude-" + "a" * 12},
                    transport_id="nodea-claude-alpha-" + "a" * 12,
                )
            self.assertEqual(result, attempt_result)
            ledger = state / "ledger.jsonl"
            if not ledger.exists():
                return []
            return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_skip_is_recorded_with_code(self) -> None:
        rows = self.run_wrapper({"outcome": "dispatch-skipped", "code": "dispatch_broker_unreachable"})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["kind"], "a2a-dispatch-skipped")
        self.assertEqual(row["code"], "dispatch_broker_unreachable")
        self.assertEqual(row["branch"], "skill-intake/nodea/alpha-claude-" + "a" * 12)
        self.assertEqual(row["pr_url"], "https://github.com/jinwon-int/fleet-skills/pull/417")
        self.assertEqual(row["node"], "nodea")
        self.assertRegex(row["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_success_writes_nothing(self) -> None:
        rows = self.run_wrapper({"outcome": "dispatched", "task_id": "t", "dispatched_task": "t", "reviewer_node": "nodee", "round_id": "r"})
        self.assertEqual(rows, [])


class DrainExposesRedispatch(unittest.TestCase):
    def test_sweep_disabled_without_dispatch_flag(self) -> None:
        config = type("Cfg", (), {"dispatch_enabled": False})()
        self.assertEqual(promotion._sweep_missing_dispatch(config, dry_run=True), [])

    def test_dry_run_reports_would_redispatch(self) -> None:
        config = type("Cfg", (), {"dispatch_enabled": True})()
        prs = [open_pr("3", "skill-intake/nodec/gamma-danso-" + "3" * 12, HEAD_C, 70_000)]
        with patch.object(promotion, "_ledger_rows", return_value=[]), \
                patch.object(promotion, "_open_intake_prs", return_value=prs):
            out = promotion._sweep_missing_dispatch(config, dry_run=True, now=NOW)
        self.assertEqual(out, [{"pr": "3", "branch": prs[0]["branch"], "head": HEAD_C, "outcome": "would-redispatch"}])

    def test_gh_failure_is_reported_not_raised(self) -> None:
        config = type("Cfg", (), {"dispatch_enabled": True})()
        with patch.object(promotion, "_ledger_rows", return_value=[]), \
                patch.object(promotion, "_open_intake_prs", side_effect=promotion.PromotionError("github_output_invalid")):
            out = promotion._sweep_missing_dispatch(config, dry_run=False)
        self.assertEqual(out, [{"outcome": "redispatch-skipped", "code": "github_output_invalid"}])


if __name__ == "__main__":
    unittest.main()
