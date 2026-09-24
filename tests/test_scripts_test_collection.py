"""Every scripts/ python test file must actually run somewhere in CI (#1528).

``tests/test_root_test_collection.py`` pins this for ``tests/``: the directory
is not collected wholesale, so a file nobody names in ci.yml runs nowhere and
nothing goes red. ``scripts/`` has the same shape with one more indirection —
its python tests run through three different wirings:

1. a ``python -m pytest`` step in ``.github/workflows/ci.yml``,
2. a ``python3 scripts/<x>_test.py`` line in ``scripts/validate-harness.sh``,
3. a sibling ``*.test.sh`` wrapper that invokes the file — every tracked
   ``*.test.sh`` is discovered as a hook-test suite by validate-harness.sh.

The 2026-09-24 audit found ten files with none of the three: eight
``scripts/auto-distill/test_*.py`` (only ``test_redact.py`` was wired), plus
``agent_cron_store_lock_test.py`` and ``danso_native_memory_test.py``. Their
only evidence of ever passing was a laptop. Pin the invariant: every
``scripts/**/test_*.py`` and ``scripts/**/*_test.py`` is referenced by name —
or by its parent directory in a pytest invocation — from one of those places.

Dependency-free on purpose (no PyYAML, no git): the root test job installs
only the pinned lint toolchain, and ``git ls-files`` would make the guard
silently vacuous in a tarball checkout.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
VALIDATE_HARNESS = SCRIPTS_DIR / "validate-harness.sh"

# A pytest invocation and its (possibly line-continued) arguments.
_PYTEST_CMD = re.compile(r"python -m pytest\b((?:\\\n|[^\n])*)")
# A path handed to pytest that lives under scripts/: a file or a directory.
_SCRIPTS_ARG = re.compile(r"(?<![\w/.-])(scripts/[\w./-]+)")

_SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "venv"}


def _scripts_test_files():
    """Tracked-shape python test files under scripts/ (both naming styles)."""
    out = set()
    for pattern in ("test_*.py", "*_test.py"):
        for path in SCRIPTS_DIR.rglob(pattern):
            if _SKIP_DIRS.intersection(path.parts):
                continue
            out.add(path.relative_to(REPO_ROOT).as_posix())
    return out


def _pytest_paths(text):
    """scripts/ paths (files or directories) named in pytest invocations."""
    paths = set()
    for cmd in _PYTEST_CMD.finditer(text):
        paths.update(_SCRIPTS_ARG.findall(cmd.group(1)))
    return paths


def _tracked_test_sh_texts():
    for path in REPO_ROOT.rglob("*.test.sh"):
        if _SKIP_DIRS.intersection(path.parts) or ".git" in path.parts:
            continue
        yield path, path.read_text(encoding="utf-8", errors="replace")


def _wired_files(present):
    """Subset of ``present`` that some CI-reachable runner names."""
    ci = CI_YML.read_text(encoding="utf-8")
    harness = VALIDATE_HARNESS.read_text(encoding="utf-8")
    wrappers = "\n".join(t for _, t in _tracked_test_sh_texts())
    by_name = harness + "\n" + wrappers
    pytest_paths = _pytest_paths(ci) | _pytest_paths(harness)

    wired = set()
    for rel in present:
        base = rel.rsplit("/", 1)[-1]
        if base in by_name or rel in ci:
            wired.add(rel)
            continue
        if any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in pytest_paths):
            wired.add(rel)
    return wired


class ScriptsTestCollectionTests(unittest.TestCase):
    def setUp(self):
        self.present = _scripts_test_files()
        self.wired = _wired_files(self.present)

    def test_fixture_is_meaningful(self):
        # Guard the guard: a wrong glob or regex would pass everything below.
        self.assertGreater(len(self.present), 40, "scripts/ test glob found almost nothing")
        self.assertTrue(self.wired, "no scripts/ test wired anywhere — scanner is wrong")
        # Both naming styles must be represented, or one convention is invisible.
        self.assertTrue(any(p.rsplit("/", 1)[-1].startswith("test_") for p in self.present))
        self.assertTrue(any(p.endswith("_test.py") for p in self.present))

    def test_directory_invocation_counts_as_wiring(self):
        # ci.yml runs `scripts/auto-distill` as a directory; every test file
        # inside must resolve as wired through that path, not by name.
        paths = _pytest_paths(CI_YML.read_text(encoding="utf-8"))
        self.assertIn("scripts/auto-distill", paths)
        inside = {p for p in self.present if p.startswith("scripts/auto-distill/")}
        self.assertTrue(inside)
        self.assertTrue(inside <= self.wired, sorted(inside - self.wired))

    def test_every_scripts_test_file_runs_somewhere(self):
        orphaned = sorted(self.present - self.wired)
        self.assertEqual(
            [],
            orphaned,
            "these scripts/ test files run nowhere in CI — name them in a pytest "
            "step in .github/workflows/ci.yml, in scripts/validate-harness.sh, "
            f"or in a sibling *.test.sh wrapper: {orphaned}",
        )

    def test_ci_does_not_reference_missing_scripts_tests(self):
        # Mirror failure: a renamed/deleted file still named in a pytest step
        # kills the step on a missing path.
        ci = CI_YML.read_text(encoding="utf-8")
        missing = sorted(
            p for p in _pytest_paths(ci) if not (REPO_ROOT / p).exists()
        )
        self.assertEqual([], missing, f"ci.yml pytest steps name missing scripts/ paths: {missing}")


if __name__ == "__main__":
    sys.exit(unittest.main())
