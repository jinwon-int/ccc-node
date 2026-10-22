#!/usr/bin/env bash
# stale-bench.py regression: the STALE implicit-conflict bench runs LLM-free against a private
# temp DB (never the node's ~/.nunchi), reports both phases, and ratchets on stale-baseline.json
# (ccc-node#2464).
# shellcheck disable=SC2034 # assertion variables are consumed through ok/eval
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }
home="$TMP/home"; mkdir -p "$home/.nunchi"
export HOME="$home"
N="$ROOT/claude/hooks/nunchi"
# 1. The shipped fixture + baseline pass (ratchet holds on the current gates).
out="$(cd "$TMP" && python3 "$N/stale-bench.py" --json 2>"$TMP/err")"; rc=$?
ok "bench exits 0 on the shipped fixture" '[ "$rc" = 0 ]'
ok "bench reports 40 scenarios and both phases" 'jq -e ".scenarios == 40 and (.phase_a.detected|type) == \"number\" and (.phase_b.new_at5|type) == \"number\" and .regressions == []" >/dev/null <<<"$out"'
ok "no scenario was dropped by the write path" 'jq -e ".phase_a.not_stored == 0 and .phase_b.not_stored == 0" >/dev/null <<<"$out"'
ok "the node DB is never created" '[ ! -e "$home/.nunchi/facts.db" ]'
# 2. A stricter baseline fails closed with a named regression.
jq '.b_new_at5_min = 999' "$N/stale-baseline.json" > "$TMP/strict.json"
out="$(cd "$TMP" && python3 "$N/stale-bench.py" --baseline "$TMP/strict.json" 2>/dev/null)"; rc=$?
ok "ratchet violation exits 2" '[ "$rc" = 2 ]'
ok "ratchet violation names the metric" 'grep -q "REGRESSION: B M_new@5" <<<"$out"'
# 3. A two-scenario fixture with an explicit overlap exercises the G3 path deterministically.
cat > "$TMP/mini.json" <<'JSON'
{"source":"test","scenarios":[
 {"uid":"a","type":"T1","M_old":"I commute to the office by bike every day.","M_new":"I commute to the office by bike every day now with a new helmet.","dim1_query":"does the user commute by bike","explanation":"overlap"},
 {"uid":"b","type":"T2","M_old":"I love my daily ten mile bike ride to the office.","M_new":"The doctor said the leg cast stays on for six more weeks.","dim1_query":"does the user still bike to the office","explanation":"implicit"}
]}
JSON
printf '{}' > "$TMP/empty-baseline.json"
out="$(cd "$TMP" && python3 "$N/stale-bench.py" --fixture "$TMP/mini.json" --baseline "$TMP/empty-baseline.json" --json 2>/dev/null)"; rc=$?
ok "mini fixture runs" '[ "$rc" = 0 ] && jq -e ".scenarios == 2" >/dev/null <<<"$out"'
ok "lexical overlap is detected, implicit conflict is missed" 'jq -e ".phase_a.detected == 1 and .phase_a.missed == 1" >/dev/null <<<"$out"'
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
