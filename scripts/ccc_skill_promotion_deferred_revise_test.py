"""#1767: deferred revision rounds — recording, the sweep, and its bounds.

_dispatch_intake_revise had exactly one call site: the moment a review verdict
is consumed. The author-offline check runs at that same moment, when the skip
is zero seconds old, and nothing ever re-entered the dispatch path. An author
node that merely happened to be offline during one collect therefore lost its
revision round permanently — the verdict was consumed, the findings were
discarded with it, and no later cycle could reconstruct the round's inputs.

These tests cover the recovery path: the deferral row that preserves those
inputs, and the sweep that retries it under the same caps as the original
dispatch.
"""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_deferred_test", Path(__file__).with_name("ccc-skill-promotion.py"))
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

HEAD = "f" * 40


def iso(*, days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def config(collect_window: int = 8) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        node="seoseo", collect_window=collect_window, revise_substitute_after_days=0)


def deferral(pr: str = "42", *, days_ago: int = 1, head: str = HEAD, task: str = "task-1") -> dict:
    return {
        "ts": iso(days_ago=days_ago),
        "kind": "a2a-revise-deferred",
        "code": "revise_author_offline",
        "dispatched_task": task,
        "pr": pr,
        "head_sha": head,
        "node": "gwakga",
        "name": "s1",
        "reviewer_node": "nosuk",
        "findings": [{"title": "finding one"}],
    }


def origin(task: str = "task-1") -> dict:
    return {"kind": "a2a-dispatch", "dispatched_task": task, "pr_url": "https://x/42",
            "head_sha": HEAD, "reviewer_node": "nosuk"}


class RecordDeferralTests(unittest.TestCase):
    def _record(self, outcome: dict, rows: list | None = None) -> list[dict]:
        written: list[dict] = []
        with patch.multiple(
            promotion,
            _revise_dispatch_target=lambda r: (
                "gwakga", "s1", "abc123abc123", "42", "claude", HEAD, "https://x/42"),
            _append_ledger=lambda c, record: written.append(record),
        ):
            promotion._record_deferred_revise(
                config(), rows if rows is not None else [], origin(),
                [{"title": "finding one"}], "nosuk", outcome)
        return written

    def test_findings_survive_the_consumed_verdict(self):
        written = self._record({"outcome": "revise-skipped", "code": "revise_author_offline"})
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["findings"], [{"title": "finding one"}])
        self.assertEqual(written[0]["node"], "gwakga")
        self.assertEqual(written[0]["name"], "s1")
        self.assertEqual(written[0]["head_sha"], HEAD)

    def test_deferral_is_recorded_once_per_head(self):
        existing = [deferral()]
        self.assertEqual(self._record(
            {"outcome": "revise-skipped", "code": "revise_author_offline"}, rows=existing), [])

    def test_ledger_failure_never_escapes(self):
        """A consumed verdict must not become an error because of bookkeeping."""
        with patch.multiple(
            promotion,
            _revise_dispatch_target=lambda r: (
                "gwakga", "s1", "abc123abc123", "42", "claude", HEAD, "https://x/42"),
            _append_ledger=lambda c, record: (_ for _ in ()).throw(
                promotion.PromotionError("ledger_unsafe")),
        ):
            rows: list[dict] = []
            promotion._record_deferred_revise(
                config(), rows, origin(), [], "nosuk",
                {"outcome": "revise-skipped", "code": "revise_author_offline"})
            self.assertEqual(rows, [], "a failed write must not be mirrored in memory")

    def test_unparseable_dispatch_row_is_ignored(self):
        written: list[dict] = []
        with patch.multiple(
            promotion,
            _revise_dispatch_target=lambda r: "revise_record_invalid",
            _append_ledger=lambda c, record: written.append(record),
        ):
            promotion._record_deferred_revise(
                config(), [], {}, [], "nosuk",
                {"outcome": "revise-skipped", "code": "revise_author_offline"})
        self.assertEqual(written, [])


