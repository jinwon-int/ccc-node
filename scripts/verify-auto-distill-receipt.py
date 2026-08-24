#!/usr/bin/env python3
"""Validate an exact-source auto-distill evaluation receipt (#1262)."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


RECEIPT_SCHEMA = "ccc.auto-distill.evaluation-receipt.v1"
SURFACE_SCHEMA = "ccc.auto-distill.canon-surface.v1"
SUBJECT_PATH = "scripts/auto-distill/auto-distill.py"
SURFACE_MEMBERS = (
    "assign:PIPELINE",
    "assign:CANON_PROMPT",
    "assign:CANON_EXCLUDE",
    "assign:_FOCUS_WORD_RE",
    "assign:TOKEN_RES",
    "assign:SELF_ADDR_RE",
    "function:unwrap_claude_envelope",
    "function:extract_json",
    "function:_focus_needles",
    "function:_needle_score",
    "function:_window_body",
    "function:section_body",
    "function:literal_hits",
    "function:self_canon_addrs",
    "function:format_self_canon",
    "function:self_canon_sections",
    "function:canon_snippets",
    "function:canon_dedup",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MD5_RE = re.compile(r"[0-9a-f]{32}")
_EVALUATION_ID_RE = re.compile(r"TM-[0-9][0-9A-Za-z-]*")


class ReceiptError(ValueError):
    """The receipt does not prove that this exact source passed its gate."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _expect_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReceiptError(f"{label} must be an object")
    return value


def _expect_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReceiptError(f"{label} keys mismatch: missing={missing} extra={extra}")


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReceiptError(f"{label} must be a non-negative integer")
    return value


