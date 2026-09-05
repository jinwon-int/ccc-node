#!/usr/bin/env bash
# SessionStart memory bootstrap for a Claude Code node (node-owned memory).
# Serves built-in MEMORY/USER + bounded cached Family Wiki/local hot memory instantly,
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
PROFILE="${CCC_MEMORY_PROFILE:-standard}"
TTL="${CCC_MEMORY_CACHE_TTL_SEC:-21600}"
MAX_TOTAL="${CCC_MEMORY_MAX_BYTES:-12000}"
MAX_MEM="${CCC_BUILTIN_MEMORY_MAX_BYTES:-4000}"
MAX_WIKI="${CCC_WIKI_MAX_BYTES:-5000}"
MAX_LOCAL="${CCC_LOCAL_MEMORY_MAX_BYTES:-3000}"
# Skill index (#1145): node skills are plain files the session cannot see, so
# name-keyword searches miss them (gh-pr-flow sat undiscovered through three
# round-trips while its description held the exact answer). Inject a bounded
# name+description index so discovery starts from descriptions, not filenames.
MAX_SKILLS="${CCC_SKILL_INDEX_MAX_BYTES:-1500}"
SKILLS_ENABLED="${CCC_SKILL_INDEX_ENABLED:-1}"
SKILLS_DIR="${CCC_SKILLS_DIR:-${HOME:-/root}/.claude/skills}"
MAX_RESUME="${CCC_RESUME_MAX_BYTES:-2000}"
WIKI_ENABLED="${CCC_WIKI_MEMORY_ENABLED:-1}"
ISOLATION_PROFILE="${CCC_NODE_ISOLATION_PROFILE:-fleet}"
[ "$ISOLATION_PROFILE" = "external" ] && WIKI_ENABLED=0
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
# Still-owed external-wait promises (#1258). Unlike the working-state block this
# defaults ON, because it is silent unless the durable registry actually holds
# an outstanding promise: with nothing owed the renderer prints nothing and this
# loader's output stays byte-identical. The registry survives restarts already
# (#740) — what was missing is any path that tells the *next* session about it,
# which is exactly what the 4h auto-new-session rotation and self-update
# restarts were eating.
PROMISES_INJECT="${CCC_MEMORY_INJECT_PENDING_PROMISES:-1}"
MAX_PROMISES="${CCC_PENDING_PROMISES_MAX_BYTES:-1024}"
EXTERNAL_WAIT_HOME="${CCC_EXTERNAL_WAIT_HOME:-${HOME:-/root}/.telegram_bot/external-wait}"
# Detached-job completion evidence (#1258, second half). Same default-ON,
# silent-when-nothing-outstanding contract as the promises block above, and for
# the same reason it cannot be a live watcher: `bridge-safe-detached-run`
# detaches the work but its Step 2 poll loop is still a session child, so a
# restart kills the watcher while the job keeps running. Re-reading the log's
# EXIT marker at SessionStart needs no surviving process at all.
DETACHED_JOBS_INJECT="${CCC_MEMORY_INJECT_DETACHED_JOBS:-1}"
MAX_DETACHED_JOBS="${CCC_DETACHED_JOBS_MAX_BYTES:-1024}"
DETACHED_JOBS_REGISTRY="${CCC_DETACHED_JOBS_REGISTRY:-$STATE_DIR/detached-jobs.jsonl}"
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
# Still-owed external-wait promises renderer (#1258); same stdlib-only, fail-open
# contract as memory_render.py above.
PENDING_PROMISES_PY="$LOAD_MEMORY_LIB_DIR/lib/pending_promises.py"
# Detached-job completion sweep (#1258); same stdlib-only, fail-open contract.
DETACHED_JOBS_PY="$LOAD_MEMORY_LIB_DIR/lib/detached_jobs.py"

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
_mark_split() { # <stage> <ms> — a stage timed by a child process (the render
  # pipeline reports its search lanes); advance the previous mark past it so the
  # enclosing bash-timed stage excludes those milliseconds.
  [ "$TIMING_ENABLED" = 1 ] || return 0
  case "$2" in ''|*[!0-9]*) return 0 ;; esac
  [ -n "$_timing_marks" ] && _timing_marks+=" "
  _timing_marks+="$1=$2"
  _TIMING_PREV=$(( _TIMING_PREV + $2 * 1000 ))
}
_timing_begin() {
  [ "$TIMING_ENABLED" = 1 ] || return 0
  _TIMING_START="$(_now_us)"
  _TIMING_PREV="$_TIMING_START"
}

