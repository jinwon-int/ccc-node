#!/usr/bin/env python3
"""Direct unit tests for a2a_piri_memory_snapshot (shared-audience producer).

Run standalone: python3 scripts/a2a_piri_memory_snapshot_test.py
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import a2a_piri_memory_snapshot as producer


class ProducerTests(unittest.TestCase):
    def test_shared_environment_uses_the_canonical_shared_piri_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "audiences"
            env = producer.build_shared_memory_environment(
                audience_root=root,
                claude_settings_path=Path("/home/x/.claude/settings.json"),
                max_bytes=1024,
            )
        bootstrap = root / "shared" / "piri" / "bootstrap"
        self.assertEqual(env["CCC_MEMORY_AUDIENCE"], "shared")
        self.assertEqual(env["CCC_MEMORY_SCOPE"], "shared")
        self.assertEqual(env["CCC_MEMORY_AUDIENCE_SCOPED"], "1")
        self.assertEqual(env["CCC_MEMORY_LEGACY_PRIVATE_READS"], "0")
        self.assertEqual(env["CCC_WIKI_MEMORY_ENABLED"], "0")
        self.assertEqual(env["NUNCHI_HOME"], str(root / "shared" / "nunchi"))
        self.assertEqual(env["CODEX_HOME"], str(bootstrap))
        self.assertEqual(env["CODEX_SQLITE_HOME"], str(bootstrap))
        self.assertEqual(env["CCC_PIRI_BOOTSTRAP_HOME"], str(bootstrap))
        self.assertEqual(env["PIRI_CODING_AGENT_SESSION_DIR"], str(root / "shared" / "piri" / "sessions"))
        self.assertEqual(env["CCC_PIRI_BOOTSTRAP_CONTEXT_FILE"], str(bootstrap / "AGENTS.md"))
        self.assertEqual(env["CCC_CODEX_MEMORY_MAX_BYTES"], "1024")
        self.assertEqual(env["CCC_MEMORY_MATERIALIZER_PROVIDER"], "piri")

    def test_publish_snapshot_atomic_private_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "AGENTS.md"
            staging.write_text("snapshot-body", encoding="utf-8")
            output = Path(directory) / "out" / "MEMORY.md"
            count = producer.publish_snapshot(staging, output, max_bytes=64)
            self.assertEqual(count, len("snapshot-body"))
            self.assertEqual(output.read_text(encoding="utf-8"), "snapshot-body")
            mode = stat.S_IMODE(output.stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode) & 0o077, 0)

    def test_publish_snapshot_refuses_missing_empty_and_oversized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "MEMORY.md"
            with self.assertRaisesRegex(RuntimeError, "snapshot_missing"):
                producer.publish_snapshot(Path(directory) / "nope", output, max_bytes=8)
            empty = Path(directory) / "AGENTS.md"
            empty.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "snapshot_empty"):
                producer.publish_snapshot(empty, output, max_bytes=8)
            empty.write_text("0123456789", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "snapshot_oversized"):
                producer.publish_snapshot(empty, output, max_bytes=8)
            self.assertFalse(output.exists())

    def test_run_fail_closed_when_materializer_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code = producer.run(
                [
                    "--materializer",
                    str(Path(directory) / "absent"),
                    "--output",
                    str(Path(directory) / "MEMORY.md"),
                ]
            )
            self.assertEqual(code, 2)

    def test_run_publishes_with_stub_materializer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub = root / "stub_materializer.py"
            stub.write_text(
                "import os, sys\n"
                "from pathlib import Path\n"
                "target = Path(os.environ['CCC_PIRI_BOOTSTRAP_CONTEXT_FILE'])\n"
                "assert os.environ['CCC_MEMORY_AUDIENCE'] == 'shared'\n"
                "assert os.environ['CCC_MEMORY_LEGACY_PRIVATE_READS'] == '0'\n"
                "target.parent.mkdir(parents=True, exist_ok=True)\n"
                "target.write_text('shared snapshot', encoding='utf-8')\n"
                "print('{}')\n",
                encoding="utf-8",
            )
            output = root / "publish" / "MEMORY.md"
            code = producer.run(
                [
                    "--audience-root",
                    str(root / "audiences"),
                    "--materializer",
                    str(stub),
                    "--output",
                    str(output),
                    "--json",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "shared snapshot")

    def test_run_keeps_previous_snapshot_when_materializer_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "MEMORY.md"
            output.write_text("previous", encoding="utf-8")
            stub = root / "stub_fail.py"
            stub.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
            code = producer.run(
                [
                    "--audience-root",
                    str(root / "audiences"),
                    "--materializer",
                    str(stub),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(code, 78)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous")


if __name__ == "__main__":
    unittest.main()
