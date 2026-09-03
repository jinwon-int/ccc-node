#!/usr/bin/env bash
# Assemble ranking fixtures (#1264 P2-7 follow-up) — the contract ground that
# any future ranking-policy change (weights, thresholds, ordering) must win
# against ("랭킹 변경은 fixture 승부 후 반영"). Each fixture pins ONE clause of
# the P2-7 ranking contract on a per-scenario isolated database, so ordering
# assertions are exact and independent of nunchi.test.sh's shared state:
#
#   1. header warnings always present (backend/review/G5 surface)
#   2. constraints always, never dropped for budget (G4), before facts
#   3. hint FTS matches (bm25 via the P2-6-widened search) above recency
#   4. recency (id DESC) fills the tail; hint matches never duplicated
#   5. byte budget bounds the fact block best-effort (skip-then-fill, never
#      head-truncation); first fact always survives; total ≤ budget
#   6. observation excluded; ⟳ live-check markers + legend preserved inline
#   7. P2-6 hallways 1-hop widening reaches facts that share an entity, not
#      a keyword
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NP="$HERE/nunchi.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

# ---- fixture helpers --------------------------------------------------------
# Each scenario gets its own DB: exact id ordering (= recency) without
# cross-scenario interference. `seed` mirrors nunchi.test.sh's payload shape.
FIX_SEED=0
fixture_db() { # <name> — fresh isolated DB; prints nothing
  FIX="$TMP/$1"; mkdir -p "$FIX"
  export NUNCHI_DB="$FIX/facts.db" NUNCHI_SNAPSHOT="$FIX/snapshot.md"
  export NUNCHI_HOME="$FIX" CCC_STATE_DIR="$FIX/state"
  python3 "$NP" init >/dev/null
}
seed() { # <kind> <subject> <text> — ingest one fact; id order = call order
  FIX_SEED=$((FIX_SEED + 1))
  printf '{"session_id":"fx%d","distilled_at":"2026-09-01T00:00:00+00:00","honcho":[{"kind":"%s","subject":"%s","text":"%s"}]}' \
    "$FIX_SEED" "$1" "$2" "$3" | python3 "$NP" ingest - >/dev/null
}
sql() { # direct row control (constraint/review/expiry) — SQL as first argument
  python3 - "$NUNCHI_DB" "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.executescript(sys.argv[2])
c.commit()
PY
}
run_asm() { # <budget> [hint] — sets ASM
  ASM="$(python3 "$NP" assemble --budget "$1" ${2:+"--hint" "$2"} 2>&1)"
}
line_of() { # <pattern> -> 1-based line in ASM (empty if absent)
  grep -n -- "$1" <<<"$ASM" | head -1 | cut -d: -f1
}

# ---- F-01 relevance beats recency ------------------------------------------
# Contract 3: an OLDER fact whose body matches --hint ranks above newer
# non-matching fillers. This is the clause that makes assemble
# task-conditioned rather than a recency feed.
fixture_db f01
seed context user "PROXY 직접 연결 채택 배경 정리"
seed context node "FILLER-ONE 트래픽 메모"
seed context node "FILLER-TWO 트래픽 메모"
run_asm 8192 "PROXY"
h="$(line_of "PROXY 직접 연결")"; f1="$(line_of "FILLER-ONE")"; f2="$(line_of "FILLER-TWO")"
# tail order is recency (id DESC): FILLER-TWO ingested last, so it comes first
ok "F-01 hint match (older) ranks above newer fillers" \
  '[ -n "$h" ] && [ -n "$f1" ] && [ -n "$f2" ] && [ "$h" -lt "$f2" ] && [ "$h" -lt "$f1" ] && [ "$f2" -lt "$f1" ]'

# ---- F-02 multi-term hint: bm25 prefers the denser match --------------------
# Both facts match the OR query, but the one containing BOTH terms is more
# relevant; bm25 must put it first (this pins search() as the ranking source,
# so a re-tune cannot silently swap bm25 for substring scoring).
fixture_db f02
seed context user "GATEWAY PROXY 전환 히스토리 정리"
seed context node "PROXY 포트만 언급하는 메모"
run_asm 8192 "GATEWAY PROXY"
g="$(line_of "GATEWAY PROXY 전환")"; p="$(line_of "PROXY 포트만")"
ok "F-02 denser hint match ranks first among matches" \
  '[ -n "$g" ] && [ -n "$p" ] && [ "$g" -lt "$p" ]'

# ---- F-03 G4: constraints never dropped, always before facts ----------------
fixture_db f03
sql "INSERT INTO peer_facts(observer,observed,kind,fact,valid_from,dedup,created_at,source_rank,review,mutability) VALUES
 ('family-assistant','yukson','constraint','CONSTRAINT-OLD 규칙','2026-08-07','c1','2026-08-07T00:00:00+00:00',3,0,'static'),
 ('family-assistant','yukson','constraint','CONSTRAINT-NEW 규칙','2026-08-08','c2','2026-08-08T00:00:00+00:00',3,0,'static');"
