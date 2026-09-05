#!/usr/bin/env python3
"""Persistent extraction retry/dead-letter regressions (#1297)."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "auto-distill.py"


def _load_auto_distill():
    spec = importlib.util.spec_from_file_location("retry_auto_distill", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load managed auto-distill")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUTO_DISTILL = _load_auto_distill()


class RetryStateTest(unittest.TestCase):
    def test_same_snapshot_reaches_budget_without_advancing_success_cursor(self):
        entry = {"lines": 17, "mtime": 10.0, "path": "/session"}
        for expected in (1, 2, 3):
            entry, failure = AUTO_DISTILL.record_extract_failure(
                entry,
                path="/session",
                snapshot_size=900,
                snapshot_mtime=20.0,
                error="bad_json: source body must not enter state",
            )
            self.assertEqual(failure["attempts"], expected)
        self.assertTrue(failure["dead_lettered"])
        self.assertEqual(failure["reason"], "bad_json")
        self.assertEqual(entry["lines"], 17)
        self.assertEqual(entry["mtime"], 10.0)
        self.assertNotIn("source body", json.dumps(entry))
        self.assertTrue(AUTO_DISTILL.dead_letter_holds(entry, 900))
        self.assertTrue(AUTO_DISTILL.dead_letter_holds(entry, 899))
        self.assertFalse(AUTO_DISTILL.dead_letter_holds(entry, 901))

    def test_file_growth_starts_a_fresh_retry_budget(self):
        entry = {}
        for _ in range(3):
            entry, _failure = AUTO_DISTILL.record_extract_failure(
                entry,
                path="/session",
                snapshot_size=100,
                snapshot_mtime=20.0,
                error="no_json",
            )
        entry, failure = AUTO_DISTILL.record_extract_failure(
            entry,
            path="/session",
            snapshot_size=101,
            snapshot_mtime=21.0,
            error="model_exit_1: private stderr",
        )
        self.assertEqual(failure["attempts"], 1)
        self.assertFalse(failure["dead_lettered"])
        self.assertEqual(failure["reason"], "model_exit_1")

    def test_malformed_watermark_never_parks_a_session(self):
        self.assertFalse(AUTO_DISTILL.dead_letter_holds(None, 10))
        self.assertFalse(AUTO_DISTILL.dead_letter_holds(
            {"extract_failure": {"dead_lettered": True, "snapshot_size": "10"}},
            10,
        ))


class RetryBudgetIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.sessions = self.home / ".piri/agent/sessions"
        self.sessions.mkdir(parents=True)
        self.state = self.home / ".hermes/state/auto-distill.watermark.json"
        self.audit = self.home / ".hermes/logs/auto-distill-audit.jsonl"
        self.calls = self.root / "model.calls"
        self.environment = os.environ.copy()
        self.environment.update({
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "COUNT_FILE": str(self.calls),
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def model(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def session(self, name: str, marker: str) -> Path:
        path = self.sessions / name
        message = {
            "type": "message",
            "id": (marker + "00000000")[:8],
            "message": {"role": "user", "content": marker + ("가" * 500)},
        }
        path.write_text(json.dumps(message) + "\n", encoding="utf-8")
        return path

    def run_distill(self, model: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SOURCE),
                "--model-cmd",
                str(model),
                "--source",
                "piri",
                "--cap",
                "1",
                "--no-cache-sync",
                "--no-entail",
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            env=self.environment,
            check=False,
        )

    def watermark(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def audit_rows(self) -> list[dict]:
        return [json.loads(line) for line in self.audit.read_text().splitlines()]

    def test_three_failures_dead_letter_and_growth_reactivates(self):
        session = self.session("retry-session.jsonl", "retry")
        model = self.model(
            "failing-model",
            'printf "called\\n" >> "$COUNT_FILE"\nexit 1\n',
        )
        key = hashlib.sha256(str(session).encode()).hexdigest()[:16]

        for _ in range(3):
            result = self.run_distill(model)
            self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.watermark()[key]
        self.assertEqual(entry.get("lines", 0), 0)
        self.assertEqual(entry["extract_failure"]["attempts"], 3)
        self.assertTrue(entry["extract_failure"]["dead_lettered"])
        self.assertEqual(self.calls.read_text().splitlines(), ["called"] * 3)

        held = self.run_distill(model)
        self.assertEqual(held.returncode, 0, held.stderr)
        self.assertIn("retry-session.jsonl", held.stdout)
        self.assertIn("state=held", held.stdout)
        self.assertEqual(self.calls.read_text().splitlines(), ["called"] * 3)

        with session.open("a", encoding="utf-8") as output:
            output.write(json.dumps({
                "type": "message",
                "id": "growth01",
                "message": {"role": "assistant", "content": "새 증가분"},
            }) + "\n")
        retried = self.run_distill(model)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        failure = self.watermark()[key]["extract_failure"]
        self.assertEqual(failure["attempts"], 1)
        self.assertFalse(failure["dead_lettered"])
        self.assertEqual(self.calls.read_text().splitlines(), ["called"] * 4)

        events = [row["event"] for row in self.audit_rows()]
        self.assertEqual(events.count("extract_fail"), 4)
        self.assertEqual(events.count("dead_letter"), 1)
        self.assertEqual(events.count("dead_letter_reactivated"), 1)
        audit = self.audit.read_text(encoding="utf-8")
        self.assertNotIn("error", audit)
        self.assertNotIn("private stderr", audit)

    def test_parked_session_does_not_consume_the_cap(self):
        dead = self.session("newest-dead.jsonl", "dead")
        healthy = self.session("older-healthy.jsonl", "healthy")
        now = time.time()
        os.utime(dead, (now, now))
        os.utime(healthy, (now - 10, now - 10))
        dead_stat = dead.stat()
        dead_key = hashlib.sha256(str(dead).encode()).hexdigest()[:16]
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({
            dead_key: {
                "lines": 0,
                "mtime": 0,
                "path": str(dead),
                "extract_failure": {
                    "attempts": 3,
                    "budget": 3,
                    "reason": "no_json",
                    "snapshot_size": dead_stat.st_size,
                    "snapshot_mtime": dead_stat.st_mtime,
                    "dead_lettered": True,
                },
            },
        }), encoding="utf-8")
        model = self.model("success-model", 'printf \'{"items":[]}\\n\'\n')

        result = self.run_distill(model)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("newest-dead.jsonl", result.stdout)
        self.assertIn("older-healthy.jsonl", result.stdout)
        healthy_key = hashlib.sha256(str(healthy).encode()).hexdigest()[:16]
        self.assertEqual(self.watermark()[healthy_key]["lines"], 1)

    def test_dry_run_does_not_consume_retry_budget(self):
        session = self.session("dry-run.jsonl", "dry")
        model = self.model("dry-failing-model", "exit 1\n")
        key = hashlib.sha256(str(session).encode()).hexdigest()[:16]

        self.assertEqual(self.run_distill(model).returncode, 0)
        self.assertEqual(self.watermark()[key]["extract_failure"]["attempts"], 1)
        self.assertEqual(self.run_distill(model, "--dry-run").returncode, 0)
        self.assertEqual(self.watermark()[key]["extract_failure"]["attempts"], 1)


class DurableStateTest(unittest.TestCase):
    """#1481 — corrupt-watermark quarantine, atomic AUTO.md, UTC ts."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state" / "auto-distill.watermark.json"
        self.audit = self.root / "logs" / "auto-distill-audit.jsonl"
        self.patches = [
            mock.patch.object(AUTO_DISTILL, "STATE", str(self.state)),
            mock.patch.object(AUTO_DISTILL, "AUDIT", str(self.audit)),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()
        self.temp.cleanup()

    def test_missing_watermark_is_silently_empty(self):
        self.assertEqual(AUTO_DISTILL.load_watermark(), {})
        self.assertFalse(self.audit.exists())

    def test_corrupt_watermark_is_quarantined_and_audited(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text('{"abc": {"lines": 3', encoding="utf-8")

        self.assertEqual(AUTO_DISTILL.load_watermark(), {})

        self.assertFalse(self.state.exists())
        quarantined = sorted(self.state.parent.glob("auto-distill.watermark.json.corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), '{"abc": {"lines": 3')
        rows = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["event"] for row in rows], ["watermark_corrupt"])
        self.assertEqual(rows[0]["quarantined"], quarantined[0].name)
        self.assertTrue(rows[0]["ts"].endswith("+0000"), rows[0]["ts"])
        # Second load sees no file: {} silently, no second quarantine.
        self.assertEqual(AUTO_DISTILL.load_watermark(), {})
        self.assertEqual(len(list(self.state.parent.glob("*.corrupt-*"))), 1)

    def test_non_object_watermark_counts_as_corrupt(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(AUTO_DISTILL.load_watermark(), {})
        self.assertFalse(self.state.exists())
        self.assertEqual(len(list(self.state.parent.glob("*.corrupt-*"))), 1)

    def test_save_watermark_is_atomic_and_utf8(self):
        AUTO_DISTILL.save_watermark({"k": {"path": "/세션", "lines": 1}})
        self.assertEqual(sorted(p.name for p in self.state.parent.iterdir()),
                         ["auto-distill.watermark.json"])
        self.assertEqual(AUTO_DISTILL.load_watermark(), {"k": {"path": "/세션", "lines": 1}})
        self.assertIn("/세션", self.state.read_bytes().decode("utf-8"))

    def test_merge_auto_md_writes_atomically(self):
        page_dir = self.root / "pages" / "nodes" / "testnode"
        page = page_dir / "AUTO.md"
        chunk = (
            "\n### ✅ 승격 후보 — 제목\n\n- **사실**: 한글 사실\n"
            "- **분류**: `fact` · **상태**: `unverified` · **키**: `0123456789ab` · **파이프라인**: `v4`\n"
        )

        added, skipped = AUTO_DISTILL.merge_auto_md(str(page), "testnode", chunk)
        self.assertEqual((added, skipped), (1, 0))
        self.assertEqual(sorted(p.name for p in page_dir.iterdir()), ["AUTO.md"])
        text = page.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# [DOC-auto-testnode]"))
        self.assertIn("**키**: `0123456789ab`", text)
        self.assertTrue(text.endswith("\n"))

        # Idempotent re-merge: same key skipped, page byte-identical, no temp files.
        added, skipped = AUTO_DISTILL.merge_auto_md(str(page), "testnode", chunk)
        self.assertEqual((added, skipped), (0, 1))
        self.assertEqual(page.read_text(encoding="utf-8"), text)
        self.assertEqual(sorted(p.name for p in page_dir.iterdir()), ["AUTO.md"])

    def test_atomic_write_leaves_no_temp_on_failure(self):
        target = self.root / "out" / "file.txt"
        with mock.patch.object(AUTO_DISTILL.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                AUTO_DISTILL._atomic_write_text(str(target), "x")
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_utc_ts_is_parse_compatible(self):
        ts = AUTO_DISTILL.utc_ts()
        self.assertTrue(ts.endswith("+0000"), ts)
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
        self.assertEqual(parsed.utcoffset(), timedelta(0))


if __name__ == "__main__":
    unittest.main()
