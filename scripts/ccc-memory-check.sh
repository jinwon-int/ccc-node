#!/usr/bin/env bash
# ccc-memory-check.sh — read-only memory cache/profile diagnostics.
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-/root/.claude/state}"
CACHE="${CCC_MEMORY_CACHE_DIR:-/root/.claude/hooks/cache}"
HONCHO_CFG="${CCC_HONCHO_CFG:-${CCC_HERMES_DIR:-/root/.hermes}/honcho.json}"
PROFILE="${CCC_MEMORY_PROFILE:-honcho}"
TTL="${CCC_MEMORY_CACHE_TTL_SEC:-21600}"
OUTPUT="${1:-text}"

now_epoch() { date -u +%s; }
file_epoch() { [ -f "$1" ] && date -u -r "$1" +%s 2>/dev/null || printf '0'; }
age_for() {
  local f="$1" ts now
  ts="$(file_epoch "$f")"; now="$(now_epoch)"
  if [ "$ts" = "0" ]; then printf '-1'; else printf '%s' "$((now - ts))"; fi
}
bytes_for() { [ -f "$1" ] && wc -c < "$1" | tr -d '[:space:]' || printf '0'; }
status_for() {
  local f="$1" age
  age="$(age_for "$f")"
  if [ "$age" -lt 0 ]; then printf 'missing'
  elif [ "$age" -gt "$TTL" ]; then printf 'stale'
  else printf 'ok'
  fi
}

wiki_file="$CACHE/wiki.txt"
honcho_file="$CACHE/honcho.txt"
meta_file="$CACHE/meta.json"
index_db="$STATE_DIR/memory-index.sqlite"

honcho_enabled="${CCC_HONCHO_MEMORY_ENABLED:-1}"
honcho_base="(missing)"
if [ -f "$HONCHO_CFG" ]; then
  honcho_base="$(jq -r '.baseUrl // "unset"' "$HONCHO_CFG" 2>/dev/null || printf 'parse-error')"
fi

wiki_status="$(status_for "$wiki_file")"
honcho_status="disabled"
if [ "$honcho_enabled" != "0" ] && [ "$honcho_enabled" != "false" ] && [ "$honcho_enabled" != "off" ]; then
  honcho_status="$(status_for "$honcho_file")"
fi

if [ "$OUTPUT" = "--json" ] || [ "$OUTPUT" = "json" ]; then
  jq -n \
    --arg profile "$PROFILE" \
    --arg cache_dir "$CACHE" \
    --arg state_dir "$STATE_DIR" \
    --arg honcho_cfg "$HONCHO_CFG" \
    --arg honcho_base "$honcho_base" \
    --arg wiki_status "$wiki_status" \
    --arg honcho_status "$honcho_status" \
    --arg meta_file "$meta_file" \
    --arg index_db "$index_db" \
    --argjson ttl "$TTL" \
    --argjson wiki_age "$(age_for "$wiki_file")" \
    --argjson honcho_age "$(age_for "$honcho_file")" \
    --argjson wiki_bytes "$(bytes_for "$wiki_file")" \
    --argjson honcho_bytes "$(bytes_for "$honcho_file")" \
    --argjson index_exists "$([ -f "$index_db" ] && printf true || printf false)" \
    '{profile:$profile, ttl_seconds:$ttl, cache:{dir:$cache_dir, meta:$meta_file}, state_dir:$state_dir,
      wiki:{status:$wiki_status, age_seconds:$wiki_age, bytes:$wiki_bytes},
      honcho:{status:$honcho_status, age_seconds:$honcho_age, bytes:$honcho_bytes, cfg:$honcho_cfg, base:$honcho_base},
      local_index:{db:$index_db, exists:$index_exists}}'
  exit 0
fi

printf '# ccc memory check\n\n'
printf -- '- profile: %s\n' "$PROFILE"
printf -- '- cache:   %s\n' "$CACHE"
printf -- '- wiki:    %s age=%ss bytes=%s\n' "$wiki_status" "$(age_for "$wiki_file")" "$(bytes_for "$wiki_file")"
printf -- '- honcho:  %s age=%ss bytes=%s base=%s\n' "$honcho_status" "$(age_for "$honcho_file")" "$(bytes_for "$honcho_file")" "$honcho_base"
printf -- '- index:   %s\n' "$index_db"
