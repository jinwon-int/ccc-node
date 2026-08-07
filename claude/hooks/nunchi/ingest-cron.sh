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
STATUS="${CCC_NUNCHI_INGEST_STATUS:-$NUNCHI_HOME/ingest.status.json}"
mkdir -p "$NUNCHI_HOME"
touch "$SEEN"

(
  flock -n 9 || exit 0

  sources=0
  [ -d "$HIST" ] && sources=$((sources+1))
  [ -d "$JOURNAL" ] && sources=$((sources+1))
  ingested=0 retired=0 deferred=0

  for f in "$HIST"/*.json; do
    [ -f "$f" ] || continue
    grep -qxF "$f" "$SEEN" && continue
    if python3 "$FM" ingest "$f" >/dev/null 2>&1; then
      echo "$f" >> "$SEEN"
      ingested=$((ingested+1))
    fi
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
        if printf '%s' "$payload" | python3 "$FM" ingest - >/dev/null 2>&1; then
          echo "$f" >> "$SEEN"
          ingested=$((ingested+1))
        fi
        ;;
      3) echo "$f" >> "$SEEN"; retired=$((retired+1)) ;;
      *) deferred=$((deferred+1)) ;;  # in flight or unreadable — retry next tick
    esac
  done

  # #1018 was invisible because the mirror ran happily with no input at all and
  # still looked healthy. Two things fix that: say what a tick did, and leave a
  # machine-readable receipt the readiness probe can judge.
  if [ "$sources" -eq 0 ]; then
    echo "nunchi ingest: no input source (distill-history=$HIST journal=$JOURNAL)" >&2
  elif [ $((ingested + retired)) -gt 0 ]; then
    echo "nunchi ingest: ingested=$ingested retired=$retired deferred=$deferred"
  fi

  now="$(date -u +%s)"
  tmp="$STATUS.$$"
  if printf '{"schema":"ccc.nunchi.ingest.v1","finished_at":%d,"sources":%d,"ingested":%d,"retired":%d,"deferred":%d}\n' \
      "$now" "$sources" "$ingested" "$retired" "$deferred" > "$tmp" 2>/dev/null; then
    mv -f "$tmp" "$STATUS" 2>/dev/null || rm -f "$tmp"
  else
    rm -f "$tmp" 2>/dev/null
  fi

  python3 "$FM" snapshot --limit 25 >/dev/null 2>&1 || true
) 9>"$LOCK"
