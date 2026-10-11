#!/usr/bin/env bash
# Tests for claude/hooks/tool-streak.sh — PostToolUse identical-call streak
# reminder (#2261). Hermetic: private CCC_STATE_DIR; asserts the one-warning-
# per-streak contract, the reset on a different call, the Read exclusion, the
# hash-only state (no raw session id / command persisted) and fail-open exits.
# shellcheck disable=SC2034  # rc/out*/STATE are read via eval inside ok()
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/tool-streak.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0
fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

export HOME="$TMP/home"
export CCC_STATE_DIR="$TMP/state"
mkdir -p "$HOME"
unset CCC_TOOL_STREAK CCC_TOOL_STREAK_LIMIT
STATE="$CCC_STATE_DIR/tool-streak"

run_hook() { printf '%s' "$1" | bash "$HOOK"; }
bash_call() { # bash_call <session> <command>
  jq -nc --arg s "$1" --arg c "$2" '{tool_name:"Bash", session_id:$s, tool_input:{command:$c}}'
}

SID="session-raw-id-ABC123"
CMD="pytest -q tests/test_x.py"

rc=0; out1="$(run_hook "$(bash_call "$SID" "$CMD")")" || rc=$?
out2="$(run_hook "$(bash_call "$SID" "$CMD")")"
ok "first two identical calls stay silent and exit 0" '[ "$rc" = 0 ] && [ -z "$out1" ] && [ -z "$out2" ]'

out3="$(run_hook "$(bash_call "$SID" "$CMD")")"
ok "third identical call emits one PostToolUse additionalContext naming the count" \
  'jq -e ".hookSpecificOutput.hookEventName == \"PostToolUse\" and (.hookSpecificOutput.additionalContext | contains(\"Bash call was issued 3 times\"))" <<<"$out3" >/dev/null'

out4="$(run_hook "$(bash_call "$SID" "$CMD")")"
ok "the same streak does not warn again on the 4th call" '[ -z "$out4" ]'

out5="$(run_hook "$(bash_call "$SID" "ls -la")")"
ok "a different call resets the streak silently" '[ -z "$out5" ]'
run_hook "$(bash_call "$SID" "$CMD")" >/dev/null
run_hook "$(bash_call "$SID" "$CMD")" >/dev/null
out6="$(run_hook "$(bash_call "$SID" "$CMD")")"
ok "after a reset a new streak of 3 warns again" \
  'jq -e ".hookSpecificOutput.additionalContext | contains(\"3 times\")" <<<"$out6" >/dev/null'

# --- state hygiene ----------------------------------------------------------------
ok "state dir is owner-only and holds one file per session" \
  '[ "$(stat -c %a "$STATE")" = 700 ] && [ "$(find "$STATE" -maxdepth 1 -type f | wc -l)" = 1 ]'
ok "no raw session id, command or tool input is persisted — hashes and counters only" \
  '! grep -rq "ABC123\|pytest\|ls -la" "$STATE" && grep -Eq "^[0-9a-f]{32} [0-9]+ [01]$" "$STATE"/*'
ok "state file name is a session hash, not the session id" \
  '! ls "$STATE" | grep -q "ABC123"'

# --- isolation, exclusions, switches ----------------------------------------------
out="$(run_hook "$(bash_call "other-session" "$CMD")")"
ok "another session starts its own count (no warning on its first call)" '[ -z "$out" ]'

read_call="$(jq -nc --arg s "$SID" '{tool_name:"Read", session_id:$s, tool_input:{file_path:"/x/y.py"}}')"
run_hook "$read_call" >/dev/null; run_hook "$read_call" >/dev/null; out="$(run_hook "$read_call")"
ok "Read is never counted (re-reading is normal)" '[ -z "$out" ]'

edit_call="$(jq -nc --arg s "$SID" '{tool_name:"Edit", session_id:$s, tool_input:{file_path:"/x/y.py", old_string:"a", new_string:"b"}}')"
run_hook "$edit_call" >/dev/null; run_hook "$edit_call" >/dev/null; out="$(run_hook "$edit_call")"
ok "identical Edit calls are counted like Bash" \
  'jq -e ".hookSpecificOutput.additionalContext | contains(\"Edit call was issued 3 times\")" <<<"$out" >/dev/null'

edit_call2="$(jq -nc --arg s "$SID" '{tool_name:"Edit", session_id:$s, tool_input:{file_path:"/x/y.py", old_string:"a", new_string:"c"}}')"
run_hook "$edit_call2" >/dev/null; run_hook "$edit_call2" >/dev/null
run_hook "$edit_call" >/dev/null; run_hook "$edit_call" >/dev/null; out="$(run_hook "$edit_call")"
ok "any difference in tool_input breaks the streak (alternating edits warn only when 3 identical in a row)" \
  '[ -n "$out" ]'

rc=0; out="$(export CCC_TOOL_STREAK_LIMIT=2; run_hook "$(bash_call "lim2" "x")"; run_hook "$(bash_call "lim2" "x")")" || rc=$?
ok "CCC_TOOL_STREAK_LIMIT=2 warns on the second identical call" \
  '[ "$rc" = 0 ] && grep -q "2 times" <<<"$out"'

rc=0; out="$(CCC_TOOL_STREAK=0 run_hook "$(bash_call "$SID" "$CMD")"; CCC_TOOL_STREAK=0 run_hook "$(bash_call "$SID" "$CMD")"; CCC_TOOL_STREAK=0 run_hook "$(bash_call "$SID" "$CMD")")" || rc=$?
ok "CCC_TOOL_STREAK=0 disables the hook" '[ "$rc" = 0 ] && [ -z "$out" ]'

rc=0; out="$(printf 'not json' | bash "$HOOK")" || rc=$?
ok "malformed stdin is fail-open" '[ "$rc" = 0 ] && [ -z "$out" ]'
rc=0; out="$(run_hook "$(jq -nc '{tool_name:"Bash", tool_input:{command:"x"}}')")" || rc=$?
ok "a call without session_id is ignored" '[ "$rc" = 0 ] && [ -z "$out" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