# Scratch directories (parallel scan lanes, render-pipeline output) are removed
# where they are consumed; the trap covers the paths that never get there
# (killed by the hook deadline, a failing `set -u` expansion, ...). Background
# lanes are async subshells, which reset traps, so they cannot fire this early.
scan_dir=""
pipe_dir=""
_cleanup_scratch() {
  [ -n "$scan_dir" ] && rm -rf "$scan_dir" 2>/dev/null
  [ -n "$pipe_dir" ] && rm -rf "$pipe_dir" 2>/dev/null
  return 0
}
trap _cleanup_scratch EXIT

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
  if [ ! -x "$HOOKDIR/scan-injection.sh" ]; then
    printf '%s' "$text"
    return
  fi
  if scanned="$(printf '%s' "$text" | bash "$HOOKDIR/scan-injection.sh" "$label" 2>/dev/null)"; then
    printf '%s' "$scanned"
  else
    # #1160: the scanner EXISTS but its invocation failed — the block is about
    # to be injected UNSCANNED on a node that believes itself protected. Note
    # it on stderr (hook stderr lands in session/hook logs) instead of letting
    # a whole platform run unprotected silently for months (#1157 shipped
    # exactly this; memory_render.py got the same note in #1169). The label
    # only — never the block body. Fail-open itself is unchanged.
    printf 'load-memory: scan-injection failed (label=%s); injecting UNSCANNED block\n' "$label" >&2
    printf '%s' "$text"
  fi
}

limit_bytes() { # <max> <text>
  local max="$1"
  python3 "$MEMORY_RENDER_PY" limit-bytes "$max"
}

byte_len() { # <text> -> byte count, no fork (length is locale-dependent, so pin C)
  local LC_ALL=C
  printf '%s' "${#1}"
}

# Combined scan + byte-cap: one scanner process applies the cap in-process
# (scan-injection.sh's optional second arg) instead of piping through a second
# limit_bytes interpreter per block. Both fail-open paths still cap the bytes.
# Empty blocks (fresh node, disabled lanes) skip both processes outright —
# the old pipeline paid two interpreter starts to transform "" into "".
scan_capped_block() { # <label> <max> <text>
  local label="$1" max="$2" text="$3" scanned
  [ -n "$text" ] || return 0
  if [ ! -x "$HOOKDIR/scan-injection.sh" ]; then
    printf '%s' "$text" | limit_bytes "$max"
    return
  fi
  if scanned="$(printf '%s' "$text" | bash "$HOOKDIR/scan-injection.sh" "$label" "$max" 2>/dev/null)"; then
    printf '%s' "$scanned"
  else
    # Same UNSCANNED contract as scan_injection_block (#1160): note it, keep
    # fail-open, still enforce the byte cap on the unscanned text.
    printf 'load-memory: scan-injection failed (label=%s); injecting UNSCANNED block\n' "$label" >&2
    printf '%s' "$text" | limit_bytes "$max"
  fi
}

