# ruff: noqa: E402
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_bot.core.session_resume import (
    persisted_transcript_exists,
    resume_persisted_enabled,
)


class ResumePersistedEnabledTests(unittest.TestCase):
    def test_default_is_enabled(self):
        self.assertTrue(resume_persisted_enabled({}))

    def test_explicit_true_values(self):
        for value in ("true", "1", "yes", "on", "TRUE", " anything "):
            with self.subTest(value=value):
                self.assertTrue(
                    resume_persisted_enabled({"CCC_RESUME_PERSISTED_SESSIONS": value})
                )

    def test_false_values_disable(self):
        for value in ("false", "0", "no", "off", "FALSE", " Off "):
            with self.subTest(value=value):
                self.assertFalse(
                    resume_persisted_enabled({"CCC_RESUME_PERSISTED_SESSIONS": value})
                )


class PersistedTranscriptExistsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conv_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_existing_transcript_is_resumable(self):
        (self.conv_dir / "abc-123.jsonl").write_text("{}\n")
        self.assertTrue(persisted_transcript_exists(self.conv_dir, "abc-123"))

    def test_missing_transcript_is_not_resumable(self):
        self.assertFalse(persisted_transcript_exists(self.conv_dir, "abc-123"))

    def test_none_or_empty_inputs(self):
        self.assertFalse(persisted_transcript_exists(None, "abc-123"))
        self.assertFalse(persisted_transcript_exists(self.conv_dir, None))
        self.assertFalse(persisted_transcript_exists(self.conv_dir, ""))

    def test_path_traversal_session_id_rejected(self):
        outside = Path(self._tmp.name) / "outside.jsonl"
        outside.write_text("{}\n")
        nested = self.conv_dir / "nested"
        nested.mkdir()
        self.assertFalse(persisted_transcript_exists(nested, "../outside"))

    def test_directory_named_like_transcript_is_not_a_file(self):
        (self.conv_dir / "dir-id.jsonl").mkdir()
        self.assertFalse(persisted_transcript_exists(self.conv_dir, "dir-id"))


if __name__ == "__main__":
    unittest.main()
