#!/usr/bin/env bash
# SessionStart memory bootstrap for a Claude Code node (node-owned memory).
# Serves built-in MEMORY/USER + cached Family Wiki + cached Honcho instantly,
# then fires a detached background refresh so the next session is fresh.
set -uo pipefail

# Event name drives hookEventName in the output so the same script serves
# both SessionStart (fresh session) and PostCompact (re-inject after compaction).
EVENT="${1:-SessionStart}"

CACHE=/root/.claude/hooks/cache
HOOKDIR=/root/.claude/hooks

# Node-owned memory lives under ~/.claude/memories (Hermes-independent).
# Fall back to the legacy ~/.hermes/memories only if the local copy is absent.
MEMDIR=/root/.claude/memories
mem="$(cat "$MEMDIR/MEMORY.md" "$MEMDIR/USER.md" 2>/dev/null)"
[ -z "$mem" ] && mem="$(cat /root/.hermes/memories/MEMORY.md /root/.hermes/memories/USER.md 2>/dev/null)"
wiki="$(cat "$CACHE/wiki.txt" 2>/dev/null)"
honcho="$(cat "$CACHE/honcho.txt" 2>/dev/null)"
stamp="$(cat "$CACHE/.last-refresh" 2>/dev/null)"

node_label="${CCC_NODE:-$(cat /root/.claude/state/node.txt 2>/dev/null || hostname -s 2>/dev/null || printf 'ccc-node')}"

ctx="# ${node_label} session memory (auto-injected: $EVENT)

Operational facts are mutable — live-check the node and verify Wiki source text before asserting or changing anything.
Family Wiki + Honcho blocks below are cached (last refreshed: ${stamp:-never}); a background refresh runs each session for the next one.

## Built-in MEMORY + USER
${mem:-(memory files unavailable)}

## Family Wiki (cache prefetch — candidates; verify with wiki-agent load before operational claims)
${wiki:-(no wiki cache yet — will populate after first background refresh)}

## Honcho working memory — Seo Jin On
${honcho:-(no honcho cache yet — will populate after first background refresh)}"

jq -n --arg ctx "$ctx" --arg event "$EVENT" \
  '{hookSpecificOutput:{hookEventName:$event,additionalContext:$ctx}}'

# Fire-and-forget: refresh caches for the NEXT session, fully detached so startup never waits.
setsid bash "$HOOKDIR/refresh-memory.sh" >/dev/null 2>&1 </dev/null &
