#!/usr/bin/env python3
"""Lineage report (#2641): the verdict-level revise share counts rounds, the
lineage view counts outcomes. These cases pin the regrouping on the ledger row
shapes the curator node actually writes (sampled 2026-11-03; node slugs here are
placeholders — the repo-wide node-name ratchet keeps fleet names out of fixtures):

    a2a-verdict            task_id skills_intake_review-pr<N>-<node>-<stamp>,
                           verdict approve|revise|reject, status consumed|malformed|infra_failed
    a2a-intake-superseded  pr -> superseded_by
    a2a-revise-result      pr -> new_pr_url (status republished), or a terminal
                           invalid / drop-recommended / publish-failed, or failed
    a2a-arbitration        pr, trigger

The headline check is the one that motivated the issue: a ledger where every
lineage ends approved after one revise round has a 50% verdict revise share but
a 100% final approve rate and 2.0 mean rounds.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "ccc-skill-lineage-report.py"

_SPEC = importlib.util.spec_from_file_location("ccc_skill_lineage_report", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mod
_SPEC.loader.exec_module(mod)

NOW = datetime(2026, 11, 2, 13, 45, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=14)


def ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y%m%dT%H%M%SZ")


def verdict(pr: int, node: str, days_ago: float, verdict: str | None, status: str = "consumed") -> dict:
    row = {
        "kind": "a2a-verdict",
        "status": status,
        "task_id": f"skills_intake_review-pr{pr}-{node}-{stamp(days_ago)}",
        "ts": ts(days_ago),
        "head_sha": f"{pr:040x}",
    }
    if verdict is not None:
        row["verdict"] = verdict
        row["findings"] = 3
    else:
        row["task_status"] = "failed"
    return row


def republished(pr: int, new_pr: int, node: str, days_ago: float) -> dict:
    return {
        "kind": "a2a-revise-result",
        "status": "republished",
        "pr": str(pr),
        "node": node,
        "name": f"skill-{pr}",
        "new_pr_url": f"https://github.com/jinwon-int/fleet-skills/pull/{new_pr}",
        "task_id": f"skills_intake_revise-pr{pr}-{node}-{stamp(days_ago)}",
        "ts": ts(days_ago),
    }


def revise_result(pr: int, status: str, days_ago: float, code: str | None = None) -> dict:
    row = {"kind": "a2a-revise-result", "status": status, "pr": str(pr), "ts": ts(days_ago)}
    if code:
        row["code"] = code
    return row


def superseded(pr: int, new_pr: int, days_ago: float) -> dict:
    return {
        "kind": "a2a-intake-superseded",
        "lifecycle": "archived:superseded",
        "pr": str(pr),
        "superseded_by": str(new_pr),
        "ts": ts(days_ago),
    }


def arbitration(pr: int, trigger: str, days_ago: float) -> dict:
    return {"kind": "a2a-arbitration", "pr": str(pr), "trigger": trigger, "ts": ts(days_ago), "node": "x", "name": "y"}


def ledger_rows() -> list[dict]:
    return [
        # A: node-a, revise -> republished -> approve  (approved, 2 rounds, first=revise)
        verdict(10, "node-a", 10.0, "revise"),
        republished(10, 11, "node-a", 9.9),
        superseded(10, 11, 9.9),
        verdict(11, "node-a", 9.5, "approve"),
        # B: node-b, approve first time  (approved, 1 round)
        verdict(20, "node-b", 8.0, "approve"),
        # C: node-c, revise -> invalid(secret)  (dropped, 1 round)
        verdict(30, "node-c", 7.0, "revise"),
        revise_result(30, "invalid", 6.9, "secret_possible-token"),
        # D: node-d, revise -> republished -> revise -> failed (retryable) + arbitration  (in_progress, 2 rounds)
        verdict(40, "node-d", 6.0, "revise"),
        republished(40, 41, "node-d", 5.9),
        verdict(41, "node-d", 5.0, "revise"),
        revise_result(41, "failed", 4.9),
        arbitration(41, "revise_failed", 4.8),
        # E: node-e, reject  (rejected, 1 round)
        verdict(50, "node-e", 4.0, "reject"),
        # malformed-only PR: never a lineage in the cohort
        verdict(60, "node-a", 3.0, None, status="malformed"),
        # F: lineage whose first verdict predates the window; its approve lands inside it
        verdict(70, "node-f", 20.0, "revise"),
        superseded(70, 71, 19.0),
        verdict(71, "node-f", 2.0, "approve"),
        # G: link known ONLY via a2a-intake-superseded (no revise-result row), approve on the successor
        verdict(80, "node-b", 2.5, "revise"),
        superseded(80, 81, 2.4),
        verdict(81, "node-b", 1.0, "approve"),
        # infra_failed row on A's final PR must not count as a round
        verdict(11, "node-a", 9.6, None, status="infra_failed"),
        # noise the parser must ignore
        {"kind": "a2a-dispatch-skipped", "ts": ts(1.0), "reason": "dispatch_ci_not_green"},
        {"kind": "a2a-verdict", "status": "consumed", "verdict": "approve", "task_id": "not-a-review-task", "ts": ts(1.0)},
    ]


def ledger_text(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in rows) + "\n"


class LineageGrouping(unittest.TestCase):
    def setUp(self) -> None:
        self.built = mod.build_lineages(ledger_rows())
        self.summary = mod.summarize(self.built, SINCE, NOW)
        self.by_root = {e["root_pr"]: e for e in self.summary["lineages"]}

    def test_links_from_revise_result_and_superseded_form_one_lineage(self) -> None:
        self.assertEqual(self.by_root[10]["prs"], [10, 11])
        self.assertEqual(self.by_root[80]["prs"], [80, 81], "superseded-only link must join the lineage")
        self.assertNotIn(11, self.by_root)
        self.assertNotIn(81, self.by_root)

    def test_outcomes_follow_latest_consumed_verdict(self) -> None:
        self.assertEqual(self.by_root[10]["outcome"], "approved")
        self.assertEqual(self.by_root[20]["outcome"], "approved")
        self.assertEqual(self.by_root[30]["outcome"], "dropped")
        self.assertEqual(self.by_root[40]["outcome"], "in_progress")
        self.assertEqual(self.by_root[50]["outcome"], "rejected")
        self.assertEqual(self.by_root[80]["outcome"], "approved")

    def test_rounds_count_only_consumed_verdicts(self) -> None:
        self.assertEqual(self.by_root[10]["rounds"], 2)
        self.assertEqual(self.by_root[10]["excluded"], {"infra_failed": 1})
        self.assertEqual(self.by_root[40]["rounds"], 2)
        self.assertEqual(self.by_root[20]["rounds"], 1)

    def test_first_and_final_verdict_and_node_attribution(self) -> None:
        a = self.by_root[10]
        self.assertEqual((a["node"], a["first_verdict"], a["final_verdict"], a["final_pr"]), ("node-a", "revise", "approve", 11))
        self.assertTrue(self.by_root[40]["arbitrated"])
        self.assertEqual(self.by_root[40]["arbitration_triggers"], ["revise_failed"])
        self.assertFalse(self.by_root[10]["arbitrated"])

    def test_cohort_is_by_first_consumed_verdict(self) -> None:
        self.assertNotIn(70, self.by_root, "first verdict 20 days ago is outside a 14-day window")
        self.assertNotIn(60, self.by_root, "malformed-only PR has no consumed verdict")
        self.assertEqual(self.summary["ledger"]["lineages_without_consumed_verdict"], 1)
        self.assertEqual(self.summary["ledger"]["unparsed_verdict_tasks"], 1)
        self.assertEqual(sorted(self.by_root), [10, 20, 30, 40, 50, 80])

    def test_fleet_metrics(self) -> None:
        fleet = self.summary["fleet"]
        self.assertEqual(fleet["lineages"], 6)
        self.assertEqual(fleet["outcomes"], {"approved": 3, "rejected": 1, "dropped": 1, "in_progress": 1})
        self.assertEqual(fleet["decided"], 5)
        # rates are rounded to 4 places, means to 3
        self.assertAlmostEqual(fleet["final_approve_rate_decided"], 3 / 5, places=3)
        self.assertAlmostEqual(fleet["final_approve_rate_all"], 3 / 6, places=3)
        self.assertAlmostEqual(fleet["first_review_approve_rate"], 1 / 6, places=3)
        # rounds: A2 B1 C1 D2 E1 G2 -> 9/6
        self.assertAlmostEqual(fleet["mean_rounds_all"], 1.5, places=2)
        self.assertAlmostEqual(fleet["mean_rounds_decided"], (2 + 1 + 1 + 1 + 2) / 5, places=2)
        self.assertAlmostEqual(fleet["mean_rounds_approved"], (2 + 1 + 2) / 3, places=2)
        self.assertEqual(fleet["rounds_histogram"], {"1": 3, "2": 3})
        self.assertEqual(fleet["arbitrated"], 1)

    def test_by_node(self) -> None:
        by_node = self.summary["by_node"]
        self.assertEqual(sorted(by_node), ["node-a", "node-b", "node-c", "node-d", "node-e"])
        self.assertEqual(by_node["node-b"]["lineages"], 2)
        self.assertEqual(by_node["node-b"]["outcomes"]["approved"], 2)
        self.assertAlmostEqual(by_node["node-b"]["first_review_approve_rate"], 0.5)
        self.assertEqual(by_node["node-c"]["outcomes"]["dropped"], 1)
        self.assertIsNone(by_node["node-d"]["final_approve_rate_decided"], "no decided lineage -> n/a, not 0")

class LegacyWindowCounts(unittest.TestCase):
    """The old #2239 definition, reproduced on the same rows for side-by-side comparison."""

    def test_counts(self) -> None:
        summary = mod.summarize(mod.build_lineages(ledger_rows()), SINCE, NOW)
        legacy = summary["legacy_verdict_window"]
        # inside 14d: pr10 revise, pr11 approve, pr20 approve, pr30 revise, pr40 revise,
        # pr41 revise, pr50 reject, pr71 approve, pr80 revise, pr81 approve -> 10 consumed
        self.assertEqual(legacy["consumed"], 10)
        self.assertEqual((legacy["approve"], legacy["revise"], legacy["reject"]), (4, 5, 1))
        self.assertAlmostEqual(legacy["legacy_revise_ratio"], 0.5)
        self.assertEqual(legacy["excluded"], {"malformed": 1, "infra_failed": 1})


