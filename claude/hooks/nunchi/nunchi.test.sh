#!/usr/bin/env bash
# Tests for the nunchi memory shadow hooks (#816).
# Isolated via NUNCHI_DB/NUNCHI_HOME/CCC_STATE_DIR/HOME overrides; the LLM
# backend and MemPalace CLI are PATH stubs — no network, no real palace.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NP="$HERE/nunchi.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

mkdir -p "$TMP/nunchi-home" "$TMP/state" "$TMP/bin" "$TMP/home"
export NUNCHI_DB="$TMP/nunchi-home/facts.db"
export NUNCHI_SNAPSHOT="$TMP/nunchi-home/snapshot.md"
export NUNCHI_HOME="$TMP/nunchi-home"
export CCC_STATE_DIR="$TMP/state"

payload() {  # payload <sid> <kind> <subject> <text>
  printf '{"session_id":"%s","distilled_at":"2026-07-31T00:00:00+00:00","honcho":[{"kind":"%s","subject":"%s","text":"%s"}]}' "$1" "$2" "$3" "$4"
}

# ---- 1. init: fresh schema has (fact, observed) FTS ------------------------
out="$(python3 "$NP" init 2>&1)"; rc=$?
ok "init creates db" '[ "$rc" = 0 ] && [ -f "$NUNCHI_DB" ]'
cols="$(python3 -c "import sqlite3;print([r[1] for r in sqlite3.connect('$NUNCHI_DB').execute('PRAGMA table_info(facts_fts)')])")"
ok "fresh FTS indexes observed (B1)" 'grep -q "observed" <<<"$cols"'

# ---- 2. B1 migration: pilot-era single-column FTS upgraded in place --------
OLD="$TMP/old.db"
python3 - "$OLD" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.executescript("""
CREATE TABLE peer_facts (id INTEGER PRIMARY KEY, observer TEXT NOT NULL,
  observed TEXT NOT NULL, kind TEXT NOT NULL, fact TEXT NOT NULL, evidence TEXT,
  confidence REAL DEFAULT 0.7, valid_from TEXT NOT NULL, valid_to TEXT,
  supersedes INTEGER, dedup TEXT UNIQUE, created_at TEXT NOT NULL);
CREATE VIRTUAL TABLE facts_fts USING fts5(fact, content='peer_facts', content_rowid='id');
CREATE TRIGGER facts_ai AFTER INSERT ON peer_facts BEGIN
  INSERT INTO facts_fts(rowid, fact) VALUES (new.id, new.fact); END;
""")
c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,valid_from,dedup,created_at)"
          " VALUES('family-assistant','yukson','fact','노드 상태 정상','2026-07-28','d1','2026-07-28')")
c.commit()
PY
out="$(NUNCHI_DB="$OLD" python3 "$NP" init 2>&1)"
mig="$(NUNCHI_DB="$OLD" python3 -c "import sqlite3;print([r[1] for r in sqlite3.connect('$OLD').execute('PRAGMA table_info(facts_fts)')])")"
ok "old DB migrated to 2-col FTS" 'grep -q "observed" <<<"$mig"'
out="$(NUNCHI_DB="$OLD" python3 "$NP" recall "yukson" 2>&1)"
ok "migrated DB answers node-name query (B1)" 'grep -q "노드 상태 정상" <<<"$out"'

# ---- 3. ingest: dedup + subject mapping + alias normalization (B3) ---------
payload s1 fact user "사용자는 병렬 실행을 선호한다" | python3 "$NP" ingest - >/dev/null
out="$(payload s1 fact user "사용자는 병렬 실행을 선호한다" | python3 "$NP" ingest - 2>&1)"
ok "duplicate fact deduped" 'grep -q "ingested 0/1" <<<"$out"'
CCC_NODE="카렐렌" payload s2 fact node "이 노드는 코덱스 러너다" | CCC_NODE="카렐렌" python3 "$NP" ingest - >/dev/null
row="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute(\"SELECT observed FROM peer_facts WHERE fact LIKE '%코덱스 러너%'\").fetchone()[0])")"
ok "persona node subject canonicalized to slug (B3)" '[ "$row" = "yukson" ]'

