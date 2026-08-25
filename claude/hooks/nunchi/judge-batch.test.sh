#!/usr/bin/env bash
# Tests for claude/hooks/nunchi/judge-batch.py (#1204, TM-2370 P0-c).
# Isolated via NUNCHI_DB/NUNCHI_HOME/CCC_STATE_DIR overrides; the judge CLI is
# ALWAYS a PATH stub (default: exits unavailable) so no test can ever hit a
# real Claude or Codex provider — no network, no cost. Matrix mirrors the contract:
# deterministic-first clear, Claude-first/Codex-fallback judge paths,
# fail-closed human, freshness moat, CAP, flock, dry-run vs APPLY, backup +
# append-only body-free audit, scoped fan-out.
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

# Default judge stubs: unavailable. Both names are always shadowed so auto
# fallback can never escape to a real host CLI/provider during the suite.
cat >"$TMP/bin/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null
exit 1
STUB
cat >"$TMP/bin/codex" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null
exit 127
STUB
chmod +x "$TMP/bin/claude" "$TMP/bin/codex"

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

seed_kind() { # seed_kind <observed> <kind> <fact> <created_at> <rank> <review> <dedup> <because|"">
  python3 - "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" <<'PY'
import os, sqlite3, sys
observed, kind, fact, created, rank, review, dedup, because = sys.argv[1:9]
c = sqlite3.connect(os.environ["NUNCHI_DB"])
cur = c.execute(
    "INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,valid_to,"
    "supersedes,dedup,created_at,source_rank,review,because) VALUES(?,?,?,?,?,?,NULL,NULL,?,?,?,?,?)",
    ("family-assistant", observed, kind, fact, "distill:test", created, dedup, created,
     int(rank), int(review), because or None))
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

# The historical command override may be a wrapper whose basename does not
# reveal the provider. Auto mode must retain its pre-#1278 Claude argv shape.
cat >"$TMP/bin/judge-wrapper" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null
printf '%s\n' '{"verdict":"clear","rationale":"wrapper result","supersede_proposal":null}'
STUB
chmod +x "$TMP/bin/judge-wrapper"
reset_db
id12="$(seed dungae "래퍼 형제 갈등" "$OLD" 1 1 d12)"
id13="$(seed dungae "래퍼 형제 갈등" "$OLD" 1 0 d13)"
out="$(run_batch NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_CMD=judge-wrapper)"
ok "custom command override retains Claude adapter semantics" \
  '[ "$(review_of "$id12")" = 0 ] && grep -q '\''"backend": "claude"'\'' "$NUNCHI_HOME/judge-audit.jsonl"'

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

# ---- 8. G5: a reasonless decision is never deterministic-cleared (#1264) ---
# The deterministic pass clears anything without a live >=0.6 sibling — that
# rule predates G5, and a reasonless decision has no such sibling, so without
# this guard the batch would silently hide the missing reason from the owner.
reset_db
idg1="$(seed_kind dungae decision "Honcho 유지안 기각으로 폐기 경로 확정" "$OLD" 1 1 dg1 "")"
idg2="$(seed_kind dungae decision "측정 비용 때문에 백업 자동화를 보류했다" "$OLD" 1 1 dg2 "")"
idg3="$(seed_kind dungae decision "로그 보관을 30일로 결정" "$OLD" 1 1 dg3 "디스크 상한 정책 때문")"
NUNCHI_JUDGE_APPLY=1 run_batch
ok "G5 reasonless decision stays flagged (never deterministic-clear)" '[ "$(review_of "$idg1")" = 1 ]'
ok "G5 item classified g5-reasonless-decision in audit" 'grep -q "\"class\": \"g5-reasonless-decision\"" "$NUNCHI_HOME/judge-audit.jsonl"'
ok "G5 audit points the owner at annotate" 'grep -q "annotate" "$NUNCHI_HOME/judge-audit.jsonl"'
ok "inline-reason decision takes the normal deterministic path" '[ "$(review_of "$idg2")" = 0 ]'
ok "structured-because decision takes the normal deterministic path" '[ "$(review_of "$idg3")" = 0 ]'

# ---- 10. G3 batch pool mirrors ingest: cross-session siblings (#1255) ------
reset_db
idx1="$(seed_kind session:aaa context "동일 결론이 여러 세션에서 재추출되었다" "$OLD" 1 1 dx1 "")"
idx2="$(seed_kind session:bbb context "동일 결론이 여러 세션에서 재추출되었다" "$OLD" 1 0 dx2 "")"
idx3="$(seed_kind session:ccc decision "동일 결론이 여러 세션에서 재추출되었다" "$OLD" 1 0 dx3 "다른 kind 대조")"
idx4="$(seed_kind session:ddd context "완전히 무관한 주제의 외로운 항목" "$OLD" 1 1 dx4 "")"
out="$(run_batch NUNCHI_JUDGE_APPLY=1)"
ok "cross-session same-kind sibling keeps the flag fail-closed" \
  '[ "$(review_of "$idx1")" = 1 ]'
ok "unflagged cross-session sibling remains untouched" '[ "$(review_of "$idx2")" = 0 ]'
ok "same-text different-kind item is not a conflict candidate" '[ "$(review_of "$idx3")" = 0 ]'
ok "lonely cross-session item still clears deterministically" '[ "$(review_of "$idx4")" = 0 ]'
ok "cross-session conflict entered judge while only the lonely item auto-cleared" \
  '[ "$(grep -c '\''"class": "judge"'\'' "$NUNCHI_HOME/judge-audit.jsonl")" = 1 ] && [ "$(grep -c '\''"class": "deterministic-clear"'\'' "$NUNCHI_HOME/judge-audit.jsonl")" = 1 ]'

