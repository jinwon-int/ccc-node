#!/usr/bin/env bash
# Notification / Stop / SubagentStop hook — local observability of attention/lifecycle events.
# Records to the audit log; Notification events (attention/approval-needed) also append to
# a dedicated approval-needed log the operator (or the Telegram bridge) can surface.
# Local-only: this does NOT send outbound messages (Telegram delivery is a separate,
# approval-gated follow-up). Always exit 0 (these events cannot block).
set -uo pipefail

EVENT="${1:-Notification}"
input="$(cat 2>/dev/null)"
LOG="${CCC_AUDIT_LOG:-${HOME:-/root}/.claude/state/audit.jsonl}"
APPROVAL="${CCC_APPROVAL_LOG:-${HOME:-/root}/.claude/state/approval-needed.log}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
msg="$(printf '%s' "$input" | jq -r '.message // .notification // empty' 2>/dev/null)"

jq -nc --arg ts "$ts" --arg ev "$EVENT" --arg msg "$msg" \
  '{ts:$ts, event:$ev} + (if $msg != "" then {message:$msg} else {} end)' >> "$LOG" 2>/dev/null

if [ "$EVENT" = "Notification" ]; then
  printf '%s\t%s\n' "$ts" "${msg:-attention-needed}" >> "$APPROVAL" 2>/dev/null
fi

# Opt-in (CCC_NOTIFY_TELEGRAM=1): enqueue an owner-only Telegram push via the bridge spool.
# This hook NEVER touches the bot token — it writes a short, redacted summary file that the
# bridge's PushNotifier delivers (token stays in the bridge). Disabled by default; best-effort.
if [ "${CCC_NOTIFY_TELEGRAM:-0}" = "1" ] && { [ "$EVENT" = "Notification" ] || [ "$EVENT" = "Stop" ]; }; then
  SPOOL="${CCC_PUSH_SPOOL:-$HOME/.claude/state/telegram-spool}"
  if mkdir -p "$SPOOL" 2>/dev/null; then
    node="${CCC_NODE:-$(hostname -s 2>/dev/null || echo node)}"
    # Redact: collapse whitespace, mask token-shaped runs (>=20 word chars), cap length.
    summary="$(printf '%s' "${msg:-attention needed}" \
      | tr '\n\r\t' '   ' \
      | sed -E 's/[A-Za-z0-9_-]{20,}/[REDACTED]/g' \
      | cut -c1-300)"
    [ "$EVENT" = "Stop" ] && [ -z "$msg" ] && summary="세션이 정지했습니다 (응답 대기/종료)."
    fname="$SPOOL/$(printf '%s' "$ts" | tr ':' '-')-${EVENT}-$$.json"
    jq -nc --arg ts "$ts" --arg ev "$EVENT" --arg node "$node" --arg text "$summary" \
      '{ts:$ts, event:$ev, node:$node, text:$text, dedup:($ev+":"+$text)}' \
      > "$fname" 2>/dev/null || true
  fi
fi

# SessionEnd: archive the working-state checkpoint so it survives session exit.
if [ "$EVENT" = "SessionEnd" ]; then
  WS="${CCC_WORKING_STATE:-${HOME:-/root}/.claude/state/working-state.md}"
  ARCH_DIR="${CCC_SESSION_ARCHIVE:-${HOME:-/root}/.claude/state/session-archive}"
  if [ -f "$WS" ]; then
    mkdir -p "$ARCH_DIR" 2>/dev/null
    stamp="$(printf '%s' "$ts" | tr ':' '-')"
    cp "$WS" "$ARCH_DIR/working-state-${stamp}.md" 2>/dev/null
  fi
fi
exit 0
