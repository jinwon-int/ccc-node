#!/usr/bin/env python3
"""Pins `run_with_graceful_timeout` (ccc-node#2397) across its copies and its signal order.

A claude CLI killed with SIGKILL inside its ~0.5 s OAuth refresh window loses
the rotated refresh token, and the next call wipes the host login (reproduced
2026-10-20; SIGTERM is safe because the CLI finishes saving). Python's
``subprocess.run(timeout=)`` SIGKILLs on timeout, so the places that run
``claude -p`` under a Python timeout use this helper instead (auto-distill aside,
see COPIES). The scripts
install to different trees and cannot share an import, so each carries a copy;
this test keeps the copies byte-identical and checks the behaviour on real
child processes (no claude, no network).
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# scripts/auto-distill/auto-distill.py is deliberately absent: extract_json is on
# its evaluation-receipt surface, so changing it needs a reissued exact-source
# receipt (tracked separately on #2397).
COPIES = [
    "claude/hooks/nunchi/judge-batch.py",
    "claude/hooks/nunchi/nunchi.py",
    "scripts/ccc-skill-promotion.py",
]
NAMES = ("GRACEFUL_STOP_SECONDS", "run_with_graceful_timeout")


def helper_source(rel: str) -> str:
    text = (ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(text)
    parts = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == NAMES[1]:
            parts.append(ast.get_source_segment(text, node))
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == NAMES[0] for t in node.targets
        ):
            parts.append(ast.get_source_segment(text, node))
    return "\n".join(p for p in parts if p)


def load_helper(grace: float) -> dict:
    ns: dict = {"subprocess": subprocess}
    exec(helper_source(COPIES[0]), ns)  # noqa: S102 - our own source, pinned above
    ns["GRACEFUL_STOP_SECONDS"] = grace
    return ns


CHILD = textwrap.dedent(
    """
    import signal, sys, time
    marker, mode = sys.argv[1], sys.argv[2]
    def on_term(*_):
        open(marker, "w").write("TERM")
        sys.exit(0 if mode == "save" else 0)
    if mode == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, on_term)
    sys.stdout.write("started\\n"); sys.stdout.flush()
    time.sleep(60)
    """
)


class GracefulTimeout(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.child = self.tmp / "child.py"
        self.child.write_text(CHILD, encoding="utf-8")
        self.marker = self.tmp / "marker"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_all_copies_are_identical_and_present(self) -> None:
        reference = helper_source(COPIES[0])
        self.assertIn("proc.terminate()", reference)
        for rel in COPIES:
            with self.subTest(copy=rel):
                self.assertEqual(helper_source(rel), reference)
                self.assertIn("run_with_graceful_timeout(", (ROOT / rel).read_text(encoding="utf-8")
                              .split("def run_with_graceful_timeout", 1)[1].split("\n", 1)[1])

    def test_model_call_sites_no_longer_use_subprocess_run_with_timeout(self) -> None:
        sites = {
            "claude/hooks/nunchi/judge-batch.py": "def _claude_judge(",
            "claude/hooks/nunchi/nunchi.py": "def _run_synth_backend(",
            "scripts/ccc-skill-promotion.py": "def _run(",
        }
        for rel, start in sites.items():
            with self.subTest(site=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                body = text.split(start, 1)[1].split("\ndef ", 1)[0]
                self.assertIn("run_with_graceful_timeout(", body)
                self.assertNotIn("subprocess.run(", body)

    def test_completed_call_matches_subprocess_run(self) -> None:
        run = load_helper(5.0)["run_with_graceful_timeout"]
        done = run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                   input="ok", capture_output=True, text=True, timeout=20)
        self.assertIsInstance(done, subprocess.CompletedProcess)
        self.assertEqual((done.returncode, done.stdout.strip(), done.stderr), (0, "OK", ""))

    def test_timeout_sends_sigterm_first(self) -> None:
        run = load_helper(5.0)["run_with_graceful_timeout"]
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            run([sys.executable, str(self.child), str(self.marker), "save"],
                capture_output=True, text=True, timeout=1)
        self.assertEqual(self.marker.read_text(), "TERM", "child must see SIGTERM, not SIGKILL")
        self.assertIn("started", caught.exception.output or "")
        self.assertLess(time.time() - started, 5.0)

    def test_sigkill_only_after_the_grace(self) -> None:
        run = load_helper(1.0)["run_with_graceful_timeout"]
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            run([sys.executable, str(self.child), str(self.marker), "ignore"],
                capture_output=True, text=True, timeout=1)
        elapsed = time.time() - started
        self.assertGreaterEqual(elapsed, 1.9, "SIGKILL came before the grace ran out")
        self.assertLess(elapsed, 10.0)

    def test_spawn_errors_propagate_like_subprocess_run(self) -> None:
        run = load_helper(1.0)["run_with_graceful_timeout"]
        with self.assertRaises(FileNotFoundError):
            run([str(self.tmp / "missing-binary")], capture_output=True, timeout=5)

    def test_default_grace_is_long_enough_for_the_refresh_window(self) -> None:
        # Measured refresh window: lock at ~0.35 s, save by ~1.1 s.
        ns: dict = {"subprocess": subprocess}
        exec(helper_source(COPIES[0]), ns)  # noqa: S102
        self.assertGreaterEqual(ns["GRACEFUL_STOP_SECONDS"], 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
