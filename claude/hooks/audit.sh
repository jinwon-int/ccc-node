#!/usr/bin/env bash
# PostToolUse audit hook — append-only, redaction-mandatory record of mutating tool calls.
# PostToolUse cannot block; always exit 0. Records to ~/.claude/state/audit.jsonl (JSONL).
# Secrets are redacted before they ever touch the log (FW-03).
set -uo pipefail

LOG="${CCC_AUDIT_LOG:-${HOME:-/root}/.claude/state/audit.jsonl}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null

input="$(cat 2>/dev/null)"; [ -n "$input" ] || exit 0
tool="$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)"
case "$tool" in
  Bash|Write|Edit|MultiEdit|NotebookEdit) ;;
  *) exit 0 ;;
esac

cmd="$(printf '%s' "$input"   | jq -r '.tool_input.command // empty'   2>/dev/null)"
fpath="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
sid="$(printf '%s' "$input"   | jq -r '.session_id // empty'           2>/dev/null)"

redact() {
  sed -E \
    -e 's/(ghp_|gho_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]+/\1<redacted>/g' \
    -e 's/(sk-)[A-Za-z0-9_-]{12,}/\1<redacted>/g' \
    -e 's/(AKIA)[A-Z0-9]{8,}/\1<redacted>/g' \
    -e 's/(-----BEGIN[^-]*PRIVATE KEY-----).*/\1<redacted>/g' \
    -e 's/((password|passwd|secret|token|api[_-]?key|authorization)[=:[:space:]"'"'"']+)[^[:space:]"'"'"'&|;]+/\1<redacted>/gI'
}

rcmd="$(printf '%s' "$cmd" | redact)"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"

jq -nc --arg ts "$ts" --arg tool "$tool" --arg cmd "$rcmd" --arg fp "$fpath" --arg sid "$sid" \
  '{ts:$ts, tool:$tool}
   + (if $sid != "" then {session_id:$sid} else {} end)
   + (if $cmd != "" then {command:$cmd} else {} end)
   + (if $fp  != "" then {file_path:$fp} else {} end)' >> "$LOG" 2>/dev/null

exit 0
