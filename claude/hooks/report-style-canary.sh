#!/usr/bin/env bash
# SessionStart: owner-facing report-style canary (#2109 A — Karpathy 2026-10-02,
# "ASD-STE100 at 80%"). Injects the controlled-language rule block as
# additionalContext ONLY on a node whose operator armed the canary flag; every
# other node gets nothing, so the fleet-wide ccc-report style is unchanged until
# the owner's verdict folds the rule in. Body-free, fail-open (always exit 0).
#
# Arm:    touch ~/.claude/state/report-style-canary.flag   (optionally write the
#         KST end time inside; it is echoed back so the model knows the window)
# Disarm: rm ~/.claude/state/report-style-canary.flag
set -uo pipefail

# Distill subprocess guard (see ~/.claude/hooks/distill.sh).
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

EVENT="${1:-SessionStart}"
case "$EVENT" in SessionStart|PostCompact) ;; *) exit 0 ;; esac

CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}"
HOOK_DIR="${CCC_HOOK_DIR:-$CLAUDE_DIR/hooks}"
FLAG="$CLAUDE_DIR/state/report-style-canary.flag"
RULE="$HOOK_DIR/lib/report-style-ste.txt"

[ -f "$FLAG" ] || exit 0
rule="$(cat "$RULE" 2>/dev/null)"
[ -n "$rule" ] || exit 0   # rule text missing: stay silent rather than inject a stub

# First non-empty line of the flag, if any, is the operator's window note
# (e.g. "end: 2026-10-09 18:00 KST"); bounded so a stray large file cannot
# bloat the context.
note="$(grep -m1 -E '\S' "$FLAG" 2>/dev/null | cut -c1-160)"
ctx="$rule"
[ -n "$note" ] && ctx="$ctx
(카나리 메모: $note)"

jq -n --arg ctx "$ctx" --arg event "$EVENT" \
  '{hookSpecificOutput:{hookEventName:$event,additionalContext:$ctx}}' 2>/dev/null
exit 0
