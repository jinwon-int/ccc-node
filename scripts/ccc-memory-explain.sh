#!/usr/bin/env bash
# ccc-memory-explain.sh — read-only explanation of task-conditioned memory recall.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${CCC_STATE_DIR:-/root/.claude/state}"
CACHE="${CCC_MEMORY_CACHE_DIR:-/root/.claude/hooks/cache}"
MEMORY_DIR="${CCC_MEMORY_DIR:-/root/.claude/memories}"
OUTPUT="text"
QUERY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --json) OUTPUT="json"; shift ;;
    --query) QUERY="${2:-}"; shift 2 ;;
    --help|-h)
      echo "usage: $0 [--json] [--query <query>]"; exit 0 ;;
    *) QUERY="${QUERY:+$QUERY }$1"; shift ;;
  esac
done
find_tool() { # <name>
  local name="$1" d
  for d in "${CCC_MEMORY_TOOLS_DIR:-}" "$SCRIPT_DIR" "$SCRIPT_DIR/../scripts"; do
    [ -n "$d" ] || continue
    if [ -x "$d/$name" ]; then printf '%s\n' "$d/$name"; return 0; fi
  done
  return 1
}
QUERY_TOOL="$(find_tool ccc-memory-query.sh 2>/dev/null || true)"
SEARCH_TOOL="$(find_tool ccc-memory-search.sh 2>/dev/null || true)"
CHECK_TOOL="$(find_tool ccc-memory-check.sh 2>/dev/null || true)"
[ -n "$QUERY" ] || QUERY="$(CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_CACHE_DIR="$CACHE" CCC_MEMORY_DIR="$MEMORY_DIR" "$QUERY_TOOL" --mode local 2>/dev/null || printf 'current task')"
search_json="{}"
if [ -n "$SEARCH_TOOL" ]; then
  search_json="$(CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_CACHE_DIR="$CACHE" CCC_MEMORY_DIR="$MEMORY_DIR" "$SEARCH_TOOL" "$QUERY" 2>/dev/null || printf '{}')"
fi
check_json="{}"
if [ -n "$CHECK_TOOL" ]; then
  check_json="$(CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_CACHE_DIR="$CACHE" CCC_MEMORY_DIR="$MEMORY_DIR" "$CHECK_TOOL" --json 2>/dev/null || printf '{}')"
fi
jq -n \
  --arg query "$QUERY" \
  --arg retrieval "${CCC_MEMORY_RETRIEVAL:-fts}" \
  --arg state_dir "$STATE_DIR" \
  --arg cache_dir "$CACHE" \
  --arg memory_dir "$MEMORY_DIR" \
  --argjson search "$search_json" \
  --argjson check "$check_json" \
  --argjson max_total "${CCC_MEMORY_MAX_BYTES:-12000}" \
  --argjson max_mem "${CCC_BUILTIN_MEMORY_MAX_BYTES:-4000}" \
  --argjson max_wiki "${CCC_WIKI_MAX_BYTES:-5000}" \
  --argjson max_honcho "${CCC_HONCHO_MAX_BYTES:-4000}" \
  --argjson max_local "${CCC_LOCAL_MEMORY_MAX_BYTES:-3000}" \
  '{ok:true, query:$query, retrievalMode:$retrieval, paths:{state_dir:$state_dir,cache_dir:$cache_dir,memory_dir:$memory_dir}, budgets:{total:$max_total,built_in:$max_mem,wiki:$max_wiki,honcho:$max_honcho,local_hot:$max_local}, cache:$check, search:$search, safety:{read_only:true, no_network:true, raw_secret_output:false, retrieved_context_is_untrusted:true}}' \
  > "${TMPDIR:-/tmp}/ccc-memory-explain.$$.json"
if [ "$OUTPUT" = "json" ]; then
  cat "${TMPDIR:-/tmp}/ccc-memory-explain.$$.json"
else
  jq -r '"# ccc memory explain\n\n- query: \(.query)\n- retrievalMode: \(.retrievalMode)\n- state: \(.paths.state_dir)\n- cache: \(.paths.cache_dir)\n- total budget: \(.budgets.total) bytes\n- wiki status: \(.cache.wiki.status // "unknown")\n- honcho status: \(.cache.honcho.status // "unknown")\n\n## Top results\n" + ((.search.results // []) | to_entries | map("\(.key+1). [\(.value.source)] \(.value.path) score=\(.value.score // "n/a")") | join("\n"))' "${TMPDIR:-/tmp}/ccc-memory-explain.$$.json"
fi
rm -f "${TMPDIR:-/tmp}/ccc-memory-explain.$$.json"
