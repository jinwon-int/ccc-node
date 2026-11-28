#!/usr/bin/env bash
# Meta-records are not stored as facts (ccc-node#2885): rows that only say the agent could not
# confirm / could not find / answered "no record" from the context it was given are dropped at
# ingest; a row with a real finding next to the hedge is kept. NUNCHI_SKIP_META_RECORDS=0 stores
# everything as before. LLM-free, synthetic fixture (a made-up bakery and library), private DB.
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
NP="$ROOT/claude/hooks/nunchi/nunchi.py"
export HOME="$TMP/home"; mkdir -p "$HOME/.nunchi"
export NUNCHI_HOME="$TMP/home/.nunchi" GOTGAN_EMBED=off
unset NUNCHI_SKIP_META_RECORDS

# 1. the classifier, case by case: (expected, kind, text)
out="$(python3 - "$NP" <<'PY'
import importlib.util, os, sys
os.environ["NUNCHI_DB"] = os.path.join(os.environ["HOME"], "never-opened.db")
spec = importlib.util.spec_from_file_location("nunchi", sys.argv[1]); n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)
META = [
    ("context", "빵집 오븐의 예열 온도는 제공된 근거에서 확인되지 않았다."),
    ("fact", "제공된 위키 검색 근거에서는 도서관 2층 열람실의 운영 시간을 확인할 수 없었다."),
    ("context", "빵집 배달 담당자는 제공된 기록에 기록이 없다."),
    ("context", "반죽 숙성 시간은 대화에서 확인되지 않았으므로 다음 세션에서 다시 확인해야 한다."),
    ("context", "도서관 반납함 위치는 제공된 발췌에서 확인되지 않아 '기록 없음'으로 답변했다."),
    ("context", "도서관 휴관일 표준 양식은 기록 없음으로 답변됐다."),
    ("fact", "해당 세션에 제공된 검색 근거에는 빵 할인 정책이나 승인 기록이 없었다."),
    ("observation", "제빵 수업 정원은 제공된 메모 발췌에 명시되어 있지 않아 확인되지 않았다."),
    ("context", "The bakery's oven temperature could not be verified in the provided context."),
    ("fact", "No record of the library's late-fee rule was found in the retrieved excerpts."),
    ("context", "The assistant answered that there was no record of the reading-room hours."),
    ("task-progress", "The baker's delivery route was not found in the provided search results."),
    ("context", "반죽 숙성 시간은 대화에서 확인되지 않았다. 도서관 반납함 위치도 제공된 근거에 기록이 없다."),
    ("unknown-kind", "빵집 오븐 모델은 제공된 근거에서 확인되지 않았다."),
]
KEEP = [
    # a real finding joined before the hedge
    ("context", "빵집 오븐은 매일 6시에 예열하며 주말 예열 온도는 제공된 근거에서 확인되지 않았다."),
    ("fact", "제공된 기록상 도서관 정문 비밀번호 보관 위치는 관리실 금고이며 예비 열쇠는 기록이 없다."),
    ("context", "배달 경로 변경은 계획됐으나 대화에서 완료 결과는 확인되지 않았다."),
    ("context", "반죽 레시피는 2단계로 바뀌었지만, 제공된 발췌에는 변경 이유가 포함되지 않았다."),
    ("context", "The oven preheats at six, but the weekend temperature could not be verified in the provided context."),
    ("context", "Delivery moved to Tuesdays; the reason was not found in the provided notes."),
    # a finding after the hedge
    ("context", "빵집 주말 메뉴는 제공된 근거에서 확인되지 않았으며 평일 메뉴는 크루아상 세 종류다."),
    ("context", "빵집 주말 메뉴는 제공된 근거에서 확인되지 않았으며 평일 메뉴는 크루아상이라 추가 확인이 필요하다."),
    # a hedge with no reference to the retrieval context is an open item, not a meta-record
    ("context", "도서관 무선 프린터의 실제 연결 여부는 확인되지 않았다."),
    ("context", "The reading-room heater repair was not confirmed yet."),
    # one finding sentence among meta sentences
    ("context", "반죽 숙성 시간은 대화에서 확인되지 않았다. 빵집은 월요일에 쉰다."),
    # rule-like kinds are never dropped
    ("preference", "사용자는 제공된 근거에서 확인되지 않은 내용은 '기록 없음'으로 답하길 원한다."),
    ("constraint", "제공된 근거에서 확인되지 않은 영업시간은 답하지 않는다."),
    ("decision", "제공된 근거에 기록이 없는 할인은 적용하지 않기로 했다."),
    ("procedure", "제공된 근거에서 확인되지 않으면 관리실에 전화해 확인한다."),
    ("correction", "앞서 빵집이 화요일에 쉰다고 한 것은 정정됐고, 제공된 근거에서 휴무일은 확인되지 않았다."),
    # ordinary facts that mention the words
    ("context", "도서관 대출 기록은 제공된 키오스크에서 확인할 수 있다."),
    ("fact", "빵집 영수증에는 고객 이름 기록이 없다."),
    ("context", ""),
]
bad = [("meta", k, t) for k, t in META if not n._is_meta_record(k, t)]
bad += [("keep", k, t) for k, t in KEEP if n._is_meta_record(k, t)]
for b in bad:
    print("MISCLASSIFIED", *b)