# ---- 4. recall: observed match + Korean alias query expansion (B3) ---------
out="$(python3 "$NP" recall "yukson" 2>&1)"
ok "recall by node slug hits node fact" 'grep -q "코덱스 러너" <<<"$out"'
out="$(python3 "$NP" recall "육손 노드" 2>&1)"
ok "recall by Korean alias expands to slug" 'grep -q "코덱스 러너" <<<"$out"'

# ---- 5. B2: correction auto-supersede --------------------------------------
payload s3 fact user "메인 모델은 opus-5 이다" | python3 "$NP" ingest - >/dev/null
payload s4 correction user "메인 모델은 opus-5 아니라 fable-5 이다" | python3 "$NP" ingest - >/dev/null
closed="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute(\"SELECT COUNT(*) FROM peer_facts WHERE fact LIKE '%opus-5 이다' AND valid_to IS NOT NULL\").fetchone()[0])")"
link="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute(\"SELECT supersedes IS NOT NULL FROM peer_facts WHERE fact LIKE '%fable-5 이다'\").fetchone()[0])")"
ok "correction closes best-overlap fact (B2)" '[ "$closed" = 1 ]'
ok "correction row links supersedes (B2)" '[ "$link" = 1 ]'
before="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute('SELECT COUNT(*) FROM peer_facts WHERE valid_to IS NOT NULL').fetchone()[0])")"
payload s5 correction user "완전히 무관한 주제의 정정 문장" | python3 "$NP" ingest - >/dev/null
after="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute('SELECT COUNT(*) FROM peer_facts WHERE valid_to IS NOT NULL').fetchone()[0])")"
ok "unrelated correction closes nothing (conservative)" '[ "$before" = "$after" ]'
payload s6 fact user "임계 테스트 전용 사실 알파" | python3 "$NP" ingest - >/dev/null
NUNCHI_NO_AUTO_SUPERSEDE=1 payload s7 correction user "임계 테스트 전용 사실 알파 아님" | NUNCHI_NO_AUTO_SUPERSEDE=1 python3 "$NP" ingest - >/dev/null
alpha="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute(\"SELECT valid_to IS NULL FROM peer_facts WHERE fact LIKE '%사실 알파'\").fetchone()[0])")"
ok "NUNCHI_NO_AUTO_SUPERSEDE=1 disables B2" '[ "$alpha" = 1 ]'

# ---- 6. snapshot + stats ----------------------------------------------------
out="$(python3 "$NP" snapshot --limit 10 2>&1)"
ok "snapshot written with header" '[ -f "$NUNCHI_SNAPSHOT" ] && grep -q "nunchi working memory" "$NUNCHI_SNAPSHOT"'
out="$(python3 "$NP" stats 2>&1)"
ok "stats reports totals" 'grep -qE "facts: [0-9]+ total" <<<"$out"'

# ---- 7. mode gate: shell entries are no-ops unless on -----------------------
printf 'off' > "$CCC_STATE_DIR/nunchi.mode"
out="$(bash "$HERE/sessionstart.sh" 2>&1)"; rc=$?
ok "sessionstart no-op when mode=off" '[ "$rc" = 0 ] && [ -z "$out" ]'
printf 'on' > "$CCC_STATE_DIR/nunchi.mode"
out="$(bash "$HERE/sessionstart.sh" 2>&1)"
ok "sessionstart prints snapshot when mode=on" 'grep -q "nunchi working memory" <<<"$out"'
out="$(CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=shared bash "$HERE/sessionstart.sh" 2>&1)"
ok "legacy sessionstart fails closed on scoped shared audiences" '[ -z "$out" ]'
out="$(CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=private bash "$HERE/sessionstart.sh" 2>&1)"
ok "legacy sessionstart also fails closed on scoped private audiences" '[ -z "$out" ]'
out="$(CCC_NUNCHI_MODE=off bash "$HERE/sessionstart.sh" 2>&1)"
ok "env CCC_NUNCHI_MODE overrides mode file" '[ -z "$out" ]'

