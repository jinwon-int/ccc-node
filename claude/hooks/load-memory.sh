#!/usr/bin/env bash
# SessionStart memory bootstrap for a Claude Code node (node-owned memory).
# Serves built-in MEMORY/USER + bounded cached Family Wiki/Honcho/local hot memory instantly,
# then fires a detached background refresh so the next session is fresh.
set -uo pipefail

# Distill subprocess guard: when a distill pipeline spawns `claude -p ...`,
# we don't want the child to re-load memory / refresh caches / fire more
# distillations. See ~/.claude/hooks/distill.sh for the parent setter.
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

EVENT="${1:-SessionStart}"
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
CACHE="${CCC_MEMORY_CACHE_DIR:-${HOME:-/root}/.claude/hooks/cache}"
HOOKDIR="${CCC_HOOK_DIR:-${HOME:-/root}/.claude/hooks}"
MEMDIR="${CCC_MEMORY_DIR:-${HOME:-/root}/.claude/memories}"
PROFILE="${CCC_MEMORY_PROFILE:-honcho}"
TTL="${CCC_MEMORY_CACHE_TTL_SEC:-21600}"
MAX_TOTAL="${CCC_MEMORY_MAX_BYTES:-12000}"
MAX_MEM="${CCC_BUILTIN_MEMORY_MAX_BYTES:-4000}"
MAX_WIKI="${CCC_WIKI_MAX_BYTES:-5000}"
MAX_HONCHO="${CCC_HONCHO_MAX_BYTES:-4000}"
MAX_LOCAL="${CCC_LOCAL_MEMORY_MAX_BYTES:-3000}"
# Skill index (#1145): node skills are plain files the session cannot see, so
# name-keyword searches miss them (gh-pr-flow sat undiscovered through three
# round-trips while its description held the exact answer). Inject a bounded
# name+description index so discovery starts from descriptions, not filenames.
MAX_SKILLS="${CCC_SKILL_INDEX_MAX_BYTES:-1500}"
SKILLS_ENABLED="${CCC_SKILL_INDEX_ENABLED:-1}"
SKILLS_DIR="${CCC_SKILLS_DIR:-${HOME:-/root}/.claude/skills}"
MAX_RESUME="${CCC_RESUME_MAX_BYTES:-2000}"
HONCHO_ENABLED="${CCC_HONCHO_MEMORY_ENABLED:-1}"
WIKI_ENABLED="${CCC_WIKI_MEMORY_ENABLED:-1}"
ISOLATION_PROFILE="${CCC_NODE_ISOLATION_PROFILE:-fleet}"
[ "$ISOLATION_PROFILE" = "external" ] && WIKI_ENABLED=0
USER_LABEL="${CCC_MEMORY_USER_LABEL:-Seo Jin On}"
# Local hot-memory search is ON by default for every profile now that the
# default retrieval reranks with durability/source/recency boosts; set
# CCC_LOCAL_MEMORY_ENABLED=0/false/off to opt out. hybrid/max-perf always query
# it regardless (that is part of their definition).
LOCAL_ENABLED="${CCC_LOCAL_MEMORY_ENABLED:-}"
QUERY="${CCC_MEMORY_QUERY:-}"
LEGACY_STATE_DIR="${CCC_MEMORY_LEGACY_STATE_DIR:-${HOME:-/root}/.claude/state}"
LEGACY_CACHE_DIR="${CCC_MEMORY_LEGACY_CACHE_DIR:-${HOME:-/root}/.claude/hooks/cache}"
LEGACY_MEMDIR="${CCC_MEMORY_LEGACY_DIR:-${HOME:-/root}/.claude/memories}"
LEGACY_HERMES_MEMDIR="${CCC_MEMORY_LEGACY_HERMES_DIR:-${HOME:-/root}/.hermes/memories}"
LEGACY_RESUME_FILE="${CCC_MEMORY_LEGACY_RESUME_FILE:-$LEGACY_STATE_DIR/resume.md}"
RESUME_FILE="${CCC_RESUME_FILE:-$STATE_DIR/resume.md}"
# Working-state checkpoint block (#1176). On Claude nodes checkpoint.sh owns
# this file across PreCompact/PostCompact, so the default here is OFF and the
# Claude output stays byte-identical. The Codex/Piri materializer (whose only
# snapshot source is this loader) turns it on, which gives those providers the
# same "continue from here" context at session start and on every
# compaction_end refresh. PostCompact is always skipped so a Claude node that
# opts in for SessionStart never double-injects next to checkpoint.sh.
WS_INJECT="${CCC_MEMORY_INJECT_WORKING_STATE:-0}"
MAX_WS="${CCC_WORKING_STATE_MAX_BYTES:-2048}"
WS_FILE="${CCC_WORKING_STATE:-$STATE_DIR/working-state.md}"
LEGACY_WS_FILE="${CCC_MEMORY_LEGACY_WORKING_STATE:-$LEGACY_STATE_DIR/working-state.md}"

