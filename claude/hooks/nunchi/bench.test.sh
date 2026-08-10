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
  '[ "$(grep -c "^## q[1-5] .*status=OK source=nunchi$" "$out")" = 5 ]'
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
  '[ -n "$out2" ] && [ "$(grep -c "^## q[1-5] .*rc=0 status=INVALID reason=provider-failure source=none$" "$out2")" = 5 ]'
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
  '[ -n "$out4" ] && [ "$(grep -c "rc=3 status=INVALID reason=exit-3 source=none$" "$out4")" = 5 ]'

# ---- #827 / TM-2339: the gate is "nunchi + Wiki", not "nunchi alone" --------
# TM-2029 assigned durable cross-node knowledge to the Family Wiki. A per-node
# store can never answer a cross-node question, so measuring nunchi alone made
# the gate unpassable by design. The Wiki layer is consulted ONLY when nunchi
# found nothing, and never for a contaminated (INVALID) row.
wiki_home="$TMP/home-wiki"
wiki_hooks="$wiki_home/.claude/hooks/nunchi"
wiki_state="$wiki_home/.claude/state"
wiki_nh="$wiki_home/.nunchi"
wbin="$TMP/bin-wiki"
mkdir -p "$wiki_hooks" "$wiki_state" "$wiki_nh" "$wbin"
cp "$ROOT/claude/hooks/nunchi/bench.sh" "$wiki_hooks/bench.sh"
chmod 700 "$wiki_hooks/bench.sh"
printf 'on' > "$wiki_state/nunchi.mode"
cp "$hooks/bench-qset.tsv" "$wiki_hooks/bench-qset.tsv"

# nunchi finds nothing, and paraphrases it the way gwakga's q6 answer did —
# a literal "기록 없음" match scored that paraphrase as a SUCCESS.
cat > "$wiki_hooks/nunchi.py" <<'PY'
#!/usr/bin/env python3
import sys
sys.stdin.read()
if sys.argv[1] == "synthesize":
    print("WIKI-ANSWER: " + sys.stdin.name)
else:
    print("질문한 내용에 대한 근거 기록이 없습니다.")
PY
cat > "$wbin/wiki-agent" <<'EOF'
#!/usr/bin/env bash
echo "pages/decisions/x.md:1-3  Termux는 chromadb 빌드 불가로 peer_facts-only로 확정"
EOF
chmod +x "$wbin/wiki-agent"

run_wiki_bench() {
  rm -f "$wiki_nh"/bench-*.md
  env HOME="$wiki_home" CCC_STATE_DIR="$wiki_state" NUNCHI_HOME="$wiki_nh" \
    NUNCHI_BENCH_QSET="$wiki_hooks/bench-qset.tsv" CCC_NODE=test-node \
    NUNCHI_BENCH_WIKI_CLI="$wbin/wiki-agent" \
    bash "$wiki_hooks/bench.sh" > "$TMP/wout" 2>&1
  wout="$(find "$wiki_nh" -maxdepth 1 -name 'bench-*.md' -type f -print -quit)"
}

# The synthesize stub must answer for the Wiki layer to count. Rewrite it so the
# stub distinguishes the two nunchi.py subcommands by argv, not by stdin.
cat > "$wiki_hooks/nunchi.py" <<'PY'
#!/usr/bin/env python3
import sys
cmd = sys.argv[1]
data = sys.stdin.read()
if cmd == "synthesize":
    print("WIKI-ANSWER " + ("(with-evidence)" if data.strip() else "(empty)"))
elif cmd == "metrics":
    print("stub metrics")
else:
    print("질문한 내용에 대한 근거 기록이 없습니다.")
PY
run_wiki_bench
ok "a paraphrased no-record is detected, not scored as an answer" \
  '[ -n "$wout" ] && [ "$(grep -c "^## q[1-5] .*source=nunchi" "$wout")" = 0 ]'
ok "the Wiki layer answers what the per-node store cannot" \
  '[ "$(grep -c "^## q[1-5] .*source=wiki$" "$wout")" = 5 ]'
ok "the Wiki answer is recorded alongside the nunchi one" \
  'grep -q "^- Wiki 계층 답변:" "$wout" && grep -q "WIKI-ANSWER" "$wout"'
ok "the summary attributes answers per layer" \
  'grep -q "^- answered by nunchi: 0$" "$wout" && grep -q "^- answered by wiki: 5$" "$wout" && grep -q "^- unanswered (gate candidates): 0$" "$wout"'

# Termux measured 2026-08-10: wiki-agent find did not finish within 12s on
# daegyo. A slow or missing Wiki must degrade to source=none, never hang or
# fail the run — the bench is a cron job on every node.
cat > "$wbin/wiki-agent-slow" <<'EOF'
#!/usr/bin/env bash
sleep 30
EOF
chmod +x "$wbin/wiki-agent-slow"
rm -f "$wiki_nh"/bench-*.md
HOME="$wiki_home" CCC_STATE_DIR="$wiki_state" NUNCHI_HOME="$wiki_nh" \
  NUNCHI_BENCH_QSET="$wiki_hooks/bench-qset.tsv" CCC_NODE=test-node \
  NUNCHI_BENCH_WIKI_CLI="$wbin/wiki-agent-slow" NUNCHI_BENCH_WIKI_TIMEOUT_SEC=1 \
  bash "$wiki_hooks/bench.sh" > "$TMP/wout-slow" 2>&1
rc_slow=$?
wout_slow="$(find "$wiki_nh" -maxdepth 1 -name 'bench-*.md' -type f -print -quit)"
ok "a Wiki lookup that times out degrades to source=none and still exits 0" \
  '[ "$rc_slow" = 0 ] && [ -n "$wout_slow" ] && [ "$(grep -c "source=none" "$wout_slow")" = 5 ]'
ok "a timed-out Wiki leaves the queries counted as gate candidates" \
  'grep -q "^- unanswered (gate candidates): 5$" "$wout_slow"'

rm -f "$wiki_nh"/bench-*.md
HOME="$wiki_home" CCC_STATE_DIR="$wiki_state" NUNCHI_HOME="$wiki_nh" \
  NUNCHI_BENCH_QSET="$wiki_hooks/bench-qset.tsv" CCC_NODE=test-node \
  NUNCHI_BENCH_WIKI_CLI="$TMP/absent-wiki-agent" \
  bash "$wiki_hooks/bench.sh" > "$TMP/wout-abs" 2>&1
rc_abs=$?
wout_abs="$(find "$wiki_nh" -maxdepth 1 -name 'bench-*.md' -type f -print -quit)"
ok "a node without wiki-agent still completes the bench" \
  '[ "$rc_abs" = 0 ] && [ -n "$wout_abs" ] && [ "$(grep -c "source=none" "$wout_abs")" = 5 ]'

# A contaminated row must never be attributed to a layer: an INVALID answer
# says nothing about retrieval, and consulting the Wiki for it would inflate
# the Wiki's apparent coverage with rows the backend never even answered.
cat > "$wiki_hooks/nunchi.py" <<'PY'
#!/usr/bin/env python3
import sys
cmd = sys.argv[1]
sys.stdin.read()
if cmd == "synthesize":
    print("WIKI-ANSWER (should not be reached)")
elif cmd == "metrics":
    print("stub metrics")
else:
    print("Not logged in · Please run /login")
PY
run_wiki_bench
ok "an INVALID row is never attributed to the Wiki layer" \
  '[ "$(grep -c "status=INVALID reason=provider-failure source=none$" "$wout")" = 5 ] && ! grep -q "WIKI-ANSWER" "$wout"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
