#!/usr/bin/env bash
# Tests for claude/hooks/nunchi/wiki-promote.py (#1447, #1264 P3-8).
# Isolated via NUNCHI_DB/NUNCHI_HOME/CCC_STATE_DIR/CCC_MEMORY_CACHE_DIR
# overrides; the Wiki cache and candidates queue always point inside the temp
# tree so no test can touch real node state, and no network or LLM backend is
# involved (the batch is fully mechanical). Matrix mirrors the owner-approved
# contract: shared-scope-only privacy, roster/kind/G5/mutability/source_refs
# gates, body screen, 3-layer dedup, cap + oldest-first, backpressure,
# dry-run vs APPLY, permanent seen ledger, read-only facts.db, body-free
# audit, scoped fan-out (shared child only, private never opened).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WP="$HERE/wiki-promote.py"
NP="$HERE/nunchi.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"
# Inherited CCC_/NUNCHI_ state reaches the batch (scoped fan-out!) and costs
# assertions — same lesson as #1023.
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

mkdir -p "$TMP/nunchi-home" "$TMP/state" "$TMP/cache"
export NUNCHI_DB="$TMP/nunchi-home/facts.db"
export NUNCHI_HOME="$TMP/nunchi-home"
export CCC_STATE_DIR="$TMP/state"
export CCC_MEMORY_CACHE_DIR="$TMP/cache"
unset CCC_NUNCHI_AUDIENCE_SCOPED CCC_NUNCHI_AUDIENCE_ROOT CCC_NUNCHI_SCOPED_CHILD \
      CCC_NUNCHI_AUDIENCE_SCOPE CCC_NUNCHI_AUDIENCE_KIND
unset NUNCHI_WIKI_PROMOTE_APPLY NUNCHI_WIKI_PROMOTE_CAP NUNCHI_WIKI_PROMOTE_BACKPRESSURE \
      NUNCHI_WIKI_PROMOTE_SERVICES

python3 "$NP" init >/dev/null

QUEUE="$TMP/state/wiki-candidates.md"
REPORT="$TMP/state/nunchi-wiki-promote-report.md"
AUDIT="$NUNCHI_HOME/wiki-promote-audit.jsonl"
SEEN="$NUNCHI_HOME/wiki-promoted.seen"

# Provenance fixtures (P1-3 shapes from nunchi.py _source_refs).
REFS_OK='[{"type":"session","ref":"sess-42"},{"type":"transcript","ref":"/tmp/t.jsonl","sha256_8":"abcd1234"}]'
REFS_QUOTE='[{"type":"quote","ref":"capped model citation"}]'

SEED_N=0
seed() { # seed <observed> <kind> <fact> <created> <mutability> <source_refs> <because> [review] [supersedes]
  SEED_N=$((SEED_N + 1))
  python3 - "$@" "$SEED_N" <<'PY'
import os, sqlite3, sys
args = sys.argv[1:]
n = args[-1] + ":" + os.urandom(4).hex()
observed, kind, fact, created, mutability, refs, because = args[:7]
review = int(args[7]) if len(args) > 8 and args[7] else 0
supersedes = int(args[8]) if len(args) > 9 and args[8] else None
c = sqlite3.connect(os.environ["NUNCHI_DB"])
cur = c.execute(
    "INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,valid_to,"
    "supersedes,dedup,created_at,source_rank,review,because,source_refs,mutability)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("family-assistant", observed, kind, fact, "distill:test", created, None,
     supersedes, f"seed:{n}:{created}", created, 1, review, because or None,
     refs or None, mutability or None))
c.commit()
print(cur.lastrowid)
PY
}

run_wp() { # run_wp [extra-env assignments...]
  env "$@" python3 "$WP"
}

