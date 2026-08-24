#!/usr/bin/env bash
# Tests for the nunchi memory shadow hooks (#816).
# Isolated via NUNCHI_DB/NUNCHI_HOME/CCC_STATE_DIR/HOME overrides; the LLM
# backend and MemPalace CLI are PATH stubs — no network, no real palace.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NP="$HERE/nunchi.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"
# Inherited CCC_/NUNCHI_ state reaches nunchi.py and costs one assertion (#1023).
ccc_test_reset_hook_env

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
migc="$(NUNCHI_DB="$OLD" python3 -c "import sqlite3;print([r[1] for r in sqlite3.connect('$OLD').execute('PRAGMA table_info(peer_facts)')])")"
ok "old DB migrated with gate columns incl. because (G5)" \
  'grep -q "source_rank" <<<"$migc" && grep -q "review" <<<"$migc" && grep -q "because" <<<"$migc"'
out="$(NUNCHI_DB="$OLD" python3 "$NP" recall "yukson" 2>&1)"
ok "migrated DB answers node-name query (B1)" 'grep -q "노드 상태 정상" <<<"$out"'

# ---- 3. ingest: dedup + subject mapping + alias normalization (B3) ---------
payload s1 fact user "사용자는 병렬 실행을 선호한다" | python3 "$NP" ingest - >/dev/null
out="$(payload s1 fact user "사용자는 병렬 실행을 선호한다" | python3 "$NP" ingest - 2>&1)"
ok "duplicate fact deduped" 'grep -q "ingested 0/1" <<<"$out"'
CCC_NODE="카렐렌" payload s2 fact node "이 노드는 코덱스 러너다" | CCC_NODE="카렐렌" python3 "$NP" ingest - >/dev/null
row="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute(\"SELECT observed FROM peer_facts WHERE fact LIKE '%코덱스 러너%'\").fetchone()[0])")"
ok "persona node subject canonicalized to slug (B3)" '[ "$row" = "yukson" ]'

# ---- 3b. ingest: mutable-ops facts filtered (#1010) ------------------------
out="$(payload s6 fact node "카렐렌 ccc-node 커밋 efe088b, Bridge active/enabled, NRestarts=0" | python3 "$NP" ingest - 2>&1)"
ok "mutable-ops state fact skipped at ingest" 'grep -q "ingested 0/1" <<<"$out" && grep -q "skipped_mutable_ops=1" <<<"$out"'
out="$(payload s7 fact node "서서 systemd HOME 교정 후 정상화" | python3 "$NP" ingest - 2>&1)"
ok "'정상화' claim skipped" 'grep -q "ingested 0/1" <<<"$out" && grep -q "skipped_mutable_ops=1" <<<"$out"'
out="$(payload s8 fact user "사용자는 한국어 보고를 선호한다" | python3 "$NP" ingest - 2>&1)"
ok "durable preference fact still ingested" 'grep -q "ingested 1/1" <<<"$out"'
out="$(payload s9 correction node "카렐렌 러너 이미지는 74f11bc9가 아니라 638e5a1 이다" | python3 "$NP" ingest - 2>&1)"
ok "correction carrying SHAs is never filtered" 'grep -q "ingested 1/1" <<<"$out"'
out="$(payload s10 fact node "20260807 롤아웃을 완료했다" | python3 "$NP" ingest - 2>&1)"
ok "bare date stamp not misclassified as SHA" 'grep -q "ingested 1/1" <<<"$out"'
out="$(python3 "$NP" recall "카렐렌" 2>&1)"
ok "skipped state fact absent from recall" '! grep -q "NRestarts" <<<"$out"'
# ---- 3c. observation TTL class + review-queue alert (#1010 proposals 2/3) ---
out="$(payload s11 observation node "TEMP-OBS-7749" | python3 "$NP" ingest - 2>&1)"
ok "observation kind ingested" 'grep -q "ingested 1/1" <<<"$out"'
python3 "$NP" snapshot --limit 25 >/dev/null
ok "observation excluded from snapshot" '! grep -q "TEMP-OBS-7749" "$NUNCHI_SNAPSHOT"'
out="$(python3 "$NP" recall "TEMP-OBS" 2>&1)"
ok "observation still searchable via recall" 'grep -q "TEMP-OBS-7749" <<<"$out"'
python3 - "$NUNCHI_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,valid_from,dedup,created_at,source_rank,review)"
          " VALUES('family-assistant','yukson','observation','OLD-OBS-8861','2020-01-01','obs-old-8861','2020-01-01T00:00:00+00:00',2,0)")
