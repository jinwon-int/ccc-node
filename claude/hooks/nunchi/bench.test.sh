#!/usr/bin/env bash
# Regression: a dialectic backend that drains stdin must not consume the
# remaining Q-set rows owned by bench.sh's read loop.
# shellcheck disable=SC2034 # assertion variables are consumed through ok/eval
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

home="$TMP/home"
hooks="$home/.claude/hooks/nunchi"
state="$home/.claude/state"
nunchi_home="$home/.nunchi"
mkdir -p "$hooks" "$state" "$nunchi_home"
cp "$ROOT/claude/hooks/nunchi/bench.sh" "$hooks/bench.sh"
chmod 700 "$hooks/bench.sh"
printf 'on' > "$state/nunchi.mode"

cat > "$hooks/nunchi.py" <<'PY'
#!/usr/bin/env python3
import sys

# Simulate a provider CLI that consumes inherited stdin.
sys.stdin.read()
print("BENCH_STDIN_ISOLATED")
PY

cat > "$hooks/bench-qset.tsv" <<'EOF'
id	category	query	expect_hint
q1	relational	query one	expect one
q2	relational	query two	expect two
q3	verbatim	query three	expect three
q4	verbatim	query four	expect four
q5	time	query five	expect five
EOF

HOME="$home" CCC_STATE_DIR="$state" NUNCHI_HOME="$nunchi_home" \
  NUNCHI_BENCH_QSET="$hooks/bench-qset.tsv" CCC_NODE=test-node \
  bash "$hooks/bench.sh" > "$TMP/stdout" 2> "$TMP/stderr"
rc=$?
out="$(find "$nunchi_home" -maxdepth 1 -name 'bench-*.md' -type f -print -quit)"

ok "bench exits successfully" '[ "$rc" = 0 ] && [ ! -s "$TMP/stderr" ]'
ok "all five Q-set rows run even when the backend drains stdin" \
  '[ -n "$out" ] && [ "$(grep -c "^## q[1-5] " "$out")" = 5 ]'
ok "every child (5 queries + metrics) is stdin-isolated" \
  '[ "$(grep -c "BENCH_STDIN_ISOLATED" "$out")" = 6 ]'
ok "bench records the #890 metrics section" 'grep -q "^## metrics " "$out"'
ok "the final Q-set row is preserved" \
  'grep -q "^- Q: query five$" "$out" && grep -q "^- expect: expect five$" "$out"'

# #1078 — a healthy backend must be scored as a usable parity sample.
ok "a healthy answer is marked status=OK" \
  '[ "$(grep -c "^## q[1-5] .*status=OK$" "$out")" = 5 ]'
ok "a healthy run reports a fully valid summary" \
  'grep -q "^## bench-summary " "$out" && grep -q "^- valid: 5$" "$out" && grep -q "^- invalid: 0$" "$out"'
ok "a healthy run carries no contamination warning" \
  '! grep -q "SAMPLE CONTAMINATED" "$out"'

# #1078 regression — a logged-out provider prints its notice to stdout and
# exits 0. Such rows contain no "기록 없음", so before this guard they scored
# as the best-performing nodes on the Phase 2 parity gate.
home2="$TMP/home-loggedout"
hooks2="$home2/.claude/hooks/nunchi"
state2="$home2/.claude/state"
nunchi_home2="$home2/.nunchi"
mkdir -p "$hooks2" "$state2" "$nunchi_home2"
cp "$ROOT/claude/hooks/nunchi/bench.sh" "$hooks2/bench.sh"
chmod 700 "$hooks2/bench.sh"
printf 'on' > "$state2/nunchi.mode"
cp "$hooks/bench-qset.tsv" "$hooks2/bench-qset.tsv"

cat > "$hooks2/nunchi.py" <<'PY'
#!/usr/bin/env python3
import sys

# Simulate a logged-out provider CLI: notice on stdout, exit code 0.
sys.stdin.read()
print("Not logged in · Please run /login")
PY

HOME="$home2" CCC_STATE_DIR="$state2" NUNCHI_HOME="$nunchi_home2" \
  NUNCHI_BENCH_QSET="$hooks2/bench-qset.tsv" CCC_NODE=test-node \
  bash "$hooks2/bench.sh" > "$TMP/stdout2" 2> "$TMP/stderr2"
rc2=$?
out2="$(find "$nunchi_home2" -maxdepth 1 -name 'bench-*.md' -type f -print -quit)"

ok "a logged-out backend still exits 0 (bench is not the failing unit)" \
  '[ "$rc2" = 0 ] && [ ! -s "$TMP/stderr2" ]'
ok "logged-out rows keep the true rc=0 but are marked status=INVALID" \
  '[ -n "$out2" ] && [ "$(grep -c "^## q[1-5] .*rc=0 status=INVALID reason=provider-failure$" "$out2")" = 5 ]'
ok "the summary reports zero valid samples" \
  'grep -q "^- valid: 0$" "$out2" && grep -q "^- invalid: 5$" "$out2"'
ok "the summary warns that the sample is contaminated" \
  'grep -q "SAMPLE CONTAMINATED" "$out2"'
ok "the provider notice is preserved for human review, not dropped" \
  'grep -q "Not logged in" "$out2"'
ok "stdout surfaces the validity counts to the cron log" \
  'grep -q "valid=0 invalid=5" "$TMP/stdout2"'

# A same-day second run must not have its summary skewed by the earlier run
# already appended to the same file.
HOME="$home2" CCC_STATE_DIR="$state2" NUNCHI_HOME="$nunchi_home2" \
  NUNCHI_BENCH_QSET="$hooks2/bench-qset.tsv" CCC_NODE=test-node \
  bash "$hooks2/bench.sh" > "$TMP/stdout3" 2>&1
ok "a same-day rerun counts only its own rows" \
  '[ "$(grep -c "^- invalid: 5$" "$out2")" = 2 ] && ! grep -q "invalid: 10" "$out2"'

# A non-zero exit is a distinct failure mode from a logged-out provider.
cat > "$hooks2/nunchi.py" <<'PY'
#!/usr/bin/env python3
import sys

sys.stdin.read()
print("boom")
sys.exit(3)
PY
rm -f "$nunchi_home2"/bench-*.md
HOME="$home2" CCC_STATE_DIR="$state2" NUNCHI_HOME="$nunchi_home2" \
  NUNCHI_BENCH_QSET="$hooks2/bench-qset.tsv" CCC_NODE=test-node \
  bash "$hooks2/bench.sh" > "$TMP/stdout4" 2>&1
out4="$(find "$nunchi_home2" -maxdepth 1 -name 'bench-*.md' -type f -print -quit)"
ok "a non-zero backend exit is marked INVALID with the exit code as reason" \
  '[ -n "$out4" ] && [ "$(grep -c "rc=3 status=INVALID reason=exit-3$" "$out4")" = 5 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
