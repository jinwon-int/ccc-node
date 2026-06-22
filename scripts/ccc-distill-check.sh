#!/usr/bin/env bash
# ccc-distill-check.sh — read-only distill health snapshot for fleet verification (#82).
#
# Reports toggle state, last result, queue size, recent log, and trigger counts.
# No mutations, no network calls, no Honcho sends.
# Usage: bash scripts/ccc-distill-check.sh [--json]
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-/root/.claude/state}"
HONCHO_CFG="${CCC_HONCHO_CFG:-/root/.hermes/honcho.json}"
LOG="$STATE_DIR/distill.log"
QUEUE="$STATE_DIR/honcho-queue.jsonl"
DEAD="$STATE_DIR/honcho-queue.jsonl.dead"
LAST="$STATE_DIR/distill-last.json"
DISABLED="$STATE_DIR/distill.disabled"
DRYRUN="$STATE_DIR/distill.dryrun"
OUTPUT="${1:-text}"

# ---- toggle state -----------------------------------------------------------
if   [ -f "$DISABLED" ]; then MODE="OFF"
elif [ -f "$DRYRUN" ];   then MODE="DRY-RUN"
else                          MODE="LIVE"
fi

# ---- queue counts -----------------------------------------------------------
queue_lines=0
[ -f "$QUEUE" ] && [ -s "$QUEUE" ] && queue_lines="$(wc -l < "$QUEUE" | tr -d '[:space:]')"
case "$queue_lines" in ''|*[!0-9]*) queue_lines=0 ;; esac

dead_lines=0
[ -f "$DEAD" ] && [ -s "$DEAD" ] && dead_lines="$(wc -l < "$DEAD" | tr -d '[:space:]')"
case "$dead_lines" in ''|*[!0-9]*) dead_lines=0 ;; esac

# ---- last distill result ----------------------------------------------------
last_summary="none"
if [ -f "$LAST" ] && [ -s "$LAST" ]; then
  last_summary="$(jq -r '"session=\(.session_id // "?") trigger=\(.trigger // "?") at=\(.distilled_at // "?") honcho=\((.honcho | length) // 0) wiki=\((.wiki_candidates | length) // 0)"' "$LAST" 2>/dev/null || echo "parse-error")"
fi

# ---- recent log tail --------------------------------------------------------
log_tail="(log missing or empty)"
if [ -f "$LOG" ] && [ -s "$LOG" ]; then
  log_tail="$(tail -5 "$LOG" 2>/dev/null | sed 's/^/  /')"
fi

# ---- trigger counts (last 14 days) -----------------------------------------
cutoff="$(date -u -d '14 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '0000-00-00T00:00:00Z')"
manual_c=0; sessionend_c=0; precompact_c=0; drain_ok=0; drain_failed=0; drain_drop=0
if [ -f "$LOG" ] && [ -s "$LOG" ]; then
  manual_c="$(awk -v c="$cutoff" '$1>=c && /start trigger=manual/ {n++} END{print n+0}' "$LOG" 2>/dev/null)"
  sessionend_c="$(awk -v c="$cutoff" '$1>=c && /start trigger=sessionend/ {n++} END{print n+0}' "$LOG" 2>/dev/null)"
  precompact_c="$(awk -v c="$cutoff" '$1>=c && /start trigger=precompact/ {n++} END{print n+0}' "$LOG" 2>/dev/null)"
  # POSIX-portable drain counters: use match()+RSTART/RLENGTH+substr() so the
  # script works on both gawk and mawk (Ubuntu/Debian default). The previous
  # 3-argument match($0, /re/, m) form is gawk-only and silently zeroed the
  # counters on mawk, masking #83/#84 acceptance evidence on fleet nodes.
  drain_ok="$(awk -v c="$cutoff" '$1>=c && /\[drain\] drained / {
      match($0,/ok=([0-9]+)/); if (RSTART) n+=substr($0,RSTART+3,RLENGTH-3)
    } END{print n+0}' "$LOG" 2>/dev/null)"
  drain_failed="$(awk -v c="$cutoff" '$1>=c && /\[drain\] drained / {
      match($0,/failed=([0-9]+)/); if (RSTART) n+=substr($0,RSTART+7,RLENGTH-7)
    } END{print n+0}' "$LOG" 2>/dev/null)"
  drain_drop="$(awk -v c="$cutoff" '$1>=c && /\[drain\] drained / {
      match($0,/dropped=([0-9]+)/); if (RSTART) n+=substr($0,RSTART+8,RLENGTH-8)
    } END{print n+0}' "$LOG" 2>/dev/null)"
fi

# ---- Honcho config reachability note ----------------------------------------
honcho_base="(missing)"
if [ -f "$HONCHO_CFG" ]; then
  honcho_base="$(jq -r '.baseUrl // "unset"' "$HONCHO_CFG" 2>/dev/null || echo "parse-error")"
fi

# ---- state dir existence ----------------------------------------------------
state_dir_ok="yes"
[ -d "$STATE_DIR" ] || state_dir_ok="no (missing)"

# ---- output -----------------------------------------------------------------
if [ "$OUTPUT" = "--json" ]; then
  jq -nc \
    --arg mode "$MODE" \
    --arg last "$last_summary" \
    --arg state_dir "$STATE_DIR" \
    --arg state_dir_ok "$state_dir_ok" \
    --arg honcho_base "$honcho_base" \
    --argjson queue_lines "$queue_lines" \
    --argjson dead_lines "$dead_lines" \
    --argjson manual "$manual_c" \
    --argjson sessionend "$sessionend_c" \
    --argjson precompact "$precompact_c" \
    --argjson drain_ok "$drain_ok" \
    --argjson drain_failed "$drain_failed" \
    --argjson drain_drop "$drain_drop" \
    '{mode:$mode, last:$last, state_dir:$state_dir, state_dir_ok:$state_dir_ok,
      honcho_base:$honcho_base,
      queue:{lines:$queue_lines, dead:$dead_lines},
      triggers:{manual:$manual, sessionend:$sessionend, precompact:$precompact},
      drain:{ok:$drain_ok, failed:$drain_failed, dropped:$drain_drop}}'
else
  printf '# ccc distill check\n\n'
  printf -- '- state dir:  `%s` (%s)\n' "$STATE_DIR" "$state_dir_ok"
  printf -- '- mode:       `%s`\n' "$MODE"
  printf -- '- last:       %s\n' "$last_summary"
  printf -- '- queue:      %s lines (dead: %s)\n' "$queue_lines" "$dead_lines"
  printf -- '- honcho:     %s\n' "$honcho_base"
  printf '\n## triggers (14d)\n\n'
  printf -- '- manual:     %s\n' "$manual_c"
  printf -- '- sessionend: %s\n' "$sessionend_c"
  printf -- '- precompact: %s\n' "$precompact_c"
  printf '\n## drain (14d)\n\n'
  printf -- '- ok:     %s\n' "$drain_ok"
  printf -- '- failed: %s\n' "$drain_failed"
  printf -- '- dropped:%s\n' "$drain_drop"
  printf '\n## recent log\n\n%s\n' "$log_tail"
fi