LOAD_MEMORY_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || LOAD_MEMORY_LIB_DIR="$HOOKDIR"
# shellcheck source=claude/hooks/lib/hook-common.sh
. "$LOAD_MEMORY_LIB_DIR/lib/hook-common.sh" || exit 0
# shellcheck source=claude/hooks/lib/memory-common.sh
. "$LOAD_MEMORY_LIB_DIR/lib/memory-common.sh" || exit 0
# Rendering/budget/bounded-search helpers (#584 P2-1): the former inline python3
# heredocs live in this stdlib-only module. Every caller keeps its fail-open
# `||` fallback, so a missing module degrades exactly like a heredoc failure.
MEMORY_RENDER_PY="$LOAD_MEMORY_LIB_DIR/lib/memory_render.py"

# Stage timing instrumentation (#897 step 1): EPOCHREALTIME marks around the
# expensive stages, appended as ONE body-free JSON line per run to
# $STATE_DIR/memory-timing.jsonl so the static latency hypotheses (serial
# search chain etc.) can be verified against real fleet data before any
# optimization lands. Default on; CCC_MEMORY_TIMING=0 opts out. Values are
# integer milliseconds under fixed stage names only — never memory content.
TIMING_ENABLED=0
if ! is_disabled "${CCC_MEMORY_TIMING:-1}" && [ -n "${EPOCHREALTIME:-}" ]; then
  TIMING_ENABLED=1
fi
_TIMING_START=0
_TIMING_PREV=0
_timing_marks=""
_now_us() { # EPOCHREALTIME -> integer microseconds (locale-safe dot/comma strip)
  local t="${EPOCHREALTIME//[.,]/}"
  printf '%s' "${t:-0}"
}
_mark() { # <stage> — record ms elapsed since the previous mark
  [ "$TIMING_ENABLED" = 1 ] || return 0
  local now prev
  now="$(_now_us)"
  prev="$_TIMING_PREV"
  [ "$prev" -gt 0 ] || prev="$now"
  [ -n "$_timing_marks" ] && _timing_marks+=" "
  _timing_marks+="$1=$(( (now - prev) / 1000 ))"
  _TIMING_PREV="$now"
}
_timing_flush() { # append one bounded JSON line; best-effort, never fails the hook
  [ "$TIMING_ENABLED" = 1 ] || return 0
  [ "$_TIMING_START" -gt 0 ] || return 0
  local file="$STATE_DIR/memory-timing.jsonl" now total m stages="" size
  now="$(_now_us)"
  total=$(( (now - _TIMING_START) / 1000 ))
  for m in $_timing_marks; do
    [ -n "$stages" ] && stages+=","
    stages+="\"${m%%=*}\":${m##*=}"
  done
  printf '%s\n' "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"$EVENT\",\"total_ms\":$total,\"stages\":{$stages}}" >> "$file" 2>/dev/null || return 0
  size="$(wc -c < "$file" 2>/dev/null || printf '0')"
  if [ "${size:-0}" -gt 262144 ]; then
    tail -c 131072 "$file" > "$file.tmp" 2>/dev/null \
      && mv -f "$file.tmp" "$file" 2>/dev/null || rm -f "$file.tmp"
  fi
}
_timing_begin() {
  [ "$TIMING_ENABLED" = 1 ] || return 0
  _TIMING_START="$(_now_us)"
  _TIMING_PREV="$_TIMING_START"
}

scoped_paths_valid() {
  memory_scope_core_valid \
    && [ "$MEMDIR" = "$AUDIENCE_ROOT/$MEMORY_SCOPE/memories" ] \
    && [ "$RESUME_FILE" = "$AUDIENCE_ROOT/$MEMORY_SCOPE/state/resume.md" ]
}

if ! is_disabled "$AUDIENCE_SCOPED"; then
  # A private audience can read pre-scope Wiki material as private legacy and
  # refresh its own route-local cache. Public audiences always fail closed.
  [ "$MEMORY_AUDIENCE" = "private" ] || WIKI_ENABLED=0
  if ! scoped_paths_valid; then
      # Fail closed: an incomplete/malformed scoped environment must never fall
      # back to global MEMORY/USER or cache paths.
      jq -n --arg event "$EVENT" \
        '{hookSpecificOutput:{hookEventName:$event,additionalContext:"Audience-scoped memory unavailable: invalid audience metadata."}}'
      exit 0
  fi
  if ! honcho_scope_valid; then
    HONCHO_ENABLED=0
  fi
fi

scan_injection_block() { # <label> <text>
  local label="$1" text="$2" scanned
  # Run the scanner through bash rather than exec'ing it. Exec'ing depends on
  # its `#!/usr/bin/env bash` resolving, and Termux has no /usr at all, so the
  # exec dies with 126 and the command substitution below fails. The `-x` test
  # still passes, so the fail-open branch takes over and the block is injected
  # UNSCANNED — no credential redaction, no prompt-injection neutralization,
  # and nothing logged (#1157). Fail-open is the right contract for a missing
  # scanner; it must not silently become the default on a whole platform.
  # scan-injection.sh's own suite never caught this because it invokes the
  # scanner as `bash "$SCAN"`, which is the form the callers lacked.
  if [ -x "$HOOKDIR/scan-injection.sh" ] \
    && scanned="$(printf '%s' "$text" | bash "$HOOKDIR/scan-injection.sh" "$label" 2>/dev/null)"; then
    printf '%s' "$scanned"
  else
    printf '%s' "$text"
  fi
}

