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
    assert "Honcho disabled" in snapshot
    assert "Family Wiki disabled" in snapshot