c.commit()
PY
python3 "$NP" snapshot --limit 25 >/dev/null
closed="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute(\"SELECT COUNT(*) FROM peer_facts WHERE fact='OLD-OBS-8861' AND valid_to IS NOT NULL\").fetchone()[0])")"
ok "expired observation auto-closed on snapshot sweep" '[ "$closed" = 1 ]'
python3 - "$NUNCHI_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,valid_from,dedup,created_at,source_rank,review)"
          " VALUES('family-assistant','yukson','fact','REVIEW-PENDING-5591','2026-08-07','rev-5591','2026-08-07T00:00:00+00:00',1,1)")
c.commit()
PY
out="$(NUNCHI_REVIEW_QUEUE_ALERT=1 python3 "$NP" snapshot --limit 25 2>&1)"
ok "review queue at threshold escalates wording" 'grep -q "임계치" <<<"$out"'
out="$(NUNCHI_REVIEW_QUEUE_ALERT=99 python3 "$NP" snapshot --limit 25 2>&1)"
ok "below threshold keeps plain warning" 'grep -q "⚠ 검토대기" <<<"$out" && ! grep -q "⚠⚠" <<<"$out"'

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
# TM-2370 P1: the Q-set grew 7 -> 48 (six columns — source/evidence fold into
# expect for the 4-column reader, so bench.sh needs no change). Contract: every
# row has exactly 6 columns, ids are unique, and the original q1-q7 queries are
# preserved verbatim for series continuity.
rows="$(tail -n +2 "$HERE/bench-qset.tsv" | grep -c .)"
badcols="$(awk -F'\t' 'NF!=6' "$HERE/bench-qset.tsv" | grep -c . || true)"
dupids="$(tail -n +2 "$HERE/bench-qset.tsv" | cut -f1 | sort | uniq -d | grep -c . || true)"
ok "bench qset has 48 rows of 6 tab-separated columns, unique ids" '[ "$rows" = 48 ] && [ "$badcols" = 0 ] && [ "$dupids" = 0 ]'
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

# G1 cross-session: a later session's completion closes an earlier session's
# in-flight fact (session:* peers are one actor's log split by session id)
gpayload s90b context session "PR #12 배포 승인 대기" | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
gpayload s90c observation session "PR #12 배포 완료" | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G1 matches across session peers" \
  'gq "SELECT valid_to IS NOT NULL FROM peer_facts WHERE fact LIKE \"%배포 승인 대기%\"" | grep -q "(1,)"'

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

# G2 quote-gate defects (#1099). Measured on 20 archived payloads, the gate
# demoted 29 of 34 verifiable claims (85%) — burying real user statements as
# agent inferences, the exact inversion G2 exists to prevent. Every case below
# is a quote that IS in the transcript and was rejected anyway.

# (a) transcript located by session id when source_cwd points elsewhere. The
# agent cd'ing mid-session made the cwd-derived path miss 9 of 12 payloads.
PROJ="$TMP/home/.claude/projects/-root-elsewhere"
mkdir -p "$PROJ"
sid_a="aaaaaaaa-1111-2222-3333-444444444444"
printf '{"type":"user","message":{"content":"야간에는 알림을 보내지 마라"}}\n' > "$PROJ/$sid_a.jsonl"
python3 - "$sid_a" <<'PY' | HOME="$TMP/home" NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
import json, sys
print(json.dumps({"session_id": sys.argv[1], "source_cwd": "/root/some/deep/subdir",
  "distilled_at": "2026-08-03T00:00:00+00:00",
  "honcho": [{"kind": "preference", "subject": "user", "text": "야간 알림 비활성화 선호",
              "source": "user-stated", "quote": "야간에는 알림을 보내지 마라"}]}, ensure_ascii=False))
