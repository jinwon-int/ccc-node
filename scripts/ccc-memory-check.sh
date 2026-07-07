#!/usr/bin/env bash
# ccc-memory-check.sh — read-only memory cache/profile diagnostics.
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
CACHE="${CCC_MEMORY_CACHE_DIR:-${HOME:-/root}/.claude/hooks/cache}"
HONCHO_CFG="${CCC_HONCHO_CFG:-${CCC_HERMES_DIR:-${HOME:-/root}/.hermes}/honcho.json}"
PROFILE="${CCC_MEMORY_PROFILE:-honcho}"
TTL="${CCC_MEMORY_CACHE_TTL_SEC:-21600}"
WIKI_TTL="${CCC_WIKI_CACHE_MAX_AGE_SEC:-$TTL}"
HONCHO_TTL="${CCC_HONCHO_CACHE_MAX_AGE_SEC:-$TTL}"
OUTPUT="${1:-text}"

now_epoch() { date -u +%s; }
is_disabled() { case "${1:-}" in 0|false|FALSE|off|OFF|no|NO) return 0;; *) return 1;; esac; }
file_epoch() { [ -f "$1" ] && date -u -r "$1" +%s 2>/dev/null || printf '0'; }
age_for() {
  local f="$1" ts now
  ts="$(file_epoch "$f")"; now="$(now_epoch)"
  # `printf '-1'` treats -1 as a flag ("invalid option") and emits nothing,
  # which makes --json fail (--argjson gets "") and text mode misreport a
  # missing cache as healthy. Use `printf '%s'` so the literal -1 is emitted.
  if [ "$ts" = "0" ]; then printf '%s' '-1'; else printf '%s' "$((now - ts))"; fi
}
bytes_for() { [ -f "$1" ] && wc -c < "$1" | tr -d '[:space:]' || printf '0'; }
meta_json_for() {
  local f="$1" ttl="$2"
  if [ ! -f "$f" ]; then printf '{}'; return 0; fi
  jq --argjson ttl "${ttl:-0}" '
    (.max_age_sec //= $ttl)
    | (.stale = (((.refreshed_at? // "") | fromdateiso8601? // 0) as $t | ($t > 0 and ((now | floor) - $t > (.max_age_sec // $ttl)))))
  ' "$f" 2>/dev/null || printf '{}'
}
status_for() {
  # Use the per-source TTL (falling back to the global one) so the status line
  # agrees with the per-source staleness the meta computation reports.
  local f="$1" ttl="${2:-$TTL}" age
  age="$(age_for "$f")"
  if [ "$age" -lt 0 ]; then printf 'missing'
  elif [ "$age" -gt "$ttl" ]; then printf 'stale'
  else printf 'ok'
  fi
}

wiki_file="$CACHE/wiki.txt"
honcho_file="$CACHE/honcho.txt"
meta_file="$CACHE/meta.json"
wiki_meta_file="$CACHE/wiki.meta.json"
honcho_meta_file="$CACHE/honcho.meta.json"
index_db="$STATE_DIR/memory-index.sqlite"

honcho_enabled="${CCC_HONCHO_MEMORY_ENABLED:-1}"
honcho_base="(missing)"
if [ -f "$HONCHO_CFG" ]; then
  # Mirror refresh-memory.sh: the config may use the nested `.hosts.hermes.*`
  # schema instead of top-level keys. Read top-level first, then fall back so the
  # diagnostic reports the same base URL the refresh path actually resolves.
  honcho_base="$(jq -r 'def nz(x): x | select(. != null and . != ""); nz(.baseUrl) // nz(.hosts.hermes.baseUrl) // "unset"' "$HONCHO_CFG" 2>/dev/null || printf 'parse-error')"
fi

wiki_status="$(status_for "$wiki_file" "$WIKI_TTL")"
honcho_status="disabled"
if ! is_disabled "$honcho_enabled"; then
  honcho_status="$(status_for "$honcho_file" "$HONCHO_TTL")"
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
    --argjson wiki_meta "$(meta_json_for "$wiki_meta_file" "$WIKI_TTL")" \
    --argjson honcho_meta "$(meta_json_for "$honcho_meta_file" "$HONCHO_TTL")" \
    --arg index_db "$index_db" \
    --argjson ttl "$TTL" \
    --argjson wiki_age "$(age_for "$wiki_file")" \
    --argjson honcho_age "$(age_for "$honcho_file")" \
    --argjson wiki_bytes "$(bytes_for "$wiki_file")" \
    --argjson honcho_bytes "$(bytes_for "$honcho_file")" \
    --argjson index_exists "$([ -f "$index_db" ] && printf true || printf false)" \
    '{profile:$profile, ttl_seconds:$ttl, cache:{dir:$cache_dir, meta:$meta_file}, state_dir:$state_dir,
      wiki:{status:$wiki_status, age_seconds:$wiki_age, bytes:$wiki_bytes, meta:$wiki_meta},
      honcho:{status:$honcho_status, age_seconds:$honcho_age, bytes:$honcho_bytes, cfg:$honcho_cfg, base:$honcho_base, meta:$honcho_meta},
      local_index:{db:$index_db, exists:$index_exists}}'
  exit 0
fi

printf '# ccc memory check\n\n'
printf -- '- profile: %s\n' "$PROFILE"
printf -- '- cache:   %s\n' "$CACHE"
printf -- '- wiki:    %s age=%ss bytes=%s\n' "$wiki_status" "$(age_for "$wiki_file")" "$(bytes_for "$wiki_file")"
printf -- '- honcho:  %s age=%ss bytes=%s base=%s\n' "$honcho_status" "$(age_for "$honcho_file")" "$(bytes_for "$honcho_file")" "$honcho_base"
printf -- '- index:   %s\n' "$index_db"
