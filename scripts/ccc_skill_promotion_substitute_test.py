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
    """An author-offline deferral as _record_deferred_revise actually writes it.

    #1767: this fixture used to fabricate a row shape production never
    emitted — kind "a2a-revise-comment" with node/name keys (_comment_once
    writes only ts/kind/pr/head_sha/marker) and a compact "%Y%m%dT%H%M%SZ"
    stamp (_utc_now emits extended ISO). Both mismatches made the gate answer
    "not due" for every real row while this suite stayed green, so the whole
    B2 feature was dead in production and tested as working. The fixture is
    now pinned to its producer by DeferralSchemaTests below."""
    ts = ts or iso(days_ago=14)
    return {"kind": "a2a-revise-deferred", "code": "revise_author_offline",
            "node": node, "name": name, "ts": ts, "skipped_at": ts}


def iso(*, days_ago: int) -> str:
    """A timestamp in the exact format _utc_now writes to the ledger."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


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
        rows = [aged_row("gwakga", "s1", ts=iso(days_ago=1))]
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: [],
                            _remote_online_worker_ids=lambda c, rb: []):
            self.assertFalse(promotion._revise_substitute_due(cfg, rows, "gwakga", "s1"))

    def test_unrelated_rows_never_make_it_due(self):
        cfg = config(7)
        rows = [aged_row("gwakga", "other-skill"),
                {"kind": "a2a-revise-deferred", "code": "revise_author_offline",
                 "ts": iso(days_ago=30)}]
        with patch.multiple(promotion, _keyring_worker_ids=lambda c: ["gwakga", "nosuk", "yukson"],
                            _broker_online_worker_ids=lambda c, s: [],
                            _remote_online_worker_ids=lambda c, rb: []):
            self.assertFalse(promotion._revise_substitute_due(cfg, rows, "gwakga", "s1"))


class DeferralSchemaTests(unittest.TestCase):
    """#1767: the gate and its fixtures must read the row production writes.

    These are the tests whose absence let three independent defects ship as a
    working feature: the gate read a key set no producer emitted, parsed a
    timestamp format no producer emitted, and was never exercised end to end.
    """

    def _recorded(self) -> dict:
        written: list[dict] = []
        row = {"dispatched_task": "task-1"}
        with patch.multiple(
            promotion,
            _revise_dispatch_target=lambda r: (
                "gwakga", "s1", "abc123abc123", "42", "claude", "f" * 40, "https://x/42"),
            _append_ledger=lambda c, record: written.append(record),
            _utc_now=lambda: iso(days_ago=0),
        ):
            promotion._record_deferred_revise(
                config(7), [], row, [{"title": "f"}], "nosuk",
                {"outcome": "revise-skipped", "code": "revise_author_offline"})
        self.assertEqual(len(written), 1)
        return written[0]

    def test_recorded_deferral_satisfies_the_age_gate(self):
        """End to end: a real recorded row, aged, makes the gate fire."""
        aged = iso(days_ago=14)
        record = dict(self._recorded(), ts=aged, skipped_at=aged)
        cfg = config(7)
        self.assertTrue(promotion._revise_substitute_due(cfg, [record], "gwakga", "s1"))

    def test_age_counts_the_skip_not_the_row(self):
        """#1628: a deferral reconstructed today for an old skip is due now.

        The drain that reopens verdicts consumed before #1768 writes rows whose
        `ts` is today but whose `skipped_at` is when the author was actually
        found offline — weeks earlier. Counting `ts` would restart every stalled
        lineage's clock at zero and hide it behind the threshold all over again.
        """
        record = dict(self._recorded(), ts=iso(days_ago=0), skipped_at=iso(days_ago=18))
        self.assertTrue(promotion._revise_substitute_due(config(7), [record], "gwakga", "s1"))

    def test_fresh_skip_recorded_today_is_still_not_due(self):
        """The converse: a genuinely fresh skip must not become due early."""
        record = dict(self._recorded(), ts=iso(days_ago=0), skipped_at=iso(days_ago=0))
        self.assertFalse(promotion._revise_substitute_due(config(7), [record], "gwakga", "s1"))

    def test_rows_without_skipped_at_fall_back_to_ts(self):
        """Deferrals written by #1768 predate the field and must still age."""
        legacy = aged_row("gwakga", "s1")
        legacy.pop("skipped_at")
        self.assertTrue(promotion._revise_substitute_due(config(7), [legacy], "gwakga", "s1"))

    def test_fixture_keys_match_the_producer(self):
        """aged_row may not drift back into inventing a schema."""
        produced = self._recorded()
        fixture = aged_row("gwakga", "s1")
        for key in fixture:
            self.assertIn(key, produced, f"fixture key {key!r} is not written by production")
        self.assertEqual(fixture["kind"], produced["kind"])
        self.assertEqual(fixture["code"], produced["code"])

    def test_comment_rows_can_never_satisfy_the_gate(self):
        """The old source: _comment_once writes no node/name, so it cannot."""
        comment = {"ts": iso(days_ago=0), "kind": "a2a-revise-comment", "pr": "42",
                   "head_sha": "f" * 40, "marker": "revise-verdict:revise_author_offline"}
        self.assertNotIn("node", comment)
        aged = dict(comment, ts=iso(days_ago=99))
        self.assertFalse(promotion._revise_substitute_due(config(7), [aged], "gwakga", "s1"))

    def test_ledger_timestamp_format_is_parsed(self):
        """_utc_now emits extended ISO; the compact form must still read."""
        self.assertIsNotNone(promotion._parse_ledger_ts("2026-08-31T03:12:15Z"))
        self.assertIsNotNone(promotion._parse_ledger_ts("20260831T031215Z"))
        for bad in ["", None, "not-a-time", 17, "2026-13-45T99:99:99Z"]:
            self.assertIsNone(promotion._parse_ledger_ts(bad), bad)

    def test_structural_skips_are_not_deferred(self):
        written: list[dict] = []
        with patch.multiple(
            promotion,
            _revise_dispatch_target=lambda r: (
                "gwakga", "s1", "abc123abc123", "42", "claude", "f" * 40, "https://x/42"),
            _append_ledger=lambda c, record: written.append(record),
        ):
            for code in ["revise_canon_lane", "revise_record_invalid", "revise_round_failed"]:
                promotion._record_deferred_revise(
                    config(7), [], {"dispatched_task": "t"}, [], "nosuk",
                    {"outcome": "revise-skipped", "code": code})
            promotion._record_deferred_revise(
                config(7), [], {"dispatched_task": "t"}, [], "nosuk",
                {"outcome": "revise-dispatched"})
        self.assertEqual(written, [])


