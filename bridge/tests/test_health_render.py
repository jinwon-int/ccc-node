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
        self.assertIn("   Codex: degraded (health missing)", lines)

    def test_unreadable_file_reports_invalid(self):
        p = self.dir / "health.json"
        p.write_text("not json{{{", encoding="utf-8")
        lines = self._render(p)
        self.assertEqual(lines[0], "🟡 Bot status: degraded")
        self.assertTrue(any("invalid health file:" in ln for ln in lines))
        self.assertIn("   Telegram: degraded (health unreadable)", lines)

    def test_fresh_available_maps_icon_and_suppresses_healthy_reasons(self):
        lines = self._render(self._write(self._fresh()))
        self.assertEqual(lines[0], "🟢 Bot status: available")
        self.assertIn("   Service: available", lines)
        self.assertIn("   Telegram: healthy", lines)  # reason suppressed when healthy
        self.assertIn("   Claude: healthy", lines)

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
