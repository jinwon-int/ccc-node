#!/usr/bin/env bash
# Tests for the auto-distill → nunchi feed (#2186): opt-in switch, offset
# progress, kept-only ingest, kind mapping, redaction, rotation, backfill,
# audience-mode refusal, ingest-cron wiring, and the assemble share cap.
# No provider/network calls.
# shellcheck disable=SC2034 # assertion variables are consumed through ok/eval
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

FEED="$ROOT/claude/hooks/nunchi/auto-distill-feed.py"
NP="$ROOT/claude/hooks/nunchi/nunchi.py"
export NUNCHI_HOME="$TMP/nunchi-home"
export NUNCHI_DB="$NUNCHI_HOME/facts.db"
export NUNCHI_SNAPSHOT="$NUNCHI_HOME/snapshot.md"
export CCC_STATE_DIR="$TMP/state"
mkdir -p "$NUNCHI_HOME" "$CCC_STATE_DIR" "$TMP/sessions"
python3 "$NP" init >/dev/null 2>&1
LOG="$TMP/auto-distill-dryrun.jsonl"
STATE="$NUNCHI_HOME/auto-distill-feed.state.json"
: > "$LOG"

feed() { python3 "$FEED" --log "$LOG" --state "$STATE" --state-dir "$CCC_STATE_DIR" --nunchi-py "$NP" "$@"; }
rows() { python3 -c 'import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute(sys.argv[2]).fetchone()[0])' "$NUNCHI_DB" "$1"; }
SECRET="api_key=ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"

record() {
  # record <session> <tag> — one auto-distill run record (2 kept, 1 quarantined)
  local transcript="$TMP/sessions/$1.jsonl"
  printf '{"type":"user","message":"x"}\n' > "$transcript"
  python3 - "$1" "$2" "$transcript" "$SECRET" <<'PY'
import json, sys
sid, tag, path, secret = sys.argv[1:5]
print(json.dumps({
    "session": sid, "path": path, "sec": 1.0, "usage": {},
    "kept": [
        {"title": f"{tag} 설정", "kind": "config",
         "fact": f"{tag} 노드의 러너 경로는 배포 사본이다 ({secret})",
         "evidence": ["a1"], "_evidence_text": str([f"assistant: {tag} 러너 경로 확인 {secret}"])},
        {"title": f"{tag} 절차", "kind": "runbook",
         "fact": f"{tag} 재시작 전에는 점유를 확인한다", "evidence": ["a2"],
         "_evidence_text": str(["assistant: 점유 확인"])},
    ],
    "quarantined": [{"title": "격리", "kind": "config", "fact": f"{tag} 격리된 주장 QUARANTINED"}],
    "dropped": [],
}, ensure_ascii=False))
PY
}

# ---- 1. opt-in switch -------------------------------------------------------
record s1 ALPHA >> "$LOG"
feed; rc=$?
ok "disabled by default: exit 0, no state, no rows" '[ "$rc" = 0 ] && [ ! -e "$STATE" ] && [ "$(rows "select count(*) from peer_facts")" = 0 ]'

echo on > "$CCC_STATE_DIR/nunchi.auto-distill-feed"

# ---- 2. first run starts at EOF --------------------------------------------
out="$(feed)"; rc=$?
ok "first run initialises at end of log without ingesting" \
  '[ "$rc" = 0 ] && grep -q "initialised at end of log" <<<"$out" && [ "$(rows "select count(*) from peer_facts")" = 0 ]'
ok "state file is owner-only" '[ "$(stat -c %a "$STATE")" = 600 ]'

# ---- 3. new kept items are fed, quarantined never --------------------------
record s2 BRAVO >> "$LOG"
out="$(feed)"; rc=$?
ok "new record is fed (2 kept items)" \
  '[ "$rc" = 0 ] && grep -q "fed_records=1 items=2" <<<"$out" && [ "$(rows "select count(*) from peer_facts where evidence like '"'"'auto-distill:%'"'"'")" = 2 ]'
ok "kinds map config→context, runbook→procedure" \
  '[ "$(rows "select count(*) from peer_facts where kind='"'"'context'"'"' and fact like '"'"'BRAVO 설정%'"'"'")" = 1 ] && [ "$(rows "select count(*) from peer_facts where kind='"'"'procedure'"'"'")" = 1 ]'
ok "quarantined items are never fed" '[ "$(rows "select count(*) from peer_facts where fact like '"'"'%QUARANTINED%'"'"'")" = 0 ]'
ok "pre-existing backlog (ALPHA) was not fed" '[ "$(rows "select count(*) from peer_facts where fact like '"'"'%ALPHA%'"'"'")" = 0 ]'
dump="$(python3 -c "import sqlite3;c=sqlite3.connect('$NUNCHI_DB');print(list(c.execute('select fact,source_refs from peer_facts')))")"
ok "redaction marker present, raw secret absent" \
  '! grep -q "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" <<<"$dump" && grep -q "REDACTED" <<<"$dump"'