audit_ids() { # verdict -> "id id id" lines from the body-free audit
  python3 - "$1" "$AUDIT" <<'PY'
import json, sys
verdict = sys.argv[1]
try:
    rows = [json.loads(l) for l in open(sys.argv[2]) if l.strip()]
except OSError:
    rows = []
print(" ".join(str(r["id"]) for r in rows if r.get("verdict") == verdict))
PY
}

reset_db() {
  rm -f "$NUNCHI_DB" "$QUEUE" "$REPORT" "$AUDIT" "$SEEN"
  python3 "$NP" init >/dev/null
}

PROC="웹훅 재시도 절차는 지수 백오프를 따른다"

# ---- 1. dry-run default: report only, queue never written -------------------
reset_db
id1="$(seed jingun procedure "$PROC" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
out="$(run_wp 2>&1)"; rc=$?
ok "dry-run is the default and exits 0" '[ "$rc" = 0 ]'
ok "dry-run writes no queue file" '[ ! -f "$QUEUE" ]'
ok "dry-run writes the report with a candidate" \
  '[ -f "$REPORT" ] && grep -q "mode: \*\*dry-run\*\*" "$REPORT" && grep -q "candidate" "$REPORT"'
ok "dry-run audit marks the row a candidate without applied flag" \
  '[ "$(audit_ids candidate)" = "$id1" ] && ! grep -q "\"applied\": true" "$AUDIT"'

# ---- 2. roster gate: fleet nodes + approved services only -------------------
reset_db
u1="$(seed seo-jin-on procedure "유저 피어 관측은 교환 대상이 아니다" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
s1="$(seed session:abc-123 procedure "세션 관측도 후보가 아니다" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
x1="$(seed atlas procedure "비로스터 가상 노드" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
v1="$(seed mempalace procedure "mempalace 스냅샷 재생성은 하루 1회" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
run_wp >/dev/null 2>&1
ok "user-peer observed is roster-rejected (unscoped privacy gate)" \
  'grep -q "#$u1 | skip | observed-not-fleet" "$REPORT"'
ok "session: observed is roster-rejected" \
  'grep -q "#$s1 | skip | observed-not-fleet" "$REPORT"'
ok "unknown slug is roster-rejected" 'grep -q "#$x1 | skip | observed-not-fleet" "$REPORT"'
ok "approved default service (mempalace) is a candidate" \
  '[ "$(audit_ids candidate)" = "$v1" ]'
ok "service roster is overridable via env" \
  'env NUNCHI_WIKI_PROMOTE_SERVICES=atlas python3 "$WP" >/dev/null 2>&1 && grep -q "#$x1 | candidate" "$REPORT" && ! grep -q "#$v1 | candidate" "$REPORT"'

# ---- 3. kind gates (constraint excluded — owner decision point 2) -----------
reset_db
k1="$(seed jingun preference "선호: 간결한 보고" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
k2="$(seed jingun constraint "제약: 노드 로컬 규칙" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
k3="$(seed jingun observation "관찰: 시점 데이터" 2026-08-01T00:00:00+00:00 live-check "$REFS_OK" '')"
k4="$(seed jingun context "맥락: 현재 진행 작업" 2026-08-01T00:00:00+00:00 live-check "$REFS_OK" '')"
k5="$(seed jingun procedure "교환 가능한 절차 fact" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
k6="$(seed jingun decision "A안을 채택했다" 2026-08-01T00:00:00+00:00 static "$REFS_OK" "벤치 결과가 근거")"
k7="$(seed jingun decision "A안 단독 채택(사유 미기재)" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
k8="$(seed jingun decision "본문에 근거: 벤치 차이" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
run_wp >/dev/null 2>&1
ok "preference/constraint/observation/context are kind-rejected" \
  'grep -q "#$k1 | skip | kind-not-exchangeable" "$REPORT" && grep -q "#$k2 | skip | kind-not-exchangeable" "$REPORT" && grep -q "#$k3 | skip | kind-not-exchangeable" "$REPORT" && grep -q "#$k4 | skip | kind-not-exchangeable" "$REPORT"'