def _digest(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ReceiptError(f"{label} has an invalid digest")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ReceiptError(f"{label} must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReceiptError(f"{label} is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReceiptError(f"{label} must include a timezone")
    return parsed


def _source_nodes(source_text: str, source_path: Path) -> tuple[int, dict[str, ast.AST]]:
    try:
        tree = ast.parse(source_text, filename=str(source_path))
    except SyntaxError as exc:
        raise ReceiptError("managed source is not valid Python") from exc

    nodes: dict[str, ast.AST] = {}
    pipeline_values: list[int] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes[f"function:{node.name}"] = node
            continue
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            nodes[f"assign:{target.id}"] = node
            if target.id == "PIPELINE":
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, int) \
                        and not isinstance(value.value, bool):
                    pipeline_values.append(value.value)

    if len(pipeline_values) != 1 or pipeline_values[0] < 1:
        raise ReceiptError("managed source must define one positive integer PIPELINE")
    return pipeline_values[0], nodes


def describe_source(source_path: Path) -> dict[str, Any]:
    if source_path.is_symlink() or not source_path.is_file():
        raise ReceiptError("managed source is missing or unsafe")
    source_bytes = source_path.read_bytes()
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("managed source is not UTF-8") from exc
    pipeline, nodes = _source_nodes(source_text, source_path)

    payload = bytearray((SURFACE_SCHEMA + "\0").encode("utf-8"))
    for member in SURFACE_MEMBERS:
        node = nodes.get(member)
        if node is None:
            raise ReceiptError(f"evaluation surface member is missing: {member}")
        segment = ast.get_source_segment(source_text, node)
        if segment is None:
            raise ReceiptError(f"cannot extract evaluation surface member: {member}")
        payload.extend(member.encode("utf-8"))
        payload.extend(b"\0")
        payload.extend(segment.encode("utf-8"))
        payload.extend(b"\0")

    return {
        "pipeline": pipeline,
        "sha256": _sha256(source_bytes),
        "surface_schema": SURFACE_SCHEMA,
        "surface_sha256": _sha256(bytes(payload)),
        "surface_members": list(SURFACE_MEMBERS),
    }


def _load_receipt(receipt_path: Path) -> dict[str, Any]:
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ReceiptError("evaluation receipt is missing or unsafe")
    if receipt_path.stat().st_size > 65_536:
        raise ReceiptError("evaluation receipt exceeds 65536 bytes")
    try:
        parsed = json.loads(
            receipt_path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except ReceiptError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("evaluation receipt is not valid UTF-8 JSON") from exc
    return _expect_object(parsed, "receipt")


def _verify_subject(receipt: dict[str, Any], description: dict[str, Any]) -> str:
    subject = _expect_object(receipt["subject"], "subject")
    _expect_keys(
        subject,
        {"path", "sha256", "surface_schema", "surface_sha256", "surface_members"},
        "subject",
    )
    if subject["path"] != SUBJECT_PATH:
        raise ReceiptError("receipt subject path is not canonical")
    subject_sha256 = _digest(subject["sha256"], _SHA256_RE, "subject.sha256")
    if subject_sha256 != description["sha256"]:
        raise ReceiptError("receipt subject SHA-256 does not match managed source")
    if subject["surface_schema"] != SURFACE_SCHEMA:
        raise ReceiptError("receipt surface schema is unsupported")
    if subject["surface_members"] != description["surface_members"]:
        raise ReceiptError("receipt surface member list does not match the verifier")
    surface_sha256 = _digest(
        subject["surface_sha256"], _SHA256_RE, "subject.surface_sha256"
    )
    if surface_sha256 != description["surface_sha256"]:
        raise ReceiptError("receipt surface SHA-256 does not match managed source")
    return subject_sha256


def _verify_corpus(evaluation: dict[str, Any]) -> int:
    corpus = _expect_object(evaluation["corpus"], "evaluation.corpus")
    _expect_keys(
        corpus,
        {
            "sample",
            "sample_md5",
            "sample_sha256",
            "verdicts",
            "verdicts_md5",
            "verdicts_sha256",
            "cases",
        },
        "evaluation.corpus",
    )
    for label in ("sample", "verdicts"):
        value = corpus[label]
        if not isinstance(value, str) or not value or len(value) > 128 \
                or value in {".", ".."} or Path(value).name != value:
            raise ReceiptError(f"evaluation.corpus.{label} must be a basename")
    _digest(corpus["sample_md5"], _MD5_RE, "evaluation.corpus.sample_md5")
    _digest(corpus["verdicts_md5"], _MD5_RE, "evaluation.corpus.verdicts_md5")
    _digest(corpus["sample_sha256"], _SHA256_RE, "evaluation.corpus.sample_sha256")
    _digest(
        corpus["verdicts_sha256"], _SHA256_RE, "evaluation.corpus.verdicts_sha256"
    )
    cases = _nonnegative_int(corpus["cases"], "evaluation.corpus.cases")
    if cases < 1:
        raise ReceiptError("evaluation corpus must not be empty")
    return cases


def _verify_provenance(evaluation: dict[str, Any]) -> None:
    harness = _expect_object(evaluation["harness"], "evaluation.harness")
    _expect_keys(
        harness,
        {"eval_canon_sha256", "recheck_addr_sha256"},
        "evaluation.harness",
    )
    for label in ("eval_canon_sha256", "recheck_addr_sha256"):
        _digest(harness[label], _SHA256_RE, f"evaluation.harness.{label}")

    artifacts = _expect_object(evaluation["artifacts"], "evaluation.artifacts")
    _expect_keys(
        artifacts,
        {"eval_log_sha256", "eval_result_sha256", "recheck_log_sha256"},
        "evaluation.artifacts",
    )
    for label in ("eval_log_sha256", "eval_result_sha256", "recheck_log_sha256"):
        _digest(artifacts[label], _SHA256_RE, f"evaluation.artifacts.{label}")


def _verify_recheck(evaluation: dict[str, Any]) -> None:
    recheck = _expect_object(evaluation["recheck"], "evaluation.recheck")
    _expect_keys(
        recheck,
        {"baseline_duplicates", "duplicates", "cases", "suspect"},
        "evaluation.recheck",
    )
    baseline = _nonnegative_int(
        recheck["baseline_duplicates"], "evaluation.recheck.baseline_duplicates"
    )
    duplicates = _nonnegative_int(
        recheck["duplicates"], "evaluation.recheck.duplicates"
    )
    recheck_cases = _nonnegative_int(recheck["cases"], "evaluation.recheck.cases")
    suspect = _nonnegative_int(recheck["suspect"], "evaluation.recheck.suspect")
    if recheck_cases < 1 or duplicates + suspect > recheck_cases:
        raise ReceiptError("evaluation recheck counts are inconsistent")
    if duplicates <= baseline:
        raise ReceiptError("evaluation recheck did not improve over baseline")


def _verify_gate_result(evaluation: dict[str, Any], cases: int) -> None:
    confusion = _expect_object(evaluation["confusion"], "evaluation.confusion")
    _expect_keys(confusion, {"tp", "fp", "fn", "tn"}, "evaluation.confusion")
    tp = _nonnegative_int(confusion["tp"], "evaluation.confusion.tp")
    fp = _nonnegative_int(confusion["fp"], "evaluation.confusion.fp")
    fn = _nonnegative_int(confusion["fn"], "evaluation.confusion.fn")
    tn = _nonnegative_int(confusion["tn"], "evaluation.confusion.tn")
    if tp + fp + fn + tn != cases:
        raise ReceiptError("confusion matrix does not match corpus size")
    collateral_damage = _nonnegative_int(
        evaluation["collateral_damage"], "evaluation.collateral_damage"
    )
    if evaluation["passed"] is not True:
        raise ReceiptError("evaluation is not signed as passed")
    if collateral_damage != 0:
        raise ReceiptError("evaluation has collateral damage")
    if tp < 1:
        raise ReceiptError("evaluation is invalid because it kept every duplicate")


def _verify_evaluation(
    receipt: dict[str, Any], subject_sha256: str
) -> dict[str, Any]:
    evaluation = _expect_object(receipt["evaluation"], "evaluation")
    _expect_keys(
        evaluation,
        {
            "id",
            "evaluated_source_sha256",
            "completed_at",
            "provider",
            "model",
            "corpus",
            "harness",
            "artifacts",
            "recheck",
            "confusion",
            "collateral_damage",
            "passed",
        },
        "evaluation",
    )
    if not isinstance(evaluation["id"], str) \
            or _EVALUATION_ID_RE.fullmatch(evaluation["id"]) is None:
        raise ReceiptError("evaluation.id is invalid")
    evaluated_sha256 = _digest(
        evaluation["evaluated_source_sha256"],
        _SHA256_RE,
        "evaluation.evaluated_source_sha256",
    )
    if evaluated_sha256 != subject_sha256:
        raise ReceiptError("evaluated source is not the exact deploy source")
    completed_at = _timestamp(evaluation["completed_at"], "evaluation.completed_at")
    issued_at = _timestamp(receipt["issued_at"], "issued_at")
    if issued_at < completed_at:
        raise ReceiptError("receipt was issued before evaluation completed")
    for label in ("provider", "model"):
        value = evaluation[label]
        if not isinstance(value, str) or not value.strip() or len(value) > 80:
            raise ReceiptError(f"evaluation.{label} is invalid")
    cases = _verify_corpus(evaluation)
    _verify_provenance(evaluation)
    _verify_recheck(evaluation)
    _verify_gate_result(evaluation, cases)
    return evaluation


def verify_receipt(source_path: Path, receipt_path: Path) -> dict[str, Any]:
    description = describe_source(source_path)
    receipt = _load_receipt(receipt_path)
    _expect_keys(
        receipt,
        {"schema", "pipeline", "subject", "evaluation", "issued_at"},
        "receipt",
    )
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise ReceiptError("unsupported receipt schema")
    pipeline = _nonnegative_int(receipt["pipeline"], "pipeline")
    if pipeline < 1 or pipeline != description["pipeline"]:
        raise ReceiptError("receipt pipeline does not match managed source")
    subject_sha256 = _verify_subject(receipt, description)
    evaluation = _verify_evaluation(receipt, subject_sha256)
    return {
        **description,
        "evaluation_id": evaluation["id"],
        "completed_at": evaluation["completed_at"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--receipt", type=Path)
    action.add_argument("--describe-source", action="store_true")
    action.add_argument("--json", action="store_true", dest="describe_json")
    args = parser.parse_args(argv)

    try:
        if args.receipt is not None:
            result = verify_receipt(args.source, args.receipt)
            print(
                "evaluation receipt ok: pipeline=%d source_sha256=%s "
                "surface_sha256=%s evaluation=%s completed_at=%s"
                % (
                    result["pipeline"],
                    result["sha256"],
                    result["surface_sha256"],
                    result["evaluation_id"],
                    result["completed_at"],
                )
            )
        else:
            result = describe_source(args.source)
            if args.describe_json:
                print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            else:
                print(
                    "pipeline=%d source_sha256=%s surface_sha256=%s"
                    % (result["pipeline"], result["sha256"], result["surface_sha256"])
                )
    except ReceiptError as exc:
        print(f"evaluation receipt invalid: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
