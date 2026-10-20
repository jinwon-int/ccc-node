#!/usr/bin/env python3
"""The claude graceful-stop wrapper in model_command.py (ccc-node#2397).

auto-distill's per-call timeout SIGKILLs its direct child. A claude CLI
SIGKILLed inside its ~0.5 s OAuth refresh window loses the rotated refresh
token and the next call wipes the host login (reproduced 2026-10-20); on
SIGTERM it finishes saving. claude therefore runs behind a wrapper whose death
reaches claude as SIGTERM (PDEATHSIG). These tests use a stub "claude" (a
Python script) — no real CLI, provider or network.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from model_command import (  # noqa: E402
    CLAUDE_ARGS,
    CLAUDE_GRACEFUL_FLAG,
    claude_model_alias,
    resolve_explicit_model_command,
    resolve_model_command,
)

MODEL_COMMAND = HERE / "model_command.py"
STUB = textwrap.dedent(
    """\
    #!{python}
    import os, signal, sys, time
    marker = os.environ["STUB_MARKER"]
    mode = os.environ.get("STUB_MODE", "echo")
    def on_term(*_):
        open(marker, "w").write("TERM")
        sys.exit(143)
    signal.signal(signal.SIGTERM, on_term)
    if mode == "echo":
        data = sys.stdin.read()
        sys.stdout.write(data.upper()); sys.stderr.write("stub-stderr\\n")
        sys.exit(3)
    open(marker + ".started", "w").write(str(os.getpid()))
    time.sleep(60)
    """
)


@unittest.skipUnless(sys.platform.startswith(("linux", "android")), "PDEATHSIG is Linux-only")
class ClaudeGracefulWrapper(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.claude = self.tmp / "claude"
        self.claude.write_text(STUB.format(python=sys.executable), encoding="utf-8")
        self.claude.chmod(0o755)
        self.marker = self.tmp / "marker"
        self.env = {**os.environ, "STUB_MARKER": str(self.marker)}

    def tearDown(self) -> None:
        started = Path(str(self.marker) + ".started")
        if started.exists():
            try:
                os.kill(int(started.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
        self._tmp.cleanup()

    def wrapper(self) -> list[str]:
        return [sys.executable, str(MODEL_COMMAND), CLAUDE_GRACEFUL_FLAG, str(self.claude), "-p"]

    def wait_for(self, path: Path, seconds: float = 10.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                if path.read_text():  # created and written (the stub writes then exits)
                    return True
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        return False

    def test_relays_stdio_and_exit_code(self) -> None:
        done = subprocess.run(self.wrapper(), input="prompt", capture_output=True, text=True,
                              timeout=30, env={**self.env, "STUB_MODE": "echo"})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "PROMPT", "stub-stderr\n"))

    def test_sigkill_of_wrapper_reaches_claude_as_sigterm(self) -> None:
        # Exactly what subprocess.run(timeout=) does to auto-distill's child.
        proc = subprocess.Popen(self.wrapper(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env={**self.env, "STUB_MODE": "hang"})
        proc.stdin.write("prompt")
        proc.stdin.close()
        proc.stdin = None
        self.assertTrue(self.wait_for(Path(str(self.marker) + ".started")), "stub never started")
        started = time.time()
        proc.kill()
        proc.communicate(timeout=10)  # caller is released once the wrapper is gone
        self.assertLess(time.time() - started, 5.0)
        self.assertTrue(self.wait_for(self.marker), "claude was not sent SIGTERM")
        self.assertEqual(self.marker.read_text(), "TERM")

    def test_sigterm_to_wrapper_is_forwarded(self) -> None:
        proc = subprocess.Popen(self.wrapper(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env={**self.env, "STUB_MODE": "hang"})
        proc.stdin.write("prompt")
        proc.stdin.close()
        proc.stdin = None
        self.assertTrue(self.wait_for(Path(str(self.marker) + ".started")))
        proc.terminate()
        _out, _err = proc.communicate(timeout=10)
        self.assertEqual(self.marker.read_text(), "TERM")
        self.assertEqual(proc.returncode, 143)

    def test_spawn_failure_is_127(self) -> None:
        done = subprocess.run([sys.executable, str(MODEL_COMMAND), CLAUDE_GRACEFUL_FLAG,
                               str(self.tmp / "missing")], input="", capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 127)
        self.assertIn("cannot spawn claude", done.stderr)


class ClaudeArgvWrapping(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def exe(self, name: str) -> Path:
        path = self.tmp / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_resolved_claude_is_wrapped_and_keeps_model_alias(self) -> None:
        claude = self.exe("claude")
        selected = resolve_model_command(
            process_environment={}, unit_environment={}, home=self.tmp,
            which={"claude": str(claude)}.get, piri_default_paths=(),
        )
        self.assertEqual(selected.argv[2:], (CLAUDE_GRACEFUL_FLAG, str(claude), *CLAUDE_ARGS))
        self.assertEqual(claude_model_alias(selected.argv), "haiku")

    def test_explicit_claude_model_cmd_is_wrapped(self) -> None:
        claude = self.exe("claude")
        selected = resolve_explicit_model_command(f"{claude} -p --model haiku", which=lambda _n: None)
        self.assertEqual(selected.engine, "custom")
        self.assertEqual(selected.argv[2:], (CLAUDE_GRACEFUL_FLAG, str(claude), "-p", "--model", "haiku"))

    def test_explicit_non_claude_command_is_untouched(self) -> None:
        other = self.exe("my-model")
        selected = resolve_explicit_model_command(f"{other} --x", which=lambda _n: None)
        self.assertEqual(selected.argv, (str(other), "--x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