limit_bytes() { # <max> <text>
  local max="$1"
  python3 "$MEMORY_RENDER_PY" limit-bytes "$max"
}

# Cross-source injection dedup. The local hot-memory search re-surfaces hits from
# MEMORY.md/USER.md (source=memory) and the wiki/honcho caches (source=cache) that
# are ALSO injected verbatim as their own blocks above — double-spending the
# bounded injection budget. Drop such a hit only when its snippet is already fully
# present in the injected text, so anything truncated away from the canonical
# block is still kept (lossless). Structured (distilled-fact) and distill-state
# hits have no other injection path and are always kept.
# Set CCC_MEMORY_INJECT_DEDUP=0/false/off to disable.
dedup_local_hot() { # <injected-text> <search-json>
  if is_disabled "${CCC_MEMORY_INJECT_DEDUP:-1}"; then printf '%s' "$2"; return 0; fi
  # JSON is passed via env, not argv: large blocks would risk ARG_MAX limits.
  INJECTED="$1" SEARCH_JSON="$2" python3 "$MEMORY_RENDER_PY" dedup-local-hot 2>/dev/null || printf '%s' "$2"
}

# Fail closed immediately when Wiki memory is disabled, even before the next
# background index update removes a stale wiki.txt row from SQLite.
filter_disabled_wiki_hits() { # <search-json>
  if ! is_disabled "$WIKI_ENABLED"; then printf '%s' "$1"; return 0; fi
  SEARCH_JSON="$1" python3 "$MEMORY_RENDER_PY" filter-disabled-wiki-hits 2>/dev/null || printf '%s' '{"results":[]}'
}

# Render the (deduped) local hot-memory search JSON as compact, readable lines
# for injection. The raw search JSON carries full filesystem paths, a per-result
# score and an 8-field `signals` object that are debug-only noise to the model
# and waste the bounded injection budget — the agent only needs the snippet and
# which source it came from. The search tool and ccc-memory-explain still emit
# full JSON for diagnostics; this only changes what gets injected.
# Set CCC_MEMORY_INJECT_RENDER=0/false/off to inject the raw JSON instead.
render_local_hot() { # <search-json>
  if is_disabled "${CCC_MEMORY_INJECT_RENDER:-1}"; then printf '%s' "$1"; return 0; fi
  SEARCH_JSON="$1" python3 "$MEMORY_RENDER_PY" render-local-hot 2>/dev/null || printf '%s' "$1"
}

# find_memory_tool comes from lib/hook-common.sh.

run_memory_search_bounded() { # <tool> <query> <limit> <timeout-seconds> [state-dir]
  local tool="$1" query="$2" limit="$3" timeout_sec="$4" state_override="${5:-}"
  python3 "$MEMORY_RENDER_PY" run-memory-search-bounded \
    "$tool" "$query" "$limit" "$timeout_sec" "$state_override" 2>/dev/null || true
}

merge_local_hot() { # <primary-json> <recent-primary-json> [shared-json] [legacy-private-json]
  PRIMARY_JSON="$1" RECENT_JSON="${2:-}" SHARED_JSON="${3:-}" LEGACY_JSON="${4:-}" \
    PRIMARY_AUDIENCE="${MEMORY_AUDIENCE:-private}" \
    python3 "$MEMORY_RENDER_PY" merge-local-hot 2>/dev/null || printf '%s' "$1"
}

build_memory_query() {
  if [ -n "${QUERY:-}" ]; then printf '%s' "$QUERY"; return 0; fi
  local query_tool
  query_tool="$(find_memory_tool ccc-memory-query.sh 2>/dev/null || true)"
  if [ -n "$query_tool" ]; then
    CCC_WORKTREE="${CCC_WORKTREE:-$(pwd 2>/dev/null || true)}" "$query_tool" --mode local 2>/dev/null && return 0
  fi
  cat "$STATE_DIR/current-task.txt" 2>/dev/null || printf 'current task'
}
QUERY="$(build_memory_query)"

age_seconds() { # <file>
  local f="$1" now ts
  [ -f "$f" ] || { printf '%s' '-1'; return; }
  now="$(date -u +%s)"
  ts="$(date -u -r "$f" +%s 2>/dev/null || printf '0')"
  [ "$ts" = "0" ] && printf '%s' '-1' || printf '%s' "$((now - ts))"
}

stale_note() { # <label> <file>
  local label="$1" file="$2" age
  age="$(age_seconds "$file")"
  if [ "$age" -lt 0 ]; then
    printf '%s cache missing' "$label"
  elif [ "$age" -gt "$TTL" ]; then
    printf '%s cache stale (%ss old)' "$label" "$age"
  else
    printf '%s cache fresh (%ss old)' "$label" "$age"
  fi
}

