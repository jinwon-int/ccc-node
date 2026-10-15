#!/usr/bin/env bash
# UserPromptSubmit hook — secret-awareness (non-blocking, non-mutating).
# If the submitted prompt appears to contain a raw credential, inject a context
# warning so Claude treats it as sensitive (FW-03: never echo/store/commit raw secrets).
# Does NOT modify or block the prompt — only adds a reminder + audits the detection.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
# shellcheck source=claude/hooks/lib/lifecycle-common.sh
[ -r "$HERE/lib/lifecycle-common.sh" ] && . "$HERE/lib/lifecycle-common.sh"
umask 077

input="$(cat 2>/dev/null)"; [ -n "$input" ] || exit 0
printf '%s' "$input" | bash "$HERE/lifecycle-feed.sh" UserPromptSubmit >/dev/null 2>&1 || true
prompt="$(printf '%s' "$input" | jq -r '.prompt // .user_prompt // empty' 2>/dev/null)"
[ -n "$prompt" ] || exit 0

# OAuth `code#state` login codes (Wiki DOC-3956): a 30+ char run that mixes
# upper case, lower case and digits, then `#`, then 20+ chars. ERE has no
# lookahead, so the mix is checked on the extracted candidates.
has_oauth_code() {
  local cand
  while IFS= read -r cand; do
    case "${cand%%#*}" in *[A-Z]*) ;; *) continue ;; esac
    case "${cand%%#*}" in *[a-z]*) ;; *) continue ;; esac
    case "${cand%%#*}" in *[0-9]*) return 0 ;; esac
  done < <(grep -oE '[A-Za-z0-9_-]{30,}#[A-Za-z0-9_-]{20,}' <<<"$1")
  return 1
}
if grep -Eq '(ghp_|gho_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}|(sk-)[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----' <<<"$prompt" \
  || has_oauth_code "$prompt"; then
  state_dir="$(ccc_lifecycle_state_dir 2>/dev/null || true)"
  LOG="${CCC_AUDIT_LOG:-${state_dir:+$state_dir/audit.jsonl}}"
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
  record="$(jq -nc --arg ts "$ts" \
    '{ts:$ts, event:"UserPromptSubmit", flag:"possible-raw-credential"}' 2>/dev/null || true)"
  if [ -n "$record" ] && command -v ccc_lifecycle_append_line >/dev/null 2>&1; then
    ccc_lifecycle_append_line "$LOG" "$record" || true
  fi
  jq -nc '{hookSpecificOutput:{hookEventName:"UserPromptSubmit", additionalContext:"⚠️ ccc-node guard: the submitted prompt appears to contain a raw credential. Per FW-03, do NOT echo, store, log, or commit it — reference its location/handling only and treat it as sensitive."}}'
fi
exit 0
