#!/usr/bin/env python3
"""#2463 slice 1: the effect-validation (replay) gate records plans and changes nothing else.

Pins: eligibility classification; the tri-state switch (off default, env or
owner flag file, typo fails closed); `_sweep_replay_plans` is inert when off,
writes one `a2a-replay-plan` per approved unpromoted tree (idempotent), previews
under dry-run, and notes `replay_worker_not_deployed` under `true`; the
promote body gets a section only when the gate is on.
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
    "promotion_replay_gate_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

TREE_A, TREE_B = "a" * 64, "b" * 64


def publish_row(pr: str, eligibility: str) -> dict:
    return {"url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}", "outcome": "pr-opened",
            "replay_eligibility": eligibility, "node": "nodea", "provider": "claude", "name": f"s{pr}"}


class Eligibility(unittest.TestCase):
    def test_classification(self) -> None:
        self.assertEqual(promotion._replay_eligibility(["SKILL.md", "references/a.md"]), "narrative")
        self.assertEqual(promotion._replay_eligibility(["SKILL.md", "scripts/run.sh"]), "script-bearing")
        self.assertEqual(promotion._replay_eligibility(["SKILL.md", "tests/t.py"]), "script-bearing")
        self.assertEqual(promotion._replay_eligibility(["SKILL.md", "templates/x.test.sh"]), "script-bearing")
        self.assertEqual(promotion._replay_eligibility([]), "narrative")

    def test_latest_record_wins(self) -> None:
        rows = [publish_row("7", "narrative"),
                {"kind": "a2a-replay-plan", "pr": "7", "eligibility": "script-bearing"}]
        self.assertEqual(promotion._replay_eligibility_by_pr(rows), {"7": "script-bearing"})


class Switch(unittest.TestCase):
    def test_default_env_and_typo(self) -> None:
        with TemporaryDirectory() as tmp:
            flag = Path(tmp) / "skill-promotion.replay-gate"
            self.assertEqual(promotion._replay_gate_mode(None, flag, trust_root=Path(tmp)), "off")
            self.assertEqual(promotion._replay_gate_mode("dry-run", flag, trust_root=Path(tmp)), "dry-run")
            self.assertEqual(promotion._replay_gate_mode("true", flag, trust_root=Path(tmp)), "true")
            with self.assertRaises(promotion.PromotionError) as caught:
                promotion._replay_gate_mode("bogus", flag, trust_root=Path(tmp))
            self.assertEqual(caught.exception.code, "replay_gate_invalid")


class SweepReplayPlans(unittest.TestCase):
    def sweep(self, rows: list[dict], *, mode: str, dry_run: bool = False):
        written: list[dict] = []
        config = types.SimpleNamespace(replay_gate_mode=mode, collect_window=8)
        pending = [{"pr": "7", "tree_sha256": TREE_A, "node": "nodea", "name": "s7", "provider": "claude"},
                   {"pr": "8", "tree_sha256": TREE_B, "node": "nodeb", "name": "s8", "provider": "claude"}]
        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_promoted_source_trees", return_value={}), \
             patch.object(promotion, "_promotable", return_value=pending), \
             patch.object(promotion, "_append_ledger", side_effect=lambda c, r: written.append(r)):
            out = promotion._sweep_replay_plans(config, dry_run=dry_run)
        return out, written

    def test_off_is_inert(self) -> None:
        self.assertEqual(self.sweep([publish_row("7", "script-bearing")], mode="off"), ([], []))

    def test_dry_run_mode_plans_once_per_tree(self) -> None:
        rows = [publish_row("7", "script-bearing"), publish_row("8", "narrative")]
        out, written = self.sweep(rows, mode="dry-run")
        self.assertEqual([(o["pr"], o["status"]) for o in out], [("7", "planned"), ("8", "skipped-narrative")])
        self.assertEqual([w["kind"] for w in written], ["a2a-replay-plan"] * 2)
        self.assertNotIn("note", written[0])
        again, more = self.sweep(rows + written, mode="dry-run")
        self.assertEqual((again, more), ([], []))

    def test_collect_dry_run_writes_nothing(self) -> None:
        out, written = self.sweep([publish_row("7", "script-bearing")], mode="dry-run", dry_run=True)
        self.assertEqual(out[0]["outcome"], "would-plan-replay")
        self.assertEqual(written, [])

    def test_true_notes_missing_worker(self) -> None:
        _, written = self.sweep([publish_row("7", "script-bearing")], mode="true")
        self.assertEqual(written[0]["note"], "replay_worker_not_deployed")

    def test_unreadable_promoted_tree_skips(self) -> None:
        config = types.SimpleNamespace(replay_gate_mode="dry-run", collect_window=8)
        with patch.object(promotion, "_ledger_rows", return_value=[]), \
             patch.object(promotion, "_promoted_source_trees", side_effect=promotion.PromotionError("gh_down")):
            self.assertEqual(promotion._sweep_replay_plans(config, dry_run=False),
                             [{"outcome": "replay-plan-skipped", "code": "gh_down"}])


class PromoteBody(unittest.TestCase):
    def test_section_only_when_on(self) -> None:
        staged = [{"pr": "7", "name": "s7"}]
        self.assertEqual(promotion._replay_body_lines([publish_row("7", "narrative")], staged, "off"), [])
        lines = promotion._replay_body_lines([publish_row("7", "narrative")], staged, "dry-run")
        self.assertTrue(any("effect-validation gate" in line for line in lines))
        self.assertIn("- #7 `s7`: `narrative`", lines)


if __name__ == "__main__":
    unittest.main()
