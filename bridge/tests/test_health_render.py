"""--status health rendering, single-sourced in utils/health_render.py (#455).

Pins the render contract that start.sh's ``--status`` shows. Byte-identical to
the former embedded heredoc is verified against goldens in start.sh's test; here
we cover the branches and the ``now``-dependent age formatting deterministically
via injection. Stdlib-only — no Claude SDK required.
"""

import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

BRIDGE_DIR = Path(__file__).resolve().parents[1]
if "telegram_bot" not in sys.modules:
    _pkg = types.ModuleType("telegram_bot")
    _pkg.__path__ = [str(BRIDGE_DIR)]
    sys.modules["telegram_bot"] = _pkg

from telegram_bot.utils.health_render import render_status_lines  # noqa: E402


class HealthRenderTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)
        self.now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

    def _write(self, data) -> Path:
        p = self.dir / "health.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def _fresh(self, **over):
        d = {
            "updated_at": (self.now - timedelta(seconds=5)).isoformat().replace(
                "+00:00", "Z"
            ),
            "service": {"state": "available", "reason": ""},
            "telegram": {"state": "healthy", "last_error": ""},
            "agent": {"state": "healthy", "provider": "claude", "last_error": ""},
            "workload": {
                "active_requests": 0,
                "oldest_request_age_seconds": 0,
                "waiting_for_turn": 0,
                "turn_occupancy": {
                    "state": "idle",
                    "observed_at": (
                        self.now - timedelta(seconds=5)
                    ).isoformat().replace("+00:00", "Z"),
                },
            },
        }
        d.update(over)
        return d

    def _render(self, path, provider="claude", stale=300):
        return render_status_lines(path, "12345", stale, provider, now=self.now)

    def test_missing_file_degraded_with_configured_label(self):
        lines = self._render(self.dir / "nope.json", provider="codex")
        self.assertEqual(lines[0], "🟡 Bot status: degraded")
        self.assertIn("   Process: alive (PID: 12345)", lines)
        self.assertIn("   Service: degraded (health missing)", lines)
        self.assertIn(
            "   Turn occupancy: unknown (health missing)",
            lines,
        )
        self.assertIn(
            "   Dead-session wakeup: unknown (health missing)",
            lines,
        )
        self.assertIn("   Codex: degraded (health missing)", lines)

    def test_unreadable_file_reports_invalid(self):
        p = self.dir / "health.json"
        p.write_text("not json{{{", encoding="utf-8")
        lines = self._render(p)
        self.assertEqual(lines[0], "🟡 Bot status: degraded")
        self.assertTrue(any("invalid health file:" in ln for ln in lines))
        self.assertIn(
            "   Turn occupancy: unknown (health unreadable)",
            lines,
        )
        self.assertIn(
            "   Dead-session wakeup: unknown (health unreadable)",
            lines,
        )
        self.assertIn("   Telegram: degraded (health unreadable)", lines)

    def test_fresh_available_maps_icon_and_suppresses_healthy_reasons(self):
        lines = self._render(self._write(self._fresh()))
        self.assertEqual(lines[0], "🟢 Bot status: available")
        self.assertIn("   Service: available", lines)
        self.assertIn(
            "   Turn occupancy: idle (0 waiting for runtime admission)",
            lines,
        )
        self.assertIn("   Telegram: healthy", lines)  # reason suppressed when healthy
        self.assertIn("   Claude: healthy", lines)

    def test_health_without_dead_session_wakeup_section_is_backward_compatible(self):
        lines = self._render(self._write(self._fresh()))

        self.assertEqual(lines[0], "🟢 Bot status: available")
        self.assertIn(
            "   Dead-session wakeup: unknown (not reported)",
            lines,
        )

    def test_disabled_dead_session_wakeup_renders_without_activity(self):
        data = self._fresh(dead_session_wakeup={"enabled": False})

        lines = self._render(self._write(data))

        self.assertIn("   Dead-session wakeup: disabled", lines)
        wakeup_line = next(line for line in lines if "Dead-session wakeup:" in line)
        self.assertNotIn("scans=", wakeup_line)
        self.assertNotIn("delivered=", wakeup_line)

    def test_enabled_all_zero_wakeup_scan_is_observable(self):
        data = self._fresh(
            dead_session_wakeup={
                "enabled": True,
                "scans": 1,
                "scanned": 0,
                "triggered": 0,
                "delivered": 0,
                "failed": 0,
                "last_scan_at": "2026-07-15T11:59:55Z",
            }
        )

        lines = self._render(self._write(data))

        self.assertIn(
            "   Dead-session wakeup: enabled "
            "(scans=1 scanned=0 triggered=0 delivered=0 failed=0; "
            "last scan 5s ago)",
            lines,
        )

    def test_budget_only_skipped_wakeup_scan_is_observable(self):
        data = self._fresh(
            dead_session_wakeup={
                "enabled": True,
                "scans": 2,
                "scanned": 1,
                "triggered": 0,
                "delivered": 0,
                "failed": 0,
                "skipped_active": 0,
                "skipped_locked": 0,
                "skipped_quarantine": 0,
                "skipped_cooldown": 0,
                "skipped_attempts": 0,
                "skipped_budget": 2,
                "last_scan_at": "2026-07-15T11:59:55Z",
            }
        )

        lines = self._render(self._write(data))

        self.assertIn(
            "   Dead-session wakeup: enabled "
            "(scans=2 scanned=1 triggered=0 delivered=0 failed=0; "
            "skipped active=0 locked=0 quarantine=0 cooldown=0 attempts=0 "
            "budget=2; last scan 5s ago)",
            lines,
        )

    def test_old_wakeup_health_without_skip_counters_still_renders(self):
        data = self._fresh(
            dead_session_wakeup={
                "enabled": True,
                "scans": 1,
                "scanned": 0,
                "triggered": 0,
                "delivered": 0,
                "failed": 0,
                "last_scan_at": "2026-07-15T11:59:55Z",
            }
        )

        lines = self._render(self._write(data))

        self.assertIn(
            "   Dead-session wakeup: enabled "
            "(scans=1 scanned=0 triggered=0 delivered=0 failed=0; "
            "last scan 5s ago)",
            lines,
        )

    def test_stale_wakeup_scan_degrades_independently_of_fresh_health(self):
        data = self._fresh(
            dead_session_wakeup={
                "enabled": True,
                "scans": 4,
                "scanned": 12,
                "triggered": 1,
                "delivered": 1,
                "failed": 0,
                "last_scan_at": "2026-07-15T11:54:59Z",
            }
        )

        lines = self._render(self._write(data), stale=300)

        self.assertEqual(lines[0], "🟢 Bot status: available")
        self.assertIn(
            "   Dead-session wakeup: unknown "
            "(scan stale: last observation 5m ago)",
            lines,
        )
        self.assertFalse(
            any("Dead-session wakeup: enabled" in line for line in lines)
        )

    def test_disabled_wakeup_with_activity_degrades_instead_of_contradicting(self):
        data = self._fresh(
            dead_session_wakeup={
                "enabled": False,
                "scans": 3,
                "scanned": 8,
                "triggered": 3,
                "delivered": 3,
                "failed": 0,
                "last_scan_at": "2026-07-15T11:59:55Z",
            }
        )

        lines = self._render(self._write(data))

        self.assertIn(
            "   Dead-session wakeup: unknown "
            "(inconsistent disabled activity)",
            lines,
        )
        self.assertNotIn("   Dead-session wakeup: disabled", lines)

    def test_enabled_wakeup_without_scan_time_degrades_to_unknown(self):
        data = self._fresh(
            dead_session_wakeup={
                "enabled": True,
                "scans": 0,
                "scanned": 0,
                "triggered": 0,
                "delivered": 0,
                "failed": 0,
                "last_scan_at": None,
            }
        )

        lines = self._render(self._write(data))

        self.assertIn(
            "   Dead-session wakeup: unknown (scan time unavailable)",
            lines,
        )

    def test_invalid_wakeup_sections_degrade_to_unknown(self):
        valid_counters = {
            "scans": 1,
            "scanned": 0,
            "triggered": 0,
            "delivered": 0,
            "failed": 0,
        }
        cases = [
            (
                "disabled timestamp",
                {
                    "enabled": False,
                    "last_scan_at": "2026-07-15T11:59:55Z",
                },
                "inconsistent disabled activity",
            ),
            (
                "invalid enabled state",
                {"enabled": "false"},
                "invalid enabled state",
            ),
            (
                "invalid counter",
                {
                    "enabled": True,
                    **valid_counters,
                    "failed": None,
                    "last_scan_at": "2026-07-15T11:59:55Z",
                },
                "invalid counters",
            ),
            (
                "timestamp with no scans",
                {
                    "enabled": True,
                    **valid_counters,
                    "scans": 0,
                    "last_scan_at": "2026-07-15T11:59:55Z",
                },
                "inconsistent scan count",
            ),
            (
                "partial skip counters",
                {
                    "enabled": True,
                    **valid_counters,
                    "skipped_budget": 1,
                    "last_scan_at": "2026-07-15T11:59:55Z",
                },
                "invalid skip counters",
            ),
        ]

        for label, section, expected in cases:
            with self.subTest(label=label):
                lines = self._render(
                    self._write(self._fresh(dead_session_wakeup=section))
                )

                self.assertIn(
                    f"   Dead-session wakeup: unknown ({expected})",
                    lines,
                )

    def test_occupied_turn_renders_stable_start_and_monotonic_elapsed(self):
        data = self._fresh(
            workload={
                "active_requests": 1,
                "oldest_request_age_seconds": 2880,
                "waiting_for_turn": 1,
                "turn_occupancy": {
                    "state": "occupied",
                    "observed_at": "2026-07-15T11:59:55Z",
                    "oldest_turn_started_at": "2026-07-15T11:12:00Z",
                    "elapsed_seconds": 2880,
                },
            }
        )

        lines = self._render(self._write(data))

        self.assertIn(
            "   Turn occupancy: occupied "
            "(1 active turn; 1 waiting for runtime admission; "
            "oldest active turn started at 2026-07-15T11:12:00Z; elapsed 48m)",
            lines,
        )
        self.assertEqual(len(lines), 7)

    def test_idle_turn_renders_without_zero_elapsed(self):
        lines = self._render(self._write(self._fresh()))

        self.assertIn(
            "   Turn occupancy: idle (0 waiting for runtime admission)",
            lines,
        )
        self.assertFalse(any("Turn occupancy" in line and "0s" in line for line in lines))
        self.assertEqual(lines.count("   Telegram: healthy"), 1)

    def test_fresh_health_without_workload_reports_occupancy_unknown(self):
        data = self._fresh()
        del data["workload"]

        lines = self._render(self._write(data))

        self.assertEqual(lines[0], "🟢 Bot status: available")
        self.assertIn(
            "   Turn occupancy: unknown (not reported)",
            lines,
        )

    def test_workload_without_occupancy_reports_not_reported(self):
        data = self._fresh(
            workload={
                "active_requests": 0,
                "oldest_request_age_seconds": 0,
                "waiting_for_turn": 0,
            }
        )

        lines = self._render(self._write(data))

        self.assertIn(
            "   Turn occupancy: unknown (not reported)",
            lines,
        )

    def test_invalid_occupancy_state_reports_unknown(self):
        data = self._fresh(
            workload={
                "active_requests": 0,
                "oldest_request_age_seconds": 0,
                "waiting_for_turn": 0,
                "turn_occupancy": {
                    "state": "wedged",
                    "observed_at": "2026-07-15T11:59:55Z",
                },
            }
        )

        lines = self._render(self._write(data))

        self.assertIn(
            "   Turn occupancy: unknown (invalid state)",
            lines,
        )

    def test_occupied_turn_without_start_time_reports_unavailable(self):
        data = self._fresh(
            workload={
                "active_requests": 1,
                "oldest_request_age_seconds": 10,
                "waiting_for_turn": 0,
                "turn_occupancy": {
                    "state": "occupied",
                    "observed_at": "2026-07-15T11:59:55Z",
                    "elapsed_seconds": 10,
                },
            }
        )

        lines = self._render(self._write(data))

        self.assertTrue(
            any(
                "Turn occupancy: occupied" in line
                and "start time unavailable" in line
                for line in lines
            )
        )

    def test_occupied_turn_without_elapsed_reports_unavailable(self):
        data = self._fresh(
            workload={
                "active_requests": 1,
                "oldest_request_age_seconds": 10,
                "waiting_for_turn": 0,
                "turn_occupancy": {
                    "state": "occupied",
                    "observed_at": "2026-07-15T11:59:55Z",
                    "oldest_turn_started_at": "2026-07-15T11:59:50Z",
                },
            }
        )

        lines = self._render(self._write(data))

        self.assertTrue(
            any(
                "Turn occupancy: occupied" in line
                and "elapsed unavailable" in line
                for line in lines
            )
        )

    def test_non_finite_elapsed_does_not_abort_status_rendering(self):
        for elapsed in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(elapsed=elapsed):
                data = self._fresh(
                    workload={
                        "active_requests": 1,
                        "oldest_request_age_seconds": 10,
                        "waiting_for_turn": 0,
                        "turn_occupancy": {
                            "state": "occupied",
                            "observed_at": "2026-07-15T11:59:55Z",
                            "oldest_turn_started_at": "2026-07-15T11:59:50Z",
                            "elapsed_seconds": elapsed,
                        },
                    }
                )

                lines = self._render(self._write(data))

                self.assertEqual(len(lines), 7)
                self.assertTrue(
                    any(
                        "Turn occupancy: occupied" in line
                        and "elapsed unavailable" in line
                        for line in lines
                    )
                )
                self.assertIn("   Telegram: healthy", lines)

    def test_stale_occupancy_degrades_independently_of_fresh_health(self):
        data = self._fresh(
            workload={
                "active_requests": 0,
                "oldest_request_age_seconds": 0,
                "waiting_for_turn": 0,
                "turn_occupancy": {
                    "state": "idle",
                    "observed_at": "2026-07-15T11:54:59Z",
                },
            }
        )

        lines = self._render(self._write(data), stale=300)

        self.assertEqual(lines[0], "🟢 Bot status: available")
        self.assertIn(
            "   Turn occupancy: unknown "
            "(workload stale: last observation 5m ago)",
            lines,
        )

    def test_occupancy_without_observation_time_degrades_to_unknown(self):
        data = self._fresh(
            workload={
                "active_requests": 0,
                "oldest_request_age_seconds": 0,
                "waiting_for_turn": 0,
                "turn_occupancy": {"state": "idle"},
            }
        )

        lines = self._render(self._write(data))

        self.assertIn(
            "   Turn occupancy: unknown (observation time unavailable)",
            lines,
        )

    def test_occupied_zero_active_turns_degrades_instead_of_contradicting(self):
        data = self._fresh(
            workload={
                "active_requests": 0,
                "oldest_request_age_seconds": 60,
                "waiting_for_turn": 0,
                "turn_occupancy": {
                    "state": "occupied",
                    "observed_at": "2026-07-15T11:59:55Z",
                    "oldest_turn_started_at": "2026-07-15T11:59:00Z",
                    "elapsed_seconds": 60,
                },
            }
        )

        lines = self._render(self._write(data))

        self.assertIn(
            "   Turn occupancy: unknown (inconsistent active turn count)",
            lines,
        )
        self.assertFalse(any("occupied (0 active turns" in line for line in lines))

    def test_missing_oldest_request_age_is_backward_compatible(self):
        data = self._fresh(
            workload={
                "active_requests": 1,
                "waiting_for_turn": 0,
                "turn_occupancy": {
                    "state": "occupied",
                    "observed_at": "2026-07-15T11:59:55Z",
                    "oldest_turn_started_at": "2026-07-15T11:59:50Z",
                    "elapsed_seconds": 10,
                },
            }
        )

        lines = self._render(self._write(data))

        self.assertTrue(
            any("Turn occupancy: occupied" in line for line in lines)
        )

    def test_degraded_shows_reasons_and_provider_label(self):
        data = self._fresh(
            service={"state": "degraded", "reason": "tg down"},
            telegram={"state": "unavailable", "last_error": "conn refused"},
            agent={"state": "healthy", "provider": "codex", "last_error": ""},
        )
        lines = self._render(self._write(data), provider="codex")
        self.assertEqual(lines[0], "🟡 Bot status: degraded")
        self.assertIn("   Service: degraded (tg down)", lines)
        self.assertIn("   Telegram: unavailable (conn refused)", lines)
        self.assertIn("   Codex: healthy", lines)

    def test_unavailable_icon(self):
        lines = self._render(self._write(self._fresh(service={"state": "unavailable"})))
        self.assertEqual(lines[0], "🔴 Bot status: unavailable")

    def test_stale_without_timestamp(self):
        data = {
            "service": {"state": "available"},
            "telegram": {"state": "healthy"},
            "agent": {"state": "healthy", "provider": "claude"},
        }
        lines = self._render(self._write(data))
        self.assertEqual(lines[0], "🟡 Bot status: degraded")
        self.assertIn("   Service: degraded (health stale)", lines)
        self.assertIn(
            "   Turn occupancy: unknown (health stale)",
            lines,
        )
        self.assertIn(
            "   Dead-session wakeup: unknown (health stale)",
            lines,
        )

    def test_stale_with_timestamp_formats_age(self):
        for delta, expected in [
            (timedelta(seconds=305), "5m"),   # 305s → 5m
            (timedelta(hours=2, minutes=1), "2h"),
            (timedelta(seconds=350), "5m"),
        ]:
            data = self._fresh(
                updated_at=(self.now - delta).isoformat().replace("+00:00", "Z")
            )
            lines = self._render(self._write(data), stale=300)
            self.assertEqual(lines[0], "🟡 Bot status: degraded")
            self.assertIn(
                f"   Service: degraded (health stale: last update {expected} ago)",
                lines,
                f"delta={delta}",
            )
            self.assertIn(
                "   Turn occupancy: unknown "
                f"(health stale: last update {expected} ago)",
                lines,
                f"delta={delta}",
            )

    def test_agent_claude_key_fallback(self):
        # Legacy snapshots may carry `claude` instead of `agent`.
        data = self._fresh()
        data["claude"] = data.pop("agent")
        lines = self._render(self._write(data))
        self.assertIn("   Claude: healthy", lines)

    def test_provider_from_agent_overrides_configured(self):
        # agent.provider drives the label even if the configured provider differs.
        data = self._fresh(
            agent={"state": "healthy", "provider": "codex", "last_error": ""}
        )
        lines = self._render(self._write(data), provider="claude")
        self.assertIn("   Codex: healthy", lines)


if __name__ == "__main__":
    unittest.main()
