#!/usr/bin/env bash
# PostToolUse audit hook — body-free record of mutating tool completions.
# PostToolUse cannot block; always exit 0. Raw commands, paths and session ids
# are inspected only to derive booleans/opaque refs and are never persisted.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
# shellcheck source=claude/hooks/lib/lifecycle-common.sh
[ -r "$HERE/lib/lifecycle-common.sh" ] && . "$HERE/lib/lifecycle-common.sh"
umask 077

input="$(cat 2>/dev/null)"; [ -n "$input" ] || exit 0
printf '%s' "$input" | bash "$HERE/lifecycle-feed.sh" PostToolUse >/dev/null 2>&1 || true

tool="$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)"
case "$tool" in
  Bash|Write|Edit|MultiEdit|NotebookEdit) ;;
  *) exit 0 ;;
esac

cmd="$(printf '%s' "$input"   | jq -r '.tool_input.command // empty'   2>/dev/null)"
fpath="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
sid="$(printf '%s' "$input"   | jq -r '.session_id // empty'           2>/dev/null)"
session_ref=""
if command -v ccc_lifecycle_ref >/dev/null 2>&1; then
  session_ref="$(ccc_lifecycle_ref "$sid" 2>/dev/null || true)"
fi

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
target_shape=""
[ -n "$cmd" ] && target_shape="command"
[ -z "$target_shape" ] && [ -n "$fpath" ] && target_shape="file"
file_change=false
case "$tool" in Write|Edit|MultiEdit|NotebookEdit) file_change=true ;; esac
verification=false
if [ "$tool" = "Bash" ] && printf '%s' "$cmd" \
   | grep -iEq '\b(pytest|test|validate|verify|--dry-run|--check|shellcheck|bats|gh pr checks|git diff|git status|lint|ruff|mypy|tsc|typecheck)\b'; then
  verification=true
fi

record="$(jq -nc --arg ts "$ts" --arg tool "$tool" --arg ref "$session_ref" \
  --arg shape "$target_shape" --argjson changed "$file_change" --argjson verified "$verification" \
  '{ts:$ts, tool:$tool}
   + (if $ref != "" then {session_ref:$ref} else {} end)
   + (if $shape != "" then {target_shape:$shape} else {} end)
   + (if $changed then {file_change:true} else {} end)
   + (if $verified then {verification:true} else {} end)' 2>/dev/null)" || exit 0

state_dir="$(ccc_lifecycle_state_dir 2>/dev/null || true)"
LOG="${CCC_AUDIT_LOG:-${state_dir:+$state_dir/audit.jsonl}}"
if command -v ccc_lifecycle_append_line >/dev/null 2>&1; then
  ccc_lifecycle_append_line "$LOG" "$record" || true
fi

exit 0
