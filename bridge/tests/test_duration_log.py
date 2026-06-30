import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
telegram_bot_pkg = types.ModuleType("telegram_bot")
telegram_bot_pkg.__path__ = [str(BRIDGE_DIR)]
sys.modules.setdefault("telegram_bot", telegram_bot_pkg)

from telegram_bot.utils.duration_log import (
    append_duration_sample,
    default_duration_log_path,
    forecast_ms,
    recent_samples,
)


class DurationLogTests(unittest.TestCase):
    def test_default_path_lives_under_bot_data_dir(self):
        self.assertEqual(
            default_duration_log_path(Path("/tmp/bot-data")),
            Path("/tmp/bot-data") / "duration.jsonl",
        )

    def test_append_writes_metadata_without_prompt_text(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "duration.jsonl"
            written = append_duration_sample(
                path=path,
                user_id=11,
                chat_id=22,
                session_id="session-1",
                model="sonnet",
                duration_ms=1234,
                success=True,
                now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )
            self.assertEqual(written, path)
            row = json.loads(path.read_text().strip())
            self.assertEqual(row["user_id"], 11)
            self.assertEqual(row["chat_id"], 22)
            self.assertEqual(row["session_id"], "session-1")
            self.assertEqual(row["model"], "sonnet")
            self.assertEqual(row["duration_ms"], 1234)
            self.assertTrue(row["success"])
            self.assertNotIn("prompt", row)
            self.assertNotIn("content", row)

    def test_trim_keeps_recent_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "duration.jsonl"
            for i in range(5):
                append_duration_sample(
                    path=path,
                    user_id=1,
                    chat_id=2,
                    session_id=None,
                    model="sonnet",
                    duration_ms=i,
                    success=True,
                    max_lines=3,
                )
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["duration_ms"] for row in rows], [2, 3, 4])

    def test_recent_and_forecast_filters(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "duration.jsonl"
            for value in [100, 200, 300]:
                append_duration_sample(
                    path=path,
                    user_id=7,
                    chat_id=2,
                    session_id=None,
                    model="sonnet",
                    duration_ms=value,
                    success=True,
                )
            append_duration_sample(
                path=path,
                user_id=7,
                chat_id=2,
                session_id=None,
                model="sonnet",
                duration_ms=9999,
                success=False,
            )
            self.assertEqual(
                [row["duration_ms"] for row in recent_samples(path, user_id=7, model="sonnet")],
                [100, 200, 300],
            )
            self.assertEqual(forecast_ms(path, user_id=7, model="sonnet", min_samples=3), 200)
            self.assertIsNone(forecast_ms(path, user_id=8, model="sonnet", min_samples=10))


if __name__ == "__main__":
    unittest.main()