print(f"cases {len(META)} {len(KEEP)} {len(bad)}")
PY
)"
read -r _ nmeta nkeep nbad <<<"$(grep '^cases' <<<"$out")"
grep MISCLASSIFIED <<<"$out"
ok "all $nmeta meta-records are classified as meta" '[ "$nmeta" -ge 14 ] && ! grep -q "^MISCLASSIFIED meta" <<<"$out"'
ok "all $nkeep findings-with-hedge and rule rows are kept" '[ "$nkeep" -ge 19 ] && ! grep -q "^MISCLASSIFIED keep" <<<"$out"'

# 2. end to end through `nunchi.py ingest`: meta rows are not written, the rest are, counter set
cat > "$TMP/payload.json" <<'JSON'
{"session_id": "bakery-1", "honcho": [
  {"kind": "context", "subject": "user", "text": "빵집 오븐의 예열 온도는 제공된 근거에서 확인되지 않았다."},
  {"kind": "fact", "subject": "user", "text": "The bakery's oven temperature could not be verified in the provided context."},
  {"kind": "context", "subject": "user", "text": "빵집 오븐은 매일 6시에 예열하며 주말 예열 온도는 제공된 근거에서 확인되지 않았다."},
  {"kind": "context", "subject": "user", "text": "도서관 무선 프린터의 실제 연결 여부는 확인되지 않았다."},
  {"kind": "preference", "subject": "user", "text": "사용자는 제공된 근거에서 확인되지 않은 내용은 '기록 없음'으로 답하길 원한다."}
]}
JSON
count() { python3 -c 'import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute("select count(*) from peer_facts").fetchone()[0])' "$1"; }
has() { python3 -c 'import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute("select count(*) from peer_facts where fact=?",(sys.argv[2],)).fetchone()[0])' "$1" "$2"; }
export NUNCHI_DB="$TMP/on.db"
python3 "$NP" init >/dev/null 2>&1
out="$(python3 "$NP" ingest "$TMP/payload.json" 2>&1)"
ok "ingest reports the dropped meta-records" 'grep -q "ingested 3/5" <<<"$out" && grep -q "skipped_meta_records=2)" <<<"$out"'
ok "three rows stored" '[ "$(count "$NUNCHI_DB")" = 3 ]'
ok "the hedged finding is stored" '[ "$(has "$NUNCHI_DB" "빵집 오븐은 매일 6시에 예열하며 주말 예열 온도는 제공된 근거에서 확인되지 않았다.")" = 1 ]'
ok "the Korean meta-record is not stored" '[ "$(has "$NUNCHI_DB" "빵집 오븐의 예열 온도는 제공된 근거에서 확인되지 않았다.")" = 0 ]'
out="$(python3 "$NP" ingest "$TMP/payload.json" 2>&1)"
ok "re-feeding is still a no-op for the kept rows" 'grep -q "ingested 0/5" <<<"$out" && [ "$(count "$NUNCHI_DB")" = 3 ]'

# 3. the off switch stores everything, as before
for v in 0 false off no; do
  export NUNCHI_DB="$TMP/off-$v.db"
  python3 "$NP" init >/dev/null 2>&1
  out="$(NUNCHI_SKIP_META_RECORDS=$v python3 "$NP" ingest "$TMP/payload.json" 2>&1)"
  ok "NUNCHI_SKIP_META_RECORDS=$v stores all five" 'grep -q "ingested 5/5" <<<"$out" && grep -q "skipped_meta_records=0)" <<<"$out" && [ "$(count "$NUNCHI_DB")" = 5 ]'
done

# 4. a payload without meta-records stores the same rows with the switch on and off
cat > "$TMP/plain.json" <<'JSON'
{"session_id": "library-1", "honcho": [
  {"kind": "context", "subject": "user", "text": "도서관은 월요일에 쉰다."},
  {"kind": "decision", "subject": "user", "text": "반납함은 정문 옆에 두기로 했다.", "because": "야간 반납이 많아서"},
  {"kind": "task-progress", "subject": "user", "text": "Library catalogue migration is half done."}
]}
JSON
dump() { python3 -c 'import sqlite3,sys;print(sorted(sqlite3.connect(sys.argv[1]).execute("select observed,kind,fact,because,review from peer_facts").fetchall()))' "$1"; }
export NUNCHI_DB="$TMP/plain-on.db"; python3 "$NP" init >/dev/null 2>&1; python3 "$NP" ingest "$TMP/plain.json" >/dev/null 2>&1
a="$(dump "$NUNCHI_DB")"
export NUNCHI_DB="$TMP/plain-off.db"; python3 "$NP" init >/dev/null 2>&1; NUNCHI_SKIP_META_RECORDS=0 python3 "$NP" ingest "$TMP/plain.json" >/dev/null 2>&1
b="$(dump "$NUNCHI_DB")"
ok "rows without meta-records are stored identically either way" '[ -n "$a" ] && [ "$a" = "$b" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
