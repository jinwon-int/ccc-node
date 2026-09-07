"""Hermetic Codex local write-back to next-snapshot round trip (#465)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_distill_local_journal import extracted_job

from telegram_bot.core.memory_audience import MemoryAudience
from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_local_worker import CodexDistillLocalSinkWorker
from telegram_bot.memory.distill_types import DistillLocalSinkStatus


ROOT = Path(__file__).resolve().parents[2]
FACT = "A harmless fact was retained."


@pytest.mark.anyio
async def test_thread_a_fact_appears_once_in_isolated_thread_b_snapshot(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    audience_root = tmp_path / "audiences"
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=audience_root,
        owner_token="roundtrip-local-worker",
        indexer_path=ROOT / "scripts" / "ccc-memory-index.sh",
    )

    written = await worker.write_once(job_id=job.job_id)

    assert written.local_sink_status is DistillLocalSinkStatus.DONE
    scope = str(job.memory_scope)
    audience = MemoryAudience("private", scope, audience_root)
    assert (audience.state_dir / "memory-index.sqlite").is_file()

    settings = SimpleNamespace(
        claude_settings_path=tmp_path / "legacy" / ".claude" / "settings.json",
        codex_audience_auth_mode="keyring",
    )
    environment = os.environ.copy()
    environment.update(audience.codex_environment(settings))
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "PROJECT_ROOT": str(ROOT),
            "CCC_CODEX_MEMORY_LOADER": str(ROOT / "claude" / "hooks" / "load-memory.sh"),
            "CCC_HOOK_DIR": str(ROOT / "claude" / "hooks"),
            "CCC_MEMORY_TOOLS_DIR": str(ROOT / "scripts"),
            "CCC_MEMORY_NO_REFRESH": "1",
            "CCC_LOCAL_MEMORY_ENABLED": "1",
            "CCC_CODEX_MEMORY_MAX_BYTES": "8192",
            "CCC_CODEX_AGENTS_BUDGET_BYTES": "16384",
        }
    )

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ccc_codex_memory.py"), "materialize", "--json"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert FACT not in completed.stdout
    assert FACT not in completed.stderr
    assert json.loads(completed.stdout)["status"] in {"updated", "unchanged"}
    snapshot = (audience.codex_home / "AGENTS.md").read_text()
    assert snapshot.count(FACT) == 1
    assert "Family Wiki disabled" in snapshot


@pytest.mark.anyio
async def test_piri_thread_a_fact_appears_in_next_audience_bootstrap(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal, provider="piri")
    audience_root = tmp_path / "audiences"
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=audience_root,
        owner_token="piri-roundtrip-local-worker",
        indexer_path=ROOT / "scripts" / "ccc-memory-index.sh",
    )

    written = await worker.write_once(job_id=job.job_id)

    assert written.local_sink_status is DistillLocalSinkStatus.DONE
    scope = str(job.memory_scope)
    audience = MemoryAudience("private", scope, audience_root)
    local_facts = (audience.state_dir / "memory-facts.jsonl").read_text()
    assert '"provider":"piri"' in local_facts
    settings = SimpleNamespace(
        claude_settings_path=tmp_path / "legacy" / ".claude" / "settings.json",
    )
    environment = os.environ.copy()
    environment.update(audience.piri_environment(settings))
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "PROJECT_ROOT": str(ROOT),
            "CODEX_HOME": str(audience.piri_bootstrap_home),
            "CODEX_SQLITE_HOME": str(audience.piri_bootstrap_home),
            "CCC_MEMORY_MATERIALIZER_PROVIDER": "piri",
            "CCC_CODEX_MEMORY_LOADER": str(ROOT / "claude" / "hooks" / "load-memory.sh"),
            "CCC_HOOK_DIR": str(ROOT / "claude" / "hooks"),
            "CCC_MEMORY_TOOLS_DIR": str(ROOT / "scripts"),
            "CCC_MEMORY_NO_REFRESH": "1",
            "CCC_LOCAL_MEMORY_ENABLED": "1",
            "CCC_CODEX_MEMORY_MAX_BYTES": "8192",
            "CCC_CODEX_AGENTS_BUDGET_BYTES": "16384",
        }
    )

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ccc_codex_memory.py"), "materialize", "--json"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    context = (audience.piri_bootstrap_home / "AGENTS.md").read_text()
    assert context.count(FACT) == 1


@pytest.mark.anyio
async def test_danso_saved_fact_reaches_actual_next_materializer_once(tmp_path, monkeypatch):
    from telegram_bot.core.danso_memory import prepare_memory_context
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    from test_distill_worker import snapshot_done_job, SuccessfulBackend
    from test_distill_local_journal import PRIVATE_SCOPE
    from telegram_bot.memory.distill_guard import DistillGuard
    from telegram_bot.memory.distill_worker import CodexDistillExtractionWorker
    from telegram_bot.memory.distill_types import DistillJobStatus
    from telegram_bot.core.usage_meter import UsageMeter
    job = snapshot_done_job(journal, provider="danso", memory_audience="private", memory_scope=PRIVATE_SCOPE)
    backend = SuccessfulBackend()
    meter = UsageMeter(tmp_path / "usage.json", budgets={"danso": 500000})
    extractor = CodexDistillExtractionWorker(journal, backend, usage_meter=meter,
        guard=DistillGuard(state_dir=tmp_path / "guard"), extractor_provider="danso",
        model="gpt-5.6-luna", wiki_enabled=False)
    job = await extractor.extract_once(job_id=job.job_id)
    assert job.status is DistillJobStatus.EXTRACTION_DONE
    assert job.extraction_attempts == 1 and len(backend.calls) == 1
    repeated = await extractor.extract_once(job_id=job.job_id)
    assert repeated.status is DistillJobStatus.EXTRACTION_DONE and len(backend.calls) == 1
    audience_root = tmp_path / "audiences"
    worker = CodexDistillLocalSinkWorker(journal, audience_root=audience_root,
        indexer_path=ROOT / "scripts" / "ccc-memory-index.sh")
    written = await worker.write_once(job_id=job.job_id)
    assert written.local_sink_status is DistillLocalSinkStatus.DONE
    audience = MemoryAudience("private", str(job.memory_scope), audience_root)
    settings = SimpleNamespace(claude_settings_path=tmp_path / "home" / ".claude" / "settings.json",
        codex_memory_materializer_path=str(ROOT / "scripts" / "ccc_codex_memory.py"),
        codex_memory_bootstrap_timeout_seconds=14)
    import shutil
    hooks = settings.claude_settings_path.parent / "hooks"
    hooks.mkdir(mode=0o700, parents=True)
    for source in list((ROOT / "scripts").glob("ccc-memory-*.sh")) + list((ROOT / "scripts").glob("ccc_memory*.py")):
        shutil.copy2(source, hooks / source.name)
    monkeypatch.delenv("CCC_CODEX_MEMORY_LOADER", raising=False)
    path = await prepare_memory_context(settings, audience)
    assert path.read_text().count(FACT) == 1
    shared = await prepare_memory_context(settings, MemoryAudience("shared", "shared", audience_root))
    assert FACT not in shared.read_text()
