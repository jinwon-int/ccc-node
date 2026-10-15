#!/usr/bin/env python3
"""The revise packet must state the publisher's path rule, and say what it rejected.

Field case, 2026-09-11 → 2026-10-15 (ccc-node#1647). The a2a-nexus worker
procedure pasted into every revise packet told revisers to put output
resources in ``assets/``, but ``_candidate_from_envelope`` only accepts
``SKILL.md`` plus ``_ALLOWED_SUPPORT_DIRS``. Every revision that created an
``assets/`` file failed as ``envelope_path_invalid`` (11 on the collector
ledger) and the ledger row did not say which path, so the cause took a
broker-DB dig to find. The docs were fixed upstream (a2a-nexus#2385), but the
publisher reads them from a pinned checkout, so the rule must also travel in
the packet from code:

- the revise manifest message and ``reviseResultSchema`` carry a path rule
  generated from ``_ALLOWED_SUPPORT_DIRS`` (so it cannot drift from the gate);
- an ``envelope_path_invalid`` ledger row names the rejected top-level dirs.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_support_dirs_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

TREE = "a" * 64
HEAD = "c" * 40


class PathRuleText(unittest.TestCase):
    def test_rule_names_exactly_the_gate_dirs(self) -> None:
        rule = promotion._SUPPORT_PATH_RULE
        self.assertIn("SKILL.md", rule)
        for directory in promotion._ALLOWED_SUPPORT_DIRS:
            self.assertIn(f"{directory}/", rule)
        self.assertIn("envelope_path_invalid", rule)
        self.assertNotIn("assets/", rule)  # named only as a rejected example

    def test_result_schema_carries_the_rule(self) -> None:
        self.assertEqual(promotion._REVISE_RESULT_SCHEMA["pathRule"], promotion._SUPPORT_PATH_RULE)

    def test_manifest_message_carries_the_rule(self) -> None:
        config = types.SimpleNamespace(broker_url="http://broker.invalid", node="node-a",
                                       revise_round_limit=2)
        manifest = promotion._build_revise_manifest(
            config, pr_number="433", branch="skill-intake/node-b/x", head=HEAD, node="node-b",
            provider="claude", name="x", tree_sha256=TREE, round_no=1,
            findings=[{"severity": "major", "area": "structure", "summary": "s"}],
            skill_files=[{"path": "SKILL.md", "content": "---\nname: x\n---\n"}],
            procedure="p" * 300, broker_id="t1", now="20261015T000000Z",
        )
        lane = manifest["lanes"][0]
        self.assertIn(promotion._SUPPORT_PATH_RULE, lane["message"])
        self.assertEqual(lane["payload"]["reviseResultSchema"]["pathRule"], promotion._SUPPORT_PATH_RULE)


class DisallowedRoots(unittest.TestCase):
    def test_roots_outside_the_gate_are_reported(self) -> None:
        roots = promotion._disallowed_path_roots(
            ["SKILL.md", "references/a.md", "assets/report.md", "assets/b.json", "notes.md"]
        )
        self.assertEqual(roots, ["assets", "notes.md"])

    def test_clean_layout_reports_nothing(self) -> None:
        self.assertEqual(
            promotion._disallowed_path_roots(["SKILL.md", "scripts/a.sh", "templates/t.md"]), []
        )


class InvalidRowNamesTheRoot(unittest.TestCase):
    def run_invalid(self, revised: list[tuple[str, bytes]], code: str) -> dict:
        captured: list[dict] = []
        config = types.SimpleNamespace(supersede_autoclose_enabled=False)
        with patch.object(promotion, "_pr_skill_files", return_value=[]), \
             patch.object(promotion, "_candidate_from_revised_files",
                          side_effect=promotion.PromotionError(code)), \
             patch.object(promotion, "_append_ledger", side_effect=lambda _c, r: captured.append(r)), \
             patch.object(promotion, "_comment_once"):
            result = promotion._republish_revised_candidate(
                config, [], revised=revised, task_id="t", pr="433", head=HEAD,
                node="node-b", name="x", provider="claude", tree=TREE,
            )
        self.assertEqual(result.get("outcome"), "revise-invalid")
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_envelope_path_invalid_row_lists_rejected_roots(self) -> None:
        row = self.run_invalid(
            [("SKILL.md", b"x"), ("assets/receipt-template.json", b"{}")], "envelope_path_invalid"
        )
        self.assertEqual(row["code"], "envelope_path_invalid")
        self.assertEqual(row["path_roots"], ["assets"])

    def test_other_codes_do_not_add_path_roots(self) -> None:
        row = self.run_invalid([("SKILL.md", b"x")], "skill_frontmatter_invalid")
        self.assertNotIn("path_roots", row)


if __name__ == "__main__":
    unittest.main()
