#!/usr/bin/env bash
# Hermetic tests for skills/gh-pr-flow/ci-watch.sh. A stub `gh` replays
# scripted rollups (with keys deliberately in alphabetical order, as gh prints
# them) so the verdict never depends on key order. Covers green, failed (names
# the checks), merged, closed, superseded head, timeout, gh failure, usage.
# shellcheck disable=SC2034  # test variables are read inside the eval'd ok() conditions
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HERE/ci-watch.sh"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

mkdir -p "$TMP/bin" "$TMP/stub"
cat > "$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
# gh pr view ... --jq <program>: apply the real jq program to the scripted JSON
[ "$1 $2" = "pr view" ] || { echo "stub gh: unexpected: $*" >&2; exit 1; }
prog=""; while [ $# -gt 0 ]; do case "$1" in --jq) prog="$2"; shift 2 ;; *) shift ;; esac; done
n=$(cat "$STUB_DIR/.count" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$STUB_DIR/.count"
f="$STUB_DIR/pr.$n"; [ -f "$f" ] || f="$STUB_DIR/pr.last"
[ -f "$f" ] || exit 1
jq -r "$prog" "$f"
EOF
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH"
reset() { rm -rf "$TMP/stub"; mkdir -p "$TMP/stub"; export STUB_DIR="$TMP/stub"; }
# rollups are written with sorted keys (jq -S) to mimic gh's output ordering
pr_json() { # <state> <head> <mergeSha|null> <checks json array>
  jq -S -n --arg state "$1" --arg head "$2" --arg merge "$3" --argjson checks "$4" \
    '{state:$state, headRefOid:$head, mergeCommit:(if $merge=="null" then null else {oid:$merge} end), statusCheckRollup:$checks}'
}
HEAD=abc123abc123abc123abc123abc123abc123abc1

# 1. running → green on the 3rd poll; transitions reported once per change
reset
pr_json OPEN $HEAD null '[{"name":"lint","conclusion":null,"state":"IN_PROGRESS"},{"name":"test","conclusion":null,"state":""}]' > "$STUB_DIR/pr.1"
pr_json OPEN $HEAD null '[{"name":"lint","conclusion":"SUCCESS"},{"name":"test","conclusion":null,"state":"PENDING"}]' > "$STUB_DIR/pr.2"
pr_json OPEN $HEAD null '[{"name":"lint","conclusion":"SUCCESS"},{"name":"test","conclusion":"SUCCESS"},{"name":"codeql","conclusion":"NEUTRAL"}]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0)"; rc=$?
ok "all checks concluded without failure → green, exit 0" '[ "$rc" = 0 ] && printf "%s" "$out" | grep -q "green pr=7 head=abc123abc123 total=3"'
ok "pending/total transitions are reported once each (2/2 then 1/2)" '[ "$(printf "%s\n" "$out" | grep -c "running pr=7")" = 2 ]'

# 2. a failing check → exit 10 naming it, even while others are still pending
reset
pr_json OPEN $HEAD null '[{"name":"lint","conclusion":"FAILURE"},{"name":"test","conclusion":null,"state":"PENDING"},{"context":"legacy/ci","state":"ERROR"}]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0)"; rc=$?
ok "a FAILURE/ERROR check → exit 10 with the failing names" '[ "$rc" = 10 ] && printf "%s" "$out" | grep -q "failed pr=7 head=abc123abc123 failing=\[lint,legacy/ci\] pending=1 total=3"'

# 3. merged / closed
reset; pr_json MERGED $HEAD deadbeefdeadbeef '[]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0)"; rc=$?
ok "merged → exit 11 with the merge sha" '[ "$rc" = 11 ] && printf "%s" "$out" | grep -q "merged pr=7 head=abc123abc123 merge=deadbeefdead"'
reset; pr_json CLOSED $HEAD null '[]' > "$STUB_DIR/pr.last"
bash "$W" --repo o/r --pr 7 --interval 0 >/dev/null; rc=$?
ok "closed unmerged → exit 12" '[ "$rc" = 12 ]'

# 4. --head pin: a newer head ends the watch as superseded, not as a verdict on the wrong rollup
reset; pr_json OPEN fedcba9876543210fedcba9876543210fedcba98 null '[{"name":"lint","conclusion":"SUCCESS"}]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0 --head abc123abc123)"; rc=$?
ok "head moved → exit 13 superseded (never green on the new head)" '[ "$rc" = 13 ] && printf "%s" "$out" | grep -q "superseded pr=7 watched=abc123abc123 current=fedcba987654"'

# 5. no checks yet is RUNNING, not green; timeout → exit 20
reset; pr_json OPEN $HEAD null '[]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 1 --timeout 1)"; rc=$?
ok "an empty rollup never counts as green; timeout → exit 20" '[ "$rc" = 20 ] && printf "%s" "$out" | grep -q "timeout pr=7 head=abc123abc123 pending=0 total=0"'

# 6. JSON verdict
reset; pr_json OPEN $HEAD null '[{"name":"lint","conclusion":"SUCCESS"}]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0 --json)"; rc=$?
ok "--json emits one verdict object" '[ "$rc" = 0 ] && printf "%s" "$out" | jq -e ".verdict == \"green\" and .pr == 7" >/dev/null'

# 7. gh failing repeatedly → exit 3, not a silent loop
reset
out="$(bash "$W" --repo o/r --pr 7 --interval 0 2>&1)"; rc=$?
ok "three consecutive gh failures → exit 3" '[ "$rc" = 3 ]'

# 8. usage
bash "$W" --pr 7 >/dev/null 2>&1; rc=$?; ok "missing --repo → exit 2" '[ "$rc" = 2 ]'
bash "$W" --repo o/r --pr 7 --head zz >/dev/null 2>&1; rc=$?; ok "non-hex --head → exit 2" '[ "$rc" = 2 ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