ok "quote is kept as a source_refs quote reference" 'grep -q "\"type\": \"quote\"" <<<"$dump"'

# ---- 4. idempotent re-run --------------------------------------------------
before="$(rows "select count(*) from peer_facts")"
out="$(feed)"; rc=$?
ok "re-run with nothing new ingests nothing" '[ "$rc" = 0 ] && [ -z "$out" ] && [ "$(rows "select count(*) from peer_facts")" = "$before" ]'

# ---- 5. a record still being written is not consumed -----------------------
record s3 CHARLIE | tr -d '\n' >> "$LOG"
feed >/dev/null
ok "partial trailing line is left for the next tick" '[ "$(rows "select count(*) from peer_facts where fact like '"'"'%CHARLIE%'"'"'")" = 0 ]'
printf '\n' >> "$LOG"
feed >/dev/null
ok "completed line is fed on the next tick" '[ "$(rows "select count(*) from peer_facts where fact like '"'"'%CHARLIE%'"'"'")" = 2 ]'

# ---- 6. rotation re-reads without duplicates -------------------------------
fed_q="select count(*) from peer_facts where fact like '%BRAVO%' or fact like '%CHARLIE%'"
before="$(rows "$fed_q")"
cp "$LOG" "$LOG.new" && mv "$LOG.new" "$LOG"   # new inode, same content
feed >/dev/null; rc=$?
ok "rotated log is re-read without duplicating already-fed rows" '[ "$rc" = 0 ] && [ "$(rows "$fed_q")" = "$before" ]'
: > "$LOG.fresh" && mv "$LOG.fresh" "$LOG" && record s4 HOTEL >> "$LOG"   # real rotation: fresh log
feed >/dev/null
ok "a fresh rotated log feeds its new records from the start" '[ "$(rows "select count(*) from peer_facts where fact like '"'"'%HOTEL%'"'"'")" = 2 ]'

# ---- 7. backfill honours the transcript-age cutoff, across ticks -----------
rm -f "$STATE"; : > "$LOG"
record old1 DELTA >> "$LOG"; touch -d '30 days ago' "$TMP/sessions/old1.jsonl"
record new1 ECHO >> "$LOG"
record old2 FOXTROT >> "$LOG"; touch -d '30 days ago' "$TMP/sessions/old2.jsonl"
feed --backfill-days 7 --max-records 1 >/dev/null
feed --max-records 1 >/dev/null
feed --max-records 1 >/dev/null
ok "backfill feeds only recent transcripts, cutoff kept across ticks" \
  '[ "$(rows "select count(*) from peer_facts where fact like '"'"'%ECHO%'"'"'")" = 2 ] && [ "$(rows "select count(*) from peer_facts where fact like '"'"'%DELTA%'"'"' or fact like '"'"'%FOXTROT%'"'"'")" = 0 ]'
ok "backfill cutoff is dropped once the backlog reaches EOF" '! grep -q backfill_cutoff "$STATE"'

# ---- 8. refusals -----------------------------------------------------------
out="$(CCC_NUNCHI_AUDIENCE_SCOPED=1 feed)"; rc=$?
ok "audience-scoped mode is skipped" '[ "$rc" = 0 ] && grep -q "audience-scoped" <<<"$out"'
mkdir -p "$TMP/orphan"; cp "$FEED" "$TMP/orphan/"
python3 "$TMP/orphan/auto-distill-feed.py" --log "$LOG" --state "$STATE" --state-dir "$CCC_STATE_DIR" --nunchi-py "$NP" >/dev/null 2>"$TMP/orphan.err"; rc=$?
ok "missing redaction module refuses (exit 2)" '[ "$rc" = 2 ] && grep -q "never ingest unredacted" "$TMP/orphan.err"'
ln -s "$LOG" "$TMP/link.jsonl"
python3 "$FEED" --log "$TMP/link.jsonl" --state "$STATE" --state-dir "$CCC_STATE_DIR" --nunchi-py "$NP" >/dev/null 2>&1; rc=$?
ok "symlinked log is refused (exit 2)" '[ "$rc" = 2 ]'
rm -f "$CCC_STATE_DIR/nunchi.auto-distill-feed"
python3 "$FEED" --log /nonexistent/log.jsonl --state "$STATE" --state-dir "$CCC_STATE_DIR" --nunchi-py "$NP"; rc=$?
ok "missing log while disabled is a silent no-op" '[ "$rc" = 0 ]'
NUNCHI_AUTO_DISTILL_FEED=1 python3 "$FEED" --log /nonexistent/log.jsonl --state "$STATE" --state-dir "$CCC_STATE_DIR" --nunchi-py "$NP"; rc=$?
ok "missing log while enabled is a silent no-op" '[ "$rc" = 0 ]'