class HeadlineContrast(unittest.TestCase):
    def test_every_lineage_approved_after_one_revise_round(self) -> None:
        rows = []
        for i, node in enumerate(("node-a", "node-b", "node-c", "node-d"), start=1):
            pr, new = 100 + 2 * i, 101 + 2 * i
            rows += [
                verdict(pr, node, 5.0 - i * 0.1, "revise"),
                republished(pr, new, node, 4.9 - i * 0.1),
                verdict(new, node, 4.0 - i * 0.1, "approve"),
            ]
        summary = mod.summarize(mod.build_lineages(rows), SINCE, NOW)
        self.assertAlmostEqual(summary["legacy_verdict_window"]["legacy_revise_ratio"], 0.5, msg="old metric: 50% 'bad'")
        self.assertAlmostEqual(summary["fleet"]["final_approve_rate_decided"], 1.0, msg="new metric: everything landed")
        self.assertAlmostEqual(summary["fleet"]["mean_rounds_all"], 2.0)
        self.assertAlmostEqual(summary["fleet"]["first_review_approve_rate"], 0.0)


class OutcomeEdgeCases(unittest.TestCase):
    def test_terminal_result_before_the_latest_verdict_does_not_drop(self) -> None:
        rows = [
            verdict(10, "node-a", 6.0, "revise"),
            revise_result(10, "invalid", 5.9, "envelope_path_invalid"),
            # operator fixed it by hand, republished, reviewed again -> still open
            republished(10, 11, "node-a", 5.0),
            verdict(11, "node-a", 4.0, "revise"),
        ]
        e = mod.summarize(mod.build_lineages(rows), SINCE, NOW)["lineages"][0]
        self.assertEqual(e["outcome"], "in_progress")

    def test_drop_recommended_and_publish_failed_are_terminal(self) -> None:
        for status in ("drop-recommended", "publish-failed"):
            rows = [verdict(10, "node-a", 6.0, "revise"), revise_result(10, status, 5.9)]
            e = mod.summarize(mod.build_lineages(rows), SINCE, NOW)["lineages"][0]
            self.assertEqual(e["outcome"], "dropped", status)

    def test_approve_then_later_revise_is_judged_by_the_latest(self) -> None:
        # intake #26 shape from the lineage_state tests: approve 08-30, revise 09-03
        rows = [verdict(26, "node-a", 6.0, "approve"), verdict(26, "node-a", 3.0, "revise")]
        e = mod.summarize(mod.build_lineages(rows), SINCE, NOW)["lineages"][0]
        self.assertEqual((e["first_verdict"], e["final_verdict"], e["outcome"], e["rounds"]), ("approve", "revise", "in_progress", 2))

    def test_empty_ledger(self) -> None:
        summary = mod.summarize(mod.build_lineages([]), SINCE, NOW)
        self.assertEqual(summary["fleet"]["lineages"], 0)
        self.assertIsNone(summary["fleet"]["final_approve_rate_decided"])
        self.assertIsNone(summary["fleet"]["mean_rounds_all"])
        self.assertIsNone(summary["legacy_verdict_window"]["legacy_revise_ratio"])

    def test_review_task_id_formats(self) -> None:
        cases = {
            "skills_intake_review-pr729-node-b-20261102T173212Z": (729, "node-b"),
            "skills_intake_review-pr69-node-g-20260830T022314Z04": (69, "node-g"),
            "skills_intake_review-pr34-ops-bot-20260831T011835Z01-t2r2": (34, "ops-bot"),
        }
        for task_id, expected in cases.items():
            match = mod.REVIEW_TASK_RE.match(task_id)
            self.assertIsNotNone(match, task_id)
            self.assertEqual((int(match.group(1)), match.group(2)), expected, task_id)
        for bad in ("skills_intake_revise-pr728-node-b-20261102T171506Z", "not-a-review-task", "skills_intake_review-prX-node-b-20261102T173212Z"):
            self.assertIsNone(mod.REVIEW_TASK_RE.match(bad), bad)

    def test_read_rows_skips_garbage(self) -> None:
        rows, skipped = mod.read_rows(["{bad json", "", "[1,2]", json.dumps({"kind": "x"})])
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped, 2)


