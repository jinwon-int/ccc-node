#!/usr/bin/env bash
# Regression: a dialectic backend that drains stdin must not consume the
# remaining Q-set rows owned by bench.sh's read loop.
# shellcheck disable=SC2034 # assertion variables are consumed through ok/eval
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"

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

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
