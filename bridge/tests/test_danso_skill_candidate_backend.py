"""Contract for the danso skill-candidate backend (#1662).

Mirrors the codex/runtime-CLI backend seam: the same ephemeral tool-free CLI
isolation as the danso distill backend, with the skill-candidate prompt/schema
/parser and ``skill_candidate_*`` failure codes. The packet rides in
--system-context-file (danso has no stdin prompt). Hermetic via a stub
executable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from telegram_bot.memory.distill_extraction import DistillProvenance
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    TranscriptMessage,
)
from telegram_bot.memory.danso_backend import DansoSkillCandidateBackend
from telegram_bot.memory.skill_candidate_backend import SkillCandidateBackendError

THREAD_HASH = hashlib.sha256(b"thread-1662-danso-skill").hexdigest()

NL = chr(10)


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


def _provenance() -> DistillProvenance:
    return DistillProvenance.model_validate(
        {
            "provider": "danso",
            "source_thread_hash": THREAD_HASH,
            "trigger": "checkpoint",
            "distilled_at": "2026-07-23T11:00:05Z",
        }
    )


def _settings(danso: Path) -> SimpleNamespace:
    return SimpleNamespace(
        danso_model="gpt-6-astra",
        danso_cli_path=str(danso),
        danso_auth_mode="chatgpt",
        danso_chatgpt_auth_file="/synthetic/danso-auth.json",
        danso_chatgpt_base_url="",
    )


def _settle_danso_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the inventory builder a resolvable danso skills root (#1659)."""
    skills = tmp_path / "danso-skills"
    skills.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("DANSO_SKILLS_DIR", str(skills))


def _stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> Path:
    executable = tmp_path / "danso-skill-stub"
    capture = tmp_path / "captured-context.md"
    argv_file = tmp_path / "captured-argv.txt"
    body_lines = ["import sys"]
    if fail:
        body_lines.append("sys.exit(3)")
    else:
        skill_md = chr(10).join(
            [
                "---",
                "name: danso-release-check",
                "description: Capture the recurring danso release verification checklist procedure.",
                "---",
                "",
                "# danso-release-check",
                "",
                "## Procedure",
                "1. Step.",
                "2. Verify.",
                "3. Record.",
                "4. Confirm.",
                "5. Done.",
            ]
        )
        body_lines += [
            "ctx = ''",
            "prev = ''",
            "for index, arg in enumerate(argv):",
            "    if prev == '--system-context-file':",
            "        ctx = arg",
            "    prev = arg",
            "open(" + repr(str(capture)) + ", 'w').write(open(ctx).read())",
            "open(" + repr(str(argv_file)) + ", 'w').write(' '.join(argv))",
            "packet = json.loads(open(ctx).read().split('Untrusted transcript JSON:' + chr(10), 1)[1])",
            "skill_md = chr(10).join(" + repr(
                [
                    "---",
                    "name: danso-release-check",
                    "description: Capture the recurring danso release verification checklist procedure.",
                    "---",
                    "",
                    "# danso-release-check",
                    "",
                    "## Procedure",
                    "1. Step.",
                    "2. Verify.",
                    "3. Record.",
                    "4. Confirm.",
                    "5. Done.",
                ]
            ) + ")",
            "json.dump({",
            "    'schema_version': 1,",
            "    'provenance': {",
            "        'provider': packet['provider'],",
            "        'source_thread_hash': packet['source_thread_hash'],",
            "        'trigger': packet['trigger'],",
            "        'distilled_at': '2026-07-23T11:00:05Z',",
            "    },",
            "    'candidates': [{",
            "        'name': 'danso-release-check',",
            "        'summary': 'Capture the recurring danso release verification checklist procedure.',",
            "        'reason': 'The session repeated the same release verification steps.',",
            "        'evidence_excerpt': 'release checklist',",
            "        'skill_md': skill_md,",
            "    }],",
            "}, sys.stdout)",
        ]
    executable.write_text(
        "#!" + sys.executable + NL
        + "import json" + NL
        + "import os" + NL
        + "import sys" + NL
        + "argv = sys.argv[1:]" + NL
        + NL.join(body_lines) + NL,
        encoding="utf-8",
    )
    executable.chmod(0o700)
    # The auth-mode wiring must forward only the selected provider's material.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-not-forwarded")
    return executable


def test_extract_returns_valid_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _stub(tmp_path, monkeypatch)
    _settle_danso_env(tmp_path, monkeypatch)
    backend = DansoSkillCandidateBackend(
        _settings(executable),
        temp_root=tmp_path,
        timeout_seconds=10,
    )
    result = asyncio.run(
        backend.extract(snapshot=_snapshot(), provenance=_provenance())
    )
    assert result.candidates[0].name == "danso-release-check"
    assert result.provenance.provider == "danso"
    # The packet traveled via --system-context-file (never argv/stdin) and the
    # auth wiring forwarded only the selected provider's material.
    argv = (tmp_path / "captured-argv.txt").read_text()
    assert "--system-context-file" in argv
    assert "--no-tools" in argv
    assert "synthetic-not-forwarded" not in argv
    captured = (tmp_path / "captured-context.md").read_text()
    assert "Untrusted transcript JSON:" in captured
    assert json.loads(captured.split("Untrusted transcript JSON:" + NL, 1)[1])[
        "provider"
    ] == "danso"


def test_thread_hash_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _stub(tmp_path, monkeypatch)
    _settle_danso_env(tmp_path, monkeypatch)
    backend = DansoSkillCandidateBackend(
        _settings(executable),
        temp_root=tmp_path,
        timeout_seconds=10,
    )
    drifted = _provenance().model_copy(update={"source_thread_hash": "b" * 64})
    with pytest.raises(SkillCandidateBackendError) as exc:
        asyncio.run(backend.extract(snapshot=_snapshot(), provenance=drifted))
    assert exc.value.code == "skill_candidate_input_invalid"


def test_nonzero_exit_is_relabeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _stub(tmp_path, monkeypatch, fail=True)
    _settle_danso_env(tmp_path, monkeypatch)
    backend = DansoSkillCandidateBackend(
        _settings(executable),
        temp_root=tmp_path,
        timeout_seconds=10,
    )
    with pytest.raises(SkillCandidateBackendError) as exc:
        asyncio.run(backend.extract(snapshot=_snapshot(), provenance=_provenance()))
    assert exc.value.code == "skill_candidate_backend_failed"


def test_inventory_unresolved_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _stub(tmp_path, monkeypatch)
    monkeypatch.delenv("DANSO_SKILLS_DIR", raising=False)
    monkeypatch.delenv("CCC_DANSO_STATE_DIR", raising=False)
    backend = DansoSkillCandidateBackend(
        _settings(executable),
        temp_root=tmp_path,
        timeout_seconds=10,
    )
    with pytest.raises(SkillCandidateBackendError) as exc:
        asyncio.run(backend.extract(snapshot=_snapshot(), provenance=_provenance()))
    assert exc.value.code == "skill_candidate_inventory_failed"
