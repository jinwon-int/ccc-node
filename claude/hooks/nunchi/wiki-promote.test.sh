#!/usr/bin/env bash
# Tests for claude/hooks/nunchi/wiki-promote.py (#1447, #1264 P3-8).
# Isolated via NUNCHI_DB/NUNCHI_HOME/CCC_STATE_DIR/CCC_MEMORY_CACHE_DIR
# overrides; no LLM, no network — the batch is deterministic by contract.
# Matrix mirrors the owner-approved design: shared-scope-only fan-out,
# mechanical eligibility (roster/kind/mutability/source_refs/G5),
# fail-closed privacy screen, 3-layer dedup (queue marker/seen/wiki-cache),
# CAP oldest-first, backpressure skip, dry-run default vs APPLY queue write,
# and a read-only fact store in every mode (DB checksum must not move).
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
unset CCC_NUNCHI_AUDIENCE_SCOPED CCC_NUNCHI_AUDIENCE_ROOT CCC_NUNCHI_SCOPED_CHILD
unset NUNCHI_WIKI_PROMOTE_APPLY NUNCHI_WIKI_PROMOTE_CAP NUNCHI_WIKI_PROMOTE_SERVICES

QUEUE="$CCC_STATE_DIR/wiki-candidates.md"
REPORT="$CCC_STATE_DIR/nunchi-wiki-promote-report.md"
SEEN="$NUNCHI_HOME/wiki-promoted.seen"
AUDIT="$NUNCHI_HOME/wiki-promote-audit.jsonl"

python3 "$NP" init >/dev/null

fresh_db() { # section isolation: the seen ledger and queue accumulate across
             # runs, so later sections start from a virgin store
  rm -f "$NUNCHI_DB"
  python3 "$NP" init >/dev/null
}

db_sha() { sha256sum "$NUNCHI_DB" | cut -d' ' -f1; }

seed() { # seed <observed> <kind> <fact> <created_at> <because|""> <refs|""> [review] [valid_to] [supersedes] [mutability] -> id
  python3 - "$@" <<'PY'
import json
import os
import sqlite3
import sys

def arg(i):
    return sys.argv[i] if len(sys.argv) > i else ""

observed, kind, fact, created = sys.argv[1:5]
because = arg(5) or None
refs = arg(6) or None
review = int(arg(7)) if arg(7) else 0
valid_to = arg(8) or None
supersedes = int(arg(9)) if arg(9) else None
# Absent 10th arg = static (the live-store default after the P1-4 backfill);
# an explicit empty arg inserts NULL (pre-backfill legacy shape).
mutability = arg(10) or None if len(sys.argv) > 10 else "static"
conn = sqlite3.connect(os.environ["NUNCHI_DB"])
cur = conn.execute(
    "INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,valid_to,"
    "supersedes,dedup,created_at,source_rank,review,because,source_refs,mutability)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("family-assistant", observed, kind, fact, "distill:test", created, valid_to,
     supersedes, json.dumps([observed, created, review]), created, 3,
     review, because, refs, mutability))
conn.commit()
print(cur.lastrowid)
PY
}

run_wp() { # run_wp [env assignments...] — runs one batch, prints stdout
  env "$@" python3 "$WP"
}

REFS='[{"type":"session","ref":"s-test"},{"type":"transcript","ref":"t.jsonl","sha256_8":"abcd1234"}]'
ELIGIBLE='nosuk 2코어 VPS 제약 테스트 사실'

# --- dry-run default: report only, queue never written ----------------------
ID1="$(seed nosuk decision "$ELIGIBLE" 2026-08-01T00:00:00+00:00 "2코어 제약" "$REFS")"
SHA_BEFORE="$(db_sha)"
out="$(run_wp)"
ok "dry-run default selects the eligible decision" \
  'grep -q "selected=1" <<<"$out" && grep -q "queued=0" <<<"$out"'
ok "dry-run writes no queue file" '[ ! -f "$QUEUE" ]'
ok "dry-run writes a mode-labeled report" 'grep -q "mode: \*\*dry-run\*\*" "$REPORT"'
ok "report stays body-free (fact text absent)" '! grep -q "2코어 VPS" "$REPORT"'
ok "dry-run leaves the fact store byte-identical" '[ "$(db_sha)" = "$SHA_BEFORE" ]'

