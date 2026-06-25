#!/usr/bin/env bash
# ccc-memory-eval.sh — local, no-network smoke/eval harness for memory changes.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
find_tool() { # <tool-name> <repo-relative-fallback>
  local name="$1" fallback="$2" d
  for d in "${CCC_MEMORY_TOOLS_DIR:-}" "$SCRIPT_DIR" "$ROOT/scripts"; do
    [ -n "$d" ] || continue
    if [ -x "$d/$name" ]; then printf '%s\n' "$d/$name"; return 0; fi
  done
  if [ -x "$ROOT/$fallback" ]; then printf '%s\n' "$ROOT/$fallback"; return 0; fi
  return 1
}
INDEX_TOOL="$(find_tool ccc-memory-index.sh scripts/ccc-memory-index.sh)"
SEARCH_TOOL="$(find_tool ccc-memory-search.sh scripts/ccc-memory-search.sh)"
LOAD_HOOK="$(find_tool load-memory.sh claude/hooks/load-memory.sh)"
QUERY="${1:-user preferences}"
KEEP_TMP="${CCC_MEMORY_EVAL_KEEP_TMP:-0}"

if [ -n "${CCC_STATE_DIR:-}" ]; then
  CALLER_STATE_DIR="$CCC_STATE_DIR"
  mkdir -p "$CALLER_STATE_DIR"
  STATE_DIR="$(mktemp -d "$CALLER_STATE_DIR/ccc-memory-eval.XXXXXX")"
  CREATED_STATE_DIR=1
else
  STATE_DIR="$(mktemp -d)"
  CREATED_STATE_DIR=1
fi
CACHE="${CCC_MEMORY_CACHE_DIR:-$STATE_DIR/cache}"
MEMORY_DIR="${CCC_MEMORY_DIR:-$STATE_DIR/memories}"
INDEX_OUT="$STATE_DIR/eval-index.json"
INDEX_ERR="$STATE_DIR/eval-index.err"
SEARCH_ERR="$STATE_DIR/eval-search.err"
LOAD_ERR="$STATE_DIR/eval-load.err"
START_MS="$(python3 -c 'import time; print(int(time.time()*1000))')"

cleanup() {
  if [ "$KEEP_TMP" = "1" ]; then
    return 0
  fi
  if [ "${CREATED_STATE_DIR:-0}" = "1" ] && [ -n "${STATE_DIR:-}" ] && [ -d "$STATE_DIR" ]; then
    rm -rf "$STATE_DIR"
  fi
}
trap cleanup EXIT
mkdir -p "$CACHE" "$MEMORY_DIR" "$STATE_DIR"
printf 'User prefers concise evidence-based reports.\n' > "$MEMORY_DIR/USER.md"
printf 'Live-check mutable node facts.\n' > "$MEMORY_DIR/MEMORY.md"
printf 'Family Wiki candidate: ccc-node memory cache TTL and Honcho hybrid profile.\n' > "$CACHE/wiki.txt"
printf 'Honcho summary: user prefers Korean practical reports and PR-first workflows.\n' > "$CACHE/honcho.txt"
printf 'eval-node\n' > "$STATE_DIR/node.txt"
printf '%s\n' "$QUERY" > "$STATE_DIR/current-task.txt"

CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_CACHE_DIR="$CACHE" CCC_MEMORY_DIR="$MEMORY_DIR" \
  "$INDEX_TOOL" rebuild >"$INDEX_OUT" 2>"$INDEX_ERR"
index_rc=$?
search_json="$(CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_INDEX_DB="$STATE_DIR/memory-index.sqlite" "$SEARCH_TOOL" "$QUERY" 2>"$SEARCH_ERR")"
search_rc=$?
load_json="$(CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_CACHE_DIR="$CACHE" CCC_MEMORY_DIR="$MEMORY_DIR" CCC_HOOK_DIR="$(dirname "$LOAD_HOOK")" CCC_MEMORY_TOOLS_DIR="$(dirname "$INDEX_TOOL")" CCC_MEMORY_PROFILE=hybrid CCC_LOCAL_MEMORY_ENABLED=1 CCC_MEMORY_QUERY="$QUERY" "$LOAD_HOOK" SessionStart 2>"$LOAD_ERR")"
load_rc=$?
END_MS="$(python3 -c 'import time; print(int(time.time()*1000))')"
bytes="$(printf '%s' "$load_json" | wc -c | tr -d '[:space:]')"
hits="$(printf '%s' "$search_json" | jq '.results | length' 2>/dev/null || printf 0)"

jq -n \
  --arg query "$QUERY" \
  --arg state_dir "$STATE_DIR" \
  --argjson index_rc "$index_rc" \
  --argjson search_rc "$search_rc" \
  --argjson load_rc "$load_rc" \
  --argjson latency_ms "$((END_MS - START_MS))" \
  --argjson injected_bytes "$bytes" \
  --argjson search_hits "$hits" \
  '{ok:($index_rc==0 and $search_rc==0 and $load_rc==0 and $search_hits>0), query:$query, state_dir:$state_dir, latency_ms:$latency_ms, injected_bytes:$injected_bytes, search_hits:$search_hits, rc:{index:$index_rc,search:$search_rc,load:$load_rc}}'
