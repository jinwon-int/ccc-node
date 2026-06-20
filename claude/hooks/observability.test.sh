#!/usr/bin/env bash
# Tests for Tier 1.5 observability hooks: audit.sh, redact.sh, notify.sh.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
pass=0; fail=0
TMP="$(mktemp -d)"
export CCC_AUDIT_LOG="$TMP/audit.jsonl"
export CCC_APPROVAL_LOG="$TMP/approval.log"
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# --- audit.sh: records mutating tools, skips read-only, redacts secrets ---
echo '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}' | bash "$HERE/audit.sh"
ok "audit records Bash"            'grep -q "\"tool\":\"Bash\"" "$CCC_AUDIT_LOG"'

echo '{"tool_name":"Read","tool_input":{"file_path":"/x"}}' | bash "$HERE/audit.sh"
ok "audit skips Read"              '[ "$(grep -c Read "$CCC_AUDIT_LOG")" = "0" ]'

echo '{"tool_name":"Bash","tool_input":{"command":"deploy --token=ghp_ABCDEF1234567890abcdef"}}' | bash "$HERE/audit.sh"
ok "audit redacts ghp token"       'grep -q "<redacted>" "$CCC_AUDIT_LOG" && ! grep -q "ABCDEF1234567890abcdef" "$CCC_AUDIT_LOG"'

echo '{"tool_name":"Bash","tool_input":{"command":"curl -H \"authorization: Bearer sk-abcdefghijklmnop1234\""}}' | bash "$HERE/audit.sh"
ok "audit redacts bearer/sk"       '! grep -q "abcdefghijklmnop1234" "$CCC_AUDIT_LOG"'

echo '{"tool_name":"Write","tool_input":{"file_path":"/opt/x/foo.md"}}' | bash "$HERE/audit.sh"
ok "audit records Write file_path" 'grep -q "foo.md" "$CCC_AUDIT_LOG"'

# --- redact.sh: warns on raw credential in prompt, silent otherwise ---
out="$(echo '{"prompt":"please use ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 to auth"}' | bash "$HERE/redact.sh")"
ok "redact warns on token"         'grep -q "raw credential" <<<"$out"'

out="$(echo '{"prompt":"normal request, refactor the parser"}' | bash "$HERE/redact.sh")"
ok "redact silent on clean prompt" '[ -z "$out" ]'

# --- notify.sh: records event + approval marker on Notification ---
echo '{"message":"Claude needs your permission"}' | bash "$HERE/notify.sh" Notification
ok "notify logs Notification"      'grep -q "\"event\":\"Notification\"" "$CCC_AUDIT_LOG"'
ok "notify writes approval marker" 'grep -q "permission" "$CCC_APPROVAL_LOG"'

echo '{}' | bash "$HERE/notify.sh" Stop
ok "notify logs Stop"              'grep -q "\"event\":\"Stop\"" "$CCC_AUDIT_LOG"'

# --- guard.sh: denial writes approval-needed marker (label only, no raw cmd) ---
echo '{"tool_name":"Bash","tool_input":{"command":"git push --force origin main"}}' | bash "$HERE/guard.sh" >/dev/null 2>&1
ok "guard logs denial label"       'grep -q "DENY\[force-push\]" "$CCC_APPROVAL_LOG"'
ok "guard denial omits raw cmd"    '! grep -q "force origin main" "$CCC_APPROVAL_LOG"'

rm -rf "$TMP"
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = "0" ]
