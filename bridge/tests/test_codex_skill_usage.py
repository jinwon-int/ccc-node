"""Success, privacy and bounded-failure evidence for Codex skill read capture."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

from telegram_bot.core.agent_runtime import ToolCompletedEvent, ToolStartedEvent
from telegram_bot.core.codex_skill_usage import CodexSkillReads, SkillUsageSink

ROOT = Path(__file__).resolve().parents[2]


def item(path="/example/skills/test-skill/SKILL.md", **overrides):
    return {"command": f"cat {path}", "cwd": "/example", "commandActions": [{"type": "read", "path": path}], "status": "completed", "exitCode": 0, "aggregatedOutput": "private skill body", **overrides}


def pair(tracker, data, key="tool-1", success=True):
    tracker.observe(ToolStartedEvent(key, "commandExecution", data))
    tracker.observe(ToolCompletedEvent(key, "commandExecution", data, success))


class ReadEvidenceTests(unittest.TestCase):
    def test_success_once_per_item_and_again_on_distinct_read(self):
        sink = Mock()
        tracker = CodexSkillReads(sink)
        for key in ("a", "a", "b"):
            pair(tracker, item(), key)
        self.assertEqual(sink.record.call_count, 2)
        sink.record.assert_called_with("test-skill")

    def test_failed_unknown_or_unexecuted_read_does_not_count(self):
        cases = [
            item(exitCode=1), item(exitCode=None), item(exitCode=False),
            item(status="failed"), item(aggregatedOutput=""),
            item(commandActions=[]), item(commandActions=[{"type": "search", "path": "/example/skills/test-skill/SKILL.md"}]),
            item(command="echo /example/skills/test-skill/SKILL.md"),
            item(command="false && cat /example/skills/test-skill/SKILL.md || true"),
            item(command="cat /example/skills/test-skill/SKILL.md >/dev/null"),
            item(command="cat --help /example/skills/test-skill/SKILL.md"),
            item(command="cat /example/skills/test-skill/SKILL.md; true"),
            item(path="/example/skills/test-skill/../SKILL.md"),
        ]
        for data in cases:
            with self.subTest(data=data):
                sink = Mock()
                pair(CodexSkillReads(sink), data)
                sink.record.assert_not_called()
        sink = Mock()
        pair(CodexSkillReads(sink), item(), success=False)
        sink.record.assert_not_called()

    def test_real_header_only_and_mismatched_reads_are_not_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "skills/example/SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("ACTUAL_SKILL_BODY\n")
            other = Path(raw) / "other.md"
            other.write_text("UNRELATED_BODY\n")
            for command in (f"head -v -n0 {path}", f"cat {other}"):
                run = subprocess.run(command, shell=True, check=True, text=True, capture_output=True)
                self.assertNotIn("ACTUAL_SKILL_BODY", run.stdout)
                sink = Mock()
                pair(CodexSkillReads(sink), item(str(path), command=command, aggregatedOutput=run.stdout))
                sink.record.assert_not_called()
            for command in (f"head -n 2 {path}", f"tail -n2 {path}", f"sed -n 1,80p {path}", f"cat -n {path}"):
                run = subprocess.run(command, shell=True, check=True, text=True, capture_output=True)
                self.assertIn("ACTUAL_SKILL_BODY", run.stdout)
                sink = Mock()
                pair(CodexSkillReads(sink), item(str(path), command=command, aggregatedOutput=run.stdout))
                sink.record.assert_called_once_with("example")

    def test_orphan_and_changed_completion_not_counted(self):
        sink = Mock()
        tracker = CodexSkillReads(sink)
        tracker.observe(ToolCompletedEvent("a", "commandExecution", item(), True))
        tracker.observe(ToolStartedEvent("b", "commandExecution", item()))
        tracker.observe(ToolCompletedEvent("b", "commandExecution", item("/example/skills/other/SKILL.md"), True))
        sink.record.assert_not_called()

    def test_relative_system_and_shell_wrapped_reads(self):
        sink = Mock()
        pair(CodexSkillReads(sink), item("SKILL.md", cwd="/example/skills/.system/test-skill", command="/bin/bash -lc 'sed -n 1,80p SKILL.md'"))
        sink.record.assert_called_once_with("test-skill")

    def test_memory_bounds_do_not_reenable_replayed_ids(self):
        sink = Mock()
        tracker = CodexSkillReads(sink)
        for i in range(300):
            pair(tracker, item(), str(i))
        pair(tracker, item(), "0")
        self.assertEqual(sink.record.call_count, 256)


class SinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_common_logger_is_body_free_and_scoped(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = {"HOME": raw, "PATH": os.environ["PATH"], "CCC_SKILL_USAGE_LOGGER": str(ROOT / "claude/hooks/skill-usage-log.sh")}
            owner = SkillUsageSink(env)
            pair(CodexSkillReads(owner), item())
            await owner.drain()
            ledger = root / ".claude/state/skill-usage/usage.jsonl"
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["runtime"], "codex")
            self.assertEqual(set(rows[0]), {"ts", "skill", "tool", "runtime"})
            self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private", ledger.read_text())
            scoped = root / "audiences/private-fixture/state"
            sink = SkillUsageSink({**env, "CCC_MEMORY_AUDIENCE_SCOPED": "1", "CCC_STATE_DIR": str(scoped)})
            pair(CodexSkillReads(sink), item())
            await sink.drain()
            self.assertTrue((scoped / "skill-usage/usage.jsonl").is_file())
            self.assertEqual(len(ledger.read_text().splitlines()), 1)

    async def test_invalid_scope_never_falls_back_to_owner(self):
        with tempfile.TemporaryDirectory() as raw:
            sink = SkillUsageSink({"HOME": raw, "CCC_MEMORY_AUDIENCE_SCOPED": "1"})
            sink.record("test-skill")
            await sink.drain()
            self.assertFalse((Path(raw) / ".claude").exists())

    async def test_slow_logger_is_bounded_and_does_not_block_observer(self):
        with tempfile.TemporaryDirectory() as raw:
            script = Path(raw) / "slow.sh"
            script.write_text("#!/bin/bash\nsleep 30\n")
            sink = SkillUsageSink({"HOME": raw, "PATH": os.environ["PATH"], "CCC_SKILL_USAGE_LOGGER": str(script)})
            with patch("telegram_bot.core.skill_usage._TIMEOUT_SECONDS", 0.05):
                for _ in range(20):
                    sink.record("test-skill")
                self.assertEqual(len(sink._tasks), 4)
                await asyncio.wait_for(sink.drain(), 2)
            self.assertFalse(sink._tasks)

    async def test_logger_descendants_are_killed_even_after_parent_success(self):
        with tempfile.TemporaryDirectory() as raw:
            pidfile = Path(raw) / "child-pids"
            script = Path(raw) / "spawn.sh"
            script.write_text(f"#!/bin/bash\nsleep 30 &\necho $! >> {pidfile}\nexit 0\n")
            sink = SkillUsageSink({"HOME": raw, "PATH": os.environ["PATH"], "CCC_SKILL_USAGE_LOGGER": str(script)})
            for _ in range(3):
                for _ in range(4):
                    sink.record("test-skill")
                await sink.drain()
            self.assertFalse(sink._tasks)
            for line in pidfile.read_text().splitlines():
                stat_path = Path("/proc") / line / "stat"
                # A killed orphan may briefly await reaping by container PID 1.
                for _ in range(20):
                    if not stat_path.exists() or stat_path.read_text().split()[2] == "Z":
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("logger descendant is still running")

    async def test_missing_logger_is_best_effort(self):
        with tempfile.TemporaryDirectory() as raw:
            sink = SkillUsageSink({"HOME": raw})
            pair(CodexSkillReads(sink), item())
            await sink.drain()
            self.assertFalse((Path(raw) / ".claude").exists())
