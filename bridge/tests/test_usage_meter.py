"""Tests for the durable usage meter and budget caps (#388)."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
from typing import TYPE_CHECKING
import unittest

if TYPE_CHECKING:
    from core.usage import UsageSnapshot
    from core.usage_meter import (
        MODE_AUTONOMOUS,
        MODE_INTERACTIVE,
        UsageMeter,
    )
else:
    from telegram_bot.core.usage import UsageSnapshot
    from telegram_bot.core.usage_meter import (
        MODE_AUTONOMOUS,
        MODE_INTERACTIVE,
        UsageMeter,
    )

# 2026-07-16 12:00 KST (03:00 UTC).
FIXED_NOW = 1784170800.0
FIXED_DAY = "2026-07-16"


class UsageMeterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "usage-meter.json"
        self.now = FIXED_NOW
        self.alerts: list[str] = []

    def make_meter(self, **kwargs: object) -> UsageMeter:
        kwargs.setdefault("clock", lambda: self.now)
        kwargs.setdefault("alert_sink", self.alerts.append)
        return UsageMeter(self.path, **kwargs)  # type: ignore[arg-type]


class RecordingTests(UsageMeterTestCase):
    def test_records_accumulate_per_day_provider_and_mode(self) -> None:
        meter = self.make_meter()
        meter.record("claude", MODE_INTERACTIVE, input_tokens=100, output_tokens=40, requests=1)
        meter.record("claude", MODE_INTERACTIVE, input_tokens=10, requests=1)
        meter.record("codex", MODE_AUTONOMOUS, requests=1)

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        day = raw["days"][FIXED_DAY]
        self.assertEqual(
            day["claude"][MODE_INTERACTIVE],
            {"input_tokens": 110, "output_tokens": 40, "requests": 2},
        )
        self.assertEqual(
            day["codex"][MODE_AUTONOMOUS],
            {"input_tokens": 0, "output_tokens": 0, "requests": 1},
        )
        self.assertEqual(meter.used_tokens("claude"), 150)

    def test_state_survives_reload_and_is_owner_only(self) -> None:
        self.make_meter().record("claude", MODE_INTERACTIVE, input_tokens=7, requests=1)
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)

        reloaded = self.make_meter()
        self.assertEqual(reloaded.used_tokens("claude"), 7)

    def test_day_buckets_use_kst(self) -> None:
        # 2026-07-16 23:30 UTC is already 2026-07-17 in KST.
        self.now = 1784244600.0
        meter = self.make_meter()
        meter.record("claude", MODE_INTERACTIVE, requests=1)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(raw["days"]), ["2026-07-17"])

    def test_corrupt_state_fails_open_to_empty_counters(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("telegram_bot.core.usage_meter", level="WARNING"):
            meter = self.make_meter()
        self.assertEqual(meter.used_tokens("claude"), 0)
        meter.record("claude", MODE_INTERACTIVE, input_tokens=5)
        self.assertEqual(self.make_meter().used_tokens("claude"), 5)

    def test_hostile_state_shapes_are_ignored(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "days": {
                        "not-a-day": {"claude": {MODE_INTERACTIVE: {"requests": 5}}},
                        FIXED_DAY: {
                            "Bad Provider!": {MODE_INTERACTIVE: {"requests": 5}},
                            "claude": {
                                "weird-mode": {"requests": 5},
                                MODE_INTERACTIVE: {
                                    "requests": "9",
                                    "input_tokens": -3,
                                    "output_tokens": 12,
                                },
                            },
                        },
                    },
                    "alerted": {FIXED_DAY: {"claude": ["warn", "bogus"]}},
                }
            ),
            encoding="utf-8",
        )
        meter = self.make_meter()
        self.assertEqual(meter.used_tokens("claude"), 12)

    def test_zero_record_is_a_noop(self) -> None:
        meter = self.make_meter()
        self.assertEqual(meter.record("claude", MODE_INTERACTIVE), ())
        self.assertFalse(self.path.exists())

    def test_invalid_provider_and_mode_are_rejected(self) -> None:
        meter = self.make_meter()
        with self.assertRaises(ValueError):
            meter.record("Bad Provider", MODE_INTERACTIVE, requests=1)
        with self.assertRaises(ValueError):
            meter.record("claude", "scheduled", requests=1)
        with self.assertRaises(ValueError):
            meter.check_autonomous_spend("../etc")

    def test_old_days_are_pruned_past_retention(self) -> None:
        meter = self.make_meter(retention_days=2)
        meter.record("claude", MODE_INTERACTIVE, requests=1)
        self.now += 5 * 86400
        meter.record("claude", MODE_INTERACTIVE, requests=1)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(raw["days"]), ["2026-07-21"])

    def test_persistence_failure_keeps_in_memory_counters(self) -> None:
        meter = self.make_meter()
        self.path.mkdir()  # os.replace onto a directory fails on POSIX
        with self.assertLogs("telegram_bot.core.usage_meter", level="WARNING"):
            meter.record("claude", MODE_INTERACTIVE, input_tokens=9)
        self.assertEqual(meter.used_tokens("claude"), 9)


class CodexDeltaTests(UsageMeterTestCase):
    @staticmethod
    def snapshot(input_tokens: int | None, output_tokens: int | None) -> UsageSnapshot:
        return UsageSnapshot(
            provider="codex", input_tokens=input_tokens, output_tokens=output_tokens
        )

    def test_first_observation_only_sets_a_baseline(self) -> None:
        meter = self.make_meter()
        meter.record_codex_thread_usage("thread-1", None, self.snapshot(5000, 700))
        self.assertEqual(meter.used_tokens("codex"), 0)

    def test_subsequent_observations_record_positive_deltas(self) -> None:
        meter = self.make_meter()
        meter.record_codex_thread_usage("thread-1", None, self.snapshot(5000, 700))
        meter.record_codex_thread_usage(
            "thread-1", self.snapshot(5000, 700), self.snapshot(5600, 900)
        )
        self.assertEqual(meter.used_tokens("codex"), 800)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["days"][FIXED_DAY]["codex"][MODE_INTERACTIVE],
            {"input_tokens": 600, "output_tokens": 200, "requests": 0},
        )

    def test_previous_snapshot_seeds_baseline_after_restart(self) -> None:
        meter = self.make_meter()
        meter.record_codex_thread_usage(
            "thread-1", self.snapshot(4000, 500), self.snapshot(4300, 600)
        )
        self.assertEqual(meter.used_tokens("codex"), 400)

    def test_shrinking_totals_rebaseline_without_negative_records(self) -> None:
        meter = self.make_meter()
        meter.record_codex_thread_usage("thread-1", None, self.snapshot(5000, 700))
        meter.record_codex_thread_usage(
            "thread-1", self.snapshot(5000, 700), self.snapshot(100, 10)
        )
        self.assertEqual(meter.used_tokens("codex"), 0)
        meter.record_codex_thread_usage(
            "thread-1", self.snapshot(100, 10), self.snapshot(150, 20)
        )
        self.assertEqual(meter.used_tokens("codex"), 60)


class BudgetTests(UsageMeterTestCase):
    def make_budgeted(self) -> UsageMeter:
        return self.make_meter(budgets={"codex": 1000}, warn_percent=80)

    def test_warn_and_enforce_alerts_fire_exactly_once_per_day(self) -> None:
        meter = self.make_budgeted()
        self.assertEqual(meter.record("codex", MODE_INTERACTIVE, input_tokens=799), ())
        warn = meter.record("codex", MODE_INTERACTIVE, input_tokens=1)
        self.assertEqual([alert.kind for alert in warn], ["warn"])
        self.assertEqual(meter.record("codex", MODE_INTERACTIVE, input_tokens=50), ())
        enforce = meter.record("codex", MODE_AUTONOMOUS, input_tokens=200)
        self.assertEqual([alert.kind for alert in enforce], ["enforce"])
        self.assertEqual(meter.record("codex", MODE_INTERACTIVE, input_tokens=10), ())
        self.assertEqual(len(self.alerts), 2)

        # A new day re-arms both alerts.
        self.now += 86400
        crossing = meter.record("codex", MODE_INTERACTIVE, input_tokens=2000)
        self.assertEqual([alert.kind for alert in crossing], ["warn", "enforce"])

    def test_alert_state_survives_reload_without_refiring(self) -> None:
        meter = self.make_budgeted()
        meter.record("codex", MODE_INTERACTIVE, input_tokens=1200)
        self.assertEqual(len(self.alerts), 2)
        reloaded = self.make_budgeted()
        self.assertEqual(reloaded.record("codex", MODE_INTERACTIVE, input_tokens=5), ())
        self.assertEqual(len(self.alerts), 2)

    def test_alert_text_is_body_free(self) -> None:
        meter = self.make_budgeted()
        meter.record("codex", MODE_INTERACTIVE, input_tokens=1200)
        for message in self.alerts:
            self.assertRegex(
                message,
                r"^(⚠️ warn|🛑 enforce): codex used \d+ of \d+ daily budget tokens",
            )

    def test_alert_sink_failure_does_not_break_recording(self) -> None:
        def broken(_message: str) -> None:
            raise RuntimeError("sink offline")

        meter = self.make_meter(budgets={"codex": 100}, alert_sink=broken)
        with self.assertLogs("telegram_bot.core.usage_meter", level="ERROR"):
            meter.record("codex", MODE_INTERACTIVE, input_tokens=100)
        self.assertEqual(meter.used_tokens("codex"), 100)

    def test_autonomous_spend_is_blocked_at_the_cap_but_interactive_is_not(self) -> None:
        meter = self.make_budgeted()
        self.assertEqual(meter.check_autonomous_spend("codex").state, "ok")
        meter.record("codex", MODE_INTERACTIVE, input_tokens=850)
        warn_decision = meter.check_autonomous_spend("codex")
        self.assertEqual(warn_decision.state, "warn")
        self.assertTrue(warn_decision.allowed)
        meter.record("codex", MODE_INTERACTIVE, input_tokens=200)

        blocked = meter.check_autonomous_spend("codex")
        self.assertEqual(blocked.state, "blocked")
        self.assertFalse(blocked.allowed)
        self.assertIn("codex used", blocked.reason())

        # Interactive recording keeps flowing after the enforce threshold —
        # only autonomous spend consults the gate.
        meter.record("codex", MODE_INTERACTIVE, input_tokens=25, requests=1)
        self.assertEqual(meter.used_tokens("codex"), 1075)

    def test_no_budget_means_always_allowed(self) -> None:
        meter = self.make_meter()
        decision = meter.check_autonomous_spend("codex")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.state, "ok")
        self.assertIn("disabled", decision.reason())


class ReportTests(UsageMeterTestCase):
    def test_report_is_compact_and_body_free(self) -> None:
        meter = self.make_meter(budgets={"codex": 1000})
        meter.record("claude", MODE_INTERACTIVE, input_tokens=120, output_tokens=30, requests=2)
        meter.record("codex", MODE_AUTONOMOUS, input_tokens=500, requests=1)
        report = meter.render_report(days=7)
        self.assertIn("claude · today 150 tok · 7d interactive 150 tok/2 req", report)
        self.assertIn("autonomous 500 tok/1 req", report)
        self.assertIn("budget 500/1000 tok (50%, ok; enforce blocks autonomous only)", report)
        self.assertNotIn("message", report.lower())

    def test_report_without_usage_or_budgets_says_so(self) -> None:
        report = self.make_meter().render_report(days=7)
        self.assertIn("no recorded usage", report)

    def test_report_window_excludes_older_days(self) -> None:
        meter = self.make_meter()
        meter.record("claude", MODE_INTERACTIVE, input_tokens=100)
        self.now += 10 * 86400
        meter.record("claude", MODE_INTERACTIVE, input_tokens=1)
        report = meter.render_report(days=7)
        self.assertIn("claude · today 1 tok · 7d interactive 1 tok/0 req", report)

    def test_report_rejects_non_positive_windows(self) -> None:
        with self.assertRaises(ValueError):
            self.make_meter().render_report(days=0)


if __name__ == "__main__":
    unittest.main()
