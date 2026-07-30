#!/usr/bin/env bash
# nunchi mirror ingester (#816) — Claude-provider nodes.
# Scans $CCC_STATE_DIR/distill-history/*.json and mirrors honcho[] items into
# the nunchi peer_facts DB. Idempotent (dedup hash per fact + seen-file). No
# LLM cost — reuses the distill extraction honcho-push.sh already produced.
# No-op unless nunchi is enabled (state/nunchi.mode=on or CCC_NUNCHI_MODE=on).
set -uo pipefail

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = "on" ] || exit 0

HERE="$(cd "$(dirname "$0")" && pwd)"
FM="$HERE/nunchi.py"
NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
HIST="$STATE/distill-history"
LOCK="$NUNCHI_HOME/.ingest.lock"
SEEN="$NUNCHI_HOME/ingested-files"
mkdir -p "$NUNCHI_HOME"
touch "$SEEN"

(
  flock -n 9 || exit 0
  for f in "$HIST"/*.json; do
    [ -f "$f" ] || continue
    grep -qxF "$f" "$SEEN" && continue
    python3 "$FM" ingest "$f" >/dev/null 2>&1 && echo "$f" >> "$SEEN"
  done
  python3 "$FM" snapshot --limit 25 >/dev/null 2>&1 || true
) 9>"$LOCK"
