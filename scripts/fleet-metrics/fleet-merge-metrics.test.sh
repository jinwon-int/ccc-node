#!/usr/bin/env bash
# Tests for scripts/fleet-metrics (#2302) — hermetic, no network, no gh.
# Wires fleet_merge_metrics_test.py into the hook-test suite (every tracked
# *.test.sh is discovered by validate-harness.sh; tests/test_scripts_test_collection.py
# fails on an unwired scripts/ python test).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
pass=0; fail=0
TMP="$(mktemp -d)" || exit 1
trap 'rm -rf "$TMP"' EXIT

if python3 "$ROOT/scripts/fleet-metrics/fleet_merge_metrics_test.py" >"$TMP/unit.out" 2>&1; then
  pass=$((pass+1))
else
  fail=$((fail+1)); echo "FAIL: fleet-metrics python unit suite"; tail -40 "$TMP/unit.out"
fi

# The entrypoint must parse --help without touching the network.
if python3 "$ROOT/scripts/fleet-metrics/fleet_merge_metrics.py" --help >"$TMP/help.out" 2>&1; then
  pass=$((pass+1))
else
  fail=$((fail+1)); echo "FAIL: fleet_merge_metrics.py --help"; tail -5 "$TMP/help.out"
fi

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