# Built-in node memory lives under ~/.claude/memories; legacy Hermes memory is fallback only.
_timing_begin
# Audience-scoped mode treats every unscoped source as private legacy input.
if ! is_disabled "$AUDIENCE_SCOPED"; then
  scoped_mem="$(cat "$MEMDIR/MEMORY.md" "$MEMDIR/USER.md" 2>/dev/null)"
  shared_mem=""
  [ -n "$SHARED_MEMDIR" ] && shared_mem="$(cat "$SHARED_MEMDIR/MEMORY.md" "$SHARED_MEMDIR/USER.md" 2>/dev/null)"
  if [ "$MEMORY_AUDIENCE" = "private" ]; then
    legacy_claude_mem="$(cat "$LEGACY_MEMDIR/MEMORY.md" "$LEGACY_MEMDIR/USER.md" 2>/dev/null)"
    legacy_hermes_mem="$(cat "$LEGACY_HERMES_MEMDIR/MEMORY.md" "$LEGACY_HERMES_MEMDIR/USER.md" 2>/dev/null)"
    legacy_mem="$(printf '%s\n%s' "$legacy_claude_mem" "$legacy_hermes_mem")"
    mem="$(printf '%s\n%s\n%s' "$legacy_mem" "$shared_mem" "$scoped_mem")"
  else
    mem="$scoped_mem"
  fi
else
  mem="$(cat "$MEMDIR/MEMORY.md" "$MEMDIR/USER.md" 2>/dev/null)"
  [ -z "$mem" ] && mem="$(cat "${HOME:-/root}/.hermes/memories/MEMORY.md" "${HOME:-/root}/.hermes/memories/USER.md" 2>/dev/null)"
fi
wiki=""
if ! is_disabled "$WIKI_ENABLED"; then
  wiki="$(cat "$CACHE/wiki.txt" 2>/dev/null)"
  if ! is_disabled "$AUDIENCE_SCOPED" \
    && [ "$MEMORY_AUDIENCE" = "private" ] \
    && [ -z "$wiki" ]; then
    wiki="$(cat "$LEGACY_CACHE_DIR/wiki.txt" 2>/dev/null)"
  fi
fi
honcho=""
if ! is_disabled "$HONCHO_ENABLED" && [ "$PROFILE" != "max-perf" ]; then
  honcho="$(cat "$CACHE/honcho.txt" 2>/dev/null)"
  if ! is_disabled "$AUDIENCE_SCOPED" \
    && [ "$MEMORY_AUDIENCE" = "private" ] \
    && [ -z "$honcho" ]; then
    honcho="$(cat "$LEGACY_CACHE_DIR/honcho.txt" 2>/dev/null)"
  fi
fi
resume="$(cat "$RESUME_FILE" 2>/dev/null)"
if ! is_disabled "$AUDIENCE_SCOPED" && [ "$MEMORY_AUDIENCE" = "private" ]; then
  legacy_resume="$(cat "$LEGACY_RESUME_FILE" 2>/dev/null)"
  resume="$(printf '%s\n%s' "$legacy_resume" "$resume")"
fi
# Working-state: same resolution as checkpoint.sh (#1155) — the scoped file
# first; an empty scoped file falls back to the node's pre-scope legacy file
# only for the private audience, never for a shared one.
ws=""; ws_file=""
if ! is_disabled "$WS_INJECT" && [ "$EVENT" != "PostCompact" ]; then
  ws_file="$WS_FILE"
  if [ ! -s "$ws_file" ] \
    && ! is_disabled "$AUDIENCE_SCOPED" && [ "$MEMORY_AUDIENCE" = "private" ] \
    && [ -n "$LEGACY_WS_FILE" ] && [ "$LEGACY_WS_FILE" != "$WS_FILE" ] \
    && [ -s "$LEGACY_WS_FILE" ]; then
    ws_file="$LEGACY_WS_FILE"
  fi
  ws="$(cat "$ws_file" 2>/dev/null)"
fi

# Limit the canonical blocks first (static caps) so we can measure their slack
_mark sources
# before sizing the local hot block.
# #897 step 2 leftover (#1040): the four canonical blocks are independent — each
# pays its own scan-injection.sh startup (~43ms) plus a limit_bytes pass and has
# no ordering dependency on the others, so run them concurrently instead of
# serially. scan-injection.sh itself is unchanged; only the call pattern moves.
# Each lane owns one file and only flags `.done` when its whole pipeline
# succeeded, so a lane that dies (spawn failure, OOM, killed subshell) leaves no
# flag and is simply recomputed serially below — the fail-open contract (scanner
# missing/erroring => original text) and the per-block byte caps then hold
# byte-for-byte, and one lane's failure cannot contaminate another's block.
#
# Gated on spare cores. The lanes are CPU-bound (a scanner process plus a Python
# limit_bytes pass each), so the win needs idle cores to land and inverts without
# them. Measured on nosuk/vps2, paired interleaved runs, n=31 each:
#   12 cores (yukson, #1040 report) : 229ms -> 90ms   (-139ms)
#    2 cores                        : 370ms -> 383ms  (median paired delta -3ms,
#                                     mean +1.1ms — indistinguishable from zero)
#    1 core (pinned, Termux-class)  : 543ms -> 605ms  (+75ms — a REGRESSION)
# So parallelize only where there is headroom; small nodes keep the serial path
# they are already fastest on. CCC_MEMORY_SCAN_PARALLEL=1 forces parallel and =0
# forces serial regardless of core count (both for tests and node-local tuning).
CCC_MEMORY_SCAN_MIN_CORES="${CCC_MEMORY_SCAN_MIN_CORES:-4}"
scan_parallel_worthwhile() {
  local cores
  case "$CCC_MEMORY_SCAN_MIN_CORES" in ''|*[!0-9]*) return 1 ;; esac
  cores="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')"
  case "$cores" in ''|*[!0-9]*) return 1 ;; esac
  [ "$cores" -ge "$CCC_MEMORY_SCAN_MIN_CORES" ]
}
scan_dir=""
scan_lane() { # <name> <label> <max> <text>
  { scan_injection_block "$2" "$4" | limit_bytes "$3"; } > "$scan_dir/$1" 2>/dev/null \
    && : > "$scan_dir/$1.done"
}
scanned_block() { # <name> <label> <max> <text>
  if [ -n "$scan_dir" ] && [ -f "$scan_dir/$1.done" ]; then
    cat "$scan_dir/$1" 2>/dev/null
  else
    scan_injection_block "$2" "$4" | limit_bytes "$3"
  fi
}
scan_parallel_pref="${CCC_MEMORY_SCAN_PARALLEL:-auto}"
if [ "$scan_parallel_pref" = auto ]; then
  scan_parallel_worthwhile && scan_parallel_want=1 || scan_parallel_want=0