PY
ok "G2 finds the transcript by session id when source_cwd misleads" \
  'gq "SELECT source_rank, review FROM peer_facts WHERE fact LIKE \"%야간 알림 비활성화%\"" | grep -q "(3, 0)"'

# (b) quote spanning a newline. Skeletonizing raw jsonl bytes left the escape
# letter behind (\n -> literal "n"), so these could never match.
TRN="$TMP/g2-newline.jsonl"
printf '{"type":"user","message":{"content":"브로커를 재시작하려면\\n반드시 사전 승인을 받아라"}}\n' > "$TRN"
gpayload s95 preference user "브로커 재시작 사전승인" user-stated "브로커를 재시작하려면 반드시 사전 승인을 받아라" "$TRN" \
  | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G2 verifies a quote spanning a transcript newline" \
  'gq "SELECT source_rank, review FROM peer_facts WHERE fact LIKE \"%브로커 재시작 사전승인%\"" | grep -q "(3, 0)"'

# (c) markdown emphasis: the model quotes rendered prose, the transcript holds
# the source. Same words, rejected on formatting alone.
TRM="$TMP/g2-markdown.jsonl"
printf '{"type":"user","message":{"content":"재전환은 **서두르지 않는 게** 좋겠습니다"}}\n' > "$TRM"
gpayload s96 preference user "재전환 신중 진행 선호" user-stated "재전환은 서두르지 않는 게 좋겠습니다" "$TRM" \
  | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G2 verifies a quote across markdown emphasis" \
  'gq "SELECT source_rank, review FROM peer_facts WHERE fact LIKE \"%재전환 신중%\"" | grep -q "(3, 0)"'

# (d) the gate must still reject a quote that is genuinely absent — the
# normalization above widens matching, so pin the security property.
gpayload s96 observation user "사용자가 롤백을 승인했다" user-stated "롤백을 승인한다" "$TRM" \
  | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G2 still demotes a quote absent from the transcript" \
  'gq "SELECT source_rank, review FROM peer_facts WHERE fact LIKE \"%롤백을 승인했다%\"" | grep -q "(1, 1)"'

# G3: high-overlap non-update conflict flags review, never auto-resolves
gpayload s93 context node "기본 모델 값은 fable-5 이다" | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
gpayload s93 context node "기본 모델 값은 opus 이다" | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G3 conflicting sibling stays open and is flagged" \
  'gq "SELECT valid_to, review FROM peer_facts WHERE fact LIKE \"%opus 이다%\"" | grep -q "(None, 1)"'

# G3 cross-session (#1255): the same conclusion re-extracted under a *different*
# session id must still be compared — a plain observed=? match would silently
# stack N near-identical facts (measured 2026-08-22: 40 clusters, 82 facts).
gpayload s97a decision session "게이트 정밀도 50% 손실률 80% 근거로 V2 자동 승격을 차단하기로 결정" \
  | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
gpayload s97b decision session "게이트 정밀도 50% 손실률 80% 근거로 V2 자동 승격을 차단하기로 함" \
  | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G3 flags a same-kind near-duplicate landed under a different session id" \
  'gq "SELECT valid_to, review FROM peer_facts WHERE fact LIKE \"%차단하기로 함%\"" | grep -q "(None, 1)"'

# G3 cross-session stays kind-scoped: a different (non-excluded) kind sharing
# tokens must not be flagged just because it lands in the same cross-session
# pool — same words, different kind of statement.
gpayload s97c context session "게이트 정밀도 50% 손실률 80% 근거로 V2 자동 승격을 차단하기로 기록" \
  | NUNCHI_DB="$GDB" python3 "$NP" ingest - >/dev/null
ok "G3 cross-session match stays scoped to the same kind" \
  'gq "SELECT review FROM peer_facts WHERE fact LIKE \"%차단하기로 기록%\"" | grep -q "(0,)"'

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

