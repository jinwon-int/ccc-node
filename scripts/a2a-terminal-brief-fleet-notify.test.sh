#!/usr/bin/env bash
# Harness wrapper for the A2A Terminal Brief -> Matrix fleet-alert consumer unit
# tests (ccc-node#2498) so validate-harness.sh and CI run them alongside the shell
# suites. Emits a PASS=/FAIL= line and exits non-zero on failure, matching the
# other *.test.sh contracts.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
out_file="$(mktemp)"
trap 'rm -f "$out_file"' EXIT

if python3 "$HERE/a2a_terminal_brief_fleet_notify_test.py" >"$out_file" 2>&1; then
  ran="$(grep -oE 'Ran [0-9]+ test' "$out_file" | grep -oE '[0-9]+' | awk '{sum += $1} END {print sum}')"
  echo "----"
  echo "PASS=${ran:-0} FAIL=0"
else
  tail -20 "$out_file"
  echo "----"
  echo "PASS=0 FAIL=1"
  exit 1
fi
