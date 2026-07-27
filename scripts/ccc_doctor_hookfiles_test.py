#!/usr/bin/env python3
"""Contract tests for doctor's hook-tree walk (#569 convention).

doctor used to watch a hand-kept list of 8 hooks. setup.sh deploys the whole
claude/hooks/ tree, so anything added since simply never entered doctor's view:
distill.sh among them, which on gwakga sat 42 lines behind and was missing the
#386 fleet autonomy kill-switch while doctor reported the node clean.

The walk must therefore mirror ccc_hook_tree_files (scripts/lib/harness-paths.sh)
— the same source setup.sh installs from.

Hermetic: builds fixture trees under tmp; no node, no network.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ccc_doctor import HOOK_FILES_FALLBACK, Doctor  # noqa: E402


def doctor_for(repo: Path) -> Doctor:
    return Doctor(repo=repo, claude_dir=repo / "home/.claude", scope="all")


class WalkMatchesShellHelperTest(unittest.TestCase):
    """The Python walk and the shell helper must not drift apart."""

    def test_matches_ccc_hook_tree_files_on_this_repo(self) -> None:
        helper = REPO / "scripts/lib/harness-paths.sh"
        if not helper.is_file():
            self.skipTest("harness-paths.sh missing")
        proc = subprocess.run(
            ["bash", "-c", f'. "{helper}" && ccc_hook_tree_files "{REPO}"'],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            self.skipTest("shell helper unavailable in this environment")
        expected = sorted(f"hooks/{line}" for line in proc.stdout.split() if line)
        self.assertEqual(doctor_for(REPO).hook_files(), expected)


class WalkRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp())
        self.hooks = self.repo / "claude/hooks"
        (self.hooks / "lib").mkdir(parents=True)
        (self.hooks / "__pycache__").mkdir()

    def write(self, rel: str) -> None:
        path = self.hooks / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")

    def test_includes_nested_files(self) -> None:
        self.write("distill.sh")
        self.write("lib/autonomy-guard.sh")
        self.write("distill/extract.sh")
        self.assertEqual(
            doctor_for(self.repo).hook_files(),
            ["hooks/distill.sh", "hooks/distill/extract.sh", "hooks/lib/autonomy-guard.sh"],
        )

    def test_excludes_the_same_names_the_shell_helper_excludes(self) -> None:
        self.write("audit.sh")
        for skipped in (
            "audit.test.sh",
            "test-stub.sh",
            "hooks.json",
            "enforcement-overlay.json",
            "README.md",
            "cached.pyc",
        ):
            self.write(skipped)
        self.write("__pycache__/thing.py")
        self.assertEqual(doctor_for(self.repo).hook_files(), ["hooks/audit.sh"])

    def test_non_sh_deployables_are_watched(self) -> None:
        # setup.sh deploys these too; a .py-only hook must not be invisible.
        self.write("lib/memory_render.py")
        self.assertIn("hooks/lib/memory_render.py", doctor_for(self.repo).hook_files())

    def test_falls_back_when_repo_tree_is_absent(self) -> None:
        empty = Path(tempfile.mkdtemp())
        self.assertEqual(doctor_for(empty).hook_files(), HOOK_FILES_FALLBACK)

    def test_falls_back_when_tree_is_empty(self) -> None:
        self.assertEqual(doctor_for(self.repo).hook_files(), HOOK_FILES_FALLBACK)


class RegressionTest(unittest.TestCase):
    def test_previously_unwatched_hooks_are_now_watched(self) -> None:
        """The exact files the hand-kept list omitted."""
        watched = set(doctor_for(REPO).hook_files())
        for rel in (
            "hooks/distill.sh",
            "hooks/refresh-memory.sh",
            "hooks/scan-injection.sh",
            "hooks/skill-review.sh",
            "hooks/lib/autonomy-guard.sh",
        ):
            if (REPO / "claude" / rel).is_file():
                self.assertIn(rel, watched, f"{rel} must be watched")

    def test_walk_is_a_superset_of_the_fallback(self) -> None:
        watched = set(doctor_for(REPO).hook_files())
        for rel in HOOK_FILES_FALLBACK:
            if (REPO / "claude" / rel).is_file():
                self.assertIn(rel, watched)


if __name__ == "__main__":
    unittest.main(verbosity=2)