# ---- 9. ingest-cron wiring -------------------------------------------------
echo on > "$CCC_STATE_DIR/nunchi.mode"
echo on > "$CCC_STATE_DIR/nunchi.auto-distill-feed"
rm -f "$STATE"; : > "$LOG"
NUNCHI_AUTO_DISTILL_LOG="$LOG" bash "$ROOT/claude/hooks/nunchi/ingest-cron.sh" >/dev/null 2>&1
record cron1 GOLF >> "$LOG"
NUNCHI_AUTO_DISTILL_LOG="$LOG" bash "$ROOT/claude/hooks/nunchi/ingest-cron.sh" >/dev/null 2>&1; rc=$?
ok "ingest-cron runs the feed lane" '[ "$rc" = 0 ] && [ "$(rows "select count(*) from peer_facts where fact like '"'"'%GOLF%'"'"'")" = 2 ]'

# ---- 10. assemble labels and caps auto-distill rows ------------------------
python3 - "$NUNCHI_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
for i in range(60):
    c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,dedup,created_at,source_rank,review,mutability)"
              " VALUES ('family-assistant','node-a','context',?,?, '2026-10-08T00:00:00Z', ?, '2026-10-08T00:00:00Z',1,0,'live-check')",
              (f"자동 추출 사실 번호 {i} " + "가" * 40, f"auto-distill:distill:bulk{i}", f"bulk{i}"))
c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,dedup,created_at,source_rank,review,mutability)"
          " VALUES ('family-assistant','node-a','procedure','대화 기억 사실 CONVERSATION','distill:conv', '2026-10-07T00:00:00Z','conv','2026-10-07T00:00:00Z',1,0,'static')")
c.commit()
PY
asm="$(python3 "$NP" assemble --budget 3000)"
auto_bytes="$(grep '·auto' <<<"$asm" | wc -c)"
ok "auto-distill lines are labelled ·auto" 'grep -q "/context·auto) " <<<"$asm"'
ok "auto-distill lines stay within 33% of the budget" '[ "$auto_bytes" -le 990 ] && [ "$auto_bytes" -gt 0 ]'
ok "conversation memory is not crowded out" 'grep -q "CONVERSATION" <<<"$asm"'
asm0="$(NUNCHI_AUTO_DISTILL_SHARE=0 python3 "$NP" assemble --budget 3000)"
ok "share 0 injects no auto-distill line" '! grep -q "·auto" <<<"$asm0"'
asmbad="$(NUNCHI_AUTO_DISTILL_SHARE=7 python3 "$NP" assemble --budget 3000 | grep '·auto' | wc -c)"
ok "invalid share falls back to the default cap" '[ "$asmbad" -le 990 ]'

# ---- 11. nunchi ignores an unknown evidence_source -------------------------
printf '{"session_id":"x1","evidence_source":"evil","honcho":[{"kind":"procedure","subject":"node","text":"UNKNOWN-SOURCE 사실"}]}' \
  | python3 "$NP" ingest - >/dev/null
ok "unknown evidence_source is not used as a tag" \
  '[ "$(rows "select count(*) from peer_facts where fact like '"'"'%UNKNOWN-SOURCE%'"'"' and evidence like '"'"'distill:%'"'"'")" = 1 ]'

# ---- 12. review fixes (#2189 adversarial review) ---------------------------
# Fresh DB for these cases.
export NUNCHI_DB="$NUNCHI_HOME/review.db"; python3 "$NP" init >/dev/null 2>&1
echo on > "$CCC_STATE_DIR/nunchi.auto-distill-feed"
rm -f "$STATE"; : > "$LOG"
feed >/dev/null   # initialise at EOF
python3 - "$TMP/sessions/g1.jsonl" >> "$LOG" <<'PY'
import json, sys
open(sys.argv[1], "w").write('{"type":"user"}\n')
print(json.dumps({"session": "g1", "path": sys.argv[1], "kept": [
    {"title": "gpu 러너", "kind": "config", "fact": "gpu 러너 롤아웃 배포 완료 확인함",
     "_evidence_text": "['assistant: 배포 완료']"}]}, ensure_ascii=False))
PY
feed >/dev/null
printf '{"session_id":"conv1","honcho":[{"kind":"context","subject":"node","text":"gpu 러너 롤아웃 재배포 진행 중"}]}' \
  | python3 "$NP" ingest - >/dev/null