# The local hot-memory chain — dynamic budget, bounded search lane(s), audience
# merge, disabled-wiki filter, cross-source dedup, compact render — plus the
# pending-promises and detached-jobs evidence blocks all run inside ONE
# `memory_render.py pipeline` interpreter (#1484). They used to be one python3
# start each (5-7 on the critical path). The knobs are unchanged:
#   CCC_MEMORY_INJECT_DEDUP=0   inject search hits even when the snippet is
#                               already fully present in MEMORY/USER/wiki
#   CCC_MEMORY_INJECT_RENDER=0  inject the raw search JSON instead of lines
#   CCC_MEMORY_DYNAMIC_BUDGET=0 keep the static local byte cap / result limit
# scan-injection.sh stays a separate spawn after the pipeline: it is the
# security boundary for every injected block and is not importable by design.
# Fail-open is preserved stage by stage inside the pipeline; if the pipeline
# itself cannot run (module missing, scratch dir unavailable) the local hot
# block is empty and the evidence blocks fall back to their own modules —
# exactly what a missing memory_render.py produced before.

# find_memory_tool comes from lib/hook-common.sh.

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
scan_lane() { # <name> <label> <max> <text>
  scan_capped_block "$2" "$3" "$4" > "$scan_dir/$1" 2>/dev/null \
    && : > "$scan_dir/$1.done"
}
scanned_block() { # <name> <label> <max> <text>
  if [ -n "$scan_dir" ] && [ -f "$scan_dir/$1.done" ]; then
    cat "$scan_dir/$1" 2>/dev/null
  else
    scan_capped_block "$2" "$3" "$4"
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
if [ -n "$scan_dir" ]; then rm -rf "$scan_dir"; scan_dir=""; fi

# Relevance-aware budget. The per-block caps sum to more than CCC_MEMORY_MAX_BYTES,
# so today the tail (wiki) is simply truncated and any budget a small/empty block
# leaves unused (no wiki cache) is wasted —
# while the local hot block is also under-filled because the search returns only
# CCC_MEMORY_SEARCH_LIMIT (5) results regardless. Reclaim that slack for the local
# hot block — the task-conditioned, most query-relevant source — by growing BOTH
# its byte budget AND how many results we fetch to fill it. Purely additive: never
# below MAX_LOCAL / the default limit (worst case == today); the final MAX_TOTAL
# cap still bounds the whole injection. Disable with CCC_MEMORY_DYNAMIC_BUDGET=0.
# The arithmetic itself runs inside the render pipeline below; only the block
# sizes are measured here (no fork).
alloc_local="$MAX_LOCAL"
search_limit="${CCC_MEMORY_SEARCH_LIMIT:-}"
budget_spec=""
if ! is_disabled "${CCC_MEMORY_DYNAMIC_BUDGET:-1}"; then
  msize="$(byte_len "$mem")"
  wsize="$(byte_len "$wiki")"
  # The working-state block is a second pointer-class block next to resume;
  # count it there so the local hot block cannot reclaim bytes it occupies.
  rsize="$(( $(byte_len "$resume") + $(byte_len "$ws") ))"
  # alloc = byte budget for local (>= MAX_LOCAL, reclaiming slack up to the total
  # minus a ~1000B scaffold reserve); dyn_limit = results to fetch to fill it
  # (~180B/result, clamped to [5,25]). The final limit_bytes is the hard bound.
  budget_spec="$MAX_TOTAL,1000,$MAX_LOCAL,180,5,25,$msize,$rsize,$wsize"
fi
_mark dynamic_budget

# Search lane(s). No line-cap on the tool output: dedup/render parse the whole
# JSON (a partial cut would break json.loads and fall back to raw). Result
# count is bounded by search_limit and the byte budget by the scan cap below.
# SessionStart is read-only and must finish before the outer 15-second hook
# deadline. A short inner deadline drops only local-hot results; canonical
# MEMORY/USER/cache/resume blocks assembled above still inject. The runner uses
# Python rather than GNU timeout so the same contract works on Termux.
# #897 step 2: in audience mode the local/recent/shared/legacy lanes are
# independent DB queries — the pipeline runs them concurrently under ONE global
# budget (default 3s) instead of the serial 3+1+3+2s chain, each lane keeping
# its own inner timeout; CCC_MEMORY_SEARCH_PARALLEL=0 restores the serial path.
search_tool=""
if [ "$PROFILE" = "hybrid" ] || [ "$PROFILE" = "max-perf" ] || ! is_disabled "$LOCAL_ENABLED"; then
  search_tool="$(find_memory_tool ccc-memory-search.sh 2>/dev/null || true)"
fi
pipe_audience=0; pipe_parallel=0; pipe_shared_state=""; pipe_legacy_state=""
if ! is_disabled "$AUDIENCE_SCOPED"; then
  pipe_audience=1
  is_disabled "${CCC_MEMORY_SEARCH_PARALLEL:-1}" || pipe_parallel=1
  if [ "$MEMORY_AUDIENCE" = "private" ] \
    && [ -n "$SHARED_STATE_DIR" ] \
    && [ "$SHARED_STATE_DIR" != "$STATE_DIR" ]; then
    pipe_shared_state="$SHARED_STATE_DIR"
    if [ -n "$LEGACY_STATE_DIR" ] \
      && [ "$LEGACY_STATE_DIR" != "$STATE_DIR" ] \
      && [ "$LEGACY_STATE_DIR" != "$SHARED_STATE_DIR" ]; then
      pipe_legacy_state="$LEGACY_STATE_DIR"
    fi
  fi
fi
pipe_wiki=1; is_disabled "$WIKI_ENABLED" && pipe_wiki=0
pipe_dedup=1; is_disabled "${CCC_MEMORY_INJECT_DEDUP:-1}" && pipe_dedup=0
pipe_render=1; is_disabled "${CCC_MEMORY_INJECT_RENDER:-1}" && pipe_render=0
pipe_promises=""
if ! is_disabled "$PROMISES_INJECT" && [ -r "$PENDING_PROMISES_PY" ]; then
  pipe_promises="$EXTERNAL_WAIT_HOME/waits.json"
fi
pipe_detached=""
if ! is_disabled "$DETACHED_JOBS_INJECT" && [ -r "$DETACHED_JOBS_PY" ]; then
  pipe_detached="$DETACHED_JOBS_REGISTRY"
fi
search_budget="${CCC_MEMORY_SEARCH_GLOBAL_TIMEOUT_SEC:-3}"
case "$search_budget" in ''|*[!0-9]*) search_budget=3 ;; esac

