#!/usr/bin/env bash
# Tests for Tier 1.5 observability hooks: audit.sh, redact.sh, notify.sh.
# shellcheck disable=SC2034  # `out` is consumed via eval inside ok()
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
pass=0; fail=0
TMP="$(mktemp -d)"
export CCC_AUDIT_LOG="$TMP/audit.jsonl"
fake_github_token="ghp_""12345678901234567890"
export CCC_APPROVAL_LOG="$TMP/approval.log"
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# --- audit.sh: records mutating tools, skips read-only, redacts secrets ---
echo '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}' | bash "$HERE/audit.sh"
ok "audit records Bash"            'grep -q "\"tool\":\"Bash\"" "$CCC_AUDIT_LOG"'

echo '{"tool_name":"Read","tool_input":{"file_path":"/x"}}' | bash "$HERE/audit.sh"
ok "audit skips Read"              '[ "$(grep -c Read "$CCC_AUDIT_LOG")" = "0" ]'

printf '{"tool_name":"Bash","tool_input":{"command":"deploy --token=%s"}}\n' "$fake_github_token" | bash "$HERE/audit.sh"
ok "audit redacts ghp token"       'grep -q "<redacted>" "$CCC_AUDIT_LOG" && ! grep -q "ABCDEF1234567890abcdef" "$CCC_AUDIT_LOG"'

echo '{"tool_name":"Bash","tool_input":{"command":"curl -H \"authorization: Bearer sk-abcdefghijklmnop1234\""}}' | bash "$HERE/audit.sh"
ok "audit redacts bearer/sk"       '! grep -q "abcdefghijklmnop1234" "$CCC_AUDIT_LOG"'

echo '{"tool_name":"Write","tool_input":{"file_path":"/opt/x/foo.md"}}' | bash "$HERE/audit.sh"
ok "audit records Write file_path" 'grep -q "foo.md" "$CCC_AUDIT_LOG"'

# --- redact.sh: warns on raw credential in prompt, silent otherwise ---
out="$(printf '{"prompt":"please use %s to auth"}\n' "$fake_github_token" | bash "$HERE/redact.sh")"
ok "redact warns on token"         'grep -q "raw credential" <<<"$out"'

out="$(echo '{"prompt":"normal request, refactor the parser"}' | bash "$HERE/redact.sh")"
ok "redact silent on clean prompt" '[ -z "$out" ]'

# --- notify.sh: records event + approval marker on Notification ---
echo '{"message":"Claude needs your permission"}' | bash "$HERE/notify.sh" Notification
ok "notify logs Notification"      'grep -q "\"event\":\"Notification\"" "$CCC_AUDIT_LOG"'
ok "notify writes approval marker" 'grep -q "permission" "$CCC_APPROVAL_LOG"'

echo '{}' | bash "$HERE/notify.sh" Stop
ok "notify logs Stop"              'grep -q "\"event\":\"Stop\"" "$CCC_AUDIT_LOG"'

# --- Telegram push spool: OFF by default, opt-in writes a redacted owner-only summary ---
echo '{"message":"needs permission"}' | CCC_PUSH_SPOOL="$TMP/spool" bash "$HERE/notify.sh" Notification
ok "push spool off by default"     '[ ! -d "$TMP/spool" ]'

printf '{"message":"approve %s now"}\n' "$fake_github_token" \
  | CCC_NOTIFY_TELEGRAM=1 CCC_NODE=testnode CCC_PUSH_SPOOL="$TMP/spool" bash "$HERE/notify.sh" Notification
ok "push spool writes when opt-in"  'ls "$TMP/spool"/*.json >/dev/null 2>&1'
ok "push spool redacts token"       'cat "$TMP/spool"/*.json | grep -q "\\[REDACTED\\]" && ! cat "$TMP/spool"/*.json | grep -q "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"'
ok "push spool carries node label"  'cat "$TMP/spool"/*.json | grep -q "testnode"'

# SessionEnd archives the working-state file
export CCC_WORKING_STATE="$TMP/ws.md"; export CCC_SESSION_ARCHIVE="$TMP/arch"
printf 'objective: test\n' > "$CCC_WORKING_STATE"
echo '{}' | bash "$HERE/notify.sh" SessionEnd
ok "SessionEnd archives ws"        'ls "$TMP/arch"/working-state-*.md >/dev/null 2>&1'

# --- audit.sh records session_id; evidence-gate.sh (Stop) uses it ---
echo '{"session_id":"sX","tool_name":"Write","tool_input":{"file_path":"/x/a.py"}}' | bash "$HERE/audit.sh"
ok "audit records session_id"      'grep -q "\"session_id\":\"sX\"" "$CCC_AUDIT_LOG"'

out="$(echo '{"session_id":"sX"}' | bash "$HERE/evidence-gate.sh")"
ok "evidence gate off by default"  '[ -z "$out" ]'

out="$(echo '{"session_id":"sX"}' | CCC_EVIDENCE_GATE=1 bash "$HERE/evidence-gate.sh")"
ok "evidence gate blocks unverified change" 'grep -q "\"decision\":\"block\"" <<<"$out"'

out="$(echo '{"session_id":"sX","stop_hook_active":true}' | CCC_EVIDENCE_GATE=1 bash "$HERE/evidence-gate.sh")"
ok "evidence gate passes when already active" '[ -z "$out" ]'

out="$(echo '{"session_id":"sOther"}' | CCC_EVIDENCE_GATE=1 bash "$HERE/evidence-gate.sh")"
ok "evidence gate ignores other sessions" '[ -z "$out" ]'

echo '{"session_id":"sX","tool_name":"Bash","tool_input":{"command":"git diff --stat"}}' | bash "$HERE/audit.sh"
out="$(echo '{"session_id":"sX"}' | CCC_EVIDENCE_GATE=1 bash "$HERE/evidence-gate.sh")"
ok "evidence gate passes with verification" '[ -z "$out" ]'

rm -rf "$TMP"
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = "0" ]
