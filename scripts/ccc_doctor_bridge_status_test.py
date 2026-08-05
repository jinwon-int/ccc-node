#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's bridge-status parser."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402


class BridgeStatusVerdictTest(unittest.TestCase):
    def test_available_is_normal(self) -> None:
        klass, status = Doctor.bridge_status_verdict(
            0, "🟢 Bot status: available\n   Telegram: healthy\n"
        )
        self.assertEqual((klass, status), ("정상", "available"))

    def test_degraded_is_not_normal_readable(self) -> None:
        klass, status = Doctor.bridge_status_verdict(
            0, "🟡 Bot status: degraded\n   Telegram: degraded (health stale)\n"
        )
        self.assertEqual((klass, status), ("경고", "degraded"))

    def test_unavailable_is_not_normal_readable(self) -> None:
        klass, status = Doctor.bridge_status_verdict(
            0, "🔴 Bot status: unavailable\n   Process: dead\n"
        )
        self.assertEqual((klass, status), ("경고", "unavailable"))

    def test_nonzero_or_unrecognized_output_is_warning(self) -> None:
        self.assertEqual(Doctor.bridge_status_verdict(3, "some output")[0], "경고")
        self.assertEqual(Doctor.bridge_status_verdict(0, "some output")[0], "경고")

    def test_provider_parser_identifies_one_live_piri_lane(self) -> None:
        output = "🟢 Bot status: available\n   Telegram: healthy\n   Piri: healthy\n"
        self.assertEqual(Doctor.bridge_status_provider(output), ("piri", "healthy"))

    def test_provider_parser_rejects_ambiguous_or_missing_labels(self) -> None:
        self.assertIsNone(Doctor.bridge_status_provider("Bot status: available"))
        self.assertIsNone(
            Doctor.bridge_status_provider("Codex: healthy\nPiri: healthy\n")
        )

    def test_healthy_live_piri_status_is_ready(self) -> None:
        doctor = Doctor(Path.cwd(), Path.cwd() / ".claude", "settings")
        doctor.provider = "piri"
        doctor._bridge_provider_state = ("piri", "healthy")

        doctor.check_provider_readiness()

        self.assertEqual(doctor.readiness, "ready")
        self.assertEqual(doctor.rows[-1].item, "Piri runtime")
        self.assertEqual(doctor.rows[-1].klass, "정상")

    def test_distill_auto_follows_live_piri_runtime(self) -> None:
        doctor = Doctor(Path.cwd(), Path.cwd() / ".claude", "settings")
        doctor.provider = "piri"
        doctor._bridge_provider_state = ("piri", "healthy")
        with patch.dict("os.environ", {"CCC_MEMORY_DISTILL_PROVIDER": "auto"}), patch(
            "ccc_doctor.shutil.which", return_value="/bin/true"
        ):
            doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "ready")
        self.assertIn("effective=piri", doctor.rows[-1].status)

    def test_cross_runtime_override_requires_separate_live_auth_proof(self) -> None:
        doctor = Doctor(Path.cwd(), Path.cwd() / ".claude", "settings")
        doctor.provider = "claude"
        doctor._bridge_provider_state = ("claude", "healthy")
        with patch.dict(
            "os.environ", {"CCC_MEMORY_DISTILL_PROVIDER": "piri"}
        ), patch("ccc_doctor.shutil.which", return_value="/bin/true"):
            doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "static-ready")
        self.assertIn("live auth unproven", doctor.rows[-1].status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
