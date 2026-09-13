"""Fair candidate admission within and across collect runs (#1617, #1647).

Publishing is capped at `max_prs` per run and takes the head of the list
`_collect_envelopes` returns. Before the #1617 fix that list was the sources
concatenated in `collect_nodes` order, so the first node with more pending
envelopes than the cap took every slot on every run, forever.

#1647 extended the contract to the local outbox and to runs: before that fix a
full local outbox spent the whole 64-envelope budget before any SSH exporter
was consulted, and with max_prs=1 every run began at the head of the local
queue, so remotes were never reached across runs. Admission is now bounded per
source, the run starts after the source the previous real collect rotated to
(owner-only cursor under the promotion lock), and dry-run neither writes the
cursor nor acks.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import stat
import subprocess
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


class LocalBudgetFairnessTests(unittest.TestCase):
    """#1647: the local outbox is one bounded source among the fleet's.

    Before the fix a full local outbox filled the global 64-envelope budget
    before a single SSH exporter was consulted (`total >= _MAX_CANDIDATES_PER_RUN`
    skipped every remote), so a backlogged publisher starved all remotes.
    """

    def setUp(self):
        self.max_prs = 3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        state = home / "state" / "skill-promotion"
        self.outbox = state / "outbox"
        self.outbox.mkdir(parents=True)
        (state / "sent").mkdir()
        self.config = SimpleNamespace(
            node="publisher",
            max_prs=self.max_prs,
            collect_nodes=("deep", "starved"),
            promotion_state_dir=state,
            home=home,
        )
        for i in range(promotion._MAX_CANDIDATES_PER_RUN):
            value = envelope("publisher", f"local-skill-{i:02d}",
                             f"2026-08-01T00:00:{i:02d}Z")
            path = self.outbox / f"{value['transport_id']}.json"
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        self.remote = {
            "deep": [envelope("deep", f"deep-skill-{i:02d}", "2026-08-02T00:00:00Z")
                     for i in range(8)],
            "starved": [envelope("starved", f"star-skill-{i:02d}", "2026-08-03T00:00:00Z")
                        for i in range(8)],
        }
        self.remote_limits: dict[str, int] = {}

        def fake_remote(node, *, limit):
            self.remote_limits[node] = limit
            return self.remote[node][:limit]

        self.enterContext(patch.object(promotion, "_remote_envelopes",
                                       side_effect=fake_remote))

    def collected(self):
        errors: list[dict[str, str]] = []
        rows = promotion._collect_envelopes(self.config, errors)
        self.assertEqual(errors, [])
        return rows

    def test_full_local_outbox_still_reaches_every_ssh_exporter(self):
        sources = {row[3] for row in self.collected()}
        self.assertEqual(sources, {"local", "deep", "starved"})

    def test_full_local_outbox_publish_window_is_one_per_source(self):
        window = [row[3] for row in self.collected()[: self.max_prs]]
        self.assertEqual(window, ["local", "deep", "starved"])

    def test_total_admission_stays_bounded(self):
        self.assertLessEqual(len(self.collected()), promotion._MAX_CANDIDATES_PER_RUN)

    def test_remote_fetch_limit_stays_within_exporter_cli_choices(self):
        self.collected()
        self.assertEqual(sorted(self.remote_limits), ["deep", "starved"])
        for limit in self.remote_limits.values():
            self.assertIn(limit, range(1, 4), limit)

    def test_single_source_fleet_keeps_the_full_budget(self):
        self.config.collect_nodes = ()
        errors: list[dict[str, str]] = []
        rows = promotion._collect_envelopes(self.config, errors)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), promotion._MAX_CANDIDATES_PER_RUN)
        self.assertEqual({row[3] for row in rows}, {"local"})


class CollectRunRotationTests(unittest.TestCase):
    """#1647: cross-run rotation, cursor state, and outcome bookkeeping.

    Each test drives the real locked-collect body (`_collect_unlocked`) with
    the GitHub, git, SSH, and ledger edges stubbed, so publish/ACK/cursor
    sequencing is exercised end to end without network or fleet access.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        state = home / "state" / "skill-promotion"
        self.outbox = state / "outbox"
        self.sent = state / "sent"
        self.outbox.mkdir(parents=True, mode=0o700)
        self.sent.mkdir(mode=0o700)
        state.chmod(0o700)
        self.cursor_path = state / promotion._COLLECT_CURSOR_FILENAME
        self.config = SimpleNamespace(
            node="publisher",
            max_prs=1,
            collect_nodes=("deep", "starved"),
            promotion_state_dir=state,
            home=home,
            repo="example/example",
            enabled=True,
            publisher_enabled=True,
            dispatch_enabled=False,
            revise_enabled=False,
            autonomy="collect",
        )
        self.remote: dict[str, list[dict]] = {
            "deep": [envelope("deep", f"deep-skill-{i}", "2026-08-02T00:00:00Z")
                     for i in range(2)],
            "starved": [envelope("starved", f"star-skill-{i}", "2026-08-03T00:00:00Z")
                        for i in range(2)],
        }
        self.acked: list[tuple[str, str]] = []
        self.ledger: list[dict] = []
        self.fail_nodes: set[str] = set()
        self.enterContext(patch.object(promotion, "_remote_envelopes", side_effect=self._remote))
        self.enterContext(patch.object(promotion, "_remote_ack", side_effect=self._remote_ack))
        self.enterContext(patch.object(promotion, "_run",
                                       side_effect=lambda argv, **kwargs:
                                       subprocess.CompletedProcess(argv, 0, b"{}", b"")))
        self.enterContext(patch.object(promotion, "_require_private_repo",
                                       lambda config: None))
        self.enterContext(patch.object(promotion, "_publish", side_effect=self._publish))
        self.enterContext(patch.object(promotion, "_ack_local", side_effect=self._ack_local))
        self.enterContext(patch.object(promotion, "_append_ledger",
                                       side_effect=lambda config, record: self.ledger.append(record)))

    # -- stub edges ---------------------------------------------------------
    def _remote(self, node, *, limit):
        return self.remote[node][:limit]

    def _remote_ack(self, node, transport_id):
        self.acked.append((node, transport_id))
        self.remote[node] = [row for row in self.remote[node]
                             if row["transport_id"] != transport_id]

    def _publish(self, config, candidate, *, created_at):
        if candidate.node in self.fail_nodes:
            raise promotion.PromotionError("git_push_rejected")
        return {"outcome": "pr-opened", "branch": "skill-intake/x"}

    def _ack_local(self, config, transport_id):
        self.acked.append(("local", transport_id))
        source = self.outbox / f"{transport_id}.json"
        if source.exists():
            source.replace(self.sent / source.name)
        return True

    # -- helpers ------------------------------------------------------------
    def stage_local(self, name: str, created_at: str) -> str:
        value = envelope("publisher", name, created_at)
        path = self.outbox / f"{value['transport_id']}.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return value["transport_id"]

    def collect(self, *, dry_run: bool = False):
        return promotion._collect_unlocked(self.config, dry_run=dry_run)

    def write_cursor(self, last_source: str) -> None:
        self.cursor_path.write_text(json.dumps(
            {"schema_version": 1, "last_source": last_source,
             "updated_at": "2026-09-01T00:00:00+00:00"}))
        self.cursor_path.chmod(0o600)

    def read_cursor(self) -> str:
        return json.loads(self.cursor_path.read_text())

    # -- rotation -----------------------------------------------------------
    def test_missing_cursor_starts_at_the_canonical_first_source(self):
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        result = self.collect()
        self.assertEqual(result["published"][0]["source"], "local")
        self.assertEqual(self.read_cursor()["last_source"], "local")

    def test_repeated_max_prs_one_collects_rotate_across_sources(self):
        # Exactly one envelope per source: each run publishes a different
        # source until the fleet drains, proving the rotation is cross-run,
        # not just within-run ordering.
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        self.remote = {
            "deep": self.remote["deep"][:1],
            "starved": self.remote["starved"][:1],
        }
        heads = [self.collect()["published"][0]["source"] for _ in range(3)]
        self.assertEqual(heads, ["local", "deep", "starved"])
        cursor = self.read_cursor()
        self.assertEqual(cursor["last_source"], "starved")
        self.assertEqual(cursor["schema_version"], 1)
        self.assertIn("updated_at", cursor)
        self.assertEqual(stat.S_IMODE(self.cursor_path.lstat().st_mode), 0o600)
        # The rotation cycles back to the local source; the fleet is drained,
        # so the run admits nothing, reports clean, and still advances the
        # cursor to keep the cycle stable.
        result = self.collect()
        self.assertEqual(result["published"], [])
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.read_cursor()["last_source"], "local")

    def test_rotation_drains_each_source_fifo(self):
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        self.stage_local("local-skill-b", "2026-08-01T00:01:00Z")
        first = [self.collect()["published"][0]["name"] for _ in range(3)]
        self.assertEqual(first[0], "local-skill-a")
        second = [self.collect()["published"][0]["name"] for _ in range(3)]
        self.assertEqual(second[0], "local-skill-b")

    def test_unknown_cursor_label_falls_back_to_canonical_order(self):
        # A removed or renamed source must not wedge the rotation: the run
        # falls back to the canonical local-first order.
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        self.write_cursor("ghost")
        self.assertEqual(self.collect()["published"][0]["source"], "local")

    def test_source_set_change_keeps_every_valid_source_in_the_cycle(self):
        self.write_cursor("deep")
        self.config.collect_nodes = ("starved",)  # deep removed from the fleet
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        heads = [self.collect()["published"][0]["source"] for _ in range(2)]
        self.assertEqual(heads, ["local", "starved"])

    def test_failed_remote_export_does_not_starve_the_rest(self):
        def explode(node, *, limit):
            if node == "deep":
                raise promotion.PromotionError("remote_export_failed")
            return self.remote[node][:limit]

        self.write_cursor("local")  # next run starts at deep
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        with patch.object(promotion, "_remote_envelopes", side_effect=explode):
            result = self.collect()
        self.assertEqual(result["errors"],
                         [{"source": "deep", "code": "remote_export_failed"}])
        self.assertEqual(result["published"][0]["source"], "starved")

    # -- cursor state contract ----------------------------------------------
    def test_undecodable_cursor_fails_closed(self):
        self.cursor_path.write_text("{not json")
        self.cursor_path.chmod(0o600)
        with self.assertRaises(promotion.PromotionError) as caught:
            self.collect()
        self.assertEqual(caught.exception.code, "collect_cursor_invalid")

    def test_wrong_schema_cursor_fails_closed(self):
        self.write_cursor("deep")
        value = json.loads(self.cursor_path.read_text())
        del value["updated_at"]
        self.cursor_path.write_text(json.dumps(value))
        with self.assertRaises(promotion.PromotionError) as caught:
            self.collect()
        self.assertEqual(caught.exception.code, "collect_cursor_invalid")

    def test_unsafe_cursor_mode_fails_closed(self):
        self.write_cursor("deep")
        self.cursor_path.chmod(0o644)
        with self.assertRaises(promotion.PromotionError) as caught:
            self.collect()
        self.assertEqual(caught.exception.code, "collect_cursor_unsafe")

    def test_dry_run_neither_writes_cursor_nor_acks(self):
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        result = self.collect(dry_run=True)
        self.assertTrue(result["published"])
        self.assertEqual({row["outcome"] for row in result["published"]},
                         {"would-open-private-intake-pr"})
        self.assertEqual(self.acked, [])
        self.assertEqual(self.ledger, [])
        self.assertFalse(self.cursor_path.exists())

    def test_dry_run_advances_nothing(self):
        self.write_cursor("local")
        before = self.cursor_path.read_text()
        self.collect(dry_run=True)
        self.assertEqual(self.cursor_path.read_text(), before)

    def test_lock_contention_reports_locked_and_writes_nothing(self):
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        with promotion._secure_fs.flock_guard(
                self.config.promotion_state_dir / "promotion.lock",
                owner_id=os.geteuid(), exact_mode=0o600):
            result = promotion._collect(self.config, dry_run=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "locked")
        self.assertEqual(result["published"], [])
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(self.acked, [])

    # -- publish / ACK outcome bookkeeping ----------------------------------
    def test_publish_error_is_reported_and_source_not_acked(self):
        # The whole interleave is attempted until max_prs successes, so every
        # source reports its own failure and nothing is acked.
        self.fail_nodes = {"publisher", "deep", "starved"}
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        result = self.collect()
        self.assertFalse(result["ok"])
        self.assertEqual(
            [(row["source"], row["code"]) for row in result["errors"]],
            [("local", "git_push_rejected"),
             ("deep", "git_push_rejected"),
             ("starved", "git_push_rejected")])
        self.assertEqual(result["published"], [])
        self.assertEqual(self.acked, [])
        self.assertEqual(self.ledger, [])
        # The envelope stays pending in the outbox for the next rotation of
        # its source.
        self.assertEqual(len(list(self.outbox.glob("*.json"))), 1)
        self.assertEqual(len(list(self.sent.glob("*.json"))), 0)
        self.assertEqual(self.read_cursor()["last_source"], "local")

    def test_publish_error_does_not_starve_later_sources_in_the_run(self):
        self.fail_nodes = {"deep"}
        self.write_cursor("local")  # next run starts at deep
        result = self.collect()
        self.assertFalse(result["ok"])
        self.assertEqual(result["published"][0]["source"], "starved")
        self.assertEqual(
            [(row["source"], row["code"]) for row in result["errors"]],
            [("deep", "git_push_rejected")])

    def test_local_ack_failure_is_reported_and_not_hidden(self):
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        self.enterContext(patch.object(promotion, "_ack_local", return_value=False))
        result = self.collect()
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"],
                         [{"source": "local", "name": "local-skill-a",
                           "code": "local_ack_failed"}])

    def test_remote_ack_failure_is_reported_and_not_hidden(self):
        def explode(node, transport_id):
            raise promotion.PromotionError("remote_ack_failed")

        self.enterContext(patch.object(promotion, "_remote_ack", side_effect=explode))
        result = self.collect()
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "remote_ack_failed")
        # The envelope was NOT consumed from the remote: it must be retried.
        self.assertEqual(len(self.remote["deep"]), 2)

    def test_successful_collect_acks_and_records_the_ledger(self):
        self.stage_local("local-skill-a", "2026-08-01T00:00:00Z")
        result = self.collect()
        self.assertTrue(result["ok"])
        self.assertEqual(result["published"][0]["outcome"], "pr-opened")
        self.assertEqual(self.acked, [("local", result["published"][0]["transport_id"])])
        self.assertEqual([row["outcome"] for row in self.ledger], ["pr-opened"])

    def test_existing_pr_is_acked_and_recorded(self):
        self.enterContext(patch.object(
            promotion, "_publish",
            side_effect=lambda config, candidate, *, created_at:
            {"outcome": "existing-pr", "branch": "skill-intake/x",
             "url": "https://github.com/example/example/pull/7"}))
        result = self.collect()
        self.assertTrue(result["ok"])
        self.assertEqual([row["outcome"] for row in result["published"]],
                         ["existing-pr", "existing-pr"])
        # Every admitted envelope whose PR already exists is acknowledged.
        self.assertEqual({source for source, _ in self.acked}, {"deep", "starved"})
        self.assertEqual([row["outcome"] for row in self.ledger],
                         ["existing-pr", "existing-pr"])

    def test_wrong_node_envelope_is_a_per_source_error_not_a_crash(self):
        def liar(node, *, limit):
            if node != "deep":
                return self.remote[node][:limit]
            # Self-consistent envelope claiming a different node than the SSH
            # alias it was exported from.
            return [envelope("someoneelse", "impostor-skill", "2026-08-02T00:00:00Z")]

        self.enterContext(patch.object(promotion, "_remote_envelopes", side_effect=liar))
        result = self.collect()
        self.assertFalse(result["ok"])
        self.assertIn({"source": "deep", "code": "remote_node_mismatch"},
                      result["errors"])
        self.assertEqual([row["source"] for row in result["published"] if row["source"] == "deep"],
                         [])

    def test_malformed_remote_envelope_is_a_per_source_error(self):
        def garbage(node, *, limit):
            return [{"schema_version": 1, "not": "an envelope"}]

        self.enterContext(patch.object(promotion, "_remote_envelopes", side_effect=garbage))
        result = self.collect()
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["source"], "deep")
        self.assertNotEqual(result["errors"][0]["code"], "remote_export_failed")


if __name__ == "__main__":
    unittest.main()
