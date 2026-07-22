"""Detection-only runtime health probe and threshold alerts (issue #389).

All alert paths are exercised with synthetic states; nothing here contacts
Telegram or any provider — real delivery stays behind the push notifier's
``CCC_PUSH_ENABLED`` opt-in.
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_bot.utils.health_alerts import (
    DEFAULT_PROBE_INTERVAL_SECONDS,
    MAX_PROBE_INTERVAL_SECONDS,
    MIN_PROBE_INTERVAL_SECONDS,
    Alert,
    AlertGate,
    AlertThresholds,
    HealthProbe,
    HealthSignals,
    count_spool_backlog,
    evaluate_alerts,
    probe_interval,
    write_alert_spool,
)


class EvaluateAlertsTests(unittest.TestCase):
    def test_healthy_signals_fire_nothing(self):
        signals = HealthSignals(
            active_requests=1,
            oldest_request_age_seconds=100.0,
            request_lifetime_seconds=600.0,
            pending_notifications=2,
        )
        self.assertEqual(evaluate_alerts(signals, AlertThresholds()), [])

    def test_each_signal_crossing_fires_its_alert(self):
        cases = {
            "request_outlived_lifetime": HealthSignals(
                oldest_request_age_seconds=601.0, request_lifetime_seconds=600.0
            ),
            "notification_backlog": HealthSignals(pending_notifications=10),
            "notifications_dropped": HealthSignals(dropped_notifications=1),
            "orphan_claude_children": HealthSignals(orphan_children=1),
        }
        for code, signals in cases.items():
            with self.subTest(code=code):
                fired = evaluate_alerts(signals, AlertThresholds())
                self.assertEqual([a.code for a in fired], [code])

    def test_heartbeat_age_threshold_tracks_request_lifetime(self):
        """#307 alignment: the alert boundary is a multiple of the lifetime."""
        thresholds = AlertThresholds(heartbeat_age_factor=2.0)
        below = HealthSignals(
            oldest_request_age_seconds=1199.0, request_lifetime_seconds=600.0
        )
        at = HealthSignals(
            oldest_request_age_seconds=1200.0, request_lifetime_seconds=600.0
        )
        self.assertEqual(evaluate_alerts(below, thresholds), [])
        self.assertEqual(
            [a.code for a in evaluate_alerts(at, thresholds)],
            ["request_outlived_lifetime"],
        )
        # Factor 0 disables the check; unknown lifetime never alerts.
        self.assertEqual(
            evaluate_alerts(at, AlertThresholds(heartbeat_age_factor=0.0)), []
        )
        self.assertEqual(
            evaluate_alerts(
                HealthSignals(oldest_request_age_seconds=9999.0), AlertThresholds()
            ),
            [],
        )

    def test_alert_messages_are_redaction_safe(self):
        signals = HealthSignals(
            oldest_request_age_seconds=1000.0,
            request_lifetime_seconds=600.0,
            pending_notifications=25,
            dropped_notifications=4,
            orphan_children=2,
        )
        fired = evaluate_alerts(signals, AlertThresholds())
        self.assertEqual(len(fired), 4)
        for alert in fired:
            # Constant templates + counts only: no filesystem paths, no secrets.
            self.assertNotRegex(alert.message, r"[/\\]")
            self.assertNotIn("token", alert.message.lower())
            self.assertNotIn("secret", alert.message.lower())


class ProbeIntervalTests(unittest.TestCase):
    def test_non_positive_and_invalid_intervals_never_reach_the_loop(self):
        """#430 review: a negative interval passed straight to asyncio.wait_for
        times out instantly and spins the probe loop hot."""
        for bad in (-1, 0, 0.0, -0.5, None, "abc"):
            with self.subTest(value=bad):
                self.assertEqual(probe_interval(bad), DEFAULT_PROBE_INTERVAL_SECONDS)

    def test_non_finite_intervals_never_reach_the_loop(self):
        """#430 review round 2: NaN passes every comparison guard (all NaN
        comparisons are False) and min/max propagate it, so
        wait_for(timeout=NaN) times out immediately — Pydantic accepts
        CCC_HEALTH_ALERTS_INTERVAL_SECONDS=nan for float fields."""
        for bad in ("nan", float("nan"), "inf", float("inf"), "-inf", float("-inf")):
            with self.subTest(value=bad):
                self.assertEqual(probe_interval(bad), DEFAULT_PROBE_INTERVAL_SECONDS)

    def test_valid_intervals_are_clamped_to_sane_bounds(self):
        self.assertEqual(probe_interval(60), 60.0)
        self.assertEqual(probe_interval(1), MIN_PROBE_INTERVAL_SECONDS)
        self.assertEqual(probe_interval(999999), MAX_PROBE_INTERVAL_SECONDS)


class ProbeLoopHotSpinRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_negative_configured_interval_does_not_hot_loop(self):
        """Drive the real lifecycle probe task with interval=-1: with the clamp
        the first tick is minutes away, so a short observation window must see
        zero probe executions (the unclamped loop ran ~100k ticks/second)."""
        from telegram_bot.core.bot_lifecycle import BotLifecycleMixin

        ticks = []

        class Bot(BotLifecycleMixin):
            def __init__(self, tmp):
                self._config = SimpleNamespace(
                    health_alerts_enabled=True,
                    health_alerts_interval_seconds=-1,
                    health_alerts_cooldown_seconds=1800.0,
                    alert_heartbeat_age_factor=1.0,
                    alert_max_pending_notifications=10,
                    alert_max_orphan_children=1,
                    push_enabled=False,
                )
                self._project_chat = SimpleNamespace(
                    workload_snapshot=lambda now: ticks.append(now) or (0, 0.0),
                    _process_timeout_seconds=600.0,
                )
                self._push_notifier = SimpleNamespace(spool_dir=Path(tmp) / "spool")

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bot = Bot(tmp)
            stop = asyncio.Event()
            task = asyncio.create_task(bot._health_alerts_probe(stop))
            await asyncio.sleep(0.25)
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)

        self.assertEqual(
            ticks, [], "clamped interval must not allow immediate hot ticks"
        )


