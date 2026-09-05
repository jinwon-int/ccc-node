#!/usr/bin/env bash
# Tests for piri-session-normalize.py — projects a Piri (pi) session into the
# Claude transcript shape so the skill-autosave drafting brain (scan.sh,
# extract.sh, skill-review.sh) consumes piri sessions unmodified.
# Record shapes in the fixtures are copied from live piri session samples
# (session/message with text/thinking/toolCall content, toolResult messages).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NORM="$HERE/piri-session-normalize.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

normalize() { # <source> <out-dir> [extra args...] — echoes the summary JSON
  local src="$1" out="$2"; shift 2
  python3 "$NORM" "$src" --out-dir "$out" "$@"
}

# Fixture mirroring a live piri bridge session: session meta + user/assistant
# text turns + bash tool calls + thinking noise + non-command tools + a
# toolResult message + bookkeeping records + a malformed line.
FIX="$TMP/session-interactive.jsonl"
cat > "$FIX" <<'EOF'
{"type":"session","version":3,"id":"01a057f6-9ef1","timestamp":"2026-08-31T13:16:15.473Z","cwd":"/home/gongmyoung"}
{"type":"model_change","id":"m1","parentId":null,"timestamp":"2026-08-31T13:16:15.506Z","provider":"zai","modelId":"glm-5.3-flash"}
{"type":"thinking_level_change","id":"t1","parentId":"m1","timestamp":"2026-08-31T13:16:15.506Z","thinkingLevel":"max"}
{"type":"message","id":"u1","timestamp":"2026-08-31T13:16:15.521Z","message":{"role":"user","content":[{"type":"text","text":"디스크 상태 점검해줘"}]}}
{"type":"message","id":"a1","timestamp":"2026-08-31T13:16:16.000Z","message":{"role":"assistant","content":[{"type":"thinking","text":"df부터 본다"},{"type":"text","text":"상태부터 점검하겠습니다"}]}}
{"type":"message","id":"a2","timestamp":"2026-08-31T13:16:17.000Z","message":{"role":"assistant","content":[{"type":"toolCall","id":"c1","name":"bash","arguments":{"command":"df -h / && journalctl -p err -n 20"}}]}}
{"type":"message","id":"tr1","timestamp":"2026-08-31T13:16:18.000Z","message":{"role":"toolResult","content":[{"type":"text","text":"Filesystem columns"}]}}
{"type":"message","id":"a3","timestamp":"2026-08-31T13:16:19.000Z","message":{"role":"assistant","content":[{"type":"toolCall","id":"c2","name":"read","arguments":{"path":"/etc/os-release"}},{"type":"toolCall","id":"c3","name":"bash","arguments":{"command":"du -sh /var/log"}}]}}
{"type":"message","id":"a4","timestamp":"2026-08-31T13:16:20.000Z","message":{"role":"assistant","content":[{"type":"text","text":"로그가 큽니다"}]}}
this line is not json
EOF

OUT="$TMP/out"
summary="$(normalize "$FIX" "$OUT")"
printf '%s' "$summary" > "$TMP/summary.json"
proj="$OUT/-home-gongmyoung/01a057f6-9ef1.jsonl"

ok "summary reports session identity and outcome fields" \
  'jq -e ".session_id==\"01a057f6-9ef1\" and .cwd==\"/home/gongmyoung\" and .excluded==false and .truncated==false and .empty==false" "$TMP/summary.json" >/dev/null'
ok "projection lands in the encoded-cwd tree under the session id" '[ -f "$proj" ]'
ok "bash toolCalls become Claude Bash tool_use rows" \
  'grep -q "\"type\": \"tool_use\", \"name\": \"Bash\", \"input\": {\"command\": \"df -h / && journalctl -p err -n 20\"}" "$proj"'
ok "every bash command is projected (multiple per message)" \
  'grep -q "du -sh /var/log" "$proj"'
ok "user and assistant text turns are projected" \
  'grep -q "디스크 상태 점검해줘" "$proj" && grep -q "로그가 큽니다" "$proj"'
ok "thinking noise is discarded" '! grep -q "df부터 본다" "$proj"'
ok "toolResult messages are discarded" '! grep -q "Filesystem columns" "$proj"'
ok "non-command toolCalls are discarded" '! grep -q "os-release" "$proj"'
ok "bookkeeping records are discarded" '! grep -q "model_change" "$proj"'
ok "malformed lines are skipped" 'jq -e ".records_in == 10" "$TMP/summary.json" >/dev/null'

# Empty projection: a session with only bookkeeping/noise records.
FIX_EMPTY="$TMP/session-empty.jsonl"
cat > "$FIX_EMPTY" <<'EOF'
{"type":"session","version":3,"id":"emptysess","cwd":"/tmp"}
{"type":"model_change","id":"m1","parentId":null,"timestamp":"2026-08-31T13:16:15.506Z","provider":"zai","modelId":"glm-5.3-flash"}
{"type":"message","id":"a1","timestamp":"2026-08-31T13:16:16.000Z","message":{"role":"assistant","content":[{"type":"thinking","text":"pure reasoning"}]}}
EOF
OUT2="$TMP/out2"
summary2="$(normalize "$FIX_EMPTY" "$OUT2")"
printf '%s' "$summary2" > "$TMP/summary2.json"
ok "noise-only session reports empty and writes no rows" \
  'jq -e ".empty==true and .records_out==0" "$TMP/summary2.json" >/dev/null' \
  && ok "empty projection still writes an empty file (no partial state)" '[ -f "$OUT2/-tmp/emptysess.jsonl" ] && [ ! -s "$OUT2/-tmp/emptysess.jsonl" ]'

# Byte cap: a cap smaller than the first row reports truncation.
summary3="$(normalize "$FIX" "$TMP/out3" --max-bytes 200)"
printf '%s' "$summary3" > "$TMP/summary3.json"
ok "byte cap truncates and reports it" \
  'jq -e ".truncated==true and .records_out < 6" "$TMP/summary3.json" >/dev/null'

# Filename fallback for the session slug when no session record exists.
FIX_NOID="$TMP/session-nometa.jsonl"
printf '{"type":"message","id":"u1","message":{"role":"user","content":[{"type":"text","text":"hello"}]}}\n' > "$FIX_NOID"
summary4="$(normalize "$FIX_NOID" "$TMP/out4")"
printf '%s' "$summary4" > "$TMP/summary4.json"
ok "missing session record falls back to filename slug and _unknown cwd" \
  'jq -e ".session_id==\"\" and .project_enc==\"_unknown\"" "$TMP/summary4.json" >/dev/null && [ -f "$TMP/out4/_unknown/session-nometa.jsonl" ]'

# Usage/read errors.
normalize "$TMP/missing.jsonl" "$TMP/out5" >/dev/null 2>&1
ok "missing source exits 1" '[ "$?" = 1 ]'
normalize "$FIX" "$TMP/out6" --max-bytes 0 >/dev/null 2>&1
ok "invalid cap exits 2" '[ "$?" = 2 ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
