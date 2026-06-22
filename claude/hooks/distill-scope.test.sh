#!/usr/bin/env bash
# Tests for distill.sh cwd scoping — no provider/network calls.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DISTILL="$HERE/distill.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

make_transcript() {
  local path="$1" lines="${2:-6}"
  mkdir -p "$(dirname "$path")"
  : > "$path"
  for i in $(seq 1 "$lines"); do
    printf '{"type":"user","message":{"content":"turn %s"}}\n' "$i" >> "$path"
  done
}

STATE="$TMP/state"
mkdir -p "$STATE"
TRANS_OTHER="$TMP/projects/-root--openclaw-workspace/sess-other.jsonl"
make_transcript "$TRANS_OTHER" 6

payload_other() {
  jq -nc --arg sid "$1" --arg tp "$2" --arg cwd "$3" \
    '{session_id:$sid, transcript_path:$tp, cwd:$cwd}'
}

out="$(payload_other sess-other "$TRANS_OTHER" "/root/.openclaw/workspace" \
  | CCC_STATE_DIR="$STATE" CCC_DISTILL_SCOPE_CWDS="/root" bash "$DISTILL" sessionend 2>&1)"; rc=$?
ok "scope mismatch exits 0" '[ "$rc" = 0 ]'
ok "scope mismatch logs cwd-out-of-scope" 'grep -q "skip reason=cwd-out-of-scope cwd=/root/.openclaw/workspace project=-root--openclaw-workspace" "$STATE/distill.log"'
ok "scope mismatch does not spawn background" '! grep -q "spawned bg" "$STATE/distill.log"'

: > "$STATE/distill.log"
TRANS_ALLOWED="$TMP/projects/-root--openclaw-workspace/sess-allowed.jsonl"
make_transcript "$TRANS_ALLOWED" 2
out="$(payload_other sess-allowed "$TRANS_ALLOWED" "/root/.openclaw/workspace" \
  | CCC_STATE_DIR="$STATE" CCC_DISTILL_SCOPE_CWDS="/root/.openclaw/workspace" bash "$DISTILL" sessionend 2>&1)"; rc=$?
ok "scope exact cwd match exits 0" '[ "$rc" = 0 ]'
ok "scope exact cwd match reaches min-content gate" 'grep -q "skip reason=too-few-lines" "$STATE/distill.log" && ! grep -q "cwd-out-of-scope" "$STATE/distill.log"'

: > "$STATE/distill.log"
printf '%s\n' '-root--openclaw-workspace' > "$STATE/distill.scope"
out="$(jq -nc --arg sid sess-encoded --arg tp "$TRANS_ALLOWED" '{session_id:$sid, transcript_path:$tp}' \
  | CCC_STATE_DIR="$STATE" bash "$DISTILL" sessionend 2>&1)"; rc=$?
ok "encoded project scope file match exits 0" '[ "$rc" = 0 ]'
ok "encoded project scope file match reaches min-content gate" 'grep -q "source_cwd=encoded:-root--openclaw-workspace" "$STATE/distill.log" && grep -q "skip reason=too-few-lines" "$STATE/distill.log" && ! grep -q "cwd-out-of-scope" "$STATE/distill.log"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
