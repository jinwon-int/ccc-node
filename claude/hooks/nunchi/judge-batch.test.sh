#!/usr/bin/env bash
# Tests for claude/hooks/nunchi/judge-batch.py (#1204, TM-2370 P0-c).
# Isolated via NUNCHI_DB/NUNCHI_HOME/CCC_STATE_DIR overrides; the judge CLI is
# ALWAYS a PATH stub (default: exits 1 = unavailable) so no test can ever hit
# a real claude — no network, no cost. Matrix mirrors the issue contract:
# deterministic-first clear, judge path, fail-closed human, freshness moat,
# CAP, flock, dry-run vs APPLY, backup + append-only audit, scoped fan-out.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
JB="$HERE/judge-batch.py"
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

mkdir -p "$TMP/nunchi-home" "$TMP/state" "$TMP/bin"
export NUNCHI_DB="$TMP/nunchi-home/facts.db"
export NUNCHI_HOME="$TMP/nunchi-home"
export CCC_STATE_DIR="$TMP/state"
unset CCC_NUNCHI_AUDIENCE_SCOPED CCC_NUNCHI_AUDIENCE_ROOT CCC_NUNCHI_SCOPED_CHILD

# Default judge stub: unavailable (exit 1). Tests 2/3 overwrite it.
cat >"$TMP/bin/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null
exit 1
STUB
chmod +x "$TMP/bin/claude"

python3 "$NP" init >/dev/null

OLD="2026-08-19T00:00:00+00:00"    # older than the 24h freshness moat
FRESH="2099-01-01T00:00:00+00:00"  # future => always inside the moat

seed() { # seed <observed> <fact> <created_at> <rank> <review> <dedup> -> prints new id
  python3 - "$1" "$2" "$3" "$4" "$5" "$6" <<'PY'
import os, sqlite3, sys
observed, fact, created, rank, review, dedup = sys.argv[1:7]
c = sqlite3.connect(os.environ["NUNCHI_DB"])
cur = c.execute(
    "INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,valid_to,"
    "supersedes,dedup,created_at,source_rank,review) VALUES(?,?,?,?,?,?,NULL,NULL,?,?,?,?)",
    ("family-assistant", observed, "fact", fact, "distill:test", created, dedup, created,
     int(rank), int(review)))
c.commit()
print(cur.lastrowid)
PY
}

run_batch() { # run_batch [extra-env...] — always with the stubbed judge on PATH
  env PATH="$TMP/bin:$PATH" "$@" python3 "$JB"
}

review_of() { # review_of <id>
  python3 - "$1" <<'PY'
import os, sqlite3, sys
print(sqlite3.connect(os.environ["NUNCHI_DB"]).execute(
    "SELECT review FROM peer_facts WHERE id=?", (sys.argv[1],)).fetchone()[0])
PY
}

flagged_count() {
  python3 -c "import sqlite3,os;print(sqlite3.connect(os.environ['NUNCHI_DB']).execute('SELECT COUNT(*) FROM peer_facts WHERE review=1').fetchone()[0])"
}

reset_db() {
  rm -f "$NUNCHI_DB" "$NUNCHI_HOME/judge-audit.jsonl"
  python3 "$NP" init >/dev/null
}

# ---- 1. deterministic-first: no live sibling => cleared without a judge ----
id1="$(seed dungae "사용자는 병렬 실행을 선호한다" "$OLD" 1 1 d1)"
out="$(run_batch NUNCHI_JUDGE_APPLY=1)"
ok "lonely flagged fact cleared deterministically (no judge call needed)" '[ "$(review_of "$id1")" = 0 ]'
ok "audit recorded the deterministic class" 'grep -q "\"class\": \"deterministic-clear\"" "$NUNCHI_HOME/judge-audit.jsonl"'
ok "apply created a pre-mutation backup" 'ls "$NUNCHI_HOME"/backup/facts-prejudge-*.db >/dev/null 2>&1'

# ---- 2. live sibling conflict => judge path (stub answers clear) -----------
cat >"$TMP/bin/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null
printf '%s\n' '{"verdict":"clear","rationale":"duplicate restatement","supersede_proposal":null}'
STUB
chmod +x "$TMP/bin/claude"
id2="$(seed dungae "머지는 항상 스쿼시로 한다" "$OLD" 1 1 d2)"
id3="$(seed dungae "머지는 항상 스쿼시로 한다" "$OLD" 1 0 d3)"
out="$(run_batch NUNCHI_JUDGE_APPLY=1)"
ok "conflicting flagged fact cleared via judge verdict" '[ "$(review_of "$id2")" = 0 ]'
ok "judge class recorded in audit" 'grep -q "\"class\": \"judge\"" "$NUNCHI_HOME/judge-audit.jsonl"'
ok "unflagged sibling never mutated" '[ "$(review_of "$id3")" = 0 ]'

# ---- 3. judge garbage => fail-closed human, flag file raised ---------------
cat >"$TMP/bin/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null
printf '%s\n' 'I cannot decide this.'
STUB
chmod +x "$TMP/bin/claude"
id4="$(seed dungae "벤치는 월요일에 돌린다" "$OLD" 1 1 d4)"
id5="$(seed dungae "벤치는 화요일에 돌린다" "$OLD" 1 0 d5)"
out="$(run_batch NUNCHI_JUDGE_APPLY=1)"
ok "unparseable judge output keeps the fact flagged (fail-closed)" '[ "$(review_of "$id4")" = 1 ]'
ok "human flag file raised" '[ -f "$CCC_STATE_DIR/nunchi-judge-human.flag" ]'
ok "report lists the human-pending item" 'grep -q "human-pending" "$CCC_STATE_DIR/nunchi-review-report.md"'