local_hot=""
promises=""
detached=""
pipe_ok=0
if [ -r "$MEMORY_RENDER_PY" ]; then
  pipe_dir="$(mktemp -d "${TMPDIR:-/tmp}/ccc-mem-pipe.XXXXXX" 2>/dev/null || true)"
fi
if [ -n "$pipe_dir" ]; then
  # INJECTED (dedup reference: what is ACTUALLY injected above, post-redaction,
  # post-truncation) rides on env, not argv — large blocks would risk ARG_MAX.
  if pipe_meta="$(INJECTED="$mem
$wiki" python3 "$MEMORY_RENDER_PY" pipeline out="$pipe_dir" \
      budget="$budget_spec" alloc="$MAX_LOCAL" limit="$search_limit" \
      tool="$search_tool" query="$QUERY" state_dir="$STATE_DIR" \
      timeout="${CCC_MEMORY_SEARCH_TIMEOUT_SEC:-3}" \
      audience_scoped="$pipe_audience" audience="${MEMORY_AUDIENCE:-private}" \
      parallel="$pipe_parallel" global_timeout="$search_budget" \
      recent_timeout="${CCC_MEMORY_RECENT_SEARCH_TIMEOUT_SEC:-1}" \
      shared_state_dir="$pipe_shared_state" shared_timeout="${CCC_MEMORY_SEARCH_TIMEOUT_SEC:-3}" \
      legacy_state_dir="$pipe_legacy_state" legacy_timeout="${CCC_MEMORY_LEGACY_SEARCH_TIMEOUT_SEC:-2}" \
      wiki_enabled="$pipe_wiki" dedup="$pipe_dedup" render="$pipe_render" \
      promises_file="$pipe_promises" promises_max="$MAX_PROMISES" \
      detached_registry="$pipe_detached" detached_max="$MAX_DETACHED_JOBS" 2>/dev/null)"; then
    pipe_ok=1
    for pipe_kv in $pipe_meta; do
      case "$pipe_kv" in
        alloc=*)
          case "${pipe_kv#alloc=}" in ''|*[!0-9]*) ;; *) alloc_local="${pipe_kv#alloc=}" ;; esac ;;
        limit=*) ;;
        *=*) _mark_split "${pipe_kv%%=*}" "${pipe_kv#*=}" ;;
      esac
    done
    [ -f "$pipe_dir/local_hot" ] && local_hot="$(<"$pipe_dir/local_hot")"
    [ -f "$pipe_dir/promises" ] && promises="$(<"$pipe_dir/promises")"
    [ -f "$pipe_dir/detached" ] && detached="$(<"$pipe_dir/detached")"
  fi
  rm -rf "$pipe_dir"; pipe_dir=""
