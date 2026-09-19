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
# The Jev backend's availability is a key, not a PATH entry. An inherited real
# key would make the typesafe cases reach api.typesafe.ai for real — network and
# cost in a suite whose whole contract is neither. Unset the env var AND point
# the ~/.secrets/typesafe-api-key file fallback (bridge-shared key file) at a
# nonexistent path for the whole run; the fixtures put a synthetic key in their
# own process environment only.
unset TYPESAFE_API_KEY NUNCHI_JUDGE_MIN_CONFIDENCE
export TYPESAFE_API_KEY_FILE="$TMP/no-such-key-file"

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
# shellcheck disable=SC2034  # id1 is read via eval inside ok()
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
# shellcheck disable=SC2034  # id2 is read via eval inside ok()
id2="$(seed dungae "머지는 항상 스쿼시로 한다" "$OLD" 1 1 d2)"
# shellcheck disable=SC2034  # id3 is read via eval inside ok()
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
# shellcheck disable=SC2034  # id4 is read via eval inside ok()
id4="$(seed dungae "벤치는 월요일에 돌린다" "$OLD" 1 1 d4)"
seed dungae "벤치는 화요일에 돌린다" "$OLD" 1 0 d5 >/dev/null
out="$(run_batch NUNCHI_JUDGE_APPLY=1)"
ok "unparseable judge output keeps the fact flagged (fail-closed)" '[ "$(review_of "$id4")" = 1 ]'
ok "human flag file raised" '[ -f "$CCC_STATE_DIR/nunchi-judge-human.flag" ]'
ok "report lists the human-pending item" 'grep -q "human-pending" "$CCC_STATE_DIR/nunchi-review-report.md"'

# ---- 4. freshness moat: items younger than 24h are inviolable --------------
# shellcheck disable=SC2034  # id6 is read via eval inside ok()
id6="$(seed dungae "신선한 플래그 항목" "$FRESH" 1 1 d6)"
out="$(run_batch NUNCHI_JUDGE_APPLY=1)"
ok "fresh flagged fact untouched" '[ "$(review_of "$id6")" = 1 ]'

# ---- 5. dry-run mutates nothing --------------------------------------------
# shellcheck disable=SC2034  # id7 is read via eval inside ok()
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
# shellcheck disable=SC2034  # id8 is read via eval inside ok()
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
# shellcheck disable=SC2034  # id9 is read via eval inside ok()
id9="$(seed dungae "판단기 없음 단독 항목" "$OLD" 1 1 d9)"
# shellcheck disable=SC2034  # id10 is read via eval inside ok()
id10="$(seed dungae "판단기 없음 형제 갈등" "$OLD" 1 1 d10)"
seed dungae "판단기 없음 형제 갈등" "$OLD" 1 0 d11 >/dev/null
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
# shellcheck disable=SC2034  # id12 is read via eval inside ok()
id12="$(seed dungae "래퍼 형제 갈등" "$OLD" 1 1 d12)"
seed dungae "래퍼 형제 갈등" "$OLD" 1 0 d13 >/dev/null
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
# shellcheck disable=SC2034  # idg1 is read via eval inside ok()
idg1="$(seed_kind dungae decision "Honcho 유지안 기각으로 폐기 경로 확정" "$OLD" 1 1 dg1 "")"
# shellcheck disable=SC2034  # idg2 is read via eval inside ok()
idg2="$(seed_kind dungae decision "측정 비용 때문에 백업 자동화를 보류했다" "$OLD" 1 1 dg2 "")"
# shellcheck disable=SC2034  # idg3 is read via eval inside ok()
idg3="$(seed_kind dungae decision "로그 보관을 30일로 결정" "$OLD" 1 1 dg3 "디스크 상한 정책 때문")"
NUNCHI_JUDGE_APPLY=1 run_batch
ok "G5 reasonless decision stays flagged (never deterministic-clear)" '[ "$(review_of "$idg1")" = 1 ]'
ok "G5 backlog is audited as a deferred aggregate" 'grep -q "\"class\": \"g5-deferred-backlog\"" "$NUNCHI_HOME/judge-audit.jsonl"'
ok "G5 audit points the owner at annotate" 'grep -q "annotate" "$NUNCHI_HOME/judge-audit.jsonl"'
ok "G5 backlog is surfaced in the report" 'grep -q "g5-deferred" "$CCC_STATE_DIR/nunchi-review-report.md"'
ok "inline-reason decision takes the normal deterministic path" '[ "$(review_of "$idg2")" = 0 ]'
ok "structured-because decision takes the normal deterministic path" '[ "$(review_of "$idg3")" = 0 ]'

