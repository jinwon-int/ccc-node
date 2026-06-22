#!/usr/bin/env bash
# SessionStart tool/command cheatsheet injection.
# Mirrors load-memory.sh output shape so the cheatsheet lands in context each session.
set -uo pipefail

# Distill subprocess guard (see ~/.claude/hooks/distill.sh).
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

EVENT="${1:-SessionStart}"
CHEAT=/root/.claude/hooks/tools-cheatsheet.md

ctx="$(cat "$CHEAT" 2>/dev/null)"
[ -z "$ctx" ] && ctx="(tools cheatsheet missing: $CHEAT)"

jq -n --arg ctx "$ctx" --arg event "$EVENT" \
  '{hookSpecificOutput:{hookEventName:$event,additionalContext:$ctx}}'
