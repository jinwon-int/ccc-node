#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's Claude transcripts-dir checks (#2306).

The distill snapshot refuses a group/other-accessible transcripts directory, and
every refused job terminalized as ``invalid_snapshot_request`` with no log line:
two nodes silently lost days of memory (0775/0755 dirs from the umask). These
tests pin that the doctor reports the served directory's mode and the recent
refusal count, and that it never changes anything it inspects.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import contextlib
import json
import os
import signal
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor, claude_project_slug  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DIR_ITEM = "claude transcripts dir"
REFUSAL_ITEM = "distill snapshot refusals"
EMPTY_ITEM = "distill empty snapshots"


class TranscriptsPrivacyCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.base = Path(self._temp.name).resolve()
        self.claude_dir = self.base / ".claude"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.served = self.claude_dir / "projects" / claude_project_slug(self.base)
        self.env = {"CCC_DOCTOR_BRIDGE_PROJECT_ROOT": str(self.base), "PATH": os.environ.get("PATH", "")}

    def tearDown(self) -> None:
        self._temp.cleanup()

    def run_checks(self) -> Doctor:
        doctor = Doctor(REPO, self.claude_dir, "settings")
        with patch.dict("os.environ", self.env, clear=True):
            doctor.check_claude_transcripts_privacy()
            doctor.check_snapshot_refusals()
            doctor.check_empty_snapshot_successes()
        return doctor

    def row(self, doctor: Doctor, item: str):
        rows = [r for r in doctor.rows if r.item == item]
        self.assertEqual(len(rows), 1, [r.item for r in doctor.rows])
        return rows[0]

    def test_slug_matches_bridge_rule(self) -> None:
        self.assertEqual(
            claude_project_slug(Path("/data/data/com.termux/files/home")),
            "-data-data-com-termux-files-home",
        )
        self.assertEqual(claude_project_slug(Path("/opt/ccc_node")), "-opt-ccc-node")

    def test_missing_dir_is_not_applicable(self) -> None:
        row = self.row(self.run_checks(), DIR_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("해당 없음", row.status)

    def test_private_dir_is_ok(self) -> None:
        self.served.mkdir(mode=0o700)
        row = self.row(self.run_checks(), DIR_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("mode=0700", row.status)

    def test_umask_dir_warns_and_is_not_modified(self) -> None:
        for mode in (0o755, 0o775):
            self.served.mkdir(mode=0o700, exist_ok=True)
            self.served.chmod(mode)
            row = self.row(self.run_checks(), DIR_ITEM)
            self.assertEqual(row.klass, "경고")
            self.assertIn(f"mode={mode:04o}", row.status)
            self.assertIn("chmod 700", row.action)
            self.assertEqual(self.served.stat().st_mode & 0o777, mode)

    def test_symlinked_dir_needs_manual_fix(self) -> None:
        real = self.base / "elsewhere"
        real.mkdir(mode=0o700)
        self.served.symlink_to(real)
        row = self.row(self.run_checks(), DIR_ITEM)
        self.assertEqual(row.klass, "수동필요")
        self.assertIn("not a real directory", row.status)

    def test_foreign_owner_needs_manual_fix(self) -> None:
        self.served.mkdir(mode=0o700)
        with patch("ccc_doctor.os.geteuid", return_value=os.geteuid() + 1):
            row = self.row(self.run_checks(), DIR_ITEM)
        self.assertEqual(row.klass, "수동필요")
        self.assertIn("owner uid=", row.status)

    def _job(self, journal: Path, name: str, payload: object, *, age_days: float = 0) -> None:
        journal.mkdir(parents=True, exist_ok=True)
        path = journal / f"{name}.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))

    def test_no_journals_reports_zero(self) -> None:
        row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("recent=0", row.status)

    def test_recent_refusals_warn_old_and_other_errors_ignored(self) -> None:
        matrix = self.base / ".ccc-matrix" / "distill-journal"
        telegram = self.base / ".telegram_bot" / "distill-journal"
        refused = {"error_code": "invalid_snapshot_request", "updated_at": "2026-10-14T02:40:00Z"}
        self._job(matrix, "a", refused)
        self._job(matrix, "b", {**refused, "updated_at": "2026-10-13T19:45:00Z"})
        self._job(telegram, "c", {"error_code": "max_attempts_exceeded"})
        self._job(telegram, "d", refused, age_days=8)
        self._job(matrix, "e", "{not json")
        row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertEqual(row.klass, "경고")
        self.assertIn("invalid_snapshot_request=2 in 7d", row.status)
        self.assertIn("latest 2026-10-14T02:40", row.status)
        self.assertIn("ccc-matrix", row.status)
        self.assertNotIn("telegram", row.status)
        self.assertIn("unreadable=1", row.status)
        self.assertIn("explicit approval", row.action)

    def test_danso_journal_counted_and_action_says_it_clears(self) -> None:
        journal = self.base / ".ccc-matrix" / "danso-distill-journal"
        self._job(journal, "a", {"error_code": "invalid_snapshot_request", "created_at": "2026-10-07T13:49:00Z"})
        row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertEqual(row.klass, "경고")
        self.assertIn("invalid_snapshot_request=1", row.status)
        self.assertIn("clears on its own", row.action)

    def test_unreadable_journal_dir_is_not_reported_as_zero(self) -> None:
        journal = self.base / ".ccc-matrix" / "distill-journal"
        self._job(journal, "a", {"error_code": "invalid_snapshot_request"})
        journal.chmod(0)
        try:
            if os.access(journal, os.R_OK):
                self.skipTest("running with read access to mode-0 dirs (root)")
            row = self.row(self.run_checks(), REFUSAL_ITEM)
        finally:
            journal.chmod(0o700)
        self.assertEqual(row.klass, "경고")
        self.assertIn("unreadable=1", row.status)
        self.assertIn("bridge user", row.action)

    def test_danso_and_claude_journals_are_labelled_apart(self) -> None:
        refused = {"error_code": "invalid_snapshot_request", "updated_at": "2026-10-14T02:40:00Z"}
        self._job(self.base / ".ccc-matrix" / "danso-distill-journal", "a", refused)
        self._job(self.base / ".telegram_bot" / "danso-distill-journal", "b", refused)
        danso = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertIn("ccc-matrix/danso,telegram_bot/danso)", danso.status)

        self._job(self.base / ".ccc-matrix" / "distill-journal", "c", refused)
        both = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertIn("invalid_snapshot_request=3", both.status)
        self.assertIn("(latest 2026-10-14T02:40, ccc-matrix,ccc-matrix/danso,telegram_bot/danso)", both.status)

    def test_claude_journal_label_has_no_danso_suffix(self) -> None:
        self._job(self.base / ".telegram_bot" / "distill-journal", "a", {"error_code": "invalid_snapshot_request"})
        row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertIn(", telegram_bot)", row.status)
        self.assertNotIn("danso", row.status)

    @contextlib.contextmanager
    def deadline(self, seconds: int = 5) -> Iterator[None]:
        """Fail (not hang) if the journal scan blocks — e.g. reading a FIFO (#2308)."""

        def _expired(signum: int, frame: object) -> None:
            raise AssertionError(f"did not finish within {seconds}s (blocked on a journal entry?)")

        previous = signal.signal(signal.SIGALRM, _expired)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(signal, "SIGALRM"), "needs mkfifo and SIGALRM")
    def test_fifo_and_symlink_entries_are_skipped_in_bounded_time(self) -> None:
        journal = self.base / ".ccc-matrix" / "distill-journal"
        journal.mkdir(parents=True)
        refused = {"error_code": "invalid_snapshot_request", "updated_at": "2026-10-14T02:40:00Z"}
        outside = self.base / "outside.json"
        outside.write_text(json.dumps(refused))
        os.mkfifo(journal / "fifo.json")
        (journal / "link-to-file.json").symlink_to(outside)
        (journal / "link-to-fifo.json").symlink_to(journal / "fifo.json")
        (journal / "dangling.json").symlink_to(self.base / "missing.json")
        (journal / "dir.json").mkdir()
        with self.deadline():
            row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("recent=0 (7d, scanned=0)", row.status)
        self.assertNotIn("unreadable", row.status)

        self._job(journal, "real", refused)
        with self.deadline():
            row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertEqual(row.klass, "경고")
        self.assertIn("invalid_snapshot_request=1 in 7d", row.status)
        self.assertNotIn("unreadable", row.status)

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(signal, "SIGALRM"), "needs mkfifo and SIGALRM")
    def test_entry_swapped_to_fifo_or_symlink_after_scan_is_skipped(self) -> None:
        from ccc_doctor import _read_regular_text

        fifo = self.base / "swapped.json"
        os.mkfifo(fifo)
        link = self.base / "link.json"
        link.symlink_to(self.base / "target.json")
        (self.base / "target.json").write_text("{}")
        with self.deadline():
            self.assertIsNone(_read_regular_text(fifo))
            self.assertIsNone(_read_regular_text(link))
        self.assertEqual(_read_regular_text(self.base / "target.json"), "{}")

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "needs /proc/self/fd to count descriptors")
    def test_entry_swapped_to_directory_or_socket_is_skipped_without_fd_leak(self) -> None:
        import socket

        from ccc_doctor import _read_regular_text

        directory = self.base / "dir.json"
        directory.mkdir()
        sock_path = self.base / "sock.json"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(sock_path))
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            self.assertIsNone(_read_regular_text(directory))
            self.assertIsNone(_read_regular_text(sock_path))
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_only_other_errors_is_ok(self) -> None:
        journal = self.base / ".ccc-matrix" / "distill-journal"
        self._job(journal, "a", {"error_code": "snapshot_read_failed"})
        self._job(journal, "b", {"status": "extraction_done", "error_code": None})
        row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("scanned=2", row.status)


    # --- #2316: zero-byte "successes" ----------------------------------------

    @staticmethod
    def _done(status: str, sizes: list[int] | None, *, byte_count: int | None = None, when: str = "2026-10-11T19:45:00Z") -> dict:
        job: dict = {"status": status, "error_code": None, "updated_at": when}
        if sizes is not None:
            job["extraction_accounting"] = [
                {"model": "provider-default", "snapshot_bytes": size, "duration_ms": 1, "estimated_max_tokens": 1}
                for size in sizes
            ]
        if byte_count is not None:
            job["snapshot"] = {"messages": [], "byte_count": byte_count}
        return job

    def test_no_journals_reports_zero_empty_snapshots(self) -> None:
        row = self.row(self.run_checks(), EMPTY_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("recent=0 (7d, scanned=0)", row.status)

    def test_zero_byte_successes_warn_per_journal_and_body_free(self) -> None:
        matrix = self.base / ".ccc-matrix" / "distill-journal"
        telegram = self.base / ".telegram_bot" / "distill-journal"
        self._job(matrix, "a", self._done("extraction_done", [0], when="2026-10-11T11:48:00Z"))
        self._job(matrix, "b", self._done("extraction_done", [0, 0], when="2026-10-11T19:45:00Z"))
        self._job(telegram, "c", self._done("snapshot_done", None, byte_count=0, when="2026-10-10T04:17:00Z"))
        # Not empty successes: real bytes, a mixed retry, a failure, an old job.
        self._job(matrix, "d", self._done("extraction_done", [512]))
        self._job(matrix, "e", self._done("extraction_done", [0, 512]))
        self._job(matrix, "f", {**self._done("terminal_failed", [0]), "error_code": "max_attempts_exceeded"})
        self._job(telegram, "g", self._done("extraction_done", [0]), age_days=8)
        self._job(telegram, "h", self._done("extraction_done", None, byte_count=2048))
        doctor = self.run_checks()
        row = self.row(doctor, EMPTY_ITEM)
        self.assertEqual(row.klass, "경고")
        self.assertIn("snapshot_bytes=0 successes=3 in 7d", row.status)
        self.assertIn("(latest 2026-10-11T19:45, ccc-matrix,telegram_bot)", row.status)
        self.assertIn("explicit approval", row.action)
        self.assertNotIn("provider-default", row.status + row.action)
        # The refusal row is independent and unaffected.
        self.assertEqual(self.row(doctor, REFUSAL_ITEM).klass, "정상")

    def test_danso_zero_byte_success_is_labelled_danso(self) -> None:
        self._job(self.base / ".ccc-matrix" / "danso-distill-journal", "a", self._done("extraction_done", [0]))
        row = self.row(self.run_checks(), EMPTY_ITEM)
        self.assertIn("successes=1", row.status)
        self.assertIn(", ccc-matrix/danso)", row.status)

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(signal, "SIGALRM"), "needs mkfifo and SIGALRM")
    def test_empty_snapshot_scan_skips_fifo_and_symlink_entries(self) -> None:
        journal = self.base / ".ccc-matrix" / "distill-journal"
        journal.mkdir(parents=True)
        outside = self.base / "outside.json"
        outside.write_text(json.dumps(self._done("extraction_done", [0])))
        os.mkfifo(journal / "fifo.json")
        (journal / "link.json").symlink_to(outside)
        with self.deadline():
            row = self.row(self.run_checks(), EMPTY_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("recent=0 (7d, scanned=0)", row.status)

    def test_unreadable_journal_is_not_reported_as_zero_empty_snapshots(self) -> None:
        journal = self.base / ".ccc-matrix" / "distill-journal"
        self._job(journal, "a", self._done("extraction_done", [0]))
        journal.chmod(0)
        try:
            if os.access(journal, os.R_OK):
                self.skipTest("running with read access to mode-0 dirs (root)")
            row = self.row(self.run_checks(), EMPTY_ITEM)
        finally:
            journal.chmod(0o700)
        self.assertEqual(row.klass, "경고")
        self.assertIn("unreadable=1", row.status)

if __name__ == "__main__":
    unittest.main()
