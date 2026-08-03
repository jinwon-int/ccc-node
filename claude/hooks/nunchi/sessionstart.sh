#!/usr/bin/env bash
# nunchi SessionStart injection (#816).
# Prints the latest peer_facts snapshot so it lands in session context.
# Fail-open, fast (<1s): never blocks session start; byte-capped at 3000.
# No-op unless nunchi is enabled (state/nunchi.mode=on or CCC_NUNCHI_MODE=on).
#
# Snapshot regeneration is asynchronous (background) when stale (#893):
# - Stale snapshots (>15min) are injected immediately, then refreshed in background.
# - This prevents Python/repository latency from blocking SessionStart.
set -uo pipefail

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = "on" ] || exit 0

# Node-global nunchi has no scope-local provenance yet. Match the managed Codex
# loader's fail-closed boundary and disable it for every audience-scoped runtime.
case "${CCC_MEMORY_AUDIENCE_SCOPED:-0}" in
  1|true|TRUE|on|ON|yes|YES) exit 0 ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
SNAP="${NUNCHI_SNAPSHOT:-$NUNCHI_HOME/snapshot.md}"
[ -f "$SNAP" ] || exit 0

# Inject current snapshot immediately (never block start).
head -c 3000 "$SNAP" 2>/dev/null || true

# Regenerate asynchronously if stale (>15min); cron refreshes every 10min normally.
# The background refresh updates the snapshot for the next session; this session
# already received the (possibly stale) current snapshot above.
if [ -n "$(find "$SNAP" -mmin +15 2>/dev/null)" ]; then
  (
    # Detached background process with nohup to survive shell exit.
    # Redirect all output to /dev/null to avoid interfering with SessionStart.
    nohup python3 "$HERE/nunchi.py" snapshot --limit 25 >/dev/null 2>&1 &
    disown $! 2>/dev/null || true
  ) 2>/dev/null || true
fi
