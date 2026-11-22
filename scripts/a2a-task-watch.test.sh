#!/usr/bin/env bash
# Tests for scripts/a2a-task-watch.sh (ccc-node#2837).
#
# The watcher reads a process environ and an env file; both are fixtures here
# (A2A_TASK_WATCH_PID / A2A_TASK_WATCH_ENVIRON), so no real worker is needed
# and nothing under $HOME is touched.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH="$ROOT/scripts/a2a-task-watch.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
    if eval "$2"; then
        pass=$((pass+1))
    else
        fail=$((fail+1))
        echo "FAIL: $1"
        echo "  cond: $2"
    fi
}

# write_environ <file> KEY=VALUE... — NUL-separated like /proc/<pid>/environ.
write_environ() {
    local f="$1"; shift
    : > "$f"
    local kv
    for kv in "$@"; do printf '%s\0' "$kv" >> "$f"; done
}

HIGH_PIN=(
    A2A_WORKER_BRIDGE_ADAPTER=claude-code
    A2A_ANALYSIS_BRIDGE_ADAPTER=claude-code
    A2A_WORKER_RUNTIME_FLAVOR=claude-code
    A2A_CLAUDE_MODEL=claude-opus-5-5
    A2A_CLAUDE_EFFORT=high
    CLAUDE_CODE_EFFORT_LEVEL=high
    A2A_WORKER_THINKING=high
)

mkdir -p "$TMP/wroot"
touch -d '-1 hour' "$TMP/wroot" 2>/dev/null || true
ENVF="$TMP/canonical.env"
{
    echo "# canonical worker env"
    echo "A2A_TERMUX_NATIVE=1"
    printf '%s\n' "${HIGH_PIN[@]}"
    echo "UNRELATED_SECRET=do-not-print"
} > "$ENVF"
ENVIRON="$TMP/environ"

run_watch() {
    A2A_TASK_WATCH_OUT="$OUT" \
    A2A_TASK_WATCH_PID="${PID_OVERRIDE-$$}" \
    A2A_TASK_WATCH_ENVIRON="$ENVIRON" \
    A2A_TASK_WATCH_WROOT="$TMP/wroot" \
    A2A_TASK_WATCH_SUPLOG="$TMP/sup.log" \
    A2A_TASK_WATCH_REPEAT_SECS="${REPEAT-3600}" \
    A2A_TASK_WATCH_MAX_BYTES="${MAXB-1048576}" \
        bash "$WATCH" testnode --env-file "${ENVF_OVERRIDE-$ENVF}"
}

# 1. #2837 regression: env file and process both say high -> no drift line.
OUT="$TMP/t1.log"
write_environ "$ENVIRON" "${HIGH_PIN[@]}" PATH=/usr/bin UNRELATED_SECRET=do-not-print
run_watch; rc=$?
ok "matching pin exits 0" "[[ $rc -eq 0 ]]"
ok "matching pin writes no drift" '! grep -q "PIN DRIFT" "$OUT" 2>/dev/null'

# 2. Only the mismatched key is reported, with expected and actual values.
OUT="$TMP/t2.log"
write_environ "$ENVIRON" "${HIGH_PIN[@]/A2A_WORKER_THINKING=high/A2A_WORKER_THINKING=medium}"
run_watch
ok "drift logged" 'grep -q "PIN DRIFT node=testnode" "$OUT"'
ok "drift names key and both values" 'grep -q "DRIFT A2A_WORKER_THINKING expected=high actual=medium" "$OUT"'
ok "matching keys not reported" '[[ $(grep -c "  DRIFT " "$OUT") -eq 1 ]]'
ok "non-pin env never printed" '! grep -q "do-not-print" "$OUT"'

# 3. Same drift within the repeat window is not logged again ...
run_watch
ok "repeat suppressed" '[[ $(grep -c "PIN DRIFT" "$OUT") -eq 1 ]]'
# ... but a changed drift signature is.
write_environ "$ENVIRON" "${HIGH_PIN[@]/A2A_CLAUDE_MODEL=claude-opus-5-5/A2A_CLAUDE_MODEL=claude-sonnet-5-5}"
run_watch
ok "new signature logged" '[[ $(grep -c "PIN DRIFT" "$OUT") -eq 2 ]]'
ok "new drift names model" 'grep -q "DRIFT A2A_CLAUDE_MODEL expected=claude-opus-5-5 actual=claude-sonnet-5-5" "$OUT"'
# ... and the same drift is logged again once the window has elapsed.
REPEAT=0 run_watch
ok "repeat after window" '[[ $(grep -c "PIN DRIFT" "$OUT") -eq 3 ]]'