# --- APPLY: queue write, seen ledger, still read-only -----------------------
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1)"
ok "APPLY queues the eligible decision" \
  'grep -q "queued=1" <<<"$out" && grep -q "^## \[CAND-1\]" "$QUEUE"'
ok "queue entry embeds the nunchi marker with fact id and hash" \
  'grep -q "<!-- nunchi-p3-8 fact#$ID1 h=[0-9a-f]\{12\} -->" "$QUEUE"'
ok "queue entry follows the wiki-queue schema fields" \
  'grep -q "^- suggested-path: .pages/nodes/nosuk/facts.md." "$QUEUE" && grep -q "^- status: pending" "$QUEUE" && grep -q "^- source-session: .s-test. (trigger=nunchi-weekly)" "$QUEUE"'
ok "queue entry carries source_refs provenance for the reviewer" \
  'grep -q "source_refs: \[" "$QUEUE"'
ok "APPLY still leaves the fact store byte-identical (read-only contract)" \
  '[ "$(db_sha)" = "$SHA_BEFORE" ]'
ok "seen ledger written with the fact hash" '[ -f "$SEEN" ] && awk "NF==4" "$SEEN" | grep -q .'
ok "promotion is audited body-free" \
  'grep -q "\"class\": \"promote-candidate\"" "$AUDIT" && ! grep -q "2코어 VPS" "$AUDIT"'

# --- dedup layer 1: seen ledger (no TTL) -------------------------------------
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1)"
ok "re-run is suppressed by the seen ledger" \
  'grep -q "selected=0" <<<"$out" && [ "$(grep -c "^## \[CAND-" "$QUEUE")" = 1 ]'

# --- mechanical eligibility gates --------------------------------------------
seed gwakga constraint "gwakga 제약 사실" 2026-08-02T00:00:00+00:00 "" "$REFS" 0 "" "" static
seed gwakga preference "gwakga 선호 사실" 2026-08-03T00:00:00+00:00 "" "$REFS" 0 "" "" static
seed gwakga observation "gwakga 관찰 사실" 2026-08-04T00:00:00+00:00 "" "$REFS" 0 "" "" live-check
seed nosuk decision "nosuk 근거 없는 결정 A" 2026-08-05T00:00:00+00:00 "" "$REFS" 0 "" "" static
seed nosuk decision "nosuk 근거 없는 결정 B" 2026-08-05T01:00:00+00:00 "" "$REFS" 0 "" "" static
seed nosuk decision "nosuk 소스 없는 결정" 2026-08-06T00:00:00+00:00 "왜" "" 0 "" "" static
seed nosuk decision "nosuk 검토 플래그 결정" 2026-08-07T00:00:00+00:00 "왜" "$REFS" 1 "" "" static
seed nosuk decision "nosuk 폐기된 결정" 2026-08-08T00:00:00+00:00 "왜" "$REFS" 0 2099-01-01T00:00:00+00:00 "" static
seed nosuk decision "nosuk 대체된 결정" 2026-08-09T00:00:00+00:00 "왜" "$REFS" 0 "" 1 static
seed "session:abc" decision "nosuk 세션 피어 결정" 2026-08-10T00:00:00+00:00 "왜" "$REFS" 0 "" "" static
out="$(run_wp)"
ok "none of the ineligible rows are selected" 'grep -q "selected=0" <<<"$out"'
ok "report counts the exclusion classes" \
  'grep -q "g5-reasonless-decision=2" "$REPORT" && grep -q "roster-miss=1" "$REPORT"'
ok "report names constraint and preference as kind-excluded" 'grep -q "kind-excluded=2" "$REPORT"'
ok "live-check observation row never queues" '! grep -q "gwakga 관찰" "$QUEUE"'
ok "SQL-gated rows (review/valid_to/supersedes/missing-refs) never queue" \
  '! grep -qE "소스 없는|검토 플래그|폐기된|대체된" "$QUEUE"'

