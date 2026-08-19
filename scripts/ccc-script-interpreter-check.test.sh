#!/usr/bin/env bash
# Registered harness suite for the interpreter-invocation checker (#1160).
# The unit/baseline tests live in scripts/ccc_script_interpreter_check_test.py
# (they emit the PASS=<n> FAIL=<n> tally this suite contract requires).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 absent — interpreter-check tests skipped"
  echo "PASS=0 FAIL=0"
  exit 0
fi
exec python3 "$ROOT/scripts/ccc_script_interpreter_check_test.py"
