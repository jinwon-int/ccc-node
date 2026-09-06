#!/usr/bin/env python3
"""Exact-source evaluation receipt regressions (#1262)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
VERIFIER = ROOT / "scripts/verify-auto-distill-receipt.py"
SOURCE = ROOT / "scripts/auto-distill/auto-distill.py"
RECEIPT = ROOT / "scripts/auto-distill/evaluation-receipt.json"
SCHEMA = ROOT / "schemas/auto-distill-evaluation-receipt-v1.schema.json"
# Fixture only: a plausible provider id shape. The canonical receipt (TM-3298)
# predates #1521 and deliberately records no resolution.
RESOLVED_ID = "claude-haiku-4-5-20251001"
MODEL_RESOLUTION = {
    "alias": "haiku",
    "resolved_id": RESOLVED_ID,
    "resolved_by": "claude-json-modelUsage:eval.log",
    "resolved_at": "2026-09-05T13:01:16Z",
}


class AutoDistillReceiptTest(unittest.TestCase):
    def run_verifier(
        self,
        *,
        source: Path = SOURCE,
        receipt: Path = RECEIPT,
        extra: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                str(VERIFIER),
                "--source",
                str(source),
                "--receipt",
                str(receipt),
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def with_model_resolution(self, **overrides):
        def mutate(data) -> None:
            data["evaluation"]["model_resolution"] = {**MODEL_RESOLUTION, **overrides}

        return self.mutated_receipt(mutate)

    def mutated_receipt(self, mutate) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        target = Path(temporary.name) / "receipt.json"
        data = json.loads(RECEIPT.read_text(encoding="utf-8"))
        mutate(data)
        target.write_text(json.dumps(data), encoding="utf-8")
        return temporary, target

    def test_canonical_receipt_validates_exact_source(self) -> None:
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("evaluation receipt ok: pipeline=6", result.stdout)
        self.assertIn("evaluation=TM-3298", result.stdout)

    def test_canonical_receipt_predates_model_resolution_and_still_verifies(self) -> None:
        # Backward compatibility (#1521): TM-3298 was issued without the
        # optional model_resolution object and must keep verifying unchanged.
        receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
        self.assertNotIn("model_resolution", receipt["evaluation"])
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("evaluation=TM-3298", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith(" model=unrecorded"), result.stdout)

    def test_require_model_rejects_receipt_without_resolution(self) -> None:
        result = self.run_verifier(extra=("--require-model",))
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("model_resolution is required", result.stderr)

    def test_valid_model_resolution_is_reported_in_summary(self) -> None:
        temporary, receipt = self.with_model_resolution()
        with temporary:
            result = self.run_verifier(receipt=receipt)
            required = self.run_verifier(receipt=receipt, extra=("--require-model",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("evaluation=TM-3298", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith(f" model={RESOLVED_ID}"), result.stdout)
        self.assertEqual(required.returncode, 0, required.stderr)
        self.assertIn(f"model={RESOLVED_ID}", required.stdout)

    def test_alias_only_resolved_id_is_rejected(self) -> None:
        for alias in ("haiku", "Haiku", "default", " sonnet "):
            with self.subTest(alias=alias):
                temporary, receipt = self.with_model_resolution(resolved_id=alias)
                with temporary:
                    result = self.run_verifier(receipt=receipt)
                self.assertEqual(result.returncode, 3, result.stdout)
                self.assertIn("resolved_id is", result.stderr)

    def test_resolved_id_must_belong_to_alias_family(self) -> None:
        temporary, receipt = self.with_model_resolution(
            resolved_id="claude-sonnet-4-5-20250929"
        )
        with temporary:
            result = self.run_verifier(receipt=receipt)
        self.assertEqual(result.returncode, 3)
        self.assertIn("does not belong to the alias family", result.stderr)

    def test_resolution_alias_must_match_evaluation_model(self) -> None:
        temporary, receipt = self.with_model_resolution(alias="sonnet")
        with temporary:
            result = self.run_verifier(receipt=receipt)
        self.assertEqual(result.returncode, 3)
        self.assertIn("alias does not match evaluation.model", result.stderr)

    def test_resolution_must_not_postdate_issuance(self) -> None:
        temporary, receipt = self.with_model_resolution(
            resolved_at="2026-09-06T00:00:00+09:00"
        )
        with temporary:
            result = self.run_verifier(receipt=receipt)
        self.assertEqual(result.returncode, 3)
        self.assertIn("issued before the model id was resolved", result.stderr)

    def test_resolution_rejects_unknown_or_missing_keys(self) -> None:
        temporary, receipt = self.with_model_resolution(provider="anthropic")
        with temporary:
            result = self.run_verifier(receipt=receipt)
        self.assertEqual(result.returncode, 3)
        self.assertIn("model_resolution keys mismatch", result.stderr)
        for missing in ("resolved_by", "resolved_at"):
            with self.subTest(missing=missing):
                temporary, receipt = self.mutated_receipt(
                    lambda data, missing=missing: data["evaluation"].update(
                        {
                            "model_resolution": {
                                key: value
                                for key, value in MODEL_RESOLUTION.items()
                                if key != missing
                            }
                        }
                    )
                )
                with temporary:
                    result = self.run_verifier(receipt=receipt)
                self.assertEqual(result.returncode, 3)
                self.assertIn(f"missing=['{missing}']", result.stderr)

    def test_schema_pins_canonical_receipt_identity_and_surface(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
        properties = schema["properties"]
        subject_properties = properties["subject"]["properties"]
        self.assertEqual(receipt["schema"], properties["schema"]["const"])
        self.assertEqual(receipt["subject"]["path"], subject_properties["path"]["const"])
        self.assertEqual(
            receipt["subject"]["surface_members"],
            subject_properties["surface_members"]["const"],
        )

    def test_describe_source_is_body_free_json(self) -> None:
        result = subprocess.run(
            ["python3", str(VERIFIER), "--source", str(SOURCE), "--json"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        description = json.loads(result.stdout)
        self.assertEqual(description["pipeline"], 6)
        self.assertEqual(len(description["sha256"]), 64)
        self.assertEqual(len(description["surface_sha256"]), 64)
        self.assertNotIn("source", description)

    def test_evaluated_source_must_equal_deploy_source(self) -> None:
        temporary, receipt = self.mutated_receipt(
            lambda data: data["evaluation"].update(
                {"evaluated_source_sha256": "0" * 64}
            )
        )
        with temporary:
            result = self.run_verifier(receipt=receipt)
        self.assertEqual(result.returncode, 3)
        self.assertIn("not the exact deploy source", result.stderr)

    def test_issued_at_must_follow_completed_at(self) -> None:
        temporary, receipt = self.mutated_receipt(
            lambda data: data.update({"issued_at": "2026-08-24T00:00:00+09:00"})
        )
        with temporary:
            result = self.run_verifier(receipt=receipt)
        self.assertEqual(result.returncode, 3)
        self.assertIn("issued before evaluation completed", result.stderr)

    def test_confusion_matrix_must_match_corpus(self) -> None:
        temporary, receipt = self.mutated_receipt(
            lambda data: data["evaluation"]["confusion"].update({"tn": 999})
        )
        with temporary:
            result = self.run_verifier(receipt=receipt)
        self.assertEqual(result.returncode, 3)
        self.assertIn("does not match corpus size", result.stderr)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt.json"
            original = RECEIPT.read_text(encoding="utf-8").rstrip()
            receipt.write_text(
                original[:-1] + ',"pipeline":6}',
                encoding="utf-8",
            )
            result = self.run_verifier(receipt=receipt)
        self.assertEqual(result.returncode, 3)
        self.assertIn("duplicate JSON key", result.stderr)


if __name__ == "__main__":
    unittest.main()
