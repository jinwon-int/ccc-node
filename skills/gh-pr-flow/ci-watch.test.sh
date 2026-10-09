#!/usr/bin/env bash
# Hermetic tests for skills/gh-pr-flow/ci-watch.sh. A stub `gh` replays
# scripted rollups (with keys deliberately in alphabetical order, as gh prints
# them) so the verdict never depends on key order. Covers green, failed (names
# the checks), merged, closed, superseded head, timeout, gh failure, usage, and
# the required-context / settle gate against early-registration greens (#2200).
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
# gh pr view ... --jq <program>: apply the real jq program to the scripted JSON.
# gh api repos/o/r/branches/<b> | rules/branches/<b>: replay branch.json /
# rules.json when present, else fail like a 404/permission error.
prog=""; for ((i=1; i<=$#; i++)); do [ "${!i}" = "--jq" ] && { j=$((i+1)); prog="${!j}"; }; done
case "$1 $2" in
  "pr view")
    case " $* " in *" baseRefName "*) echo main; exit 0 ;; esac
    n=$(cat "$STUB_DIR/.count" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$STUB_DIR/.count"
    f="$STUB_DIR/pr.$n"; [ -f "$f" ] || f="$STUB_DIR/pr.last"
    [ -f "$f" ] || exit 1
    jq -r "$prog" "$f" ;;
  "api repos/o/r/branches/main") [ -f "$STUB_DIR/branch.json" ] || exit 1; jq -c "$prog" "$STUB_DIR/branch.json" ;;
  "api repos/o/r/rules/branches/main") [ -f "$STUB_DIR/rules.json" ] || exit 1; jq -c "$prog" "$STUB_DIR/rules.json" ;;
  *) echo "stub gh: unexpected: $*" >&2; exit 1 ;;
esac
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
out="$(bash "$W" --repo o/r --pr 7 --interval 0 --required-context lint --required-context test)"; rc=$?
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

# 9. #2200: required contexts from branch protection gate GREEN. Right after
# update-branch only CodeQL had registered (one pass, one skipping): pending=0,
# total=2 — that must stay RUNNING, not green.
REQ_BRANCH='{"protection":{"required_status_checks":{"contexts":["validate-harness","codeql-python","bridge-tests (3.11)"]}}}'
reset; printf '%s' "$REQ_BRANCH" > "$STUB_DIR/branch.json"
pr_json OPEN $HEAD null '[{"name":"codeql-python","conclusion":"SUCCESS"},{"name":"CodeQL","conclusion":"SKIPPED"}]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 1 --timeout 1)"; rc=$?
ok "#2200 only early checks registered (pending 0) → never green; timeout names the missing contexts" \
  '[ "$rc" = 20 ] && printf "%s" "$out" | grep -q "missing=\[validate-harness,bridge-tests (3.11)\]\|missing=\[bridge-tests (3.11),validate-harness\]"'
ok "#2200 the required set source is reported" 'printf "%s" "$out" | grep -q "required pr=7 source=protection contexts=3"'

reset; printf '%s' "$REQ_BRANCH" > "$STUB_DIR/branch.json"
pr_json OPEN $HEAD null '[{"name":"codeql-python","conclusion":"SUCCESS"},{"name":"CodeQL","conclusion":"SKIPPED"}]' > "$STUB_DIR/pr.1"
pr_json OPEN $HEAD null '[{"name":"codeql-python","conclusion":"SUCCESS"},{"name":"validate-harness","conclusion":"SUCCESS"},{"name":"bridge-tests (3.11)","conclusion":"SUCCESS"},{"name":"extra","conclusion":"NEUTRAL"}]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0)"; rc=$?
ok "#2200 green once every required context is present and successful (single poll)" \
  '[ "$rc" = 0 ] && printf "%s" "$out" | grep -q "green pr=7 head=abc123abc123 total=4 required=protection" && [ "$(cat "$STUB_DIR/.count")" = 2 ]'
ok "#2200 the running line counts the missing required contexts" 'printf "%s" "$out" | grep -q "running pr=7 head=abc123abc123 pending=0 total=2 missing=2"'

# 10. rulesets contribute required contexts too (union with classic protection)
reset; printf '%s' '{"protection":{"enabled":false}}' > "$STUB_DIR/branch.json"
printf '%s' '[{"type":"deletion"},{"type":"required_status_checks","parameters":{"required_status_checks":[{"context":"test"}]}}]' > "$STUB_DIR/rules.json"
pr_json OPEN $HEAD null '[{"name":"lint","conclusion":"SUCCESS"}]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 1 --timeout 1)"; rc=$?
ok "a ruleset-required context that never registers keeps the watch running" '[ "$rc" = 20 ] && printf "%s" "$out" | grep -q "missing=\[test\]"'

# 11. no readable required set → GREEN must hold for --settle (2) consecutive polls
reset
pr_json OPEN $HEAD null '[{"name":"codeql","conclusion":"SUCCESS"}]' > "$STUB_DIR/pr.1"
pr_json OPEN $HEAD null '[{"name":"codeql","conclusion":"SUCCESS"},{"name":"test","conclusion":null,"state":"QUEUED"}]' > "$STUB_DIR/pr.2"
pr_json OPEN $HEAD null '[{"name":"codeql","conclusion":"SUCCESS"},{"name":"test","conclusion":"SUCCESS"}]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0)"; rc=$?
ok "fallback: a lone green poll followed by a new pending check is not green; settles on 2 consecutive" \
  '[ "$rc" = 0 ] && [ "$(cat "$STUB_DIR/.count")" = 4 ] && printf "%s" "$out" | grep -q "green pr=7 head=abc123abc123 total=2 required=none"'
reset; pr_json OPEN $HEAD null '[{"name":"codeql","conclusion":"SUCCESS"}]' > "$STUB_DIR/pr.last"
bash "$W" --repo o/r --pr 7 --interval 0 --settle 3 >/dev/null; rc=$?
ok "--settle 3 needs three consecutive green polls" '[ "$rc" = 0 ] && [ "$(cat "$STUB_DIR/.count")" = 3 ]'

# 12. --min-checks keeps a too-small rollup running
reset; pr_json OPEN $HEAD null '[{"name":"lint","conclusion":"SUCCESS"},{"name":"test","conclusion":"SUCCESS"}]' > "$STUB_DIR/pr.last"
bash "$W" --repo o/r --pr 7 --interval 1 --timeout 1 --required-context lint --min-checks 3 >/dev/null; rc=$?
ok "--min-checks 3 with two concluded checks → not green" '[ "$rc" = 20 ]'

# 13. failing check names with spaces survive field parsing intact
reset; pr_json OPEN $HEAD null '[{"name":"bridge-tests (3.11)","conclusion":"FAILURE"},{"name":"lint","conclusion":"SUCCESS"}]' > "$STUB_DIR/pr.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0)"; rc=$?
ok "a failing name with spaces is reported whole, counts not shifted" \
  '[ "$rc" = 10 ] && printf "%s" "$out" | grep -q "failing=\[bridge-tests (3.11)\] pending=0 total=2"'
bash "$W" --repo o/r --pr 7 --min-checks x >/dev/null 2>&1; rc=$?; ok "non-integer --min-checks → exit 2" '[ "$rc" = 2 ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