ok "procedure is a candidate" 'grep -q "#$k5 | candidate" "$REPORT"'
ok "decision with because is a candidate" 'grep -q "#$k6 | candidate" "$REPORT"'
ok "reasonless decision is G5-rejected (shared semantic contract)" \
  'grep -q "#$k7 | skip | g5-reasonless-decision" "$REPORT"'
ok "decision with inline reason passes G5" 'grep -q "#$k8 | candidate" "$REPORT"'

# ---- 4. mutability gate: static only, stored must agree with derived --------
reset_db
m1="$(seed jingun fact "legacy fact kind" 2026-08-01T00:00:00+00:00 live-check "$REFS_OK" '')"
m2="$(seed jingun decision "드리프트된 저장값" 2026-08-01T00:00:00+00:00 live-check "$REFS_OK" "근거 있음")"
m3="$(seed jingun fact "저장 static 파생 live-check" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
m4="$(seed jingun procedure "mutability 미기재 레거시" 2026-08-01T00:00:00+00:00 '' "$REFS_OK" '')"
run_wp >/dev/null 2>&1
ok "legacy fact kind dies at the mutability gate (retag is the migration path)" \
  'grep -q "#$m1 | skip | mutability-not-static" "$REPORT"'
ok "stored live-check on a static kind is mutability-rejected" \
  'grep -q "#$m2 | skip | mutability-not-static" "$REPORT"'
ok "stored static disagreeing with derived live-check is drift (fail-closed)" \
  'grep -q "#$m3 | skip | mutability-drift" "$REPORT"'
ok "missing mutability is rejected" 'grep -q "#$m4 | skip | mutability-not-static" "$REPORT"'

# ---- 5. source_refs gate (P1-3 traceability) --------------------------------
reset_db
r1="$(seed jingun procedure "근거 없는 레거시 행" 2026-08-01T00:00:00+00:00 static '' '')"
r2="$(seed jingun procedure "인용만 있는 행" 2026-08-01T00:00:00+00:00 static "$REFS_QUOTE" '')"
r3="$(seed jingun procedure "파싱 불가 refs" 2026-08-01T00:00:00+00:00 static 'not-json' '')"
r4="$(seed jingun procedure "세션+전사 refs 행" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
run_wp >/dev/null 2>&1
ok "missing source_refs is rejected (no review without backtrack)" \
  'grep -q "#$r1 | skip | source-refs-missing" "$REPORT"'
ok "quote-only source_refs is untraceable-rejected" \
  'grep -q "#$r2 | skip | source-refs-untraceable" "$REPORT"'
ok "unparsable source_refs is rejected" 'grep -q "#$r3 | skip | source-refs-missing" "$REPORT"'
ok "session+transcript refs make the row a candidate" \
  'grep -q "#$r4 | candidate" "$REPORT"'

# ---- 5b. lifecycle gate: review flag / supersede link -----------------------
reset_db
l1="$(seed jingun procedure "검토 플래그가 열린 행" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '' 1 '')"
l2="$(seed jingun procedure "supersede된 행" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '' 0 3)"
run_wp >/dev/null 2>&1
ok "review-flagged and superseded rows never surface as candidates" \
  '! grep -q "#$l1" "$REPORT" && ! grep -q "#$l2" "$REPORT"'

# ---- 6. body screen: fail-closed exclusion, never a rewrite -----------------
reset_db
b1="$(seed jingun procedure "설정은 /root/.ssh/config에 있다" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
b2="$(seed jingun procedure "배포 토큰은 ghp_Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9로 회전했다" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
b3="$(seed jingun decision "A안 채택" 2026-08-01T00:00:00+00:00 static "$REFS_OK" "근거: sk-abcdefghijklmnopqrst키 형태의 자격증명 노출")"
run_wp >/dev/null 2>&1
ok "local path in fact body is screened out" \
  'grep -q "#$b1 | skip | body-screen:local-path" "$REPORT"'
