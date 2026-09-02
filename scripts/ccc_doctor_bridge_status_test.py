#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's bridge-status parser."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import USAGE_BUDGET_TOKENS_DEFAULT, Doctor  # noqa: E402


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
        with patch.dict("os.environ", {"CCC_MEMORY_DISTILL_PROVIDER": "auto", "CCC_MEMORY_DISTILL_ALLOW_UNBOUNDED": "1"}), patch(
            "ccc_doctor.shutil.which", return_value="/bin/true"
        ):
            doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "ready")
        self.assertIn("effective=piri", doctor.rows[-1].status)

    def test_piri_distill_path_falls_back_to_matching_bridge_unit(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "ccc-node"
            root.mkdir()
            executable = Path(temp) / "ccc-piri"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            unit = Path(temp) / "ccc-telegram-bridge.service"
            unit.write_text(
                "[Service]\n"
                f'Environment="CCC_PIRI_CLI_PATH={executable}"\n'
                f"ExecStart=/bin/bash {root}/bridge/start.sh --path {temp}\n",
                encoding="utf-8",
            )
            stale_unit = Path(temp) / "stale-bridge.service"
            stale_unit.write_text(
                "[Service]\n"
                "Environment=CCC_PIRI_CLI_PATH=/missing/stale/ccc-piri\n"
                f"ExecStart=/bin/bash {temp}/stale/bridge/start.sh --path {temp}\n",
                encoding="utf-8",
            )
            doctor = Doctor(root, Path(temp) / ".claude", "settings")
            doctor.provider = "piri"
            doctor._bridge_provider_state = ("piri", "healthy")
            with patch.dict(
                "os.environ", {"CCC_MEMORY_DISTILL_PROVIDER": "auto", "CCC_MEMORY_DISTILL_ALLOW_UNBOUNDED": "1"}, clear=True
            ), patch.object(
                doctor, "bridge_systemd_units", return_value=[stale_unit, unit]
            ), patch.object(
                doctor, "running_bridge_root", return_value=str(root)
            ):
                doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "ready")
        self.assertIn("executable=available", doctor.rows[-1].status)
        self.assertNotIn(str(executable), doctor.rows[-1].status)

    def test_codex_distill_check_does_not_read_bridge_unit_environment(self) -> None:
        doctor = Doctor(Path.cwd(), Path.cwd() / ".claude", "settings")
        doctor.provider = "codex"
        doctor._bridge_provider_state = ("codex", "healthy")
        with patch.dict(
            "os.environ", {"CCC_MEMORY_DISTILL_PROVIDER": "auto", "CCC_MEMORY_DISTILL_ALLOW_UNBOUNDED": "1"}, clear=True
        ), patch(
            "ccc_doctor.shutil.which", return_value=None
        ), patch.object(
            doctor,
            "bridge_unit_environment_value",
            # The budget lookup may consult the unit (#1318), but the CLI
            # path must never: the Codex readiness check runs live probes
            # and its service wrapper is not a probe-safe binary.
            side_effect=lambda key: (
                AssertionError("Codex path must not come from the bridge unit")
                if key == "CCC_PIRI_CLI_PATH"
                else None
            ),
        ):
            doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "failed")
        self.assertIn("effective=codex", doctor.rows[-1].status)

    def test_cross_runtime_override_requires_separate_live_auth_proof(self) -> None:
        doctor = Doctor(Path.cwd(), Path.cwd() / ".claude", "settings")
        doctor.provider = "claude"
        doctor._bridge_provider_state = ("claude", "healthy")
        with patch.dict(
            "os.environ", {"CCC_MEMORY_DISTILL_PROVIDER": "piri", "CCC_MEMORY_DISTILL_ALLOW_UNBOUNDED": "1"}
        ), patch("ccc_doctor.shutil.which", return_value="/bin/true"):
            doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "static-ready")
        self.assertIn("live auth unproven", doctor.rows[-1].status)

    def test_budget_falls_back_to_matching_bridge_unit_dropin(self) -> None:
        """Clean-shell sweeps read the drop-in budget, not just os.environ (#1318)."""

        with TemporaryDirectory() as temp:
            root = Path(temp) / "ccc-node"
            root.mkdir()
            executable = Path(temp) / "ccc-piri"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            unit = Path(temp) / "ccc-telegram-bridge.service"
            unit.write_text(
                "[Service]\n"
                f"ExecStart=/bin/bash {root}/bridge/start.sh --path {temp}\n",
                encoding="utf-8",
            )
            dropin_dir = unit.parent / f"{unit.name}.d"
            dropin_dir.mkdir()
            dropin = dropin_dir / "memory-budget.conf"
            dropin.write_text(
                "[Service]\n"
                "Environment=CCC_USAGE_BUDGET_TOKENS_PIRI=4000000\n",
                encoding="utf-8",
            )
            stale_unit = Path(temp) / "stale-bridge.service"
            stale_unit.write_text(
                "[Service]\n"
                "Environment=CCC_USAGE_BUDGET_TOKENS_PIRI=1\n"
                f"ExecStart=/bin/bash {temp}/stale/bridge/start.sh --path {temp}\n",
                encoding="utf-8",
            )
            doctor = Doctor(root, Path(temp) / ".claude", "settings")
            doctor.provider = "piri"
            doctor._bridge_provider_state = ("piri", "healthy")
            with patch.dict(
                "os.environ",
                {"CCC_MEMORY_DISTILL_PROVIDER": "auto"},
                clear=True,
            ), patch.object(
                doctor,
                "bridge_systemd_units",
                return_value=[stale_unit, unit],
            ), patch.object(
                doctor, "running_bridge_root", return_value=str(root)
            ), patch(
                "ccc_doctor.shutil.which", return_value=str(executable)
            ):
                doctor.check_distill_readiness()

        # The drop-in budget resolves: fail-closed must NOT fire. The stale
        # twin unit's budget=1 is ignored (ExecStart root mismatch).
        self.assertEqual(doctor.distill_readiness, "ready")
        self.assertNotIn("fail-closed", doctor.rows[-1].status)

    def test_budget_fallback_keeps_fail_closed_without_any_unit(self) -> None:
        """No unit, explicit zero budget: the fail-closed warning stays hermetic (#1318)."""

        doctor = Doctor(Path.cwd(), Path.cwd() / ".claude", "settings")
        doctor.provider = "codex"
        doctor._bridge_provider_state = ("codex", "healthy")
        with patch.dict(
            "os.environ",
            {"CCC_MEMORY_DISTILL_PROVIDER": "auto", "CCC_USAGE_BUDGET_TOKENS_CODEX": "0"},
            clear=True,
        ), patch.object(doctor, "bridge_systemd_units", return_value=[]):
            doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "blocked")
        self.assertIn("fail-closed", doctor.rows[-1].status)

    def test_piri_launcher_fallback_when_no_unit_exposes_the_path(self) -> None:
        """Termux: no unit, no env — the harness launcher hooks/ccc-piri counts as the executable."""

        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            (claude_dir / "hooks").mkdir(parents=True)
            launcher = claude_dir / "hooks" / "ccc-piri"
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o700)
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            doctor.provider = "piri"
            doctor._bridge_provider_state = ("piri", "healthy")
            with patch.dict(
                "os.environ",
                {"CCC_MEMORY_DISTILL_PROVIDER": "auto"},
                clear=True,
            ), patch.object(doctor, "bridge_systemd_units", return_value=[]), patch(
                "ccc_doctor.shutil.which", return_value=None
            ):
                doctor.check_distill_readiness()

        self.assertNotEqual(doctor.distill_readiness, "failed")
        self.assertNotIn("executable=missing", doctor.rows[-1].status)

    def test_piri_without_launcher_or_unit_is_still_missing(self) -> None:
        """The fallback only helps when the launcher file really exists."""

        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            (claude_dir / "hooks").mkdir(parents=True)
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            doctor.provider = "piri"
            doctor._bridge_provider_state = ("piri", "healthy")
            with patch.dict(
                "os.environ",
                {"CCC_MEMORY_DISTILL_PROVIDER": "auto"},
                clear=True,
            ), patch.object(doctor, "bridge_systemd_units", return_value=[]), patch(
                "ccc_doctor.shutil.which", return_value=None
            ):
                doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "failed")
        self.assertIn("executable=missing", doctor.rows[-1].status)

    def test_unset_budget_uses_the_fleet_default_and_is_ready(self) -> None:
        """No env, no unit: doctor judges the same 2M default the bridge runs with."""

        with TemporaryDirectory() as temp:
            # Hermetic against the host: CI runners have no provider CLI, so the
            # executable probe is stubbed exactly like the drop-in test above.
            executable = Path(temp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            doctor = Doctor(Path.cwd(), Path.cwd() / ".claude", "settings")
            doctor.provider = "codex"
            doctor._bridge_provider_state = ("codex", "healthy")
            with patch.dict(
                "os.environ",
                {"CCC_MEMORY_DISTILL_PROVIDER": "auto"},
                clear=True,
            ), patch.object(doctor, "bridge_systemd_units", return_value=[]), patch(
                "ccc_doctor.shutil.which", return_value=str(executable)
            ):
                doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "ready")
        self.assertNotIn("fail-closed", doctor.rows[-1].status)
        self.assertGreater(USAGE_BUDGET_TOKENS_DEFAULT, 0)

    def test_distill_without_finite_budget_is_safely_blocked(self) -> None:
        doctor = Doctor(Path.cwd(), Path.cwd() / ".claude", "settings")
        doctor.provider = "codex"
        doctor._bridge_provider_state = ("codex", "healthy")
        with patch.dict(
            "os.environ",
            # The fleet default is finite now; "without finite budget" must be
            # spelled out as an explicit 0.
            {"CCC_MEMORY_DISTILL_PROVIDER": "auto", "CCC_USAGE_BUDGET_TOKENS_CODEX": "0"},
            clear=True,
        ), patch.object(
            # Hermetic against the host: a node whose own unit drop-in sets
            # this budget must not flip the expectation (#1318 follow-up).
            doctor,
            "bridge_unit_environment_value",
            return_value=None,
        ):
            doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "blocked")
        self.assertIn("fail-closed", doctor.rows[-1].status)

    def test_global_distill_marker_wins_before_provider_probe(self) -> None:
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            state = claude_dir / "state"
            state.mkdir(parents=True)
            (state / "distill.disabled").touch()
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            doctor.provider = "piri"
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_distill_readiness()

        self.assertEqual(doctor.distill_readiness, "disabled")
        self.assertIn("global off-switch active", doctor.rows[-1].status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