# ---- 8b. G5 never occupies a CAP slot (head-of-line block) ----------------
# Regression: fetch_queue was `ORDER BY id LIMIT CAP`, and a G5 verdict leaves
# review=1, so the oldest G5 items were re-selected every run and the queue
# behind them was never reached. Measured on yukson before the fix: the same
# ten ids (#747..#994) re-triaged to `human` on eight consecutive days while
# 613 judgeable facts behind them had never once been looked at.
reset_db
# Seed CAP g5 items FIRST so they own the lowest ids, then judgeable ones.
for i in 1 2 3; do
  seed_kind dungae decision "G5 선두 항목 $i 확정" "$OLD" 1 1 "hol-g5-$i" "" >/dev/null
done
# Deliberately unrelated to each other: a >=0.6 mutual overlap would send them
# to the (stubbed-unavailable) judge and mask what this case is measuring.
# shellcheck disable=SC2034  # read via eval inside ok()
idh1="$(seed_kind dungae context "브리지 포트는 8791 이며 루프백에만 바인딩된다" "$OLD" 1 1 hol-ok-1 "")"
# shellcheck disable=SC2034  # read via eval inside ok()
idh2="$(seed_kind dungae preference "사용자는 번호형 선택지를 선호한다" "$OLD" 1 1 hol-ok-2 "")"
out="$(NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_CAP=3 run_batch 2>&1)"
ok "items behind the g5 head are reached despite CAP=3" '[ "$(review_of "$idh1")" = 0 ] && [ "$(review_of "$idh2")" = 0 ]'
ok "run line states the deferred g5 count" 'printf "%s" "$out" | grep -q "3 g5-deferred"'
ok "g5 items still stay flagged for the owner" '[ "$(flagged_count)" = 3 ]'

# ---- 10. G3 batch pool mirrors ingest: cross-session siblings (#1255) ------
reset_db
# shellcheck disable=SC2034  # idx1 is read via eval inside ok()
idx1="$(seed_kind session:aaa context "동일 결론이 여러 세션에서 재추출되었다" "$OLD" 1 1 dx1 "")"
# shellcheck disable=SC2034  # idx2 is read via eval inside ok()
idx2="$(seed_kind session:bbb context "동일 결론이 여러 세션에서 재추출되었다" "$OLD" 1 0 dx2 "")"
# shellcheck disable=SC2034  # idx3 is read via eval inside ok()
idx3="$(seed_kind session:ccc decision "동일 결론이 여러 세션에서 재추출되었다" "$OLD" 1 0 dx3 "다른 kind 대조")"
# shellcheck disable=SC2034  # idx4 is read via eval inside ok()
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
# shellcheck disable=SC2034  # idc1 is read via eval inside ok()
idc1="$(seed dungae "Codex 폴백 형제 갈등" "$OLD" 1 1 dc1)"
seed dungae "Codex 폴백 형제 갈등" "$OLD" 1 0 dc2 >/dev/null
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
# shellcheck disable=SC2034  # idc3 is read via eval inside ok()
idc3="$(seed dungae "Codex 오류 형제 갈등" "$OLD" 1 1 dc3)"
seed dungae "Codex 오류 형제 갈등" "$OLD" 1 0 dc4 >/dev/null
printf '%s\n' bad >"$TMP/codex-mode"
out="$(run_batch NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_PROVIDER=codex)"
ok "invalid Codex output fails closed to human" '[ "$(review_of "$idc3")" = 1 ]'
ok "invalid Codex failure class is sanitized in audit" \
  'grep -q '\''"codex:no-json"'\'' "$NUNCHI_HOME/judge-audit.jsonl" && ! grep -q "not-json" "$NUNCHI_HOME/judge-audit.jsonl"'