cp "$LOG" "$LOG.new" && mv "$LOG.new" "$LOG"   # rotation → the auto row is re-fed
feed >/dev/null
ok "re-fed auto row never closes a newer conversation fact (P1-a)" \
  '[ "$(rows "select count(*) from peer_facts where fact like '"'"'%재배포 진행 중%'"'"' and valid_to is null")" = 1 ]'
ok "re-fed auto row stores no duplicate" \
  '[ "$(rows "select count(*) from peer_facts where fact like '"'"'%배포 완료 확인함%'"'"'")" = 1 ]'
ok "auto rows are never G3-flagged for review" \
  '[ "$(rows "select count(*) from peer_facts where evidence like '"'"'auto-distill:%'"'"' and review=1")" = 0 ]'
ok "valid_from is the session time, not the feed time" \
  '[ "$(rows "select count(*) from peer_facts where evidence like '"'"'auto-distill:%'"'"' and valid_from like '"'"'20%Z'"'"'")" -ge 1 ]'

python3 - "$NUNCHI_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
ins = ("INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,dedup,created_at,source_rank,review,mutability)"
       " VALUES ('family-assistant','node-a',?,?,?,'2026-10-08T00:00:00Z',?,'2026-10-08T00:00:00Z',1,0,?)")
for i in range(5):
    c.execute(ins, ("procedure", f"대화 기억 CONV{i}", f"distill:c{i}", f"c{i}", "static"))
for i in range(30):
    c.execute(ins, ("context", f"자동 사실 AUTO{i}", f"auto-distill:distill:a{i}", f"a{i}", "live-check"))
for i in range(12):
    c.execute(ins, ("constraint", f"제약 규칙 {i} " + "나" * 60, f"distill:k{i}", f"k{i}", "static"))
c.commit()
PY
snap="$(python3 "$NP" snapshot 25)"
ok "snapshot labels and caps auto rows (P1-b)" \
  '[ "$(grep -c "·auto" <<<"$snap")" -le 8 ] && [ "$(grep -c "·auto" <<<"$snap")" -ge 1 ] && [ "$(grep -c "CONV" <<<"$snap")" = 5 ]'
asmc="$(python3 "$NP" assemble --budget 3000)"
ok "assemble keeps conversation memory beside a long constraint list (P2-a)" 'grep -q "CONV" <<<"$asmc"'

out="$(feed --backfill-days 7)"
ok "backfill on an initialised feed says it was ignored (P2-b)" 'grep -q "backfill-days ignored" <<<"$out"'
python3 - "$NUNCHI_HOME/.auto-distill-feed.lock" <<'PY' &
import fcntl, sys, time
fh = open(sys.argv[1], "a"); fcntl.flock(fh, fcntl.LOCK_EX); time.sleep(4)
PY
sleep 1
out="$(feed)"
wait
ok "a concurrent run skips instead of double-feeding (P2-b)" 'grep -q "another run holds the lock" <<<"$out"'

printf '{"offset":"junk","inode":1}' > "$STATE"
out="$(feed)"; rc=$?
ok "corrupt state restarts cleanly (P3)" '[ "$rc" = 0 ] && grep -q "initialised at end of log" <<<"$out"'

record poison1 INDIA >> "$LOG"
for _ in 1 2 3; do python3 "$FEED" --log "$LOG" --state "$STATE" --state-dir "$CCC_STATE_DIR" --nunchi-py /nonexistent/nunchi.py >/dev/null 2>"$TMP/poison.err"; done
ok "a record failing ingest 3 ticks in a row is skipped, not stuck (P3)" \
  'grep -q "skipping record" "$TMP/poison.err" && python3 -c "import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if d[\"offset\"]==__import__(\"os\").path.getsize(sys.argv[2]) else 1)" "$STATE" "$LOG"'

mkdir -p "$TMP/wp-cache"
CCC_MEMORY_CACHE_DIR="$TMP/wp-cache" python3 "$ROOT/claude/hooks/nunchi/wiki-promote.py" >/dev/null 2>&1
rep="$CCC_STATE_DIR/nunchi-wiki-promote-report.md"
auto_hits=0
for i in $(rows "select group_concat(id, ' ') from peer_facts where evidence like 'auto-distill:%'"); do
  grep -q "#$i |" "$rep" 2>/dev/null && auto_hits=$((auto_hits+1))
done
conv_id="$(rows "select id from peer_facts where fact = '대화 기억 CONV0'")"
ok "wiki-promote never even considers auto-distill rows (P2-c)" \
  '[ -f "$rep" ] && grep -q "#$conv_id |" "$rep" && [ "$auto_hits" = 0 ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
