#!/usr/bin/env python3
"""A cut owner notice keeps a full local copy and names it (#2324 (3), option B).

The owner spool body keeps at most 900 characters per stream. Before this,
the rest was dropped silently: two scheduled verdicts in 2026-10 reached the
owner cut short and had to be rebuilt from the journal. Now the full,
identically redacted body goes to an owner-only 0600 file under a 0700
directory next to the task store, the notice names that file, and the newest
FULL_OUTPUT_KEEP files are kept. Nothing is written anywhere remote.
"""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_cron

SECRET = "ghp_" + "Q" * 36
OWNER_TASK = {"notify": "owner"}


def _long(prefix, n=40):
    return "\n".join(f"{prefix} line {i:03d} " + "x" * 30 for i in range(n))


class FullOutputTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(agent_cron._canonical_redaction, "canonical redaction must load")
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-cron-full-output-"))
        self.spool = self.tmp / "spool"
        self.full = self.tmp / "state" / "agent-cron" / "full-output"
        self._env = {k: os.environ.get(k) for k in (
            "CCC_PUSH_SPOOL", "CCC_AGENT_CRON_PUSH_SPOOL",
            "CCC_AGENT_CRON_FULL_OUTPUT_DIR", "CCC_AGENT_CRON_NOTIFY_ALLOWED_CHATS")}
        os.environ.pop("CCC_AGENT_CRON_PUSH_SPOOL", None)
        os.environ["CCC_PUSH_SPOOL"] = str(self.spool)
        os.environ["CCC_AGENT_CRON_FULL_OUTPUT_DIR"] = str(self.full)
        self._keep = agent_cron.FULL_OUTPUT_KEEP
        self._store = agent_cron.store

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        agent_cron.FULL_OUTPUT_KEEP = self._keep
        agent_cron.store = self._store
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spool(self, headless, status="failed", task=OWNER_TASK, run_id="task-1-100-1"):
        result = agent_cron.write_owner_spool(
            task, "task", run_id, "2026-10-23T00:00:00Z", status, headless, None)
        records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.spool.glob("*.json"))]
        return result, records

    def test_short_body_writes_no_file(self):
        result, (record,) = self._spool({"exitCode": 1, "stdout": "short", "stderr": "err"})
        self.assertEqual(result["delivery"], "spooled")
        self.assertNotIn("fullOutputPath", result)
        self.assertNotIn("fullOutputPath", record)
        self.assertNotIn("full redacted copy", record["text"])
        self.assertFalse(self.full.exists())

    def test_cut_stdout_keeps_full_redacted_copy_and_names_it(self):
        stdout = _long("out") + "\nleaked " + SECRET + "\nLAST-STDOUT-LINE"
        result, (record,) = self._spool({"exitCode": 1, "stdout": stdout, "stderr": ""})
        path = Path(result["fullOutputPath"])
        self.assertEqual(record["fullOutputPath"], str(path))
        self.assertEqual(path.parent, self.full)
        self.assertEqual(path.name, "task-1-100-1.txt")
        self.assertIn(f"full redacted copy (local, owner-only): {path}", record["text"])
        self.assertNotIn("LAST-STDOUT-LINE", record["text"])
        body = path.read_text(encoding="utf-8")
        self.assertIn("LAST-STDOUT-LINE", body)
        self.assertIn("runId=task-1-100-1", body)
        self.assertNotIn(SECRET, body)
        self.assertIn(agent_cron._canonical_redaction.REDACTION_MARKER, body)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.full.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in self.full.iterdir()], [path.name])  # no temp left

    def test_cut_stderr_on_failure_is_kept(self):
        stderr = _long("err") + "\nLAST-STDERR-LINE"
        result, (record,) = self._spool({"exitCode": 1, "stdout": "ok", "stderr": stderr})
        body = Path(result["fullOutputPath"]).read_text(encoding="utf-8")
        self.assertIn("LAST-STDERR-LINE", body)
        self.assertIn("stdout:\nok", body)

    def test_hidden_success_stderr_is_not_a_cut(self):
        # A successful run with a report hides stderr entirely; nothing shown
        # was cut, so no file and no path line.
        result, (record,) = self._spool(
            {"exitCode": 0, "stdout": "report", "stderr": _long("banner")}, status="success")
        self.assertNotIn("fullOutputPath", result)
        self.assertFalse(self.full.exists())

    def test_build_owner_text_is_unchanged_for_a_cut_body(self):
        headless = {"exitCode": 1, "stdout": _long("out"), "stderr": ""}
        text = agent_cron.build_owner_text("task", "run", None, "failed", headless)
        self.assertIsInstance(text, str)
        self.assertNotIn("full redacted copy", text)
        self.assertLessEqual(len(text.split("stdout:\n", 1)[1]), agent_cron.OWNER_BODY_STREAM_LIMIT)

    def test_symlinked_directory_is_not_followed(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        self.full.parent.mkdir(parents=True)
        self.full.symlink_to(target)
        result, (record,) = self._spool({"exitCode": 1, "stdout": _long("out"), "stderr": ""})
        self.assertEqual(result["delivery"], "spooled")
        self.assertNotIn("fullOutputPath", result)
        self.assertIn("fullOutputError", result)
        self.assertIn("local full copy not saved", record["text"])
        self.assertEqual(list(target.iterdir()), [])

    def test_loose_directory_mode_is_tightened(self):
        self.full.mkdir(parents=True)
        os.chmod(self.full, 0o755)
        result, _ = self._spool({"exitCode": 1, "stdout": _long("out"), "stderr": ""})
        self.assertIn("fullOutputPath", result)
        self.assertEqual(stat.S_IMODE(self.full.stat().st_mode), 0o700)

    def test_retention_keeps_the_newest_files_only(self):
        agent_cron.FULL_OUTPUT_KEEP = 3
        self.full.mkdir(parents=True, mode=0o700)
        now = time.time()
        for i in range(5):
            old = self.full / f"old-{i}.txt"
            old.write_text("old", encoding="utf-8")
            os.utime(old, (now - 1000 + i, now - 1000 + i))
        unrelated = self.full / "keep-me.json"
        unrelated.write_text("{}", encoding="utf-8")
        result, _ = self._spool({"exitCode": 1, "stdout": _long("out"), "stderr": ""})
        left = sorted(p.name for p in self.full.glob("*.txt"))
        self.assertEqual(left, ["old-3.txt", "old-4.txt", "task-1-100-1.txt"])
        self.assertTrue(unrelated.exists())

    def test_chat_recipient_gets_no_local_path(self):
        os.environ["CCC_AGENT_CRON_NOTIFY_ALLOWED_CHATS"] = "-100123"
        result, (record,) = self._spool(
            {"exitCode": 1, "stdout": _long("out"), "stderr": ""},
            task={"notify": "telegram-chat", "notifyChatId": "-100123"})
        self.assertEqual(record["recipient"], "chat")
        self.assertNotIn("fullOutputPath", result)
        self.assertNotIn("full redacted copy", record["text"])
        self.assertFalse(self.full.exists())

    def test_redaction_unavailable_writes_nothing(self):
        saved = agent_cron._canonical_redaction
        agent_cron._canonical_redaction = None
        try:
            result, records = self._spool({"exitCode": 1, "stdout": _long("out"), "stderr": ""})
        finally:
            agent_cron._canonical_redaction = saved
        self.assertEqual(result["delivery"], "blocked-redaction-unavailable")
        self.assertEqual(records, [])
        self.assertFalse(self.full.exists())

    def test_default_directory_sits_next_to_the_store(self):
        os.environ.pop("CCC_AGENT_CRON_FULL_OUTPUT_DIR", None)
        agent_cron.store = self.tmp / "state" / "agent-cron" / "tasks.json"
        self.assertEqual(agent_cron.full_output_dir(), self.full)


if __name__ == "__main__":
    unittest.main()
