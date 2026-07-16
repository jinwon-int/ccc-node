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

    def test_zero_previous_meters_a_fresh_threads_first_turn(self) -> None:
        # The runtime passes a zero-usage previous snapshot for threads it
        # created itself, so a new thread's very first turn is real spend.
        meter = self.make_meter()
        meter.record_codex_thread_usage(
            "thread-new", self.snapshot(0, 0), self.snapshot(500, 100)
        )
        self.assertEqual(meter.used_tokens("codex"), 600)

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


class ReserveTests(UsageMeterTestCase):
    def test_reserve_is_atomic_and_prospective(self) -> None:
        meter = self.make_meter(budgets={"codex": 5000})
        decisions = [
            meter.reserve_autonomous_spend("codex", input_tokens=2058, requests=1)
            for _ in range(4)
        ]
        # Admission requires the whole reservation to fit under the cap:
        # 2 x 2058 fits, a third would cross 5000 and is rejected without
        # charging, so the recorded total can never exceed the cap.
        self.assertEqual([d.allowed for d in decisions], [True, True, False, False])
        self.assertEqual([d.state for d in decisions], ["ok", "ok", "blocked", "blocked"])
        self.assertEqual(meter.used_tokens("codex"), 2 * 2058)
        self.assertLessEqual(meter.used_tokens("codex"), 5000)

    def test_reserve_rejects_a_single_oversized_attempt_outright(self) -> None:
        meter = self.make_meter(budgets={"codex": 5000})
        decision = meter.reserve_autonomous_spend("codex", input_tokens=34816)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.state, "blocked")
        self.assertEqual(meter.used_tokens("codex"), 0)

    def test_reserve_reports_warn_when_admitted_past_the_early_alarm(self) -> None:
        meter = self.make_meter(budgets={"codex": 10000})
        sizes = (3000, 3000, 2500, 1000, 600)
        decisions = [
            meter.reserve_autonomous_spend("codex", input_tokens=size)
            for size in sizes
        ]
        self.assertEqual(
            [d.state for d in decisions], ["ok", "ok", "ok", "warn", "blocked"]
        )
        self.assertEqual(meter.used_tokens("codex"), 9500)

    def test_refund_unwinds_exactly_one_reservation(self) -> None:
        meter = self.make_meter(budgets={"codex": 5000})
        reservation = meter.reserve_autonomous_spend(
            "codex", input_tokens=2058, requests=1
        )
        meter.refund_reservation(reservation)
        self.assertEqual(meter.used_tokens("codex"), 0)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["days"][FIXED_DAY]["codex"][MODE_AUTONOMOUS],
            {"input_tokens": 0, "output_tokens": 0, "requests": 0},
        )

    def test_refunding_a_blocked_reservation_is_a_noop(self) -> None:
        meter = self.make_meter(budgets={"codex": 100})
        blocked = meter.reserve_autonomous_spend("codex", input_tokens=500)
        self.assertFalse(blocked.allowed)
        meter.refund_reservation(blocked)
        self.assertEqual(meter.used_tokens("codex"), 0)

    def test_reservation_is_pinned_to_its_accounting_day(self) -> None:
        # Reviewer probe: a reservation admitted just before midnight must
        # charge (and later refund) its own day, never a newer day's bucket.
        meter = self.make_meter(budgets={"codex": 1000})
        reservation = meter.reserve_autonomous_spend("codex", input_tokens=200)
        self.assertTrue(reservation.allowed)
        self.assertEqual(reservation.day, FIXED_DAY)

        self.now += 86400  # cross into the next KST day
        meter.record("codex", MODE_AUTONOMOUS, input_tokens=900)
        meter.refund_reservation(reservation)

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["days"][FIXED_DAY]["codex"][MODE_AUTONOMOUS]["input_tokens"], 0
        )
        self.assertEqual(
            raw["days"]["2026-07-17"]["codex"][MODE_AUTONOMOUS]["input_tokens"], 900
        )

    def test_overlapping_meter_instances_merge_instead_of_clobbering(self) -> None:
        # Reviewer probe: two meters on one path must persist 300, not 200.
        first = self.make_meter()
        second = self.make_meter()
        first.record("codex", MODE_AUTONOMOUS, input_tokens=100)
        second.record("codex", MODE_AUTONOMOUS, input_tokens=200)

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["days"][FIXED_DAY]["codex"][MODE_AUTONOMOUS]["input_tokens"], 300
        )
        # Prospective admission also sees the other writer's spend.
        gated = self.make_meter(budgets={"codex": 400})
        decision = gated.reserve_autonomous_spend("codex", input_tokens=150)
        self.assertFalse(decision.allowed)

    def test_reserve_without_budget_admits_and_charges(self) -> None:
        meter = self.make_meter()
        decision = meter.reserve_autonomous_spend("codex", input_tokens=100, requests=1)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.state, "ok")
        self.assertEqual(meter.used_tokens("codex"), 100)

    def test_reserve_records_under_the_autonomous_mode(self) -> None:
        meter = self.make_meter()
        meter.reserve_autonomous_spend("codex", input_tokens=64, requests=1)
        import json as _json

        raw = _json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["days"][FIXED_DAY]["codex"][MODE_AUTONOMOUS],
            {"input_tokens": 64, "output_tokens": 0, "requests": 1},
        )

    def test_reserve_validates_provider(self) -> None:
        with self.assertRaises(ValueError):
            self.make_meter().reserve_autonomous_spend("../etc", input_tokens=1)

    def test_tiny_budget_does_not_warn_or_block_at_zero_usage(self) -> None:
        meter = self.make_meter(budgets={"codex": 1})
        self.assertEqual(meter.check_autonomous_spend("codex").state, "ok")
        decision = meter.reserve_autonomous_spend("codex", input_tokens=1)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.state, "ok")
        self.assertEqual(meter.check_autonomous_spend("codex").state, "blocked")