# ---- #1082: synthesis backend must be chosen by "can answer", not "exists" ---
# A codex/piri node ships the claude binary without credentials. Presence-only
# selection locked synthesis onto it and returned the login notice as an answer,
# while an authenticated codex sat unused on the same host.
synth_bin="$TMP/bin-synth"; mkdir -p "$synth_bin"
cat > "$synth_bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "Not logged in · Please run /login"
EOF
cat > "$synth_bin/codex" <<'EOF'
#!/usr/bin/env bash
echo "session log line"
echo "CODEX-ANSWER"
EOF
chmod +x "$synth_bin/claude" "$synth_bin/codex"
out="$(PATH="$synth_bin:/usr/bin:/bin" HOME="$TMP/home" python3 "$NP" dialectic "모델" 2>&1)"
ok "logged-out claude falls back to an authenticated codex" \
  'grep -q "CODEX-ANSWER" <<<"$out" && ! grep -q "Not logged in" <<<"$out"'
ok "codex fallback still takes the final block, not a session log line" \
  '! grep -q "session log line" <<<"$out"'

# A healthy claude must still win — the fallback must not reorder the default.
cat > "$synth_bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "CLAUDE-ANSWER"
EOF
chmod +x "$synth_bin/claude"
out="$(PATH="$synth_bin:/usr/bin:/bin" HOME="$TMP/home" python3 "$NP" dialectic "모델" 2>&1)"
ok "a healthy claude is still preferred over codex" \
  'grep -q "CLAUDE-ANSWER" <<<"$out" && ! grep -q "CODEX-ANSWER" <<<"$out"'

# A non-zero exit is a distinct failure mode from a login notice.
cat > "$synth_bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "boom"; exit 3
EOF
chmod +x "$synth_bin/claude"
out="$(PATH="$synth_bin:/usr/bin:/bin" HOME="$TMP/home" python3 "$NP" dialectic "모델" 2>&1)"
ok "a crashing backend also falls through to the next candidate" \
  'grep -q "CODEX-ANSWER" <<<"$out"'

# When every present backend is unusable, say so instead of passing a notice off
# as an answer — that masking is what corrupted the #827 parity sample.
cat > "$synth_bin/codex" <<'EOF'
#!/usr/bin/env bash
echo "Not logged in · Please run /login"
EOF
cat > "$synth_bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "Not logged in · Please run /login"
EOF
chmod +x "$synth_bin/claude" "$synth_bin/codex"
out="$(PATH="$synth_bin:/usr/bin:/bin" HOME="$TMP/home" python3 "$NP" dialectic "모델" 2>&1)"
ok "all-unusable reports each backend and its reason" \
  'grep -q "합성 백엔드 사용 불가" <<<"$out" && grep -q "claude:unavailable" <<<"$out" && grep -q "codex:unavailable" <<<"$out"'

# Cross-file constant check (#1072 precedent): the Python matcher and bench.sh's
# shell default must not drift — a pattern known to one and not the other
# reintroduces exactly the silent failure both exist to catch.
py_re="$(python3 -c "
import sys; sys.path.insert(0, '$HERE')
from nunchi import SYNTH_UNAVAILABLE_RE
print(SYNTH_UNAVAILABLE_RE.pattern)")"
sh_re="$(sed -n 's/^INVALID_RE="\${NUNCHI_BENCH_INVALID_RE:-\(.*\)}"$/\1/p' "$HERE/bench.sh")"
ok "bench.sh and nunchi.py share one unavailable-backend pattern" \
  '[ -n "$sh_re" ] && [ "$py_re" = "$sh_re" ]'

