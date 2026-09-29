#!/usr/bin/env python3
"""#1903: git must not leave a detached child behind in a throwaway clone.

`_publish` and `_promote` clone into a `tempfile.TemporaryDirectory`, commit,
push, and let the context manager delete the clone. Since git 2.29 a `commit`
(or `fetch`) ends by spawning `git maintenance run --auto --detach` (older git:
`git gc --auto`, also detached). That child outlives the git call and takes
locks / writes under `.git/objects` while `TemporaryDirectory.__exit__` is
already removing the tree, so cleanup intermittently raised
`OSError: [Errno 39] Directory not empty: .../repo/.git/objects`. `main()`
maps that to `internal_error` — AFTER the branch was pushed but BEFORE the PR
was created or the ledger row written.

On CI this surfaced as the R2 p3–p6 + #1629 cascade in
ccc-skill-promotion.test.sh: the p3 republish crashed, p4 finished it one
collect late, and every later phase asserted against the wrong round.

`_run` now disables auto-maintenance and auto-gc on every git invocation, so
no background writer exists to race the cleanup.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_git_background_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)


def _config_pairs(argv: list[str]) -> set[str]:
    """The `-c key=value` settings given before the git subcommand."""
    pairs: set[str] = set()
    index = 1
    while index + 1 < len(argv) and argv[index] == "-c":
        pairs.add(argv[index + 1])
        index += 2
    return pairs


class RunArgvTests(unittest.TestCase):
    def captured(self, argv: list[str]) -> list[str]:
        seen: list[list[str]] = []

        def fake_run(args, **_kwargs):
            seen.append(list(args))
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with patch.object(promotion.subprocess, "run", side_effect=fake_run):
            promotion._run(argv)
        return seen[0]

    def test_git_runs_without_auto_maintenance_or_auto_gc(self) -> None:
        argv = self.captured(["git", "commit", "--quiet", "-m", "x"])
        self.assertEqual(argv[0], "git")
        self.assertLessEqual({"maintenance.auto=false", "gc.auto=0"}, _config_pairs(argv))
        self.assertEqual(argv[-4:], ["commit", "--quiet", "-m", "x"])

    def test_caller_config_is_kept_after_the_guard(self) -> None:
        """A caller's own `-c` (the commit identity) still applies."""
        argv = self.captured(["git", "-c", "user.name=n", "commit", "-m", "x"])
        self.assertIn("user.name=n", _config_pairs(argv))
        self.assertEqual(argv[-3:], ["commit", "-m", "x"])

    def test_non_git_argv_is_untouched(self) -> None:
        self.assertEqual(self.captured(["gh", "pr", "list"]), ["gh", "pr", "list"])


@unittest.skipUnless(shutil.which("git"), "git not installed")
class RealGitTests(unittest.TestCase):
    """End to end against the real git binary: trace every child process a
    commit spawns and require that none of them is a background maintainer."""

    def test_commit_through_run_spawns_no_background_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            trace = root / "trace.json"
            env = dict(os.environ)
            env.update(
                {
                    "HOME": str(root),
                    # The node's own git config must not decide the outcome.
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TRACE2_EVENT": str(trace),
                }
            )
            subprocess.run(
                ["git", "init", "--quiet", str(repo)], env=env, check=True,
                stdin=subprocess.DEVNULL, capture_output=True,
            )
            trace.unlink(missing_ok=True)
            promotion._run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit", "--quiet", "--allow-empty", "-m", "x"],
                cwd=repo, env=env,
            )
            if not trace.exists():
                self.skipTest("git build without trace2 support")
            argvs = []
            for line in trace.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if isinstance(event.get("argv"), list):
                    argvs.append(event["argv"])
            self.assertTrue(any("commit" in argv for argv in argvs), argvs)
            background = [
                argv for argv in argvs
                if "maintenance" in argv or ("gc" in argv and "--auto" in argv)
            ]
            self.assertEqual(background, [])


if __name__ == "__main__":
    unittest.main(verbosity=0)
