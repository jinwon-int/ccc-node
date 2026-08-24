#!/usr/bin/env bash
# Managed auto-distill provider/installer/receipt regressions (#1257, #1262).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 - "$ROOT/scripts/auto-distill" <<'PY'
import pathlib
import sys
import unittest

test_dir = pathlib.Path(sys.argv[1])
suite = unittest.defaultTestLoader.discover(str(test_dir), pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=0).run(suite)
failures = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
passed = result.testsRun - failures - len(result.skipped)
print(f"PASS={passed} FAIL={failures}")
raise SystemExit(0 if result.wasSuccessful() else 1)
PY