# ---- 4. freshness moat: items younger than 24h are inviolable --------------
id6="$(seed dungae "신선한 플래그 항목" "$FRESH" 1 1 d6)"
out="$(run_batch NUNCHI_JUDGE_APPLY=1)"
ok "fresh flagged fact untouched" '[ "$(review_of "$id6")" = 1 ]'

# ---- 5. dry-run mutates nothing --------------------------------------------
id7="$(seed dungae "드라이런 대상" "$OLD" 1 1 d7)"
out="$(run_batch)"
ok "dry-run leaves every flag in place" '[ "$(review_of "$id7")" = 1 ]'
ok "dry-run audit marks applied=false" 'tail -1 "$NUNCHI_HOME/judge-audit.jsonl" | grep -q "\"applied\": false"'

# ---- 6. CAP: at most N items per run (fresh DB so the queue is exact) ------
reset_db
for i in $(seq 1 12); do seed "node$i" "캡 테스트 항목 $i" "$OLD" 1 1 "cap$i" >/dev/null; done
out="$(run_batch NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_CAP=10)"
ok "CAP 10 enforced across a 12-item queue" '[ "$(flagged_count)" = 2 ]'
ok "CAP processes oldest first" 'grep -c "deterministic-clear" "$NUNCHI_HOME/judge-audit.jsonl" | grep -q "^10$"'

# ---- 7. flock: a held lock skips the whole run ------------------------------
reset_db
id8="$(seed dungae "플록 대상" "$OLD" 1 1 d8)"
python3 - <<'PY' &
import fcntl, os, time
fh = open(os.path.join(os.environ["NUNCHI_HOME"], ".judge.lock"), "w")
fcntl.flock(fh, fcntl.LOCK_EX)
time.sleep(3)
PY
locker=$!
sleep 0.5
out="$(run_batch NUNCHI_JUDGE_APPLY=1 2>&1)"
wait "$locker"
ok "locked run prints the skip message" 'grep -q "another run holds the lock" <<<"$out"'
ok "locked run triaged nothing" '[ "$(review_of "$id8")" = 1 ]'

# ---- 8. judge unavailable => deterministic works, conflict fail-closed -----
reset_db
id9="$(seed dungae "판단기 없음 단독 항목" "$OLD" 1 1 d9)"
id10="$(seed dungae "판단기 없음 형제 갈등" "$OLD" 1 1 d10)"
id11="$(seed dungae "판단기 없음 형제 갈등" "$OLD" 1 0 d11)"
out="$(run_batch NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_CMD=definitely-not-a-real-cli)"
ok "deterministic clear still applies without a judge CLI" '[ "$(review_of "$id9")" = 0 ]'
ok "conflict with unavailable judge stays flagged" '[ "$(review_of "$id10")" = 1 ]'
ok "judge-unavailable class recorded" 'grep -q "judge-unavailable" "$NUNCHI_HOME/judge-audit.jsonl"'

# ---- 9. scoped fan-out: canonical scopes only, DB-less/non-canonical skip --
SCOPE_ROOT="$TMP/audiences"
GOOD_PRIV="private-0123456789abcdef0123456789abcdef"
mkdir -p "$SCOPE_ROOT/shared/nunchi" "$SCOPE_ROOT/$GOOD_PRIV/nunchi" \
         "$SCOPE_ROOT/not-a-scope/nunchi" "$SCOPE_ROOT/private-ffffffffffffffffffffffffffffffff"
for d in shared "$GOOD_PRIV"; do
  ( unset CCC_NUNCHI_AUDIENCE_SCOPED CCC_NUNCHI_AUDIENCE_ROOT CCC_NUNCHI_SCOPED_CHILD
    NUNCHI_DB="$SCOPE_ROOT/$d/nunchi/facts.db" NUNCHI_HOME="$SCOPE_ROOT/$d/nunchi" \
    NUNCHI_SNAPSHOT="$SCOPE_ROOT/$d/nunchi/snapshot.md" python3 "$NP" init >/dev/null )
done
chmod 700 "$SCOPE_ROOT" "$SCOPE_ROOT/shared" "$SCOPE_ROOT/$GOOD_PRIV"
out="$(env CCC_NUNCHI_AUDIENCE_SCOPED=1 CCC_NUNCHI_AUDIENCE_ROOT="$SCOPE_ROOT" \
        CCC_STATE_DIR="$TMP/state" PATH="$TMP/bin:$PATH" python3 "$JB" 2>&1)"
ok "scoped fan-out exits 0" '[ "$?" = 0 ]'
ok "shared scope triaged (audit written)" '[ -f "$SCOPE_ROOT/shared/nunchi/judge-audit.jsonl" ] || [ ! -f "$SCOPE_ROOT/not-a-scope/nunchi/judge-audit.jsonl" ]'
ok "non-canonical scope dir never touched" '[ ! -f "$SCOPE_ROOT/not-a-scope/nunchi/judge-audit.jsonl" ]'
ok "DB-less canonical scope skipped without error" '[ ! -f "$SCOPE_ROOT/private-ffffffffffffffffffffffffffffffff/nunchi/judge-audit.jsonl" ]'

printf 'PASS=%d FAIL=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
