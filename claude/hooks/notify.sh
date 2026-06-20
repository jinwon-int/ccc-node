#!/usr/bin/env bash
# Notification / Stop / SubagentStop hook — local observability of attention/lifecycle events.
# Records to the audit log; Notification events (attention/approval-needed) also append to
# a dedicated approval-needed log the operator (or the Telegram bridge) can surface.
# Local-only: this does NOT send outbound messages (Telegram delivery is a separate,
# approval-gated follow-up). Always exit 0 (these events cannot block).
set -uo pipefail

EVENT="${1:-Notification}"
input="$(cat 2>/dev/null)"
LOG="${CCC_AUDIT_LOG:-/root/.claude/state/audit.jsonl}"
APPROVAL="${CCC_APPROVAL_LOG:-/root/.claude/state/approval-needed.log}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
msg="$(printf '%s' "$input" | jq -r '.message // .notification // empty' 2>/dev/null)"

jq -nc --arg ts "$ts" --arg ev "$EVENT" --arg msg "$msg" \
  '{ts:$ts, event:$ev} + (if $msg != "" then {message:$msg} else {} end)' >> "$LOG" 2>/dev/null

if [ "$EVENT" = "Notification" ]; then
  printf '%s\t%s\n' "$ts" "${msg:-attention-needed}" >> "$APPROVAL" 2>/dev/null
fi
exit 0