elif is_disabled "$scan_parallel_pref"; then
  scan_parallel_want=0
else
  scan_parallel_want=1
fi
if [ "$scan_parallel_want" = 1 ]; then
  scan_dir="$(mktemp -d "${TMPDIR:-/tmp}/ccc-mem-scan.XXXXXX" 2>/dev/null || true)"
fi
if [ -n "$scan_dir" ]; then
  scan_lane mem built-in-memory "$MAX_MEM" "$mem" &
  scan_lane resume resume-pointer "$MAX_RESUME" "$resume" &
  if [ -n "$ws" ]; then
    scan_lane ws working-state-checkpoint "$MAX_WS" "$ws" &
  fi
  if ! is_disabled "$WIKI_ENABLED"; then
    scan_lane wiki family-wiki-cache "$MAX_WIKI" "$wiki" &
  fi
  scan_lane honcho honcho-cache "$MAX_HONCHO" "$honcho" &
  wait
  _mark scan_parallel
fi
mem="$(scanned_block mem built-in-memory "$MAX_MEM" "$mem")"
resume="$(scanned_block resume resume-pointer "$MAX_RESUME" "$resume")"
# Agent-written and re-entering model context: scanned like every other block
# (same label checkpoint.sh uses for its PostCompact re-injection, #1045).
if [ -n "$ws" ]; then
  ws="$(scanned_block ws working-state-checkpoint "$MAX_WS" "$ws")"
fi
if ! is_disabled "$WIKI_ENABLED"; then
  wiki="$(scanned_block wiki family-wiki-cache "$MAX_WIKI" "$wiki")"
fi
honcho="$(scanned_block honcho honcho-cache "$MAX_HONCHO" "$honcho")"
if [ -n "$scan_dir" ]; then rm -rf "$scan_dir"; fi

# Relevance-aware budget. The per-block caps sum to more than CCC_MEMORY_MAX_BYTES,
# so today the tail (Honcho) is simply truncated and any budget a small/empty block
# leaves unused (no wiki/honcho cache, or max-perf which drops Honcho) is wasted —
# while the local hot block is also under-filled because the search returns only
# CCC_MEMORY_SEARCH_LIMIT (5) results regardless. Reclaim that slack for the local
# hot block — the task-conditioned, most query-relevant source — by growing BOTH
# its byte budget AND how many results we fetch to fill it. Purely additive: never
# below MAX_LOCAL / the default limit (worst case == today); the final MAX_TOTAL
# cap still bounds the whole injection. Disable with CCC_MEMORY_DYNAMIC_BUDGET=0.
alloc_local="$MAX_LOCAL"
search_limit="${CCC_MEMORY_SEARCH_LIMIT:-}"
if ! is_disabled "${CCC_MEMORY_DYNAMIC_BUDGET:-1}"; then
  msize="$(printf '%s' "$mem" | wc -c)"
  wsize="$(printf '%s' "$wiki" | wc -c)"
  hsize="$(printf '%s' "$honcho" | wc -c)"
  # The working-state block is a second pointer-class block next to resume;
  # count it there so the local hot block cannot reclaim bytes it occupies.
  rsize="$(( $(printf '%s' "$resume" | wc -c) + $(printf '%s' "$ws" | wc -c) ))"
  # alloc = byte budget for local (>= MAX_LOCAL, reclaiming slack up to the total
  # minus a ~1000B scaffold reserve); dyn_limit = results to fetch to fill it
  # (~180B/result, clamped to [5,25]). The final limit_bytes is the hard bound.
  budget_out="$(python3 "$MEMORY_RENDER_PY" dynamic-budget \
    "$MAX_TOTAL" 1000 "$MAX_LOCAL" 180 5 25 "$msize" "$rsize" "$wsize" "$hsize" 2>/dev/null || true)"
  alloc_candidate="${budget_out%% *}"
  limit_candidate="${budget_out##* }"
  case "$alloc_candidate" in ''|*[!0-9]*) ;; *) alloc_local="$alloc_candidate" ;; esac
  if [ -z "$search_limit" ]; then
    case "$limit_candidate" in ''|*[!0-9]*) ;; *) search_limit="$limit_candidate" ;; esac
  fi
