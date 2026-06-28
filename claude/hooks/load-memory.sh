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
HONCHO_ENABLED="${CCC_HONCHO_MEMORY_ENABLED:-1}"
# Local hot-memory search is ON by default for every profile now that the
# default retrieval reranks with durability/source/recency boosts; set
# CCC_LOCAL_MEMORY_ENABLED=0/false/off to opt out. hybrid/max-perf always query
# it regardless (that is part of their definition).
LOCAL_ENABLED="${CCC_LOCAL_MEMORY_ENABLED:-}"
QUERY="${CCC_MEMORY_QUERY:-}"

is_disabled() { case "${1:-}" in 0|false|FALSE|off|OFF|no|NO) return 0;; *) return 1;; esac; }

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
  python3 -c 'import sys
limit = int(sys.argv[1])
data = sys.stdin.buffer.read()
if limit > 0 and len(data) > limit:
    text = data[:limit].decode("utf-8", errors="ignore")
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.write("\n… [truncated by CCC memory budget]\n")
else:
    sys.stdout.buffer.write(data)
' "$max"
}

find_memory_tool() { # <tool-name>
  local name="$1" d
  for d in "${CCC_MEMORY_TOOLS_DIR:-}" "$HOOKDIR" "$HOOKDIR/../../scripts"; do
    [ -n "$d" ] || continue
    if [ -x "$d/$name" ]; then printf '%s\n' "$d/$name"; return 0; fi
  done
  return 1
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
mem="$(cat "$MEMDIR/MEMORY.md" "$MEMDIR/USER.md" 2>/dev/null)"
[ -z "$mem" ] && mem="$(cat ${HOME:-/root}/.hermes/memories/MEMORY.md ${HOME:-/root}/.hermes/memories/USER.md 2>/dev/null)"
wiki="$(cat "$CACHE/wiki.txt" 2>/dev/null)"
honcho=""
if ! is_disabled "$HONCHO_ENABLED" && [ "$PROFILE" != "max-perf" ]; then
  honcho="$(cat "$CACHE/honcho.txt" 2>/dev/null)"
fi

local_hot=""
if [ "$PROFILE" = "hybrid" ] || [ "$PROFILE" = "max-perf" ] || ! is_disabled "$LOCAL_ENABLED"; then
  search_tool="$(find_memory_tool ccc-memory-search.sh 2>/dev/null || true)"
  if [ -n "$search_tool" ]; then
    local_hot="$({ "$search_tool" "$QUERY" 2>/dev/null || true; } | sed -n '1,120p')"
  fi
fi

mem="$(scan_injection_block built-in-memory "$mem" | limit_bytes "$MAX_MEM")"
wiki="$(scan_injection_block family-wiki-cache "$wiki" | limit_bytes "$MAX_WIKI")"
honcho="$(scan_injection_block honcho-cache "$honcho" | limit_bytes "$MAX_HONCHO")"
local_hot="$(scan_injection_block local-hot-memory "$local_hot" | limit_bytes "$MAX_LOCAL")"

node_label="${CCC_NODE:-$(cat "$STATE_DIR/node.txt" 2>/dev/null || hostname -s 2>/dev/null || printf 'ccc-node')}"
stamp="$(cat "$CACHE/.last-refresh" 2>/dev/null)"
wiki_note="$(stale_note 'Family Wiki' "$CACHE/wiki.txt")"
honcho_note="Honcho disabled"
if ! is_disabled "$HONCHO_ENABLED" && [ "$PROFILE" != "max-perf" ]; then
  honcho_note="$(stale_note 'Honcho' "$CACHE/honcho.txt")"
fi

ctx="# ${node_label} session memory (auto-injected: $EVENT)

Operational facts are mutable — live-check the node and verify Wiki source text before asserting or changing anything.
Memory profile: ${PROFILE}; last refresh: ${stamp:-never}; ${wiki_note}; ${honcho_note}. A background refresh runs each session for the next one.

## Built-in MEMORY + USER
${mem:-(memory files unavailable)}

## Local hot memory (task-conditioned cache search)
${local_hot:-(local hot memory disabled or no hits)}

## Family Wiki (cache prefetch — candidates; verify with wiki-agent load before operational claims)
${wiki:-(no wiki cache yet — will populate after first background refresh)}

## Honcho working memory — Seo Jin On
${honcho:-(Honcho disabled or no Honcho cache yet)}"

ctx="$(printf '%s' "$ctx" | limit_bytes "$MAX_TOTAL")"

jq -n --arg ctx "$ctx" --arg event "$EVENT" \
  '{hookSpecificOutput:{hookEventName:$event,additionalContext:$ctx}}'

# Fire-and-forget: refresh caches for the NEXT session, fully detached so startup never waits.
setsid bash "$HOOKDIR/refresh-memory.sh" >/dev/null 2>&1 </dev/null &
