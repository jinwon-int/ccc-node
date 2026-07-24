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

# --- audit.sh: records body-free mutation metadata, skips read-only ---
echo '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}' | bash "$HERE/audit.sh"
ok "audit records Bash"            'grep -q "\"tool\":\"Bash\"" "$CCC_AUDIT_LOG"'

echo '{"tool_name":"Read","tool_input":{"file_path":"/x"}}' | bash "$HERE/audit.sh"
ok "audit skips Read"              '[ "$(grep -c Read "$CCC_AUDIT_LOG")" = "0" ]'

printf '{"tool_name":"Bash","tool_input":{"command":"deploy --token=%s"}}\n' "$fake_github_token" | bash "$HERE/audit.sh"
ok "audit stores no raw command/token" \
  '! grep -Fq "$fake_github_token" "$CCC_AUDIT_LOG" && ! jq -e "has(\"command\")" "$CCC_AUDIT_LOG" >/dev/null'

echo '{"tool_name":"Bash","tool_input":{"command":"curl -H \"authorization: Bearer sk-abcdefghijklmnop1234\""}}' | bash "$HERE/audit.sh"
ok "audit stores no bearer/sk"     '! grep -q "abcdefghijklmnop1234" "$CCC_AUDIT_LOG"'

echo '{"tool_name":"Write","tool_input":{"file_path":"/opt/x/foo.md"}}' | bash "$HERE/audit.sh"
ok "audit reduces Write path to shape" \
  '! grep -q "foo.md" "$CCC_AUDIT_LOG" && jq -e "select(.tool==\"Write\") | .target_shape==\"file\" and .file_change==true" "$CCC_AUDIT_LOG" >/dev/null'

# --- redact.sh: warns on raw credential in prompt, silent otherwise ---
out="$(printf '{"prompt":"please use %s to auth"}\n' "$fake_github_token" | bash "$HERE/redact.sh")"
ok "redact warns on token"         'grep -q "raw credential" <<<"$out"'

out="$(echo '{"prompt":"normal request, refactor the parser"}' | bash "$HERE/redact.sh")"
ok "redact silent on clean prompt" '[ -z "$out" ]'

# --- notify.sh: records event + approval marker on Notification ---
echo '{"message":"Claude needs your permission"}' | bash "$HERE/notify.sh" Notification
ok "notify logs Notification"      'grep -q "\"event\":\"Notification\"" "$CCC_AUDIT_LOG"'
ok "notify writes body-free approval marker" \
  'grep -q "attention-needed" "$CCC_APPROVAL_LOG" && ! grep -q "permission" "$CCC_APPROVAL_LOG"'
ok "notify stores no raw message" \
  '! grep -q "Claude needs your permission" "$CCC_AUDIT_LOG" "$CCC_APPROVAL_LOG"'

echo '{}' | bash "$HERE/notify.sh" Stop
ok "notify logs Stop"              'grep -q "\"event\":\"Stop\"" "$CCC_AUDIT_LOG"'

# --- Telegram push spool: OFF by default, opt-in writes a redacted owner-only summary ---
echo '{"message":"needs permission"}' | CCC_PUSH_SPOOL="$TMP/spool" bash "$HERE/notify.sh" Notification
ok "push spool off by default"     '[ ! -d "$TMP/spool" ]'

printf '{"message":"approve %s now"}\n' "$fake_github_token" \
  | CCC_NOTIFY_TELEGRAM=1 CCC_NODE=testnode CCC_PUSH_SPOOL="$TMP/spool" bash "$HERE/notify.sh" Notification
ok "push spool writes when opt-in"  'ls "$TMP/spool"/*.json >/dev/null 2>&1'
ok "push spool is body-free" \
  '! grep -Fq "$fake_github_token" "$TMP/spool"/*.json && jq -e ".text == \"Claude notification requires operator attention.\"" "$TMP/spool"/*.json >/dev/null'
