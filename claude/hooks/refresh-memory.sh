#!/usr/bin/env bash
# Background refresh of Family Wiki + Honcho memory caches.
# Run detached from the SessionStart hook so startup never blocks on slow LLM calls.
# Single-flight via flock; each source fail-open; caches updated atomically only on success.
set -uo pipefail

# Distill subprocess guard (see ~/.claude/hooks/distill.sh).
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

CACHE="${CCC_MEMORY_CACHE_DIR:-/root/.claude/hooks/cache}"
mkdir -p "$CACHE"

# Non-blocking single-flight lock: if a refresh is already running, exit.
exec 9>"$CACHE/.refresh.lock"
flock -n 9 || exit 0

WIKI="${CCC_WIKI_AGENT_BIN:-/root/.wiki-agent/bin/wiki-agent}"

# Honcho config is read from ~/.hermes/honcho.json — NEVER hard-code the endpoint here.
# baseUrl / workspace / peerName / target are node-local, not committed to this repo.
HONCHO_CFG="${CCC_HERMES_DIR:-/root/.hermes}/honcho.json"
HONCHO="$(jq -r '.baseUrl // empty' "$HONCHO_CFG" 2>/dev/null)"
WS="$(jq -r '.workspace // "seoyoon-family"' "$HONCHO_CFG" 2>/dev/null)"
PEER="$(jq -r '.peerName // empty' "$HONCHO_CFG" 2>/dev/null)"
TARGET="$(jq -r '.target // "seo-jin-on"' "$HONCHO_CFG" 2>/dev/null)"
# Optional bearer token for when the Honcho server runs with AUTH_USE_AUTH=true.
# Read from honcho.json (authToken/apiKey). When absent, no header is sent and
# behaviour is identical to before — so this is safe to ship ahead of any
# server-side auth change.
HONCHO_TOKEN="$(jq -r '.authToken // .apiKey // empty' "$HONCHO_CFG" 2>/dev/null)"
HONCHO_AUTH_ARGS=()
[ -n "$HONCHO_TOKEN" ] && HONCHO_AUTH_ARGS=(-H "Authorization: Bearer $HONCHO_TOKEN")

# Family Wiki cache prefetch (local, budget-capped). Set PREFETCH_QUERY per node.
PREFETCH_QUERY="${PREFETCH_QUERY:-this node operating memory, current status, and Seoyoon ops priorities}"
w="$(timeout 60 "$WIKI" --no-notify prefetch "$PREFETCH_QUERY" 2>/dev/null)"
if [ -n "$w" ]; then
  printf '%s\n' "$w" > "$CACHE/wiki.txt.tmp" && mv "$CACHE/wiki.txt.tmp" "$CACHE/wiki.txt"
fi

# Honcho dialectic — working memory about the user (network, LLM-backed)
if [ -n "$HONCHO" ] && [ -n "$PEER" ]; then
  h="$(timeout 60 curl -s -X POST \
    "$HONCHO/v3/workspaces/$WS/peers/$PEER/chat" \
    -H 'Content-Type: application/json' \
    "${HONCHO_AUTH_ARGS[@]}" \
    -d "{\"query\":\"Summarize what you know about working with the user: preferences, current priorities, and operating context.\",\"target\":\"$TARGET\",\"reasoning_level\":\"low\"}" \
    2>/dev/null | jq -r '.content // empty' 2>/dev/null)"
  if [ -n "$h" ]; then
    printf '%s\n' "$h" > "$CACHE/honcho.txt.tmp" && mv "$CACHE/honcho.txt.tmp" "$CACHE/honcho.txt"
  fi
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "$CACHE/.last-refresh"

# Update FTS5 hot index when CCC_MEMORY_PROFILE=hybrid or max-perf.
# The FTS5 scripts are shipped in the ccc-node repo scripts/ dir; resolve
# via CCC_FTS5_SCRIPT_DIR or auto-discover from the hook location.
FTS5_UPDATE="${CCC_FTS5_SCRIPT_DIR:-$HOME/ccc-node/scripts}/ccc-fts5-update.sh"
if [ -x "$FTS5_UPDATE" ]; then
  bash "$FTS5_UPDATE" >/dev/null 2>&1 || true
fi
