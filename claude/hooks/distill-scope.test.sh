#!/usr/bin/env bash
# Tests for distill.sh cwd scoping — no provider/network calls.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DISTILL="$HERE/distill.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

make_transcript() {
  local path="$1" lines="${2:-6}" type="${3:-user}"
  mkdir -p "$(dirname "$path")"
  : > "$path"
  for i in $(seq 1 "$lines"); do
    printf '{"type":"%s","message":{"content":"turn %s"}}\n' "$type" "$i" >> "$path"
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
ok "scope exact cwd match reaches turn-count gate" 'grep -q "skip reason=too-few-turns turns=2" "$STATE/distill.log" && ! grep -q "cwd-out-of-scope" "$STATE/distill.log"'

: > "$STATE/distill.log"
printf '%s\n' '-root--openclaw-workspace' > "$STATE/distill.scope"
out="$(jq -nc --arg sid sess-encoded --arg tp "$TRANS_ALLOWED" '{session_id:$sid, transcript_path:$tp}' \
  | CCC_STATE_DIR="$STATE" bash "$DISTILL" sessionend 2>&1)"; rc=$?
ok "encoded project scope file match exits 0" '[ "$rc" = 0 ]'
ok "encoded project scope file match reaches turn-count gate" 'grep -q "source_cwd=encoded:-root--openclaw-workspace" "$STATE/distill.log" && grep -q "skip reason=too-few-turns turns=2" "$STATE/distill.log" && ! grep -q "cwd-out-of-scope" "$STATE/distill.log"'

: > "$STATE/distill.log"
TRANS_STRUCTURAL="$TMP/projects/-root--openclaw-workspace/sess-structural.jsonl"
make_transcript "$TRANS_STRUCTURAL" 8 "queue-operation"
out="$(payload_other sess-structural "$TRANS_STRUCTURAL" "/root/.openclaw/workspace" \
  | CCC_STATE_DIR="$STATE" CCC_DISTILL_SCOPE_CWDS="/root/.openclaw/workspace" bash "$DISTILL" sessionend 2>&1)"; rc=$?
ok "structural-only transcript exits 0" '[ "$rc" = 0 ]'
ok "structural-only transcript is skipped by turn gate" 'grep -q "skip reason=too-few-turns turns=0" "$STATE/distill.log" && ! grep -q "spawned bg" "$STATE/distill.log"'

mkdir -p "$TMP/bin"
write_exec_stub "$TMP/bin/claude" <<'SH'
cat >/dev/null
if [ -n "${CLAUDE_STUB_COUNTER:-}" ]; then
  n=0
  [ -f "$CLAUDE_STUB_COUNTER" ] && n="$(cat "$CLAUDE_STUB_COUNTER" 2>/dev/null || printf 0)"
  n=$((n + 1))
  printf '%s' "$n" > "$CLAUDE_STUB_COUNTER"
  printf '{"session_id":"sess-%s","honcho":[],"wiki_candidates":[],"resume":{"last_activity":"ok","pending_action":"","awaiting_user":false,"open_question":"","next_step":"","evidence":[]}}' "$n"
else
  printf '{"honcho":[],"wiki_candidates":[],"resume":{"last_activity":"ok","pending_action":"","awaiting_user":false,"open_question":"","next_step":"","evidence":[]}}'
fi
SH
chmod +x "$TMP/bin/claude"
PATH="$TMP/bin:$PATH"
touch "$STATE/distill.dryrun"
: > "$STATE/distill.log"
TRANS_REAL="$TMP/projects/-root--openclaw-workspace/sess-real.jsonl"
make_transcript "$TRANS_REAL" 3 "user"
out="$(payload_other sess-real "$TRANS_REAL" "/root/.openclaw/workspace" \
  | CCC_STATE_DIR="$STATE" CCC_DISTILL_SCOPE_CWDS="/root/.openclaw/workspace" bash "$DISTILL" sessionend 2>&1)"; rc=$?
for _ in $(seq 1 25); do
  grep -q "dry-run skipping honcho/wiki push" "$STATE/distill.log" && break
  sleep 0.1
done
ok "three real turns exit 0" '[ "$rc" = 0 ]'
ok "three real turns pass turn gate and spawn" 'grep -q "spawned bg" "$STATE/distill.log" && grep -q "dry-run skipping honcho/wiki push" "$STATE/distill.log"'

: > "$STATE/distill.log"
rm -rf "$STATE/distill-history"
rm -f "$STATE/distill-last.json"
export CLAUDE_STUB_COUNTER="$TMP/claude-counter"
: > "$CLAUDE_STUB_COUNTER"
for r in 1 2 3 4; do
  TRANS_RING="$TMP/projects/-root--openclaw-workspace/sess-ring-$r.jsonl"
  make_transcript "$TRANS_RING" 3 "user"
  out="$(payload_other "sess-ring-$r" "$TRANS_RING" "/root/.openclaw/workspace" \
    | CCC_STATE_DIR="$STATE" CCC_DISTILL_HISTORY_KEEP=2 CCC_DISTILL_SCOPE_CWDS="/root/.openclaw/workspace" bash "$DISTILL" sessionend 2>&1)"; rc=$?
  ok "ring run $r exits 0" '[ "$rc" = 0 ]'
  for _ in $(seq 1 25); do
    jq -e --arg sid "sess-ring-$r" '.session_id == $sid' "$STATE/distill-last.json" >/dev/null 2>&1 && break
    sleep 0.1
  done
  : > "$STATE/distill.log"
done
for _ in $(seq 1 25); do
  hist_count="$(find "$STATE/distill-history" -maxdepth 1 -type f -name '*.json' 2>/dev/null | wc -l | tr -d '[:space:]')"
  [ "$hist_count" = 2 ] && break
  sleep 0.1
done
ok "distill history keep cap is enforced" '[ "$hist_count" = 2 ]'
ok "latest distill-last survives" 'jq -e ".session_id == \"sess-ring-4\"" "$STATE/distill-last.json" >/dev/null'
ok "old snapshots survive future runs" 'find "$STATE/distill-history" -maxdepth 1 -type f -name "*.json" -print0 | xargs -0 -r grep -h "sess-ring-" | grep -Eq "sess-ring-[23]"'
ok "distill, skill-review, and load-memory share one detached-spawn helper" \
  '[ -f "$HERE/lib/spawn-detached.sh" ] && grep -q '\''spawn_detached '\'' "$DISTILL" && grep -q '\''spawn_detached '\'' "$HERE/skill-review.sh" && grep -q '\''spawn_detached '\'' "$HERE/load-memory.sh"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