# 4. Recovery clears the drift state, so a later drift is logged immediately.
write_environ "$ENVIRON" "${HIGH_PIN[@]}"
run_watch
ok "state cleared on recovery" '[[ ! -e "$OUT.drift-state" ]]'

# 5. A key missing from the process but pinned in the env file is drift.
OUT="$TMP/t5.log"
write_environ "$ENVIRON" "${HIGH_PIN[@]:0:6}"
run_watch
ok "unset key is drift" 'grep -q "DRIFT A2A_WORKER_THINKING expected=high actual=<unset>" "$OUT"'

# 6. A key absent from the env file is not pinned and never drifts.
OUT="$TMP/t6.log"
ENVF6="$TMP/partial.env"
printf '%s\n' "${HIGH_PIN[@]:0:4}" > "$ENVF6"
write_environ "$ENVIRON" "${HIGH_PIN[@]:0:4}" A2A_WORKER_THINKING=medium
ENVF_OVERRIDE="$ENVF6" run_watch
ok "unpinned key ignored" '! grep -q "PIN DRIFT" "$OUT" 2>/dev/null'

# 7. An env file pinning none of the keys is reported as NOPIN.
OUT="$TMP/t7.log"
ENVF7="$TMP/nopin.env"
echo "A2A_TERMUX_NATIVE=1" > "$ENVF7"
ENVF_OVERRIDE="$ENVF7" run_watch
ok "nopin reported" 'grep -q "  NOPIN" "$OUT"'

# 8. export prefix and quoted values parse like the worker harness.
OUT="$TMP/t8.log"
ENVF8="$TMP/quoted.env"
printf '%s\n' 'export A2A_WORKER_THINKING="high"' "A2A_CLAUDE_MODEL='claude-opus-5-5'" > "$ENVF8"
write_environ "$ENVIRON" "${HIGH_PIN[@]}"
ENVF_OVERRIDE="$ENVF8" run_watch
ok "quoted/export parsed" '! grep -q "PIN DRIFT" "$OUT" 2>/dev/null'

# 9. Usage errors and a missing env file exit 2.
bash "$WATCH" >/dev/null 2>&1; rc=$?
ok "no args -> rc 2" "[[ $rc -eq 2 ]]"
bash "$WATCH" testnode >/dev/null 2>&1; rc=$?
ok "no env file -> rc 2" "[[ $rc -eq 2 ]]"
OUT="$TMP/t9.log"
ENVF_OVERRIDE="$TMP/missing.env" run_watch 2>/dev/null; rc=$?
ok "missing env file -> rc 2" "[[ $rc -eq 2 ]]"

# 10. No worker process -> WARN line, rc 0.
OUT="$TMP/t10.log"
PID_OVERRIDE="" run_watch; rc=$?
ok "no worker rc 0" "[[ $rc -eq 0 ]]"
ok "no worker warned" 'grep -q "WARN worker not running on testnode" "$OUT"'

# 11. Fresh worker artifacts -> TASK ACTIVITY block with pin status.
OUT="$TMP/t11.log"
write_environ "$ENVIRON" "${HIGH_PIN[@]}"
echo "tail line" > "$TMP/sup.log"
touch "$TMP/wroot/artifact.json"
run_watch
ok "activity block" 'grep -q "TASK ACTIVITY node=testnode pid=$$ pin=ok" "$OUT"'
ok "activity lists artifact" 'grep -q "artifact.json" "$OUT"'
ok "activity has supervisor tail" 'grep -q "    tail line" "$OUT"'
rm -f "$TMP/wroot/artifact.json"

# 12. An oversized log rotates to .1 before the run appends.
OUT="$TMP/t12.log"
head -c 2048 /dev/zero > "$OUT"
write_environ "$ENVIRON" "${HIGH_PIN[@]/A2A_CLAUDE_EFFORT=high/A2A_CLAUDE_EFFORT=low}"
MAXB=1024 run_watch
ok "rotated" '[[ -s "$OUT.1" ]]'
ok "fresh log after rotation" '[[ $(wc -c < "$OUT") -lt 1024 ]] && grep -q "PIN DRIFT" "$OUT"'

echo "PASS=$pass FAIL=$fail"
[[ $fail -eq 0 ]]
