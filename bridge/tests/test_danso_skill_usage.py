"""Native read correlation, privacy and real subprocess/common-logger wiring."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from telegram_bot.core.agent_runtime import SessionRequest
from telegram_bot.core.danso_progress import ProgressDecoder
from telegram_bot.core.danso_runtime import build_danso_runtime
from telegram_bot.core.danso_skill_usage import DansoSkillReads
from telegram_bot.core.memory_audience import MemoryAudience
from telegram_bot.core.skill_usage import SkillUsageSink
from test_danso_runtime import configured, anyio_backend  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def call(ident="c1", name="read", **arguments):
    return {"type": "toolCall", "id": ident, "name": name,
            "arguments": {"path": "/private/skills/example-skill/SKILL.md", **arguments}}


def interim(*calls):
    return {"type": "message", "message": {"role": "assistant", "stopReason": "toolUse", "content": list(calls)}}


def progress(phase, tool="read", sequence=1, **fields):
    return {"type": "danso_progress", "version": 1, "sequence": sequence, "phase": phase, "tool": tool, **fields}


def result(**fields):
    return {"type": "message", "message": {
        "role": "toolResult", "toolCallId": "c1", "toolName": "read", "isError": False,
        "content": [{"type": "text", "text": "PRIVATE_SKILL_BODY"}], **fields}}


def finish():
    return {"type": "message", "message": {"role": "assistant", "stopReason": "stop",
            "content": [{"type": "text", "text": "done"}]}}


def frames():
    return [{"type": "session", "version": 3}, interim(call()), progress("started"),
            result(), progress("settled", success=True), finish()]


def run(records):
    sink = Mock()
    observer = DansoSkillReads(sink, "/workspace")
    decoder = ProgressDecoder(observer)
    events = [decoder.feed(json.dumps(record)) for record in records]
    assert "PRIVATE_SKILL_BODY" not in repr(events)
    assert "/private/" not in repr(events) and "example-skill" not in repr(events)
    return sink, observer, decoder


def test_native_success_is_counted_once_without_bodies_or_paths():
    sink, observer, decoder = run(frames())
    sink.record.assert_called_once_with("example-skill")
    assert observer.active is None and not observer.pending
    assert decoder.finish() == b"done"


@pytest.mark.parametrize("change", [
    {"isError": True}, {"isError": 0}, {"isError": "false"},
    {"toolCallId": "other"}, {"toolName": "bash"},
    {"content": []}, {"content": [{"type": "text", "text": " \n"}]},
    {"content": [{"type": "image", "text": "body"}]},
])
def test_failed_empty_or_unmatched_result_does_not_count(change):
    records = frames()
    records[3] = result(**change)
    run(records)[0].record.assert_not_called()


@pytest.mark.parametrize("arguments", [
    {"path": "/skills/example-skill/README.md"}, {"path": "/tmp/example-skill/SKILL.md"},
    {"path": "/skills/example-skill/../example-skill/SKILL.md"}, {"path": None},
    {"path": "/skills/UPPER/SKILL.md"}, {"offset": True}, {"limit": 0},
])
def test_path_mentions_and_invalid_reads_are_not_evidence(arguments):
    records = frames()
    records[1] = interim(call(**arguments))
    run(records)[0].record.assert_not_called()


@pytest.mark.parametrize("path", ["skills/example-skill/SKILL.md", "/x/skills/.system/example-skill/SKILL.md"])
def test_relative_and_system_skill_paths(path):
    records = frames()
    records[1] = interim(call(path=path))
    run(records)[0].record.assert_called_once_with("example-skill")


@pytest.mark.parametrize("body,count", [
    ("[read: lines 1-2 of 4; next offset 3]\nactual content", 1),
    ("[read: lines 1-2 of 2; EOF]\nactual content", 1),
    ("[read: lines 1-2 of 2; EOF]\n \n", 0),
    ("[read: no lines at offset 3; 2 total lines; EOF]\n", 0),
    ("unrecognized header\nbody", 0),
])
def test_ranged_reads_require_actual_content(body, count):
    records = frames()
    records[1] = interim(call(limit=2))
    records[3] = result(content=[{"type": "text", "text": body}])
    assert run(records)[0].record.call_count == count


def test_incomplete_duplicate_failed_and_orphan_events():
    original = frames()
    for records in (original[:3], original[:3] + original[4:],
                    original[:4] + [original[3]] + original[4:],
                    original[:4] + [progress("settled", success=False), finish()],
                    [original[0]] + original[2:]):
        run(records)[0].record.assert_not_called()


def test_batch_order_other_tools_duplicates_and_bounds():
    records = [frames()[0], interim(call(ident="bash", name="bash"), call()),
               progress("started", tool="bash"), result(toolCallId="bash", toolName="bash"),
               progress("settled", tool="bash", success=True), progress("started", sequence=2),
               result(), progress("settled", sequence=2, success=True),
               interim(call()), progress("started", sequence=3), result(),
               progress("settled", sequence=3, success=True), finish()]
    sink, observer, _ = run(records)
    sink.record.assert_called_once_with("example-skill")
    assert observer.disabled
    for calls in ([call(), call()], [call(ident=str(i)) for i in range(65)]):
        sink, observer, _ = run([frames()[0], interim(*calls)])
        assert observer.disabled and not observer.pending and not observer.seen
        sink.record.assert_not_called()


def test_observer_failure_does_not_change_conversation():
    observer = Mock()
    observer.observe.side_effect = RuntimeError("telemetry unavailable")
    decoder = ProgressDecoder(observer)
    for record in frames():
        decoder.feed(json.dumps(record))
    assert decoder.finish() == b"done"


@pytest.mark.anyio
async def test_real_subprocess_writes_common_ledger_from_host_home(configured, tmp_path, monkeypatch):  # noqa: F811
    host = tmp_path / "host"
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("CCC_SKILL_USAGE_LOGGER", str(ROOT / "claude/hooks/skill-usage-log.sh"))
    binary = Path(configured.danso_cli_path)
    usage = {"requests": 1, "inputTokens": 1, "outputTokens": 1,
             "cacheReadTokens": 0, "cacheWriteTokens": 0, "totalTokens": 2}
    script = "#!/usr/bin/python3\nimport sys\n"
    script += "if '--help' in sys.argv:\n print('--progress-jsonl')\nelse:\n"
    script += " print(" + repr("\n".join(json.dumps(f) for f in frames())) + ")\n"
    for prefix in ("DANSO_USAGE", "PIRI_USAGE"):
        script += " print(" + repr(prefix + "=" + json.dumps(usage)) + ", file=sys.stderr)\n"
    binary.write_text(script)
    settings = configured.model_copy(update={"claude_settings_path": host / ".claude/settings.json"})
    runtime = build_danso_runtime(settings)
    assert runtime.progress_jsonl
    assert "CCC_CLAUDE_DIR" not in runtime.environment
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    events = [e async for e in session.send_turn("read fixture")]
    assert events[-1].kind == "completion", events
    ledger = host / ".claude/state/skill-usage/usage.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["runtime"] == "danso" and rows[0]["skill"] == "example-skill"
    assert set(rows[0]) == {"ts", "runtime", "tool", "skill"}
    assert not list(Path(runtime.environment["HOME"]).rglob("usage.jsonl"))


@pytest.mark.anyio
async def test_audience_wiring_preserves_host_logger_and_shared_budget(configured, tmp_path, monkeypatch):  # noqa: F811
    host = tmp_path / "host"
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("CCC_SKILL_USAGE_LOGGER", str(ROOT / "claude/hooks/skill-usage-log.sh"))
    settings = configured.model_copy(update={
        "claude_settings_path": host / ".claude/settings.json", "bridge_memory_mode": "audience-scoped",
        "bridge_memory_audience_root": str(tmp_path / "audiences"),
        "codex_memory_materializer_path": str(ROOT / "scripts/ccc_codex_memory.py"),
    })
    runtime = build_danso_runtime(settings)
    for scope, kind in (("private-" + "a"*32, "private"), ("shared", "shared")):
        audience = MemoryAudience(kind, scope, Path(settings.bridge_memory_audience_root))
        session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace,
            memory_environment=audience.danso_environment(settings)))
        sink = session.runtime.skill_usage_sink
        assert sink._tasks is runtime.skill_usage_sink._tasks
        sink.record("example-skill")
        await sink.drain()
        ledger = Path(audience.hook_environment(settings)["CCC_STATE_DIR"]) / "skill-usage/usage.jsonl"
        assert json.loads(ledger.read_text())["runtime"] == "danso"
    assert not (host / ".claude/state/skill-usage/usage.jsonl").exists()


@pytest.mark.anyio
async def test_invalid_scope_and_missing_logger_disable_capture(tmp_path):
    sink = SkillUsageSink({"HOME": str(tmp_path), "PATH": os.defpath,
        "CCC_MEMORY_AUDIENCE_SCOPED": "1", "CCC_SKILL_USAGE_LOGGER": str(ROOT / "claude/hooks/skill-usage-log.sh")}, runtime="danso")
    sink.record("example-skill")
    await sink.drain()
    assert not list(tmp_path.rglob("usage.jsonl"))
