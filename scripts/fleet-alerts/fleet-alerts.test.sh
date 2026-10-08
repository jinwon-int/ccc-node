#!/usr/bin/env bash
# Tests for scripts/fleet-alerts (#2182) — hermetic, no network, no Matrix.
# Wires fleet_alerts_test.py into the hook-test suite (every tracked *.test.sh
# is discovered by validate-harness.sh; tests/test_scripts_test_collection.py
# fails on an unwired scripts/ python test). The python suite needs the
# telegram_bot import shim for bridge/core/fleet_alert_relay (stdlib only).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
pass=0; fail=0
TMP="$(mktemp -d)" || exit 1
trap 'rm -rf "$TMP"' EXIT

if PYTHONPATH="$ROOT/.github/pythonpath" python3 "$ROOT/scripts/fleet-alerts/fleet_alerts_test.py" >"$TMP/unit.out" 2>&1; then
  pass=$((pass+1))
else
  fail=$((fail+1)); echo "FAIL: fleet-alerts python unit suite"; tail -40 "$TMP/unit.out"
fi

# The two entrypoints must parse --help without matrix-nio installed
# (the sender imports the Matrix transport lazily).
for entry in fleet_alerts_receiver.py fleet_alerts_sender.py; do
  if PYTHONPATH="$ROOT/.github/pythonpath" python3 "$ROOT/scripts/fleet-alerts/$entry" --help >"$TMP/help.out" 2>&1; then
    pass=$((pass+1))
  else
    fail=$((fail+1)); echo "FAIL: $entry --help"; tail -5 "$TMP/help.out"
  fi
done

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
