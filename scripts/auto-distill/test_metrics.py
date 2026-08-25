#!/usr/bin/env python3
"""metrics.py regressions.

The low-coverage warning path used to compare ``loss > 0.5`` while ``loss`` is
None whenever no quarantine-bucket item has a human verdict yet — the most
common state the warning exists for ("some pass items judged, quarantine not
yet judged") — so the whole report died with a TypeError instead of printing.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
METRICS = ROOT / "scripts/auto-distill/metrics.py"


def _write_fixture_home(home: Path, pipeline: int) -> None:
    """A hermetic $HOME: one deployed extractor pin + one node's AUTO.md."""

    extractor = home / ".hermes/auto-distill/auto-distill.py"
    extractor.parent.mkdir(parents=True, exist_ok=True)
    extractor.write_text(f"PIPELINE = {pipeline}\n", encoding="utf-8")

    auto_md = home / ".wiki-agent/wiki-cache/pages/nodes/gwakga/AUTO.md"
    auto_md.parent.mkdir(parents=True, exist_ok=True)
    auto_md.write_text(
        textwrap.dedent(
            f"""\
            ### ✅ judged pass item
            **키**: `aaaaaaaaaaaa`
            **상태**: promoted
            **파이프라인**: `v{pipeline}`

            ### 🔍 unjudged quarantine item
            **키**: `bbbbbbbbbbbb`
            **상태**: unverified
            **파이프라인**: `v{pipeline}`
            """
        ),
        encoding="utf-8",
    )


class MetricsNoneLossTest(unittest.TestCase):
    def run_metrics(self, home: Path) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, HOME=str(home))
        # No ssh on PATH: every non-local node fails fast into the
        # "unreachable" bucket instead of attempting real connections.
        env["PATH"] = str(home / "no-bin")
        return subprocess.run(
            [sys.executable, str(METRICS), "--node", "gwakga"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def test_unjudged_quarantine_does_not_crash_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_fixture_home(home, pipeline=6)
            proc = self.run_metrics(home)
        self.assertEqual(
            proc.returncode, 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        self.assertNotIn("Traceback", proc.stderr)
        # The judged-pass/unjudged-quarantine state must land in the
        # low-coverage warning (the branch that used to crash), not die
        # before printing it.
        self.assertIn("판정 커버리지가 낮다", proc.stdout)


if __name__ == "__main__":
    unittest.main()
