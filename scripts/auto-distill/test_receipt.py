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
# Fixture only: a plausible provider id shape used to mutate copies of the
# canonical receipt. The canonical receipt itself records its own resolution
# since the #1521 re-issue; `canonical_receipt()` reads the live values.
#
# The fixture pins a BARE ALIAS on purpose: the family check
# ("alias=haiku must not resolve to a sonnet id") only engages when the alias is
# a bare alias, so a fixture that inherited a fully-qualified id from the
# canonical receipt would silently skip it. `with_model_resolution` therefore
# rewrites `evaluation.model` to match the fixture alias instead of assuming the
# canonical receipt still records one — a receipt is free to hand the launcher a
# fully-qualified id (TM-3322 does), and that must not disarm these assertions.
RESOLVED_ID = "claude-haiku-4-5-20251001"
MODEL_RESOLUTION = {
    "alias": "haiku",
    "resolved_id": RESOLVED_ID,
    "resolved_by": "claude-json-modelUsage:eval.log",
    "resolved_at": "2026-09-05T13:01:16Z",
}


def canonical_receipt() -> dict:
    return json.loads(RECEIPT.read_text(encoding="utf-8"))


def without_model_resolution(data) -> None:
    data["evaluation"].pop("model_resolution", None)


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

    def with_model_resolution(self, *, sync_model: bool = True, **overrides):
        """Attach the fixture resolution to a copy of the canonical receipt.

        `sync_model` keeps `evaluation.model` equal to the fixture alias so the
        copy is internally consistent whatever the canonical receipt records.
        Pass `sync_model=False` to build the deliberate alias/model mismatch.
        """

        def mutate(data) -> None:
            resolution = {**MODEL_RESOLUTION, **overrides}
            data["evaluation"]["model_resolution"] = resolution
            if sync_model:
                data["evaluation"]["model"] = resolution["alias"]

        return self.mutated_receipt(mutate)

    def mutated_receipt(self, mutate) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        target = Path(temporary.name) / "receipt.json"
        data = json.loads(RECEIPT.read_text(encoding="utf-8"))
        mutate(data)
        target.write_text(json.dumps(data), encoding="utf-8")
        return temporary, target

    def test_canonical_receipt_validates_exact_source(self) -> None:
        evaluation_id = canonical_receipt()["evaluation"]["id"]
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("evaluation receipt ok: pipeline=6", result.stdout)
        self.assertIn(f"evaluation={evaluation_id}", result.stdout)

    def test_canonical_receipt_records_resolved_model_id(self) -> None:
        # #1521 re-issue: the canonical receipt pins the provider model id the
        # launcher alias resolved to, and must verify under --require-model.
        evaluation = canonical_receipt()["evaluation"]
        resolution = evaluation["model_resolution"]
        self.assertEqual(resolution["alias"], evaluation["model"])
        self.assertNotIn(resolution["resolved_id"].lower(), {"haiku", "sonnet", "opus", "default"})
        self.assertTrue(resolution["resolved_by"].startswith("claude-json-modelUsage:"))
        for extra in ((), ("--require-model",)):
            with self.subTest(extra=extra):
                result = self.run_verifier(extra=extra)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"evaluation={evaluation['id']}", result.stdout)
                self.assertTrue(
                    result.stdout.rstrip().endswith(f" model={resolution['resolved_id']}"),
                    result.stdout,
                )

    def test_receipt_without_model_resolution_still_verifies(self) -> None:
        # Backward compatibility (#1521): receipts issued before the optional
        # model_resolution object (TM-3298 shape) keep verifying unchanged.
        temporary, receipt = self.mutated_receipt(without_model_resolution)
        with temporary:
            result = self.run_verifier(receipt=receipt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.rstrip().endswith(" model=unrecorded"), result.stdout)

    def test_require_model_rejects_receipt_without_resolution(self) -> None:
        temporary, receipt = self.mutated_receipt(without_model_resolution)
        with temporary:
            result = self.run_verifier(receipt=receipt, extra=("--require-model",))
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("model_resolution is required", result.stderr)

    def test_valid_model_resolution_is_reported_in_summary(self) -> None:
        temporary, receipt = self.with_model_resolution()
        with temporary:
            result = self.run_verifier(receipt=receipt)
            required = self.run_verifier(receipt=receipt, extra=("--require-model",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"evaluation={canonical_receipt()['evaluation']['id']}", result.stdout)
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
        temporary, receipt = self.with_model_resolution(
            alias="sonnet", sync_model=False
        )
        with temporary:
            result = self.run_verifier(receipt=receipt)
        self.assertEqual(result.returncode, 3)
        self.assertIn("alias does not match evaluation.model", result.stderr)

    def test_resolution_must_not_postdate_issuance(self) -> None:
        temporary, receipt = self.with_model_resolution(
            resolved_at="2099-01-01T00:00:00+09:00"
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

    def test_schema_declares_model_resolution_as_optional(self) -> None:
        # #1521: the schema and the verifier must agree on the optional object
        # so a re-issued receipt carrying it is not rejected by either.
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        evaluation = schema["properties"]["evaluation"]
        self.assertNotIn("model_resolution", evaluation["required"])
        resolution = evaluation["properties"]["model_resolution"]
        self.assertFalse(resolution["additionalProperties"])
        self.assertEqual(
            set(resolution["required"]), {"alias", "resolved_id", "resolved_by", "resolved_at"}
        )
        self.assertEqual(set(resolution["properties"]), set(MODEL_RESOLUTION))

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