class Cli(unittest.TestCase):
    def test_json_output_from_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text(ledger_text(ledger_rows()) + "not json\n", encoding="utf-8")
            out = io.StringIO()
            rc = mod.main([str(path), "--json", "--until", NOW.strftime("%Y-%m-%dT%H:%M:%SZ")], out=out)
            self.assertEqual(rc, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["fleet"]["lineages"], 6)
            self.assertEqual(data["ledger"]["skipped_lines"], 1)
            self.assertEqual(data["ledger"]["path"], str(path))
            self.assertEqual(data["window"]["since"], SINCE.strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertEqual(len(data["lineages"]), 6)
            self.assertEqual(data["lineages"][0]["first_ts"], ts(10.0))

    def test_text_output_and_lineage_listing(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text(ledger_text(ledger_rows()), encoding="utf-8")
            out = io.StringIO()
            rc = mod.main([str(path), "--lineages", "--since", SINCE.strftime("%Y-%m-%dT%H:%M:%SZ"), "--until", NOW.strftime("%Y-%m-%dT%H:%M:%SZ")], out=out)
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("lineages 6  decided 5  approved 3", text)
            self.assertIn("final approve rate  decided 60.0%  all 50.0%   first-review approve 16.7%", text)
            self.assertIn("revise ratio 50.0%", text)
            self.assertIn("10,11", text)
            self.assertIn("node-a", text)

    def test_stdin_and_env_default(self) -> None:
        text = ledger_text(ledger_rows())
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text(text, encoding="utf-8")
            out = io.StringIO()
            with unittest.mock.patch.dict("os.environ", {mod.ENV_LEDGER: str(path)}):
                rc = mod.main(["--json"], now=NOW, out=out)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out.getvalue())["fleet"]["lineages"], 6)
        out = io.StringIO()
        with unittest.mock.patch("sys.stdin", io.StringIO(text)):
            rc = mod.main(["-", "--json"], now=NOW, out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["ledger"]["path"], "<stdin>")

    def test_usage_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.jsonl"
            self.assertEqual(mod.main([str(missing)], now=NOW, out=io.StringIO()), 66)
            path = Path(tmp) / "ledger.jsonl"
            path.write_text("", encoding="utf-8")
            self.assertEqual(mod.main([str(path), "--since", "yesterday"], now=NOW, out=io.StringIO()), 64)
            self.assertEqual(mod.main([str(path), "--until", "2026-13-01T00:00:00Z"], now=NOW, out=io.StringIO()), 64)
            self.assertEqual(mod.main([str(path), "--window-days", "0"], now=NOW, out=io.StringIO()), 64)
            self.assertEqual(mod.main([str(path), "--since", "2026-11-02T00:00:00Z", "--until", "2026-11-01T00:00:00Z"], now=NOW, out=io.StringIO()), 64)


if __name__ == "__main__":
    # validate-harness requires a `PASS=<n> FAIL=<n>` summary line.
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    failed = len(result.failures) + len(result.errors)
    print(f"PASS={result.testsRun - failed} FAIL={failed}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