ok "token-like string in fact body is screened out" \
  'grep -q "#$b2 | skip | body-screen:token-like" "$REPORT"'
ok "because text is screened too" \
  'grep -q "#$b3 | skip | body-screen:token-like" "$REPORT"'
ok "screened bodies never reach the audit" \
  '! grep -q "ghp_Aa1Bb2Cc3Dd4" "$AUDIT"'

# ---- 7. dedup: wiki cache, in-queue markers, permanent seen ledger ----------
reset_db
LONG="mempalace 주간 스냅샷 재생성 절차는 토요일 새벽에 도는 것이 정본이다"
cache_dir="$CCC_MEMORY_CACHE_DIR"; mkdir -p "$cache_dir"
d1="$(seed mempalace procedure "$LONG" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
printf '무관한 위키 내용입니다.\n%s\n' "$LONG" > "$cache_dir/wiki.txt"
run_wp >/dev/null 2>&1
ok "fact already documented in the wiki cache is skipped" \
  'grep -q "#$d1 | skip | wiki-cache-hit" "$REPORT"'
SHORT="아주 짧은 사실"
d2="$(seed mempalace procedure "$SHORT" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
run_wp >/dev/null 2>&1
ok "short facts never trigger the substring layer (too weak to be distinctive)" \
  'grep -q "#$d2 | candidate" "$REPORT"'
rm -f "$cache_dir/wiki.txt"

d3="$(seed gongyung procedure "이미 큐에 있는 절차" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
HASH="$(python3 - "$LONG" <<'PY'
import hashlib, re, sys
print(hashlib.sha256(re.sub(r"\s+", " ", sys.argv[1].strip().lower()).encode()).hexdigest()[:12])
PY
)"
mkdir -p "$CCC_STATE_DIR"
printf '## [CAND-3] 2026-09-01 — gongyung: 기존 후보\n- status: pending\n<!-- nunchi-p3-8 fact#%s scope=unscoped hash=%s -->\n' "$d3" "$HASH" > "$QUEUE"
run_wp >/dev/null 2>&1
ok "fact id already pending in the queue is deduped (marker layer)" \
  'grep -q "#$d3 | skip | already-queued" "$REPORT"'

d4="$(seed nosuk procedure "원장에 기록된 절차" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
printf '%s deadbeefcafe 1756684800\n' "$d4" > "$SEEN"
run_wp >/dev/null 2>&1
ok "seen-ledger id suppresses re-promotion permanently" \
  'grep -q "#$d4 | skip | already-promoted" "$REPORT"'

# ---- 8. APPLY: queue schema, numbering continuation, seen ledger, read-only --
reset_db
a1="$(seed jingun procedure "APPLY 대상 절차 하나" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
a2="$(seed jingun decision "APPLY 대상 결정" 2026-08-01T00:00:00+00:00 static "$REFS_OK" "근거: 벤치 우위")"
DBSUM_BEFORE="$(sha256sum "$NUNCHI_DB" | cut -d' ' -f1)"
printf '## [CAND-7] 2026-09-01 — 기존 distill 후보\n- status: pending\n- summary: 선행 항목\n' > "$QUEUE"
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1 2>&1)"; rc=$?
ok "APPLY run exits 0 and queues" '[ "$rc" = 0 ] && grep -q "queued=2" <<<"$out"'
ok "CAND numbering continues the distill queue's id space" \
  'grep -q "## \[CAND-8\]" "$QUEUE" && grep -q "## \[CAND-9\]" "$QUEUE"'
ok "entries follow the wiki-queue schema (pending, suggested-path, marker)" \
  'grep -q "^- status: pending" "$QUEUE" && grep -q "suggested-path: \`pages/nodes/jingun/\`" "$QUEUE" && grep -q "nunchi-p3-8 fact#$a1 scope=unscoped hash=[0-9a-f]\{12\}" "$QUEUE"'
