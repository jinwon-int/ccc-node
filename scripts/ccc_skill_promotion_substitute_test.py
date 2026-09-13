"""DOC-3366 B2: substitute revise dispatch — eligibility, pick and broker.

The substitute path fires only when the author is online nowhere AND the
author-offline skip has aged past config.revise_substitute_after_days. The
picked worker is never the author and never the reviewer of record
(policies/REVIEW.md), and the pick is deterministic for a given lineage.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_substitute_test", Path(__file__).with_name("ccc-skill-promotion.py"))
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

T2 = {"name": "t2", "broker_url": "https://t2.example", "ssh_host": "vps7", "nexus_dir": "/opt/nexus"}


def config(substitute_days: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        node="seoseo",
        remote_brokers=(T2,),
        revise_substitute_after_days=substitute_days,
        broker_url="https://primary.example",
    )


def aged_row(node: str, name: str, ts: str | None = None) -> dict:
    ts = ts or (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y%m%dT%H%M%SZ")
    return {"kind": "a2a-revise-comment", "node": node, "name": name,
            "marker": "round-limit:revise_author_offline", "ts": ts}


class SubstituteDueTests(unittest.TestCase):
    def test_off_by_default(self):
        rows = [aged_row("gwakga", "s1")]
        cfg = config(0)
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: [],
                            _remote_online_worker_ids=lambda c, rb: []):
            self.assertFalse(promotion._revise_substitute_due(cfg, rows, "gwakga", "s1"))

    def test_aged_skip_is_due(self):
        cfg = config(7)
        rows = [aged_row("gwakga", "s1")]
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: [],
                            _remote_online_worker_ids=lambda c, rb: []):
            self.assertTrue(promotion._revise_substitute_due(cfg, rows, "gwakga", "s1"))

    def test_fresh_skip_is_not_due(self):
        cfg = config(7)
        rows = [aged_row("gwakga", "s1", ts=(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ"))]
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: [],
                            _remote_online_worker_ids=lambda c, rb: []):
            self.assertFalse(promotion._revise_substitute_due(cfg, rows, "gwakga", "s1"))

    def test_unrelated_rows_never_make_it_due(self):
        cfg = config(7)
        rows = [aged_row("gwakga", "other-skill"), {"kind": "a2a-revise-comment", "ts": "20260901T000000Z"}]
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: [],
                            _remote_online_worker_ids=lambda c, rb: []):
            self.assertFalse(promotion._revise_substitute_due(cfg, rows, "gwakga", "s1"))


class SubstitutePickTests(unittest.TestCase):
    def test_excludes_author_and_reviewer_and_is_deterministic(self):
        cfg = config(7)
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: ["gwakga", "nosuk"],
                            _remote_online_worker_ids=lambda c, rb: ["yukson"]):
            first = promotion._revise_substitute_pick(cfg, "s", "gwakga", "nosuk", "key-1")
            second = promotion._revise_substitute_pick(cfg, "s", "gwakga", "nosuk", "key-1")
            self.assertEqual(first, second)
            self.assertIn(first, {"yukson"})
            self.assertNotIn(first, {"gwakga", "nosuk"})

    def test_no_candidate_when_only_author_and_reviewer_online(self):
        cfg = config(7)
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: ["gwakga"],
                            _remote_online_worker_ids=lambda c, rb: ["nosuk"]):
            self.assertIsNone(promotion._revise_substitute_pick(cfg, "s", "gwakga", "nosuk", "k"))


class BrokerOfWorkerTests(unittest.TestCase):
    def test_primary_online_means_primary_broker(self):
        cfg = config(7)
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: ["nosuk"],
                            _remote_online_worker_ids=lambda c, rb: []):
            self.assertIsNone(promotion._revise_broker_of_worker(cfg, "nosuk", "s"))

    def test_remote_online_means_remote_broker(self):
        cfg = config(7)
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: [],
                            _remote_online_worker_ids=lambda c, rb: ["yukson"] if rb["name"] == "t2" else []):
            broker = promotion._revise_broker_of_worker(cfg, "yukson", "s")
            self.assertEqual(broker["name"], "t2")


class ResolveTargetTests(unittest.TestCase):
    def test_author_online_keeps_author_and_primary(self):
        cfg = config(7)
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: ["gwakga"],
                            _remote_online_worker_ids=lambda c, rb: [],
                            _broker_id=lambda c, s: "primary-broker"):
            revise_rb, broker_id, _, reviser, substitute = promotion._resolve_revise_target(
                cfg, {}, [], "gwakga", "s1", "nosuk", "tree12", "s")
            self.assertIsNone(revise_rb)
            self.assertIsNone(substitute)
            self.assertEqual(reviser, "gwakga")
            self.assertEqual(broker_id, "primary-broker")

    def test_author_offline_with_b2_routes_to_substitute(self):
        cfg = config(7)
        rows = [aged_row("gwakga", "s1")]
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: ["nosuk", "yukson"],
                            _remote_online_worker_ids=lambda c, rb: [],
                            _broker_id=lambda c, s: "primary-broker",
                            _remote_broker_id=lambda c, rb: rb["name"]):
            revise_rb, broker_id, _, reviser, substitute = promotion._resolve_revise_target(
                cfg, {}, rows, "gwakga", "s1", "nosuk", "tree12", "s")
            self.assertEqual(reviser, "yukson")
            self.assertEqual(substitute, "yukson")
            self.assertIsNone(revise_rb)
            self.assertEqual(broker_id, "primary-broker")

    def test_author_offline_without_b2_raises_offline(self):
        cfg = config(0)
        rows = [aged_row("gwakga", "s1")]
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: [],
                            _remote_online_worker_ids=lambda c, rb: []):
            with self.assertRaises(promotion.PromotionError) as caught:
                promotion._resolve_revise_target(cfg, {}, rows, "gwakga", "s1", "nosuk", "tree12", "s")
            self.assertEqual(caught.exception.code, "revise_author_offline")


class SubstituteGuardRegressionTests(unittest.TestCase):
    def test_persisted_true_marker_blocks_another_substitute_across_heads(self):
        rows = [{"kind": "a2a-revise-dispatch", "node": "node-author", "name": "sample",
                 "tree_sha256": "old-tree", "substitute": True, "reviser_node": "node-beta"}]
        with patch.object(promotion, "_revise_substitute_due", return_value=True), \
             patch.object(promotion, "_revise_substitute_pick", return_value="node-gamma") as pick:
            self.assertIsNone(promotion._revise_substitute_for(
                config(7), rows, "node-author", "sample", "node-reviewer", "new-tree", "synthetic"))
            pick.assert_not_called()

    def test_marker_scope_and_legacy_author_records(self):
        rows = [
            {"kind": "a2a-revise-dispatch", "node": "node-other", "name": "sample", "substitute": True},
            {"kind": "a2a-revise-dispatch", "node": "node-author", "name": "other", "substitute": True},
            {"kind": "a2a-revise-dispatch", "node": "node-author", "name": "sample", "substitute": False},
            {"kind": "a2a-revise-dispatch", "node": "node-author", "name": "sample"},
        ]
        with patch.object(promotion, "_revise_substitute_due", return_value=True), \
             patch.object(promotion, "_revise_substitute_pick", return_value="node-beta"):
            self.assertEqual(promotion._revise_substitute_for(
                config(7), rows, "node-author", "sample", "node-reviewer", "tree", "synthetic"), "node-beta")

    def test_malformed_explicit_markers_withhold_new_substitution(self):
        for marker in ["node-beta", 0, 1, None, [], {}]:
            with self.subTest(marker=marker), \
                 patch.object(promotion, "_revise_substitute_due", return_value=True), \
                 patch.object(promotion, "_revise_substitute_pick", return_value="node-gamma") as pick:
                row = {"kind": "a2a-revise-dispatch", "node": "node-author", "name": "sample", "substitute": marker}
                self.assertIsNone(promotion._revise_substitute_for(
                    config(7), [row], "node-author", "sample", "node-reviewer", "tree", "synthetic"))
                pick.assert_not_called()

    def test_online_untrusted_workers_are_ineligible(self):
        with patch.multiple(promotion,
                            _keyring_worker_ids=lambda *_: [],
                            _broker_online_worker_ids=lambda *_: {"node-untrusted"},
                            _remote_online_worker_ids=lambda *_: set()):
            self.assertIsNone(promotion._revise_substitute_pick(
                config(7), "synthetic", "node-author", "node-reviewer", "lineage"))

    def test_trust_and_independence_intersection(self):
        with patch.multiple(promotion,
                            _keyring_worker_ids=lambda *_: ["node-author", "node-reviewer", "node-beta", "node-offline"],
                            _broker_online_worker_ids=lambda *_: {"node-untrusted", "node-author", "node-reviewer"},
                            _remote_online_worker_ids=lambda *_: {"node-beta", "node-untrusted"}):
            self.assertEqual(promotion._revise_substitute_pick(
                config(7), "synthetic", "node-author", "node-reviewer", "lineage"), "node-beta")

    def test_selection_is_order_independent_and_deduplicated(self):
        choices = []
        for pool in [["node-beta", "node-gamma"], ["node-gamma", "node-beta"],
                     ["node-beta", "node-beta", "node-gamma"]]:
            with patch.multiple(promotion,
                                _keyring_worker_ids=lambda *_: ["node-beta", "node-gamma"],
                                _broker_online_worker_ids=lambda *_: pool,
                                _remote_online_worker_ids=lambda *_: {"node-gamma"}):
                choices.append(promotion._revise_substitute_pick(
                    config(7), "synthetic", "node-author", "node-reviewer", "lineage"))
        self.assertEqual(len(set(choices)), 1)

    def test_selection_errors_raise_typed_failures_without_tracebacks(self):
        for error, expected in [(promotion.PromotionError("dispatch_keyring_invalid"), "dispatch_keyring_invalid"),
                                (ValueError("private response text"), "revise_substitute_unavailable")]:
            captured = io.StringIO()
            with self.subTest(error=type(error).__name__), \
                 patch.object(promotion, "_revise_target_broker", side_effect=promotion.PromotionError("revise_author_offline")), \
                 patch.object(promotion, "_revise_substitute_for", side_effect=error), \
                 contextlib.redirect_stderr(captured):
                with self.assertRaises(promotion.PromotionError) as caught:
                    promotion._resolve_revise_target(config(7), {}, [], "node-author", "sample",
                                                     "node-reviewer", "tree", "synthetic")
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(captured.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
