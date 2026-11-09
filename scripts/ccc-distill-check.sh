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

# shellcheck source=claude/hooks/lib/distill-journal.sh
. "$CDC_SELF_DIR/../claude/hooks/lib/distill-journal.sh" || exit 1
ccc_check_distill_journal || exit 1
COMMON_JOURNAL="$CCC_CHECK_JOURNAL"

# ---- toggle state -----------------------------------------------------------
if   [ -f "$DISABLED" ]; then MODE="OFF"
elif [ -f "$DRYRUN" ];   then MODE="DRY-RUN"
else                          MODE="LIVE"
fi

# ---- queue counts -----------------------------------------------------------
# ---- provider-neutral journal/circuit counts (body-free) -------------------
# One body-free count pass per journal: "<total> <ready> <retryable> <done> <terminal>".
count_journal() {
  local dir="$1" job status total=0 ready=0 retryable=0 done_n=0 terminal=0
  if [ -d "$dir" ] && [ ! -L "$dir" ]; then
    for job in "$dir"/*.json; do
      [ -f "$job" ] && [ ! -L "$job" ] || continue
      total=$((total + 1))
      status="$(jq -r '.status // "unknown"' "$job" 2>/dev/null || printf 'invalid')"
      case "$status" in
        snapshot_done) ready=$((ready + 1)) ;;
        extraction_retryable_failed) retryable=$((retryable + 1)) ;;
        extraction_done) done_n=$((done_n + 1)) ;;
        extraction_terminal_failed) terminal=$((terminal + 1)) ;;
      esac
    done
  fi
  printf '%s %s %s %s %s' "$total" "$ready" "$retryable" "$done_n" "$terminal"
}
common_total=0; common_ready=0; common_retryable=0; common_done=0; common_terminal=0
if [ "$CCC_CHECK_JOURNAL_STATUS" = present ]; then
  read -r common_total common_ready common_retryable common_done common_terminal \
    <<<"$(count_journal "$COMMON_JOURNAL")"
fi

# Every channel journal with its owner (#2720 follow-up). The selector above
# sees the one journal BOT_DATA_DIR names; outside a bridge session that is the
# Telegram data dir even on nodes that retired Telegram, so a failing hande or
# Matrix journal went unseen. Terminal failures are split by owner: a live (or
# override) journal is a real write-back loss, an orphaned one is a leftover no
# bridge will retry or prune.
DC_NOW="${CCC_DISTILL_CHECK_NOW_EPOCH:-}"
case "$DC_NOW" in ''|*[!0-9]*) DC_NOW="$(date -u +%s)" ;; esac
DC_LIVE_SEC="${CCC_CHECK_JOURNAL_LIVE_SEC:-3600}"
case "$DC_LIVE_SEC" in ''|*[!0-9]*|0) DC_LIVE_SEC=3600 ;; esac
journals_json='[]'
while IFS= read -r journal_path; do
  [ -n "$journal_path" ] || continue
  read -r j_total j_ready j_retryable j_done j_terminal <<<"$(count_journal "$journal_path")"
  read -r j_owner j_health_age <<<"$(ccc_check_distill_journal_owner "$journal_path" "$DC_NOW" "$DC_LIVE_SEC")"
  j_channel="$(basename "$(dirname "$journal_path")")"
  j_channel="${j_channel#.}"
  case "$journal_path" in */danso-distill-journal) j_channel="$j_channel/danso" ;; esac
  journals_json="$(jq -c --arg path "$journal_path" --arg channel "$j_channel" --arg owner "$j_owner" \
    --argjson health_age "$j_health_age" --argjson total "$j_total" --argjson ready "$j_ready" \
    --argjson retryable "$j_retryable" --argjson done_n "$j_done" --argjson terminal "$j_terminal" '
    . + [{path:$path, channel:$channel, owner:$owner, health_age_seconds:$health_age,
          total:$total, ready:$ready, retryable:$retryable, done:$done_n, terminal:$terminal}]' <<<"$journals_json")"
done < <(ccc_check_distill_journal_candidates)
live_terminal="$(jq '[.[] | select(.owner == "live" or .owner == "override") | .terminal] | add // 0' <<<"$journals_json")"
orphaned_terminal="$(jq '[.[] | select(.owner == "orphaned") | .terminal] | add // 0' <<<"$journals_json")"
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
    --arg journal_status "$CCC_CHECK_JOURNAL_STATUS" \
    --arg journal_reason "$CCC_CHECK_JOURNAL_REASON" \
    --arg journal_source "$CCC_CHECK_JOURNAL_SOURCE" \
    --arg journal_provider "$CCC_CHECK_JOURNAL_PROVIDER" \
    --argjson common_total "$common_total" \
    --argjson common_ready "$common_ready" \
    --argjson common_retryable "$common_retryable" \
    --argjson common_done "$common_done" \
    --argjson common_terminal "$common_terminal" \
    --argjson cooldown_files "$cooldown_files" \
    --argjson journals "$journals_json" \
    --argjson live_terminal "$live_terminal" \
    --argjson orphaned_terminal "$orphaned_terminal" \
    --argjson live_sec "$DC_LIVE_SEC" \
    '{mode:$mode, last:$last, state_dir:$state_dir, state_dir_ok:$state_dir_ok,
      checkpoint:{dir:$ckpt_dir, snapshots:$ckpt_snapshots, last:$ckpt_last},
      triggers:{manual:$manual, sessionend:$sessionend, precompact:$precompact},
      drain:{ok:$drain_ok, failed:$drain_failed, dropped:$drain_drop},
      provider_neutral:{journal:$common_journal, journal_status:$journal_status,
        reason:$journal_reason, source:$journal_source, provider:$journal_provider, total:$common_total,
        ready:$common_ready, retryable:$common_retryable, done:$common_done,
        terminal:$common_terminal, cooldown_files:$cooldown_files,
        journals:$journals, live_terminal:$live_terminal, orphaned_terminal:$orphaned_terminal,
        live_threshold_seconds:$live_sec}}'
else
  printf '# ccc distill check\n\n'
  printf -- '- state dir:  `%s` (%s)\n' "$STATE_DIR" "$state_dir_ok"
  printf -- '- mode:       `%s`\n' "$MODE"
  printf -- '- last:       %s\n' "$last_summary"
  printf -- '- journal:    `%s` (%s; %s)\n' "$COMMON_JOURNAL" "$CCC_CHECK_JOURNAL_STATUS" "$CCC_CHECK_JOURNAL_REASON"
  printf -- '- common:     %s jobs (ready: %s, retryable: %s, done: %s, terminal: %s, cooldown files: %s)\n' "$common_total" "$common_ready" "$common_retryable" "$common_done" "$common_terminal" "$cooldown_files"
  printf -- '- journals:   live_terminal=%s orphaned_terminal=%s live_threshold=%ss\n' "$live_terminal" "$orphaned_terminal" "$DC_LIVE_SEC"
  jq -r '.[] | "  - \(.channel): owner=\(.owner) jobs=\(.total) ready=\(.ready) retryable=\(.retryable) done=\(.done) terminal=\(.terminal) health_age=\(.health_age_seconds)s"' <<<"$journals_json" 2>/dev/null || :
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
