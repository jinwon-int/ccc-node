"""Regression test for setup_logging() root/handler level gating.

The bug: setup_logging did `logging.basicConfig(level=WARNING)` in non-debug
mode, which set the ROOT logger level to WARNING. The root level is a hard gate
applied before any handler's own level, so every INFO record (boot banners and
operational markers like "Ignoring persisted session_id") was dropped before it
could reach the INFO-level file handler — bot.log silently lost all INFO output.

These tests pin the invariant: the root passes INFO+, the file handler captures
INFO, and the console handler stays quiet at WARNING in non-debug mode.
"""

import logging
import tempfile
import types
import unittest
from pathlib import Path

pytest = None
try:  # pydantic-backed config; skip cleanly where deps aren't installed.
    from telegram_bot.utils.logging_setup import setup_logging
    _HAVE_CONFIG = True
except Exception:  # pragma: no cover - import guard
    _HAVE_CONFIG = False


@unittest.skipUnless(_HAVE_CONFIG, "config (pydantic) not importable in this env")
class SetupLoggingLevelTest(unittest.TestCase):
    def setUp(self):
        self._root = logging.getLogger()
        self._saved_handlers = list(self._root.handlers)
        self._saved_level = self._root.level
        # Explicit settings, like the production caller (__main__ always
        # passes them): other test modules leak fake config modules into
        # sys.modules, so the ambient-default path is not test-stable.
        self._tmp = tempfile.TemporaryDirectory()
        logs_dir = Path(self._tmp.name) / "logs"
        self._settings = types.SimpleNamespace(
            logs_dir=logs_dir,
            log_level="INFO",
            log_format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

    def tearDown(self):
        self._tmp.cleanup()
        for h in list(self._root.handlers):
            if h not in self._saved_handlers:
                try:
                    h.close()
                except Exception:
                    pass
                self._root.removeHandler(h)
        self._root.setLevel(self._saved_level)

    def _console_handlers(self):
        return [
            h
            for h in self._root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]

    def test_root_passes_info_and_console_stays_warning(self):
        setup_logging(self._settings)
        # Root must pass INFO+ so the INFO file handler actually receives records.
        self.assertLessEqual(self._root.level, logging.INFO)
        # At least one file handler exists and captures INFO.
        file_handlers = [
            h for h in self._root.handlers if isinstance(h, logging.FileHandler)
        ]
        self.assertTrue(file_handlers)
        self.assertTrue(any(h.level <= logging.INFO for h in file_handlers))
        # Console handlers stay quiet (WARNING+) in non-debug mode.
        for h in self._console_handlers():
            self.assertGreaterEqual(h.level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