fi
_mark pipeline

if [ -n "$local_hot" ]; then
  # Apply the (possibly enlarged) local byte budget: scan + cap in one process.
  local_hot="$(scan_capped_block local-hot-memory "$alloc_local" "$local_hot")"
  _mark render
fi

node_label="${CCC_NODE:-$(cat "$STATE_DIR/node.txt" 2>/dev/null || hostname -s 2>/dev/null || printf 'ccc-node')}"
stamp="$(cat "$CACHE/.last-refresh" 2>/dev/null)"
wiki_note="Family Wiki disabled"
if ! is_disabled "$WIKI_ENABLED"; then
  wiki_note="$(stale_note 'Family Wiki' "$CACHE/wiki.txt")"
fi

resume_block=""
if [ -n "${resume:-}" ]; then
  resume_block="▶ 직전 세션에서 이어서:
${resume}
"
fi

# Working-state block (#1176). Placed right after MEMORY+USER so the byte caps
# downstream (the materializer truncates from the tail) cut the wiki block before
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

# Still-owed promises block (#1258). Sits next to the working-state pointer for
# the same reason: downstream byte caps truncate from the tail, and an
# outstanding promise is live-task context, not background reading. Fail-open —
# a missing/broken helper yields an empty block, never a failed hook.
promises_block=""
if ! is_disabled "$PROMISES_INJECT"; then
  # Rendered inside the pipeline above; only a pipeline that could not run
  # falls back to the module's own interpreter.
  if [ "$pipe_ok" != 1 ] && [ -r "$PENDING_PROMISES_PY" ]; then
    promises="$(python3 "$PENDING_PROMISES_PY" \
      "$EXTERNAL_WAIT_HOME/waits.json" --max-bytes "$MAX_PROMISES" 2>/dev/null)" || promises=""
  fi
  if [ -n "$promises" ]; then
    promises_block="
## ⚠ 미완 약속 (durable external-wait 레지스트리 — 세션이 바뀌어도 살아 있음)
${promises}
"
  fi
fi

# Detached-job completion block (#1258). Sits with the promises block for the
# same tail-truncation reason. This is the half the earlier slice left open: a
# systemd-run job outlives the session, but the watcher that was supposed to
# notice it finishing does not, so the only durable evidence is the log's EXIT
# marker and nothing was re-reading it. Fail-open — a missing/broken helper
# yields an empty block, never a failed hook.
detached_block=""
if ! is_disabled "$DETACHED_JOBS_INJECT"; then
  # Same pipeline-first / module fallback as the promises block.
  if [ "$pipe_ok" != 1 ] && [ -r "$DETACHED_JOBS_PY" ]; then
    detached="$(python3 "$DETACHED_JOBS_PY" sweep \
      "$DETACHED_JOBS_REGISTRY" --max-bytes "$MAX_DETACHED_JOBS" 2>/dev/null)" || detached=""
  fi
  if [ -n "$detached" ]; then
    detached_block="