# ---- 8. ingest-cron e2e (mode on, isolated state) ---------------------------
mkdir -p "$CCC_STATE_DIR/distill-history"
payload cron1 fact user "크론 미러 테스트 사실" > "$CCC_STATE_DIR/distill-history/t1.json"
bash "$HERE/ingest-cron.sh"
out="$(python3 "$NP" recall "크론 미러 테스트" 2>&1)"
ok "ingest-cron mirrors distill-history" 'grep -q "크론 미러" <<<"$out"'
ok "ingest-cron marks file seen" 'grep -q "t1.json" "$NUNCHI_HOME/ingested-files"'

# ---- 9. mempalace_verbatim: degrade + stub ----------------------------------
out="$(HOME="$TMP/home" PATH="/usr/bin:/bin" python3 -c "
import sys; sys.path.insert(0, '$HERE')
from nunchi import mempalace_verbatim
print('EMPTY' if mempalace_verbatim('any query') == '' else 'NONEMPTY')")"
ok "verbatim degrades to empty without CLI" '[ "$out" = "EMPTY" ]'
cat > "$TMP/bin/mempalace" <<'EOF'
#!/usr/bin/env bash
echo "  Results for: \"q\""
echo "  [1] wing / technical"
echo "      Source: x.jsonl"
echo "      Match:  cosine_sim=0.9"
echo "      quiz2-api listens on 127.0.0.1:8801"
EOF
chmod +x "$TMP/bin/mempalace"
out="$(PATH="$TMP/bin:/usr/bin:/bin" python3 -c "
import sys; sys.path.insert(0, '$HERE')
from nunchi import mempalace_verbatim
print(mempalace_verbatim('quiz2-api port'))")"
ok "verbatim returns cleaned excerpt via CLI" 'grep -q "8801" <<<"$out" && ! grep -q "cosine" <<<"$out"'

# ---- 10. dialectic e2e with stubbed LLM (no network) -------------------------
cat > "$TMP/bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "STUB-ANSWER"
EOF
chmod +x "$TMP/bin/claude"
out="$(PATH="$TMP/bin:/usr/bin:/bin" HOME="$TMP/home" python3 "$NP" dialectic "모델" 2>&1)"
ok "dialectic synthesizes via stub backend" 'grep -q "STUB-ANSWER" <<<"$out"'

# ---- 11. #824 Phase 1: keyword projection + hybrid verbatim -----------------
out="$(python3 -c "
import sys; sys.path.insert(0, '$HERE')
from nunchi import _keywords
print(' '.join(_keywords('퀴즈 API가 어느 주소에서 돌고 있었는지 기억나?')))")"
ok "keywords strip particles and stopwords" 'grep -q "퀴즈" <<<"$out" && grep -q "API" <<<"$out" && grep -q "주소" <<<"$out" && ! grep -q "기억나" <<<"$out"'
cat > "$TMP/bin/mempalace" <<'EOF'
#!/usr/bin/env bash
q="$2"
echo "      SHARED-LINE common excerpt"
case "$q" in
  *기억나*) echo "      NATURAL-ONLY excerpt" ;;
  *)        echo "      KEYWORD-ONLY excerpt" ;;
esac
EOF
chmod +x "$TMP/bin/mempalace"
out="$(PATH="$TMP/bin:/usr/bin:/bin" python3 -c "
import sys; sys.path.insert(0, '$HERE')
from nunchi import mempalace_verbatim
print(mempalace_verbatim('퀴즈 API가 어느 주소에서 돌고 있었는지 기억나?'))")"
ok "hybrid merges natural + keyword search results" 'grep -q "NATURAL-ONLY" <<<"$out" && grep -q "KEYWORD-ONLY" <<<"$out"'
ok "hybrid de-duplicates shared excerpts" '[ "$(grep -c "SHARED-LINE" <<<"$out")" = 1 ]'

