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
# The feed CLI is default-off; mirror its own gate here so the default path
# does not pay a no-op bash fork on every mutating tool call.
case "${CCC_LIFECYCLE_AUDIT:-}" in
  1|true|TRUE|yes|YES|on|ON)
    printf '%s' "$input" | bash "$HERE/lifecycle-feed.sh" PostToolUse >/dev/null 2>&1 || true
    ;;
esac

# One jq pass derives everything this hook needs (this hook runs on every
# mutating tool call, so each extra fork is paid constantly). Only the raw
# session id leaves jq, for the shell-side opaque-ref helper; the command and
# file path never leave the jq process at all.
meta="$(printf '%s' "$input" | jq -r '
  (.tool_name // "") as $tool
  | (.tool_input.command // "") as $cmd
  | (.tool_input.file_path // "") as $fpath
  | if (["Bash","Write","Edit","MultiEdit","NotebookEdit"] | index($tool)) != null then
      [$tool,
       (.session_id // ""),
       (if $cmd != "" then "command" elif $fpath != "" then "file" else "" end),
       (if $tool != "Bash" then "true" else "false" end),
       (if $tool == "Bash" and ($cmd
          | test("\\b(pytest|test|validate|verify|--dry-run|--check|shellcheck|bats|gh pr checks|git diff|git status|lint|ruff|mypy|tsc|typecheck)\\b"; "i"))
        then "true" else "false" end)]
      | join("\u0001")
    else empty end
' 2>/dev/null)" || exit 0
[ -n "$meta" ] || exit 0
tool=""; sid=""; target_shape=""; file_change=false; verification=false
IFS=$'\x01' read -r tool sid target_shape file_change verification <<<"$meta" || true
case "$file_change" in true|false) ;; *) exit 0 ;; esac
case "$verification" in true|false) ;; *) exit 0 ;; esac

session_ref=""
if command -v ccc_lifecycle_ref >/dev/null 2>&1; then
  session_ref="$(ccc_lifecycle_ref "$sid" 2>/dev/null || true)"
fi

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"

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
