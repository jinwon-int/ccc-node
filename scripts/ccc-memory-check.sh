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

now_epoch() {
  case "${CCC_MEMORY_CHECK_NOW_EPOCH:-}" in
    ''|*[!0-9]*) date -u +%s ;;
    *) printf '%s' "$CCC_MEMORY_CHECK_NOW_EPOCH" ;;
  esac
}
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

empty_writeback_json() {
  local status="$1" invalid="${2:-0}"
  jq -cn --arg status "$status" --argjson invalid "$invalid" '{
    status:$status, jobs:0, pending_jobs:0, invalid_records:$invalid,
    record_bytes:0, snapshot_bytes:0,
    oldest_age_seconds:-1, oldest_pending_age_seconds:-1,
    retries:{snapshot:0, extraction:0, local:0, total:0},
    status_counts:{}, local_status_counts:{}
  }'
}

writeback_queue_json() {
  local root="$1" now invalid=0 record_bytes=0 path name size safe
  local -a records=() paths=()

  # Diagnostics are strictly read-only. In particular, a missing queue is not
  # initialized here, and an unsafe root is never traversed.
  if [ -L "$root" ] || { [ -e "$root" ] && [ ! -d "$root" ]; }; then
    empty_writeback_json degraded 1
    return 0
  fi
  if [ ! -e "$root" ]; then
    empty_writeback_json missing 0
    return 0
  fi

  shopt -s nullglob
  paths=("$root"/*.json)
  shopt -u nullglob
  now="$(now_epoch)"

  for path in "${paths[@]}"; do
    if [ -L "$path" ] || [ ! -f "$path" ]; then
      invalid=$((invalid + 1))
      continue
    fi
    size="$(wc -c < "$path" 2>/dev/null | tr -d '[:space:]')"
    case "$size" in ''|*[!0-9]*) size=0;; esac
    record_bytes=$((record_bytes + size))
    if [ "$size" -gt 1048576 ]; then
      invalid=$((invalid + 1))
      continue
    fi
    name="${path##*/}"
    if [[ ! "$name" =~ ^[0-9a-f]{64}\.json$ ]]; then
      invalid=$((invalid + 1))
      continue
    fi

    # Project only scalar counters and timestamps out of each record. Raw
    # thread ids, messages, extraction output, route values, and error text
    # never enter the projected records or command output.
    safe="$(jq -ce --arg expected_id "${name%.json}" '
      def nnint: type == "number" and floor == . and . >= 0;
      def oneof($values): . as $value | ($values | index($value)) != null;
      def journal_epoch:
        if type == "string"
        then (sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601?)
        else null
        end;
      . as $job
      | select(
          type == "object"
          and .job_id == $expected_id
          and .provider == "codex"
          and (.thread_hash | type == "string" and test("^[0-9a-f]{64}$"))
          and (.trigger | oneof(["new_command","provider_switch","auto_new","explicit","shutdown","checkpoint"]))
          and (.status | oneof(["queued","running_snapshot","snapshot_done","retryable_failed","terminal_failed","running_extraction","extraction_retryable_failed","extraction_done","extraction_terminal_failed"]))
          and ((.local_sink_status // null) | . == null or oneof(["pending","running","retryable_failed","done","terminal_failed","unroutable"]))
          and (.created_at | journal_epoch != null)
          and (.attempts | nnint)
          and (.extraction_attempts | nnint)
          and (.local_sink_attempts | nnint)
          and ((.snapshot // null) | . == null or (type == "object" and (.byte_count | nnint)))
        )
      | .status as $status
      | (.local_sink_status // null) as $local
      | ((.memory_audience // null) != null or (.memory_scope // null) != null) as $routed
      | {
          status:$status,
          local_status:$local,
          created_epoch:(.created_at | journal_epoch),
          snapshot_bytes:((.snapshot.byte_count // 0)),
          snapshot_retries:.attempts,
          extraction_retries:.extraction_attempts,
          local_retries:.local_sink_attempts,
          pending:(
            ($status | oneof(["queued","running_snapshot","snapshot_done","retryable_failed","running_extraction","extraction_retryable_failed"]))
            or ($status == "extraction_done" and $local == null and $routed)
            or ($status == "extraction_done" and ($local | oneof(["pending","running","retryable_failed"])))
          ),
          degraded:(
            ($status | oneof(["retryable_failed","terminal_failed","extraction_retryable_failed","extraction_terminal_failed"]))
            or ($local | oneof(["retryable_failed","terminal_failed"]))
          )
        }
    ' "$path" 2>/dev/null)"
    if [ -z "$safe" ]; then
      invalid=$((invalid + 1))
      continue
    fi
    records+=("$safe")
  done

  if [ "${#records[@]}" -eq 0 ]; then
    if [ "$invalid" -gt 0 ]; then
      jq -cn --argjson invalid "$invalid" --argjson bytes "$record_bytes" '{
        status:"degraded", jobs:0, pending_jobs:0, invalid_records:$invalid,
        record_bytes:$bytes, snapshot_bytes:0,
        oldest_age_seconds:-1, oldest_pending_age_seconds:-1,
        retries:{snapshot:0, extraction:0, local:0, total:0},
        status_counts:{}, local_status_counts:{}
      }'
    else
      empty_writeback_json empty 0
    fi
    return 0
  fi

  printf '%s\n' "${records[@]}" | jq -cs \
    --argjson now "$now" \
    --argjson invalid "$invalid" \
    --argjson record_bytes "$record_bytes" '
      . as $jobs
      | ([ $jobs[] | select(.pending) ] | length) as $pending
      | ([ $jobs[] | ($now - .created_epoch) | if . < 0 then 0 else . end ] | max // -1) as $oldest
      | ([ $jobs[] | select(.pending) | ($now - .created_epoch) | if . < 0 then 0 else . end ] | max // -1) as $oldest_pending
      | ([ $jobs[] | .snapshot_retries ] | add // 0) as $snapshot_retries
      | ([ $jobs[] | .extraction_retries ] | add // 0) as $extraction_retries
      | ([ $jobs[] | .local_retries ] | add // 0) as $local_retries
      | {
          status:(
            if $invalid > 0 or any($jobs[]; .degraded) then "degraded"
            elif $pending > 0 then "active"
            else "settled"
            end
          ),
          jobs:($jobs | length),
          pending_jobs:$pending,
          invalid_records:$invalid,
          record_bytes:$record_bytes,
          snapshot_bytes:([ $jobs[] | .snapshot_bytes ] | add // 0),
          oldest_age_seconds:$oldest,
          oldest_pending_age_seconds:$oldest_pending,
          retries:{
            snapshot:$snapshot_retries,
            extraction:$extraction_retries,
            local:$local_retries,
            total:($snapshot_retries + $extraction_retries + $local_retries)
          },
          status_counts:(reduce $jobs[] as $job ({}; .[$job.status] = ((.[$job.status] // 0) + 1))),
          local_status_counts:(reduce ($jobs[] | select(.local_status != null)) as $job ({}; .[$job.local_status] = ((.[$job.local_status] // 0) + 1)))
        }
    '
}

wiki_file="$CACHE/wiki.txt"
honcho_file="$CACHE/honcho.txt"
meta_file="$CACHE/meta.json"
wiki_meta_file="$CACHE/wiki.meta.json"
honcho_meta_file="$CACHE/honcho.meta.json"
index_db="$STATE_DIR/memory-index.sqlite"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_MATERIALIZER="${CCC_CODEX_MEMORY_MATERIALIZER_PATH:-$SCRIPT_DIR/ccc_codex_memory.py}"
BOT_DATA_DIR="${BOT_DATA_DIR:-${PROJECT_ROOT:-$PWD}/.telegram_bot}"
DISTILL_JOURNAL_DIR="${CCC_DISTILL_JOURNAL_DIR:-$BOT_DATA_DIR/distill-journal}"
codex_json='{"status":"unavailable","active_kind":null,"snapshot_sha256":null,"snapshot_bytes":0,"file_bytes":0,"metadata_status":"missing"}'
if [ -x "$CODEX_MATERIALIZER" ] && [ -f "$CODEX_MATERIALIZER" ]; then
  candidate="$("$CODEX_MATERIALIZER" status --json 2>/dev/null || true)"
  if jq -e 'type == "object" and (.status | type == "string")' >/dev/null 2>&1 <<<"$candidate"; then
    codex_json="$candidate"
  fi
fi
writeback_json="$(writeback_queue_json "$DISTILL_JOURNAL_DIR")"

honcho_enabled="${CCC_HONCHO_MEMORY_ENABLED:-1}"
wiki_enabled="${CCC_WIKI_MEMORY_ENABLED:-1}"
if [ "${CCC_NODE_ISOLATION_PROFILE:-fleet}" = "external" ]; then
  wiki_enabled=0
fi
honcho_base="(missing)"
if [ -f "$HONCHO_CFG" ]; then
  # Mirror refresh-memory.sh: the config may use the nested `.hosts.hermes.*`
  # schema instead of top-level keys. Read top-level first, then fall back so the
  # diagnostic reports the same base URL the refresh path actually resolves.
  honcho_base="$(jq -r 'def nz(x): x | select(. != null and . != ""); nz(.baseUrl) // nz(.hosts.hermes.baseUrl) // "unset"' "$HONCHO_CFG" 2>/dev/null || printf 'parse-error')"
fi

wiki_status="disabled"
if ! is_disabled "$wiki_enabled"; then
  wiki_status="$(status_for "$wiki_file" "$WIKI_TTL")"
fi
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
    --argjson codex "$codex_json" \
    --argjson writeback "$writeback_json" \
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
      local_index:{db:$index_db, exists:$index_exists},
      codex:$codex,
      writeback_queue:$writeback}'
  exit 0
fi

printf '# ccc memory check\n\n'
printf -- '- profile: %s\n' "$PROFILE"
printf -- '- cache:   %s\n' "$CACHE"
printf -- '- wiki:    %s age=%ss bytes=%s\n' "$wiki_status" "$(age_for "$wiki_file")" "$(bytes_for "$wiki_file")"
printf -- '- honcho:  %s age=%ss bytes=%s base=%s\n' "$honcho_status" "$(age_for "$honcho_file")" "$(bytes_for "$honcho_file")" "$honcho_base"
printf -- '- index:   %s\n' "$index_db"
printf -- '- codex:   %s kind=%s hash=%s metadata=%s\n' \
  "$(jq -r '.status' <<<"$codex_json")" \
  "$(jq -r '.active_kind // "none"' <<<"$codex_json")" \
  "$(jq -r '.snapshot_sha256 // "none"' <<<"$codex_json")" \
  "$(jq -r '.metadata_status // "missing"' <<<"$codex_json")"
printf -- '- writeback: status=%s jobs=%s pending=%s invalid=%s bytes=%s snapshot_bytes=%s oldest=%ss retries=%s\n' \
  "$(jq -r '.status' <<<"$writeback_json")" \
  "$(jq -r '.jobs' <<<"$writeback_json")" \
  "$(jq -r '.pending_jobs' <<<"$writeback_json")" \
  "$(jq -r '.invalid_records' <<<"$writeback_json")" \
  "$(jq -r '.record_bytes' <<<"$writeback_json")" \
  "$(jq -r '.snapshot_bytes' <<<"$writeback_json")" \
  "$(jq -r '.oldest_age_seconds' <<<"$writeback_json")" \
  "$(jq -r '.retries.total' <<<"$writeback_json")"
