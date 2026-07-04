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
    forecast_samples,
    recent_samples,
    remaining_ms,
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

    def test_forecast_samples_fallback_chain(self):
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
            # exact user+model level qualifies
            self.assertEqual(
                forecast_samples(path, user_id=7, model="sonnet", min_samples=3),
                [100, 200, 300],
            )
            # unknown user falls back to the global level (same rows here)
            self.assertEqual(
                forecast_samples(path, user_id=8, model="opus", min_samples=3),
                [100, 200, 300],
            )
            # nothing qualifies below min_samples
            self.assertEqual(
                forecast_samples(path, user_id=8, model="opus", min_samples=10), []
            )


class RemainingMsTests(unittest.TestCase):
    def test_conditions_on_samples_longer_than_elapsed(self):
        # Mixed quick/slow history: at t=0 the naive median (30s) applies, but
        # once elapsed passes the quick cluster only the slow samples remain
        # informative and the estimate updates instead of going stale.
        values = [5_000, 6_000, 30_000, 300_000, 360_000, 420_000]
        self.assertEqual(remaining_ms(values, elapsed_ms=0), 165_000)
        self.assertEqual(remaining_ms(values, elapsed_ms=60_000), 300_000)

    def test_remaining_decreases_with_elapsed_within_cluster(self):
        values = [120_000, 120_000, 120_000]
        self.assertEqual(remaining_ms(values, elapsed_ms=0), 120_000)
        self.assertEqual(remaining_ms(values, elapsed_ms=30_000), 90_000)

    def test_hides_when_too_few_comparable_samples(self):
        # elapsed beyond nearly all history -> honest None, not a stale number.
        values = [120_000, 120_000, 120_000]
        self.assertIsNone(remaining_ms(values, elapsed_ms=200_000))
        self.assertIsNone(remaining_ms([], elapsed_ms=0))

    def test_rounds_to_whole_seconds_and_floors_at_zero(self):
        self.assertEqual(remaining_ms([120_400] * 3, elapsed_ms=120_000), 0)
        self.assertEqual(remaining_ms([121_600] * 3, elapsed_ms=120_000), 2_000)


if __name__ == "__main__":
    unittest.main()