class PersistenceFailureTests(UsageMeterTestCase):
    def test_repeated_save_failures_preserve_inmemory_deltas(self) -> None:
        # Reviewer probe: two 9-token records against an unavailable state
        # path must report 18, not 9 — the failed-save deltas survive the
        # next mutation instead of being reloaded over, and the budget keeps
        # gating on the merged in-memory state while degraded.
        meter = self.make_meter(budgets={"codex": 10})
        self.path.mkdir()  # os.replace onto a directory fails on POSIX
        with self.assertLogs("telegram_bot.core.usage_meter", level="WARNING"):
            meter.record("codex", MODE_INTERACTIVE, input_tokens=9)
        self.assertEqual(meter.used_tokens("codex"), 9)
        with self.assertLogs("telegram_bot.core.usage_meter", level="WARNING"):
            meter.record("codex", MODE_INTERACTIVE, input_tokens=9)
        self.assertEqual(meter.used_tokens("codex"), 18)
        self.assertFalse(
            meter.reserve_autonomous_spend("codex", input_tokens=1).allowed
        )


class TransientSaveRecoveryTests(UsageMeterTestCase):
    def test_recovery_save_merges_other_writers_spend(self) -> None:
        # Reviewer probe: A fails to persist 100, B persists 200, then A's
        # recovered 50-token record must merge to 350 — never 150 — and the
        # gate must see the full prior spend.
        meter_a = self.make_meter()
        meter_b = self.make_meter()
        self.path.mkdir()  # os.replace onto a directory fails on POSIX
        with self.assertLogs("telegram_bot.core.usage_meter", level="WARNING"):
            meter_a.record("codex", MODE_INTERACTIVE, input_tokens=100)
        self.path.rmdir()
        meter_b.record("codex", MODE_INTERACTIVE, input_tokens=200)

        meter_a.record("codex", MODE_INTERACTIVE, input_tokens=50)

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["days"][FIXED_DAY]["codex"][MODE_INTERACTIVE]["input_tokens"], 350
        )
        gated = self.make_meter(budgets={"codex": 250})
        self.assertFalse(
            gated.reserve_autonomous_spend("codex", input_tokens=100).allowed
        )