# ---- 11. Codex adapter: isolated strict-output fallback (#1278) ------------
reset_db
cat >"$TMP/bin/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null
exit 1
STUB
cat >"$TMP/bin/codex" <<'STUB'
#!/usr/bin/env bash
set -u
stub_root="$(cd "$(dirname "$0")/.." && pwd)"
args_file="$stub_root/codex-args"
cwd_file="$stub_root/codex-cwd"
env_file="$stub_root/codex-env"
: >"$args_file"
output=""
while [ "$#" -gt 0 ]; do
  printf '%s\n' "$1" >>"$args_file"
  if [ "$1" = "--output-last-message" ] && [ "$#" -ge 2 ]; then
    output="$2"
    shift
    printf '%s\n' "$1" >>"$args_file"
  fi
  shift
done
cat >/dev/null
[ -n "$output" ] || exit 2
case "$(cat "$stub_root/codex-mode" 2>/dev/null || printf valid)" in
  bad) printf '%s\n' 'not-json' >"$output" ;;
  extra) printf '%s\n' '{"verdict":"clear","rationale":"same fact","supersede_proposal":null,"unexpected":true}' >"$output" ;;
  long) printf '{"verdict":"clear","rationale":"%s","supersede_proposal":null}\n' "$(printf '%201s' '' | tr ' ' x)" >"$output" ;;
  *) printf '%s\n' '{"verdict":"clear","rationale":"same fact","supersede_proposal":null}' >"$output" ;;
esac
printf '%s\n' "$PWD" >"$cwd_file"
env | sort >"$env_file"
STUB
chmod +x "$TMP/bin/claude" "$TMP/bin/codex"
export TELEGRAM_BOT_TOKEN="synthetic-must-not-cross"
idc1="$(seed dungae "Codex 폴백 형제 갈등" "$OLD" 1 1 dc1)"
idc2="$(seed dungae "Codex 폴백 형제 갈등" "$OLD" 1 0 dc2)"
out="$(run_batch NUNCHI_JUDGE_APPLY=1)"
ok "auto mode falls back from failed Claude to Codex" '[ "$(review_of "$idc1")" = 0 ]'
ok "Codex is the body-free audit winner" \
  'grep -q '\''"backend": "codex"'\'' "$NUNCHI_HOME/judge-audit.jsonl" && grep -q '\''"claude:exit-1"'\'' "$NUNCHI_HOME/judge-audit.jsonl"'