ok "decision entry carries its because line" \
  "grep -q -- '- because: 근거: 벤치 우위' \"\$QUEUE\""
ok "source-refs rendering lands in the entry" 'grep -q "source-refs: session: sess-42" "$QUEUE"'
ok "APPLY writes the permanent seen ledger" \
  '[ "$(wc -l < "$SEEN" | tr -d " ")" = "2" ] && grep -qE "^$a1 [0-9a-f]{12} [0-9-]+T[0-9:]+\+00:00$" "$SEEN"'
ok "facts.db is never mutated (read-only contract)" \
  '[ "$(sha256sum "$NUNCHI_DB" | cut -d" " -f1)" = "$DBSUM_BEFORE" ]'

# APPLY onto a NON-existent queue bootstraps the canonical wiki-queue header.
rm -f "$QUEUE" "$SEEN"
a3="$(seed seoseo procedure "헤더 부트스트랩용 절차" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
run_wp NUNCHI_WIKI_PROMOTE_APPLY=1 >/dev/null 2>&1
ok "APPLY onto a missing queue bootstraps the canonical wiki-queue header" \
  '[ "$(head -1 "$QUEUE")" = "# Wiki Candidates Queue (auto-generated by distill; review with \`/wiki-record\`)" ]'

# Re-seed the pre-existing CAND-7 queue for the idempotence check.
printf '## [CAND-7] 2026-09-01 — 기존 distill 후보\n- status: pending\n- summary: 선행 항목\n' > "$QUEUE"
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1 2>&1)"
ok "re-APPLY is idempotent (seen ledger: no duplicate entries)" \
  '[ "$(grep -c "^## \[CAND-" "$QUEUE")" = "1" ] && grep -q "#$a1 | skip | already-promoted" "$REPORT"'

# ---- 9. CAP + oldest-first ---------------------------------------------------
reset_db
cids=""
for i in 1 2 3 4 5 6 7; do
  cid="$(seed gwakga procedure "상한 측정용 절차 $i" "2026-08-0${i}T00:00:00+00:00" static "$REFS_OK" '')"
  cids="$cids $cid"
done
out="$(run_wp NUNCHI_WIKI_PROMOTE_CAP=2 NUNCHI_WIKI_PROMOTE_APPLY=1 2>&1)"
first_two="$(python3 - "$QUEUE" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
print(" ".join(re.findall(r"nunchi-p3-8 fact#(\d+) scope=", text)[:2]))
PY
)"
ok "CAP truncates to the configured limit" 'grep -q "eligible candidates: 7 (cap 2, overflow 5)" "$REPORT"'
expected="$(echo $cids | cut -d' ' -f1-2)"
ok "CAP keeps the OLDEST facts (created_at ASC — no starvation)" \
  '[ "$first_two" = "$expected" ]'
ok "overflow rows are recorded as cap-overflow skips" \
  'grep -c "| skip | cap-overflow |" "$REPORT" | grep -q "^5$"'

# ---- 10. backpressure --------------------------------------------------------
reset_db
bp="$(seed jingun procedure "역압 하에서도 후보는 된다" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
python3 - <<'PY'
import os
path = os.path.join(os.environ["CCC_STATE_DIR"], "wiki-candidates.md")
with open(path, "w") as fh:
    for i in range(21):
        fh.write(f"## [CAND-{900+i}] 2026-09-01 — bp {i}\n- status: pending\n")
PY
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1 2>&1)"; rc=$?
ok "21 pending entries skip the whole run even in APPLY" \
  '[ "$rc" = 0 ] && grep -q "SKIPPED backpressure" <<<"$out" && ! grep -q "fact#$bp" "$QUEUE"'
python3 - <<'PY'
import os
path = os.path.join(os.environ["CCC_STATE_DIR"], "wiki-candidates.md")
lines = open(path).read().splitlines(keepends=True)
open(path, "w").writelines(lines[:-2])
PY
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1 2>&1)"
ok "at exactly the threshold (20 pending) the run proceeds" \
  'grep -q "fact#$bp" "$QUEUE"'

