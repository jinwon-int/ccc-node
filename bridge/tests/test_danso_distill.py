"""Offline automatic Danso memory boundaries; no real provider credentials."""

import asyncio
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from telegram_bot.memory.danso_backend import DansoDistillBackend, _run
from telegram_bot.memory.danso_snapshot import read_danso_snapshot
from telegram_bot.memory.distill_extraction import build_extraction_input
from telegram_bot.memory.distill_types import (
    DistillTrigger,
    SnapshotUnavailableError,
    TranscriptBounds,
)
from telegram_bot.memory.runtime_cli_backend import RuntimeDistillBackendError


def journal(root, cwd=Path("/fixture")):
    root.mkdir(mode=0o700)
    ident = str(uuid.uuid4())
    path = root / (ident + ".jsonl")
    rows = [
        {"type": "session", "version": 3, "id": str(uuid.uuid4()), "cwd": str(cwd)},
        {
            "type": "message",
            "id": "user-1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {
                "role": "user",
                "content": "Remember my preference for concise Korean answers.",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    return ident, path


def test_snapshot_pins_scope_identity_permissions_and_lock(tmp_path):
    root = tmp_path / "private"
    ident, path = journal(root)
    result = read_danso_snapshot(root, ident, bounds=TranscriptBounds(), cwd=Path("/fixture"))
    assert result.thread_hash == hashlib.sha256(ident.encode()).hexdigest()
    assert result.messages[0].role == "user"
    with path.open() as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            read_danso_snapshot(root, ident, bounds=TranscriptBounds(), cwd=Path("/fixture"))
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_danso_snapshot(root, ident, bounds=TranscriptBounds(), cwd=Path("/fixture"))
    path.chmod(0o600)
    link = tmp_path / "alias"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        read_danso_snapshot(link, ident, bounds=TranscriptBounds(), cwd=Path("/fixture"))
    with pytest.raises(SnapshotUnavailableError):
        read_danso_snapshot(
            tmp_path / "shared", ident, bounds=TranscriptBounds(), cwd=Path("/fixture")
        )
    with pytest.raises(ValueError):
        read_danso_snapshot(root, "../escape", bounds=TranscriptBounds(), cwd=Path("/fixture"))


@pytest.mark.parametrize("damage", ["partial", "unresolved", "wrong_identity", "file_link"])
def test_snapshot_rejects_partial_unresolved_or_redirected(tmp_path, damage):
    root = tmp_path / "private"
    ident, path = journal(root)
    if damage == "partial":
        path.write_bytes(path.read_bytes()[:-1])
    elif damage == "unresolved":
        with path.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "id": "call-1",
                                    "name": "bash",
                                    "arguments": {"command": "touch marker"},
                                }
                            ],
                        },
                    }
                )
                + "\n"
            )
    elif damage == "wrong_identity":
        path.write_text(path.read_text().replace("/fixture", "/wrong-workspace"))
    else:
        backup = tmp_path / "original"
        path.rename(backup)
        path.symlink_to(backup)
    with pytest.raises((ValueError, OSError, SnapshotUnavailableError)):
        read_danso_snapshot(root, ident, bounds=TranscriptBounds(), cwd=Path("/fixture"))


def output(data):
    return {
        "schema_version": 1,
        "provenance": {
            "provider": data.provider,
            "source_thread_hash": data.source_thread_hash,
            "trigger": data.trigger.value,
            "distilled_at": "2026-09-07T12:00:00Z",
        },
        "honcho": [],
        "wiki_candidates": [],
        "resume": {
            "last_activity": "Checked memory",
            "pending_action": "",
            "awaiting_user": False,
            "open_question": "",
            "next_step": "Continue",
            "evidence": [],
        },
    }


