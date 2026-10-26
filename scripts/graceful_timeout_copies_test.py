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
import os
import signal
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

# #2533 — a descendant that keeps the parent's stdout/stderr open. In "escape"
# mode it leaves the process group (setsid), so only the bounded drain can free
# the caller; otherwise it ignores SIGTERM ("ignore") or saves on it ("save").
GRANDCHILD = textwrap.dedent(
    """
    import os, signal, sys, time
    pidfile, marker, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    if mode == "escape":
        os.setsid()
    if mode == "save":
        def on_term(*_):
            open(marker, "w").write("TERM")
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term)
    else:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open(pidfile, "w").write(str(os.getpid()))
    time.sleep(60)
    """
)

# Stands in for the claude CLI: spawns the descendant with inherited pipes.
PARENT = textwrap.dedent(
    """
    import signal, subprocess, sys, time
    grand, pidfile, gmarker, gmode, pmode = sys.argv[1:6]
    if pmode == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    subprocess.Popen([sys.executable, grand, pidfile, gmarker, gmode])
    for _ in range(200):
        try:
            if open(pidfile).read():
                break
        except OSError:
            pass
        time.sleep(0.05)
    sys.stdout.write("started\\n"); sys.stdout.flush()
    time.sleep(60)
    """
)


def _gone(pid: int, wait: float = 3.0) -> bool:
    """True once pid no longer runs (absent, or a zombie awaiting its reaper)."""
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                return True
        except (OSError, IndexError):
            return True
        time.sleep(0.05)
    return False


class GracefulTimeout(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.child = self.tmp / "child.py"
        self.child.write_text(CHILD, encoding="utf-8")
        self.marker = self.tmp / "marker"
        self.grand = self.tmp / "grand.py"
        self.grand.write_text(GRANDCHILD, encoding="utf-8")
        self.parent = self.tmp / "parent.py"
        self.parent.write_text(PARENT, encoding="utf-8")
        self.gpid = self.tmp / "grand.pid"
        self.gmarker = self.tmp / "grand.marker"

    def tearDown(self) -> None:
        # An escaped descendant outlives the helper by design; never leak it.
        try:
            os.kill(int(self.gpid.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
        self._tmp.cleanup()

    def _family(self, gmode: str, pmode: str) -> list[str]:
        return [sys.executable, str(self.parent), str(self.grand), str(self.gpid),
                str(self.gmarker), gmode, pmode]

    def test_all_copies_are_identical_and_present(self) -> None:
        reference = helper_source(COPIES[0])
        # #2533: signals go to the child's own process group, not just the child.
        self.assertIn("os.killpg(", reference)
        self.assertIn("start_new_session", reference)
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

    def test_descendant_holding_the_pipes_is_killed_with_the_group(self) -> None:
        # #2533: proc.kill() reached only the direct child, so an in-group
        # descendant kept stdout open and the unbounded communicate() hung.
        run = load_helper(1.0)["run_with_graceful_timeout"]
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            run(self._family("ignore", "ignore"), capture_output=True, text=True, timeout=1)
        self.assertLess(time.time() - started, 8.0, "caller was pinned by the descendant")
        self.assertTrue(_gone(int(self.gpid.read_text())), "descendant survived SIGKILL")

    def test_descendant_outside_the_group_cannot_pin_the_caller(self) -> None:
        # A descendant that called setsid() escapes killpg; the drain after
        # SIGKILL is bounded, so the caller still returns.
        run = load_helper(1.0)["run_with_graceful_timeout"]
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            run(self._family("escape", "ignore"), capture_output=True, text=True, timeout=1)
        self.assertLess(time.time() - started, 10.0, "unbounded wait after SIGKILL")

    def test_descendants_get_sigterm_first_too(self) -> None:
        # The #2397 order holds for the whole group: SIGTERM, not SIGKILL.
        run = load_helper(5.0)["run_with_graceful_timeout"]
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            run(self._family("save", "save"), capture_output=True, text=True, timeout=1)
        self.assertEqual(self.gmarker.read_text(), "TERM")
        self.assertIn("started", caught.exception.output or "")
        self.assertLess(time.time() - started, 5.0)

    def test_caller_may_opt_out_of_a_new_session(self) -> None:
        run = load_helper(5.0)["run_with_graceful_timeout"]
        done = run([sys.executable, "-c", "import os; print(os.getpgrp() == os.getpgid(os.getppid()))"],
                   capture_output=True, text=True, timeout=20, start_new_session=False)
        self.assertEqual(done.stdout.strip(), "True")
        done = run([sys.executable, "-c", "import os; print(os.getpgrp() == os.getpid())"],
                   capture_output=True, text=True, timeout=20)
        self.assertEqual(done.stdout.strip(), "True")

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
