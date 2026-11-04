#!/usr/bin/env python3
"""A CI-red intake PR must be recorded once, deprioritized, and closed when stale (#2656).

Field case, 2026-10-26 → 2026-11-04. Two intake PRs whose `skills` check
failed (`secret_possible-token`) sat open for up to nine days. Nothing owned
them: `_sweep_missing_dispatch` retried the never-reviewed head every 15
minutes, arbitration skips never-reviewed heads, and no close sweep reached
them. The ledger gained 1,098 identical `dispatch_ci_not_green` rows, the
oldest-first redispatch window (3 slots) kept going to the red PRs, and no
one was told. These tests pin:

- `_skills_check_state` separates a completed failure from pending/unknown;
- the dispatch wrapper records a CI skip once per (pr, head, ci state), sends
  one notice for the first `failure` row, and still records other codes;
- `_missing_dispatch_prs` puts known-red heads after every other owed PR;
- `_sweep_ci_red_intakes` closes an open PR whose head has been red for
  `ci_red_close_days` (re-reading the check first), and leaves alone a
  younger, pending-only, reviewed, changed-head, or already-closed head;
- `_collect_drain` runs the close sweep before the redispatch sweep.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_ci_red_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40
HEAD_D = "d" * 40
NOW = 1_800_000_000.0
DAY = 86_400
URL = "https://github.com/jinwon-int/fleet-skills/pull/{}"
BRANCH = "skill-intake/nodea/alpha-claude-" + "a" * 12


def iso(offset_sec: float) -> str:
    return datetime.fromtimestamp(NOW + offset_sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_pr(number: str, head: str, age_sec: float, branch: str = BRANCH) -> dict:
    return {"number": number, "url": URL.format(number), "branch": branch, "head": head, "created_at": NOW - age_sec}


def ci_skip(pr: str, head: str, ci: str, offset_sec: float) -> dict:
    return {"ts": iso(offset_sec), "kind": "a2a-dispatch-skipped", "code": "dispatch_ci_not_green",
            "pr": pr, "head_sha": head, "ci": ci, "pr_url": URL.format(pr), "branch": BRANCH}


class SkillsCheckState(unittest.TestCase):
    def state_for(self, stdout: bytes | None) -> str:
        config = types.SimpleNamespace(repo="jinwon-int/fleet-skills")

        def fake_run(argv, **_kw):
            if stdout is None:
                raise promotion.PromotionError("github_output_invalid")
            return types.SimpleNamespace(stdout=stdout)

        with patch.object(promotion, "_run", side_effect=fake_run):
            return promotion._skills_check_state(config, HEAD_A)

    def test_three_states(self) -> None:
        cases = {
            b"completed/success\n": "success",
            b"completed/failure\n": "failure",
            b"completed/cancelled\n": "failure",
            b"completed/timed_out\n": "failure",
            b"in_progress/\n": "pending",
            b"queued/\n": "pending",
            b"null/\n": "pending",       # no `skills` check reported yet
            b"completed/\n": "pending",  # malformed: no conclusion
            b"": "pending",
            None: "pending",             # gh error never ages a PR toward the close
        }
        for stdout, expected in cases.items():
            self.assertEqual(self.state_for(stdout), expected, stdout)


class CiSkipRecording(unittest.TestCase):
    def run_wrapper(self, results: list[dict]) -> tuple[list[dict], list[dict]]:
        notices: list[dict] = []
        with TemporaryDirectory() as temp:
            state = Path(temp) / "skill-promotion"
            state.mkdir(mode=0o700)
            config = types.SimpleNamespace(promotion_state_dir=state, ci_red_close_days=3)
            candidate = types.SimpleNamespace(node="nodea", provider="claude", name="alpha", tree_sha256="a" * 64)
            with patch.object(promotion, "_dispatch_intake_review_attempt", side_effect=results), \
                    patch.object(promotion, "_push_notify", side_effect=lambda c, **kw: notices.append(kw) or True):
                for expected in results:
                    got = promotion._dispatch_intake_review(
                        config, candidate, {"url": URL.format(617), "branch": BRANCH},
                        transport_id="nodea-claude-alpha-" + "a" * 12,
                    )
                    self.assertEqual(got, expected)
            ledger = state / "ledger.jsonl"
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()] \
                if ledger.exists() else []
        return rows, notices

    @staticmethod
    def red(ci: str, head: str = HEAD_A) -> dict:
        return {"outcome": "dispatch-skipped", "code": "dispatch_ci_not_green", "head_sha": head, "ci": ci}

    def test_repeated_red_skip_is_recorded_once_with_pr_head_ci(self) -> None:
        rows, notices = self.run_wrapper([self.red("failure"), self.red("failure"), self.red("failure")])
        self.assertEqual(len(rows), 1)
        self.assertEqual({k: rows[0][k] for k in ("kind", "code", "pr", "head_sha", "ci")},
                         {"kind": "a2a-dispatch-skipped", "code": "dispatch_ci_not_green",
                          "pr": "617", "head_sha": HEAD_A, "ci": "failure"})
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["event"], "SkillIntakeCiRed")
        self.assertIn("#617", notices[0]["text"])
        self.assertIn("3일", notices[0]["text"])
        self.assertEqual(notices[0]["dedup"], "SkillIntakeCiRed:617:" + HEAD_A[:12])

    def test_pending_then_failure_records_both_and_notifies_once(self) -> None:
        rows, notices = self.run_wrapper([self.red("pending"), self.red("pending"), self.red("failure")])
        self.assertEqual([row["ci"] for row in rows], ["pending", "failure"])
        self.assertEqual(len(notices), 1)

    def test_new_head_is_a_new_row(self) -> None:
        rows, _ = self.run_wrapper([self.red("failure", HEAD_A), self.red("failure", HEAD_B)])
        self.assertEqual([row["head_sha"] for row in rows], [HEAD_A, HEAD_B])

    def test_other_codes_still_record_every_skip(self) -> None:
        skip = {"outcome": "dispatch-skipped", "code": "dispatch_broker_unreachable"}
        rows, notices = self.run_wrapper([dict(skip), dict(skip)])
        self.assertEqual([row["code"] for row in rows], ["dispatch_broker_unreachable"] * 2)
        self.assertNotIn("head_sha", rows[0])
        self.assertEqual(notices, [])


class RedHeadsGoLast(unittest.TestCase):
    def test_known_red_head_yields_slots_to_other_owed_prs(self) -> None:
        prs = [open_pr("1", HEAD_A, 9 * DAY), open_pr("2", HEAD_B, 3 * DAY),
               open_pr("3", HEAD_C, 2 * DAY), open_pr("4", HEAD_D, 1 * DAY)]
        rows = [ci_skip("1", HEAD_A, "failure", -8 * DAY)]
        picked = promotion._missing_dispatch_prs(rows, prs, now=NOW)
        self.assertEqual([pr["number"] for pr in picked], ["2", "3", "4"])
        picked = promotion._missing_dispatch_prs(rows, prs, now=NOW, limit=4)
        self.assertEqual([pr["number"] for pr in picked], ["2", "3", "4", "1"])

    def test_pending_only_head_keeps_its_place(self) -> None:
        prs = [open_pr("1", HEAD_A, 9 * DAY), open_pr("2", HEAD_B, 3 * DAY)]
        rows = [ci_skip("1", HEAD_A, "pending", -8 * DAY)]
        picked = promotion._missing_dispatch_prs(rows, prs, now=NOW)
        self.assertEqual([pr["number"] for pr in picked], ["1", "2"])


class SweepCiRedIntakes(unittest.TestCase):
    def run_sweep(self, rows: list[dict], *, prs: list[dict] | None = None, days: int = 3,
                  recheck: str = "failure", dry_run: bool = False, dispatch_enabled: bool = True):
        written: list[dict] = []
        commands: list[list[str]] = []
        rechecked: list[str] = []
        config = types.SimpleNamespace(dispatch_enabled=dispatch_enabled, ci_red_close_days=days,
                                       repo="jinwon-int/fleet-skills")

        def fake_run(argv, **_kw):
            commands.append(list(argv))
            return types.SimpleNamespace(stdout=b"")

        with patch.object(promotion, "_ledger_rows", return_value=list(rows)), \
                patch.object(promotion, "_open_intake_prs",
                             return_value=prs if prs is not None else [open_pr("617", HEAD_A, 9 * DAY)]), \
                patch.object(promotion, "_skills_check_state",
                             side_effect=lambda _c, head: rechecked.append(head) or recheck), \
                patch.object(promotion, "_append_ledger", side_effect=lambda c, r: written.append(r)), \
                patch.object(promotion, "_utc_now", return_value=iso(0)), \
                patch.object(promotion, "_run", side_effect=fake_run):
            out = promotion._sweep_ci_red_intakes(config, dry_run=dry_run, now=NOW)
        return out, written, commands, rechecked

    def test_stale_red_head_is_closed_with_terminal_and_state_rows(self) -> None:
        out, written, commands, rechecked = self.run_sweep([ci_skip("617", HEAD_A, "failure", -4 * DAY)])
        self.assertEqual(out, [{"outcome": "ci-red-closed", "pr": "617"}])
        self.assertEqual(rechecked, [HEAD_A])
        self.assertEqual(commands[0][:5], ["gh", "pr", "close", "617", "--repo"])
        self.assertIn("#2656", commands[0][-1])
        self.assertIn(HEAD_A[:12], commands[0][-1])
        self.assertEqual(written[0]["kind"], "a2a-intake-ci-red-closed")
        self.assertEqual(written[0]["lifecycle"], "archived:ci-not-green")
        self.assertEqual((written[0]["pr"], written[0]["head_sha"], written[0]["node"], written[0]["name"]),
                         ("617", HEAD_A, "nodea", "alpha"))
        self.assertEqual(written[0]["since"], iso(-4 * DAY))
        self.assertEqual(written[1], {"ts": iso(0), "kind": "a2a-intake-state", "pr": "617", "state": "CLOSED"})

    def test_age_counts_from_the_first_failure_row(self) -> None:
        rows = [ci_skip("617", HEAD_A, "pending", -9 * DAY), ci_skip("617", HEAD_A, "failure", -2 * DAY)]
        out, written, commands, _ = self.run_sweep(rows)
        self.assertEqual((out, written, commands), ([], [], []))

    def test_younger_than_threshold_waits(self) -> None:
        out, written, commands, rechecked = self.run_sweep([ci_skip("617", HEAD_A, "failure", -2 * DAY)])
        self.assertEqual((out, written, commands, rechecked), ([], [], [], []))

    def test_pending_only_never_closes(self) -> None:
        out, written, _, _ = self.run_sweep([ci_skip("617", HEAD_A, "pending", -30 * DAY)])
        self.assertEqual((out, written), ([], []))

    def test_pre_2656_rows_without_head_are_ignored(self) -> None:
        legacy = {"ts": iso(-30 * DAY), "kind": "a2a-dispatch-skipped", "code": "dispatch_ci_not_green",
                  "pr_url": URL.format(617), "branch": BRANCH}
        out, written, _, _ = self.run_sweep([legacy])
        self.assertEqual((out, written), ([], []))

    def test_recheck_not_failure_keeps_it_open(self) -> None:
        out, written, commands, _ = self.run_sweep([ci_skip("617", HEAD_A, "failure", -4 * DAY)], recheck="success")
        self.assertEqual(out, [{"pr": "617", "head": HEAD_A, "since": iso(-4 * DAY),
                                "outcome": "ci-red-recheck", "ci": "success"}])
        self.assertEqual((written, commands), ([], []))

    def test_reviewed_head_is_left_alone(self) -> None:
        rows = [ci_skip("617", HEAD_A, "failure", -4 * DAY),
                {"ts": iso(-DAY), "kind": "a2a-dispatch", "head_sha": HEAD_A, "dispatched_task": "t1"}]
        out, written, _, _ = self.run_sweep(rows)
        self.assertEqual((out, written), ([], []))

    def test_owner_reopen_after_close_is_respected(self) -> None:
        rows = [ci_skip("617", HEAD_A, "failure", -9 * DAY),
                {"ts": iso(-DAY), "kind": "a2a-intake-ci-red-closed", "pr": "617", "head_sha": HEAD_A}]
        out, written, _, _ = self.run_sweep(rows)
        self.assertEqual((out, written), ([], []))

    def test_changed_head_is_not_closed_on_the_old_heads_record(self) -> None:
        out, written, _, _ = self.run_sweep([ci_skip("617", HEAD_A, "failure", -9 * DAY)],
                                            prs=[open_pr("617", HEAD_B, 9 * DAY)])
        self.assertEqual((out, written), ([], []))

    def test_dry_run_previews_only(self) -> None:
        out, written, commands, _ = self.run_sweep([ci_skip("617", HEAD_A, "failure", -4 * DAY)], dry_run=True)
        self.assertEqual(out, [{"pr": "617", "head": HEAD_A, "since": iso(-4 * DAY), "outcome": "would-close-ci-red"}])
        self.assertEqual((written, commands), ([], []))

    def test_off_when_days_zero_or_dispatch_disabled(self) -> None:
        rows = [ci_skip("617", HEAD_A, "failure", -30 * DAY)]
        self.assertEqual(self.run_sweep(rows, days=0)[0], [])
        self.assertEqual(self.run_sweep(rows, dispatch_enabled=False)[0], [])

    def test_close_failure_writes_nothing(self) -> None:
        written: list[dict] = []
        config = types.SimpleNamespace(dispatch_enabled=True, ci_red_close_days=3, repo="jinwon-int/fleet-skills")
        with patch.object(promotion, "_ledger_rows", return_value=[ci_skip("617", HEAD_A, "failure", -4 * DAY)]), \
                patch.object(promotion, "_open_intake_prs", return_value=[open_pr("617", HEAD_A, 9 * DAY)]), \
                patch.object(promotion, "_skills_check_state", return_value="failure"), \
                patch.object(promotion, "_append_ledger", side_effect=lambda c, r: written.append(r)), \
                patch.object(promotion, "_run", side_effect=promotion.PromotionError("github_cli_failed")):
            out = promotion._sweep_ci_red_intakes(config, dry_run=False, now=NOW)
        self.assertEqual(out, [{"outcome": "ci-red-close-failed", "pr": "617", "code": "github_cli_failed"}])
        self.assertEqual(written, [])


class DrainOrder(unittest.TestCase):
    def test_close_sweep_runs_before_redispatch_and_is_reported(self) -> None:
        calls: list[str] = []
        names = ["_sweep_ci_red_intakes", "_sweep_missing_dispatch", "_sweep_intake_states",
                 "_sweep_promoted_intakes", "_sweep_superseded_intakes", "_sweep_arbitration",
                 "_sweep_identity_holds", "_sweep_replay_plans", "_sweep_promote_merge"]
        patches = [patch.object(promotion, name, side_effect=lambda *_a, _n=name, **_k: calls.append(_n) or [])
                   for name in names]
        for p in patches:
            p.start()
        try:
            out = promotion._collect_drain(types.SimpleNamespace(auto_promote_enabled=False), dry_run=True)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(calls[:2], ["_sweep_ci_red_intakes", "_sweep_missing_dispatch"])
        self.assertEqual(out["ci_red_closed"], [])


class ConfigDefault(unittest.TestCase):
    def test_default_three_days(self) -> None:
        self.assertEqual(promotion.Config.__dataclass_fields__["ci_red_close_days"].default, 3)


if __name__ == "__main__":
    unittest.main()