# ---- 12. #824 Phase 1: snapshot header promoted + bench runner --------------
python3 "$NP" snapshot --limit 5 >/dev/null 2>&1
ok "snapshot header marks nunchi primary" 'grep -q "nunchi working memory (primary" "$NUNCHI_SNAPSHOT"'
printf 'off' > "$CCC_STATE_DIR/nunchi.mode"
out="$(bash "$HERE/bench.sh" 2>&1)"; rc=$?
ok "bench no-op when mode=off" '[ "$rc" = 0 ] && [ -z "$out" ]'
rows="$(tail -n +2 "$HERE/bench-qset.tsv" | grep -c .)"
badcols="$(awk -F'\t' 'NF!=4' "$HERE/bench-qset.tsv" | grep -c . || true)"
ok "bench qset has 7 rows of 4 tab-separated columns" '[ "$rows" = 7 ] && [ "$badcols" = 0 ]'
printf 'on' > "$CCC_STATE_DIR/nunchi.mode"

# ---- 13. #890 write gate ----------------------------------------------------
GDB="$TMP/nunchi-home/gate.db"
gq() { NUNCHI_DB="$GDB" python3 -c "
import sqlite3, sys
print(sqlite3.connect('$GDB').execute(sys.argv[1]).fetchall())" "$1"; }
gpayload() {  # gpayload <sid> <kind> <subject> <text> [source] [quote] [tpath]
  python3 - "$@" <<'PY'
import json, sys
sid, kind, subject, text = sys.argv[1:5]
item = {"kind": kind, "subject": subject, "text": text}
if len(sys.argv) > 5 and sys.argv[5]: item["source"] = sys.argv[5]
if len(sys.argv) > 6 and sys.argv[6]: item["quote"] = sys.argv[6]
p = {"session_id": sid, "distilled_at": "2026-08-03T00:00:00+00:00", "honcho": [item]}
if len(sys.argv) > 7 and sys.argv[7]: p["transcript_path"] = sys.argv[7]
print(json.dumps(p, ensure_ascii=False))
PY
}

# G1: progress→done update auto-closes the in-flight fact, link preserved
gpayload s90 context session "PR #9 리뷰 진행 중, CI 대기" | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
gpayload s90 observation session "PR #9 머지 완료" | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G1 closes the stale in-flight fact" \
  '[ "$(gq "SELECT COUNT(*) FROM peer_facts WHERE valid_to IS NOT NULL" | tr -dc 0-9)" = 1 ]'
ok "G1 keeps the supersedes link on the newcomer" \
  'gq "SELECT supersedes FROM peer_facts WHERE fact LIKE \"%머지 완료%\"" | grep -q "(1,)"'

# G2: verified quote earns the claimed rank; unverifiable claim demotes + review
TR="$TMP/gate-transcript.jsonl"
printf '{"type":"user","message":{"content":"등애는 절대 원격 삭제 금지라고 했다"}}\n' > "$TR"
gpayload s91 preference user "원격 삭제 금지 선호" user-stated "절대 원격 삭제 금지" "$TR" \
  | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G2 verified user-stated quote earns rank 3, no review" \
  'gq "SELECT source_rank, review FROM peer_facts WHERE fact LIKE \"%삭제 금지 선호%\"" | grep -q "(3, 0)"'
gpayload s91 observation user "사용자가 X를 승인했다고 함" user-stated "이 인용은 원문에 없다" "$TR" \
  | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G2 unverifiable claim demotes to rank 1 and flags review" \
  'gq "SELECT source_rank, review FROM peer_facts WHERE fact LIKE \"%승인했다고%\"" | grep -q "(1, 1)"'

# G2 guard: an inferred completion cannot close a verified user-stated fact
printf '{"type":"user","message":{"content":"배포 승인 대기 원칙 유지 진행 중이라고 했다"}}\n' > "$TMP/tr2.jsonl"
gpayload s92 preference user "배포 승인 대기 원칙 유지 진행 중" user-stated "배포 승인 대기 원칙 유지 진행 중" "$TMP/tr2.jsonl" \
  | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
gpayload s92 observation user "배포 승인 대기 원칙 폐기 완료" inferred "" "$TMP/tr2.jsonl" \
  | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G2 rank guard: inferred done cannot close user-stated fact" \
  'gq "SELECT valid_to FROM peer_facts WHERE fact LIKE \"%원칙 유지 진행 중%\"" | grep -q "(None,)"'

# G3: high-overlap non-update conflict flags review, never auto-resolves
gpayload s93 context node "기본 모델 값은 fable-5 이다" | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
gpayload s93 context node "기본 모델 값은 opus 이다" | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G3 conflicting sibling stays open and is flagged" \
  'gq "SELECT valid_to, review FROM peer_facts WHERE fact LIKE \"%opus 이다%\"" | grep -q "(None, 1)"'

# G4: constraints always injected into the snapshot, ahead of the limit
gpayload s94 constraint user "절대 main 브랜치에 직접 푸시 금지" | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
snap="$(NUNCHI_DB="$GDB" NUNCHI_SNAPSHOT="$TMP/gate-snap.md" python3 "$NP" snapshot --limit 1)"
ok "G4 constraint survives a limit-1 snapshot" 'grep -q "\[제약/seo-jin-on\] 절대 main" <<<"$snap"'
ok "snapshot surfaces the review queue count" 'grep -q "검토대기" <<<"$snap"'

# review CLI: list + clear
out="$(NUNCHI_DB="$GDB" python3 "$NP" review)"
ok "review lists flagged facts" 'grep -q "opus 이다" <<<"$out"'
rid="$(gq "SELECT id FROM peer_facts WHERE fact LIKE \"%opus 이다%\"" | tr -dc 0-9)"
NUNCHI_DB="$GDB" python3 "$NP" review "$rid" --clear >/dev/null
ok "review --clear drops the flag" \
  'gq "SELECT review FROM peer_facts WHERE id=$rid" | grep -q "(0,)"'