# ---- #827 / TM-2339: synthesize reads evidence from stdin -------------------
# The bench Wiki layer needs a synthesis entry point that is NOT dialectic, so
# live recall behaviour stays put while the gate gains the layer TM-2029 made
# responsible for cross-node knowledge. It must reuse llm_synthesize so the
# #1082 backend-selection fix is not duplicated in a second place.
syn_bin="$TMP/bin-syn"; mkdir -p "$syn_bin"
cat > "$syn_bin/claude" <<'EOF'
#!/usr/bin/env bash
# Echo the prompt back so the test can assert the evidence reached the backend.
echo "SYN-OK:$(grep -c . <<<"$2")"
EOF
chmod +x "$syn_bin/claude"
out="$(printf 'pages/decisions/x.md — Termux는 chromadb 빌드 불가\n' \
  | PATH="$syn_bin:/usr/bin:/bin" HOME="$TMP/home" python3 "$NP" synthesize "왜 peer_facts-only인가" 2>&1)"
ok "synthesize answers from stdin evidence via the shared backend" \
  'grep -q "SYN-OK" <<<"$out"'

out="$(printf '' | PATH="$syn_bin:/usr/bin:/bin" HOME="$TMP/home" python3 "$NP" synthesize "질문" 2>&1)"
ok "synthesize with no evidence reports 기록 없음 without calling a backend" \
  '[ "$out" = "기록 없음" ]'

# dialectic must be untouched — this change is a measurement surface, and a
# session-path regression here would move live recall behaviour.
cat > "$syn_bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "DIALECTIC-STUB"
EOF
chmod +x "$syn_bin/claude"
out="$(PATH="$syn_bin:/usr/bin:/bin" HOME="$TMP/home" python3 "$NP" dialectic "모델" 2>&1)"
ok "dialectic still synthesizes from peer facts, unchanged" \
  'grep -q "DIALECTIC-STUB" <<<"$out"'

# ---- #1263: silent fallback degradation must surface, not hide ------------
# nosuk/jingun/bangtong ran on codex for an unknown time because a working
# fallback looks identical to a healthy primary from the outside. Each
# llm_synthesize() call now appends to a small rolling status file; snapshot()
# and `backend-status` must turn "primary keeps losing to the fallback" into
# a visible warning instead of quiet, indefinite substitution.
#
# NUNCHI_DB is `export`-ed near the top of this file (line ~20) for the rest
# of the suite, so every call below pins its own NUNCHI_DB/NUNCHI_SNAPSHOT
# explicitly — relying on HOME-derived defaults here would silently read and
# write the shared suite-wide DB instead of this section's isolated one.
health_home="$TMP/home-backend-health"; mkdir -p "$health_home"
health_bin="$TMP/bin-health"; mkdir -p "$health_bin"
HDB="$health_home/.nunchi/facts.db"
HSNAP="$health_home/.nunchi/snapshot.md"
hdialectic() { NUNCHI_DB="$HDB" NUNCHI_SNAPSHOT="$HSNAP" PATH="$health_bin:/usr/bin:/bin" HOME="$health_home" python3 "$NP" dialectic "모델" >/dev/null 2>&1; }
hstatus()    { NUNCHI_DB="$HDB" NUNCHI_SNAPSHOT="$HSNAP" PATH="$health_bin:/usr/bin:/bin" HOME="$health_home" python3 "$NP" backend-status 2>&1; }
hsnapshot()  { NUNCHI_DB="$HDB" NUNCHI_SNAPSHOT="$HSNAP" python3 "$NP" snapshot --limit 1; }

cat > "$health_bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "CLAUDE-ANSWER"
EOF
cat > "$health_bin/codex" <<'EOF'
#!/usr/bin/env bash
echo "session log line"
echo "CODEX-ANSWER"
EOF
chmod +x "$health_bin/claude" "$health_bin/codex"

# One healthy call: primary (claude) wins, state stays ok, no warning line.
hdialectic
snap="$(hsnapshot)"
ok "snapshot stays clean while the primary backend is answering" \
  '! grep -q "합성 백엔드" <<<"$snap"'

# Take claude down: three straight calls fall to codex — #1263's exact pattern.
cat > "$health_bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "Not logged in · Please run /login"
EOF
chmod +x "$health_bin/claude"
for _ in 1 2 3; do hdialectic; done
out="$(hstatus)"
ok "backend-status reports degraded once the fallback carries most calls" \
  'grep -q "^state: degraded" <<<"$out"'