fi

local_hot=""
_mark dynamic_budget
recent_hot=""
shared_hot=""
legacy_hot=""
if [ "$PROFILE" = "hybrid" ] || [ "$PROFILE" = "max-perf" ] || ! is_disabled "$LOCAL_ENABLED"; then
  search_tool="$(find_memory_tool ccc-memory-search.sh 2>/dev/null || true)"
  if [ -n "$search_tool" ]; then
    # No line-cap here: dedup/render parse the whole JSON (a partial cut would
    # break json.loads and fall back to raw). Result count is bounded by
    # search_limit and the byte budget is enforced by limit_bytes below.
    # SessionStart is read-only and must finish before the outer 15-second hook
    # deadline. A short inner deadline drops only local-hot results; canonical
    # MEMORY/USER/cache/resume blocks assembled above still inject. The helper
    # uses Python rather than GNU timeout so the same contract works on Termux.
    # #897 step 2: in audience mode the local/recent/shared/legacy lanes are
    # independent DB queries — run them concurrently under ONE global budget
    # (default 3s) instead of the serial 3+1+3+2s chain. Each lane keeps its
    # own inner timeout so a killed wait never orphans an unbounded search;
    # CCC_MEMORY_SEARCH_PARALLEL=0 restores the serial path. Needs EPOCHREALTIME
    # (bash 5) for the deadline; without it the serial path is used.
    search_dir=""
    if ! is_disabled "$AUDIENCE_SCOPED" \
      && ! is_disabled "${CCC_MEMORY_SEARCH_PARALLEL:-1}" \
      && [ -n "${EPOCHREALTIME:-}" ]; then
      search_dir="$(mktemp -d "${TMPDIR:-/tmp}/ccc-mem-search.XXXXXX" 2>/dev/null || true)"
    fi
    if ! is_disabled "$AUDIENCE_SCOPED" && [ -n "$search_dir" ]; then
      ( run_memory_search_bounded "$search_tool" "$QUERY" "$search_limit" "${CCC_MEMORY_SEARCH_TIMEOUT_SEC:-3}" "$STATE_DIR" > "$search_dir/local" 2>/dev/null ) &
      ( run_memory_search_bounded "$search_tool" "distilled text" "$search_limit" "${CCC_MEMORY_RECENT_SEARCH_TIMEOUT_SEC:-1}" "$STATE_DIR" > "$search_dir/recent" 2>/dev/null ) &
      if [ "$MEMORY_AUDIENCE" = "private" ] \
        && [ -n "$SHARED_STATE_DIR" ] \
        && [ "$SHARED_STATE_DIR" != "$STATE_DIR" ]; then
        ( run_memory_search_bounded "$search_tool" "$QUERY" "$search_limit" "${CCC_MEMORY_SEARCH_TIMEOUT_SEC:-3}" "$SHARED_STATE_DIR" > "$search_dir/shared" 2>/dev/null ) &
        if [ -n "$LEGACY_STATE_DIR" ] \
          && [ "$LEGACY_STATE_DIR" != "$STATE_DIR" ] \
          && [ "$LEGACY_STATE_DIR" != "$SHARED_STATE_DIR" ]; then
          ( run_memory_search_bounded "$search_tool" "$QUERY" "$search_limit" "${CCC_MEMORY_LEGACY_SEARCH_TIMEOUT_SEC:-2}" "$LEGACY_STATE_DIR" > "$search_dir/legacy" 2>/dev/null ) &
        fi
      fi
      search_budget="${CCC_MEMORY_SEARCH_GLOBAL_TIMEOUT_SEC:-3}"
      case "$search_budget" in ''|*[!0-9]*) search_budget=3 ;; esac
      deadline_us=$(( $(_now_us) + search_budget * 1000000 ))
      while :; do
        alive=0
        for search_pid in $(jobs -p); do
          kill -0 "$search_pid" 2>/dev/null && alive=1
        done
        [ "$alive" = 0 ] && break
        [ "$(_now_us)" -ge "$deadline_us" ] && break
        sleep 0.05
      done
      for search_pid in $(jobs -p); do
        kill "$search_pid" 2>/dev/null
      done
      wait 2>/dev/null
      local_hot="$(cat "$search_dir/local" 2>/dev/null)"
      recent_hot="$(cat "$search_dir/recent" 2>/dev/null)"
      shared_hot="$(cat "$search_dir/shared" 2>/dev/null)"
      legacy_hot="$(cat "$search_dir/legacy" 2>/dev/null)"
      rm -rf "$search_dir"
      _mark search_parallel
    else
      local_hot="$(run_memory_search_bounded "$search_tool" "$QUERY" "$search_limit" "${CCC_MEMORY_SEARCH_TIMEOUT_SEC:-3}" "$STATE_DIR")"
      _mark search_local
    fi
    if ! is_disabled "$AUDIENCE_SCOPED"; then
      if [ -z "$search_dir" ]; then
        # A just-committed Codex fact may not match the checkout-derived startup
        # query yet. The write-back indexer tags these rows `distilled`; merge one
        # small recent-fact lane so the immediately following isolated thread sees
        # the durable fact without waiting for another turn or background refresh.
        recent_hot="$(run_memory_search_bounded "$search_tool" "distilled text" "$search_limit" "${CCC_MEMORY_RECENT_SEARCH_TIMEOUT_SEC:-1}" "$STATE_DIR")"
        _mark search_recent
        if [ "$MEMORY_AUDIENCE" = "private" ] \
          && [ -n "$SHARED_STATE_DIR" ] \
          && [ "$SHARED_STATE_DIR" != "$STATE_DIR" ]; then
          shared_hot="$(run_memory_search_bounded "$search_tool" "$QUERY" "$search_limit" "${CCC_MEMORY_SEARCH_TIMEOUT_SEC:-3}" "$SHARED_STATE_DIR")"
          _mark search_shared
          if [ -n "$LEGACY_STATE_DIR" ] \
            && [ "$LEGACY_STATE_DIR" != "$STATE_DIR" ] \
            && [ "$LEGACY_STATE_DIR" != "$SHARED_STATE_DIR" ]; then
            legacy_hot="$(run_memory_search_bounded "$search_tool" "$QUERY" "$search_limit" "${CCC_MEMORY_LEGACY_SEARCH_TIMEOUT_SEC:-2}" "$LEGACY_STATE_DIR")"
            _mark search_legacy
          fi
        fi
      fi
      local_hot="$(merge_local_hot "$local_hot" "$recent_hot" "$shared_hot" "$legacy_hot")"
      _mark merge
    fi
  fi