# --- privacy screen (fail-closed) --------------------------------------------
fresh_db
seed nosuk decision "노숙 설정은 /root/.claude/state 에 저장된다" 2026-08-11T00:00:00+00:00 "경로 포함" "$REFS" 0 "" "" static
seed nosuk procedure "토큰은 sk-realtoken123 값을 쓴다" 2026-08-12T00:00:00+00:00 "" "$REFS" 0 "" "" static
out="$(run_wp)"
ok "path and token facts are fail-closed excluded (screen-privacy=2)" \
  'grep -q "screen-privacy=2" "$REPORT"'
ok "screened facts never reach the queue" \
  '! grep -q "root/.claude/state" "$QUEUE" && ! grep -q "sk-realtoken" "$QUEUE"'

# --- default service roster + env override -----------------------------------
fresh_db
rm -f "$QUEUE" "$SEEN"
seed searxng procedure "searxng 인스턴스 재시작 절차 사실" 2026-08-13T00:00:00+00:00 "" "$REFS" 0 "" "" static
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1)"
ok "registered-service fact (searxng) is queued" \
  'grep -q "queued=1" <<<"$out" && grep -q "pages/services/searxng.md" "$QUEUE"'
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1 NUNCHI_WIKI_PROMOTE_SERVICES=custom-svc)"
ok "service env override demotes default services to roster-miss" \
  'grep -q "selected=0" <<<"$out" && grep -q "roster-miss=1" "$REPORT"'
seed custom-svc decision "커스텀 서비스 결정 사실" 2026-08-14T00:00:00+00:00 "왜" "$REFS" 0 "" "" static
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1 NUNCHI_WIKI_PROMOTE_SERVICES=custom-svc)"
ok "overridden roster admits the custom service" \
  'grep -q "queued=1" <<<"$out" && grep -q "pages/services/custom-svc.md" "$QUEUE"'

# --- CAP: oldest first --------------------------------------------------------
rm -f "$QUEUE" "$SEEN"
fresh_db
for i in 1 2 3 4 5 6 7; do
  seed nosuk procedure "캡 테스트 절차 ${i}번 사실" "2026-07-1${i}T00:00:00+00:00" "" "$REFS" 0 "" "" static
done
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1 NUNCHI_WIKI_PROMOTE_CAP=5)"
ok "CAP caps a run at 5 queue entries" '[ "$(grep -c "^## \[CAND-" "$QUEUE")" = 5 ]'
ok "CAP keeps the OLDEST candidates (created_at asc, no starvation)" \
  '[ "$(grep -oE "fact#[0-9]+" "$QUEUE" | head -1)" = "$(grep -oE "fact#[0-9]+" "$QUEUE" | sort -t# -k2 -n | head -1)" ]'
ok "deferred remainder is reported" 'grep -q "cap-deferred=2" "$REPORT"'

# --- dedup layer 1b: queue markers (any status) -------------------------------
rm -f "$QUEUE" "$SEEN"
fresh_db
IDQ="$(seed nosuk procedure "큐 마커 중복 절차 사실" 2026-08-21T00:00:00+00:00 "" "$REFS" 0 "" "" static)"
IDQ2="$(seed nosuk procedure "큐 신규 절차 사실" 2026-08-22T00:00:00+00:00 "" "$REFS" 0 "" "" static)"
printf '\n## [CAND-99] 2026-08-20 — 기존 항목\n<!-- nunchi-p3-8 fact#%s h=deadbeefcafe -->\n- status: merged\n' "$IDQ" >> "$QUEUE"
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1)"
ok "marker id in queue (any status) blocks re-queueing" \
  'grep -q "queue-dupe-id=1" "$REPORT" && [ "$(grep -c "fact#'$IDQ'" "$QUEUE")" = 1 ]'
ok "the non-marked fact still queues" 'grep -q "fact#$IDQ2" "$QUEUE"'

# --- dedup layer 2: wiki cache substring --------------------------------------
rm -f "$QUEUE" "$SEEN"
fresh_db
seed nosuk procedure "위키 캐시 중복 절차 사실입니다" 2026-08-23T00:00:00+00:00 "" "$REFS" 0 "" "" static
printf 'pages/nodes/nosuk.md: 위키 캐시 중복 절차 사실입니다 (정본)\n' > "$CCC_MEMORY_CACHE_DIR/wiki.txt"
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1)"
ok "wiki-cache substring hit skips the candidate" \
  'grep -q "wiki-cache-hit=1" "$REPORT" && [ ! -f "$QUEUE" ]'

