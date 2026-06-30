import unittest
import sys
import types
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
telegram_bot_pkg = types.ModuleType("telegram_bot")
telegram_bot_pkg.__path__ = [str(BRIDGE_DIR)]
sys.modules.setdefault("telegram_bot", telegram_bot_pkg)

from telegram_bot.core.heartbeat import (
    compose_heartbeat_text,
    format_duration,
    has_recent_visible_progress,
    should_update_heartbeat,
    tool_label,
)


class HeartbeatFormatTests(unittest.TestCase):
    def test_format_duration_is_compact(self):
        self.assertEqual(format_duration(9.8), "9s")
        self.assertEqual(format_duration(65), "1m 05s")
        self.assertEqual(format_duration(3661), "1h 01m")

    def test_tool_label_extracts_safe_short_summary(self):
        self.assertEqual(tool_label("Read", {"file_path": "/very/long/path/to/example.py"}), "Read: /very/long/path/to/example.py")
        label = tool_label("Bash", {"command": "python -m pytest bridge/tests/test_heartbeat_format.py --maxfail=1"})
        self.assertTrue(label.startswith("Bash: python -m pytest"))
        self.assertLessEqual(len(label), len("Bash: ") + 61)

    def test_compose_heartbeat_text(self):
        self.assertEqual(
            compose_heartbeat_text(elapsed_seconds=15, current_tool="Read: file.py"),
            "⏳ Working — 15s | Read: file.py",
        )
        self.assertEqual(
            compose_heartbeat_text(elapsed_seconds=75, current_tool=None, forecast_seconds=120),
            "⏳ Working — 1m 15s · ETA ~2m 00s",
        )

    def test_update_gate_and_progress_suppression_helpers(self):
        self.assertFalse(
            should_update_heartbeat(
                now=10,
                started_at=0,
                last_update_at=0,
                threshold_seconds=15,
                update_interval_seconds=15,
            )
        )
        self.assertTrue(
            should_update_heartbeat(
                now=15,
                started_at=0,
                last_update_at=0,
                threshold_seconds=15,
                update_interval_seconds=15,
            )
        )
        self.assertFalse(
            should_update_heartbeat(
                now=20,
                started_at=0,
                last_update_at=15,
                threshold_seconds=15,
                update_interval_seconds=15,
            )
        )
        self.assertTrue(has_recent_visible_progress(now=20, last_visible_progress_at=10, window_seconds=15))
        self.assertFalse(has_recent_visible_progress(now=30, last_visible_progress_at=10, window_seconds=15))


if __name__ == "__main__":
    unittest.main()
