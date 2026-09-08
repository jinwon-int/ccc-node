"""Every root-level test file must actually run in CI (#1593).

``tests/`` has no ``conftest.py`` or ``pytest.ini`` collecting it wholesale, so
pytest never discovers this directory on its own. CI instead names each file in
an explicit ``python -m pytest -q tests/...`` step. That works only for as long
as somebody remembers to add the next file to the workflow.

This is not hypothetical. ``ci.yml`` carries the scar in a comment of its own:
``test_autoresearch_streaming.py`` "was orphaned since it landed" — it sat in
``tests/`` passing locally and running nowhere in CI, and nothing went red.

So pin the invariant itself: every ``tests/test_*.py`` must appear in a pytest
invocation in ci.yml. A new root test that nobody wired now fails this test
instead of silently never running.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Matches the paths handed to pytest in a workflow step, e.g. `tests/test_x.py`.
_PYTEST_ARG = re.compile(r"tests/(test_[A-Za-z0-9_]+\.py)")
# A workflow step opens at this indent; its keys sit deeper.
_STEP_START = re.compile(r"^ {6}- ")
_WORKING_DIR = re.compile(r"^ {8}working-directory:\s*(\S+)")


def _split_steps(text):
    """Yield the raw text of each workflow step.

    Deliberately dependency-free: the root test job installs only the pinned
    lint toolchain, and pulling PyYAML in would mean regenerating the hashed
    requirements pair just to read one file.
    """
    step, started = [], False
    for line in text.splitlines():
        if _STEP_START.match(line):
            if started:
                yield "\n".join(step)
            step, started = [line], True
        elif started:
            # A dedent below step-key depth ends the steps: block.
            if line.strip() and not line.startswith(" " * 8):
                yield "\n".join(step)
                step, started = [], False
            else:
                step.append(line)
    if started:
        yield "\n".join(step)


def _root_wired_tests(text):
    """Root-level ``tests/*.py`` named in pytest steps.

    Steps that declare ``working-directory: bridge`` resolve the same
    ``tests/...`` prefix against ``bridge/tests/``, so counting them would both
    credit root files that never ran and flag bridge files as missing. Scope by
    the step's working directory, not by the literal string.
    """
    wired = set()
    for step in _split_steps(text):
        if any(_WORKING_DIR.match(line) for line in step.splitlines()):
            continue  # not the repository root
        wired.update(_PYTEST_ARG.findall(step))
    return wired


class RootTestCollectionTests(unittest.TestCase):
    def setUp(self):
        self.ci = CI_YML.read_text(encoding="utf-8")
        self.present = {p.name for p in TESTS_DIR.glob("test_*.py")}
        self.wired = _root_wired_tests(self.ci)

    def test_fixture_is_meaningful(self):
        # Guard the guard: if the glob or the regex silently matched nothing,
        # every other assertion here would pass vacuously.
        self.assertTrue(self.present, "no tests/test_*.py found — glob is wrong")
        self.assertTrue(self.wired, "no tests/*.py wired in ci.yml — regex is wrong")

    def test_step_scanner_still_understands_the_workflow(self):
        # The scanner is hand-rolled (no PyYAML in the root test job), so a
        # reformat of ci.yml could quietly make it see nothing and turn every
        # assertion here vacuous. Pin the two facts it depends on: steps are
        # found, and bridge-scoped steps are actually being excluded.
        steps = list(_split_steps(self.ci))
        self.assertGreater(len(steps), 20, "step scanner parsed almost nothing")
        scoped = [s for s in steps
                  if any(_WORKING_DIR.match(ln) for ln in s.splitlines())]
        self.assertTrue(scoped, "no working-directory steps seen — scoping is dead")
        # test_owner_operator_contract.py lives in bridge/tests and is run from a
        # working-directory: bridge step; it must never count as root-wired.
        self.assertNotIn("test_owner_operator_contract.py", self.wired)

    def test_every_root_test_file_is_wired_into_ci(self):
        orphaned = sorted(self.present - self.wired)
        self.assertEqual(
            [],
            orphaned,
            "these tests/ files run nowhere in CI — add them to a pytest step in "
            f".github/workflows/ci.yml: {orphaned}",
        )

    def test_ci_does_not_reference_missing_root_tests(self):
        # The mirror failure: a file is renamed or deleted but ci.yml still names
        # it, so the step dies on a missing path (or, worse, someone "fixes" it
        # by dropping the whole step).
        missing = sorted(self.wired - self.present)
        self.assertEqual(
            [],
            missing,
            f"ci.yml names tests/ files that do not exist: {missing}",
        )


if __name__ == "__main__":
    sys.exit(unittest.main())
