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
from unittest.mock import patch

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
    classify_network_failure,
    count_spool_backlog,
    evaluate_alerts,
    init_retry_loop_alert,
    init_retry_recovered_alert,
    outage_realert_due_stage,
    outage_realert_stage,
    probe_interval,
    write_alert_spool,
)


def _chain(*excs):
    """Raise ``excs[0]`` from ``excs[1]`` from ... and return the outermost."""
    inner = None
    for exc in reversed(excs):
        if inner is not None:
            exc.__cause__ = inner
        inner = exc
    return inner


class OutageRealertScheduleTests(unittest.TestCase):
    """#2086: staged reminders for a persistent outage."""

    def test_due_stage_follows_10min_1h_then_every_6h(self):
        cases = {
            0: 0,
            599.9: 0,
            600: 1,
            3599: 1,
            3600: 2,
            3600 + 6 * 3600 - 1: 2,
            3600 + 6 * 3600: 3,
            3600 + 12 * 3600: 4,
            36033: 3,  # the 10-hour ND-3626 outage
        }
        for elapsed, stage in cases.items():
            with self.subTest(elapsed=elapsed):
                self.assertEqual(outage_realert_due_stage(elapsed), stage)

    def test_invalid_elapsed_is_never_due(self):
        for elapsed in (-5, float("nan"), float("inf"), None, "soon"):
            with self.subTest(elapsed=elapsed):
                self.assertEqual(outage_realert_due_stage(elapsed), 0)

    def test_next_stage_fires_once_per_stage_and_skips_to_the_highest(self):
        self.assertIsNone(outage_realert_stage(300, 0))
        self.assertEqual(outage_realert_stage(600, 0), 1)
        self.assertIsNone(outage_realert_stage(900, 1))
        self.assertEqual(outage_realert_stage(3600, 1), 2)
        # A slow loop that missed 1 h and 7 h announces once, at stage 3.
        self.assertEqual(outage_realert_stage(8 * 3600, 1), 3)
        self.assertIsNone(outage_realert_stage(8 * 3600, 3))

    def test_ten_hour_outage_yields_three_reminders(self):
        stage, fired = 0, []
        for elapsed in range(0, 36_034, 5):
            nxt = outage_realert_stage(elapsed, stage)
            if nxt is not None:
                stage = nxt
                fired.append(elapsed)
        self.assertEqual(fired, [600, 3600, 25200])