class AlertGateTests(unittest.TestCase):
    def test_persistent_condition_alerts_once_per_cooldown(self):
        gate = AlertGate(cooldown_seconds=100.0)
        alert = Alert(code="notification_backlog", message="m")

        self.assertEqual(gate.admit([alert], now=0.0), [alert])
        self.assertEqual(gate.admit([alert], now=50.0), [])
        self.assertEqual(gate.admit([alert], now=100.0), [alert])

    def test_cleared_condition_rearms_immediately(self):
        gate = AlertGate(cooldown_seconds=1000.0)
        alert = Alert(code="orphan_claude_children", message="m")

        self.assertEqual(gate.admit([alert], now=0.0), [alert])
        self.assertEqual(gate.admit([], now=1.0), [])  # condition cleared
        self.assertEqual(gate.admit([alert], now=2.0), [alert])


class SpoolTests(unittest.TestCase):
    def test_alert_spools_as_push_notifier_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            alert = Alert(code="notification_backlog", message="12 queued")

            self.assertTrue(write_alert_spool(spool, alert, node="test-node"))

            files = list(spool.glob("*.json"))
            self.assertEqual(len(files), 1)
            record = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(record["event"], "health-alert")
            self.assertEqual(record["node"], "test-node")
            self.assertEqual(record["text"], "12 queued")
            self.assertEqual(record["dedup"], "health-alert:notification_backlog")
            self.assertEqual(count_spool_backlog(spool), 1)


class HealthProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_all_signal_groups_from_synthetic_state(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            spool.mkdir()
            (spool / "pending-1.json").write_text("{}", encoding="utf-8")
            (spool / "pending-2.json").write_text("{}", encoding="utf-8")

            handler = SimpleNamespace(
                workload_snapshot=lambda now: (2, 750.0),
                waiting_for_turn_snapshot=lambda: 1,
                _process_timeout_seconds=600.0,
            )
            probe = HealthProbe(
                project_chat=handler,
                spool_dir=spool,
                orphan_probe=lambda: [111, 222],
                health_snapshot=lambda: {"recovery": {"quarantined_transcripts": 3}},
            )

            signals = probe.collect(now=1000.0)

            self.assertEqual(signals.active_requests, 2)
            self.assertEqual(signals.waiting_for_turn, 1)
            self.assertEqual(signals.oldest_request_age_seconds, 750.0)
            self.assertEqual(signals.request_lifetime_seconds, 600.0)
            self.assertEqual(signals.pending_notifications, 2)
            self.assertEqual(signals.dropped_notifications, 3)
            self.assertEqual(signals.orphan_children, 2)

            fired = evaluate_alerts(signals, AlertThresholds())
            self.assertEqual(
                sorted(a.code for a in fired),
                [
                    "notifications_dropped",
                    "orphan_claude_children",
                    "request_outlived_lifetime",
                ],
            )

    async def test_probe_is_fail_open_on_broken_collaborators(self):
        def broken_snapshot():
            raise RuntimeError("no health file")

        def broken_orphans():
            raise RuntimeError("no /proc")

        handler = SimpleNamespace()
        probe = HealthProbe(
            project_chat=handler,
            spool_dir=Path("/nonexistent/spool"),
            orphan_probe=broken_orphans,
            health_snapshot=broken_snapshot,
        )

        signals = probe.collect(now=0.0)

        self.assertEqual(signals, HealthSignals())
        self.assertEqual(evaluate_alerts(signals, AlertThresholds()), [])

    async def test_signals_export_shape_for_health_json(self):
        signals = HealthSignals(
            active_requests=1,
            waiting_for_turn=1,
            oldest_request_age_seconds=12.7,
            request_lifetime_seconds=600.0,
            pending_notifications=0,
            dropped_notifications=0,
            orphan_children=0,
        )
        data = signals.as_dict()
        self.assertEqual(data["oldest_request_age_seconds"], 12)
        self.assertEqual(data["request_lifetime_seconds"], 600)
        self.assertEqual(
            sorted(data),
            [
                "active_requests",
                "dropped_notifications",
                "oldest_request_age_seconds",
                "orphan_children",
                "pending_notifications",
                "request_lifetime_seconds",
                "waiting_for_turn",
            ],
        )


class HealthReporterSignalsTests(unittest.TestCase):
    def test_reporter_publishes_signals_section(self):
        import importlib
        import tempfile

        sys.modules.pop("telegram_bot.utils.health", None)
        health_module = importlib.import_module("telegram_bot.utils.health")
        with tempfile.TemporaryDirectory() as tmp:
            reporter = health_module.RuntimeHealthReporter(Path(tmp) / ".telegram_bot")
            signals = HealthSignals(pending_notifications=1, orphan_children=2).as_dict()

            reporter.record_health_signals(signals, alerts_fired=2)
            reporter.record_health_signals(
                HealthSignals(pending_notifications=0, orphan_children=0).as_dict(),
                alerts_fired=0,
            )

            snapshot = reporter.snapshot()["signals"]
            self.assertEqual(snapshot["pending_notifications"], 0)
            self.assertEqual(snapshot["orphan_children"], 0)
            self.assertEqual(snapshot["alerts_fired"], 2)  # cumulative
            on_disk = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertIn("signals", on_disk)


if __name__ == "__main__":
    unittest.main()
