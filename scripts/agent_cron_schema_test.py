#!/usr/bin/env python3
"""Contract tests for the agent-cron schema and import boundary (#347)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from agent_cron_schema import load_schema, validate_store  # noqa: E402
from agent_cron_repository import load_doc, write_doc  # noqa: E402


def valid_store() -> dict[str, object]:
    return {
        "version": 1,
        "tasks": [
            {
                "id": "daily",
                "schedule": "0 0 * * *",
                "prompt": "Summarize safely",
                "enabled": True,
                "notify": "none",
            }
        ],
    }


class SchemaContractTests(unittest.TestCase):
    def test_checked_in_schema_accepts_valid_store(self) -> None:
        self.assertEqual(validate_store(valid_store()), [])
        self.assertEqual(load_schema()["$id"], (
            "https://github.com/jinwon-int/ccc-node/"
            "schemas/agent-cron-task-store.schema.json"
        ))

    def test_unknown_fields_fail_closed_at_every_object_boundary(self) -> None:
        cases = []
        root = valid_store()
        root["surprise"] = True
        cases.append((root, "surprise"))

        task = valid_store()
        task["tasks"][0]["surprise"] = True  # type: ignore[index]
        cases.append((task, "tasks[0].surprise"))

        policy = valid_store()
        policy["tasks"][0]["retryPolicy"] = {"surprise": 1}  # type: ignore[index]
        cases.append((policy, "tasks[0].retryPolicy.surprise"))

        for payload, path in cases:
            with self.subTest(path=path):
                self.assertTrue(any(path in error for error in validate_store(payload)))

    def test_missing_type_enum_and_boundaries_match_schema(self) -> None:
        cases = (
            ("missing", lambda task: task.pop("prompt"), "tasks[0].prompt"),
            ("type", lambda task: task.__setitem__("enabled", 1), "tasks[0].enabled"),
            ("enum", lambda task: task.__setitem__("notify", "channel"), "tasks[0].notify"),
            ("minimum", lambda task: task.__setitem__("maxCatchup", 0), "tasks[0].maxCatchup"),
            ("maximum", lambda task: task.__setitem__("maxCatchup", 101), "tasks[0].maxCatchup"),
            ("pattern", lambda task: task.__setitem__("id", "bad id"), "tasks[0].id"),
            ("blank", lambda task: task.__setitem__("prompt", "   "), "tasks[0].prompt"),
        )
        for name, mutate, path in cases:
            payload = copy.deepcopy(valid_store())
            mutate(payload["tasks"][0])  # type: ignore[index]
            with self.subTest(name=name):
                self.assertTrue(any(path in error for error in validate_store(payload)))

    def test_duplicate_ids_are_rejected_as_store_semantics(self) -> None:
        payload = valid_store()
        payload["tasks"].append(copy.deepcopy(payload["tasks"][0]))  # type: ignore[union-attr,index]
        self.assertIn("duplicate task id: daily", validate_store(payload))

    def test_payload_semantics_fail_closed(self) -> None:
        store = valid_store()
        task = store["tasks"][0]

        task["payload"] = {"kind": "command"}
        self.assertIn(
            "tasks[0].payload.argv is required for kind 'command'",
            validate_store(store),
        )

        task["payload"] = {"kind": "command", "argv": ["sh", "-c", "true"],
                           "model": "m"}
        self.assertIn(
            "tasks[0].payload.model is not allowed for kind 'command'",
            validate_store(store),
        )

        task["payload"] = {"kind": "prompt", "argv": ["sh"]}
        self.assertIn(
            "tasks[0].payload.argv is not allowed for kind 'prompt'",
            validate_store(store),
        )

        task["payload"] = {"kind": "command", "argv": ["sh", "-c", "true"],
                           "cwd": "/tmp", "timeoutSec": 30,
                           "outputMaxBytes": 4096}
        self.assertEqual(validate_store(store), [])
        task["payload"] = {"kind": "prompt", "model": "claude-test",
                           "timeoutSec": 60}
        self.assertEqual(validate_store(store), [])

    def test_repository_uses_same_validator_for_reads_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            write_doc(path, valid_store())
            loaded, errors = load_doc(path)
            self.assertEqual(errors, [])
            self.assertEqual(loaded, valid_store())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            invalid = valid_store()
            invalid["unknown"] = True
            with self.assertRaisesRegex(ValueError, "unknown"):
                write_doc(path, invalid)
            self.assertEqual(load_doc(path)[0], valid_store())


class ImportBoundaryTests(unittest.TestCase):
    def test_import_has_no_dispatch_output_or_environment_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            probe = (
                "import json, os; "
                "before=dict(os.environ); import agent_cron; "
                "print(json.dumps({'changed': before != dict(os.environ), "
                "'has_main': callable(agent_cron.main)}))"
            )
            env = {"HOME": home, "PATH": os.environ.get("PATH", "")}
            completed = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=HERE,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"changed": False, "has_main": True},
        )


if __name__ == "__main__":
    unittest.main()
