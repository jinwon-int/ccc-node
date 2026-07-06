"""Unit tests for the persistent heartbeat message-id registry."""

import json
import tempfile
import unittest
from pathlib import Path

from telegram_bot.utils.heartbeat_store import (
    default_heartbeat_store_path,
    discard_heartbeat,
    drain_heartbeats,
    record_heartbeat,
    store_path_for,
)


class HeartbeatStoreTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = Path(self._td.name) / "sub" / "heartbeats.json"

    def test_record_creates_and_persists(self):
        record_heartbeat(self.path, 111, 222)
        self.assertTrue(self.path.exists())
        self.assertEqual(drain_heartbeats(self.path), [(111, 222)])

    def test_record_is_idempotent(self):
        record_heartbeat(self.path, 111, 222)
        record_heartbeat(self.path, 111, 222)
        record_heartbeat(self.path, 111, 333)
        self.assertEqual(
            sorted(drain_heartbeats(self.path)), [(111, 222), (111, 333)]
        )

    def test_discard_removes_only_matching_ref(self):
        record_heartbeat(self.path, 1, 10)
        record_heartbeat(self.path, 1, 11)
        discard_heartbeat(self.path, 1, 10)
        self.assertEqual(drain_heartbeats(self.path), [(1, 11)])

    def test_discard_missing_ref_is_noop(self):
        record_heartbeat(self.path, 1, 10)
        discard_heartbeat(self.path, 9, 9)  # not present
        self.assertEqual(drain_heartbeats(self.path), [(1, 10)])

    def test_drain_clears_the_file(self):
        record_heartbeat(self.path, 5, 6)
        self.assertEqual(drain_heartbeats(self.path), [(5, 6)])
        # File removed → second drain is empty.
        self.assertFalse(self.path.exists())
        self.assertEqual(drain_heartbeats(self.path), [])

    def test_drain_missing_file_returns_empty(self):
        self.assertEqual(drain_heartbeats(self.path), [])

    def test_corrupted_file_fails_open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json", encoding="utf-8")
        # Reads yield nothing; a record still succeeds by overwriting.
        self.assertEqual(drain_heartbeats(self.path), [])
        record_heartbeat(self.path, 7, 8)
        self.assertEqual(drain_heartbeats(self.path), [(7, 8)])

    def test_malformed_entries_are_skipped(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([[1, 2], ["a", "b"], [3], [4, 5, 6], [7, 8]]),
            encoding="utf-8",
        )
        self.assertEqual(sorted(drain_heartbeats(self.path)), [(1, 2), (7, 8)])

    def test_store_path_for_override_and_default_and_none(self):
        override = Path("/tmp/custom-heartbeats.json")
        self.assertEqual(store_path_for(Path("/data"), override), override)
        self.assertEqual(
            store_path_for(Path("/data")),
            default_heartbeat_store_path(Path("/data")),
        )
        self.assertIsNone(store_path_for(None))


if __name__ == "__main__":
    unittest.main()
