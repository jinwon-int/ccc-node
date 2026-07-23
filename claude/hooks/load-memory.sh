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
LEGACY_RESUME_FILE="${CCC_MEMORY_LEGACY_RESUME_FILE:-$LEGACY_STATE_DIR/resume.md}"
RESUME_FILE="${CCC_RESUME_FILE:-$STATE_DIR/resume.md}"

LOAD_MEMORY_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || LOAD_MEMORY_LIB_DIR="$HOOKDIR"
# shellcheck source=claude/hooks/lib/hook-common.sh
. "$LOAD_MEMORY_LIB_DIR/lib/hook-common.sh" || exit 0
# shellcheck source=claude/hooks/lib/memory-common.sh
. "$LOAD_MEMORY_LIB_DIR/lib/memory-common.sh" || exit 0
# Rendering/budget/bounded-search helpers (#584 P2-1): the former inline python3
# heredocs live in this stdlib-only module. Every caller keeps its fail-open
# `||` fallback, so a missing module degrades exactly like a heredoc failure.
MEMORY_RENDER_PY="$LOAD_MEMORY_LIB_DIR/lib/memory_render.py"

scoped_paths_valid() {
  memory_scope_core_valid \
    && [ "$MEMDIR" = "$AUDIENCE_ROOT/$MEMORY_SCOPE/memories" ] \
    && [ "$RESUME_FILE" = "$AUDIENCE_ROOT/$MEMORY_SCOPE/state/resume.md" ]
}

if ! is_disabled "$AUDIENCE_SCOPED"; then
  # Family Wiki reads remain global. Honcho is allowed only through a
  # server-side workspace suffix bound to this validated opaque route.
  WIKI_ENABLED=0
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
  if [ -x "$HOOKDIR/scan-injection.sh" ] \
    && scanned="$(printf '%s' "$text" | "$HOOKDIR/scan-injection.sh" "$label" 2>/dev/null)"; then
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
# Audience-scoped mode treats every unscoped source as private legacy input.
if ! is_disabled "$AUDIENCE_SCOPED"; then
  scoped_mem="$(cat "$MEMDIR/MEMORY.md" "$MEMDIR/USER.md" 2>/dev/null)"
  shared_mem=""
  [ -n "$SHARED_MEMDIR" ] && shared_mem="$(cat "$SHARED_MEMDIR/MEMORY.md" "$SHARED_MEMDIR/USER.md" 2>/dev/null)"
  if [ "$MEMORY_AUDIENCE" = "private" ]; then
    legacy_mem="$(cat "$LEGACY_MEMDIR/MEMORY.md" "$LEGACY_MEMDIR/USER.md" 2>/dev/null)"
    [ -z "$legacy_mem" ] && legacy_mem="$(cat "${HOME:-/root}/.hermes/memories/MEMORY.md" "${HOME:-/root}/.hermes/memories/USER.md" 2>/dev/null)"
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
fi
honcho=""
if ! is_disabled "$HONCHO_ENABLED" && [ "$PROFILE" != "max-perf" ]; then
  honcho="$(cat "$CACHE/honcho.txt" 2>/dev/null)"
fi
resume="$(cat "$RESUME_FILE" 2>/dev/null)"
if ! is_disabled "$AUDIENCE_SCOPED" && [ "$MEMORY_AUDIENCE" = "private" ]; then
  legacy_resume="$(cat "$LEGACY_RESUME_FILE" 2>/dev/null)"
  resume="$(printf '%s\n%s' "$legacy_resume" "$resume")"
fi

# Limit the canonical blocks first (static caps) so we can measure their slack
# before sizing the local hot block.
mem="$(scan_injection_block built-in-memory "$mem" | limit_bytes "$MAX_MEM")"
resume="$(scan_injection_block resume-pointer "$resume" | limit_bytes "$MAX_RESUME")"
if ! is_disabled "$WIKI_ENABLED"; then
  wiki="$(scan_injection_block family-wiki-cache "$wiki" | limit_bytes "$MAX_WIKI")"
fi
honcho="$(scan_injection_block honcho-cache "$honcho" | limit_bytes "$MAX_HONCHO")"

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
  rsize="$(printf '%s' "$resume" | wc -c)"
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
    local_hot="$(run_memory_search_bounded "$search_tool" "$QUERY" "$search_limit" "${CCC_MEMORY_SEARCH_TIMEOUT_SEC:-3}" "$STATE_DIR")"
    if ! is_disabled "$AUDIENCE_SCOPED"; then
      # A just-committed Codex fact may not match the checkout-derived startup
      # query yet. The write-back indexer tags these rows `distilled`; merge one
      # small recent-fact lane so the immediately following isolated thread sees
      # the durable fact without waiting for another turn or background refresh.
      recent_hot="$(run_memory_search_bounded "$search_tool" "distilled text" "$search_limit" "${CCC_MEMORY_RECENT_SEARCH_TIMEOUT_SEC:-1}" "$STATE_DIR")"
      if [ "$MEMORY_AUDIENCE" = "private" ] \
        && [ -n "$SHARED_STATE_DIR" ] \
        && [ "$SHARED_STATE_DIR" != "$STATE_DIR" ]; then
        shared_hot="$(run_memory_search_bounded "$search_tool" "$QUERY" "$search_limit" "${CCC_MEMORY_SEARCH_TIMEOUT_SEC:-3}" "$SHARED_STATE_DIR")"
        if [ -n "$LEGACY_STATE_DIR" ] \
          && [ "$LEGACY_STATE_DIR" != "$STATE_DIR" ] \
          && [ "$LEGACY_STATE_DIR" != "$SHARED_STATE_DIR" ]; then
          legacy_hot="$(run_memory_search_bounded "$search_tool" "$QUERY" "$search_limit" "${CCC_MEMORY_LEGACY_SEARCH_TIMEOUT_SEC:-2}" "$LEGACY_STATE_DIR")"
        fi
      fi
      local_hot="$(merge_local_hot "$local_hot" "$recent_hot" "$shared_hot" "$legacy_hot")"
    fi
  fi
fi

local_hot="$(filter_disabled_wiki_hits "$local_hot")"

# Dedup the local hot block against what we ACTUALLY inject above (post-redaction,
# post-truncation) before rendering it — so it surfaces index-only content
# (distilled facts) instead of echoing the canonical blocks.
local_hot="$(dedup_local_hot "$mem
$wiki
$honcho" "$local_hot")"
# Render the search JSON to compact readable lines, then apply the (possibly
# enlarged) local byte budget.
local_hot="$(render_local_hot "$local_hot")"
local_hot="$(scan_injection_block local-hot-memory "$local_hot" | limit_bytes "$alloc_local")"

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

resume_block=""
if [ -n "${resume:-}" ]; then
  resume_block="▶ 직전 세션에서 이어서:
${resume}
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

## Local hot memory (task-conditioned cache search)
${local_hot:-(local hot memory disabled or no hits)}
${wiki_block}
## Honcho working memory — ${USER_LABEL}
${honcho:-(Honcho disabled or no Honcho cache yet)}"

ctx="$(printf '%s' "$ctx" | limit_bytes "$MAX_TOTAL")"

jq -n --arg ctx "$ctx" --arg event "$EVENT" \
  '{hookSpecificOutput:{hookEventName:$event,additionalContext:$ctx}}'

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