seed fact user "FILLER-NEW 최신 사실"
seed fact user "FILLER-OLD 오래된 사실"
run_asm 300 ""
c_new="$(line_of "CONSTRAINT-NEW")"; c_old="$(line_of "CONSTRAINT-OLD")"
f_new="$(line_of "FILLER-NEW")"
ok "F-03 constraints survive a tiny budget (G4)" \
  '[ -n "$c_new" ] && [ -n "$c_old" ]'
ok "F-03 constraint order is newest-first" '[ "$c_new" -lt "$c_old" ]'
ok "F-03 constraints render before the fact block" \
  '[ -n "$f_new" ] && [ "$c_old" -lt "$f_new" ]'

# ---- F-04 header warnings always present, even at tiny budget ---------------
fixture_db f04
sql "INSERT INTO peer_facts(observer,observed,kind,fact,valid_from,dedup,created_at,source_rank,review,because) VALUES
 ('family-assistant','yukson','decision','REVIEW-G5-99 결정','2026-08-07','g5-99','2026-08-07T00:00:00+00:00',1,1,NULL),
 ('family-assistant','yukson','fact','REVIEW-PLAIN-98 사실','2026-08-07','rev-98','2026-08-07T00:00:00+00:00',1,1,NULL);"
run_asm 120 ""
ok "F-04 review-queue warning survives the budget cut" 'grep -q "검토대기" <<<"$ASM"'
ok "F-04 G5 reasonless-decision warning survives too" 'grep -q "근거 결측 결정" <<<"$ASM"'
ok "F-04 header title always present" 'grep -q "nunchi working memory" <<<"$ASM"'

# ---- F-05 best-effort budget: skip-then-fill, not head-truncation -----------
# A line that does not fit is SKIPPED and a later, shorter line still fits.
# head -c truncation (the legacy defect) would keep the long line and drop
# everything after it — this fixture pins the difference.
fixture_db f05
seed context user "SHORT-A 짧은 행"
seed context node "LONG-B $(printf '여유行%.0s' $(seq 1 60)) 길어서 예산 초과"
seed context user "SHORT-C 뒤의 짧은 행"
run_asm 700 ""
a="$(line_of "SHORT-A")"; b="$(line_of "LONG-B")"; c="$(line_of "SHORT-C")"
ok "F-05 over-budget long line is skipped" '[ -z "$b" ]'
ok "F-05 later short line still fits (skip-then-fill)" '[ -n "$c" ]'
# included rows keep recency order: SHORT-C (newest) ahead of SHORT-A
ok "F-05 included lines keep their ranking order" '[ -n "$a" ] && [ -n "$c" ] && [ "$c" -lt "$a" ]'

# ---- F-06 first-fact guarantee under a microscopic budget -------------------
fixture_db f06
seed context user "SOLO-FACT 유일한 사실"
run_asm 150 ""
ok "F-06 tiny budget still yields one fact (never an empty block)" \
  'grep -q "SOLO-FACT" <<<"$ASM"'

# ---- F-07 hint miss falls back to pure recency ------------------------------
fixture_db f07
seed context node "OLD-ROW 이전 행"
seed context node "NEW-ROW 최신 행"
run_asm 8192 "NOMATCH-XYZ"
ok "F-07 hint miss: newest first" \
  '[ "$(line_of "NEW-ROW")" -lt "$(line_of "OLD-ROW")" ]'

# ---- F-08 observation kind excluded -----------------------------------------
fixture_db f08
seed observation node "TEMP-OBS 휘발성 관측"
seed context node "KEPT-ROW 유지 행"
run_asm 8192 ""
ok "F-08 observation never assembles" '! grep -q "TEMP-OBS" <<<"$ASM"'
ok "F-08 sibling context row assembles" 'grep -q "KEPT-ROW" <<<"$ASM"'

# ---- F-09 ⟳ live-check contract: kind-derived, inline, with legend ----------
fixture_db f09
seed preference user "STATIC-PREF 정적 선호"
seed context node "LIVE-ONE 가변 운영 사실"
seed context node "LIVE-TWO 가변 운영 사실 둘째"
seed procedure user "STATIC-PROC 정적 절차"
run_asm 8192 ""
ok "F-09 static rows carry no ⟳ marker" \
  '! grep -q "⟳ (.*/preference) STATIC-PREF" <<<"$ASM" && ! grep -q "⟳ (.*/procedure) STATIC-PROC" <<<"$ASM"'
ok "F-09 live rows carry the inline ⟳ marker" \
  'grep -q "⟳ (.*/context) LIVE-ONE" <<<"$ASM" && grep -q "⟳ (.*/context) LIVE-TWO" <<<"$ASM"'
ok "F-09 legend states the live-check count" 'grep -q "⟳ live-check 2건" <<<"$ASM"'
legend="$(line_of "live-check 2건")"; first_live="$(line_of "LIVE-ONE")"
ok "F-09 legend precedes the live rows" '[ -n "$legend" ] && [ -n "$first_live" ] && [ "$legend" -lt "$first_live" ]'

