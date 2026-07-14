"""Coverage floor is an enforced gate, not report-only (#453, #348 remainder).

The bridge-tests CI command (``pytest --cov=telegram_bot --cov-report=term``)
looks report-only, but pytest-cov enforces the branch-coverage
``[tool.coverage.report] fail_under`` from bridge/pyproject.toml — a run below
the floor exits non-zero and fails the job. (An earlier source audit misread
the step as ungated precisely because the floor lives in the coverage config,
not on the CLI; #453 is that visibility gap.)

These tests pin the gate so it can't be silently removed or weakened: the floor
stays configured at a meaningful branch-coverage value, the CI job actually runs
``--cov`` (so the config gate executes) across both matrix legs, and the floor
is not duplicated onto the CLI where it could drift.
"""

from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "bridge" / "pyproject.toml"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The measured baseline the floor must not fall below. Raising it (the ratchet)
# is tracked in #348; this only guards against the gate being dropped/weakened.
_MIN_FLOOR = 69


class CoverageFloorGateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        self.ci = CI_YML.read_text(encoding="utf-8")

    def test_fail_under_is_configured_and_meaningful(self):
        report = self.cfg.get("tool", {}).get("coverage", {}).get("report", {})
        self.assertIn("fail_under", report, "coverage floor (fail_under) removed")
        self.assertGreaterEqual(
            report["fail_under"],
            _MIN_FLOOR,
            "coverage floor weakened below the measured baseline",
        )

    def test_branch_coverage_is_the_gated_metric(self):
        run = self.cfg.get("tool", {}).get("coverage", {}).get("run", {})
        self.assertTrue(run.get("branch"), "branch coverage must be enabled")

    def test_ci_bridge_tests_runs_cov_so_the_gate_executes(self):
        # Without --cov the config fail_under never runs — the gate would be dead
        # even though it is still 'configured'. Pin the wiring.
        self.assertIn("--cov=telegram_bot", self.ci)

    def test_gate_applies_to_both_python_matrix_legs(self):
        # The floor lives in pyproject (shared), and the bridge-tests job runs the
        # same coverage command across the 3.11/3.12 matrix.
        self.assertIn("matrix:", self.ci)
        self.assertIn('"3.11"', self.ci)
        self.assertIn('"3.12"', self.ci)
        self.assertIn("bridge-tests (${{ matrix.python-version }})", self.ci)

    def test_floor_not_duplicated_on_the_cli(self):
        # A --cov-fail-under on the command would either duplicate the pyproject
        # value (drift) or silently override/weaken it. Keep it single-sourced.
        self.assertNotIn("--cov-fail-under", self.ci)


if __name__ == "__main__":
    sys.exit(unittest.main())
