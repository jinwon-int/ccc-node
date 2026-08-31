#!/usr/bin/env bash
# Tests for codex-rollout-normalize.py (#1353) — projects a Codex rollout into
# the Claude transcript shape so the skill-autosave drafting brain (scan.sh,
# extract.sh, skill-review.sh) consumes codex sessions unmodified.
# Record shapes in the fixtures are copied from live rollout samples
# (session_meta/response_item/function_call/event_msg/turn_context/token_count).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NORM="$HERE/codex-rollout-normalize.py"
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

# Fixture mirroring a live interactive rollout: meta + injected developer
# noise + real user/assistant messages + shell/exec_command/apply_patch calls
# + non-command tools + discarded bookkeeping records.
FIX="$TMP/rollout-interactive.jsonl"
cat > "$FIX" <<'EOF'
{"timestamp":"2026-08-28T12:05:28.425Z","type":"session_meta","payload":{"session_id":"01a04842-bbf3","id":"01a04842-bbf3","cwd":"/root/projects/my app","originator":"codex_cli","base_instructions":{"text":"SEVERAL KB OF DIRECTIVES"}}}
{"timestamp":"2026-08-28T12:05:28.430Z","type":"event_msg","payload":{"type":"task_started","turn_id":"t1"}}
{"timestamp":"2026-08-28T12:05:32.744Z","type":"response_item","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"<skills_instructions> injected noise"}]}}
{"timestamp":"2026-08-28T12:05:32.745Z","type":"event_msg","payload":{"type":"user_message","message":"fallback user msg must not duplicate"}}
{"timestamp":"2026-08-28T12:05:32.745Z","type":"event_msg","payload":{"type":"agent_message","message":"fallback agent msg must not duplicate"}}
{"timestamp":"2026-08-28T12:05:39.085Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"배포 스크립트 점검해줘"}]}}
{"timestamp":"2026-08-28T12:05:39.085Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"systemctl 상태부터 점검하겠습니다"}]}}
{"timestamp":"2026-08-28T12:07:13.997Z","type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"{\"command\":[\"bash\",\"-lc\",\"systemctl status myapp && journalctl -u myapp -n 50\"]}"}}
{"timestamp":"2026-08-28T12:07:20.000Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"git status --short\",\"timeout\":10}"}}
{"timestamp":"2026-08-28T12:07:25.000Z","type":"response_item","payload":{"type":"function_call","name":"apply_patch","arguments":"{\"input\":\"*** Begin Patch\\n*** Update File: a.txt\"}"}}
{"timestamp":"2026-08-28T12:07:30.000Z","type":"response_item","payload":{"type":"function_call","name":"wait","arguments":"{\"cell_id\":\"8\"}"}}
{"timestamp":"2026-08-28T12:07:31.000Z","type":"response_item","payload":{"type":"reasoning","summary":"noise"}}
{"timestamp":"2026-08-28T12:07:32.000Z","type":"turn_context","payload":{"cwd":"/root/projects/my app","model":"gpt-5.6-sol"}}
{"timestamp":"2026-08-28T12:07:33.000Z","type":"token_count","payload":{"info":{"total_token_usage":{"input_tokens":1}}}}
EOF

OUT="$TMP/out"
summary="$(normalize "$FIX" "$OUT")"
printf '%s' "$summary" > "$TMP/summary.json"
proj="$OUT/-root-projects-my-app/01a04842-bbf3.jsonl"

ok "summary reports session identity and non-exclusion" \
  'jq -e ".session_id==\"01a04842-bbf3\" and .originator==\"codex_cli\" and .excluded==false and .truncated==false" "$TMP/summary.json" >/dev/null'
ok "cwd encodes like skill-review.sh encode_project_dir (non-alnum → -)" \
  'jq -e ".project_enc==\"-root-projects-my-app\"" "$TMP/summary.json" >/dev/null'
ok "projected file lands at <out>/<encoded-cwd>/<session-id>.jsonl" '[ -f "$proj" ]'
ok "injected developer noise and bookkeeping records are discarded" \
  '[ "$(grep -c "skills_instructions\|task_started\|token_count\|turn_context\|reasoning\|SEVERAL KB" "$proj")" = 0 ]'
ok "event_msg rows are NOT emitted when response_item messages exist (no duplicate turns)" \
  '[ "$(grep -c "must not duplicate" "$proj")" = 0 ]'
ok "user/assistant text rows carry the Claude message shape" \
  'grep -q "{\"type\": \"user\", \"message\": {\"role\": \"user\", \"content\": \[{\"type\": \"text\", \"text\": \"배포 스크립트 점검해줘\"}\]}}" "$proj" && grep -q "\"type\": \"assistant\"" "$proj"'
ok "shell bash -lc wrapper is unwrapped to the script payload" \
  'grep -q "\"command\": \"systemctl status myapp && journalctl -u myapp -n 50\"" "$proj"'
ok "exec_command cmd and apply_patch input are projected as Bash tool_use" \
  '[ "$(grep -c "\"type\": \"tool_use\", \"name\": \"Bash\"" "$proj")" = 3 ]'
ok "non-command function_call (wait) is discarded" '! grep -q "cell_id" "$proj"'

# The whole point of the exact tool_use shape: scan.sh's extractor runs over
# the normalized tree unchanged.
extracted="$(find "$OUT" -name '*.jsonl' -print0 2>/dev/null | xargs -0 -r cat | jq -Rrc 'fromjson? | (.message.content[]? // empty) | select(.type=="tool_use" and .name=="Bash") | (.input.command | gsub("[\n\t]+";" "))' | wc -l | tr -d '[:space:]')"
ok "scan.sh's Bash-shape extractor finds all 3 commands over the normalized tree" '[ "$extracted" = 3 ]'

# --- exec exclusion: machine-driven runs must not self-reference -------------
EXEC_FIX="$TMP/rollout-exec.jsonl"
cat > "$EXEC_FIX" <<'EOF'
{"timestamp":"2026-08-28T12:05:28.425Z","type":"session_meta","payload":{"session_id":"execsess01","cwd":"/root","originator":"codex_exec"}}
{"timestamp":"2026-08-28T12:05:39.085Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"machine run"}]}}
EOF
EXEC_OUT="$TMP/exec-out"
normalize "$EXEC_FIX" "$EXEC_OUT" > "$TMP/summary-exec.json"
ok "a codex_exec session is excluded by default" \
  'jq -e ".excluded==true and .out_path==\"\"" "$TMP/summary-exec.json" >/dev/null'
ok "an excluded session writes no file" '[ ! -e "$EXEC_OUT/-root/execsess01.jsonl" ]'
normalize "$EXEC_FIX" "$EXEC_OUT" --include-exec > "$TMP/summary-exec2.json"
ok "--include-exec lifts the exclusion" \
  'jq -e ".excluded==false" "$TMP/summary-exec2.json" >/dev/null && [ -f "$EXEC_OUT/-root/execsess01.jsonl" ]'

# --- headless fallback: event_msg is the only transcript --------------------
HEADLESS_FIX="$TMP/rollout-headless.jsonl"
cat > "$HEADLESS_FIX" <<'EOF'
{"timestamp":"2026-08-28T12:05:28.425Z","type":"session_meta","payload":{"session_id":"headless01","cwd":"/srv/job","originator":"codex_exec"}}
{"timestamp":"2026-08-28T12:05:32.832Z","type":"event_msg","payload":{"type":"user_message","message":"run the nightly backup job"}}
{"timestamp":"2026-08-28T12:05:39.078Z","type":"event_msg","payload":{"type":"agent_message","message":"backup completed with verification"}}
EOF
normalize "$HEADLESS_FIX" "$TMP/headless-out" --include-exec > /dev/null
hproj="$TMP/headless-out/-srv-job/headless01.jsonl"
ok "headless rollouts fall back to event_msg rows" \
  'grep -q "run the nightly backup job" "$hproj" && grep -q "backup completed with verification" "$hproj"'

# --- projection cap ---------------------------------------------------------
BIG_FIX="$TMP/rollout-big.jsonl"
{
  head -1 "$FIX"
  for i in $(seq 1 400); do
    printf '{"timestamp":"t","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"%0400d"}]}}\n' "$i"
  done
} > "$BIG_FIX"
normalize "$BIG_FIX" "$TMP/big-out" --max-bytes 4096 > "$TMP/summary-big.json"
ok "the per-session output cap truncates and reports it" \
  'jq -e ".truncated==true and .out_bytes<=4096" "$TMP/summary-big.json" >/dev/null'
ok "truncated output stays within the cap" \
  '[ "$(wc -c < "$TMP/big-out/-root-projects-my-app/01a04842-bbf3.jsonl" 2>/dev/null || echo 999999)" -le 4096 ]'

# --- malformed input + exit codes ------------------------------------------
printf 'not json at all\n{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"survivor"}]}}\n' > "$TMP/rollout-malformed.jsonl"
normalize "$TMP/rollout-malformed.jsonl" "$TMP/mal-out" > /dev/null
ok "malformed lines are skipped, valid rows survive" \
  'grep -rq "survivor" "$TMP/mal-out"'
normalize "$TMP/does-not-exist.jsonl" "$TMP/none-out" >/dev/null 2>&1
rc=$?
ok "a missing source exits 1" '[ "$rc" = 1 ]'
python3 "$NORM" "$FIX" --out-dir "$TMP/x" --max-bytes 0 >/dev/null 2>&1
rc=$?
ok "a non-positive --max-bytes exits 2" '[ "$rc" = 2 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
