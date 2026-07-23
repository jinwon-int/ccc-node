"""Hermetic contract for the isolated Codex skill-candidate backend (#667).

No real Codex: a shell stub stands in for the ``codex`` executable and writes a
canned output to the ``--output-last-message`` path. Exercises success, the
redaction of the stdin payload, and the stable body-free failure codes shared
with the reused isolation runner.
"""

from __future__ import annotations

import asyncio
from functools import wraps
import hashlib
import json
from pathlib import Path
from typing import Awaitable, Callable, ParamSpec

import pytest

from telegram_bot.memory.distill_extraction import DistillProvenance
from telegram_bot.memory.distill_types import CodexTranscriptSnapshot, TranscriptMessage
from telegram_bot.memory.skill_candidate import SkillCandidateOutput
from telegram_bot.memory.skill_candidate_backend import (
    CodexExecSkillCandidateBackend,
    SkillCandidateBackendError,
    canonical_skill_candidate_input_bytes,
)

THREAD_HASH = hashlib.sha256(b"thread-667-backend").hexdigest()
P = ParamSpec("P")


def async_test(function: Callable[P, Awaitable[None]]) -> Callable[P, None]:
    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(function(*args, **kwargs))

    return wrapper


def _provenance() -> DistillProvenance:
    return DistillProvenance.model_validate(
        {
            "provider": "codex",
            "source_thread_hash": THREAD_HASH,
            "trigger": "checkpoint",
            "distilled_at": "2026-07-23T10:00:00Z",
        }
    )


def _snapshot(text: str = "run the release checklist again") -> CodexTranscriptSnapshot:
    return CodexTranscriptSnapshot(
        thread_hash=THREAD_HASH,
        last_turn_id="turn-1",
        messages=(TranscriptMessage("user", text, "2026-07-23T10:00:00Z"),),
        byte_count=len(text.encode("utf-8")),
        truncated=False,
        captured_at="2026-07-23T10:00:00Z",
    )


def _valid_output_json(*, trigger: str = "checkpoint") -> str:
    skill_md = (
        "---\nname: codex-release-check\n"
        "description: Capture the recurring Codex release verification checklist procedure.\n"
        "---\n\n# codex-release-check\n\n## Procedure\n1. Step.\n2. Verify.\n3. Record.\n4. Confirm.\n5. Done.\n"
    )
    return json.dumps(
        {
            "schema_version": 1,
            "provenance": {
                "provider": "codex",
                "source_thread_hash": THREAD_HASH,
                "trigger": trigger,
                "distilled_at": "2026-07-23T10:00:00Z",
            },
            "candidates": [
                {
                    "name": "codex-release-check",
                    "summary": "Capture the recurring Codex release verification checklist procedure.",
                    "reason": "The session repeated the same release verification steps.",
                    "evidence_excerpt": "release checklist",
                    "skill_md": skill_md,
                }
            ],
        }
    )


def _stub(tmp_path: Path, body: str) -> str:
    """A codex-exec stub. `body` is shell that may use $OUT (output path)."""
    script = (
        "#!/bin/sh\n"
        "OUT=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "--output-last-message" ]; then OUT="$2"; shift; fi\n'
        "  shift\n"
        "done\n"
        "cat >/dev/null\n"  # drain stdin
        f"{body}\n"
    )
    path = tmp_path / "codex"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)
    return str(path.resolve())


def _backend(tmp_path: Path, executable: str, **kw) -> CodexExecSkillCandidateBackend:
    return CodexExecSkillCandidateBackend(
        executable=executable, temp_root=str(tmp_path), timeout_seconds=10.0, **kw
    )


# --------------------------------------------------------------------------- #

def test_input_bytes_are_redacted() -> None:
    snap = _snapshot("token is ghp_abcdefghijklmnopqrstuvwxyz012345 do not leak")
    payload = canonical_skill_candidate_input_bytes(snap, _provenance())
    assert b"ghp_" not in payload
    assert b"[REDACTED]" in payload
    # Provenance-copy fields the model must echo are present.
    assert THREAD_HASH.encode() in payload


