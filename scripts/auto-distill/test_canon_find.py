#!/usr/bin/env python3
"""canon_snippets wiki-agent argv regressions.

A candidate title such as "--apply 렌더 이력 jsonl" made the canon query start
with "--". `wiki-agent find <query>` parsed it as an unknown option and exited
64, which (a) paged the operator through wiki-agent's failure notifier on every
auto-distill run that met such a title, and (b) came back as empty stdout, so
canon_snippets returned [] — read downstream as "no canon match" rather than a
failed search.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "auto-distill.py"

HIT_STDOUT = (
    "Semantic candidates:\n"
    "1. score=0.8719 pages/runbooks/example.md:10-20\n"
    "   example canon line\n"
)


def _load():
    spec = importlib.util.spec_from_file_location("canon_find_auto_distill", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CanonFindArgvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def _run(self, query, returncode=0, stdout=HIT_STDOUT):
        done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
        with mock.patch.object(self.mod.subprocess, "run", return_value=done) as run:
            result = self.mod.canon_snippets(query)
        return result, run.call_args.args[0]

    def test_dash_leading_query_is_passed_after_double_dash(self) -> None:
        query = "--apply 렌더 이력 jsonl --apply마다 <state>/history.jsonl"
        _, argv = self._run(query)
        self.assertEqual(argv, ["wiki-agent", "--no-notify", "find", "--", query])

    def test_hits_are_parsed(self) -> None:
        result, _ = self._run("ordinary query")
        self.assertEqual(result, ["pages/runbooks/example.md:10-20 :: example canon line"])

    def test_nonzero_exit_is_search_failure_not_empty_match(self) -> None:
        result, _ = self._run("--bad", returncode=64, stdout="")
        self.assertIsNone(result)

    def test_no_hits_with_zero_exit_stays_empty_list(self) -> None:
        result, _ = self._run("nothing", stdout="Text matches:\n  no text matches\n")
        self.assertEqual(result, [])

    def test_spawn_error_is_search_failure(self) -> None:
        with mock.patch.object(self.mod.subprocess, "run", side_effect=OSError("boom")):
            self.assertIsNone(self.mod.canon_snippets("q"))


if __name__ == "__main__":
    unittest.main()
