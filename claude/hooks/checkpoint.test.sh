#!/usr/bin/env bash
# Tests for checkpoint.sh — verifies non-root CCC_STATE_DIR support.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CHECKPOINT="$HERE/checkpoint.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export CCC_STATE_DIR="$TMP/state"
mkdir -p "$CCC_STATE_DIR"
printf 'active work\n' > "$CCC_STATE_DIR/working-state.md"

out="$(bash "$CHECKPOINT" PreCompact 2>&1)"; rc=$?
ok "PreCompact exits 0" '[ "$rc" = 0 ]'
ok "PreCompact writes snapshot under CCC_STATE_DIR" '[ "$(find "$CCC_STATE_DIR/checkpoints" -maxdepth 1 -type f -name "working-state-*.md" | wc -l | tr -d "[:space:]")" = 1 ]'
ok "PreCompact output is hook JSON" 'jq -e ".systemMessage and .suppressOutput == true" <<<"$out" >/dev/null'
ok "PreCompact log stays under CCC_STATE_DIR" '[ -s "$CCC_STATE_DIR/checkpoint.log" ] && grep -q "PreCompact" "$CCC_STATE_DIR/checkpoint.log"'

out="$(bash "$CHECKPOINT" PostCompact 2>&1)"; rc=$?
ok "PostCompact exits 0" '[ "$rc" = 0 ]'
ok "PostCompact reinjects working state" 'jq -e ".hookSpecificOutput.hookEventName == \"PostCompact\" and (.hookSpecificOutput.additionalContext | contains(\"active work\"))" <<<"$out" >/dev/null'

CLAUDE_DISTILL_INFLIGHT=1 bash "$CHECKPOINT" PreCompact >/tmp/checkpoint-guard.out 2>&1; rc=$?
ok "distill recursion guard exits 0" '[ "$rc" = 0 ]'
ok "distill recursion guard emits no output" '[ ! -s /tmp/checkpoint-guard.out ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