# Defense in depth: the local parser independently enforces the checked-in
# schema instead of trusting an external CLI to reject extra fields.
reset_db
# shellcheck disable=SC2034  # idc5 is read via eval inside ok()
idc5="$(seed dungae "Codex 스키마 형제 갈등" "$OLD" 1 1 dc5)"
seed dungae "Codex 스키마 형제 갈등" "$OLD" 1 0 dc6 >/dev/null
printf '%s\n' extra >"$TMP/codex-mode"
out="$(run_batch NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_PROVIDER=codex)"
ok "local parser rejects schema-extra Codex output fail-closed" '[ "$(review_of "$idc5")" = 1 ]'
ok "schema failure is body-free in audit" \
  'grep -q '\''"codex:schema-invalid"'\'' "$NUNCHI_HOME/judge-audit.jsonl" && ! grep -q "unexpected" "$NUNCHI_HOME/judge-audit.jsonl"'

reset_db
# shellcheck disable=SC2034  # idc7 is read via eval inside ok()
idc7="$(seed dungae "Codex 길이 형제 갈등" "$OLD" 1 1 dc7)"
seed dungae "Codex 길이 형제 갈등" "$OLD" 1 0 dc8 >/dev/null
printf '%s\n' long >"$TMP/codex-mode"
# shellcheck disable=SC2034  # out is read via eval inside ok()
out="$(run_batch NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_PROVIDER=codex)"
ok "local parser rejects overlong Codex rationale fail-closed" '[ "$(review_of "$idc7")" = 1 ]'
ok "overlong output body is absent from audit" \
  'grep -q '\''"codex:schema-invalid"'\'' "$NUNCHI_HOME/judge-audit.jsonl" && ! grep -q "xxxxxxxxxxxxxxxx" "$NUNCHI_HOME/judge-audit.jsonl"'

