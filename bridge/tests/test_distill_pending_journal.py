"""Contract tests for the provider-neutral pending distill journal (#584)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from telegram_bot.memory.distill_pending_journal import (
        PENDING_DISTILL_ID_DISCRIMINATOR,
        PENDING_DISTILL_SCHEMA,
        PendingDistillError,
        PendingDistillJournal,
        pending_distill_job_id,
    )
except ModuleNotFoundError:  # Direct stdlib-only checkout validation.
    from bridge.memory.distill_pending_journal import (
        PENDING_DISTILL_ID_DISCRIMINATOR,
        PENDING_DISTILL_SCHEMA,
        PendingDistillError,
        PendingDistillJournal,
        pending_distill_job_id,
    )


class PendingDistillJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.transcript = self.base / "transcript.jsonl"
        self.transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def enqueue(
        self,
        journal: PendingDistillJournal,
        *,
        session_id: str = "session-1",
        transcript: Path | None = None,
    ):
        return journal.enqueue_once(
            session_id=session_id,
            transcript_path=transcript or self.transcript,
            source_cwd="/workspace with spaces",
            source_project="-workspace-with-spaces",
            trigger="sessionend",
            dryrun=1,
            isolation_profile="external",
            wiki_memory_enabled="1",
            memory_audience_scoped="0",
            memory_audience="legacy",
            memory_scope="",
            honcho_memory_enabled="1",
            memory_user_label="user",
            memory_assistant_label="assistant",
            created_at="2026-08-01T00:00:00Z",
        )

    def test_enqueue_deduplicates_with_stable_legacy_v1_id(self) -> None:
        journal = PendingDistillJournal(self.base / "journal")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.enqueue(journal), range(8)))

        jobs = {job.job_id for job, _created in results}
        self.assertEqual(len(jobs), 1)
        self.assertEqual(sum(created for _job, created in results), 1)
        job = results[0][0]
        expected_hash = __import__("hashlib").sha256(
            self.transcript.read_bytes()
        ).hexdigest()
        self.assertEqual(
            job.job_id, pending_distill_job_id("session-1", expected_hash)
        )
        self.assertEqual(PENDING_DISTILL_ID_DISCRIMINATOR, "v1")
        self.assertEqual(stat.S_IMODE(journal.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(journal.job_path(job.job_id).stat().st_mode), 0o600)
        self.assertEqual(journal.job_path(job.job_id).stat().st_nlink, 1)

    def test_existing_bash_v1_record_is_discoverable_and_drainable(self) -> None:
        journal = PendingDistillJournal(self.base / "legacy")
        journal.initialize()
        transcript_hash = __import__("hashlib").sha256(
            self.transcript.read_bytes()
        ).hexdigest()
        job_id = pending_distill_job_id("legacy-session", transcript_hash)
        payload = {
            "schema": PENDING_DISTILL_SCHEMA,
            "job_id": job_id,
            "transcript_sha256": transcript_hash,
            "session_id": "legacy-session",
            "transcript_path": str(self.transcript),
            "source_cwd": "/legacy cwd",
            "source_project": "-legacy-cwd",
            "trigger": "sessionend",
            "dryrun": 1,
            "created_at": "2026-07-01T00:00:00Z",
            "isolation_profile": "fleet",
            "wiki_memory_enabled": "1",
        }
        path = journal.job_path(job_id)
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)

        ready, rejected = journal.scan(limit=3)
        self.assertEqual(ready, (path,))
        self.assertEqual(rejected, ())
        result = journal.claim_and_run(path, [sys.executable, "-c", "pass"])
        self.assertEqual(result.status, "completed")
        self.assertFalse(path.exists())

    def test_concurrent_claim_has_one_winner_and_reports_held(self) -> None:
        journal = PendingDistillJournal(self.base / "claims")
        job, _ = self.enqueue(journal)
        path = journal.job_path(job.job_id)
        marker = self.base / "child-started"
        release = self.base / "child-release"
        child = (
            "from pathlib import Path; import sys,time; "
            "marker=Path(sys.argv[1]); release=Path(sys.argv[2]); "
            "marker.touch(); deadline=time.monotonic()+5; "
            "exec('while not release.exists() and time.monotonic() < deadline:\\n time.sleep(0.01)')"
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(
                journal.claim_and_run,
                path,
                [sys.executable, "-c", child, str(marker), str(release)],
            )
            self._wait_for(marker.exists)
            held = journal.claim_and_run(path, [sys.executable, "-c", "pass"])
            release.touch()
            completed = winner.result(timeout=5)

        self.assertEqual(held.status, "held")
        self.assertEqual(completed.status, "completed")
        self.assertFalse(path.exists())

    def test_preheld_legacy_lock_does_not_claim_or_remove(self) -> None:
        journal = PendingDistillJournal(self.base / "held")
        job, _ = self.enqueue(journal)
        path = journal.job_path(job.job_id)
        lock_path = path.with_name(f"{path.name}.lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = journal.claim_and_run(path, [sys.executable, "-c", "pass"])
        finally:
            os.close(descriptor)
        self.assertEqual(result.status, "held")
        self.assertTrue(path.exists())

    def test_failed_and_killed_workers_retain_then_retry_successfully(self) -> None:
        journal = PendingDistillJournal(self.base / "retry")
        job, _ = self.enqueue(journal)
        path = journal.job_path(job.job_id)
        failed = journal.claim_and_run(
            path, [sys.executable, "-c", "raise SystemExit(9)"]
        )
        self.assertEqual((failed.status, failed.child_returncode), ("retained", 9))
        self.assertTrue(path.exists())

        marker = self.base / "crash-child-started"
        child = "from pathlib import Path; import sys,time; Path(sys.argv[1]).touch(); time.sleep(30)"
        cli = Path(__file__).parents[2] / "claude/hooks/distill/pending-job.py"
        process = subprocess.Popen(
            [
                sys.executable,
                str(cli),
                "run",
                "--queue-dir",
                str(journal.root),
                "--job-path",
                str(path),
                "--",
                sys.executable,
                "-c",
                child,
                str(marker),
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self._wait_for(marker.exists)
            held = journal.claim_and_run(path, [sys.executable, "-c", "pass"])
            self.assertEqual(held.status, "held")
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        self.assertTrue(path.exists())

        completed = journal.claim_and_run(path, [sys.executable, "-c", "pass"])
        self.assertEqual(completed.status, "completed")
        self.assertFalse(path.exists())

    def test_corrupt_symlink_hardlink_mode_and_foreign_owner_are_rejected(self) -> None:
        unsafe_dir = self.base / "unsafe-mode"
        unsafe_dir.mkdir(mode=0o700)
        unsafe_dir.chmod(0o750)
        with self.assertRaisesRegex(PendingDistillError, "pending-dir-mode"):
            PendingDistillJournal(unsafe_dir).validate_existing()

        real_dir = self.base / "real-dir"
        real_dir.mkdir(mode=0o700)
        linked_dir = self.base / "linked-dir"
        linked_dir.symlink_to(real_dir, target_is_directory=True)
        with self.assertRaisesRegex(PendingDistillError, "pending-dir-unsafe"):
            PendingDistillJournal(linked_dir).validate_existing()

        journal = PendingDistillJournal(self.base / "unsafe-jobs")
        job, _ = self.enqueue(journal)
        path = journal.job_path(job.job_id)
        path.chmod(0o640)
        with self.assertRaisesRegex(PendingDistillError, "job-mode"):
            journal.get(job.job_id)
        path.chmod(0o600)

        hardlink = self.base / "second-link"
        os.link(path, hardlink)
        with self.assertRaisesRegex(PendingDistillError, "job-multiple-links"):
            journal.get(job.job_id)
        hardlink.unlink()

        path.unlink()
        target = self.base / "target"
        target.write_text("{}", encoding="utf-8")
        path.symlink_to(target)
        ready, rejected = journal.scan(limit=3)
        self.assertEqual(ready, ())
        self.assertIn("job-symlink", rejected)
        self.assertTrue(path.is_symlink())

        path.unlink()
        path.write_text("not-json", encoding="utf-8")
        path.chmod(0o600)
        ready, rejected = journal.scan(limit=3)
        self.assertEqual(ready, ())
        self.assertIn("job-json-invalid", rejected)
        self.assertTrue(path.exists())

        with patch(
            f"{PendingDistillJournal.__module__}.os.getuid",
            return_value=os.getuid() + 1,
        ), self.assertRaisesRegex(PendingDistillError, "pending-dir-owner"):
            journal.validate_existing()

    def test_paths_with_spaces_and_nofollow_fallback(self) -> None:
        spaced = self.base / "queue with spaces"
        transcript = self.base / "transcript with spaces.jsonl"
        transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
        journal = PendingDistillJournal(spaced)
        job, _ = self.enqueue(journal, transcript=transcript)
        path = journal.job_path(job.job_id)

        original = getattr(os, "O_NOFOLLOW", None)
        try:
            if original is not None:
                delattr(os, "O_NOFOLLOW")
            self.assertEqual(journal.get(job.job_id).transcript_path, str(transcript))
            link = self.base / "transcript-link.jsonl"
            link.symlink_to(transcript)
            with self.assertRaisesRegex(PendingDistillError, "transcript-not-regular"):
                self.enqueue(journal, session_id="symlinked", transcript=link)
        finally:
            if original is not None:
                setattr(os, "O_NOFOLLOW", original)

        result = journal.claim_and_run(path, [sys.executable, "-c", "pass"])
        self.assertEqual(result.status, "completed")

    def test_cli_diagnostics_never_echo_job_or_transcript_content(self) -> None:
        journal = PendingDistillJournal(self.base / "body-free")
        journal.initialize()
        job_id = "a" * 64
        path = journal.job_path(job_id)
        secret = "credential=raw-secret-do-not-log"
        path.write_text(json.dumps({"schema": PENDING_DISTILL_SCHEMA, "body": secret}))
        path.chmod(0o600)
        cli = Path(__file__).parents[2] / "claude/hooks/distill/pending-job.py"

        result = subprocess.run(
            [
                sys.executable,
                str(cli),
                "scan",
                "--queue-dir",
                str(journal.root),
                "--limit",
                "3",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertIn("pending rejected reason=", result.stderr)
        self.assertTrue(path.exists())

    @staticmethod
    def _wait_for(predicate, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("timed out waiting for test process")


if __name__ == "__main__":
    unittest.main()
