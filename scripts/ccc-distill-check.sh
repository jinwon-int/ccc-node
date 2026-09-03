#!/usr/bin/env bash
# ccc-distill-check.sh — read-only distill health snapshot for fleet verification (#82).
#
# Reports toggle state, last result, queue size, recent log, and trigger counts.
# No mutations and no network calls.
# Usage: bash scripts/ccc-distill-check.sh [--json]
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/distill.log"
LAST="$STATE_DIR/distill-last.json"
CKPT_DIR="$STATE_DIR/checkpoints"
DISABLED="$STATE_DIR/distill.disabled"
DRYRUN="$STATE_DIR/distill.dryrun"
COMMON_JOURNAL="${CCC_DISTILL_JOURNAL_DIR:-${PROJECT_ROOT:-$PWD}/.telegram_bot/distill-journal}"
COOLDOWN_DIR="$STATE_DIR/distill-provider-cooldowns"
OUTPUT="${1:-text}"

# Portable mtime select helper (busybox find has no -printf; see #449).
CDC_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || CDC_SELF_DIR=""
# shellcheck source=claude/hooks/lib/mtime-prune.sh
for _cdc_lib in \
  "${CCC_MTIME_PRUNE_LIB:-}" \
  "${CDC_SELF_DIR:+$CDC_SELF_DIR/../claude/hooks/lib/mtime-prune.sh}" \
  "${HOME:-/root}/.claude/hooks/lib/mtime-prune.sh"; do
  if [ -n "$_cdc_lib" ] && [ -r "$_cdc_lib" ]; then . "$_cdc_lib"; break; fi
done

# ---- toggle state -----------------------------------------------------------
if   [ -f "$DISABLED" ]; then MODE="OFF"
elif [ -f "$DRYRUN" ];   then MODE="DRY-RUN"
else                          MODE="LIVE"
fi

# ---- queue counts -----------------------------------------------------------
# ---- provider-neutral journal/circuit counts (body-free) -------------------
common_total=0; common_ready=0; common_retryable=0; common_done=0; common_terminal=0
if [ -d "$COMMON_JOURNAL" ]; then
  for common_job in "$COMMON_JOURNAL"/*.json; do
    [ -f "$common_job" ] || continue
    common_total=$((common_total + 1))
    common_status="$(jq -r '.status // "unknown"' "$common_job" 2>/dev/null || printf 'invalid')"
    case "$common_status" in
      snapshot_done) common_ready=$((common_ready + 1)) ;;
      extraction_retryable_failed) common_retryable=$((common_retryable + 1)) ;;
      extraction_done) common_done=$((common_done + 1)) ;;
      extraction_terminal_failed) common_terminal=$((common_terminal + 1)) ;;
    esac
  done
fi
cooldown_files=0
if [ -d "$COOLDOWN_DIR" ]; then
  cooldown_files="$(find "$COOLDOWN_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | wc -l | tr -d '[:space:]')"
  case "$cooldown_files" in ''|*[!0-9]*) cooldown_files=0 ;; esac
fi

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

# ---- checkpoint freshness ---------------------------------------------------
checkpoint_snapshots=0
checkpoint_last="none"
if [ -d "$CKPT_DIR" ]; then
  checkpoint_snapshots="$(find "$CKPT_DIR" -maxdepth 1 -type f -name 'working-state-*.md' 2>/dev/null | wc -l | tr -d '[:space:]')"
  case "$checkpoint_snapshots" in ''|*[!0-9]*) checkpoint_snapshots=0 ;; esac
  checkpoint_last_path="$(newest_file "$CKPT_DIR" 'working-state-*.md')"
  if [ -n "$checkpoint_last_path" ] && [ -f "$checkpoint_last_path" ]; then
    checkpoint_last="$(basename "$checkpoint_last_path") mtime=$(date -u -r "$checkpoint_last_path" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf 'unknown')"
  fi
fi

# ---- trigger counts (last 14 days) -----------------------------------------
cutoff="$(date -u -d '14 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '0000-00-00T00:00:00Z')"
manual_c=0; sessionend_c=0; precompact_c=0; drain_ok=0; drain_failed=0; drain_drop=0
if [ -f "$LOG" ] && [ -s "$LOG" ]; then
  manual_c="$(awk -v c="$cutoff" '$1>=c && /start trigger=manual/ {n++} END{print n+0}' "$LOG" 2>/dev/null)"
  sessionend_c="$(awk -v c="$cutoff" '$1>=c && /start trigger=sessionend/ {n++} END{print n+0}' "$LOG" 2>/dev/null)"
  precompact_c="$(awk -v c="$cutoff" '$1>=c && /start trigger=precompact/ {n++} END{print n+0}' "$LOG" 2>/dev/null)"
  drain_ok="$(awk -v c="$cutoff" '$1>=c && /\[drain\] drained / {if(match($0,/ok=[0-9]+/)) {n+=substr($0,RSTART+3,RLENGTH-3)}} END{print n+0}' "$LOG" 2>/dev/null)"
  drain_failed="$(awk -v c="$cutoff" '$1>=c && /\[drain\] drained / {if(match($0,/failed=[0-9]+/)) {n+=substr($0,RSTART+7,RLENGTH-7)}} END{print n+0}' "$LOG" 2>/dev/null)"
  drain_drop="$(awk -v c="$cutoff" '$1>=c && /\[drain\] drained / {if(match($0,/dropped=[0-9]+/)) {n+=substr($0,RSTART+8,RLENGTH-8)}} END{print n+0}' "$LOG" 2>/dev/null)"
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
    --arg ckpt_dir "$CKPT_DIR" \
    --arg ckpt_last "$checkpoint_last" \
    --argjson ckpt_snapshots "$checkpoint_snapshots" \
    --argjson manual "$manual_c" \
    --argjson sessionend "$sessionend_c" \
    --argjson precompact "$precompact_c" \
    --argjson drain_ok "$drain_ok" \
    --argjson drain_failed "$drain_failed" \
    --argjson drain_drop "$drain_drop" \
    --arg common_journal "$COMMON_JOURNAL" \
    --argjson common_total "$common_total" \
    --argjson common_ready "$common_ready" \
    --argjson common_retryable "$common_retryable" \
    --argjson common_done "$common_done" \
    --argjson common_terminal "$common_terminal" \
    --argjson cooldown_files "$cooldown_files" \
    '{mode:$mode, last:$last, state_dir:$state_dir, state_dir_ok:$state_dir_ok,
      checkpoint:{dir:$ckpt_dir, snapshots:$ckpt_snapshots, last:$ckpt_last},
      triggers:{manual:$manual, sessionend:$sessionend, precompact:$precompact},
      drain:{ok:$drain_ok, failed:$drain_failed, dropped:$drain_drop},
      provider_neutral:{journal:$common_journal, total:$common_total,
        ready:$common_ready, retryable:$common_retryable, done:$common_done,
        terminal:$common_terminal, cooldown_files:$cooldown_files}}'
else
  printf '# ccc distill check\n\n'
  printf -- '- state dir:  `%s` (%s)\n' "$STATE_DIR" "$state_dir_ok"
  printf -- '- mode:       `%s`\n' "$MODE"
  printf -- '- last:       %s\n' "$last_summary"
  printf -- '- common:     %s jobs (ready: %s, retryable: %s, done: %s, terminal: %s, cooldown files: %s)\n' "$common_total" "$common_ready" "$common_retryable" "$common_done" "$common_terminal" "$cooldown_files"
  printf -- '- checkpoint: %s snapshots (last: %s)\n' "$checkpoint_snapshots" "$checkpoint_last"
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
