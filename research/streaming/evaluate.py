#!/usr/bin/env python3
"""Deterministic, local evaluator for streaming-boundary candidate policies."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
DEFAULT_CANDIDATE = ROOT / "candidate.py"
DEFAULT_FIXTURES = ROOT / "fixtures.json"
MAX_BUBBLE_CHARS = 4000


class CandidateError(ValueError):
    """Raised when a candidate violates the evaluator contract."""


def _load_candidate(path: Path) -> tuple[ModuleType, Callable[[list[dict[str, Any]]], Any]]:
    spec = importlib.util.spec_from_file_location("ccc_streaming_candidate", path)
    if spec is None or spec.loader is None:
        raise CandidateError(f"cannot import candidate: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    segment = getattr(module, "segment", None)
    if not callable(segment):
        raise CandidateError("candidate must define callable segment(events)")
    return module, segment


def _validate_output(value: Any, event_count: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise CandidateError("segment(events) must return a list")
    output: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise CandidateError(f"bubble {index} must be an object")
        text = item.get("text")
        released_at = item.get("released_at_event")
        if not isinstance(text, str) or not text.strip():
            raise CandidateError(f"bubble {index} text must be non-empty")
        if len(text) > MAX_BUBBLE_CHARS:
            raise CandidateError(f"bubble {index} exceeds {MAX_BUBBLE_CHARS} characters")
        if not isinstance(released_at, int) or not 0 <= released_at <= event_count:
            raise CandidateError(f"bubble {index} has invalid released_at_event")
        output.append({"text": text.strip(), "released_at_event": released_at})
    return output


def _multiset_f1(expected: list[str], actual: list[str]) -> float:
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    matches = sum((expected_counts & actual_counts).values())
    if not expected and not actual:
        return 1.0
    precision = matches / len(actual) if actual else 0.0
    recall = matches / len(expected) if expected else 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def evaluate(candidate_path: Path, fixtures_path: Path) -> dict[str, Any]:
    _, segment = _load_candidate(candidate_path)
    payload = json.loads(fixtures_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "ccc.autoresearch.streaming.v1":
        raise CandidateError("unsupported fixture schema")

    rows: list[dict[str, Any]] = []
    exact_total = 0.0
    f1_total = 0.0
    latency_points = 0.0
    latency_total = 0
    invalid_cases = 0

    for case in payload.get("cases", []):
        events = case["events"]
        expected = case["expected"]
        error: str | None = None
        try:
            actual = _validate_output(segment(events), len(events))
        except Exception as exc:  # Candidate failures become scored evidence.
            actual = []
            error = f"{type(exc).__name__}: {exc}"
            invalid_cases += 1

        expected_text = [item["text"] for item in expected]
        actual_text = [item["text"] for item in actual]
        exact = actual_text == expected_text
        bubble_f1 = _multiset_f1(expected_text, actual_text)
        exact_total += float(exact)
        f1_total += bubble_f1

        case_latency_scores: list[float] = []
        for expected_index, expected_item in enumerate(expected):
            if not expected_item.get("interim"):
                continue
            latency_total += 1
            if expected_index >= len(actual):
                case_latency_scores.append(0.0)
                continue
            actual_item = actual[expected_index]
            if actual_item["text"] != expected_item["text"]:
                case_latency_scores.append(0.0)
                continue
            deadline = int(expected_item["released_by_event"])
            released_at = actual_item["released_at_event"]
            score = 1.0 if released_at <= deadline else 1.0 / (1 + released_at - deadline)
            case_latency_scores.append(score)
            latency_points += score

        rows.append(
            {
                "id": case["id"],
                "provider": case["provider"],
                "exact": exact,
                "bubble_f1": round(bubble_f1, 6),
                "latency": round(
                    sum(case_latency_scores) / len(case_latency_scores), 6
                )
                if case_latency_scores
                else None,
                "expected": expected,
                "actual": actual,
                "error": error,
            }
        )

    case_count = len(rows)
    if case_count == 0:
        raise CandidateError("fixture set contains no cases")
    exact_rate = exact_total / case_count
    bubble_f1 = f1_total / case_count
    latency = latency_points / latency_total if latency_total else 1.0
    score = max(0.0, 70 * exact_rate + 20 * bubble_f1 + 10 * latency)
    if invalid_cases:
        score = 0.0

    candidate_bytes = candidate_path.read_bytes()
    return {
        "schema": "ccc.autoresearch.streaming.result.v1",
        "ok": invalid_cases == 0,
        "score": round(score, 6),
        "metrics": {
            "exact_sequence_rate": round(exact_rate, 6),
            "bubble_f1": round(bubble_f1, 6),
            "interim_latency_score": round(latency, 6),
            "invalid_cases": invalid_cases,
            "case_count": case_count,
        },
        "candidate": {
            "path": str(candidate_path),
            "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        },
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--fail-under", type=float, default=None)
    parser.add_argument("--summary", action="store_true", help="print one compact line")
    args = parser.parse_args()

    try:
        result = evaluate(args.candidate.resolve(), args.fixtures.resolve())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 2

    if args.summary:
        metrics = result["metrics"]
        print(
            f"score={result['score']:.3f} exact={metrics['exact_sequence_rate']:.3f} "
            f"f1={metrics['bubble_f1']:.3f} latency={metrics['interim_latency_score']:.3f}"
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
