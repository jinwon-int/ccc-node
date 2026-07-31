#!/usr/bin/env bash
# nunchi SessionStart injection (#816).
# Prints the latest peer_facts snapshot so it lands in session context.
# Fail-open, fast (<1s): never blocks session start; byte-capped at 3000.
# No-op unless nunchi is enabled (state/nunchi.mode=on or CCC_NUNCHI_MODE=on).
set -uo pipefail

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = "on" ] || exit 0

HERE="$(cd "$(dirname "$0")" && pwd)"
NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
SNAP="${NUNCHI_SNAPSHOT:-$NUNCHI_HOME/snapshot.md}"
[ -f "$SNAP" ] || exit 0
# Regenerate inline if stale (>15min); cron refreshes every 10min normally.
if [ -n "$(find "$SNAP" -mmin +15 2>/dev/null)" ]; then
  python3 "$HERE/nunchi.py" snapshot --limit 25 >/dev/null 2>&1 || true
fi
head -c 3000 "$SNAP" 2>/dev/null || true