fi

local_hot="$(filter_disabled_wiki_hits "$local_hot")"
_mark filter_wiki

# Dedup the local hot block against what we ACTUALLY inject above (post-redaction,
# post-truncation) before rendering it — so it surfaces index-only content
# (distilled facts) instead of echoing the canonical blocks.
local_hot="$(dedup_local_hot "$mem
$wiki
$honcho" "$local_hot")"
_mark dedup
# Render the search JSON to compact readable lines, then apply the (possibly
# enlarged) local byte budget.
local_hot="$(render_local_hot "$local_hot")"
local_hot="$(scan_injection_block local-hot-memory "$local_hot" | limit_bytes "$alloc_local")"
_mark render

node_label="${CCC_NODE:-$(cat "$STATE_DIR/node.txt" 2>/dev/null || hostname -s 2>/dev/null || printf 'ccc-node')}"
stamp="$(cat "$CACHE/.last-refresh" 2>/dev/null)"
wiki_note="Family Wiki disabled"
if ! is_disabled "$WIKI_ENABLED"; then
  wiki_note="$(stale_note 'Family Wiki' "$CACHE/wiki.txt")"
fi
honcho_note="Honcho disabled"
if ! is_disabled "$HONCHO_ENABLED" && [ "$PROFILE" != "max-perf" ]; then
  honcho_note="$(stale_note 'Honcho' "$CACHE/honcho.txt")"
fi

# Gate-3 transition (#824 Phase 1): on nunchi-enabled nodes the nunchi
# snapshot hook is the primary working memory; label Honcho secondary so the
# model weighs sources accordingly. Honcho stays injected for verification
# until Phase 3 (freeze/retire).
honcho_role=""
nunchi_mode="${CCC_NUNCHI_MODE:-$(cat "$STATE_DIR/nunchi.mode" 2>/dev/null || printf 'off')}"
if [ "$nunchi_mode" = "on" ]; then
  honcho_role=" (secondary — nunchi snapshot is primary during the gate-3 transition)"
fi

resume_block=""
if [ -n "${resume:-}" ]; then
  resume_block="▶ 직전 세션에서 이어서:
${resume}
"
fi

# Working-state block (#1176). Placed right after MEMORY+USER so the byte caps
# downstream (the materializer truncates from the tail) cut Honcho/Wiki before
# the live task pointer. Stale guard mirrors checkpoint.sh: CCC_CKPT_STALE_DAYS
# (default 14, 0 disables) — a weeks-old objective must not read as current.
ws_block=""
if ! is_disabled "$WS_INJECT" && [ "$EVENT" != "PostCompact" ]; then
  ws_stale=""
  ws_stale_days="${CCC_CKPT_STALE_DAYS:-14}"
  case "$ws_stale_days" in ''|*[!0-9]*) ws_stale_days=14 ;; esac
  if [ -n "$ws" ] && [ "$ws_stale_days" -gt 0 ]; then
    ws_age="$(age_seconds "$ws_file")"
    if [ "$ws_age" -ge $(( ws_stale_days * 86400 )) ]; then
      ws_stale="⚠ STALE: working-state.md was last modified $(( ws_age / 86400 )) days ago — it may describe an already-finished task. Verify against live state before acting on it, and clear it to an idle note when its task closes.
"
    fi
  fi
  ws_block="
