#!/usr/bin/env bash
# ccc-memory-check.sh — read-only memory cache/profile diagnostics.
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
CACHE="${CCC_MEMORY_CACHE_DIR:-${HOME:-/root}/.claude/hooks/cache}"
PROFILE="${CCC_MEMORY_PROFILE:-standard}"
TTL="${CCC_MEMORY_CACHE_TTL_SEC:-21600}"
WIKI_TTL="${CCC_WIKI_CACHE_MAX_AGE_SEC:-$TTL}"
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
    retries:{snapshot:0, extraction:0, local:0, wiki:0, total:0},
    accounting:{accounted_attempts:0, turn_bytes:0, duration_ms:0, estimated_max_tokens:0, model_counts:{}},
    status_counts:{}, local_status_counts:{}, wiki_status_counts:{}
  }'
}

# Per-record validation/projection shared by the batched and per-file passes
# below. $expected_id must be bound by the caller: the batched pass derives it
# from input_filename, the per-file fallback binds it with --arg. Projects only
# scalar counters and timestamps out of each record. Raw thread ids, messages,
# extraction output, route values, and error text never enter the projected
# records or command output.
WB_VALIDATE_JQ='
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
          and ((.wiki_sink_status // null) | . == null or oneof(["pending","running","retryable_failed","done","terminal_failed","disabled"]))
          and (.created_at | journal_epoch != null)
          and (.attempts | nnint)
          and (.extraction_attempts | nnint)
          and (.local_sink_attempts | nnint)
          and ((.wiki_sink_attempts // 0) | nnint)
          and ((.snapshot // null) | . == null or (type == "object" and (.byte_count | nnint)))
          and ((.extraction_accounting // []) |
            type == "array"
            and length <= $job.extraction_attempts
            and all(.[];
              type == "object"
              and (.model | type == "string" and test("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"))
              and (.snapshot_bytes | nnint)
              and .snapshot_bytes == ($job.snapshot.byte_count // 0)
              and (.duration_ms | nnint)
              and (.estimated_max_tokens | nnint)
            )
          )
        )
      | .status as $status
      | (.local_sink_status // null) as $local
      | (.wiki_sink_status // null) as $wiki
      | ((.memory_audience // null) != null or (.memory_scope // null) != null) as $routed
      | {
          status:$status,
          local_status:$local,
          wiki_status:$wiki,
          created_epoch:(.created_at | journal_epoch),
          snapshot_bytes:((.snapshot.byte_count // 0)),
          snapshot_retries:.attempts,
          extraction_retries:.extraction_attempts,
          local_retries:.local_sink_attempts,
          wiki_retries:(.wiki_sink_attempts // 0),
          accounting:(
            [(.extraction_accounting // [])[] | {
              model, turn_bytes:.snapshot_bytes, duration_ms, estimated_max_tokens
            }]
          ),
          pending:(
            ($status | oneof(["queued","running_snapshot","snapshot_done","retryable_failed","running_extraction","extraction_retryable_failed"]))
            or ($status == "extraction_done" and $local == null and $routed)
            or ($status == "extraction_done" and ($local | oneof(["pending","running","retryable_failed"])))
            or ($status == "extraction_done" and ($wiki | oneof(["pending","running","retryable_failed"])))
          ),
          degraded:(
            ($status | oneof(["retryable_failed","terminal_failed","extraction_retryable_failed","extraction_terminal_failed"]))
            or ($local | oneof(["retryable_failed","terminal_failed"]))
            or ($wiki | oneof(["retryable_failed","terminal_failed"]))
          )
        }
    '

writeback_queue_json() {
  local root="$1" now invalid=0 record_bytes=0 path name size safe
  local expected total_lines batch src_count i
  local -a records=() paths=() statable=() size_lines=() jq_files=() batch_lines=()

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
    statable+=("$path")
  done

  # One size pass for every remaining file (was one `wc -c` spawn per file).
  # wc prints one line per operand in argument order, plus a trailing total
  # line for two or more operands. On any line-count mismatch (e.g. a file
  # vanished between the glob and the batch) fall back to the per-file probe,
  # which keeps the old ''/garbage -> 0 normalization per file.
  if [ "${#statable[@]}" -gt 0 ]; then
    mapfile -t size_lines < <(wc -c "${statable[@]}" 2>/dev/null | awk '{print $1}')
    expected="${#statable[@]}"
    total_lines=$((expected > 1 ? expected + 1 : expected))
    if [ "${#size_lines[@]}" -eq "$total_lines" ]; then
      size_lines=("${size_lines[@]:0:expected}")
    else
      size_lines=()
      for path in "${statable[@]}"; do
        size_lines+=("$(wc -c < "$path" 2>/dev/null | tr -d '[:space:]')")
      done
    fi
  fi

  for i in "${!statable[@]}"; do
    path="${statable[$i]}"
    size="${size_lines[$i]:-}"
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
    jq_files+=("$path")
  done

  # One jq reads every surviving queue file (was one large jq spawn per file):
  # input_filename recovers per-file identity, so the expected job id is the
  # file's own basename, exactly as --arg expected_id passed it before. The
  # first output line counts the distinct files that produced a projected
  # record; a file yielding none is invalid, matching the old per-file
  # empty-output verdict. jq aborts the whole batch when any file is malformed
  # JSON, so that path falls back to the original per-file loop, which
  # reproduces the per-file verdicts one by one.
  if [ "${#jq_files[@]}" -gt 0 ]; then
    if batch="$(jq -cn '
        [ inputs
          | input_filename as $wb_src
          | ($wb_src | split("/") | last | rtrimstr(".json")) as $expected_id
          | ('"$WB_VALIDATE_JQ"') + {_wb_src: $wb_src}
        ] as $tagged
        | ($tagged | map(._wb_src) | unique | length),
          ($tagged[] | del(._wb_src))
      ' "${jq_files[@]}" </dev/null 2>/dev/null)"; then
      mapfile -t batch_lines <<<"$batch"
      src_count="${batch_lines[0]:-0}"
      case "$src_count" in ''|*[!0-9]*) src_count=0;; esac
      invalid=$((invalid + ${#jq_files[@]} - src_count))
      if [ "${#batch_lines[@]}" -gt 1 ]; then
        records=("${batch_lines[@]:1}")
      fi
    else
      for path in "${jq_files[@]}"; do
        name="${path##*/}"
        safe="$(jq -ce --arg expected_id "${name%.json}" "$WB_VALIDATE_JQ" "$path" 2>/dev/null)"
        if [ -z "$safe" ]; then
          invalid=$((invalid + 1))
          continue
        fi
        records+=("$safe")
      done
    fi
  fi

  if [ "${#records[@]}" -eq 0 ]; then
    if [ "$invalid" -gt 0 ]; then
      jq -cn --argjson invalid "$invalid" --argjson bytes "$record_bytes" '{
        status:"degraded", jobs:0, pending_jobs:0, invalid_records:$invalid,
        record_bytes:$bytes, snapshot_bytes:0,
        oldest_age_seconds:-1, oldest_pending_age_seconds:-1,
        retries:{snapshot:0, extraction:0, local:0, wiki:0, total:0},
        accounting:{accounted_attempts:0, turn_bytes:0, duration_ms:0, estimated_max_tokens:0, model_counts:{}},
        status_counts:{}, local_status_counts:{}, wiki_status_counts:{}
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
      | ([ $jobs[] | .wiki_retries ] | add // 0) as $wiki_retries
      | ([ $jobs[] | .accounting[] ]) as $accounting
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
            wiki:$wiki_retries,
            total:($snapshot_retries + $extraction_retries + $local_retries + $wiki_retries)
          },
          accounting:{
            accounted_attempts:($accounting | length),
            turn_bytes:([$accounting[] | .turn_bytes] | add // 0),
            duration_ms:([$accounting[] | .duration_ms] | add // 0),
            estimated_max_tokens:([$accounting[] | .estimated_max_tokens] | add // 0),
            model_counts:(reduce $accounting[] as $item ({}; .[$item.model] = ((.[$item.model] // 0) + 1)))
          },
          status_counts:(reduce $jobs[] as $job ({}; .[$job.status] = ((.[$job.status] // 0) + 1))),
          local_status_counts:(reduce ($jobs[] | select(.local_status != null)) as $job ({}; .[$job.local_status] = ((.[$job.local_status] // 0) + 1))),
          wiki_status_counts:(reduce ($jobs[] | select(.wiki_status != null)) as $job ({}; .[$job.wiki_status] = ((.[$job.wiki_status] // 0) + 1)))
        }
    '
}

wiki_file="$CACHE/wiki.txt"
meta_file="$CACHE/meta.json"
wiki_meta_file="$CACHE/wiki.meta.json"
index_db="$STATE_DIR/memory-index.sqlite"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_MATERIALIZER="${CCC_CODEX_MEMORY_MATERIALIZER_PATH:-$SCRIPT_DIR/ccc_codex_memory.py}"
MEMORY_PROBE="${CCC_MEMORY_PROBE_PATH:-$SCRIPT_DIR/ccc_memory_probe.py}"
BOT_DATA_DIR="${BOT_DATA_DIR:-${PROJECT_ROOT:-$PWD}/.telegram_bot}"
DISTILL_JOURNAL_DIR="${CCC_DISTILL_JOURNAL_DIR:-$BOT_DATA_DIR/distill-journal}"
codex_json='{"status":"unavailable","active_kind":null,"snapshot_sha256":null,"snapshot_bytes":0,"file_bytes":0,"metadata_status":"missing"}'
if [ -x "$CODEX_MATERIALIZER" ] && [ -f "$CODEX_MATERIALIZER" ]; then
  candidate="$(python3 "$CODEX_MATERIALIZER" status --json 2>/dev/null || true)"
  if jq -e 'type == "object" and (.status | type == "string")' >/dev/null 2>&1 <<<"$candidate"; then
    codex_json="$candidate"
  fi
fi
memory_probe_json='{"nunchi":{"status":"unavailable","mode":"unknown","reasons":["probe-unavailable"]},"mempalace":{"status":"unavailable","required":false,"reasons":["probe-unavailable"]}}'
if [ -f "$MEMORY_PROBE" ]; then
  candidate="$(CCC_STATE_DIR="$STATE_DIR" CCC_CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}" \
    python3 "$MEMORY_PROBE" 2>/dev/null || true)"
  if jq -e '.nunchi.status and .mempalace.status' >/dev/null 2>&1 <<<"$candidate"; then
    memory_probe_json="$candidate"
  fi
fi
writeback_json="$(writeback_queue_json "$DISTILL_JOURNAL_DIR")"

wiki_enabled="${CCC_WIKI_MEMORY_ENABLED:-1}"
if [ "${CCC_NODE_ISOLATION_PROFILE:-fleet}" = "external" ]; then
  wiki_enabled=0
fi

wiki_status="disabled"
if ! is_disabled "$wiki_enabled"; then
  wiki_status="$(status_for "$wiki_file" "$WIKI_TTL")"
fi

if [ "$OUTPUT" = "--json" ] || [ "$OUTPUT" = "json" ]; then
  jq -n \
    --arg profile "$PROFILE" \
    --arg cache_dir "$CACHE" \
    --arg state_dir "$STATE_DIR" \
    --arg wiki_status "$wiki_status" \
    --arg meta_file "$meta_file" \
    --argjson wiki_meta "$(meta_json_for "$wiki_meta_file" "$WIKI_TTL")" \
    --argjson codex "$codex_json" \
    --argjson nunchi "$(jq -c '.nunchi' <<<"$memory_probe_json")" \
    --argjson mempalace "$(jq -c '.mempalace' <<<"$memory_probe_json")" \
    --argjson writeback "$writeback_json" \
    --arg index_db "$index_db" \
    --argjson ttl "$TTL" \
    --argjson wiki_age "$(age_for "$wiki_file")" \
    --argjson wiki_bytes "$(bytes_for "$wiki_file")" \
    --argjson index_exists "$([ -f "$index_db" ] && printf true || printf false)" \
    '{profile:$profile, ttl_seconds:$ttl, cache:{dir:$cache_dir, meta:$meta_file}, state_dir:$state_dir,
      wiki:{status:$wiki_status, age_seconds:$wiki_age, bytes:$wiki_bytes, meta:$wiki_meta},
      local_index:{db:$index_db, exists:$index_exists},
      codex:$codex,
      nunchi:$nunchi,
      mempalace:$mempalace,
      writeback_queue:$writeback}'
  exit 0
fi

printf '# ccc memory check\n\n'
printf -- '- profile: %s\n' "$PROFILE"
printf -- '- cache:   %s\n' "$CACHE"
printf -- '- wiki:    %s age=%ss bytes=%s\n' "$wiki_status" "$(age_for "$wiki_file")" "$(bytes_for "$wiki_file")"
printf -- '- index:   %s\n' "$index_db"
# One jq per source blob renders all of that blob's report fields, one output
# line per printf argument (was one jq spawn per field, 35 spawns for these
# five lines). Every field is try-wrapped so a bad path degrades to the same
# empty string a failed per-field spawn used to yield, without aborting the
# fields after it; the :- fallbacks keep set -u safe if jq dies outright.
mapfile -t codex_f < <(jq -r '
  (try (.status) catch ""),
  (try (.active_kind // "none") catch ""),
  (try (.snapshot_sha256 // "none") catch ""),
  (try (.metadata_status // "missing") catch "")' <<<"$codex_json")
printf -- '- codex:   %s kind=%s hash=%s metadata=%s\n' \
  "${codex_f[0]:-}" "${codex_f[1]:-}" "${codex_f[2]:-}" "${codex_f[3]:-}"
mapfile -t probe_f < <(jq -r '
  (try (.nunchi.status) catch ""),
  (try (.nunchi.mode) catch ""),
  (try (.nunchi.db.facts // 0) catch ""),
  (try (.nunchi.snapshot.bytes // 0) catch ""),
  (try (.nunchi.cron.feed // "missing") catch ""),
  (try (.nunchi.reasons | join(",")) catch ""),
  (try (.nunchi.audience_scoped.enabled // false) catch ""),
  (try (.nunchi.audience_scoped.root_status // "disabled") catch ""),
  (try (.nunchi.audience_scoped.scope_count // 0) catch ""),
  (try (.nunchi.audience_scoped.private_count // 0) catch ""),
  (try (.nunchi.audience_scoped.shared_count // 0) catch ""),
  (try (.nunchi.audience_scoped.session_roots // 0) catch ""),
  (try (.nunchi.audience_scoped.nunchi_db_partitions // 0) catch ""),
  (try (.nunchi.audience_scoped.snapshot_partitions // 0) catch ""),
  (try (.nunchi.audience_scoped.mempalace_index_partitions // 0) catch ""),
  (try (.nunchi.audience_scoped.invalid_entries // 0) catch ""),
  (try (.mempalace.status) catch ""),
  (try (.mempalace.required) catch ""),
  (try (.mempalace.embeddings // 0) catch ""),
  (try (.mempalace.reasons | join(",")) catch "")' <<<"$memory_probe_json")
printf -- '- nunchi: %s mode=%s facts=%s snapshot_bytes=%s feed=%s reasons=%s\n' \
  "${probe_f[0]:-}" "${probe_f[1]:-}" "${probe_f[2]:-}" \
  "${probe_f[3]:-}" "${probe_f[4]:-}" "${probe_f[5]:-}"
printf -- '- nunchi_audiences: enabled=%s root=%s scopes=%s private=%s shared=%s sessions=%s dbs=%s snapshots=%s mempalace_indexes=%s invalid=%s\n' \
  "${probe_f[6]:-}" "${probe_f[7]:-}" "${probe_f[8]:-}" "${probe_f[9]:-}" \
  "${probe_f[10]:-}" "${probe_f[11]:-}" "${probe_f[12]:-}" "${probe_f[13]:-}" \
  "${probe_f[14]:-}" "${probe_f[15]:-}"
printf -- '- mempalace: %s required=%s embeddings=%s reasons=%s\n' \
  "${probe_f[16]:-}" "${probe_f[17]:-}" "${probe_f[18]:-}" "${probe_f[19]:-}"
mapfile -t wb_f < <(jq -r '
  (try (.status) catch ""),
  (try (.jobs) catch ""),
  (try (.pending_jobs) catch ""),
  (try (.invalid_records) catch ""),
  (try (.record_bytes) catch ""),
  (try (.snapshot_bytes) catch ""),
  (try (.oldest_age_seconds) catch ""),
  (try (.retries.total) catch ""),
  (try (.accounting.accounted_attempts) catch ""),
  (try (.accounting.estimated_max_tokens) catch ""),
  (try (.accounting.duration_ms) catch "")' <<<"$writeback_json")
printf -- '- writeback: status=%s jobs=%s pending=%s invalid=%s bytes=%s snapshot_bytes=%s oldest=%ss retries=%s accounted=%s estimated_max_tokens=%s duration_ms=%s\n' \
  "${wb_f[0]:-}" "${wb_f[1]:-}" "${wb_f[2]:-}" "${wb_f[3]:-}" "${wb_f[4]:-}" \
  "${wb_f[5]:-}" "${wb_f[6]:-}" "${wb_f[7]:-}" "${wb_f[8]:-}" "${wb_f[9]:-}" \
  "${wb_f[10]:-}"
