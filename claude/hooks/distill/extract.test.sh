#!/usr/bin/env bash
# Tests for distill/extract.sh — hermetic claude stub, no network/provider calls.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
EXTRACT="$HERE/extract.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

make_transcript() {
  local path="$1"
  cat > "$path" <<'JSONL'
{"type":"system","content":"ignore"}
{"type":"user","message":{"content":"hello ghp_abcdefghijklmnopqrstuvwxyz123456"}}
{"type":"assistant","message":{"content":[{"type":"text","text":"noted"},{"type":"tool_use","name":"Bash"}]}}
{"type":"user","message":{"content":"Bearer abcdefghijklmnopqrstuvwxyz123456"}}
{"type":"assistant","message":{"content":"final"}}
JSONL
}

install_stub() {
  local mode="$1"
  mkdir -p "$TMP/bin"
  cat > "$TMP/bin/claude" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
mode="${CLAUDE_STUB_MODE:-valid}"
count_file="${CLAUDE_STUB_COUNT_FILE:?}"
input_file="${CLAUDE_STUB_INPUT_FILE:-}"
count=0
[ -f "$count_file" ] && count="$(cat "$count_file")"
count=$((count + 1))
printf '%s' "$count" > "$count_file"
if [ -n "$input_file" ]; then cat > "$input_file"; else cat >/dev/null; fi
case "$mode" in
  valid)
    printf '{"honcho":[{"kind":"context","text":"ok","subject":"session"}],"wiki_candidates":[]}'
    ;;
  fenced)
    printf '```json\n{"honcho":[],"wiki_candidates":[]}\n```\n'
    ;;
  drift)
    if [ "$count" = 1 ]; then printf 'Here is the JSON: nope\n'; else printf '{"honcho":[],"wiki_candidates":[]}'; fi
    ;;
  timeout)
    if [ "$count" = 1 ]; then sleep 5; else printf '{"honcho":[],"wiki_candidates":[]}'; fi
    ;;
  *)
    printf '{"honcho":[],"wiki_candidates":[]}'
    ;;
esac
SH
  chmod +x "$TMP/bin/claude"
  export PATH="$TMP/bin:$PATH"
  export CLAUDE_STUB_MODE="$mode"
  export CLAUDE_STUB_COUNT_FILE="$TMP/count-$mode"
  export CLAUDE_STUB_INPUT_FILE="$TMP/input-$mode.txt"
  : > "$CLAUDE_STUB_COUNT_FILE"
}

run_extract() {
  local transcript="$1" mode="$2" timeout_s="${3:-4}"
  install_stub "$mode"
  export CLAUDE_DISTILL_TRANSCRIPT="$transcript"
  export CLAUDE_DISTILL_SESSION="sess-test"
  export CLAUDE_DISTILL_TRIGGER="manual"
  export CLAUDE_DISTILL_TIMEOUT="$timeout_s"
  export CLAUDE_DISTILL_MAX_TURNS=20
  export CLAUDE_DISTILL_MAX_BYTES=20000
  out="$(bash "$EXTRACT" 2>"$TMP/stderr-$mode")"; rc=$?
}

TRANSCRIPT="$TMP/transcript.jsonl"
make_transcript "$TRANSCRIPT"

out=""; rc=99
run_extract "$TRANSCRIPT" valid
ok "valid JSON exits 0" '[ "$rc" = 0 ]'
ok "valid JSON is tagged with session metadata" 'jq -e ".session_id == \"sess-test\" and .trigger == \"manual\" and (.honcho|length)==1" <<<"$out" >/dev/null'
ok "transcript input is redacted before claude" '! grep -q "ghp_abcdefghijklmnopqrstuvwxyz123456\|Bearer abcdefghijklmnopqrstuvwxyz123456" "$TMP/input-valid.txt" && grep -q "REDACTED" "$TMP/input-valid.txt"'

run_extract "$TRANSCRIPT" fenced
ok "fenced JSON is stripped" '[ "$rc" = 0 ] && jq -e ".honcho == [] and .wiki_candidates == []" <<<"$out" >/dev/null'

run_extract "$TRANSCRIPT" drift
ok "JSON-drift retry exits 0" '[ "$rc" = 0 ] && jq -e ".honcho == [] and .wiki_candidates == []" <<<"$out" >/dev/null'
ok "JSON-drift retry logs recovery" 'grep -q "recovered on JSON-drift retry" "$TMP/stderr-drift"'
ok "JSON-drift invokes claude twice" '[ "$(cat "$TMP/count-drift")" = 2 ]'

run_extract "$TRANSCRIPT" timeout 1
ok "timeout retry exits 0" '[ "$rc" = 0 ] && jq -e ".honcho == [] and .wiki_candidates == []" <<<"$out" >/dev/null'
ok "timeout retry logs recovery" 'grep -q "recovered on timeout retry" "$TMP/stderr-timeout"'
ok "timeout retry invokes claude twice" '[ "$(cat "$TMP/count-timeout")" = 2 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
