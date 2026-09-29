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
# shellcheck source=claude/hooks/nunchi/feed-common.sh
. "$HERE/feed-common.sh" 2>/dev/null || {
  echo "${0##*/}: feed-common.sh missing beside this feed — the harness is only partially deployed; re-run setup.sh. Refusing to run rather than tick without ingesting (#1698)." >&2
  exit 2
}
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

  # Audience-scoped mode (#1921): every Claude transcript lives in one shared
  # ~/.claude/projects tree, so items are routed per session through the
  # bridge's session_id -> audience sidecar into <root>/<scope>/nunchi. An
  # item with no, ambiguous or invalid mapping is skipped and counted — never
  # written to this node-global store, which would mix audiences. The
  # node-wide $HIST is NOT an input here: bridge-managed distill never writes
  # it, so it only holds non-bridge (terminal CLI/cron/worker) sessions, which
  # must not be routed into an audience by session id alone.
  if [ "${CCC_NUNCHI_AUDIENCE_SCOPED:-0}" = 1 ]; then
    # Last tick's rejection counts: the log only speaks when they change, so a
    # permanently unmapped/invalid backlog is not re-logged every ten minutes.
    prev="$(python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
print(d.get("unmapped", "-"), d.get("ambiguous", "-"), d.get("invalid", "-"), d.get("skipped") or "-")' "$STATUS" 2>/dev/null)" || prev=""
    counts="$(python3 "$HERE/claude-audience-feed.py" \
      --audience-root "${CCC_NUNCHI_AUDIENCE_ROOT:-}" \
      --journal "$JOURNAL" --seen "$SEEN" \
      --nunchi-py "$FM" ${CCC_NUNCHI_CLAUDE_PROJECTS:+--projects-root "$CCC_NUNCHI_CLAUDE_PROJECTS"})" || counts=""
    case "$counts" in *[!0-9\ ]*) counts="" ;; esac  # ten integers or nothing
    read -r sources ingested retired deferred unmapped ambiguous invalid scopes sidecars pruned <<<"$counts"
    if [ -z "${pruned:-}" ]; then
      echo "nunchi ingest (audience-scoped): router failed — nothing ingested this tick" >&2
      nunchi_write_status "$STATUS" claude 0 0 0 0 '"audience_scoped":true,"skipped":"audience-router-failed"'
      exit 0
    fi
    skipped="-"
    [ "$sidecars" -eq 0 ] && skipped="no-audience-sidecar"
    if [ "$skipped" = no-audience-sidecar ] && [ "${prev##* }" != no-audience-sidecar ]; then
      echo "nunchi ingest (audience-scoped): no Claude session->audience sidecar under ${CCC_NUNCHI_AUDIENCE_ROOT:-<unset>} — every item is unmapped and skipped (fail-closed); the bridge writes one per Claude turn (#1921)" >&2
    fi
    if [ $((ingested + retired + pruned)) -gt 0 ] \
        || [ "$unmapped $ambiguous $invalid $skipped" != "$prev" ]; then
      echo "nunchi ingest (audience-scoped): ingested=$ingested retired=$retired deferred=$deferred unmapped=$unmapped ambiguous=$ambiguous invalid=$invalid scopes=$scopes sidecars=$sidecars pruned=$pruned"
    fi
    extra="\"audience_scoped\":true,\"unmapped\":$unmapped,\"ambiguous\":$ambiguous,\"invalid\":$invalid,\"scopes\":$scopes,\"sidecars\":$sidecars,\"pruned\":$pruned"
    [ "$skipped" != "-" ] && extra="$extra,\"skipped\":\"$skipped\""
    nunchi_write_status "$STATUS" claude "$sources" "$ingested" "$retired" "$deferred" "$extra"
    exit 0
  fi

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

  nunchi_write_status "$STATUS" claude "$sources" "$ingested" "$retired" "$deferred"

  python3 "$FM" snapshot --limit 25 >/dev/null 2>&1 || true
) 9>"$LOCK"
