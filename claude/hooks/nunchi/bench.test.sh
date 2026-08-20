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

# ---------------------------------------------------------------------------
# #827 — audience-scoped dispatch.
#
# On a scoped node ingest writes only to <audience-root>/<scope>/nunchi/facts.db.
# bench.sh had no scope awareness and always scored $HOME/.nunchi, so from the
# moment scoping was enabled it graded a frozen store: soonwook's bench read 52
# facts last written 2026-07-30 while ingest held 58 written 2026-08-19. Four of
# twelve fleet nodes were affected, which invalidates cross-node comparison of
# the Phase 2 parity gate.

# The enumerator is duplicated across three scripts by repo convention. Nothing
# guarded that, and a security-relevant path validator silently drifting apart
# is worse than the duplication itself.
extract_scope_enum() {  # <script path>
  awk '/"\$audience_root" "\$max_scopes" <<.PY.$/{f=1;next} f&&/^PY$/{exit} f' "$1"
}
extract_scope_enum "$ROOT/claude/hooks/nunchi/bench.sh"             > "$TMP/enum-bench"
extract_scope_enum "$ROOT/claude/hooks/nunchi/piri-feed.sh"         > "$TMP/enum-feed"
extract_scope_enum "$ROOT/claude/hooks/nunchi/mempalace-refresh.sh" > "$TMP/enum-refresh"
ok "the scope enumerator is non-empty in all three scripts" \
  '[ -s "$TMP/enum-bench" ] && [ -s "$TMP/enum-feed" ] && [ -s "$TMP/enum-refresh" ]'
ok "bench.sh's scope enumerator stays byte-identical to piri-feed.sh's" \
  'cmp -s "$TMP/enum-bench" "$TMP/enum-feed"'
ok "bench.sh's scope enumerator stays byte-identical to mempalace-refresh.sh's" \
  'cmp -s "$TMP/enum-bench" "$TMP/enum-refresh"'

s_home="$TMP/scoped"
s_hooks="$s_home/.claude/hooks/nunchi"
s_state="$s_home/.claude/state"
s_unscoped="$s_home/.nunchi"
s_root="$s_home/audiences"
s_priv="$s_root/private-$(printf '0%.0s' $(seq 32) | tr '0' 'a')"
s_shared="$s_root/shared"
mkdir -p "$s_hooks" "$s_state" "$s_unscoped" "$s_priv/nunchi" "$s_shared/nunchi"
chmod 700 "$s_root" "$s_priv" "$s_shared"
cp "$ROOT/claude/hooks/nunchi/bench.sh" "$s_hooks/bench.sh"
chmod 700 "$s_hooks/bench.sh"
printf 'on' > "$s_state/nunchi.mode"
cp "$hooks/bench-qset.tsv" "$s_hooks/bench-qset.tsv"
# Both scopes have ingested; only these two may be benched.
: > "$s_priv/nunchi/facts.db"
: > "$s_shared/nunchi/facts.db"
# A scope that has never ingested must be skipped rather than graded as a
# sheet of retrieval failures.
mkdir -p "$s_root/private-$(printf 'b%.0s' $(seq 32))/nunchi"
chmod 700 "$s_root/private-$(printf 'b%.0s' $(seq 32))"

# Echo the DB the child was pointed at so the assertion can prove the dispatch
# rebound it, not merely that some bench file appeared.
cat > "$s_hooks/nunchi.py" <<'PY'
#!/usr/bin/env python3
import os, sys
cmd = sys.argv[1]
sys.stdin.read()
if cmd == "metrics":
    print("stub metrics")
else:
    print("DBSEEN=" + os.environ.get("NUNCHI_DB", "unset"))
PY

HOME="$s_home" CCC_STATE_DIR="$s_state" NUNCHI_HOME="$s_unscoped" \
  NUNCHI_BENCH_QSET="$s_hooks/bench-qset.tsv" CCC_NODE=scoped-node \
  CCC_NUNCHI_AUDIENCE_SCOPED=1 CCC_NUNCHI_AUDIENCE_ROOT="$s_root" \
  NUNCHI_BENCH_WIKI_CLI=/nonexistent \
  bash "$s_hooks/bench.sh" > "$TMP/s-stdout" 2> "$TMP/s-stderr"
s_rc=$?
s_priv_out="$(find "$s_priv/nunchi" -maxdepth 1 -name 'bench-*.md' -type f -print -quit)"
s_shared_out="$(find "$s_shared/nunchi" -maxdepth 1 -name 'bench-*.md' -type f -print -quit)"
s_unscoped_out="$(find "$s_unscoped" -maxdepth 1 -name 'bench-*.md' -type f -print -quit)"

ok "scoped dispatch exits cleanly" '[ "$s_rc" = 0 ]'
ok "each ingested scope gets its own bench sheet" \
  '[ -n "$s_priv_out" ] && [ -n "$s_shared_out" ]'
ok "the frozen unscoped store is no longer benched on a scoped node" \
  '[ -z "$s_unscoped_out" ]'
ok "the private scope is graded against its own DB, not \$HOME/.nunchi" \
  'grep -q "DBSEEN=$s_priv/nunchi/facts.db" "$s_priv_out"'
ok "the shared scope is graded against its own DB" \
  'grep -q "DBSEEN=$s_shared/nunchi/facts.db" "$s_shared_out"'
ok "a scope that has never ingested is skipped, not scored as failure" \
  '[ -z "$(find "$s_root/private-$(printf "b%.0s" $(seq 32))/nunchi" -name "bench-*.md" -print -quit)" ]'
ok "each sheet records its opaque scope and kind, and no fact bodies" \
  'grep -q "node=scoped-node scope=${s_priv##*/} kind=private" "$s_priv_out" \
   && grep -q "node=scoped-node scope=shared kind=shared" "$s_shared_out"'

# The unscoped path must keep working unchanged for the 8 non-scoped nodes.
HOME="$s_home" CCC_STATE_DIR="$s_state" NUNCHI_HOME="$s_unscoped" \
  NUNCHI_BENCH_QSET="$s_hooks/bench-qset.tsv" CCC_NODE=plain-node \
  NUNCHI_BENCH_WIKI_CLI=/nonexistent \
  bash "$s_hooks/bench.sh" > "$TMP/p-stdout" 2>&1
p_rc=$?
p_out="$(find "$s_unscoped" -maxdepth 1 -name 'bench-*.md' -type f -print -quit)"
ok "an unscoped node still benches \$NUNCHI_HOME and emits no scope label" \
  '[ "$p_rc" = 0 ] && [ -n "$p_out" ] && grep -q "node=plain-node$" "$p_out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
