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
[ -n "$input" ] || exit 0
case "$EVENT" in
  Notification|Stop|SubagentStop|SessionEnd) ;;
  *) exit 0 ;;
esac
# A malformed payload is not a lifecycle event. Stay fail-open without creating
# a false approval marker or a synthetic notification.
canonical_input="$(printf '%s' "$input" | jq -cS 'select(type == "object")' 2>/dev/null)"
[ -n "$canonical_input" ] || exit 0
message_present="$(printf '%s' "$canonical_input" \
  | jq -r '[.message?, .notification?]
           | any(.[]; (type == "string") and test("\\S"))' 2>/dev/null)"
# Empty/unknown Notification objects are not operator attention. Stop and
# SessionEnd legitimately carry no message, so this gate is event-specific.
[ "$EVENT" != "Notification" ] || [ "$message_present" = "true" ] || exit 0
printf '%s' "$input" | bash "$HERE/lifecycle-feed.sh" "$EVENT" >/dev/null 2>&1 || true
state_dir="$(ccc_lifecycle_state_dir 2>/dev/null || true)"
LOG="${CCC_AUDIT_LOG:-${state_dir:+$state_dir/audit.jsonl}}"
APPROVAL="${CCC_APPROVAL_LOG:-${state_dir:+$state_dir/approval-needed.log}}"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
sid="$(printf '%s' "$canonical_input" | jq -r '.session_id // empty' 2>/dev/null)"
session_ref=""
if command -v ccc_lifecycle_ref >/dev/null 2>&1; then
  session_ref="$(ccc_lifecycle_ref "$sid" 2>/dev/null || true)"
fi
event_digest=""
if command -v ccc_lifecycle_digest >/dev/null 2>&1; then
  event_digest="$(ccc_lifecycle_digest "$EVENT:$canonical_input" 2>/dev/null || true)"
fi
dedup="${event_digest:+lifecycle:$EVENT:$event_digest}"

record="$(jq -nc --arg ts "$ts" --arg ev "$EVENT" --arg ref "$session_ref" \
    --arg dedup "$dedup" --argjson present "${message_present:-false}" \
    '{ts:$ts, event:$ev}
     + (if $ref != "" then {session_ref:$ref} else {} end)
     + (if $dedup != "" then {dedup:$dedup} else {} end)
     + (if $present then {message_present:true} else {} end)' 2>/dev/null || true)"
if [ -n "$record" ]; then
  if [ -n "$dedup" ] && command -v ccc_lifecycle_append_unique_line >/dev/null 2>&1; then
    ccc_lifecycle_append_unique_line "$LOG" "$record" "$dedup" || true
  elif command -v ccc_lifecycle_append_line >/dev/null 2>&1; then
    ccc_lifecycle_append_line "$LOG" "$record" || true
  fi
fi

if [ "$EVENT" = "Notification" ] \
   && [ -n "$dedup" ] \
   && command -v ccc_lifecycle_append_unique_line >/dev/null 2>&1; then
  ccc_lifecycle_append_unique_line \
    "$APPROVAL" "$ts	attention-needed	$dedup" "$dedup" || true
fi

# Opt-in (CCC_NOTIFY_TELEGRAM=1): enqueue an owner-only Telegram push via the bridge spool.
# This hook NEVER touches the bot token — it writes a short, body-free notice that the
# bridge's PushNotifier delivers (token stays in the bridge). Disabled by default; best-effort.
if [ "${CCC_NOTIFY_TELEGRAM:-0}" = "1" ] && { [ "$EVENT" = "Notification" ] || [ "$EVENT" = "Stop" ]; }; then
  SPOOL="${CCC_PUSH_SPOOL:-${state_dir:+$state_dir/telegram-spool}}"
  if [ -n "$event_digest" ] \
     && command -v ccc_lifecycle_prepare_private_dir >/dev/null 2>&1 \
     && ccc_lifecycle_prepare_private_dir "$SPOOL"; then
    node="${CCC_NODE:-$(hostname -s 2>/dev/null || echo node)}"
    summary="Claude notification requires operator attention."
    [ "$EVENT" = "Stop" ] && summary="Claude session stopped or is waiting."
    tmp="$(mktemp "$SPOOL/.lifecycle-notice.XXXXXX" 2>/dev/null || true)"
    fname="$SPOOL/lifecycle-${EVENT}-${event_digest:0:24}.json"
    if [ -n "$tmp" ] && jq -nc --arg ts "$ts" --arg ev "$EVENT" --arg node "$node" \
      --arg text "$summary" --arg dedup "$dedup" \
      '{ts:$ts, event:$ev, node:$node, text:$text, dedup:$dedup}' \
      > "$tmp" 2>/dev/null; then
      chmod 600 "$tmp" 2>/dev/null || true
      ln "$tmp" "$fname" 2>/dev/null || true
      rm -f "$tmp"
    else
      [ -z "${tmp:-}" ] || rm -f "$tmp"
    fi
  fi
fi

# SessionEnd: archive the working-state checkpoint so it survives session exit.
if [ "$EVENT" = "SessionEnd" ]; then
  WS="${CCC_WORKING_STATE:-${state_dir:+$state_dir/working-state.md}}"
  ARCH_DIR="${CCC_SESSION_ARCHIVE:-${state_dir:+$state_dir/session-archive}}"
  if [ -n "$event_digest" ] && [ -n "$WS" ] && [ -n "$ARCH_DIR" ] \
     && [ -f "$WS" ] && [ ! -L "$WS" ] && [ -O "$WS" ] \
     && command -v ccc_lifecycle_prepare_private_dir >/dev/null 2>&1 \
     && ccc_lifecycle_prepare_private_dir "$ARCH_DIR"; then
    tmp="$(mktemp "$ARCH_DIR/.working-state.XXXXXX" 2>/dev/null || true)"
    fname="$ARCH_DIR/working-state-${event_digest:0:24}.md"
    if [ -n "$tmp" ] && cp "$WS" "$tmp" 2>/dev/null; then
      chmod 600 "$tmp" 2>/dev/null || true
      ln "$tmp" "$fname" 2>/dev/null || true
      rm -f "$tmp"
    else
      [ -z "${tmp:-}" ] || rm -f "$tmp"
    fi
  fi
fi
exit 0