@async_test
async def test_backend_returns_validated_output(tmp_path: Path) -> None:
    output = _valid_output_json().replace("'", "'\\''")
    executable = _stub(tmp_path, f"printf '%s' '{output}' > \"$OUT\"\nexit 0")
    backend = _backend(tmp_path, executable)
    result = await backend.extract(snapshot=_snapshot(), provenance=_provenance())
    assert isinstance(result, SkillCandidateOutput)
    assert result.candidates[0].name == "codex-release-check"


@async_test
async def test_backend_accepts_model_generated_distilled_at(tmp_path: Path) -> None:
    # The model cannot know our distilled_at and generates its own; only the
    # identity fields (provider/thread hash/trigger) must be echoed. A differing
    # distilled_at must NOT be rejected (the real-node canary failure).
    payload = json.loads(_valid_output_json())
    payload["provenance"]["distilled_at"] = "2026-01-01T00:00:00Z"  # != our provenance
    output = json.dumps(payload).replace("'", "'\\''")
    executable = _stub(tmp_path, f"printf '%s' '{output}' > \"$OUT\"\nexit 0")
    backend = _backend(tmp_path, executable)
    result = await backend.extract(snapshot=_snapshot(), provenance=_provenance())
    assert result.candidates[0].name == "codex-release-check"


@async_test
async def test_backend_rejects_provenance_mismatch(tmp_path: Path) -> None:
    # Stub echoes a different trigger than the backend passed in.
    output = _valid_output_json(trigger="shutdown").replace("'", "'\\''")
    executable = _stub(tmp_path, f"printf '%s' '{output}' > \"$OUT\"\nexit 0")
    backend = _backend(tmp_path, executable)
    with pytest.raises(SkillCandidateBackendError) as exc:
        await backend.extract(snapshot=_snapshot(), provenance=_provenance())
    assert exc.value.code == "skill_candidate_output_invalid"


@async_test
async def test_backend_maps_nonzero_exit(tmp_path: Path) -> None:
    executable = _stub(tmp_path, "exit 3")
    backend = _backend(tmp_path, executable)
    with pytest.raises(SkillCandidateBackendError) as exc:
        await backend.extract(snapshot=_snapshot(), provenance=_provenance())
    assert exc.value.code == "skill_candidate_nonzero_exit"


@async_test
async def test_backend_maps_missing_output(tmp_path: Path) -> None:
    executable = _stub(tmp_path, "exit 0")  # writes nothing to $OUT
    backend = _backend(tmp_path, executable)
    with pytest.raises(SkillCandidateBackendError) as exc:
        await backend.extract(snapshot=_snapshot(), provenance=_provenance())
    assert exc.value.code == "skill_candidate_output_missing"


@async_test
async def test_backend_rejects_invalid_output(tmp_path: Path) -> None:
    executable = _stub(tmp_path, 'printf "not json" > "$OUT"\nexit 0')
    backend = _backend(tmp_path, executable)
    with pytest.raises(SkillCandidateBackendError) as exc:
        await backend.extract(snapshot=_snapshot(), provenance=_provenance())
    assert exc.value.code == "skill_candidate_output_invalid"


def test_backend_rejects_bad_config(tmp_path: Path) -> None:
    with pytest.raises(SkillCandidateBackendError):
        CodexExecSkillCandidateBackend(executable="codex", timeout_seconds=0)
    with pytest.raises(SkillCandidateBackendError):
        CodexExecSkillCandidateBackend(executable="codex", model="bad model name!")


def test_backend_rejects_snapshot_provenance_mismatch(tmp_path: Path) -> None:
    executable = _stub(tmp_path, "exit 0")
    backend = _backend(tmp_path, executable)
    bad = DistillProvenance.model_validate(
        {
            "provider": "codex",
            "source_thread_hash": "e" * 64,
            "trigger": "checkpoint",
            "distilled_at": "2026-07-23T10:00:00Z",
        }
    )
    with pytest.raises(SkillCandidateBackendError) as exc:
        asyncio.run(backend.extract(snapshot=_snapshot(), provenance=bad))
    assert exc.value.code == "skill_candidate_input_invalid"
