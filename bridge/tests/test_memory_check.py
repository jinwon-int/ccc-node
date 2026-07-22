"""Body-free diagnostics for Codex write-back sink state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from test_distill_local_journal import extracted_job

from telegram_bot.memory.distill_journal import DistillJournal


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.anyio
async def test_memory_check_aggregates_wiki_retries_without_candidate_body(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    claimed = journal.claim_wiki_sink(job.job_id, owner_token="wiki-worker")
    assert claimed is not None
    journal.mark_wiki_sink_retryable_failed(
        job.job_id,
        owner_token="wiki-worker",
        lease_epoch=claimed.wiki_sink_lease_epoch,
        error_code="wiki_sink_io_failed",
    )
    environment = {
        **os.environ,
        "CCC_DISTILL_JOURNAL_DIR": str(journal.root),
        "CCC_MEMORY_CACHE_DIR": str(tmp_path / "missing-cache"),
        "CCC_STATE_DIR": str(tmp_path / "missing-state"),
        "CCC_CODEX_MEMORY_MATERIALIZER_PATH": str(tmp_path / "missing-helper"),
        "CCC_MEMORY_CHECK_NOW_EPOCH": "1785312000",
    }

    completed = subprocess.run(
        [str(REPO_ROOT / "scripts" / "ccc-memory-check.sh"), "--json"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    result = json.loads(completed.stdout)["writeback_queue"]
    assert result["status"] == "degraded"
    assert result["pending_jobs"] == 1
    assert result["retries"]["wiki"] == 1
    assert result["wiki_status_counts"] == {"retryable_failed": 1}
    assert "A harmless candidate summary" not in completed.stdout