# ---- F-10 hallways 1-hop: entity association reaches keyword-less facts -----
# P2-6: the query mentions ONE endpoint of a hallway edge; a fact containing
# only the OTHER endpoint must still be found (and an exact-keyword match
# must still outrank it — widening adds recall, never buries precision).
fixture_db f10
printf '%s\n' '[{"entity_a":"gwakga","entity_b":"vps7","co_occurrence_count":9}]' > "$FIX/hallways.json"
export NUNCHI_HALLWAYS_FILE="$FIX/hallways.json"
seed context node "vps7 호스트명 관련 사실"
seed context node "gwakga 프록시 게이트웨이 사실"
seed context node "UNRELATED-ROW 무관한 행"
run_asm 8192 "gwakga 프록시"
direct="$(line_of "gwakga 프록시 게이트웨이")"; hop="$(line_of "vps7 호스트명")"; unrel="$(line_of "UNRELATED-ROW")"
ok "F-10 hallway hop reaches the other endpoint's fact" '[ -n "$hop" ]'
ok "F-10 two-term exact match outranks the hop (bm25 precision)" \
  '[ -n "$direct" ] && [ "$direct" -lt "$hop" ]'
ok "F-10 matched rows outrank the non-matching filler" \
  '[ -n "$unrel" ] && [ "$hop" -lt "$unrel" ]'
unset NUNCHI_HALLWAYS_FILE

# ---- F-11 expired facts are excluded ----------------------------------------
fixture_db f11
sql "INSERT INTO peer_facts(observer,observed,kind,fact,valid_from,valid_to,dedup,created_at,source_rank,review,mutability) VALUES
 ('family-assistant','yukson','context','EXPIRED-ROW 만료된 사실','2026-08-01','2026-08-15','e1','2026-08-01T00:00:00+00:00',1,0,'live');"
seed context node "ALIVE-ROW 유효 사실"
run_asm 8192 ""
ok "F-11 expired (valid_to past) row never assembles" '! grep -q "EXPIRED-ROW" <<<"$ASM"'
ok "F-11 alive sibling row assembles" 'grep -q "ALIVE-ROW" <<<"$ASM"'

# ---- F-12 a hint match is never duplicated in the recency tail --------------
fixture_db f12
seed context node "DUP-TARGET 중복 없음 확인"
seed context node "AFTER-ONE 뒤 행"
seed context node "AFTER-TWO 뒤 행2"
run_asm 8192 "DUP-TARGET"
ok "F-12 hint match appears exactly once" \
  '[ "$(grep -c "DUP-TARGET" <<<"$ASM")" = "1" ]'

# ---- F-13 budget bounds the fact block (CJK = 3B/char) ----------------------
# Declared overhead outside the best-effort bound: the header and the single
# ⟳ legend line. The FACT block itself must respect the budget, and a larger
# budget must admit strictly more rows (the cut is real).
fixture_db f13
for i in 1 2 3 4 5 6; do
  seed context node "바이트-행$i 한글 콘텐츠가 바이트를 빠르게 소모한다"
done
run_asm 400 ""
fact_bytes="$(grep -v "^## nunchi working memory" <<<"$ASM" | grep -v "^- ⚠" | grep -v "^- ⟳ live-check" | wc -c)"
ok "F-13 fact block respects the byte budget (CJK)" '[ "$fact_bytes" -le 400 ]'
ok "F-13 budget cut still emits the block header" 'grep -q "nunchi working memory" <<<"$ASM"'
rows_400="$(grep -c "^- ⟳ (" <<<"$ASM")"
run_asm 4000 ""
rows_4000="$(grep -c "^- ⟳ (" <<<"$ASM")"
total="$(python3 "$NP" assemble --budget 4000 | wc -c)"
ok "F-13 larger budget admits strictly more rows" '[ "$rows_4000" -gt "$rows_400" ]'
ok "F-13 when content fits, total stays within the budget" '[ "$total" -le 4000 ]'

# ---- F-14 review=1 rows still assemble (pinned current contract) ------------
# The header WARN about pending review; the row itself is not hidden. If a
# future policy wants review-pending rows suppressed, this fixture is the
# explicit decision point to flip — not an accident.
fixture_db f14
sql "INSERT INTO peer_facts(observer,observed,kind,fact,valid_from,dedup,created_at,source_rank,review) VALUES
 ('family-assistant','yukson','fact','REVIEW-VISIBLE-77 검토중 사실','2026-08-07','rv-77','2026-08-07T00:00:00+00:00',1,1);"
run_asm 8192 ""
ok "F-14 review-pending row still assembles (documented contract)" \
  'grep -q "REVIEW-VISIBLE-77" <<<"$ASM"'
ok "F-14 header still warns about it" 'grep -q "검토대기 1건" <<<"$ASM"'

# ---- F-15 peers are not scoped away: user+node rows assemble together -------
fixture_db f15
seed preference user "USER-PREF 유저 선호"
seed fact node "NODE-FACT 노드 사실"
run_asm 8192 ""
ok "F-15 user-peer row assembles" 'grep -q "USER-PREF" <<<"$ASM"'
ok "F-15 node-peer row assembles" 'grep -q "NODE-FACT" <<<"$ASM"'
ok "F-15 recency tail order holds across peers" \
  '[ "$(line_of "NODE-FACT")" -lt "$(line_of "USER-PREF")" ]'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