# --- backpressure --------------------------------------------------------------
rm -f "$QUEUE" "$SEEN"
{
  echo "# Wiki Candidates Queue"
  for i in $(seq 1 21); do
    printf '\n## [CAND-%d] 2026-08-01 — 역압 %d\n- status: pending\n' "$i" "$i"
  done
} > "$QUEUE"
out="$(run_wp NUNCHI_WIKI_PROMOTE_APPLY=1)"
ok "backpressure (pending > 20) skips the whole run" \
  'grep -q "backpressure-skip" <<<"$out" && [ "$(grep -c "^## \[CAND-" "$QUEUE")" = 21 ]'
ok "backpressure skip is audited" 'grep -q "backpressure-skip" "$AUDIT"'

# --- scoped fan-out: shared only, private never opened -------------------------
rm -rf "$QUEUE" "$SEEN" "$TMP/state-scoped"
ROOT="$TMP/audiences"
mkdir -p -m 700 "$ROOT/shared/nunchi" "$ROOT/private-0123456789abcdef0123456789abcdef/nunchi"
# mkdir -p -m applies to the deepest component only (SC2174): the canonical
# scope enumerator rejects ANY group/other bit, so harden every level — CI
# runners use umask 022 and root shells don't (measured 2026-09-04, #1462).
chmod 700 "$ROOT" "$ROOT/shared" "$ROOT/private-0123456789abcdef0123456789abcdef" \
          "$ROOT/shared/nunchi" "$ROOT/private-0123456789abcdef0123456789abcdef/nunchi"
export CCC_NUNCHI_AUDIENCE_ROOT="$ROOT" CCC_NUNCHI_AUDIENCE_SCOPED=1
seed_scoped() { # seed_scoped <scope> <label>
  local scope="$1" label="$2" db="$ROOT/$1/nunchi/facts.db"
  NUNCHI_DB="$db" NUNCHI_HOME="$ROOT/$scope/nunchi" \
  CCC_STATE_DIR="$TMP/state-scoped" CCC_MEMORY_CACHE_DIR="$TMP/cache" \
    python3 "$NP" init >/dev/null
  NUNCHI_DB="$db" python3 - "$db" "$label" <<'PY'
import os
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.execute(
    "INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,valid_to,"
    "supersedes,dedup,created_at,source_rank,review,because,source_refs,mutability)"
    " VALUES('family-assistant','nosuk','decision',?,'e',"
    "'2026-08-01T00:00:00+00:00',NULL,NULL,'dd','2026-08-01T00:00:00+00:00',3,0,"
    "'왜','[{\"type\":\"session\",\"ref\":\"s-1\"}]','static')",
    (f"{sys.argv[2]} 스코프 결정 사실",))
conn.commit()
PY
}
seed_scoped shared shared
seed_scoped private-0123456789abcdef0123456789abcdef private-0123
SQUEUE="$TMP/state-scoped/wiki-candidates.md"
out="$(NUNCHI_WIKI_PROMOTE_APPLY=1 CCC_STATE_DIR="$TMP/state-scoped" CCC_MEMORY_CACHE_DIR="$TMP/cache" python3 "$WP")"
ok "scoped parent fans out to the shared scope only" \
  'grep -q "queued=1" <<<"$out" && [ "$(grep -c "^## \[CAND-" "$SQUEUE")" = 1 ]'
ok "shared-scope entry is the shared fact" 'grep -q "shared 스코프 결정 사실" "$SQUEUE"'
ok "private-scope fact never reaches the queue" '! grep -q "private-0123" "$SQUEUE"'
ok "shared-scope report written next to the queue" \
  '[ -f "$TMP/state-scoped/nunchi-wiki-promote-report.md" ]'

echo "----------------------------------------"
echo "wiki-promote.test.sh: $pass pass, $fail fail"
# validate-harness's suite_summary greps for exactly this final line.
printf 'PASS=%d FAIL=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
