#!/usr/bin/env python3
"""Transaction and boundary tests for install-auto-distill.sh."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/install-auto-distill.sh"
SOURCE = ROOT / "scripts/auto-distill"
FILES = ("auto-distill.py", "metrics.py", "model_command.py", "evaluation-receipt.json")
MODES = {
    "auto-distill.py": 0o700,
    "metrics.py": 0o700,
    "model_command.py": 0o700,
    "evaluation-receipt.json": 0o600,
}


class AutoDistillInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_installer(
        self,
        action: str,
        *,
        environment_updates: dict[str, str] | None = None,
        installer: Path = INSTALLER,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["CCC_AUTO_DISTILL_TARGET_HOME"] = str(self.home)
        environment.update(environment_updates or {})
        return subprocess.run(
            ["bash", str(installer), action],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
            check=False,
        )

    @property
    def destination(self) -> Path:
        return self.home / ".hermes/auto-distill"

    def copy_installer_tree(self) -> tuple[Path, Path]:
        scripts = Path(self.temp.name) / "fixture-scripts"
        scripts.mkdir()
        fixture_installer = scripts / INSTALLER.name
        fixture_verifier = scripts / "verify-auto-distill-receipt.py"
        shutil.copy2(INSTALLER, fixture_installer)
        shutil.copy2(ROOT / "scripts/verify-auto-distill-receipt.py", fixture_verifier)
        fixture_source = scripts / "auto-distill"
        shutil.copytree(SOURCE, fixture_source)
        return fixture_installer, fixture_source

    def test_preview_is_non_mutating(self) -> None:
        result = self.run_installer("--preview")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cron/services: unchanged", result.stdout)
        self.assertFalse(self.destination.exists())

    def test_apply_installs_exact_owner_only_sources(self) -> None:
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in FILES:
            installed = self.destination / name
            self.assertEqual(installed.read_bytes(), (SOURCE / name).read_bytes())
            self.assertEqual(installed.stat().st_mode & 0o777, MODES[name])
        self.assertIn("cron/services: unchanged", result.stdout)
        self.assertIn("evaluation receipt ok", result.stdout)

    def test_reapply_is_idempotent_without_backup(self) -> None:
        self.assertEqual(self.run_installer("--apply").returncode, 0)
        second = self.run_installer("--apply")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already current", second.stdout)
        backup_root = self.home / ".hermes/backups/auto-distill"
        self.assertFalse(backup_root.exists())

    def test_changed_file_is_backed_up_before_replacement(self) -> None:
        self.assertEqual(self.run_installer("--apply").returncode, 0)
        target = self.destination / "auto-distill.py"
        target.write_text("operator-local-old-copy\n", encoding="utf-8")
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list(
            (self.home / ".hermes/backups/auto-distill").glob("*/auto-distill.py")
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "operator-local-old-copy\n")
        self.assertEqual(target.read_bytes(), (SOURCE / "auto-distill.py").read_bytes())

    def test_second_replacement_failure_rolls_back_first_file(self) -> None:
        self.assertEqual(self.run_installer("--apply").returncode, 0)
        first = self.destination / "auto-distill.py"
        second = self.destination / "metrics.py"
        first.write_text("old-first\n", encoding="utf-8")
        second.write_text("old-second\n", encoding="utf-8")

        stub_bin = Path(self.temp.name) / "stub-bin"
        stub_bin.mkdir()
        move_count = Path(self.temp.name) / "move-count"
        move_stub = stub_bin / "mv"
        move_stub.write_text(
            """#!/usr/bin/env bash