ok "push spool carries node label"  'cat "$TMP/spool"/*.json | grep -q "testnode"'
spool_count="$(find "$TMP/spool" -maxdepth 1 -type f -name '*.json' | wc -l | tr -d '[:space:]')"
audit_count_before_retry="$(wc -l < "$CCC_AUDIT_LOG" | tr -d '[:space:]')"
approval_count_before_retry="$(wc -l < "$CCC_APPROVAL_LOG" | tr -d '[:space:]')"
printf '{  "message" : "approve %s now" }\n' "$fake_github_token" \
  | CCC_NOTIFY_TELEGRAM=1 CCC_NODE=testnode CCC_PUSH_SPOOL="$TMP/spool" bash "$HERE/notify.sh" Notification
ok "push spool dedups a semantically identical retry" \
  '[ "$(find "$TMP/spool" -maxdepth 1 -type f -name "*.json" | wc -l | tr -d "[:space:]")" = "$spool_count" ]'
ok "push spool dedup is payload-stable" \
  'jq -e ".dedup | startswith(\"lifecycle:Notification:\")" "$TMP/spool"/*.json >/dev/null'
ok "notification audit and approval logs dedup the retry" \
  '[ "$(wc -l < "$CCC_AUDIT_LOG" | tr -d "[:space:]")" = "$audit_count_before_retry" ] && [ "$(wc -l < "$CCC_APPROVAL_LOG" | tr -d "[:space:]")" = "$approval_count_before_retry" ]'

# SessionEnd archives the working-state file
export CCC_WORKING_STATE="$TMP/ws.md"; export CCC_SESSION_ARCHIVE="$TMP/arch"
printf 'objective: test\n' > "$CCC_WORKING_STATE"
echo '{}' | bash "$HERE/notify.sh" SessionEnd
ok "SessionEnd archives ws"        'ls "$TMP/arch"/working-state-*.md >/dev/null 2>&1'
echo '{}' | bash "$HERE/notify.sh" SessionEnd
ok "SessionEnd archive is atomic and retry-deduped" \
  '[ "$(find "$TMP/arch" -maxdepth 1 -type f -name "working-state-*.md" | wc -l | tr -d "[:space:]")" = 1 ] && [ "$(stat -c "%a" "$TMP/arch"/working-state-*.md)" = 600 ]'

# --- audit.sh stores an opaque session_ref; evidence-gate.sh still scopes by it ---
echo '{"session_id":"sX","tool_name":"Write","tool_input":{"file_path":"/x/a.py"}}' | bash "$HERE/audit.sh"
ok "audit hashes session_id" \
  '! grep -q "\"session_id\":\"sX\"" "$CCC_AUDIT_LOG" && jq -e "select(.tool==\"Write\") | (.session_ref | length)==16" "$CCC_AUDIT_LOG" >/dev/null'

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

# --- actual Bash hook -> canonical lifecycle CLI feed, opt-in and fail-open ---
fake_python="$TMP/fake-python"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$*" > "$CCC_LIFECYCLE_CAPTURE.args"' \
  'cat > "$CCC_LIFECYCLE_CAPTURE.stdin"' \
  'exit 0' > "$fake_python"
chmod +x "$fake_python"
export CCC_LIFECYCLE_CAPTURE="$TMP/lifecycle-capture"
rm -f "$CCC_LIFECYCLE_CAPTURE.args" "$CCC_LIFECYCLE_CAPTURE.stdin"
echo '{"session_id":"feed-off","tool_name":"Write","tool_input":{"file_path":"/secret/path"}}' \
  | CCC_LIFECYCLE_PYTHON="$fake_python" bash "$HERE/audit.sh"
ok "lifecycle feed is disabled by default" '[ ! -e "$CCC_LIFECYCLE_CAPTURE.args" ]'

payload='{"session_id":"feed-on","tool_name":"Write","tool_input":{"file_path":"/secret/path"}}'
printf '%s' "$payload" \
  | CCC_LIFECYCLE_AUDIT=1 CCC_LIFECYCLE_PYTHON="$fake_python" bash "$HERE/audit.sh"
ok "audit hook invokes canonical lifecycle module" \
  'grep -Fq -- "-m telegram_bot.core.lifecycle_hook PostToolUse" "$CCC_LIFECYCLE_CAPTURE.args"'
ok "audit hook forwards exact payload only to CLI stdin" \
  '[ "$(cat "$CCC_LIFECYCLE_CAPTURE.stdin")" = "$payload" ]'

