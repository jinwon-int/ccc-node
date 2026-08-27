#!/usr/bin/env python3
"""Hermetic tests for the typed side-effect and recovery contract (#872)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("ccc_side_effect_contract.py")
SPEC = importlib.util.spec_from_file_location("ccc_side_effect_contract", MODULE_PATH)
assert SPEC and SPEC.loader
CONTRACT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONTRACT
SPEC.loader.exec_module(CONTRACT)


def _operation() -> dict[str, object]:
    return {
        "operation": "fixture.deliver",
        "owner": "fixture worker",
        "registration": {
            "kind": "python-symbol",
            "path": "src/effect.py",
            "symbol": "deliver",
        },
        "external": True,
        "idempotency": "native",
        "idempotency_key": "fixture:<job-id>",
        "retry_class": "safe",
        "ambiguous_window": "success before ACK",
        "reconcile": "none",
        "compensation": "none",
        "audit_surface": "fixture ledger",
        "approval_boundary": "fixture policy",
        "recovery": {
            "before_intent": "safe-replay",
            "after_intent_before_call": "safe-replay",
            "after_external_success_before_ack": "safe-replay",
            "after_ack_before_terminal": "safe-replay",
            "duplicate_restart_replay": "safe-replay",
        },
    }


def _contract(operation: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "registration_roots": ["src"],
        "operations": [operation or _operation()],
    }


class SideEffectContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_dir = self.root / "src"
        self.source_dir.mkdir()
        self.source = self.source_dir / "effect.py"
        self.source.write_text(
            "# ccc-side-effect: fixture.deliver\n"
            "def deliver():\n"
            "    return None\n",
            encoding="utf-8",
        )
        self.contract_path = self.root / "contract.json"
        self.document_path = self.root / "contract.md"
        subprocess.run(
            ["git", "init", "-q"],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.track(self.source)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def track(self, *paths: Path) -> None:
        subprocess.run(
            [
                "git",
                "add",
                "--",
                *(path.relative_to(self.root).as_posix() for path in paths),
            ],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def write_contract(self, value: dict[str, object] | None = None) -> None:
        self.contract_path.write_text(json.dumps(value or _contract()), encoding="utf-8")
        parsed = CONTRACT.load_contract(self.contract_path)
        self.document_path.write_text(
            CONTRACT.generated_document_block(parsed) + "\n", encoding="utf-8"
        )

    def validate(self):  # type: ignore[no-untyped-def]
        return CONTRACT.validate(self.root, self.contract_path, self.document_path)

    def test_checked_in_production_matrix_has_twenty_deterministic_drills(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        contract = CONTRACT.load_contract(repo / "architecture/side-effect-contract-v1.json")
        observations = CONTRACT.run_recovery_drills(contract)
        self.assertEqual(
            [item.operation for item in contract.operations],
            [
                "telegram.send_text",
                "honcho.deliver_distill",
                "self_update.apply",
                "agent_cron.spool_notify",
                "external_wait.wake_resume",
                "skill_autosave.sweep",
            ],
        )
        self.assertEqual(len(observations), 30)
        by_op = {
            operation.operation: {
                item.boundary: item for item in observations if item.operation == operation.operation
            }
            for operation in contract.operations
        }
        telegram_ambiguous = by_op["telegram.send_text"][
            CONTRACT.RecoveryBoundary.AFTER_EXTERNAL_SUCCESS_BEFORE_ACK
        ]
        self.assertEqual(telegram_ambiguous.action.value, "manual-review")
        self.assertEqual(telegram_ambiguous.attempts, 1)
        self.assertFalse(telegram_ambiguous.ack_recorded)
        honcho_dup = by_op["honcho.deliver_distill"][
            CONTRACT.RecoveryBoundary.DUPLICATE_RESTART_REPLAY
        ]
        self.assertEqual(honcho_dup.action.value, "safe-replay")
        self.assertEqual(honcho_dup.attempts, 2)
        self.assertEqual(honcho_dup.unique_effects, 1)
        cron_ambiguous = by_op["agent_cron.spool_notify"][
            CONTRACT.RecoveryBoundary.AFTER_EXTERNAL_SUCCESS_BEFORE_ACK
        ]
        self.assertEqual(cron_ambiguous.action.value, "reconcile")
        self.assertEqual(cron_ambiguous.attempts, 1)
        self.assertTrue(cron_ambiguous.ack_recorded)
        wake_ambiguous = by_op["external_wait.wake_resume"][
            CONTRACT.RecoveryBoundary.AFTER_EXTERNAL_SUCCESS_BEFORE_ACK
        ]
        self.assertEqual(wake_ambiguous.action.value, "reconcile")
        self.assertEqual(wake_ambiguous.attempts, 1)
        self.assertTrue(wake_ambiguous.ack_recorded)
        autosave_dup = by_op["skill_autosave.sweep"][
            CONTRACT.RecoveryBoundary.DUPLICATE_RESTART_REPLAY
        ]
        self.assertEqual(autosave_dup.action.value, "manual-review")
        self.assertEqual(autosave_dup.attempts, 1)
        self.assertEqual(autosave_dup.unique_effects, 1)
        self.assertFalse(autosave_dup.ack_recorded)

    def test_compensate_fixture_undoes_the_unacked_effect(self) -> None:
        operation = _operation()
        operation["idempotency"] = "none"
        operation["idempotency_key"] = "none"
        operation["retry_class"] = "conditional"
        operation["reconcile"] = "none"
        operation["compensation"] = "delete"
        recovery = operation["recovery"]
        assert isinstance(recovery, dict)
        recovery["after_external_success_before_ack"] = "compensate"
        recovery["duplicate_restart_replay"] = "compensate"
        self.contract_path.write_text(json.dumps(_contract(operation)), encoding="utf-8")
        contract = CONTRACT.load_contract(self.contract_path)
        ambiguous = CONTRACT.run_recovery_drills(contract)[2]
        self.assertEqual(ambiguous.action.value, "compensate")
        self.assertEqual(ambiguous.attempts, 1)
        self.assertEqual(ambiguous.unique_effects, 0)
        self.assertTrue(ambiguous.ack_recorded)

    def test_valid_contract_runs_five_deterministic_fake_sink_drills(self) -> None:
        self.write_contract()
        result = self.validate()
        self.assertEqual(result.operation_count, 1)
        self.assertEqual(result.marker_count, 1)
        self.assertEqual(result.drill_count, 5)
        contract = CONTRACT.load_contract(self.contract_path)
        observations = CONTRACT.run_recovery_drills(contract)
        ambiguous = observations[2]
        self.assertEqual(ambiguous.attempts, 2)
        self.assertEqual(ambiguous.unique_effects, 1)
        self.assertTrue(ambiguous.intent_recorded)
        self.assertTrue(ambiguous.ack_recorded)
        self.assertTrue(ambiguous.terminal_projected)

    def test_unkeyed_ambiguous_effect_stops_for_manual_review(self) -> None:
        operation = _operation()
        operation["idempotency"] = "none"
        operation["idempotency_key"] = "none"
        operation["retry_class"] = "conditional"
        operation["reconcile"] = "manual"
        operation["compensation"] = "delete"
        recovery = operation["recovery"]
        assert isinstance(recovery, dict)
        recovery["after_external_success_before_ack"] = "manual-review"
        recovery["duplicate_restart_replay"] = "manual-review"
        self.contract_path.write_text(json.dumps(_contract(operation)), encoding="utf-8")
        contract = CONTRACT.load_contract(self.contract_path)
        ambiguous = CONTRACT.run_recovery_drills(contract)[2]
        self.assertEqual(ambiguous.action.value, "manual-review")
        self.assertEqual(ambiguous.attempts, 1)
        self.assertEqual(ambiguous.unique_effects, 1)
        self.assertFalse(ambiguous.ack_recorded)
        self.assertTrue(ambiguous.terminal_projected)

    def test_local_ledger_ambiguous_effect_reconciles_without_replay(self) -> None:
        operation = _operation()
        operation["idempotency"] = "local-ledger"
        operation["retry_class"] = "conditional"
        operation["reconcile"] = "query"
        operation["compensation"] = "restore-snapshot"
        recovery = operation["recovery"]
        assert isinstance(recovery, dict)
        recovery["after_external_success_before_ack"] = "reconcile"
        recovery["duplicate_restart_replay"] = "reconcile"
        self.contract_path.write_text(json.dumps(_contract(operation)), encoding="utf-8")
        contract = CONTRACT.load_contract(self.contract_path)
        ambiguous = CONTRACT.run_recovery_drills(contract)[2]
        self.assertEqual(ambiguous.action.value, "reconcile")
        self.assertEqual(ambiguous.attempts, 1)
        self.assertEqual(ambiguous.unique_effects, 1)
        self.assertTrue(ambiguous.ack_recorded)

    def test_untracked_marker_is_ignored(self) -> None:
        self.write_contract()
        (self.source_dir / "venv.py").write_text(
            "# ccc-side-effect: untracked.effect\n", encoding="utf-8"
        )
        self.assertEqual(self.validate().marker_count, 1)

    def test_tracked_symlink_registration_source_fails_closed(self) -> None:
        outside = self.root / "outside.py"
        outside.write_text(
            "# ccc-side-effect: fixture.deliver\ndef deliver(): pass\n",
            encoding="utf-8",
        )
        self.source.unlink()
        self.source.symlink_to(outside)
        self.track(self.source)
        self.write_contract()
        with self.assertRaisesRegex(CONTRACT.ContractError, "source is a symlink"):
            self.validate()

    def test_registered_operation_missing_from_contract_fails_closed(self) -> None:
        extra = self.source_dir / "extra.py"
        extra.write_text(
            "# ccc-side-effect: fixture.extra\ndef extra(): pass\n", encoding="utf-8"
        )
        self.track(extra)
        self.write_contract()
        with self.assertRaisesRegex(CONTRACT.ContractError, "missing from contract"):
            self.validate()

    def test_contract_operation_missing_source_marker_fails_closed(self) -> None:
        self.source.write_text("def deliver(): return None\n", encoding="utf-8")
        self.write_contract()
        with self.assertRaisesRegex(CONTRACT.ContractError, "missing source marker"):
            self.validate()

    def test_missing_registered_symbol_fails_closed(self) -> None:
        self.source.write_text(
            "# ccc-side-effect: fixture.deliver\ndef renamed(): return None\n",
            encoding="utf-8",
        )
        self.write_contract()
        with self.assertRaisesRegex(CONTRACT.ContractError, "symbol is missing"):
            self.validate()

    def test_non_idempotent_effect_cannot_claim_safe_retry(self) -> None:
        operation = _operation()
        operation["idempotency"] = "none"
        operation["idempotency_key"] = "none"
        operation["retry_class"] = "safe"
        self.contract_path.write_text(json.dumps(_contract(operation)), encoding="utf-8")
        with self.assertRaisesRegex(CONTRACT.ContractError, "cannot be safe"):
            CONTRACT.load_contract(self.contract_path)

    def test_non_idempotent_effect_requires_recovery_handoff(self) -> None:
        operation = _operation()
        operation["idempotency"] = "none"
        operation["idempotency_key"] = "none"
        operation["retry_class"] = "conditional"
        operation["reconcile"] = "none"
        recovery = operation["recovery"]
        assert isinstance(recovery, dict)
        recovery["after_external_success_before_ack"] = "terminal-failure"
        recovery["duplicate_restart_replay"] = "terminal-failure"
        self.contract_path.write_text(json.dumps(_contract(operation)), encoding="utf-8")
        with self.assertRaisesRegex(CONTRACT.ContractError, "needs recovery handoff"):
            CONTRACT.load_contract(self.contract_path)

    def test_policy_derived_recovery_action_rejects_drift(self) -> None:
        operation = _operation()
        recovery = operation["recovery"]
        assert isinstance(recovery, dict)
        recovery["after_external_success_before_ack"] = "manual-review"
        self.contract_path.write_text(json.dumps(_contract(operation)), encoding="utf-8")
        with self.assertRaisesRegex(CONTRACT.ContractError, "must use safe-replay"):
            CONTRACT.load_contract(self.contract_path)

    def test_generated_document_drift_is_rejected(self) -> None:
        self.write_contract()
        self.document_path.write_text("stale\n", encoding="utf-8")
        with self.assertRaisesRegex(CONTRACT.ContractError, "generated block"):
            self.validate()

    def test_duplicate_json_key_is_rejected(self) -> None:
        self.contract_path.write_text(
            '{"schema_version":1,"schema_version":1,"registration_roots":["src"],"operations":[]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(CONTRACT.ContractError, "duplicate key"):
            CONTRACT.load_contract(self.contract_path)

    def test_credential_shaped_metadata_is_rejected_without_echo(self) -> None:
        credential = "ghp_" + "A" * 24
        operation = _operation()
        operation["audit_surface"] = credential
        self.contract_path.write_text(json.dumps(_contract(operation)), encoding="utf-8")
        with self.assertRaisesRegex(CONTRACT.ContractError, "resembles a credential") as caught:
            CONTRACT.load_contract(self.contract_path)
        self.assertNotIn(credential, str(caught.exception))

    def test_shell_main_registration_is_typed_and_validated(self) -> None:
        shell = self.source_dir / "effect.sh"
        shell.write_text(
            "#!/usr/bin/env bash\n# ccc-side-effect: fixture.deliver\nexit 0\n",
            encoding="utf-8",
        )
        self.track(shell)
        self.source.write_text("def unrelated(): pass\n", encoding="utf-8")
        operation = _operation()
        operation["registration"] = {
            "kind": "shell-main",
            "path": "src/effect.sh",
            "symbol": "<top-level>",
        }
        self.write_contract(_contract(operation))
        self.assertEqual(self.validate().marker_count, 1)

    def test_cli_diagnostic_does_not_print_private_source_body(self) -> None:
        marker = "PRIVATE_SOURCE_BODY_MUST_NOT_APPEAR"
        self.source.write_text(
            "# ccc-side-effect: fixture.deliver\n"
            f"def deliver(: # {marker}\n",
            encoding="utf-8",
        )
        self.write_contract()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = CONTRACT.main(
                [
                    "--repo-root",
                    str(self.root),
                    "--contract",
                    str(self.contract_path),
                    "--document",
                    str(self.document_path),
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("cannot parse registered Python source", stderr.getvalue())
        self.assertNotIn(marker, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