set -eu
count=0
[ ! -f "$CCC_TEST_MOVE_COUNT" ] || count="$(cat "$CCC_TEST_MOVE_COUNT")"
count=$((count + 1))
printf '%s\n' "$count" > "$CCC_TEST_MOVE_COUNT"
[ "$count" -lt 2 ] || exit 55
exec /bin/mv "$@"
""",
            encoding="utf-8",
        )
        move_stub.chmod(0o700)
        result = self.run_installer(
            "--apply",
            environment_updates={
                "CCC_TEST_MOVE_COUNT": str(move_count),
                "PATH": f"{stub_bin}:{os.environ['PATH']}",
            },
        )

        self.assertEqual(result.returncode, 55, result.stdout + result.stderr)
        self.assertIn("install rolled back", result.stderr)
        self.assertEqual(first.read_text(), "old-first\n")
        self.assertEqual(second.read_text(), "old-second\n")

    def test_check_reports_exact_and_drifted_state(self) -> None:
        self.assertEqual(self.run_installer("--apply").returncode, 0)
        exact = self.run_installer("--check")
        self.assertEqual(exact.returncode, 0, exact.stdout + exact.stderr)
        (self.destination / "metrics.py").write_text("drift\n", encoding="utf-8")
        drifted = self.run_installer("--check")
        self.assertEqual(drifted.returncode, 1)
        self.assertIn("drift metrics.py", drifted.stdout)

    def test_missing_receipt_fails_closed_before_preview_mutation(self) -> None:
        installer, source = self.copy_installer_tree()
        (source / "evaluation-receipt.json").unlink()
        result = self.run_installer("--preview", installer=installer)
        self.assertEqual(result.returncode, 3)
        self.assertIn("evaluation receipt invalid", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_stale_full_source_hash_fails_closed(self) -> None:
        installer, source = self.copy_installer_tree()
        with (source / "auto-distill.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# unreceipted source drift\n")
        result = self.run_installer("--apply", installer=installer)
        self.assertEqual(result.returncode, 3)
        self.assertIn("subject SHA-256", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_surface_drift_fails_even_when_full_hash_field_is_rewritten(self) -> None:
        installer, source = self.copy_installer_tree()
        target = source / "auto-distill.py"
        text = target.read_text(encoding="utf-8")
        target.write_text(
            text.replace("def _needle_score(text, needles):", "def _needle_score(text, needles):\n    # untested semantic surface"),
            encoding="utf-8",
        )
        receipt_path = source / "evaluation-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        changed_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        receipt["subject"]["sha256"] = changed_sha
        receipt["evaluation"]["evaluated_source_sha256"] = changed_sha
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        result = self.run_installer("--apply", installer=installer)
        self.assertEqual(result.returncode, 3)
        self.assertIn("surface SHA-256", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_failed_gate_receipt_is_rejected(self) -> None:
        installer, source = self.copy_installer_tree()
        receipt_path = source / "evaluation-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["evaluation"]["passed"] = False
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        result = self.run_installer("--apply", installer=installer)
        self.assertEqual(result.returncode, 3)
        self.assertIn("not signed as passed", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_invalid_receipt_does_not_touch_existing_target(self) -> None:
        installer, source = self.copy_installer_tree()
        self.destination.mkdir(parents=True, mode=0o700)
        existing = self.destination / "auto-distill.py"
        existing.write_text("preserve-existing-target\n", encoding="utf-8")
        receipt_path = source / "evaluation-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["evaluation"]["collateral_damage"] = 1
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        result = self.run_installer("--apply", installer=installer)

        self.assertEqual(result.returncode, 3)
        self.assertIn("collateral damage", result.stderr)
        self.assertEqual(existing.read_text(encoding="utf-8"), "preserve-existing-target\n")
        self.assertFalse((self.home / ".hermes/backups/auto-distill").exists())

    def test_symlinked_managed_receipt_is_rejected(self) -> None:
        installer, source = self.copy_installer_tree()
        receipt_path = source / "evaluation-receipt.json"
        referent = Path(self.temp.name) / "outside-receipt.json"
        receipt_path.replace(referent)
        receipt_path.symlink_to(referent)

        result = self.run_installer("--apply", installer=installer)

        self.assertEqual(result.returncode, 3)
        self.assertIn("missing or unsafe", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_symlink_target_is_rejected_without_touching_referent(self) -> None:
        self.destination.mkdir(parents=True, mode=0o700)
        referent = Path(self.temp.name) / "outside"
        referent.write_text("keep\n", encoding="utf-8")
        (self.destination / "metrics.py").symlink_to(referent)
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe target file", result.stderr)
        self.assertEqual(referent.read_text(), "keep\n")


if __name__ == "__main__":
    unittest.main()