class ReviserExclusionTests(unittest.TestCase):
    """#1628 B2: "author ∪ prior reviser disqualified" for review rounds.

    A substitute revise republishes the revised tree as a fresh intake PR whose
    author is still the original node. Reviewer selection disqualified only the
    author, so the node that wrote the revision stayed eligible to review it —
    self-review, which policies/REVIEW.md forbids and which the B2 design named
    as a required exclusion that was never implemented.
    """

    ONLINE = ["node-author", "node-beta", "node-gamma"]

    def _pick(self, rows: list[dict], online: list[str] | None = None) -> str:
        with patch.multiple(
            promotion,
            _ledger_rows=lambda c: rows,
            _keyring_worker_ids=lambda c: self.ONLINE,
            _broker_online_worker_ids=lambda c, s: online or self.ONLINE,
            _remote_online_worker_ids=lambda c, rb: [],
        ):
            cfg = types.SimpleNamespace(remote_brokers=(), broker_url="https://p.example")
            reviewer, _ = promotion._dispatch_target_worker(
                cfg, "node-author", "secret",
                disqualified=promotion._lineage_revisers(cfg, "node-author", "sample"))
            return reviewer

    def _revise_row(self, **over) -> dict:
        row = {"kind": "a2a-revise-dispatch", "node": "node-author", "name": "sample",
               "reviser_node": "node-beta", "substitute": True}
        row.update(over)
        return row

    def test_prior_substitute_reviser_cannot_review_its_own_revision(self):
        for _ in range(20):
            self.assertEqual(self._pick([self._revise_row()]), "node-gamma")

    def test_author_only_revision_does_not_shrink_the_pool(self):
        """A normal (non-substitute) revise is done by the author, already excluded."""
        rows = [self._revise_row(reviser_node="node-author", substitute=False)]
        self.assertIn(self._pick(rows), {"node-beta", "node-gamma"})

    def test_exclusion_is_scoped_to_the_lineage(self):
        rows = [self._revise_row(name="other-skill"), self._revise_row(node="node-other")]
        self.assertIn(self._pick(rows), {"node-beta", "node-gamma"})

    def test_every_prior_reviser_is_excluded_not_just_the_last(self):
        rows = [self._revise_row(reviser_node="node-beta"),
                self._revise_row(reviser_node="node-gamma")]
        with self.assertRaises(promotion.PromotionError) as caught:
            self._pick(rows)
        self.assertEqual(caught.exception.code, "dispatch_no_reviewer_online")

    def test_empty_pool_fails_closed_rather_than_self_reviewing(self):
        rows = [self._revise_row(reviser_node="node-beta")]
        with self.assertRaises(promotion.PromotionError) as caught:
            self._pick(rows, online=["node-author", "node-beta"])
        self.assertEqual(caught.exception.code, "dispatch_no_reviewer_online")

    def test_malformed_reviser_values_are_ignored(self):
        for bad in [None, "", 0, [], {}]:
            with self.subTest(reviser=bad):
                rows = [self._revise_row(reviser_node=bad)]
                self.assertIn(self._pick(rows), {"node-beta", "node-gamma"})

    def test_no_revise_history_keeps_prior_behaviour(self):
        self.assertIn(self._pick([]), {"node-beta", "node-gamma"})


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