# review-stale: retro G1 lists then closes only on --close
gpayload s95 context session "이슈 #7 분석 실행 중" | NUNCHI_NO_AUTO_SUPERSEDE=1 NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
gpayload s95 observation session "이슈 #7 분석 종결" | NUNCHI_NO_AUTO_SUPERSEDE=1 NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
out="$(NUNCHI_DB="$GDB" python3 "$NP" review-stale)"
ok "review-stale lists the retro candidate without closing" \
  'grep -q "1 candidates" <<<"$out" && gq "SELECT valid_to FROM peer_facts WHERE fact LIKE \"%분석 실행 중%\"" | grep -q "(None,)"'
NUNCHI_DB="$GDB" python3 "$NP" review-stale --close >/dev/null
ok "review-stale --close closes the stale fact" \
  'gq "SELECT valid_to FROM peer_facts WHERE fact LIKE \"%분석 실행 중%\"" | grep -qv "(None,)"'

# metrics: body-free counters present
out="$(NUNCHI_DB="$GDB" python3 "$NP" metrics)"
ok "metrics emits gate counters" \
  'grep -q "facts_open=" <<<"$out" && grep -q "stale_suspect_ratio=" <<<"$out" && grep -q "review_pending=" <<<"$out" && grep -q "constraints_open=" <<<"$out"'

# migration: a pre-gate DB gains the columns losslessly
MDB="$TMP/pre-gate.db"
python3 - "$MDB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("""CREATE TABLE peer_facts (id INTEGER PRIMARY KEY, observer TEXT NOT NULL,
  observed TEXT NOT NULL, kind TEXT NOT NULL, fact TEXT NOT NULL, evidence TEXT,
  confidence REAL DEFAULT 0.7, valid_from TEXT NOT NULL, valid_to TEXT,
  supersedes INTEGER, dedup TEXT UNIQUE, created_at TEXT NOT NULL)""")
c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,valid_from,created_at)"
          " VALUES ('o','p','context','기존 사실','2026-07-01','2026-07-01')")
c.commit()
PY
NUNCHI_DB="$MDB" python3 "$NP" init >/dev/null
cols="$(python3 -c "import sqlite3;print([r[1] for r in sqlite3.connect('$MDB').execute('PRAGMA table_info(peer_facts)')])")"
ok "pre-gate DB gains source_rank/review in place" \
  'grep -q "source_rank" <<<"$cols" && grep -q "review" <<<"$cols"'
mrows="$(python3 -c "import sqlite3;print(sqlite3.connect('$MDB').execute('SELECT COUNT(*) FROM peer_facts').fetchone()[0])")"
ok "pre-gate rows survive migration" '[ "$mrows" = 1 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