# ---- 11. scoped fan-out: shared child only, private never opened -------------
reset_db
AUD="$TMP/aud"
mkdir -m 700 -p "$AUD/shared/nunchi" "$AUD/private-b3362e2106be28b2f3221f38d9624b84/nunchi"
chmod 700 "$AUD" "$AUD/private-b3362e2106be28b2f3221f38d9624b84"
NUNCHI_DB="$AUD/shared/nunchi/facts.db" python3 "$NP" init >/dev/null
NUNCHI_DB="$AUD/private-b3362e2106be28b2f3221f38d9624b84/nunchi/facts.db" python3 "$NP" init >/dev/null
sh1="$(NUNCHI_DB="$AUD/shared/nunchi/facts.db" seed jingun procedure "shared 스코프의 플릿 절차" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
NUNCHI_DB="$AUD/private-b3362e2106be28b2f3221f38d9624b84/nunchi/facts.db" \
  seed jingun procedure "id 확보용 더미" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '' >/dev/null
pv1="$(NUNCHI_DB="$AUD/private-b3362e2106be28b2f3221f38d9624b84/nunchi/facts.db" seed jingun procedure "private 스코프의 절차 — 절대 승격 금지" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
out="$(env CCC_NUNCHI_AUDIENCE_SCOPED=1 CCC_NUNCHI_AUDIENCE_ROOT="$AUD" \
  NUNCHI_WIKI_PROMOTE_APPLY=1 python3 "$WP" 2>&1)"; rc=$?
ok "scoped parent run exits 0" '[ "$rc" = 0 ]'
ok "shared-scope fact is queued with the shared scope tag" \
  "grep -q 'nunchi-p3-8 fact#$sh1 scope=shared' \"\$QUEUE\""
ok "private-scope fact NEVER reaches the queue (physical boundary)" \
  '! grep -q "fact#$pv1" "$QUEUE"'
SHARED_AUDIT="$AUD/shared/nunchi/wiki-promote-audit.jsonl"
ok "private scope never appears in the audit" \
  '! grep -q "private-" "$SHARED_AUDIT"'
ok "audit records the shared child run" 'grep -q "\"scope\": \"shared\"" "$SHARED_AUDIT"'

# No shared child at all -> clean no-op.
rm -rf "$AUD/shared"
out="$(env CCC_NUNCHI_AUDIENCE_SCOPED=1 CCC_NUNCHI_AUDIENCE_ROOT="$AUD" python3 "$WP" 2>&1)"; rc=$?
ok "missing shared scope is a clean no-op" \
  '[ "$rc" = 0 ] && grep -q "no canonical shared scope" <<<"$out"'

# ---- 12. flock + body-free audit ----------------------------------------------
reset_db
f1="$(seed dungae procedure "플록 대상 절차" 2026-08-01T00:00:00+00:00 static "$REFS_OK" '')"
python3 - <<'PY' &
import fcntl, os, time
fh = open(os.path.join(os.environ["NUNCHI_HOME"], ".wiki-promote.lock"), "w")
fcntl.flock(fh, fcntl.LOCK_EX)
time.sleep(3)
PY
locker=$!
sleep 0.5
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1 2>&1)"
wait "$locker"
ok "locked run prints the skip message" 'grep -q "another run holds the lock" <<<"$out"'
ok "locked run queued nothing" '[ ! -f "$QUEUE" ]'

run_wp NUNCHI_WIKI_PROMOTE_APPLY=1 >/dev/null 2>&1
ok "audit is body-free (no fact text, only verdict/reason/id)" \
  '! grep -q "플록 대상" "$AUDIT" && grep -q "\"verdict\": \"candidate\"" "$AUDIT"'

printf 'PASS=%d FAIL=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
