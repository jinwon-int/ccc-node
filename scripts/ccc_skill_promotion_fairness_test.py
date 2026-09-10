"""A backlogged node must not starve the nodes behind it in collect order (#1617).

Publishing is capped at `max_prs` per run and takes the head of the list
`_collect_envelopes` returns. Before the fix that list was the sources
concatenated in `collect_nodes` order, so the first node with more pending
envelopes than the cap took every slot on every run, forever.
"""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_fairness_test", Path(__file__).with_name("ccc-skill-promotion.py"))
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)


def envelope(node: str, name: str, created_at: str) -> dict:
    """A minimal envelope that survives `_candidate_from_envelope`."""
    description = "a fixture skill description long enough to pass validation"
    body = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"# {name}\n"
        "\n"
        "Documented skill body for the fairness fixture.\n"
    )
    content = body.encode()
    files = [{"path": "SKILL.md", "content_b64": base64.b64encode(content).decode(),
              "executable": False}]
    skill_sha = hashlib.sha256(content).hexdigest()
    digest = hashlib.sha256()
    for item in files:
        digest.update(item["path"].encode())
        digest.update(b"\0")
        digest.update(skill_sha.encode())
        digest.update(b"\0")
    tree_sha = digest.hexdigest()
    return {
        "schema_version": 1,
        "transport_id": f"{node}-claude-{name}-{tree_sha[:12]}",
        "created_at": created_at,
        "node": node,
        "provider": "claude",
        "name": name,
        "description": description,
        "skill_sha256": skill_sha,
        "tree_sha256": tree_sha,
        "files": files,
    }


class CollectFairnessTests(unittest.TestCase):
    """`_collect_envelopes` ordering — the part that decides who gets published."""

    def setUp(self):
        self.max_prs = 3
        self.config = SimpleNamespace(
            node="publisher",
            max_prs=self.max_prs,
            collect_nodes=("deep", "starved", "alsostarved"),
            promotion_state_dir=Path("/nonexistent-outbox"),
            home=Path("/nonexistent-home"),
        )
        # `deep` has a backlog far larger than the cap; the others have work too.
        self.remote = {
            "deep": [envelope("deep", f"deep-skill-{i:02d}", "2026-08-01T00:00:00Z")
                     for i in range(12)],
            "starved": [envelope("starved", f"starved-skill-{i:02d}", "2026-08-02T00:00:00Z")
                        for i in range(4)],
            "alsostarved": [envelope("alsostarved", f"also-skill-{i:02d}", "2026-08-03T00:00:00Z")
                            for i in range(4)],
        }
        self.enterContext(patch.object(promotion, "_pending_envelopes", return_value=[]))
        self.enterContext(patch.object(
            promotion, "_remote_envelopes",
            side_effect=lambda node, *, limit: self.remote[node][:limit]))

    def published_nodes(self):
        """The nodes whose envelopes actually reach a PR in one run."""
        errors: list[dict[str, str]] = []
        collected = promotion._collect_envelopes(self.config, errors)
        self.assertEqual(errors, [])
        return [candidate.node for candidate, _, _, _ in collected[:self.max_prs]]

    def test_deep_backlog_does_not_take_every_published_slot(self):
        self.assertEqual(sorted(self.published_nodes()),
                         ["alsostarved", "deep", "starved"])

    def test_every_source_is_represented_before_any_source_repeats(self):
        errors: list[dict[str, str]] = []
        collected = promotion._collect_envelopes(self.config, errors)
        nodes = [candidate.node for candidate, _, _, _ in collected]
        first_round = nodes[:3]
        self.assertEqual(len(set(first_round)), 3, nodes)

    def test_starved_node_is_reached_when_an_earlier_node_fails(self):
        def explode(node, *, limit):
            if node == "deep":
                raise promotion.PromotionError("remote_export_failed")
            return self.remote[node][:limit]

        with patch.object(promotion, "_remote_envelopes", side_effect=explode):
            errors: list[dict[str, str]] = []
            collected = promotion._collect_envelopes(self.config, errors)
        self.assertEqual(errors, [{"source": "deep", "code": "remote_export_failed"}])
        nodes = {candidate.node for candidate, _, _, _ in collected[:self.max_prs]}
        self.assertEqual(nodes, {"starved", "alsostarved"})

    def test_duplicate_transport_ids_are_still_collapsed(self):
        shared = envelope("starved", "shared-skill", "2026-08-02T00:00:00Z")
        self.remote["starved"] = [shared, shared]
        errors: list[dict[str, str]] = []
        collected = promotion._collect_envelopes(self.config, errors)
        ids = [transport_id for _, _, transport_id, _ in collected]
        self.assertEqual(len(ids), len(set(ids)))

    def test_single_source_fleet_order_is_unchanged(self):
        self.config.collect_nodes = ("deep",)
        errors: list[dict[str, str]] = []
        collected = promotion._collect_envelopes(self.config, errors)
        names = [candidate.name for candidate, _, _, _ in collected]
        self.assertEqual(names, [row["name"] for row in self.remote["deep"][:self.max_prs]])


class PendingEnvelopeOrderTests(unittest.TestCase):
    """`_pending_envelopes` must serve a node's own outbox oldest-first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        state = home / "state" / "skill-promotion"
        self.outbox = state / "outbox"
        self.outbox.mkdir(parents=True)
        (state / "sent").mkdir()
        self.config = SimpleNamespace(promotion_state_dir=state, home=home)

    def stage(self, name: str, created_at: str) -> None:
        value = envelope("author", name, created_at)
        path = self.outbox / f"{value['transport_id']}.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def served(self, limit: int) -> list[str]:
        rows = promotion._pending_envelopes(self.config, limit=limit)
        return [str(row["name"]) for row in rows]

    def test_oldest_envelope_is_served_first_regardless_of_name(self):
        # `zulu` is oldest but sorts last alphabetically — the starvation case.
        self.stage("zulu-skill", "2026-08-01T00:00:00Z")
        self.stage("alpha-skill", "2026-09-01T00:00:00Z")
        self.assertEqual(self.served(1), ["zulu-skill"])

    def test_limit_is_honoured_in_chronological_order(self):
        self.stage("charlie-skill", "2026-08-03T00:00:00Z")
        self.stage("bravo-skill", "2026-08-02T00:00:00Z")
        self.stage("alpha-skill", "2026-08-01T00:00:00Z")
        self.assertEqual(self.served(2), ["alpha-skill", "bravo-skill"])

    def test_a_malformed_envelope_deeper_in_the_queue_does_not_break_the_head(self):
        self.stage("alpha-skill", "2026-08-01T00:00:00Z")
        broken = self.outbox / "author-claude-broken-skill-abcdef012345.json"
        broken.write_text(json.dumps({"schema_version": 1, "not": "an envelope"}))
        broken.chmod(0o600)
        self.assertEqual(self.served(1), ["alpha-skill"])


if __name__ == "__main__":
    unittest.main()