class SweepTests(unittest.TestCase):
    def _sweep(self, rows: list[dict], *, window: int = 8, dry_run: bool = False,
               result: dict | None = None):
        calls: list[tuple] = []

        def fake_dispatch(cfg, row, all_rows, findings, reviewer, *, dry_run):
            calls.append((row, findings, reviewer, dry_run))
            return result or {"outcome": "revise-dispatched", "pr": "42", "round": 1}

        with patch.multiple(
            promotion,
            _ledger_rows=lambda c: rows,
            _dispatch_intake_revise=fake_dispatch,
        ):
            swept = promotion._sweep_deferred_revises(config(window), dry_run=dry_run)
        return swept, calls

    def test_due_deferral_is_retried_with_its_preserved_findings(self):
        swept, calls = self._sweep([deferral(), origin()])
        self.assertEqual(len(calls), 1)
        dispatched_row, findings, reviewer, _ = calls[0]
        self.assertEqual(dispatched_row["dispatched_task"], "task-1")
        self.assertEqual(findings, [{"title": "finding one"}])
        self.assertEqual(reviewer, "nosuk")
        self.assertEqual(swept[0]["outcome"], "deferred-revise-retry")

    def test_already_dispatched_head_is_not_retried(self):
        rows = [deferral(), origin(),
                {"kind": "a2a-revise-dispatch", "pr": "42", "head_sha": HEAD}]
        swept, calls = self._sweep(rows)
        self.assertEqual(calls, [])
        self.assertEqual(swept, [])

    def test_retry_stops_after_the_age_bound(self):
        stale = promotion._REVISE_DEFERRED_MAX_AGE_DAYS + 1
        swept, calls = self._sweep([deferral(days_ago=stale), origin()])
        self.assertEqual(calls, [])
        self.assertEqual(swept, [])

    def test_age_bound_is_inclusive_at_the_boundary(self):
        edge = promotion._REVISE_DEFERRED_MAX_AGE_DAYS
        _, calls = self._sweep([deferral(days_ago=edge), origin()])
        self.assertEqual(len(calls), 1)

    def test_dry_run_dispatches_nothing(self):
        swept, calls = self._sweep([deferral(), origin()], dry_run=True)
        self.assertEqual(calls, [])
        self.assertEqual(swept[0]["outcome"], "would-retry-deferred-revise")

    def test_window_bounds_the_cycle_and_overflow_is_reported(self):
        rows = [deferral(str(n), task=f"task-{n}") for n in range(10)]
        rows += [origin(f"task-{n}") for n in range(10)]
        swept, calls = self._sweep(rows, window=3)
        self.assertEqual(len(calls), 3)
        overflow = [s for s in swept if s["outcome"] == "deferred-revise-window-overflow"]
        self.assertEqual(len(overflow), 1)
        self.assertEqual(overflow[0]["deferred"], 10)

    def test_oldest_deferrals_go_first(self):
        # The ledger is append-only, so _ledger_rows yields deferrals in the
        # order they were recorded: oldest first. The window must consume that
        # end, or a busy fleet starves the oldest revision forever — the LIFO
        # failure mode #1394 fixed for the verdict poll.
        rows = [deferral("old", days_ago=9, task="task-old"),
                deferral("new", days_ago=1, task="task-new"),
                origin("task-old"), origin("task-new")]
        swept, calls = self._sweep(rows, window=1)
        self.assertEqual(swept[0]["pr"], "old")
        self.assertEqual(calls[0][0]["dispatched_task"], "task-old")

    def test_missing_origin_row_is_a_typed_skip_not_a_crash(self):
        swept, calls = self._sweep([deferral()])
        self.assertEqual(calls, [])
        self.assertEqual(swept[0]["code"], "origin_row_missing")

    def test_malformed_deferrals_are_ignored(self):
        broken = [
            dict(deferral(), findings="not-a-list"),
            dict(deferral(), pr=""),
            dict(deferral(), head_sha=None),
            dict(deferral(), ts="not-a-timestamp"),
        ]
        swept, calls = self._sweep(broken + [origin()])
        self.assertEqual(calls, [])
        self.assertEqual(swept, [])

    def test_non_dict_findings_entries_are_dropped(self):
        row = dict(deferral(), findings=[{"title": "ok"}, "junk", None])
        _, calls = self._sweep([row, origin()])
        self.assertEqual(calls[0][1], [{"title": "ok"}])

    def test_skip_result_is_reported_not_swallowed(self):
        swept, _ = self._sweep(
            [deferral(), origin()],
            result={"outcome": "revise-skipped", "code": "revise_author_offline"})
        self.assertEqual(swept[0]["result"]["code"], "revise_author_offline")


if __name__ == "__main__":
    unittest.main()
