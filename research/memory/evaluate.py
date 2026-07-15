#!/usr/bin/env python3
"""Deterministic scorer for shared-all memory ranking candidates."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
DEFAULT_CANDIDATE = ROOT / "candidate.py"
DEFAULT_FIXTURES = ROOT / "fixtures.json"


class CandidateError(ValueError):
    """Raised when a ranking candidate violates the evaluator contract."""


def _load_ranker(path: Path) -> Callable[..., Any]:
    spec = importlib.util.spec_from_file_location("ccc_memory_candidate", path)
    if spec is None or spec.loader is None:
        raise CandidateError(f"cannot import candidate: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ranker = getattr(module, "rank", None)
    if not callable(ranker):
        raise CandidateError("candidate must define callable rank(query, context, documents, limit)")
    return ranker


def evaluate(candidate_path: Path, fixtures_path: Path, *, limit: int = 5) -> dict[str, Any]:
    ranker = _load_ranker(candidate_path)
    payload = json.loads(fixtures_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "ccc.autoresearch.memory.v1":
        raise CandidateError("unsupported fixture schema")
    documents = payload.get("documents", [])
    by_id = {document["id"]: document for document in documents}

    rows: list[dict[str, Any]] = []
    recall_total = 0.0
    precision_total = 0.0
    precision_at_1_total = 0.0
    contamination_total = 0.0
    budget_total = 0.0
    invalid_cases = 0

    for case in payload.get("cases", []):
        error: str | None = None
        try:
            ranked = ranker(case["query"], case["context"], documents, limit)
            if not isinstance(ranked, list) or any(not isinstance(item, str) for item in ranked):
                raise CandidateError("rank must return a list of document id strings")
            if len(ranked) > limit or len(ranked) != len(set(ranked)):
                raise CandidateError("rank returned too many or duplicate ids")
            unknown = [item for item in ranked if item not in by_id]
            if unknown:
                raise CandidateError(f"rank returned unknown ids: {unknown}")
        except Exception as exc:  # Candidate failures become scored evidence.
            ranked = []
            error = f"{type(exc).__name__}: {exc}"
            invalid_cases += 1

        expected = case["expected"]
        expected_hits = sum(1 for item in expected if item in ranked)
        recall = expected_hits / len(expected) if expected else float(not ranked)
        precision = expected_hits / len(ranked) if ranked else float(not expected)
        precision_at_1 = float(bool(ranked) and ranked[0] in expected)

        forbidden = set(case.get("forbidden", []))
        context_owner = case["context"].get("owner_id")
        for document in documents:
            if document.get("owner_id") != context_owner:
                forbidden.add(document["id"])
            if document.get("sensitivity") == "secret":
                forbidden.add(document["id"])
        contamination_free = float(not any(item in forbidden for item in ranked))

        used_chars = sum(len(str(by_id[item].get("text", ""))) for item in ranked)
        max_chars = max(1, int(case["context"].get("max_chars", 600)))
        budget_score = 1.0 if used_chars <= max_chars else max_chars / used_chars

        recall_total += recall
        precision_total += precision
        precision_at_1_total += precision_at_1
        contamination_total += contamination_free
        budget_total += budget_score
        rows.append(
            {
                "id": case["id"],
                "ranked": ranked,
                "expected": expected,
                "forbidden": sorted(forbidden),
                "recall": round(recall, 6),
                "precision": round(precision, 6),
                "precision_at_1": round(precision_at_1, 6),
                "contamination_free": bool(contamination_free),
                "used_chars": used_chars,
                "max_chars": max_chars,
                "error": error,
            }
        )

    case_count = len(rows)
    if case_count == 0:
        raise CandidateError("fixture set contains no cases")
    recall = recall_total / case_count
    precision = precision_total / case_count
    precision_at_1 = precision_at_1_total / case_count
    contamination = contamination_total / case_count
    budget = budget_total / case_count
    score = 40 * recall + 30 * precision + 20 * contamination + 10 * budget
    if invalid_cases:
        score = 0.0

    return {
        "schema": "ccc.autoresearch.memory.result.v1",
        "ok": invalid_cases == 0,
        "score": round(score, 6),
        "metrics": {
            "recall": round(recall, 6),
            "precision": round(precision, 6),
            "precision_at_1": round(precision_at_1, 6),
            "contamination_avoidance": round(contamination, 6),
            "context_budget": round(budget, 6),
            "invalid_cases": invalid_cases,
            "case_count": case_count,
        },
        "candidate": {
            "path": str(candidate_path),
            "sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        },
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--fail-under", type=float, default=None)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    try:
        result = evaluate(args.candidate.resolve(), args.fixtures.resolve())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 2

    if args.summary:
        metrics = result["metrics"]
        print(
            f"score={result['score']:.3f} recall={metrics['recall']:.3f} "
            f"precision={metrics['precision']:.3f} p@1={metrics['precision_at_1']:.3f} "
            f"clean={metrics['contamination_avoidance']:.3f} "
            f"budget={metrics['context_budget']:.3f}"
        )
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))

    if not result["ok"]:
        return 2
    if args.fail_under is not None and result["score"] < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
