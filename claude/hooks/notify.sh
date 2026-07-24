#!/usr/bin/env bash
# Notification / Stop / SubagentStop hook — body-free local observability.
# Raw provider messages are never written to the audit, approval log, or spool.
# Local-only: this does NOT send outbound messages (Telegram delivery is a separate,
# approval-gated follow-up). Always exit 0 (these events cannot block).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
# shellcheck source=claude/hooks/lib/lifecycle-common.sh
[ -r "$HERE/lib/lifecycle-common.sh" ] && . "$HERE/lib/lifecycle-common.sh"
umask 077
EVENT="${1:-Notification}"
input="$(cat 2>/dev/null)"
printf '%s' "$input" | bash "$HERE/lifecycle-feed.sh" "$EVENT" >/dev/null 2>&1 || true
state_dir="$(ccc_lifecycle_state_dir 2>/dev/null || true)"
LOG="${CCC_AUDIT_LOG:-${state_dir:+$state_dir/audit.jsonl}}"
APPROVAL="${CCC_APPROVAL_LOG:-${state_dir:+$state_dir/approval-needed.log}}"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
message_present="$(printf '%s' "$input" | jq -r '((.message // .notification // "") != "")' 2>/dev/null)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)"
session_ref=""
if command -v ccc_lifecycle_ref >/dev/null 2>&1; then
  session_ref="$(ccc_lifecycle_ref "$sid" 2>/dev/null || true)"
fi

if [ -n "$LOG" ] && [ ! -L "$LOG" ]; then
  mkdir -p "$(dirname "$LOG")" 2>/dev/null
  chmod 700 "$(dirname "$LOG")" 2>/dev/null || true
  jq -nc --arg ts "$ts" --arg ev "$EVENT" --arg ref "$session_ref" \
    --argjson present "${message_present:-false}" \
    '{ts:$ts, event:$ev}
     + (if $ref != "" then {session_ref:$ref} else {} end)
     + (if $present then {message_present:true} else {} end)' >> "$LOG" 2>/dev/null
  chmod 600 "$LOG" 2>/dev/null || true
fi

if [ "$EVENT" = "Notification" ] && [ -n "$APPROVAL" ] && [ ! -L "$APPROVAL" ]; then
  mkdir -p "$(dirname "$APPROVAL")" 2>/dev/null
  printf '%s\tattention-needed\n' "$ts" >> "$APPROVAL" 2>/dev/null
  chmod 600 "$APPROVAL" 2>/dev/null || true
fi

# Opt-in (CCC_NOTIFY_TELEGRAM=1): enqueue an owner-only Telegram push via the bridge spool.
# This hook NEVER touches the bot token — it writes a short, body-free notice that the
# bridge's PushNotifier delivers (token stays in the bridge). Disabled by default; best-effort.
if [ "${CCC_NOTIFY_TELEGRAM:-0}" = "1" ] && { [ "$EVENT" = "Notification" ] || [ "$EVENT" = "Stop" ]; }; then
  SPOOL="${CCC_PUSH_SPOOL:-${state_dir:+$state_dir/telegram-spool}}"
  if [ -n "$SPOOL" ] && [ ! -L "$SPOOL" ] && mkdir -p "$SPOOL" 2>/dev/null; then
    chmod 700 "$SPOOL" 2>/dev/null || true
    node="${CCC_NODE:-$(hostname -s 2>/dev/null || echo node)}"
    summary="Claude notification requires operator attention."
    [ "$EVENT" = "Stop" ] && summary="Claude session stopped or is waiting."
    dedup="$EVENT:${session_ref:-none}:$ts"
    tmp="$(mktemp "$SPOOL/.lifecycle-notice.XXXXXX" 2>/dev/null || true)"
    fname="$SPOOL/$(printf '%s' "$ts" | tr ':' '-')-${EVENT}-$$.json"
    if [ -n "$tmp" ] && jq -nc --arg ts "$ts" --arg ev "$EVENT" --arg node "$node" \
      --arg text "$summary" --arg dedup "$dedup" \
      '{ts:$ts, event:$ev, node:$node, text:$text, dedup:$dedup}' \
      > "$tmp" 2>/dev/null; then
      chmod 600 "$tmp" 2>/dev/null || true
      mv -f "$tmp" "$fname" 2>/dev/null || rm -f "$tmp"
    else
      [ -z "${tmp:-}" ] || rm -f "$tmp"
    fi
  fi
fi

# SessionEnd: archive the working-state checkpoint so it survives session exit.
if [ "$EVENT" = "SessionEnd" ]; then
  WS="${CCC_WORKING_STATE:-${state_dir:+$state_dir/working-state.md}}"
  ARCH_DIR="${CCC_SESSION_ARCHIVE:-${state_dir:+$state_dir/session-archive}}"
  if [ -n "$WS" ] && [ -n "$ARCH_DIR" ] && [ -f "$WS" ] && [ ! -L "$WS" ]; then
    mkdir -p "$ARCH_DIR" 2>/dev/null
    stamp="$(printf '%s' "$ts" | tr ':' '-')"
    cp "$WS" "$ARCH_DIR/working-state-${stamp}.md" 2>/dev/null
  fi
fi
exit 0
