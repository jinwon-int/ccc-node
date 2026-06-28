#!/usr/bin/env bash
# UserPromptSubmit hook — secret-awareness (non-blocking, non-mutating).
# If the submitted prompt appears to contain a raw credential, inject a context
# warning so Claude treats it as sensitive (FW-03: never echo/store/commit raw secrets).
# Does NOT modify or block the prompt — only adds a reminder + audits the detection.
set -uo pipefail

input="$(cat 2>/dev/null)"; [ -n "$input" ] || exit 0
prompt="$(printf '%s' "$input" | jq -r '.prompt // .user_prompt // empty' 2>/dev/null)"
[ -n "$prompt" ] || exit 0

if grep -Eq '(ghp_|gho_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}|(sk-)[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----' <<<"$prompt"; then
  LOG="${CCC_AUDIT_LOG:-${HOME:-/root}/.claude/state/audit.jsonl}"; mkdir -p "$(dirname "$LOG")" 2>/dev/null
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
  jq -nc --arg ts "$ts" '{ts:$ts, event:"UserPromptSubmit", flag:"possible-raw-credential"}' >> "$LOG" 2>/dev/null
  jq -nc '{hookSpecificOutput:{hookEventName:"UserPromptSubmit", additionalContext:"⚠️ ccc-node guard: the submitted prompt appears to contain a raw credential. Per FW-03, do NOT echo, store, log, or commit it — reference its location/handling only and treat it as sensitive."}}'
fi
exit 0
