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
ok "legacy sessionstart fails closed on shared audiences" '[ -z "$out" ]'
out="$(CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=private bash "$HERE/sessionstart.sh" 2>&1)"
ok "legacy sessionstart remains available to the private owner" 'grep -q "nunchi working memory" <<<"$out"'
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
ok "bench qset has 5 rows of 4 tab-separated columns" '[ "$rows" = 5 ] && [ "$badcols" = 0 ]'
printf 'on' > "$CCC_STATE_DIR/nunchi.mode"

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