snap="$(hsnapshot)"
ok "snapshot surfaces the degraded-backend warning" \
  'grep -q "강등됨" <<<"$snap"'

# Both backends dead: state escalates to outage, distinct from degraded.
cat > "$health_bin/codex" <<'EOF'
#!/usr/bin/env bash
echo "Not logged in · Please run /login"
EOF
chmod +x "$health_bin/codex"
hdialectic
out="$(hstatus)"
ok "backend-status escalates to outage when every backend fails" \
  'grep -q "^state: outage" <<<"$out"'
snap="$(hsnapshot)"
ok "snapshot escalates its wording for a full backend outage" \
  'grep -q "전멸" <<<"$snap"'

# Recovery: enough healthy primary calls to flush the bad entries out of the
# rolling window (_BACKEND_DEGRADED_WINDOW=5) must clear the state — a single
# good call should not still read "degraded" off stale window entries.
cat > "$health_bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "CLAUDE-ANSWER"
EOF
chmod +x "$health_bin/claude"
for _ in 1 2 3 4 5; do hdialectic; done
out="$(hstatus)"
ok "backend-status clears back to ok once the primary is healthy again" \
  'grep -q "^state: ok" <<<"$out"'

# Cross-file constant check, same precedent as SYNTH_UNAVAILABLE_RE (#1072):
# a no-record phrasing known to one file and not the other silently rescores
# retrieval quality — that is exactly how gwakga's q6 paraphrase was counted
# as a success.
py_nr="$(python3 -c "
import sys; sys.path.insert(0, '$HERE')
from nunchi import NO_RECORD_RE
print(NO_RECORD_RE.pattern)")"
sh_nr="$(sed -n 's/^NO_RECORD_RE="\${NUNCHI_BENCH_NO_RECORD_RE:-\(.*\)}"$/\1/p' "$HERE/bench.sh")"
ok "bench.sh and nunchi.py share one no-record pattern" \
  '[ -n "$sh_nr" ] && [ "$py_nr" = "$sh_nr" ]'
ok "the no-record pattern covers the paraphrase that was scored as success" \
  'python3 -c "
