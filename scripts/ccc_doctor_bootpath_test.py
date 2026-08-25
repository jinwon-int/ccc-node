#!/usr/bin/env python3
"""Contract tests for the doctor's bridge boot-path check (#55 follow-up).

The failure this guards against is silent: the bridge runs healthy from one
checkout while the unit that would restart it points at a stale twin, so the
node looks fine until a reboot serves months-old code. Observed on yukson
2026-07-27 (enabled unit aimed at a checkout 111 commits behind).

Hermetic: no systemd, no ps, no network — the process/unit inputs are injected.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402


def make_doctor() -> Doctor:
    root = Path(tempfile.mkdtemp())
    return Doctor(repo=root, claude_dir=root / ".claude", scope="all")


class CheckoutRootParsingTest(unittest.TestCase):
    """Both deployed ExecStart shapes must resolve to the same checkout root."""

    def test_start_sh_shape(self) -> None:
        cmd = "/bin/bash /root/ccc-node/bridge/start.sh --path /root"
        self.assertEqual(Doctor.checkout_root_of(cmd), "/root/ccc-node")

    def test_venv_python_shape(self) -> None:
        # soonwook's unit uses this form; a start.sh-only parser reports a bogus
        # mismatch on it, which is how a false positive reached the operator.
        cmd = "/opt/ccc-node/bridge/venv/bin/python -m telegram_bot --path /root"
        self.assertEqual(Doctor.checkout_root_of(cmd), "/opt/ccc-node")

    def test_user_home_checkout(self) -> None:
        cmd = "/home/gongmyoung/ccc-node/bridge/venv/bin/python -m telegram_bot --path /home/gongmyoung"
        self.assertEqual(Doctor.checkout_root_of(cmd), "/home/gongmyoung/ccc-node")

    def test_unrelated_command_has_no_root(self) -> None:
        self.assertIsNone(Doctor.checkout_root_of("/usr/bin/sshd -D"))

    def test_relative_marker_is_not_a_root(self) -> None:
        # A leading "/bridge/..." has no checkout prefix; must not return "".
        self.assertIsNone(Doctor.checkout_root_of("/bridge/start.sh --path /root"))


class UnitRootParsingTest(unittest.TestCase):
    def _unit(self, body: str) -> Path:
        path = Path(tempfile.mkdtemp()) / "ccc-telegram-bridge.service"
        path.write_text(body)
        return path

    def test_reads_execstart(self) -> None:
        doctor = make_doctor()
        unit = self._unit(
            "[Service]\nWorkingDirectory=/opt/ccc-node\n"
            "ExecStart=/bin/bash /opt/ccc-node/bridge/start.sh --path /root\n"
        )
        self.assertEqual(doctor.unit_bridge_root(unit), "/opt/ccc-node")

    def test_missing_unit_is_none(self) -> None:
        doctor = make_doctor()
        self.assertIsNone(doctor.unit_bridge_root(Path("/nonexistent/unit.service")))

    def test_unit_without_execstart_is_none(self) -> None:
        doctor = make_doctor()
        self.assertIsNone(doctor.unit_bridge_root(self._unit("[Service]\nType=simple\n")))


class BootPathVerdictTest(unittest.TestCase):
    """The verdict itself, with the live lookups stubbed out."""

    def _run(self, running: str | None, unit_root: str | None) -> Doctor:
        doctor = make_doctor()
        doctor.running_bridge_root = lambda: running  # type: ignore[method-assign]
        doctor.unit_bridge_root = lambda _unit: unit_root  # type: ignore[method-assign]
        # Hermeticity: check_bridge_boot_path branches on the HOST's systemd
        # before touching the stubbed lookups — without this stub the suite
        # leaked into the Termux branch and false-failed on any systemd-less
        # machine (containers, WSL).
        doctor.has_systemd = lambda: True  # type: ignore[method-assign]
        doctor.check_bridge_boot_path()
        return doctor

    def _row(self, doctor: Doctor):
        rows = [r for r in doctor.rows if r.item == "bridge boot path"]
        self.assertEqual(len(rows), 1, "expected exactly one boot-path row")
        return rows[0]

    def test_mismatch_is_manual_action(self) -> None:
        doctor = self._run(running="/root/ccc-node", unit_root="/opt/ccc-node")
        row = self._row(doctor)
        self.assertEqual(row.klass, "수동필요")
        self.assertIn("/opt/ccc-node", row.status)
        self.assertIn("/root/ccc-node", row.status)

    def test_agreement_is_clean(self) -> None:
        row = self._row(self._run(running="/opt/ccc-node", unit_root="/opt/ccc-node"))
        self.assertEqual(row.klass, "정상")

    def test_no_unit_declares_the_running_bridge(self) -> None:
        # Nothing would bring the bridge back after a reboot — worth a warning,
        # but it is not the stale-code hazard, so it must not read as 수동필요.
        row = self._row(self._run(running="/root/ccc-node", unit_root=None))
        self.assertEqual(row.klass, "경고")

    def test_node_without_a_bridge_is_not_flagged(self) -> None:
        row = self._row(self._run(running=None, unit_root="/opt/ccc-node"))
        self.assertEqual(row.klass, "정상")


if __name__ == "__main__":
    unittest.main(verbosity=2)
