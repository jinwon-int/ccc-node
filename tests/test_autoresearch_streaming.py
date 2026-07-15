"""Contract tests for the isolated streaming-boundary research evaluator."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "research" / "streaming"


def _load_evaluator():
    path = RESEARCH_DIR / "evaluate.py"
    spec = importlib.util.spec_from_file_location("streaming_evaluator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_baseline_candidate_scores_full_marks() -> None:
    evaluator = _load_evaluator()
    result = evaluator.evaluate(
        RESEARCH_DIR / "candidate.py", RESEARCH_DIR / "fixtures.json"
    )

    assert result["ok"] is True
    assert result["score"] == 100.0
    assert result["metrics"] == {
        "exact_sequence_rate": 1.0,
        "bubble_f1": 1.0,
        "interim_latency_score": 1.0,
        "invalid_cases": 0,
        "case_count": 8,
    }


def test_contract_violation_invalidates_run(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("def segment(events):\n    return 'not-a-list'\n", encoding="utf-8")
    evaluator = _load_evaluator()

    result = evaluator.evaluate(candidate, RESEARCH_DIR / "fixtures.json")

    assert result["ok"] is False
    assert result["score"] == 0.0
    assert result["metrics"]["invalid_cases"] == 8


def test_fixtures_are_synthetic_and_schema_versioned() -> None:
    payload = json.loads((RESEARCH_DIR / "fixtures.json").read_text(encoding="utf-8"))

    assert payload["schema"] == "ccc.autoresearch.streaming.v1"
    assert {case["provider"] for case in payload["cases"]} == {"claude", "codex"}
    serialized = json.dumps(payload).lower()
    assert "telegram_bot_token" not in serialized
    assert "api_key" not in serialized


def test_memory_baseline_scores_full_marks() -> None:
    memory_dir = ROOT / "research" / "memory"
    path = memory_dir / "evaluate.py"
    spec = importlib.util.spec_from_file_location("memory_evaluator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module.evaluate(memory_dir / "candidate.py", memory_dir / "fixtures.json")

    assert result["ok"] is True
    assert result["score"] == 100.0
    assert result["metrics"] == {
        "recall": 1.0,
        "precision": 1.0,
        "precision_at_1": 1.0,
        "contamination_avoidance": 1.0,
        "context_budget": 1.0,
        "invalid_cases": 0,
        "case_count": 8,
    }
