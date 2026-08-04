import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_delegated_task_metrics_aggregate_without_persisting_request_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")
            reporter.initialize_process()

            reporter.record_delegated_task_activity(101, 4)
            reporter.record_delegated_task_activity(202, 2)
            reporter.record_terminal_stall_deferred_for_tasks()
            reporter.record_delegated_task_stall()
            reporter.record_delegated_task_activity(101, 0)

            health_text = reporter.health_file.read_text(encoding="utf-8")
            health = json.loads(health_text)
            self.assertEqual(health["requests"]["delegated_tasks_active"], 2)
            self.assertEqual(
                health["requests"]["terminal_stall_deferred_for_tasks"], 1
            )
            self.assertEqual(health["requests"]["delegated_task_stalls"], 1)
            self.assertNotIn("request_ref", health_text)
            self.assertEqual(
                set(health["requests"]),
                {
                    "stalled",
                    "delegated_tasks_active",
                    "terminal_stall_deferred_for_tasks",
                    "delegated_task_stalls",
                },
            )

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

    def test_crush_provider_is_reported_as_itself(self):
        # #926 added the crush lane but health kept a two-way codex/claude
        # test, so a crush node reported provider=claude and an operator could
        # not tell the lanes apart (measured on dungae, 2026-08-04).
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(
                project_root / ".telegram_bot", agent_provider="crush"
            )

            reporter.initialize_process()
            reporter.record_telegram_ok()
            reporter.record_agent_error("crush server did not become ready")

            health = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertEqual(health["agent"]["provider"], "crush")
            self.assertEqual(
                health["service"]["reason"],
                "Crush: crush server did not become ready",
            )

    def test_unknown_provider_falls_back_to_claude(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(
                project_root / ".telegram_bot", agent_provider="not-a-provider"
            )

            reporter.initialize_process()
            health = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertEqual(health["agent"]["provider"], "claude")

    def test_record_empty_completion_counts_by_outcome(self):
        # #775: empty normal completions split into the event-loss class
        # (recovered from the terminal payload) and the truly-empty class.
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")

            reporter.record_empty_completion(recovered=True)
            reporter.record_empty_completion(recovered=True)
            reporter.record_empty_completion(recovered=False)

            health = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertEqual(health["requests"]["empty_completion_recovered"], 2)
            self.assertEqual(health["requests"]["empty_completion_failed"], 1)

    def test_enabled_dead_session_wakeup_records_all_zero_scan_and_accumulates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(
                project_root / ".telegram_bot",
                dead_session_wakeup=True,
            )

            self.assertEqual(
                reporter.snapshot()["dead_session_wakeup"],
                {
                    "enabled": True,
                    "scans": 0,
                    "scanned": 0,
                    "triggered": 0,
                    "delivered": 0,
                    "failed": 0,
                    "skipped_active": 0,
                    "skipped_locked": 0,
                    "skipped_quarantine": 0,
                    "skipped_cooldown": 0,
                    "skipped_attempts": 0,
                    "skipped_budget": 0,
                    "last_scan_at": None,
                },
            )

            with patch.object(
                module,
                "_utc_now_iso",
                return_value="2026-07-29T12:00:00Z",
            ):
                reporter.record_dead_session_wakeup_scan(
                    scanned=0,
                    triggered=0,
                    delivered=0,
                    failed=0,
                    skipped_active=0,
                    skipped_locked=0,
                    skipped_quarantine=0,
                    skipped_cooldown=0,
                    skipped_attempts=0,
                    skipped_budget=1,
                )

            first = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertEqual(
                first["dead_session_wakeup"],
                {
                    "enabled": True,
                    "scans": 1,
                    "scanned": 0,
                    "triggered": 0,
                    "delivered": 0,
                    "failed": 0,
                    "skipped_active": 0,
                    "skipped_locked": 0,
                    "skipped_quarantine": 0,
                    "skipped_cooldown": 0,
                    "skipped_attempts": 0,
                    "skipped_budget": 1,
                    "last_scan_at": "2026-07-29T12:00:00Z",
                },
            )

            with patch.object(
                module,
                "_utc_now_iso",
                return_value="2026-07-29T12:01:00Z",
            ):
                reporter.record_dead_session_wakeup_scan(
                    scanned=4,
                    triggered=2,
                    delivered=1,
                    failed=1,
                    skipped_active=3,
                    skipped_locked=4,
                    skipped_quarantine=5,
                    skipped_cooldown=6,
                    skipped_attempts=7,
                    skipped_budget=2,
                )

            second = reporter.snapshot()["dead_session_wakeup"]
            self.assertEqual(
                second,
                {
                    "enabled": True,
                    "scans": 2,
                    "scanned": 4,
                    "triggered": 2,
                    "delivered": 1,
                    "failed": 1,
                    "skipped_active": 3,
                    "skipped_locked": 4,
                    "skipped_quarantine": 5,
                    "skipped_cooldown": 6,
                    "skipped_attempts": 7,
                    "skipped_budget": 3,
                    "last_scan_at": "2026-07-29T12:01:00Z",
                },
            )

    def test_disabled_dead_session_wakeup_never_fabricates_activity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(
                project_root / ".telegram_bot",
                dead_session_wakeup=False,
            )
            reporter.initialize_process()

            reporter.record_dead_session_wakeup_scan(
                scanned=3,
                triggered=2,
                delivered=1,
                failed=1,
            )

            self.assertEqual(
                reporter.snapshot()["dead_session_wakeup"],
                {"enabled": False},
            )
            on_disk = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["dead_session_wakeup"], {"enabled": False})

    def test_deferred_health_reporter_bind_carries_wakeup_configuration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.DeferredHealthReporter()

            reporter.bind(
                project_root / ".telegram_bot",
                "codex",
                True,
            )

            snapshot = reporter.snapshot()
            self.assertEqual(snapshot["agent"]["provider"], "codex")
            self.assertTrue(snapshot["dead_session_wakeup"]["enabled"])

    def test_record_workload_recomputes_oldest_turn_start_after_clock_step(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")
            first_observed = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

            reporter.record_workload(
                1,
                60,
                waiting_for_turn=1,
                utc_now=first_observed,
            )
            first = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            reporter.record_workload(
                1,
                70,
                waiting_for_turn=0,
                utc_now=first_observed - timedelta(hours=1) + timedelta(seconds=10),
            )
            second = json.loads(reporter.health_file.read_text(encoding="utf-8"))

            self.assertEqual(first["workload"]["active_requests"], 1)
            self.assertEqual(
                first["workload"]["turn_occupancy"],
                {
                    "state": "occupied",
                    "observed_at": "2026-07-15T12:00:00Z",
                    "oldest_turn_started_at": "2026-07-15T11:59:00Z",
                    "occupied_since": "2026-07-15T11:59:00Z",
                    "elapsed_seconds": 60,
                },
            )
            self.assertEqual(
                second["workload"]["turn_occupancy"]["oldest_turn_started_at"],
                "2026-07-15T10:59:00Z",
            )
            self.assertEqual(
                second["workload"]["turn_occupancy"]["occupied_since"],
                "2026-07-15T10:59:00Z",
            )
            self.assertEqual(
                second["workload"]["turn_occupancy"]["elapsed_seconds"], 70
            )
            self.assertEqual(second["workload"]["waiting_for_turn"], 0)

    def test_record_workload_recomputes_oldest_active_turn_after_handoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")

            reporter.record_workload(
                2,
                30 * 60,
                waiting_for_turn=1,
                utc_now=datetime(2026, 7, 15, 12, 30, 0, tzinfo=timezone.utc),
            )
            first = reporter.snapshot()["workload"]["turn_occupancy"]
            reporter.record_workload(
                1,
                2 * 60,
                waiting_for_turn=0,
                utc_now=datetime(2026, 7, 15, 12, 32, 5, tzinfo=timezone.utc),
            )
            second = reporter.snapshot()["workload"]["turn_occupancy"]

            self.assertEqual(
                first["oldest_turn_started_at"],
                "2026-07-15T12:00:00Z",
            )
            self.assertEqual(
                second["oldest_turn_started_at"],
                "2026-07-15T12:30:05Z",
            )

    def test_record_workload_production_defaults_stamp_current_wall_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")

            before = datetime.now(timezone.utc)
            reporter.record_workload(1, 5, waiting_for_turn=1)
            after = datetime.now(timezone.utc)

            occupancy = reporter.snapshot()["workload"]["turn_occupancy"]
            observed_at = datetime.fromisoformat(
                occupancy["observed_at"].replace("Z", "+00:00")
            )
            oldest_started = datetime.fromisoformat(
                occupancy["oldest_turn_started_at"].replace("Z", "+00:00")
            )
            self.assertLessEqual(before, observed_at)
            self.assertLessEqual(observed_at, after)
            self.assertGreaterEqual(
                (observed_at - oldest_started).total_seconds(),
                5,
            )
            self.assertLess(
                (observed_at - oldest_started).total_seconds(),
                6,
            )

    def test_positive_active_count_always_reports_occupied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")
            observed = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

            for active_count in (1, 2, 7):
                with self.subTest(active_count=active_count):
                    reporter.record_workload(
                        active_count,
                        10,
                        waiting_for_turn=1,
                        utc_now=observed,
                    )
                    workload = reporter.snapshot()["workload"]
                    self.assertEqual(
                        workload["turn_occupancy"]["state"],
                        "occupied",
                    )

    def test_idle_state_always_has_zero_active_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")

            reporter.record_workload(0, 999, waiting_for_turn=3)

            workload = reporter.snapshot()["workload"]
            self.assertEqual(workload["turn_occupancy"]["state"], "idle")
            self.assertEqual(workload["active_requests"], 0)
            self.assertEqual(workload["waiting_for_turn"], 0)

    def test_record_workload_idle_has_no_fabricated_elapsed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            module = self._load_health_module(project_root)
            reporter = module.RuntimeHealthReporter(project_root / ".telegram_bot")

            reporter.record_workload(
                1,
                90,
                waiting_for_turn=1,
                utc_now=datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc),
            )
            reporter.record_workload(
                0,
                0,
                utc_now=datetime(2026, 7, 15, 12, 1, 30, tzinfo=timezone.utc),
            )

            health = json.loads(reporter.health_file.read_text(encoding="utf-8"))
            self.assertEqual(
                health["workload"],
                {
                    "active_requests": 0,
                    "oldest_request_age_seconds": 0,
                    "waiting_for_turn": 0,
                    "turn_occupancy": {
                        "state": "idle",
                        "observed_at": "2026-07-15T12:01:30Z",
                    },
                },
            )


if __name__ == "__main__":
    unittest.main()