rm -f "$CCC_LIFECYCLE_CAPTURE.args" "$CCC_LIFECYCLE_CAPTURE.stdin"
printf '%s' '{"message":"sensitive provider body"}' \
  | CCC_LIFECYCLE_AUDIT=1 CCC_LIFECYCLE_PYTHON="$fake_python" bash "$HERE/notify.sh" Notification
ok "notification hook invokes canonical lifecycle module" \
  'grep -Fq -- "-m telegram_bot.core.lifecycle_hook Notification" "$CCC_LIFECYCLE_CAPTURE.args"'
ok "notification body never reaches legacy files" \
  '! grep -R -Fq "sensitive provider body" "$CCC_AUDIT_LOG" "$CCC_APPROVAL_LOG" "$TMP/spool"'

audit_count="$(wc -l < "$CCC_AUDIT_LOG" | tr -d '[:space:]')"
approval_count="$(wc -l < "$CCC_APPROVAL_LOG" | tr -d '[:space:]')"
printf 'not-json' | bash "$HERE/notify.sh" Notification
printf '{}' | bash "$HERE/notify.sh" '../../invalid'
printf '{}' | bash "$HERE/notify.sh" Notification
printf '{"message":"   "}' | bash "$HERE/notify.sh" Notification
printf '{"message":123}' | bash "$HERE/notify.sh" Notification
printf '{"message":{}}' | bash "$HERE/notify.sh" Notification
ok "malformed/unknown notifications create no false attention" \
  '[ "$(wc -l < "$CCC_AUDIT_LOG" | tr -d "[:space:]")" = "$audit_count" ] && [ "$(wc -l < "$CCC_APPROVAL_LOG" | tr -d "[:space:]")" = "$approval_count" ]'

printf '{"message":"   ","notification":"fallback attention"}' \
  | bash "$HERE/notify.sh" Notification
ok "notification falls back to a valid alternate string" \
  '[ "$(wc -l < "$CCC_AUDIT_LOG" | tr -d "[:space:]")" = $((audit_count + 1)) ] && [ "$(wc -l < "$CCC_APPROVAL_LOG" | tr -d "[:space:]")" = $((approval_count + 1)) ]'

# Refuse direct symlink targets and survive environments without HOME/state.
printf 'sentinel\n' > "$TMP/external-audit"
ln -s "$TMP/external-audit" "$TMP/audit-link"
printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"/x"}}' \
  | CCC_AUDIT_LOG="$TMP/audit-link" bash "$HERE/audit.sh"
ok "audit refuses a symlink target" \
  '[ "$(cat "$TMP/external-audit")" = "sentinel" ]'
mkdir "$TMP/external-parent"
ln -s "$TMP/external-parent" "$TMP/audit-parent-link"
printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"/x"}}' \
  | CCC_AUDIT_LOG="$TMP/audit-parent-link/audit.jsonl" bash "$HERE/audit.sh"
ok "audit refuses a symlink parent" '[ ! -e "$TMP/external-parent/audit.jsonl" ]'
mkdir "$TMP/nested-root" "$TMP/nested-target"
ln -s "$TMP/nested-target" "$TMP/nested-root/link"
printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"/x"}}' \
  | CCC_AUDIT_LOG="$TMP/nested-root/link/state/audit.jsonl" bash "$HERE/audit.sh"
ok "audit refuses a symlink in any parent component" \
  '[ ! -e "$TMP/nested-target/state/audit.jsonl" ]'
mkdir "$TMP/shared-parent"
chmod 755 "$TMP/shared-parent"
printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"/x"}}' \
  | CCC_AUDIT_LOG="$TMP/shared-parent/audit.jsonl" bash "$HERE/audit.sh"
ok "audit refuses and never chmods a non-private override parent" \
  '[ ! -e "$TMP/shared-parent/audit.jsonl" ] && [ "$(stat -c "%a" "$TMP/shared-parent")" = 755 ]'

env -u HOME -u CCC_STATE_DIR -u CCC_AUDIT_LOG \
  bash "$HERE/notify.sh" Notification <<< '{"message":"no-home-body"}'
rc=$?
ok "notify is fail-open without HOME/state" '[ "$rc" = 0 ]'

rm -rf "$TMP"
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = "0" ]
