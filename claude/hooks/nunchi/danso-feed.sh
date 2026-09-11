#!/usr/bin/env bash
# nunchi danso-feed mirror (#1698) — Danso-provider nodes.
#
# Danso nodes had no nunchi feed at all: install-nunchi.sh only knew the
# claude/codex/piri lanes, so a node that switched to CCC_AGENT_PROVIDER=danso
# kept running whichever feed was installed before the switch. Its input dried
# up and every tick reported `ingested: 0` — a silent failure that left
# gongmyoung 6 days and soonwook 43 days without a new fact.
#
# This lane is a MIRROR, not an extractor. On a Danso node the bridge already
# runs distill itself and lands the validated result in its own journal
# (bot_data_dir/danso-distill-journal, see bridge/__main__.py journal_name).
# Those job files use the same DistillJournal schema as the claude lane, so
# bridge-journal.py adapts them unchanged. Re-extracting the raw native session
# journals under CCC_DANSO_STATE_DIR would instead need Danso credentials in
# cron and would pay for an extraction the bridge already performed.
#
# Consequence: this lane can only mirror what the bridge extracted. When the
# bridge's distill lane is off (CCC_MEMORY_DISTILL_PROVIDER=off, the shipped
# default in docs/danso-telegram.md), there is nothing to mirror — and that is
# reported as `skipped: distill-journal-missing` rather than a silent zero.
#
# No LLM cost. Idempotent (dedup hash per fact + seen-file).
# No-op unless nunchi is enabled (state/nunchi.mode=on or CCC_NUNCHI_MODE=on).
set -uo pipefail
umask 077

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
LOCK="$NUNCHI_HOME/.danso-feed.lock"
SEEN="$NUNCHI_HOME/danso-seen"
STATUS="${CCC_NUNCHI_INGEST_STATUS:-$NUNCHI_HOME/ingest.status.json}"
# Explicit override first, then the bridge layout. bot_data_dir defaults to
# PROJECT_ROOT/.telegram_bot and the bridge serves $HOME on these nodes; an
# operator whose PROJECT_ROOT differs points CCC_BRIDGE_DISTILL_JOURNAL at it,
# and the absent-source notice below is what surfaces a wrong guess.
JOURNAL="${CCC_BRIDGE_DISTILL_JOURNAL:-${BOT_DATA_DIR:-${PROJECT_ROOT:-$HOME}/.telegram_bot}/danso-distill-journal}"
mkdir -p "$NUNCHI_HOME"
touch "$SEEN"

(
  flock -n 9 || exit 0

  if [ ! -d "$JOURNAL" ]; then
    # Loud, not silent: a Danso node with bridge distill off produces no journal
    # at all, and that is a configuration answer the operator must see. Still a
    # liveness tick so ccc-doctor does not age this into ingest-tick-stale and
    # hide the real cause (#1698).
    echo "danso-feed: no distill journal at $JOURNAL — set CCC_MEMORY_DISTILL_PROVIDER=danso with a non-zero CCC_USAGE_BUDGET_TOKENS_DANSO, or point CCC_BRIDGE_DISTILL_JOURNAL at the bridge's data dir" >&2
    nunchi_write_status "$STATUS" danso 0 0 0 0 '"skipped":"distill-journal-missing"'
    exit 0
  fi

  ingested=0 retired=0 deferred=0 sources=0
  # A job still in flight is left unseen so a later tick picks it up once
  # extraction lands; a finished job with nothing to mirror is marked seen so it
  # is not re-read forever. Exit codes are bridge-journal.py's contract.
  for f in "$JOURNAL"/*.json; do
    [ -f "$f" ] || continue
    sources=$((sources+1))
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

  if [ "$sources" -eq 0 ]; then
    echo "danso-feed: distill journal $JOURNAL is empty — the bridge has not completed a distill job yet" >&2
  elif [ $((ingested + retired)) -gt 0 ]; then
    echo "danso-feed: ingested=$ingested retired=$retired deferred=$deferred"
  fi

  python3 "$FM" snapshot --limit 25 >/dev/null 2>&1 || true
  nunchi_write_status "$STATUS" danso "$sources" "$ingested" "$retired" "$deferred"
) 9>"$LOCK"