# ---- #1336: TTL-imminent observation evidence is annotated, not silent -----
TTLFIX="$TMP/ttl-fixture.py"
cat > "$TTLFIX" <<'FIXTURE'
import importlib.util, sys
from datetime import datetime, timedelta, timezone
spec = importlib.util.spec_from_file_location("jb_ttl", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["jb_ttl"] = m
spec.loader.exec_module(m)
now = datetime.now(timezone.utc)
# 6.5-day-old observation against the default 7d TTL -> within 24h of expiry
note = m.observation_ttl_note((now - timedelta(days=6, hours=12)).isoformat())
assert note.startswith("observation evidence expires in ~"), note
# young evidence -> silent
assert m.observation_ttl_note((now - timedelta(days=1)).isoformat()) == "", "young"
# sweep disabled -> always silent (policy: no sweep, no expiry race)
m.nunchi._OBSERVATION_TTL_DAYS = 0
assert m.observation_ttl_note((now - timedelta(days=30)).isoformat()) == "", "disabled"
m.nunchi._OBSERVATION_TTL_DAYS = 7
# garbage timestamp -> silent (never block a batch on formatting)
assert m.observation_ttl_note("not-a-date") == "", "garbage"
# the judge prompt carries the note only for near-expiry observations
item_old = (11, "user", "observation", "aging evidence", 1,
            (now - timedelta(days=6, hours=12)).isoformat(), "")
item_fact = (12, "user", "fact", "durable evidence", 1,
             (now - timedelta(days=6, hours=12)).isoformat(), "")
sibs = [(21, "sibling fact")]
assert "Evidence-weight note" in m.build_judge_prompt(item_old, sibs), "prompt-note"
assert "Evidence-weight note" not in m.build_judge_prompt(item_fact, sibs), "prompt-clean"
print("TTL-NOTE-OK")
FIXTURE
python3 "$TTLFIX" "$JB" >/dev/null 2>&1
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "#1336 TTL-imminent observation evidence annotated in the judge prompt" '[ "$rc" = 0 ]'

# ---- 12. TypeSafe Jev backend: a typed verdict, nothing parsed from text ---
# The transport is stubbed at urlopen (the endpoint is pinned in code on
# purpose, so there is no URL env var to redirect at a local server): the
# fixture asserts the exact wire contract AND that the bearer key never leaves
# the Authorization header — not into the payload, the decision, or a failure
# class that later lands in the audit log.
JEVFIX="$TMP/jev-fixture.py"
cat > "$JEVFIX" <<'FIXTURE'
import importlib.util, io, json, os, sys, urllib.error
spec = importlib.util.spec_from_file_location("jb_jev", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["jb_jev"] = m
spec.loader.exec_module(m)

KEY = "synthetic-jev-key-must-not-leak"
os.environ["TYPESAFE_API_KEY"] = KEY
m.JUDGE_PROVIDER = "typesafe"

item = (41, "dungae", "fact", "머지는 항상 스쿼시로 한다", 1,
        "2026-08-19T00:00:00+00:00", "")
sibs = [(42, "머지는 언제나 스쿼시로 한다")]
captured = {}


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self, size=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stub(body):
    payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def _open(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Resp(payload)

    m.urllib.request.urlopen = _open


CLEAR = {"answers": {
    "verdict": {"choice": "clear", "confidence": 0.93,
                "probabilities": {"clear": 0.93, "conflict": 0.05, "human": 0.02}},
    "contradicts": {"noul": 0.05}}}
stub(CLEAR)
decision, failure = m._typesafe_judge(item, sibs)
assert failure is None, failure
assert decision["verdict"] == "clear", decision
assert decision["confidence"] == 0.93, decision
assert decision["supersede_proposal"] is None, decision
assert decision["rationale"] == "jev: clear p=0.93 conf=0.93 contradicts=0.05", \
    decision["rationale"]

# wire contract
assert captured["url"] == "https://api.typesafe.ai/v1/systemone", captured["url"]
assert captured["method"] == "POST", captured["method"]
assert captured["headers"]["authorization"] == "Bearer " + KEY, "auth-header"
assert captured["headers"]["content-type"] == "application/json", "content-type"
assert captured["timeout"] == m.JUDGE_TIMEOUT, "timeout"
body = captured["body"]
assert body["model"] == "jev-latest", body["model"]
assert set(body["questions"]) == {"verdict", "contradicts"}, body["questions"]
assert body["questions"]["verdict"]["type"] == "choice", "verdict-type"
assert set(body["questions"]["verdict"]["criteria"]) == {"clear", "conflict", "human"}, "criteria"
assert body["questions"]["contradicts"]["type"] == "noul", "noul-type"
assert body["questions"]["verdict"]["instructions"], "verdict-instructions"
assert body["questions"]["contradicts"]["instructions"], "contradicts-instructions"
# the state is the judged material only — the rubric lives in the questions
assert "머지는 항상 스쿼시로 한다" in body["state"], "state-fact"
assert "#42" in body["state"], "state-sibling"
assert "Decide one verdict" not in body["state"], "state-rubric-leak"
assert "JSON" not in body["state"], "state-json-contract-leak"
# key redaction: header only, nowhere else
assert KEY not in json.dumps(body, ensure_ascii=False), "key-in-payload"
assert KEY not in json.dumps(decision, ensure_ascii=False), "key-in-decision"

# a missing `contradicts` degrades the rationale, it does not fail the verdict
stub({"answers": {"verdict": {"choice": "clear", "confidence": 0.8}}})
decision, failure = m._typesafe_judge(item, sibs)
assert failure is None and decision["rationale"] == "jev: clear p=n/a conf=0.80 contradicts=n/a", \
    decision

# conflict: Jev chooses, it does not write — the proposal stays a human's
stub({"answers": {
    "verdict": {"choice": "conflict", "confidence": 0.62,
                "probabilities": {"clear": 0.20, "conflict": 0.62, "human": 0.18}},
    "contradicts": {"noul": 0.88}}})
decision, failure = m._typesafe_judge(item, sibs)
assert failure is None, failure
assert decision["verdict"] == "conflict", decision
assert decision["supersede_proposal"] is None, decision
assert "사람이 작성" in decision["rationale"], decision["rationale"]
assert len(decision["rationale"]) <= 200, len(decision["rationale"])

# judge_item integration: backend attribution + confidence reach the decision
stub(CLEAR)
routed = m.judge_item(item, sibs)
assert routed["backend"] == "typesafe", routed
assert routed["confidence"] == 0.93, routed
assert routed["verdict"] == "clear", routed
assert m.judge_available() is True, "available-with-key"

# no key => the candidate drops out; no crash, fail-closed to human
for absent in ("", "   "):
    os.environ["TYPESAFE_API_KEY"] = absent
    assert m.judge_candidates() == [("typesafe", "")], m.judge_candidates()
    assert m.judge_available() is False, "available-without-key:" + repr(absent)
    assert m._typesafe_judge(item, sibs) == (None, "no-key"), "no-key"
    unavailable = m.judge_item(item, sibs)
    assert unavailable["verdict"] == "human", unavailable
    assert unavailable["backend"] is None, unavailable
    assert unavailable["confidence"] is None, unavailable
    assert unavailable["attempts"] == ["typesafe:unavailable"], unavailable
del os.environ["TYPESAFE_API_KEY"]
assert m.judge_available() is False, "available-when-unset"
os.environ["TYPESAFE_API_KEY"] = KEY


# a provider error must not smuggle the key into a failure class
def raising(request, timeout=None):
    raise urllib.error.HTTPError(
        m.TYPESAFE_URL + "?leak=" + KEY, 401, "Unauthorized " + KEY, {},
        io.BytesIO(b"denied " + KEY.encode()))


m.urllib.request.urlopen = raising
assert m._typesafe_judge(item, sibs) == (None, "http-401"), "http-failure-class"
failed = m.judge_item(item, sibs)
assert failed["verdict"] == "human" and failed["attempts"] == ["typesafe:http-401"], failed
assert KEY not in json.dumps(failed, ensure_ascii=False), "key-in-attempts"

# malformed / out-of-rubric answers are fail-closed, never guessed
for payload, expected in (
        ({"answers": {"verdict": {"choice": "clear"}}}, "confidence-missing"),
        ({"answers": {"verdict": {"choice": "clear", "confidence": 1.5}}}, "confidence-missing"),
        ({"answers": {"verdict": {"choice": "clear", "confidence": "high"}}}, "confidence-missing"),
        ({"answers": {"verdict": {"choice": "rm -rf", "confidence": 0.99}}}, "verdict-outside-rubric"),
        ({"answers": {"verdict": "clear"}}, "schema-invalid"),
        ({"answers": []}, "schema-invalid"),
        (b"not json at all", "response-unparseable"),
        (b"", "empty"),
):
    stub(payload)
    result, failure = m._typesafe_judge(item, sibs)
    assert result is None and failure == expected, (expected, failure)

# a confident-zero answer is a real answer (and the gate's job, not the parser's)
stub({"answers": {"verdict": {"choice": "clear", "confidence": 0.0}}})
decision, failure = m._typesafe_judge(item, sibs)
assert failure is None and decision["confidence"] == 0.0, decision
print("JEV-OK")
FIXTURE
jev_out="$(python3 "$JEVFIX" "$JB" 2>&1)"
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "Jev backend turns a typed HTTP answer into verdict+confidence (no text parsing)" \
  '[ "$rc" = 0 ] && [ "$jev_out" = "JEV-OK" ]'
[ "$rc" = 0 ] || printf '%s\n' "$jev_out"

# End-to-end: provider=typesafe with no key must degrade, not crash.
reset_db
# shellcheck disable=SC2034  # idt1 is read via eval inside ok()
idt1="$(seed dungae "Jev 키 없음 형제 갈등" "$OLD" 1 1 dt1)"
seed dungae "Jev 키 없음 형제 갈등" "$OLD" 1 0 dt2 >/dev/null
run_batch NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_PROVIDER=typesafe >/dev/null 2>&1
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "typesafe without TYPESAFE_API_KEY exits cleanly (key-based availability, no crash)" '[ "$rc" = 0 ]'
ok "typesafe without a key fails closed to human" '[ "$(review_of "$idt1")" = 1 ]'
ok "typesafe without a key is recorded as judge-unavailable" \
  'grep -q "judge-unavailable" "$NUNCHI_HOME/judge-audit.jsonl"'

# ---- 12b. typesafe key-file fallback (bridge ~/.secrets/typesafe-api-key) ---
# Same file the bridge jev-skill-advice feature reads, same safety contract
# (owned regular file, no group/other bits, O_NOFOLLOW leaf, private parent).
# An unsafe shape must degrade to "no key" — never raise, never log content.
KEYFIX="$TMP/key-fixture.py"
cat > "$KEYFIX" <<'FIXTURE'
import importlib.util, os, sys, tempfile
spec = importlib.util.spec_from_file_location("jb_key", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["jb_key"] = m
spec.loader.exec_module(m)

home = tempfile.mkdtemp()
# Point expanduser at the fixture home FIRST — the runner's real ~/.secrets
# must never be reachable from this test.
os.environ["HOME"] = home
secrets = os.path.join(home, ".secrets")
os.makedirs(secrets)
keypath = os.path.join(secrets, "typesafe-api-key")
os.chmod(secrets, 0o700)
os.environ.pop("TYPESAFE_API_KEY", None)
os.environ.pop("TYPESAFE_API_KEY_FILE", None)

assert m.typesafe_key() == "", "no file -> empty"

with open(keypath, "w") as fh:
    fh.write("file-secret-abc\n")
os.chmod(keypath, 0o600)
assert m.typesafe_key() == "file-secret-abc", "safe file -> key"

os.environ["TYPESAFE_API_KEY"] = "env-secret"
assert m.typesafe_key() == "env-secret", "env precedence over file"
del os.environ["TYPESAFE_API_KEY"]

os.chmod(keypath, 0o640)
assert m.typesafe_key() == "", "group-readable file -> empty"
os.chmod(keypath, 0o600)

os.rename(keypath, keypath + ".real")
os.symlink(keypath + ".real", keypath)
assert m.typesafe_key() == "", "leaf symlink -> empty"
os.rename(keypath + ".real", keypath)

os.chmod(secrets, 0o777)
assert m.typesafe_key() == "", "group/other-writable parent -> empty"
os.chmod(secrets, 0o700)

with open(keypath, "w") as fh:
    fh.write("bad\nkey")
assert m.typesafe_key() == "", "control char content -> empty"
alt = os.path.join(home, "alt-key")
with open(alt, "w") as fh:
    fh.write("alt-secret")
os.chmod(alt, 0o600)
os.environ["TYPESAFE_API_KEY_FILE"] = alt
assert m.typesafe_key() == "alt-secret", "explicit TYPESAFE_API_KEY_FILE override"
os.chmod(alt, 0o644)
assert m.typesafe_key() == "", "unsafe override file -> empty"
del os.environ["TYPESAFE_API_KEY_FILE"]
print("KEY-OK")
FIXTURE
key_out="$(python3 "$KEYFIX" "$JB" 2>&1)"
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "typesafe key resolves from owner-only ~/.secrets file; unsafe shapes degrade to empty" \
  '[ "$rc" = 0 ] && [ "$key_out" = "KEY-OK" ]'
[ "$rc" = 0 ] || printf '%s\n' "$key_out"

# ---- 13. NUNCHI_JUDGE_MIN_CONFIDENCE gate ---------------------------------
# The gate only ever holds a decision that CARRIES a confidence. haiku/codex
# report none, so a threshold must leave their clears exactly as they were —
# that regression (a threshold silently freezing every CLI-backed clear) is what
# the `confidence is None` case below pins down.
GATEFIX="$TMP/gate-fixture.py"
cat > "$GATEFIX" <<'FIXTURE'
import importlib.util, json, sqlite3, sys
spec = importlib.util.spec_from_file_location("jb_gate", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["jb_gate"] = m
spec.loader.exec_module(m)

low_id, high_id, none_id, guard_id = (int(a) for a in sys.argv[2:6])
expected = float(sys.argv[6])


def clear_decision(fact_id, backend, confidence):
    return {"id": fact_id, "class": "judge", "verdict": "clear",
            "rationale": "fixture", "supersede_proposal": None,
            "backend": backend, "attempts": [], "confidence": confidence}


low = clear_decision(low_id, "typesafe", 0.5)     # typed, under any real gate
high = clear_decision(high_id, "typesafe", 0.95)  # typed, over it
blind = clear_decision(none_id, "claude", None)   # free-text backend, no number
# Every mutation happens before the first assert on purpose: an early
# AssertionError must not be able to hide what the apply path did to the DB,
# because the shell-side review flag checks are the load-bearing assertions.
conn = sqlite3.connect(m.DB)
clears, applied, backup, held = m.apply_decisions(conn, [low, high, blind])
# defense in depth: the mutation itself refuses a gated decision
guard_gate = m.apply_clear(conn, guard_id, {"confidence": 0.1})
conn.commit()
guard_review = conn.execute(
    "SELECT review FROM peer_facts WHERE id=?", (guard_id,)).fetchone()[0]
conn.close()
report = m.build_report("ts", [low, high, blind], clears, [], applied, backup, (), held)
print(json.dumps({"applied": applied, "held": [d["id"] for d in held]}))
assert m.MIN_CONFIDENCE == expected, (m.MIN_CONFIDENCE, expected)
assert m.APPLY is True, "fixture needs APPLY"
if expected > 0.0:
    assert guard_gate is False, "apply_clear-gate"
    assert guard_review == 1, "apply_clear-gate-mutated"
    assert [d["id"] for d in held] == [low_id], held
    assert low["class"] == "low-confidence", low
    assert low["applied"] is False, low
    assert high["class"] == "judge" and blind["class"] == "judge", (high, blind)
    assert applied == 2, applied
    assert "confidence gate" in report, "report-gate-line"
    assert "low-confidence" in report, "report-held-section"
else:
    # positive control for the same call: with no gate it really does clear
    assert guard_gate is True, "apply_clear-no-gate"
    assert guard_review == 0, "apply_clear-no-gate-unmutated"
    assert held == [], held
    assert applied == 3, applied
    assert low["class"] == "judge", low
    assert "confidence gate" not in report, "report-gate-line-when-off"
assert "| conf |" in report, "report-conf-column"
FIXTURE

reset_db
idg_low="$(seed dungae "저신뢰 판정 대상" "$OLD" 1 1 cf1)"
idg_high="$(seed dungae "고신뢰 판정 대상" "$OLD" 1 1 cf2)"
idg_none="$(seed dungae "무신뢰 백엔드 대상" "$OLD" 1 1 cf3)"
idg_guard="$(seed dungae "직접 호출 방어 대상" "$OLD" 1 1 cf3g)"
gate_out="$(env NUNCHI_JUDGE_APPLY=1 NUNCHI_JUDGE_MIN_CONFIDENCE=0.9 \
  python3 "$GATEFIX" "$JB" "$idg_low" "$idg_high" "$idg_none" "$idg_guard" 0.9 2>&1)"
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "gate fixture ran (threshold 0.9 parsed from the environment)" '[ "$rc" = 0 ]'
[ "$rc" = 0 ] || printf '%s\n' "$gate_out"
ok "confidence 0.5 clear under a 0.9 gate is not applied" '[ "$(review_of "$idg_low")" = 1 ]'
ok "gated clear is reclassified low-confidence" 'printf "%s" "$gate_out" | grep -q "\"held\": \[$idg_low\]"'
ok "confidence 0.95 clear passes the same gate" '[ "$(review_of "$idg_high")" = 0 ]'
# THE regression guard: haiku/codex return no confidence at all. If a missing
# confidence were treated as 0.0, setting any threshold would silently stop
# every CLI-backed clear in the fleet.
ok "confidence-less (haiku/codex) clear still applies under a 0.9 gate" \
  '[ "$(review_of "$idg_none")" = 0 ]'
ok "apply_clear itself refuses a gated decision (defense in depth)" \
  '[ "$(review_of "$idg_guard")" = 1 ]'

reset_db
idg2_low="$(seed dungae "기본값 저신뢰 대상" "$OLD" 1 1 cf4)"
idg2_high="$(seed dungae "기본값 고신뢰 대상" "$OLD" 1 1 cf5)"
idg2_none="$(seed dungae "기본값 무신뢰 대상" "$OLD" 1 1 cf6)"
idg2_guard="$(seed dungae "기본값 직접 호출 대상" "$OLD" 1 1 cf6g)"
gate_out="$(env NUNCHI_JUDGE_APPLY=1 \
  python3 "$GATEFIX" "$JB" "$idg2_low" "$idg2_high" "$idg2_none" "$idg2_guard" 0.0 2>&1)"
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "default (no NUNCHI_JUDGE_MIN_CONFIDENCE) keeps the pre-gate behavior" \
  '[ "$rc" = 0 ] && [ "$(review_of "$idg2_low")" = 0 ] && [ "$(review_of "$idg2_high")" = 0 ] && [ "$(review_of "$idg2_none")" = 0 ]'
[ "$rc" = 0 ] || printf '%s\n' "$gate_out"

# Out-of-range / unparseable thresholds are bounded, never crash the batch.
BOUNDFIX="$TMP/bound-fixture.py"
cat > "$BOUNDFIX" <<'FIXTURE'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("jb_bound", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["jb_bound"] = m
spec.loader.exec_module(m)
b = m.bounded_float_env
for raw, want in (("0.5", 0.5), ("2", 1.0), ("-3", 0.0), ("junk", 0.0),
                  ("nan", 0.0), ("", 0.0)):
    got = b({"K": raw}, "K", 0.0, 0.0, 1.0, clamp=True)
    assert got == want, (raw, got, want)
assert b({}, "K", 0.0, 0.0, 1.0, clamp=True) == 0.0, "unset"
# a gate of 0.0 is a constant False, whatever the decision carries
m.MIN_CONFIDENCE = 0.0
for confidence in (None, 0.0, 0.5, "junk"):
    assert m.confidence_below_gate({"confidence": confidence}) is False, confidence
m.MIN_CONFIDENCE = 0.5
assert m.confidence_below_gate({"confidence": None}) is False, "none-passes"
assert m.confidence_below_gate({}) is False, "absent-passes"
assert m.confidence_below_gate({"confidence": 0.5}) is False, "at-threshold-passes"
assert m.confidence_below_gate({"confidence": 0.49}) is True, "below-holds"
# present but unusable is fail-closed, the same direction as a bad verdict
assert m.confidence_below_gate({"confidence": "high"}) is True, "garbage-holds"
assert m.confidence_below_gate({"confidence": float("nan")}) is True, "nan-holds"
print("BOUND-OK")
FIXTURE
bound_out="$(python3 "$BOUNDFIX" "$JB" 2>&1)"
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "confidence threshold is bounded to [0,1] and a 0.0 gate is inert" \
  '[ "$rc" = 0 ] && [ "$bound_out" = "BOUND-OK" ]'
[ "$rc" = 0 ] || printf '%s\n' "$bound_out"

# A normal (CLI-backend) run still audits/reports exactly as before, plus a
# null confidence column.
reset_db
seed dungae "기본 감사 확인 항목" "$OLD" 1 1 cn1 >/dev/null
run_batch NUNCHI_JUDGE_APPLY=1 >/dev/null
ok "audit records a null confidence for backends that report none" \
  'grep -q "\"confidence\": null" "$NUNCHI_HOME/judge-audit.jsonl"'
ok "no gate line in the report when the gate is unset" \
  '! grep -q "confidence gate" "$CCC_STATE_DIR/nunchi-review-report.md"'

printf 'PASS=%d FAIL=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
