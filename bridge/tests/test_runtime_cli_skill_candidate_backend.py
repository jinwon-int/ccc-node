"""Contract for the Claude/Piri runtime-CLI skill-candidate backend (#667).

Mirrors the codex backend seam: the same ephemeral tool-free CLI isolation as
the distill runtime backend, with the skill-candidate prompt/schema/parser and
``skill_candidate_*`` re-labeled failure codes. Hermetic via stub executables.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import sys

import pytest

from telegram_bot.memory.distill_extraction import DistillProvenance
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    TranscriptMessage,
)
from telegram_bot.memory.runtime_cli_backend import (
    RuntimeCliSkillCandidateBackend,
    RuntimeDistillBackendError,
)
from telegram_bot.memory.skill_candidate_backend import SkillCandidateBackendError

THREAD_HASH = hashlib.sha256(b"thread-667-runtime-skill").hexdigest()


def _snapshot() -> CodexTranscriptSnapshot:
    text = "run the release checklist again"
    return CodexTranscriptSnapshot(
        thread_hash=THREAD_HASH,
        last_turn_id="turn-1",
        messages=(TranscriptMessage("user", text, "2026-07-23T11:00:00Z"),),
        byte_count=len(text.encode("utf-8")),
        truncated=False,
        captured_at="2026-07-23T11:00:00Z",
    )


def _provenance(provider: str = "piri") -> DistillProvenance:
    return DistillProvenance.model_validate(
        {
            "provider": provider,
            "source_thread_hash": THREAD_HASH,
            "trigger": "checkpoint",
            "distilled_at": "2026-07-23T11:00:05Z",
        }
    )


def _stub(tmp_path: Path) -> Path:
    executable = tmp_path / "skill-extractor-stub"
    executable.write_text(
        f"""#!{sys.executable}
import json
import sys
value = json.load(sys.stdin)
skill_md = (
    "---\\nname: piri-release-check\\n"
    "description: Capture the recurring Piri release verification checklist procedure.\\n"
    "---\\n\\n# piri-release-check\\n\\n## Procedure\\n1. Step.\\n2. Verify.\\n3. Record.\\n4. Confirm.\\n5. Done.\\n"
)
json.dump({{
    "schema_version": 1,
    "provenance": {{
        "provider": value["provider"],
        "source_thread_hash": value["source_thread_hash"],
        "trigger": value["trigger"],
        "distilled_at": "2026-07-23T11:00:05Z",
    }},
    "candidates": [{{
        "name": "piri-release-check",
        "summary": "Capture the recurring Piri release verification checklist procedure.",
        "reason": "The session repeated the same release verification steps.",
        "evidence_excerpt": "release checklist",
        "skill_md": skill_md,
    }}],
}}, sys.stdout)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _failing_stub(tmp_path: Path) -> Path:
    executable = tmp_path / "failing-skill-extractor-stub"
    executable.write_text(
        f"""#!{sys.executable}
import sys
sys.exit(3)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def test_extract_returns_valid_candidate(tmp_path: Path) -> None:
    backend = RuntimeCliSkillCandidateBackend(
        "piri",
        executable=str(_stub(tmp_path)),
        temp_root=tmp_path,
    )
    result = asyncio.run(
        backend.extract(snapshot=_snapshot(), provenance=_provenance())
    )
    assert result.candidates[0].name == "piri-release-check"
    assert result.provenance.provider == "piri"


def test_thread_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    backend = RuntimeCliSkillCandidateBackend(
        "piri",
        executable=str(_stub(tmp_path)),
        temp_root=tmp_path,
    )
    drifted = _provenance().model_copy(update={"source_thread_hash": "b" * 64})
    with pytest.raises(SkillCandidateBackendError) as exc:
        asyncio.run(backend.extract(snapshot=_snapshot(), provenance=drifted))
    assert exc.value.code == "skill_candidate_input_invalid"


def test_nonzero_exit_is_relabeled(tmp_path: Path) -> None:
    backend = RuntimeCliSkillCandidateBackend(
        "piri",
        executable=str(_failing_stub(tmp_path)),
        temp_root=tmp_path,
    )
    with pytest.raises(SkillCandidateBackendError) as exc:
        asyncio.run(
            backend.extract(snapshot=_snapshot(), provenance=_provenance())
        )
    assert exc.value.code == "skill_candidate_nonzero_exit"
    assert exc.value.exit_status == 3


def test_config_validation_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(SkillCandidateBackendError) as exc:
        RuntimeCliSkillCandidateBackend(
            "codex",
            executable=str(_stub(tmp_path)),
            temp_root=tmp_path,
        )
    assert exc.value.code == "skill_candidate_config_invalid"


def test_runtime_error_codes_stay_distill_named() -> None:
    # The shared runner keeps the historical distill_* namespace; the relabel
    # belongs to the skill backend boundary only.
    assert RuntimeDistillBackendError("distill_timeout").code == "distill_timeout"
