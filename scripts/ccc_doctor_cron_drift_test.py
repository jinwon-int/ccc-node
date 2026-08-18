#!/usr/bin/env python3
"""Hermetic tests for doctor's installer-cron gen-stamp drift check (#1081).

Fixtures: a temp repo carrying the REAL scripts/lib/installer-gen-stamp.sh
(copied from this checkout, so the doctor-side comparison exercises the same
helper apply-time stamping uses) plus fake installer scripts with known
content, and a stub crontab honoring `-l` backed by a temp file — the same
seam (CCC_CRONTAB_CMD) the installer tests use. No real crontab is touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import INSTALLER_CRON_MARKERS, Doctor  # noqa: E402

REAL_LIB = Path(__file__).resolve().parent / "lib" / "installer-gen-stamp.sh"
INSTALLERS = tuple(INSTALLER_CRON_MARKERS.values())


class CronDriftTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.repo = tmp / "repo"
        (self.repo / "scripts" / "lib").mkdir(parents=True)
        (self.repo / ".claude").mkdir()
        (self.repo / "scripts" / "lib" / "installer-gen-stamp.sh").write_text(
            REAL_LIB.read_text()
        )
        for name in INSTALLERS:
            (self.repo / "scripts" / name).write_text(f"# fake {name} v1\n")
        self.cron_store = tmp / "cron.store"
        self.cron_store.write_text("")
        stub = tmp / "crontab"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'[ "${{1:-}}" = -l ] && {{ cat "{self.cron_store}"; exit 0; }}\n'
            "exit 0\n"
        )
        stub.chmod(0o755)
        self.env = patch.dict(os.environ, {"CCC_CRONTAB_CMD": str(stub)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def stamp_of(self, installer: str) -> str:
        out = subprocess.run(
            [
                "bash", "-c", '. "$1" && ccc_installer_gen_stamp "$2"', "_",
                str(self.repo / "scripts" / "lib" / "installer-gen-stamp.sh"),
                str(self.repo / "scripts" / installer),
            ],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        assert out.startswith("h_") and len(out) == 14, out
        return out

    def run_check(self, cron_text: str) -> Doctor:
        self.cron_store.write_text(cron_text)
        doctor = Doctor(self.repo, self.repo / ".claude", "settings")
        doctor.check_cron_drift()
        return doctor

    def row(self, doctor: Doctor, item: str):
        matches = [r for r in doctor.rows if r.item == item]
        self.assertEqual(len(matches), 1, f"{item}: {doctor.rows}")
        return matches[0]

    def test_empty_crontab_is_normal_opt_in(self) -> None:
        doctor = self.run_check("")
        row = self.row(doctor, "installer cron entries")
        self.assertEqual((row.klass, row.action), ("정상", "none"))
        self.assertIn("none installed", row.status)

    def test_matching_stamp_is_normal(self) -> None:
        gen = self.stamp_of("install-memory-refresh-cron.sh")
        doctor = self.run_check(
            f"*/30 * * * * bash -lc 'x' >> /log 2>&1  # ccc-node:memory-refresh gen={gen}\n"
        )
        row = self.row(doctor, "cron entry ccc-node:memory-refresh")
        self.assertEqual(row.klass, "정상")
        self.assertIn(gen, row.status)
        self.assertEqual(row.action, "none")

    def test_stale_stamp_is_warning_with_reapply_action(self) -> None:
        gen = self.stamp_of("install-pr-status-poll-cron.sh")
        stale = "h_000000000000"
        self.assertNotEqual(gen, stale)
        doctor = self.run_check(
            f"*/15 * * * * bash -lc 'x' >> /log 2>&1  # ccc-node:pr-status-poll gen={stale}\n"
        )
        row = self.row(doctor, "cron entry ccc-node:pr-status-poll")
        self.assertEqual(row.klass, "경고")
        self.assertIn("gen drift", row.status)
        self.assertIn("scripts/install-pr-status-poll-cron.sh --apply", row.action)
        self.assertIn(stale, row.action)
        self.assertIn(gen, row.action)

    def test_unstamped_entry_is_warning(self) -> None:
        doctor = self.run_check(
            "*/30 * * * * bash -lc 'x' >> /log 2>&1  # ccc-node:memory-refresh\n"
        )
        row = self.row(doctor, "cron entry ccc-node:memory-refresh")
        self.assertEqual(row.klass, "경고")
        self.assertIn("unstamped pre-#1081", row.status)
        self.assertIn("scripts/install-memory-refresh-cron.sh --apply", row.action)

    def test_nunchi_multi_line_marker_aggregates_to_one_row(self) -> None:
        gen = self.stamp_of("install-nunchi.sh")
        cron = "".join(
            f"{sched} bash /h/{name}.sh >> /log 2>&1 # nunchi:#816 gen={gen}\n"
            for sched, name in (
                ("*/10 * * * *", "piri-feed"),
                ("17 * * * *", "mempalace-refresh"),
                ("7 8 * * 1", "bench"),
            )
        )
        doctor = self.run_check(cron)
        row = self.row(doctor, "cron entry nunchi:#816")
        self.assertEqual(row.klass, "정상")
        self.assertIn("3 line(s)", row.status)

    def test_mixed_unstamped_lines_report_unstamped_count(self) -> None:
        gen = self.stamp_of("install-nunchi.sh")
        doctor = self.run_check(
            f"*/10 * * * * bash /h/feed.sh >> /log 2>&1 # nunchi:#816 gen={gen}\n"
            "17 * * * * bash /h/refresh.sh >> /log 2>&1 # nunchi:#816\n"
        )
        row = self.row(doctor, "cron entry nunchi:#816")
        self.assertEqual(row.klass, "경고")
        self.assertIn("1/2 line(s)", row.status)

    def test_ownerless_marker_is_informational_warning(self) -> None:
        doctor = self.run_check(
            "0 4 * * * bash /root/ccc-node/scripts/ccc-self-update.sh # ccc-node:self-update\n"
        )
        row = self.row(doctor, "cron entry ccc-node:self-update")
        self.assertEqual(row.klass, "경고")
        self.assertIn("no installer in repo", row.status)

    def test_autosave_block_markers_are_ignored(self) -> None:
        gen = self.stamp_of("install-skill-autosave-cron.sh")
        doctor = self.run_check(
            "# ccc-node:autosave-schedule:begin\n"
            "# every 6 hours\n"
            "# ccc-node:autosave-schedule:end\n"
            f"0 */6 * * * bash -lc 'x' >> /log 2>&1  # ccc-node:skill-autosave gen={gen}\n"
        )
        self.assertEqual(len(doctor.rows), 1, doctor.rows)
        self.assertEqual(doctor.rows[0].klass, "정상")

    def test_unrelated_cron_lines_do_not_report(self) -> None:
        doctor = self.run_check("0 3 * * * /usr/local/bin/certbot renew\n")
        row = self.row(doctor, "installer cron entries")
        self.assertEqual(row.klass, "정상")

    def test_missing_installer_in_checkout_degrades_readably(self) -> None:
        (self.repo / "scripts" / "install-memory-refresh-cron.sh").unlink()
        doctor = self.run_check(
            "*/30 * * * * bash -lc 'x' >> /log 2>&1  # ccc-node:memory-refresh gen=h_1234567890ab\n"
        )
        row = self.row(doctor, "cron entry ccc-node:memory-refresh")
        self.assertEqual(row.klass, "경고")
        self.assertIn("cannot recompute stamp", row.status)

    def test_crontab_command_missing_is_normal_opt_in(self) -> None:
        with patch.dict(os.environ, {"CCC_CRONTAB_CMD": "/no/such/crontab"}):
            doctor = Doctor(self.repo, self.repo / ".claude", "settings")
            doctor.check_cron_drift()
        row = self.row(doctor, "installer cron entries")
        self.assertEqual(row.klass, "정상")

    def test_warnings_never_flip_the_exit_code(self) -> None:
        doctor = self.run_check(
            "*/30 * * * * bash -lc 'x' >> /log 2>&1  # ccc-node:memory-refresh gen=h_000000000000\n"
            "0 4 * * * bash x.sh # ccc-node:self-update\n"
        )
        self.assertEqual(doctor.counts["교정가능"], 0)
        self.assertEqual(doctor.counts["수동필요"], 0)
        self.assertEqual(doctor.report_exit_code(), 0)
        self.assertGreaterEqual(doctor.counts["경고"], 2)


if __name__ == "__main__":
    unittest.main()