import sys; sys.path.insert(0, \"$HERE\")
from nunchi import NO_RECORD_RE
import sys as s
s.exit(0 if NO_RECORD_RE.search(\"질문한 내용에 대한 근거 기록이 없습니다.\") else 1)"'

# ---- 4. G5: decision must carry a reason (#1264) --------------------------
# A decision without its why gets blindly re-litigated or blindly obeyed.
# G5 flags (never rejects) reasonless decisions; the reason rides a structured
# `because` field or an inline marker in the sentence itself.
payloadb() {  # payloadb <sid> <kind> <subject> <text> [because]
  if [ -n "${5:-}" ]; then
    printf '{"session_id":"%s","distilled_at":"2026-08-24T00:00:00+00:00","honcho":[{"kind":"%s","subject":"%s","text":"%s","because":"%s"}]}' "$1" "$2" "$3" "$4" "$5"
  else
    printf '{"session_id":"%s","distilled_at":"2026-08-24T00:00:00+00:00","honcho":[{"kind":"%s","subject":"%s","text":"%s"}]}' "$1" "$2" "$3" "$4"
  fi
}
# isolated observed peer so G3/G1 cannot interfere with the G5 assertions
out="$(CCC_NODE=nosuk payloadb sg5a decision node "노드 백업 정책을 주간 수동으로 확정" | CCC_NODE=nosuk python3 "$NP" ingest - 2>&1)"
ok "G5: reasonless decision still ingested (flag, never reject)" 'grep -q "ingested 1/1" <<<"$out"'
row="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute(\"SELECT review FROM peer_facts WHERE fact LIKE '%백업 정책%'\").fetchone()[0])")"
ok "G5: reasonless decision flagged review=1" '[ "$row" = "1" ]'

CCC_NODE=nosuk payloadb sg5b decision node "측정 비용 때문에 백업 자동화를 보류했다" | CCC_NODE=nosuk python3 "$NP" ingest - >/dev/null
row="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute(\"SELECT review FROM peer_facts WHERE fact LIKE '%백업 자동화%'\").fetchone()[0])")"
ok "G5: inline-reason decision not flagged" '[ "$row" = "0" ]'

CCC_NODE=nosuk payloadb sg5c decision node "로그 보관을 30일로 결정" "디스크 상한 80% 정책 때문" | CCC_NODE=nosuk python3 "$NP" ingest - >/dev/null
row="$(python3 -c "import sqlite3;print(*sqlite3.connect('$NUNCHI_DB').execute(\"SELECT review, because FROM peer_facts WHERE fact LIKE '%로그 보관%'\").fetchone())")"
ok "G5: structured because stored, not flagged" '[ "$row" = "0 디스크 상한 80% 정책 때문" ]'

out="$(python3 "$NP" recall "로그 보관" 2>&1)"
ok "recall prints the because rationale" 'grep -q "근거: 디스크 상한 80%" <<<"$out"'

# annotate — owner backfill for pre-G5 decisions; review stays owner-cleared
out="$(python3 "$NP" annotate 99999 --because "없는 id" 2>&1)"; rc=$?
ok "annotate rejects unknown fact id" '[ "$rc" != 0 ]'
fid="$(python3 -c "import sqlite3;print(sqlite3.connect('$NUNCHI_DB').execute(\"SELECT id FROM peer_facts WHERE fact LIKE '%백업 정책%'\").fetchone()[0])")"
python3 "$NP" annotate "$fid" --because "자동화 실패 이력(2026-06) 때문" >/dev/null
row="$(python3 -c "import sqlite3;print(*sqlite3.connect('$NUNCHI_DB').execute(\"SELECT because, review FROM peer_facts WHERE id=$fid\").fetchone())")"
ok "annotate backfills because; review flag stays explicit-clear-only" \
  '[ "$row" = "자동화 실패 이력(2026-06) 때문 1" ]'

# dialectic evidence must carry the structured because so q7-class reason
# questions can be answered from peer facts instead of failing to wiki-only.
cat > "$syn_bin/claude" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" > "${DIALECTIC_PROMPT_CAPTURE:?}"
echo "STUB-OK"
EOF
chmod +x "$syn_bin/claude"
out="$(DIALECTIC_PROMPT_CAPTURE="$TMP/dcap" PATH="$syn_bin:/usr/bin:/bin" HOME="$TMP/home" python3 "$NP" dialectic "로그 보관" 2>&1)"
ok "dialectic evidence carries the structured because" \
  'grep -q "근거: 디스크 상한 80% 정책 때문" "$TMP/dcap" && grep -q "STUB-OK" <<<"$out"'

# ---- 5. stdin decode hardening (#1264 follow-up) ----------------------------
# A byte-capped producer (head -c) can truncate stdin mid-multibyte. The bench
# measured a raw UnicodeDecodeError traceback leaking into the q9 wiki-lane
# report on 2026-08-24; stdin reads must decode tolerantly instead.
cat > "$syn_bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "SYN-OK"
EOF
chmod +x "$syn_bin/claude"
out="$(python3 -c "
import sys
sys.stdout.buffer.write('질문 근거 텍스트 — 페이지 경로'.encode('utf-8')[:16])" \
  | PATH="$syn_bin:/usr/bin:/bin" HOME="$TMP/home" python3 "$NP" synthesize "질문" 2>&1)"
ok "synthesize survives truncated multibyte stdin (no traceback)" \
  '! grep -q "Traceback\|UnicodeDecodeError" <<<"$out"'
out="$(python3 -c "
import sys
sys.stdout.buffer.write('{\"session_id\":\"strunc\",\"honcho\":[{\"kind\":\"fact\",\"subject\":\"node\",\"text\":\"짤린 텍스트'.encode('utf-8'))" \
  | CCC_NODE=nosuk python3 "$NP" ingest - 2>&1)"; rc=$?
ok "ingest rejects truncated stdin cleanly (no traceback, nonzero exit)" \
  '[ "$rc" != 0 ] && ! grep -q "Traceback\|UnicodeDecodeError" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