## Working-state checkpoint (agent-written — objective / progress / next step; re-injected at session start and after compaction)
${ws_stale}${ws:-(working-state.md empty — if a task is in progress, keep ${WS_FILE} updated as objective / progress / next step)}
"
fi

operational_note="Operational facts are mutable — live-check the node before asserting or changing anything."
audience_note=""
if ! is_disabled "$AUDIENCE_SCOPED"; then
  if [ "$MEMORY_AUDIENCE" = "private" ]; then
    audience_note="Memory audience: private DM plus explicitly shared public facts. Unscoped legacy memory is private-only."
  else
    audience_note="Memory audience: shared public facts only. DM-private and unscoped legacy memory are unavailable."
  fi
fi
skills_block=""
if ! is_disabled "$SKILLS_ENABLED" && [ -d "$SKILLS_DIR" ]; then
  skills_index="$(
    for skill_md in "$SKILLS_DIR"/*/SKILL.md; do
      [ -r "$skill_md" ] || continue
      awk 'NR==1 && $0!="---" {exit}
        /^---$/ {fm++; next}
        fm==1 && /^name:[ ]*/ {sub(/^name:[ ]*/,""); name=$0}
        fm==1 && /^description:[ ]*/ {sub(/^description:[ ]*/,""); desc=substr($0,1,160)}
        END {if (name!="") printf "- %s — %s\n", name, desc}' "$skill_md"
    done | sort
  )"
  skills_note="read the SKILL.md before use; search these descriptions, not filenames"
  if [ -n "$skills_index" ] && [ "$(printf '%s' "$skills_index" | wc -c)" -gt "$MAX_SKILLS" ]; then
    # No silent tail-drop (#1081 lesson): degrade the WHOLE index to names
    # rather than truncating away the skills that sort last.
    skills_note="names only — descriptions exceed the byte budget; read each SKILL.md"
    skills_index="$(
      for skill_md in "$SKILLS_DIR"/*/SKILL.md; do
        [ -r "$skill_md" ] || continue
        awk 'NR==1 && $0!="---" {exit}
          /^---$/ {fm++; next}
          fm==1 && /^name:[ ]*/ {sub(/^name:[ ]*/,""); print "- " $0; exit}' "$skill_md"
      done | sort
    )"
  fi
  if [ -n "$skills_index" ]; then
    skills_block="
## Node skills index (${skills_note})
$(printf '%s' "$skills_index" | limit_bytes "$MAX_SKILLS")
"
  fi
fi
wiki_block=""
if ! is_disabled "$WIKI_ENABLED"; then
  operational_note="Operational facts are mutable — live-check the node and verify Wiki source text before asserting or changing anything."
  wiki_block="
## Family Wiki (cache prefetch — candidates; verify with wiki-agent load before operational claims)
${wiki:-(no wiki cache yet — will populate after first background refresh)}
"
fi

ctx="# ${node_label} session memory (auto-injected: $EVENT)

${resume_block}${operational_note}
${audience_note}
Memory profile: ${PROFILE}; last refresh: ${stamp:-never}; ${wiki_note}; ${honcho_note}. A background refresh runs each session for the next one.

## Built-in MEMORY + USER
${mem:-(memory files unavailable)}
${ws_block}
## Local hot memory (task-conditioned cache search)
${local_hot:-(local hot memory disabled or no hits)}
${skills_block}${wiki_block}
## Honcho working memory — ${USER_LABEL}${honcho_role}
${honcho:-(Honcho disabled or no Honcho cache yet)}"

ctx="$(printf '%s' "$ctx" | limit_bytes "$MAX_TOTAL")"

jq -n --arg ctx "$ctx" --arg event "$EVENT" \
  '{hookSpecificOutput:{hookEventName:$event,additionalContext:$ctx}}'
_timing_flush

# Fire-and-forget: refresh caches for the NEXT session, fully detached so startup never waits.
# CCC_MEMORY_NO_REFRESH=1 suppresses it — for hermetic tests (the detached refresh
# rebuilds the index / consolidates facts out-of-band, which otherwise mutates
# shared state mid-test) and for any caller that wants a strictly read-only inject.
run_refresh_memory_bg() { bash "$HOOKDIR/refresh-memory.sh"; }
LOAD_MEMORY_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
SPAWN_DETACHED_LIB="${CCC_SPAWN_DETACHED_LIB:-$HOOKDIR/lib/spawn-detached.sh}"
if [ ! -r "$SPAWN_DETACHED_LIB" ] && [ -n "$LOAD_MEMORY_SELF_DIR" ]; then
  SPAWN_DETACHED_LIB="$LOAD_MEMORY_SELF_DIR/lib/spawn-detached.sh"
fi
case "${CCC_MEMORY_NO_REFRESH:-0}" in
  1|true|TRUE|on|ON|yes|YES) : ;;
  *)
    if [ -r "$SPAWN_DETACHED_LIB" ]; then
      # shellcheck source=claude/hooks/lib/spawn-detached.sh
      . "$SPAWN_DETACHED_LIB"
      spawn_detached "$HOOKDIR/refresh-memory.sh" "" run_refresh_memory_bg || true
    fi
    ;;
esac
