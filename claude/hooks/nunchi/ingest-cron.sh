#!/usr/bin/env bash
# nunchi mirror ingester (#816) — Claude-provider nodes.
# Mirrors honcho[] items into the nunchi peer_facts DB from two input sources:
#   1. $CCC_STATE_DIR/distill-history/*.json — written by claude/hooks/distill.sh
#   2. the bridge distill journal — used when the bridge owns distill (#1018),
#      in which case distill.sh no-ops and source 1 is never written
# Idempotent (dedup hash per fact + seen-file). No LLM cost — both sources
# reuse an extraction that already happened.
# No-op unless nunchi is enabled (state/nunchi.mode=on or CCC_NUNCHI_MODE=on).
set -uo pipefail

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = "on" ] || exit 0

HERE="$(cd "$(dirname "$0")" && pwd)"
FM="$HERE/nunchi.py"
ADAPTER="$HERE/bridge-journal.py"
NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
HIST="$STATE/distill-history"
# The bridge writes its journal under the project root it serves, which is not
# always $HOME. Operators point this at the serving checkout's data dir when it
# differs; the absent-source warning below is what surfaces a wrong guess.
JOURNAL="${CCC_BRIDGE_DISTILL_JOURNAL:-$HOME/.telegram_bot/distill-journal}"
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

  # Bridge-managed lane: adapt each finished journal job, then ingest it. A job
  # still in flight is left unseen so a later tick picks it up once extraction
  # lands; a finished job with nothing to mirror is marked seen so it is not
  # re-read forever.
  for f in "$JOURNAL"/*.json; do
    [ -f "$f" ] || continue
    grep -qxF "$f" "$SEEN" && continue
    payload="$(python3 "$ADAPTER" "$f" 2>/dev/null)"
    case $? in
      0)
        printf '%s' "$payload" | python3 "$FM" ingest - >/dev/null 2>&1 \
          && echo "$f" >> "$SEEN"
        ;;
      3) echo "$f" >> "$SEEN" ;;
      *) : ;;  # in flight or unreadable — retry next tick
    esac
  done

  # #1018 was invisible because the mirror ran happily with no input at all.
  # Say so once per tick rather than reporting success over an empty pipeline.
  if [ ! -d "$HIST" ] && [ ! -d "$JOURNAL" ]; then
    echo "nunchi ingest: no input source (distill-history=$HIST journal=$JOURNAL)" >&2
  fi

  python3 "$FM" snapshot --limit 25 >/dev/null 2>&1 || true
) 9>"$LOCK"
