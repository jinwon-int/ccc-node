import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class RuntimeHealthReporterTests(unittest.TestCase):
    def _load_health_module(self, project_root: Path):
        original_config = sys.modules.pop("telegram_bot.utils.config", None)
        original_health = sys.modules.pop("telegram_bot.utils.health", None)

        def restore_modules():
            if original_config is not None:
                sys.modules["telegram_bot.utils.config"] = original_config
            else:
                sys.modules.pop("telegram_bot.utils.config", None)
            if original_health is not None:
                sys.modules["telegram_bot.utils.health"] = original_health
            else:
                sys.modules.pop("telegram_bot.utils.health", None)

        self.addCleanup(restore_modules)

        with patch.dict(
            os.environ,
            {
                "PROJECT_ROOT": str(project_root),
                "TELEGRAM_BOT_TOKEN": "123456789:test-token",
            },
            clear=False,
        ):
            import telegram_bot.utils.health as health_module

            return importlib.reload(health_module)

    def test_initialize_process_uses_runtime_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")

            lock_file = project_root / ".telegram_bot" / "token.lock"
            with patch.dict(
                os.environ,
                {
                    "BOT_PROCESS_MODE": "daemon",
                    "BOT_TOKEN_LOCK_FILE": str(lock_file),
                    "BOT_OWNS_TOKEN_LOCK": "1",
                },
                clear=False,
            ):
                reporter.initialize_process()

            health = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertEqual(health["process"]["mode"], "daemon")
            self.assertEqual(health["process"]["pid"], os.getpid())
            self.assertEqual(health["service"]["state"], "starting")
            self.assertTrue(reporter.pid_file.exists())

    def test_health_transitions_and_cleanup_preserve_health_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")
            lock_file = project_root / ".telegram_bot" / "token.lock"
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock_file.write_text("lock\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "BOT_PROCESS_MODE": "foreground",
                    "BOT_TOKEN_LOCK_FILE": str(lock_file),
                    "BOT_OWNS_TOKEN_LOCK": "1",
                },
                clear=False,
            ):
                reporter.initialize_process()
                reporter.record_telegram_ok()
                reporter.record_claude_error("auth unavailable")
                reporter.mark_unavailable("Stopped by signal")
                reporter.cleanup_runtime_files()

            health = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertEqual(health["telegram"]["state"], "healthy")
            self.assertEqual(health["claude"]["state"], "degraded")
            self.assertEqual(health["service"]["state"], "unavailable")
            self.assertEqual(health["service"]["reason"], "Stopped by signal")
            self.assertFalse(reporter.pid_file.exists())
            self.assertFalse(lock_file.exists())


if __name__ == "__main__":
    unittest.main()
