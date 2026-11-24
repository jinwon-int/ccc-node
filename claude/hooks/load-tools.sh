#!/usr/bin/env bash
# SessionStart tool/command cheatsheet injection.
# Mirrors load-memory.sh output shape so the cheatsheet lands in context each session.
set -uo pipefail

# Distill subprocess guard (see ~/.claude/hooks/distill.sh).
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

EVENT="${1:-SessionStart}"
# Same hook-dir override load-memory.sh honours, so a test/alternate tree can
# point at its own cheatsheet (#1484).
CHEAT="${CCC_HOOK_DIR:-${HOME:-/root}/.claude/hooks}/tools-cheatsheet.md"

ctx="$(cat "$CHEAT" 2>/dev/null)"
[ -z "$ctx" ] && ctx="(tools cheatsheet missing: $CHEAT)"

# Fleet notes (#2858). The cheatsheet is node-owned: setup.sh seeds it once and
# never overwrites it, so a line added to the template reaches no installed
# node. Guidance every node should have therefore lives in a repo-managed file
# the hook-tree walk deploys on each update, and is appended here.
#   CCC_TOOLS_FLEET_NOTES=0 (false/off/no) turns the block off.
# Never make a node worse: Claude Code replaces a hook output over 10,000
# characters with a file path and a short preview, so the block is skipped
# when cheatsheet + notes would pass 9,500 BYTES (a string has no more
# characters than bytes, and the shell cannot count characters without
# trusting the locale).
NOTES="${CHEAT%/*}/lib/tools-fleet-notes.txt"
FLEET_NOTES_MAX_BYTES=9500
byte_len() { local LC_ALL=C; printf '%s' "${#1}"; }
case "${CCC_TOOLS_FLEET_NOTES:-1}" in
  0|false|FALSE|off|OFF|no|NO) notes="" ;;
  *) notes="$(cat "$NOTES" 2>/dev/null)" ;;
esac
if [ -n "$notes" ] && [ "$(( $(byte_len "$ctx") + $(byte_len "$notes") + 2 ))" -le "$FLEET_NOTES_MAX_BYTES" ]; then
  ctx="$ctx"$'\n\n'"$notes"
fi

jq -n --arg ctx "$ctx" --arg event "$EVENT" \
  '{hookSpecificOutput:{hookEventName:$event,additionalContext:$ctx}}'
