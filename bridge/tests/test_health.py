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

    def test_cleanup_preserves_pid_file_owned_by_another_process(self):
        """A dying instance must not delete the pid file of a concurrent
        surviving instance (pid-file race — observed on daegyo 2026-07-08)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")

            with patch.dict(
                os.environ,
                {"BOT_PROCESS_MODE": "foreground", "BOT_OWNS_TOKEN_LOCK": "0"},
                clear=False,
            ):
                reporter.initialize_process()
                # Another (surviving) instance overwrites the shared pid file.
                other_pid = os.getpid() + 1
                reporter.pid_file.write_text(f"{other_pid}\n", encoding="utf-8")
                reporter.cleanup_runtime_files()

            self.assertTrue(reporter.pid_file.exists())
            self.assertEqual(
                reporter.pid_file.read_text(encoding="utf-8").strip(),
                str(other_pid),
            )

    def _spawn_live_pid(self) -> int:
        """A real, live process pid (a short sleeper) cleaned up after the test."""
        import subprocess

        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.kill)
        return proc.pid

    def test_write_pid_does_not_clobber_live_foreign_instance(self):
        """The survivor race root cause (#703): a newcomer must NOT overwrite a
        pid file that records a different, live bot — otherwise, when the
        newcomer later loses the getUpdates conflict and exits, its own cleanup
        deletes the file and orphans the survivor."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")

            survivor_pid = self._spawn_live_pid()
            reporter.pid_file.parent.mkdir(parents=True, exist_ok=True)
            reporter.pid_file.write_text(f"{survivor_pid}\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {"BOT_PROCESS_MODE": "foreground", "BOT_OWNS_TOKEN_LOCK": "0"},
                clear=False,
            ):
                # Newcomer initializes while the survivor is alive …
                reporter.initialize_process()
                self.assertEqual(
                    reporter.pid_file.read_text(encoding="utf-8").strip(),
                    str(survivor_pid),
                    "newcomer must not clobber a live survivor's pid file",
                )
                # … and when the newcomer exits, it must not delete it.
                reporter.cleanup_runtime_files()

            self.assertTrue(reporter.pid_file.exists())
            self.assertEqual(
                reporter.pid_file.read_text(encoding="utf-8").strip(),
                str(survivor_pid),
            )

    def test_write_pid_claims_file_recording_dead_pid(self):
        """Legitimate restart: a pid file recording a dead pid is reclaimed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")

            reporter.pid_file.parent.mkdir(parents=True, exist_ok=True)
            reporter.pid_file.write_text("999999\n", encoding="utf-8")  # dead

            with patch.dict(
                os.environ,
                {"BOT_PROCESS_MODE": "foreground", "BOT_OWNS_TOKEN_LOCK": "0"},
                clear=False,
            ):
                reporter.initialize_process()

            self.assertEqual(
                reporter.pid_file.read_text(encoding="utf-8").strip(),
                str(os.getpid()),
            )

    def test_cleanup_preserves_token_lock_owned_by_live_survivor(self):
        """A losing instance (BOT_OWNS_TOKEN_LOCK=1) must not delete a token
        lock the survivor has overwritten with its own live pid (#703)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")

            lock_file = project_root / ".telegram_bot" / "token.lock"
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            survivor_pid = self._spawn_live_pid()
            lock_file.write_text(f"{survivor_pid}\n", encoding="utf-8")

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
                reporter.cleanup_runtime_files()

            self.assertTrue(lock_file.exists())
            self.assertEqual(
                lock_file.read_text(encoding="utf-8").strip(),
                str(survivor_pid),
            )

    def test_codex_provider_reports_active_agent_and_legacy_alias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(
                project_root / ".telegram_bot", agent_provider="codex"
            )

            reporter.initialize_process()
            reporter.record_telegram_ok()
            reporter.record_agent_error("codex authentication unavailable")

            health = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertEqual(health["agent"]["provider"], "codex")
            self.assertEqual(health["agent"]["state"], "degraded")
            self.assertEqual(health["claude"]["state"], "degraded")
            self.assertEqual(
                health["service"]["reason"],
                "Codex: codex authentication unavailable",
            )

            reporter.record_agent_ok()
            health = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertEqual(health["agent"]["state"], "healthy")
            self.assertEqual(health["claude"]["state"], "healthy")
            self.assertEqual(health["service"]["state"], "available")


if __name__ == "__main__":
    unittest.main()