@pytest.mark.anyio
@pytest.mark.parametrize("escaped", [False, True])
async def test_extractor_uses_only_explicit_auth_and_tool_free_private_input(tmp_path, monkeypatch, escaped):
    ident, _ = journal(tmp_path / "private")
    snapshot = read_danso_snapshot(
        tmp_path / "private", ident, bounds=TranscriptBounds(), cwd=Path("/fixture")
    )
    data = build_extraction_input(snapshot, provider="danso", trigger=DistillTrigger.CHECKPOINT)
    if escaped:
        from telegram_bot.memory.distill_extraction import DistillExtractionInput
        value = data.model_dump(mode="json")
        text = "Remember my preference" + "\x00" * 8000
        value.update(messages=[{"role": "user", "text": text}], message_count=1, byte_count=len(text.encode()))
        data = DistillExtractionInput.model_validate(value)
    script = tmp_path / "danso"
    marker = tmp_path / "capture.json"
    script.write_text(
        "#!/usr/bin/python3\nimport os,sys,json,pathlib\na=sys.argv\n"
        'p=pathlib.Path(a[a.index("--system-context-file")+1])\n'
        "assert p.stat().st_mode & 0o777 == 0o600\n"
        "assert len(p.read_bytes()) <= 32768\n"
        f"assert json.loads(p.read_text().split('Untrusted transcript JSON:\\n',1)[1])['truncated'] is {escaped!r}\n"
        'assert "Remember my preference" in p.read_text()\n'
        'assert "Remember my preference" not in " ".join(a)\n'
        'assert "--no-tools" in a and a[a.index("--max-turns")+1]=="1"\n'
        'assert "TELEGRAM_BOT_TOKEN" not in os.environ\n'
        'assert os.environ["DANSO_CHATGPT_AUTH_FILE"]=="/synthetic/danso-auth.json"\n'
        "assert not list(pathlib.Path.cwd().iterdir())\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(p.parent))\n"
        f"print({json.dumps(output(data))!r})\n"
    )
    script.chmod(0o700)
    settings = SimpleNamespace(
        danso_model="gpt-6-astra",
        danso_cli_path=str(script),
        danso_auth_mode="chatgpt",
        danso_chatgpt_auth_file="/synthetic/danso-auth.json",
        danso_chatgpt_base_url="",
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-not-forwarded")
    backend = DansoDistillBackend(
        settings, wiki_enabled=False, model="gpt-5.6-luna", timeout_seconds=5
    )
    result = await backend.extract(data)
    assert result.provenance.provider == "danso"
    assert not Path(marker.read_text()).exists()  # Only generated scratch is removed.


@pytest.mark.anyio
@pytest.mark.parametrize("body", ['print("x"*70000)', "import time;time.sleep(20)"])
async def test_extractor_output_and_timeout_are_bounded(tmp_path, body):
    script = tmp_path / "fake"
    script.write_text("#!/usr/bin/python3\n" + body + "\n")
    script.chmod(0o700)
    with pytest.raises(RuntimeDistillBackendError):
        await _run([str(script)], {"PATH": os.defpath}, tmp_path, 0.2)


@pytest.mark.anyio
async def test_extractor_cancellation_reaps_child(tmp_path):
    script = tmp_path / "fake"
    pid = tmp_path / "pid"
    script.write_text(
        "#!/usr/bin/python3\nimport os,time\n"
        + f'open({str(pid)!r},"w").write(str(os.getpid()))\n'
        + "time.sleep(20)\n"
    )
    script.chmod(0o700)
    task = asyncio.create_task(_run([str(script)], {"PATH": os.defpath}, tmp_path, 10))
    async with asyncio.timeout(3):
        while not pid.exists():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_danso_facts_persist_idempotently_in_the_bound_audience(tmp_path):
    from telegram_bot.memory.distill_extraction import DistillExtractionOutput
    from telegram_bot.memory.distill_local_sink import CodexLocalMemorySink

    ident, _ = journal(tmp_path / "journal")
    data = build_extraction_input(
        read_danso_snapshot(
            tmp_path / "journal", ident, bounds=TranscriptBounds(), cwd=Path("/fixture")
        ),
        provider="danso",
        trigger=DistillTrigger.CHECKPOINT,
    )
    value = output(data)
    value["honcho"] = [
        {"kind": "preference", "text": "Prefer concise Korean answers.", "subject": "user"}
    ]
    validated = DistillExtractionOutput.model_validate(value)
    private = tmp_path / "audiences" / "private" / "state"
    first = CodexLocalMemorySink(private, audience="private").write(validated, job_id="a" * 64)
    second = CodexLocalMemorySink(private, audience="private").write(validated, job_id="a" * 64)
    assert first.facts_added == 1 and second.facts_added == 0
    fact = json.loads((private / "memory-facts.jsonl").read_text())
    assert fact["source"]["provider"] == "danso" and fact["privacy"] == "private"
    assert not (tmp_path / "audiences" / "shared").exists()
    assert "provider=danso" in (private / "resume.md").read_text()

@pytest.mark.parametrize("text", ["\x00" * 8192, "\x01가" * 2048])
def test_context_fits_escaped_json_and_preserves_provenance(tmp_path, text):
    from telegram_bot.memory.danso_backend import _context_bytes
    from telegram_bot.memory.distill_extraction import DistillExtractionInput
    ident, _ = journal(tmp_path / "journal")
    snapshot = read_danso_snapshot(tmp_path / "journal", ident, bounds=TranscriptBounds(), cwd=Path("/fixture"))
    original = build_extraction_input(snapshot, provider="danso", trigger=DistillTrigger.CHECKPOINT)
    data = original.model_dump(mode="json")
    data.update(messages=[{"role":"user", "text":text}], message_count=1, byte_count=len(text.encode()))
    value = DistillExtractionInput.model_validate(data)
    encoded = _context_bytes(value, "p" * 20000, "{}")
    assert len(encoded) <= 32768
    result = DistillExtractionInput.model_validate_json(encoded.split(b"Untrusted transcript JSON:\n", 1)[1])
    assert result.truncated and 0 < result.byte_count < value.byte_count
    assert text.startswith(result.messages[0].text)
    assert result.source_thread_hash == value.source_thread_hash
    assert result.trigger == value.trigger


def test_snapshot_native_header_uuid_is_independent(tmp_path):
    ident, path = journal(tmp_path / "journal")
    assert json.loads(path.read_text().splitlines()[0])["id"] != ident
    snapshot = read_danso_snapshot(path.parent, ident, bounds=TranscriptBounds(), cwd=Path("/fixture"))
    assert snapshot.thread_hash == hashlib.sha256(ident.encode()).hexdigest()


@pytest.mark.anyio
@pytest.mark.parametrize("diagnostic,code", [
    ("HTTP 401 unauthorized", "distill_auth_unavailable"),
    ("HTTP 429 too many requests", "distill_rate_limited"),
    ("usage limit reached", "distill_quota_exhausted"),
    ("unknown model", "distill_model_unavailable"),
])
async def test_native_failure_trips_guard_without_persisting_stderr(tmp_path, diagnostic, code):
    from test_distill_worker import snapshot_done_job
    from telegram_bot.memory.distill_journal import DistillJournal
    from telegram_bot.memory.distill_guard import DistillGuard, COOLDOWN_ACTIVE_CODE
    from telegram_bot.memory.distill_worker import CodexDistillExtractionWorker
    from telegram_bot.memory.distill_types import DistillJobStatus
    from telegram_bot.core.usage_meter import UsageMeter
    marker = tmp_path / "calls"
    script = tmp_path / "danso"
    script.write_text(
        "#!/usr/bin/python3\nimport sys,pathlib\n"
        f"p=pathlib.Path({str(marker)!r});p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        f"print({(diagnostic+' PRIVATE_DIAGNOSTIC_FIXTURE')!r},file=sys.stderr)\nsys.exit(1)\n"
    )
    script.chmod(0o700)
    settings = SimpleNamespace(danso_model="gpt-6-astra", danso_cli_path=str(script),
        danso_auth_mode="api-key", openai_api_key="synthetic", danso_base_url="")
    backend = DansoDistillBackend(settings, wiki_enabled=False, model="gpt-5.6-luna", timeout_seconds=5)
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    first = snapshot_done_job(journal, provider="danso", thread_id="first")
    second = snapshot_done_job(journal, provider="danso", thread_id="second")
    guard = DistillGuard(state_dir=tmp_path / "guard")
    worker = CodexDistillExtractionWorker(journal, backend,
        usage_meter=UsageMeter(tmp_path / "usage.json", budgets={"danso":500000}),
        guard=guard, extractor_provider="danso", model="gpt-5.6-luna", wiki_enabled=False)
    failed = await worker.extract_once(job_id=first.job_id)
    deferred = await worker.extract_once(job_id=second.job_id)
    assert failed.error_code == code
    assert guard.decision("danso", "gpt-5.6-luna").code == COOLDOWN_ACTIVE_CODE
    assert deferred.status is DistillJobStatus.SNAPSHOT_DONE and deferred.extraction_attempts == 0
    assert marker.read_text() == "x"
    for path in list(journal.root.glob("*.json")) + list(guard.cooldown_dir.glob("*.json")):
        assert "PRIVATE_DIAGNOSTIC_FIXTURE" not in path.read_text()


@pytest.mark.anyio
async def test_zai_auth_mode_passes_only_glm_credentials(tmp_path, monkeypatch):
    ident, _ = journal(tmp_path / "private")
    snapshot = read_danso_snapshot(
        tmp_path / "private", ident, bounds=TranscriptBounds(), cwd=Path("/fixture")
    )
    data = build_extraction_input(snapshot, provider="danso", trigger=DistillTrigger.CHECKPOINT)
    captured = tmp_path / "environment.json"
    script = tmp_path / "danso"
    script.write_text(
        "#!/usr/bin/python3\nimport os,sys,json,pathlib\n"
        f"p=pathlib.Path({str(captured)!r})\n"
        "p.write_text(json.dumps({k: os.environ.get(k) for k in "
        "('ZAI_API_KEY','DANSO_GLM_BASE_URL','DANSO_GLM_ENDPOINT','OPENAI_API_KEY','TELEGRAM_BOT_TOKEN')}))\n"
        f"print({json.dumps(output(data))!r})\n"
    )
    script.chmod(0o700)
    settings = SimpleNamespace(
        danso_model="glm-5.3-flash",
        danso_cli_path=str(script),
        danso_auth_mode="zai",
        zai_api_key="synthetic-zai-key",
        danso_glm_base_url="",
        danso_glm_endpoint="coding",
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-not-forwarded")
    backend = DansoDistillBackend(settings, wiki_enabled=False, model="provider-default", timeout_seconds=5)
    result = await backend.extract(data)
    assert result.provenance.provider == "danso"
    child = json.loads(captured.read_text())
    assert child["ZAI_API_KEY"] == "synthetic-zai-key"
    assert child["DANSO_GLM_ENDPOINT"] == "coding"
    assert child["OPENAI_API_KEY"] is None, "openai credentials must not leak into the zai lane"
    assert child["TELEGRAM_BOT_TOKEN"] is None
