#!/usr/bin/env bash
# PostToolUse(Bash|Edit|Write|MultiEdit|NotebookEdit) identical-call streak
# reminder (#2261).
#
# A turn-based agent can spin, reissuing the same failing action until it
# exhausts its budget. Harness-design evidence (arXiv 2609.20804 §2.4) keeps a
# streak monitor on the tool log for exactly this. Danso has
# CCC_DANSO_TASK_REPEAT_LIMIT and Codex an orphan-tool-loop counter; the Claude
# lane had nothing.
#
# Contract:
#   - Identical call = same tool_name AND byte-identical tool_input (JSON
#     compacted). Read is deliberately outside the matcher: re-reading is normal.
#   - Reminder, not a gate: PostToolUse cannot block; this hook always exits 0.
#     At CCC_TOOL_STREAK_LIMIT consecutive identical calls (default 3) it emits
#     ONE hookSpecificOutput.additionalContext for that streak; the streak must
#     break (a different call) before it can warn again.
#   - State per session under $CCC_STATE_DIR/tool-streak/<sha256(session)[:16]>:
#     one line "<input-sha256> <count> <warned>". No raw session id, command,
#     path or content is persisted — only hashes and counters. Files older than
#     2 days are pruned. Off switch: CCC_TOOL_STREAK=0.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
# shellcheck source=claude/hooks/lib/hook-common.sh
[ -r "$HERE/lib/hook-common.sh" ] && . "$HERE/lib/hook-common.sh"
command -v is_disabled >/dev/null 2>&1 || is_disabled() { case "${1:-}" in 0|false|FALSE|off|OFF|no|NO) return 0;; *) return 1;; esac; }
umask 077

is_disabled "${CCC_TOOL_STREAK:-1}" && exit 0
command -v jq >/dev/null 2>&1 || exit 0
command -v sha256sum >/dev/null 2>&1 || exit 0

input="$(cat 2>/dev/null)"; [ -n "$input" ] || exit 0

LIMIT="${CCC_TOOL_STREAK_LIMIT:-3}"; case "$LIMIT" in ''|*[!0-9]*) LIMIT=3 ;; esac
[ "$LIMIT" -ge 2 ] || LIMIT=2

# One jq pass: tool name, session id and the compacted input (hashed below;
# the raw input never leaves this process).
meta="$(printf '%s' "$input" | jq -r '
  (.tool_name // "") as $tool
  | if (["Bash","Edit","Write","MultiEdit","NotebookEdit"] | index($tool)) != null
    then [$tool, (.session_id // ""), ((.tool_input // {}) | tojson)] | join("\u0001")
    else empty end
' 2>/dev/null)" || exit 0
[ -n "$meta" ] || exit 0
tool=""; sid=""; tinput=""
IFS=$'\x01' read -r tool sid tinput <<<"$meta" || true
[ -n "$sid" ] || exit 0

sid_hash="$(printf '%s' "$sid" | sha256sum | cut -c1-16)"
call_hash="$(printf '%s\x1f%s' "$tool" "$tinput" | sha256sum | cut -c1-32)"

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}/tool-streak"
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
chmod 700 "$STATE_DIR" 2>/dev/null || :
state="$STATE_DIR/$sid_hash"

prev_hash=""; count=0; warned=0
if [ -f "$state" ]; then
  read -r prev_hash count warned < "$state" 2>/dev/null || { prev_hash=""; count=0; warned=0; }
  case "$count" in ''|*[!0-9]*) count=0 ;; esac
  case "$warned" in 0|1) ;; *) warned=0 ;; esac
fi

if [ "$prev_hash" = "$call_hash" ]; then count=$((count + 1)); else count=1; warned=0; fi

emit=0
if [ "$count" -ge "$LIMIT" ] && [ "$warned" -eq 0 ]; then emit=1; warned=1; fi

tmp="$state.tmp.$$"
if printf '%s %s %s\n' "$call_hash" "$count" "$warned" > "$tmp" 2>/dev/null; then
  mv -f "$tmp" "$state" 2>/dev/null || rm -f "$tmp" 2>/dev/null
fi
# Cheap prune of stale sessions (the hook only runs on mutating calls).
find "$STATE_DIR" -maxdepth 1 -type f -mtime +2 -delete 2>/dev/null || :

[ "$emit" -eq 1 ] || exit 0

ctx="tool-streak: the same $tool call was issued $count times in a row with identical input. Repeating it is unlikely to change the result — re-read the error, change the approach, or stop and report the blocker."
jq -n --arg ctx "$ctx" '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}' 2>/dev/null || :
exit 0