class ClassifyNetworkFailureTests(unittest.TestCase):
    """#2086: the alert names the kind of failure."""

    def test_dns_through_ptb_httpx_wrapping(self):
        import socket

        import httpx
        import telegram.error

        gai = socket.gaierror(-3, "Temporary failure in name resolution")
        exc = _chain(
            telegram.error.NetworkError("httpx.ConnectError: boom"),
            httpx.ConnectError("boom"),
            gai,
        )
        self.assertEqual(classify_network_failure(exc), ("dns", "gaierror"))

    def test_dns_from_message_when_the_chain_is_lost(self):
        import telegram.error

        exc = telegram.error.NetworkError(
            "httpx.ConnectError: [Errno -3] Temporary failure in name resolution"
        )
        self.assertEqual(classify_network_failure(exc), ("dns", "NetworkError"))

    def test_dns_via_context_aiohttp_name_and_exception_group(self):
        class ClientConnectorDNSError(OSError):
            pass

        try:
            try:
                raise ClientConnectorDNSError("Cannot connect to host")
            except OSError:
                raise RuntimeError("sync failed")
        except RuntimeError as wrapped:
            exc = wrapped
        self.assertEqual(
            classify_network_failure(exc), ("dns", "ClientConnectorDNSError")
        )
        group = ExceptionGroup("task group", [ValueError("x"), exc])
        self.assertEqual(
            classify_network_failure(group), ("dns", "ClientConnectorDNSError")
        )

    def test_other_kinds(self):
        import ssl

        import httpx
        import telegram.error

        cases = [
            (
                _chain(telegram.error.TimedOut("Pool timeout"), httpx.PoolTimeout("x")),
                ("timeout", "TimedOut"),
            ),
            (httpx.ConnectTimeout("x"), ("timeout", "ConnectTimeout")),
            (TimeoutError(), ("timeout", "TimeoutError")),
            (
                _chain(
                    telegram.error.NetworkError("httpx.ConnectError: x"),
                    ConnectionRefusedError(111, "Connection refused"),
                ),
                ("connection_refused", "ConnectionRefusedError"),
            ),
            (
                _chain(
                    telegram.error.NetworkError("x"),
                    ssl.SSLCertVerificationError("certificate verify failed"),
                ),
                ("tls", "SSLCertVerificationError"),
            ),
            (telegram.error.NetworkError("Bad Gateway"), ("http_error", "NetworkError")),
            (
                _chain(telegram.error.NetworkError("x"), ValueError("y")),
                ("other", "ValueError"),
            ),
        ]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_network_failure(exc), expected)

    def test_dns_wins_over_a_timeout_in_the_same_chain(self):
        import socket

        import telegram.error

        exc = _chain(telegram.error.TimedOut("Timed out"), socket.gaierror(-3, "x"))
        self.assertEqual(classify_network_failure(exc)[0], "dns")

    def test_cyclic_chain_terminates(self):
        a, b = RuntimeError("a"), RuntimeError("b")
        a.__cause__, b.__cause__ = b, a
        self.assertEqual(classify_network_failure(a), ("other", "RuntimeError"))


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
                    foreground_workload_snapshot=lambda now: ticks.append(now) or (0, 0.0),
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


class WorkloadReporterLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_reporter_publishes_runtime_admission_wait_count(self):
        from telegram_bot.core.bot_lifecycle import BotLifecycleMixin

        stop = asyncio.Event()

        def workload_snapshot(now):
            self.assertGreaterEqual(now, 0)
            stop.set()
            return 2, 75.0

        bot = type("Bot", (BotLifecycleMixin,), {})()
        bot._project_chat = SimpleNamespace(
            workload_snapshot=workload_snapshot,
            waiting_for_turn_snapshot=lambda: 1,
        )
        bot.application = None
        bot._WORKLOAD_INTERVAL = 0

        with patch(
            "telegram_bot.core.bot_lifecycle.health_reporter.record_workload"
        ) as record_workload:
            await bot._workload_reporter(stop)

        record_workload.assert_called_once_with(
            2,
            75.0,
            waiting_for_turn=1,
        )


class SessionResourceGuardLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_guard_loop_runs_enforcement_and_stops_cleanly(self):
        from telegram_bot.core.bot_lifecycle import BotLifecycleMixin

        stop = asyncio.Event()
        calls = 0

        async def enforce():
            nonlocal calls
            calls += 1
            stop.set()

        async def immediate_timeout(awaitable, *, timeout):
            del timeout
            awaitable.close()
            raise asyncio.TimeoutError

        bot = type("Bot", (BotLifecycleMixin,), {})()
        bot._config = SimpleNamespace(session_guard_interval_seconds=10.0)
        bot._project_chat = SimpleNamespace(
            enforce_session_resource_limits=enforce
        )

        with patch(
            "telegram_bot.core.bot_lifecycle.asyncio.wait_for",
            new=immediate_timeout,
        ):
            await bot._session_resource_guard(stop)

        self.assertEqual(calls, 1)


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
    def test_init_retry_alerts_carry_numbers_only(self):
        loop = init_retry_loop_alert(7, 61.4)
        recovered = init_retry_recovered_alert(7, -0.5)

        self.assertEqual(loop.code, "telegram_init_retry_loop")
        self.assertEqual(loop.dedup_key(), "health-alert:telegram_init_retry_loop")
        self.assertIn("7 times in a row over 61s", loop.message)
        self.assertEqual(recovered.code, "telegram_init_recovered")
        self.assertIn("after 7 failed attempt(s) over 0s", recovered.message)
        for alert in (loop, recovered):
            self.assertNotIn("/", alert.message)
            self.assertNotIn("token", alert.message.lower())

    def test_init_retry_reminders_carry_stage_cause_and_their_own_dedup_key(self):
        cause = ("dns", "gaierror")
        first = init_retry_loop_alert(3, 30.0, cause=cause)
        reminder = init_retry_loop_alert(7079, 36033.0, cause=cause, stage=3)
        recovered = init_retry_recovered_alert(7079, 36033.0, cause=cause)

        self.assertEqual(first.dedup_key(), "health-alert:telegram_init_retry_loop")
        self.assertEqual(
            reminder.dedup_key(), "health-alert:telegram_init_retry_loop:stage3"
        )
        self.assertIn("Cause: dns (gaierror).", first.message)
        self.assertIn(
            "still failing to initialize: 7079 failed attempts over 36033s (10h00m)",
            reminder.message,
        )
        self.assertIn("(reminder 3)", reminder.message)
        self.assertIn("Cause: dns (gaierror).", reminder.message)
        self.assertIn("7079 failed attempt(s) over 36033s (10h00m)", recovered.message)
        self.assertIn("Last failure cause: dns (gaierror).", recovered.message)
        for alert in (first, reminder, recovered):
            self.assertNotIn("/", alert.message)

    def test_staged_reminders_survive_the_spool_consumer_dedup(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            keys = set()
            for stage in (0, 1, 2):
                # One dir per record: filenames are millisecond-stamped.
                spool = Path(tmp) / f"spool{stage}"
                write_alert_spool(
                    spool, init_retry_loop_alert(3, 60.0, stage=stage), node="n"
                )
                (record,) = spool.glob("*.json")
                keys.add(json.loads(record.read_text(encoding="utf-8"))["dedup"])
            self.assertEqual(len(keys), 3)

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


class FanOutSpoolTests(unittest.TestCase):
    """Receiving side of a push fan-out: consume dir != write dir."""

    def _probe(self, spool, extra=()):
        handler = SimpleNamespace(
            foreground_workload_snapshot=lambda now: (0, 0.0),
            waiting_for_turn_snapshot=lambda: 0,
            session_resource_snapshot=lambda: {},
            _process_timeout_seconds=600.0,
        )
        return HealthProbe(
            project_chat=handler,
            spool_dir=spool,
            orphan_probe=lambda: [],
            health_snapshot=lambda: {},
            extra_spool_dirs=extra,
        )

    def test_backlog_counts_consume_and_write_dirs_once_each(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            primary, mirror = Path(tmp) / "spool", Path(tmp) / "spool" / "fanout-telegram"
            mirror.mkdir(parents=True)
            for i in range(3):
                (primary / f"p{i}.json").write_text("{}", encoding="utf-8")
            (mirror / "m.json").write_text("{}", encoding="utf-8")
            self.assertEqual(self._probe(mirror).collect(1.0).pending_notifications, 1)
            self.assertEqual(
                self._probe(mirror, (primary,)).collect(1.0).pending_notifications,
                4,
                "a stalled primary consumer must still show up in this probe",
            )
            self.assertEqual(
                self._probe(primary, (primary,)).collect(1.0).pending_notifications, 3
            )

    def test_init_retry_alert_goes_to_write_spool_not_consume_dir(self):
        import tempfile

        from telegram_bot.core.bot_lifecycle import BotLifecycleMixin

        with tempfile.TemporaryDirectory() as tmp:
            write_dir, consume_dir = Path(tmp) / "spool", Path(tmp) / "mirror"
            fake = SimpleNamespace(
                _push_notifier=SimpleNamespace(
                    spool_dir=consume_dir, write_spool_dir=write_dir
                )
            )
            BotLifecycleMixin._spool_init_retry_alert(fake, init_retry_loop_alert(3, 30.0))
            self.assertEqual(len(list(write_dir.glob("*.json"))), 1)
            self.assertFalse(consume_dir.exists() and list(consume_dir.glob("*.json")))
            # A stub without write_spool_dir keeps the old behaviour.
            legacy = SimpleNamespace(_push_notifier=SimpleNamespace(spool_dir=consume_dir))
            BotLifecycleMixin._spool_init_retry_alert(legacy, init_retry_loop_alert(3, 30.0))
            self.assertEqual(len(list(consume_dir.glob("*.json"))), 1)


class HealthProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_all_signal_groups_from_synthetic_state(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            spool.mkdir()
            (spool / "pending-1.json").write_text("{}", encoding="utf-8")
            (spool / "pending-2.json").write_text("{}", encoding="utf-8")

            handler = SimpleNamespace(
                foreground_workload_snapshot=lambda now: (2, 750.0),
                waiting_for_turn_snapshot=lambda: 1,
                session_resource_snapshot=lambda: {
                    "resident_sessions": 3,
                    "active_sessions": 2,
                    "tree_rss_mb": 1234.5,
                    "evictions": 4,
                    "runtime_recycles": 2,
                    "codex_attachments": 1,
                },
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
            self.assertEqual(signals.resident_sessions, 3)
            self.assertEqual(signals.active_sessions, 2)
            self.assertEqual(signals.session_tree_rss_mb, 1234.5)
            self.assertEqual(signals.session_guard_evictions, 4)
            self.assertEqual(signals.runtime_recycles, 2)
            self.assertEqual(signals.codex_attachments, 1)

            fired = evaluate_alerts(signals, AlertThresholds())
            self.assertEqual(
                sorted(a.code for a in fired),
                [
                    "notifications_dropped",
                    "orphan_claude_children",
                    "request_outlived_lifetime",
                ],
            )

    async def test_background_task_age_does_not_trip_request_lifetime_alert(self):
        """#1291: run-in-background Bash tasks legitimately outlive the turn
        timeout and used to be folded into oldest_request_age_seconds, so a
        healthy long-running background task re-armed the "lifecycle leaked"
        alert every probe tick for hours while nothing leaked. The probe must
        compare only foreground turn age against _process_timeout_seconds."""
        import tempfile

        handler = SimpleNamespace(
            foreground_workload_snapshot=lambda now: (1, 100.0),
            workload_snapshot=lambda now: (2, 20000.0),  # what pre-#1291 saw
            waiting_for_turn_snapshot=lambda: 0,
            session_resource_snapshot=lambda: {},
            _process_timeout_seconds=600.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            probe = HealthProbe(
                project_chat=handler,
                spool_dir=Path(tmp) / "spool",
                orphan_probe=lambda: [],
                health_snapshot=lambda: {"recovery": {}},
            )
            signals = probe.collect(now=1000.0)

        self.assertEqual(signals.active_requests, 1)
        self.assertEqual(signals.oldest_request_age_seconds, 100.0)
        self.assertEqual(signals.request_lifetime_seconds, 600.0)
        self.assertEqual([a.code for a in evaluate_alerts(signals, AlertThresholds())], [])

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
            resident_sessions=2,
            active_sessions=1,
            session_tree_rss_mb=1024.9,
            session_guard_evictions=3,
            runtime_recycles=1,
            codex_attachments=2,
            orphan_tool_loop_recent=0,
        )
        data = signals.as_dict()
        self.assertEqual(data["oldest_request_age_seconds"], 12)
        self.assertEqual(data["request_lifetime_seconds"], 600)
        self.assertEqual(
            sorted(data),
            [
                "active_requests",
                "active_sessions",
                "codex_attachments",
                "dropped_notifications",
                "oldest_request_age_seconds",
                "orphan_children",
                "orphan_tool_loop_recent",
                "pending_notifications",
                "request_lifetime_seconds",
                "resident_sessions",
                "runtime_recycles",
                "session_guard_evictions",
                "session_tree_rss_mb",
                "waiting_for_turn",
            ],
        )
        self.assertEqual(data["session_tree_rss_mb"], 1024)


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
