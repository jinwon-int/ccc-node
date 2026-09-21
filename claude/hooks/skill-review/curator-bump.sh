#!/usr/bin/env bash
# PostToolUse(Skill) → curator telemetry bump (#752).
# Contract: ALWAYS exit 0. Telemetry must never block/delay a foreground
# skill invocation — any failure is swallowed after a best-effort bump.
#
# #1675: swallowing the failure is right; swallowing the *reason* was not.
# Every exit below used to be a bare `exit 0`, so a broken telemetry path and
# a genuinely unused skill produced the same observable state — an empty
# ledger — which is precisely the distinction the retirement audit (#1648)
# depends on. Each silent exit now leaves one bounded line behind. The
# foreground contract is unchanged: still never blocks, still always exit 0.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
CURATOR="$HERE/curator.py"
STATE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}/state/skill-usage"
DEGRADED_LOG="$STATE_DIR/degraded.log"
MAX_DEGRADED_BYTES=$((256 * 1024))

# Bounded, owner-only, best-effort. Never the reason a skill invocation fails.
note_degraded() { # note_degraded <reason> [skill_name]
  # Create the state path owner-only on first touch: ownership.py's contract
  # check rejects group/other bits on *any* path component, so a world-readable
  # `state` intermediate poisons every later recording bump into a permanent
  # fail-open. `mkdir -p -m 700` modes the leaf alone -- intermediates follow
  # umask -- hence a 077 umask in a subshell makes each fresh component 0700.
  # Pre-existing wrong modes are left alone and surface as a
  # curator:contract:unsafe_state_directory line below.
  ( umask 077 && mkdir -p "$STATE_DIR" ) 2>/dev/null || return 0
  # Size cap before append: telemetry must not be able to fill a disk.
  if [ -f "$DEGRADED_LOG" ]; then
    size="$(wc -c <"$DEGRADED_LOG" 2>/dev/null || echo 0)"
    [ "$size" -lt "$MAX_DEGRADED_BYTES" ] 2>/dev/null || return 0
  fi
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" || return 0
  # #1866: the two post-extraction call sites also know which skill degraded.
  # The name bypasses ownership._validate_name here, so it is sanitized
  # before logging: newlines folded to spaces (one line per degrade) and the
  # field capped at 64 bytes. Lines written without a name keep the original
  # two-field format, so existing parsers stay compatible either way.
  local skill=""
  if [ "$#" -ge 2 ] && [ -n "$2" ]; then
    skill="$(printf '%s' "$2" | tr '\r\n' '  ' | cut -c1-64)"
  fi
  printf '%s bump %s%s\n' "$ts" "$1" "${skill:+ $skill}" >>"$DEGRADED_LOG" 2>/dev/null || return 0
  chmod 600 "$DEGRADED_LOG" 2>/dev/null || true
  return 0
}

payload="$(cat 2>/dev/null)" || exit 0
# An empty payload is the hook being invoked outside a tool call, not a
# failure — the only silent exit that stays silent.
[ -n "$payload" ] || exit 0

command -v jq >/dev/null 2>&1 || { note_degraded "wrapper:jq_missing"; exit 0; }
command -v python3 >/dev/null 2>&1 || { note_degraded "wrapper:python3_missing"; exit 0; }
[ -f "$CURATOR" ] || { note_degraded "wrapper:curator_missing"; exit 0; }

name="$(printf '%s' "$payload" | jq -r '.tool_input.skill // empty' 2>/dev/null)" \
  || { note_degraded "wrapper:payload_unparsable"; exit 0; }
[ -n "$name" ] || { note_degraded "wrapper:skill_name_absent"; exit 0; }

out="$(python3 "$CURATOR" bump --event use --name "$name" 2>/dev/null)" || {
  note_degraded "wrapper:curator_nonzero" "$name"
  exit 0
}
# curator is fail-open: a non-recording bump still exits 0 and says so.
case "$out" in
  *'"recorded": true'*) : ;;
  *)
    reason="$(printf '%s' "$out" | jq -r '.reason // "unreported"' 2>/dev/null)" || reason="unreported"
    note_degraded "curator:${reason}" "$name"
    ;;
esac
exit 0