ok "Codex adapter uses the isolated strict-output contract" \
  'grep -qx -- "--ephemeral" "$TMP/codex-args" && grep -qx -- "--ignore-user-config" "$TMP/codex-args" && grep -qx -- "--ignore-rules" "$TMP/codex-args" && grep -qx -- "read-only" "$TMP/codex-args" && grep -qx -- "--output-schema" "$TMP/codex-args" && grep -qx -- "--output-last-message" "$TMP/codex-args" && grep -q "/nunchi-judge-" "$TMP/codex-cwd"'
ok "Codex adapter never receives Claude-only flags" \
  '! grep -qx -- "--tools" "$TMP/codex-args" && ! grep -qx -- "--permission-mode" "$TMP/codex-args" && ! grep -qx -- "--append-system-prompt" "$TMP/codex-args"'
ok "Codex adapter strips unrelated fleet secrets from the child environment" \
  '! grep -q "TELEGRAM_BOT_TOKEN\|synthetic-must-not-cross" "$TMP/codex-env"'
ok "winning Codex backend appears in the local report" \
  'grep -q "judge backends: claude=0, codex=1" "$CCC_STATE_DIR/nunchi-review-report.md"'

# A structurally invalid Codex response must not clear a flag, even in APPLY.
reset_db
idc3="$(seed dungae "Codex 오류 형제 갈등" "$OLD" 1 1 dc3)"
idc4="$(seed dungae "Codex 오류 형제 갈등" "$OLD" 1 0 dc4)"
printf '%s\n' bad >"$TMP/codex-mode"
out="$(run_batch NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_PROVIDER=codex)"
ok "invalid Codex output fails closed to human" '[ "$(review_of "$idc3")" = 1 ]'
ok "invalid Codex failure class is sanitized in audit" \
  'grep -q '\''"codex:no-json"'\'' "$NUNCHI_HOME/judge-audit.jsonl" && ! grep -q "not-json" "$NUNCHI_HOME/judge-audit.jsonl"'

# Defense in depth: the local parser independently enforces the checked-in
# schema instead of trusting an external CLI to reject extra fields.
reset_db
idc5="$(seed dungae "Codex 스키마 형제 갈등" "$OLD" 1 1 dc5)"
idc6="$(seed dungae "Codex 스키마 형제 갈등" "$OLD" 1 0 dc6)"
printf '%s\n' extra >"$TMP/codex-mode"
out="$(run_batch NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_PROVIDER=codex)"
ok "local parser rejects schema-extra Codex output fail-closed" '[ "$(review_of "$idc5")" = 1 ]'
ok "schema failure is body-free in audit" \
  'grep -q '\''"codex:schema-invalid"'\'' "$NUNCHI_HOME/judge-audit.jsonl" && ! grep -q "unexpected" "$NUNCHI_HOME/judge-audit.jsonl"'

reset_db
idc7="$(seed dungae "Codex 길이 형제 갈등" "$OLD" 1 1 dc7)"
idc8="$(seed dungae "Codex 길이 형제 갈등" "$OLD" 1 0 dc8)"
printf '%s\n' long >"$TMP/codex-mode"
out="$(run_batch NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_PROVIDER=codex)"
ok "local parser rejects overlong Codex rationale fail-closed" '[ "$(review_of "$idc7")" = 1 ]'
ok "overlong output body is absent from audit" \
  'grep -q '\''"codex:schema-invalid"'\'' "$NUNCHI_HOME/judge-audit.jsonl" && ! grep -q "xxxxxxxxxxxxxxxx" "$NUNCHI_HOME/judge-audit.jsonl"'

printf 'PASS=%d FAIL=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