## 🧱 detached 작업 상태 (로그 EXIT 마커 기준 — 감시자 생존과 무관)
${detached}
"
  fi
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
  # One awk pass over every readable SKILL.md emits name<TAB>desc pairs; both
  # renderings (full and the #1081 names-only degrade) derive from that single
  # pass in shell. The old shape forked awk per file and, whenever descriptions
  # overflowed the budget (the common case on a well-stocked node), discarded
  # the whole first pass and forked the per-file loop a second time.
  skill_files=()
  for skill_md in "$SKILLS_DIR"/*/SKILL.md; do
    [ -r "$skill_md" ] && skill_files+=("$skill_md")
  done
  skills_pairs=""
  if [ "${#skill_files[@]}" -gt 0 ]; then
    skills_pairs="$(awk '
      function flush() { if (name != "") printf "%s\t%s\n", name, desc; name=""; desc=""; fm=0; skip=0 }
      FNR==1 { flush(); if ($0 != "---") { skip=1 } else { fm=1 }; next }
      skip { next }
      /^---$/ { fm++; next }
      fm==1 && /^name:[ ]*/ { sub(/^name:[ ]*/,""); name=$0; next }
      fm==1 && /^description:[ ]*/ { sub(/^description:[ ]*/,""); desc=substr($0,1,160); next }
      END { flush() }' "${skill_files[@]}" 2>/dev/null | sort)"
  fi
  skills_index=""
  skills_names=""
  while IFS=$'\t' read -r s_name s_desc; do
    [ -n "$s_name" ] || continue
    skills_index+="- ${s_name} — ${s_desc}"$'\n'
    skills_names+="- ${s_name}"$'\n'
  done <<<"$skills_pairs"
  skills_index="${skills_index%$'\n'}"
  skills_names="${skills_names%$'\n'}"
  skills_note="read the SKILL.md before use; search these descriptions, not filenames"
  if [ -n "$skills_index" ] && [ "$(byte_len "$skills_index")" -gt "$MAX_SKILLS" ]; then
    # No silent tail-drop (#1081 lesson): degrade the WHOLE index to names
    # rather than truncating away the skills that sort last.
    skills_note="names only — descriptions exceed the byte budget; read each SKILL.md"
    skills_index="$skills_names"
  fi
  if [ -n "$skills_index" ]; then
    # The chosen rendering usually fits; only cap (one interpreter) when even
    # the names-only degrade overflows the budget.
    if [ "$(byte_len "$skills_index")" -gt "$MAX_SKILLS" ]; then
      skills_index="$(printf '%s' "$skills_index" | limit_bytes "$MAX_SKILLS")"
    fi
    skills_block="
## Node skills index (${skills_note})
${skills_index}
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
Memory profile: ${PROFILE}; last refresh: ${stamp:-never}; ${wiki_note}. A background refresh runs each session for the next one.

## Built-in MEMORY + USER
${mem:-(memory files unavailable)}
${ws_block}${promises_block}${detached_block}
## Local hot memory (task-conditioned cache search)
${local_hot:-(local hot memory disabled or no hits)}
${skills_block}${wiki_block}"

# Final hard cap. limit-bytes passes an under-cap payload through unchanged, so
# spend the interpreter only when there is something to cut; the shell-side
# path only has to mirror the trailing-newline strip of the `$(...)` it replaces.
# A non-numeric cap keeps going through the interpreter (its behaviour, not a
# new one here).
case "$MAX_TOTAL" in
  ''|*[!0-9]*) ctx="$(printf '%s' "$ctx" | limit_bytes "$MAX_TOTAL")" ;;
  *)
    if [ "$MAX_TOTAL" -gt 0 ] && [ "$(byte_len "$ctx")" -gt "$MAX_TOTAL" ]; then
      ctx="$(printf '%s' "$ctx" | limit_bytes "$MAX_TOTAL")"
    else
      ctx="${ctx%"${ctx##*[!$'\n']}"}"
    fi
    ;;
esac

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
