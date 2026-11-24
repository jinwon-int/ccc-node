#!/usr/bin/env bash
# Query-side particle stripping + prefix matching for fact recall (ccc-node#2885).
#
# `facts_fts` tokenises on spaces and punctuation only, so a Korean word in a
# question ("도서관") and its inflected form in a fact ("도서관은") are
# different tokens. The query now adds each Hangul stem as a prefix query.
# This pins the measurement that motivated it on a synthetic fixture (a made-up
# library and bakery — no real fact text): questions whose words are inflected
# differently from the fact are missed by the old query and found by the new
# one, and nothing the old query found is lost.
#
# LLM-free and deterministic, against a private temp DB.
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
export NUNCHI_DB="$TMP/facts.db"
export GOTGAN_EMBED=off
# No hallway file unless a case below names one: the caller's MemPalace must
# not reach the fixture (the resolver also honours the palace-path variables).
unset NUNCHI_QUERY_PREFIX MEMPALACE_PALACE_PATH MEMPAL_PALACE_PATH CCC_NUNCHI_MEMPALACE_HOME
export NUNCHI_HALLWAYS_FILE="$TMP/no-hallways.json"
python3 "$NP" init >/dev/null 2>&1

# One Python process builds the fixture, runs every query both ways and writes
# `name value` lines; the assertions below read them.
python3 - "$ROOT/claude/hooks/nunchi" "$NUNCHI_DB" "$TMP" > "$TMP/metrics" <<'PY'
import json, os, sqlite3, sys, time
sys.path.insert(0, sys.argv[1])
import nunchi

# (fact, keyword question, natural question). In every row the question's words
# are inflected differently from the fact's, as they are in real use.
ROWS = [
    ("도서관은 매주 월요일에 휴관한다", "도서관 휴관", "도서관이 휴관하는 날은 언제인가"),
    ("반납함은 정문 왼쪽에 설치했다", "반납함 정문", "반납함을 정문의 어느 쪽에 두었나"),
    ("대출 기간은 이주일이고 연장은 한 번만 된다", "기간 연장", "대출한 책의 기간을 연장할 수 있나"),
    ("어린이 열람실에서는 음료를 마실 수 없다", "열람실 음료", "열람실에 음료가 허용되나"),
    ("제과점의 식빵은 아침 일곱 시에 나온다", "제과점 식빵", "식빵이 제과점에 나오는 시각"),
    ("오븐을 예열하는 데 이십 분이 걸린다", "오븐 예열", "오븐의 예열에 걸리는 시간"),
    ("밀가루는 서늘한 창고에 보관한다", "밀가루 창고", "밀가루를 어느 창고로 옮기나"),
    ("단팥빵에는 호두를 넣지 않는다", "단팥빵 호두", "단팥빵에 호두가 들어가나"),
    ("배달 주문은 오후 세 시까지 받는다", "주문 마감 오후", "배달 주문을 오후에도 받나"),
    ("포장 상자를 재생지로 바꿨다", "상자 재생지", "포장 상자의 재질은 재생지인가"),
    ("회원증을 잃어버리면 재발급 수수료가 천 원이다", "회원증 수수료", "회원증의 재발급 수수료는 얼마인가"),
    ("동화책 코너를 이층으로 옮겼다", "동화책 코너", "동화책의 코너는 어디로 갔나"),
    ("자원봉사자는 토요일마다 서가를 정리한다", "자원봉사자 서가", "서가의 정리는 누가 하나"),
    ("복사기의 토너를 지난주에 교체했다", "복사기 토너", "복사기 토너는 언제 갈았나"),
    ("케이크 예약은 사흘 전까지 해야 한다", "케이크 예약", "케이크를 예약하려면 며칠 전인가"),
    ("크루아상 반죽은 하루 동안 숙성한다", "크루아상 반죽", "크루아상의 반죽을 얼마나 두나"),
    ("주차장이 좁아서 자전거 거치대를 늘렸다", "주차장 거치대", "자전거의 거치대는 늘었나"),
    ("신간 도서는 금요일에 입고된다", "신간 도서 금요일", "신간이 들어오는 요일은 금요일인가"),
    ("커피 원두는 산미가 약한 것으로 정했다", "원두 산미", "커피의 원두는 산미가 어떤가"),
    ("냉장고의 온도를 사 도로 맞춘다", "냉장고 온도", "냉장고는 온도가 몇 도인가"),
]
# Rows the old query already finds (same word form, or ASCII) and one near-miss
# that must stay out of an unrelated question.
EXACT = [
    ("shelf-map v2 파일은 backup 폴더에 있다", "shelf-map backup"),
    ("wifi 안내문 게시판 옆", "wifi 안내문"),
    ("설문 결과를 다음 달에 공개한다", "설문"),
]
NEAR_MISS = "결석한 봉사자는 없었다"
# Mixed words and their decoys (see the cases further down).
MIXED = "PR 본문에 요약을 적는다"
PARTICLE_DECOY = "`도구`에서 파일을 읽는다"      # a bare ending is its own index token here
SHORT_DECOY = "도서 목록을 새로 뽑았다"          # shares only the first two syllables of 도서관

c = sqlite3.connect(sys.argv[2])
def add(fact, n):
    cur = c.execute(
        "INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,dedup,created_at,source_rank,review,mutability)"
        " VALUES('family-assistant','user','fact',?,?,?,?,?,1,0,'static')",
        (fact, f"d:q{n}", f"2026-10-{1 + n % 27:02d}T00:00:00+00:00", f"q{n}", f"2026-10-{1 + n % 27:02d}T00:00:00+00:00"))
    return cur.lastrowid
ids = [add(fact, n) for n, (fact, _k, _q) in enumerate(ROWS)]
exact_ids = [add(fact, 100 + n) for n, (fact, _q) in enumerate(EXACT)]
near_id = add(NEAR_MISS, 200)
mixed_id = add(MIXED, 201)
particle_decoy_id = add(PARTICLE_DECOY, 202)
short_decoy_id = add(SHORT_DECOY, 203)
c.commit()
changes_before = c.total_changes

def run(query, prefix, **kw):
    os.environ["NUNCHI_QUERY_PREFIX"] = prefix
    return [r[0] for r in nunchi.search(c, query, **kw)]

def found(style, prefix, top, **kw):
    return sum(1 for fid, row in zip(ids, ROWS) if fid in run(row[style], prefix, limit=top, **kw)[:top])

RECALL = {"include_history": True, "state_aware": True}
print("rows", len(ROWS))
print("kw_old_top5", found(1, "0", 5))
print("kw_new_top5", found(1, "1", 5))
print("kw_new_top1", found(1, "1", 1))
print("nat_old_top5", found(2, "0", 5))
print("nat_new_top5", found(2, "1", 5))
print("kw_old_recall12", found(1, "0", 12, **RECALL))
print("kw_new_recall12", found(1, "1", 12, **RECALL))
print("nat_new_recall12", found(2, "1", 12, **RECALL))

# Nothing the old query matched is lost: its whole result set is inside the new one.
lost = 0
queries = [row[1] for row in ROWS] + [row[2] for row in ROWS] + [q for _f, q in EXACT]
for q in queries:
    if not set(run(q, "0", limit=500)) <= set(run(q, "1", limit=500)):
        lost += 1
print("superset_violations", lost)
print("exact_old", sum(1 for fid, (_f, q) in zip(exact_ids, EXACT) if fid in run(q, "0", limit=5)))
print("exact_new", sum(1 for fid, (_f, q) in zip(exact_ids, EXACT) if fid in run(q, "1", limit=5)))

# A two-syllable word that ends in a particle-like syllable keeps both
# syllables: "결과" finds "결과를", and does not turn into the prefix "결".
hits = run("결과", "1", limit=50)
print("short_stem_found", int(exact_ids[2] in hits))
print("short_stem_overreach", int(near_id in hits))

os.environ["NUNCHI_QUERY_PREFIX"] = "1"
stems = nunchi._query_stems
cases = [
    ("도서관이 휴관하는 날", ["도서관", "휴관하"]),                # one-syllable words are dropped
    ("결과 경로 평가", ["결과", "경로", "평가"]),                  # stem would be one syllable: word kept
    ("앞으로", ["앞으로"]),                                        # same for a longer word
    ("오븐을 오븐의 오븐", ["오븐"]),                              # de-duplicated
    ("언제 식빵은 어떻게", ["식빵"]),                              # question words are stopwords
    ("무엇을 어디에 식빵은", ["식빵"]),                            # also when inflected
    ("에서 으로 는 식빵", ["식빵"]),                               # a bare particle is not a word
    ("backup shelf-map 123", []),                                  # non-Hangul is left to the phrase query
    ("", []),
]
print("stem_cases_bad", sum(1 for q, want in cases if stems(q) != want))
many = " ".join(chr(0xAC00 + 28 * i) + chr(0xB098) + chr(0xB2E4) for i in range(30))
print("stem_cap", len(stems(many)))
# The cap counts stems, not tokens: skipped words in front do not use it up.
print("stem_cap_after_skips", len(stems("언제 어디 무엇 왜 누가 어느 그리고 또는 있다 없다 한다 했다 " + many)))
# ...and it bounds the work: a very long question stops at the twelfth stem.
huge = " ".join(chr(0xAC00 + i % 11172) + chr(0xAC00 + i // 11172) + chr(0xB098) for i in range(20000))
t0 = time.time()
stems(huge)
print("stem_huge_fast", int(time.time() - t0 < 1.0))

# A Hangul ending glued to a non-Hangul word is one index token ("pr에서"):
# the ending is not a word, and the head is sent as a phrase.
terms = nunchi._query_terms
print("mixed_terms", int(terms("PR에서 main으로 CI까지 v2처럼") == ([], ["PR", "main", "CI", "v2"])))
print("mixed_parts", int(nunchi._fts_query_parts("PR에서 리뷰를") == ['"PR에서"', '"리뷰를"', '"PR"', '"리뷰"*']))
hits = run("PR에서 막힌 것", "1", limit=50)
print("mixed_found", int(mixed_id in hits))
print("mixed_particle_decoy", int(particle_decoy_id in hits))
print("mixed_old_found", int(mixed_id in run("PR에서 막힌 것", "0", limit=50)))
# A stem of three or more syllables is used whole, not cut to two.
print("long_stem_decoy", int(short_decoy_id in run("도서관은 휴관", "1", limit=50)))

parts = nunchi._fts_query_parts
legacy = lambda q: [nunchi._fts_phrase(w) for w in nunchi._expand_query(q) + nunchi._hallway_expansions(q)]
queries += ["PR에서 막힌 것", "도서관은 휴관", "결과"]
os.environ["NUNCHI_QUERY_PREFIX"] = "0"
print("off_is_legacy", int(all(parts(q) == legacy(q) for q in queries)))
os.environ["NUNCHI_QUERY_PREFIX"] = "1"
print("on_keeps_legacy_first", int(all(parts(q)[:len(legacy(q))] == legacy(q) for q in queries)))
print("on_adds_prefix", int(parts("오븐을 예열") == ['"오븐을"', '"예열"', '"오븐"*', '"예열"*']))

# A quote in the query must not break the MATCH expression (it would drop the
# whole recall to the unranked LIKE fallback).
quoted = '오븐"의 예열'
try:
    c.execute("SELECT count(*) FROM facts_fts WHERE facts_fts MATCH ?", (" OR ".join(parts(quoted)),)).fetchone()
    print("quote_safe", 1)
except sqlite3.OperationalError:
    print("quote_safe", 0)
print("quote_found", int(ids[5] in run(quoted, "1", limit=5)))

# The switch value: case and surrounding space do not matter; anything that is
# not an "off" word leaves the feature on.
def on_with(value):
    os.environ["NUNCHI_QUERY_PREFIX"] = value
    return '"오븐"*' in parts("오븐을 예열")
print("switch_values", int([on_with(v) for v in ("OFF", " 0 ", "False", "No", "1", "on", "maybe", "")] ==
                           [False, False, False, False, True, True, True, True]))
os.environ["NUNCHI_QUERY_PREFIX"] = "1"

# Searching writes nothing (the whole suite above ran on this connection).
print("no_writes", int(c.total_changes == changes_before))
# A query with no usable word still runs (it falls back to the whole query as a phrase).
print("empty_operands_ok", int(isinstance(run("가", "1", limit=5), list) and isinstance(run("", "1", limit=5), list)))

# Aliases and hallway expansions. Synthetic alias table and hallway file: an
# alias written without a particle resolves in the old query too; with a
# particle only the stem resolves, and then it gets the same slug and the
# same hallway expansion.
saved = dict(nunchi.NODE_ALIASES)
nunchi.NODE_ALIASES.clear()
nunchi.NODE_ALIASES.update({"별칭이름": "slug-a", "aliasx": "slug-a"})
hall = os.path.join(sys.argv[3], "hallways.json")
with open(hall, "w", encoding="utf-8") as fh:
    json.dump([{"entity_a": "slug-a", "entity_b": "partner-term", "co_occurrence_count": 5}], fh)
os.environ["NUNCHI_HALLWAYS_FILE"] = hall
bare, inflected, glued = "별칭이름 어디", "별칭이름은 별칭이름이 어디", "aliasx는 어디"
os.environ["NUNCHI_QUERY_PREFIX"] = "0"
print("alias_off_is_legacy", int(all(parts(q) == legacy(q) for q in (bare, inflected, glued))))
print("alias_off_bare_has_both", int('"slug-a"' in parts(bare) and '"partner-term"' in parts(bare)))
print("alias_slug_off", int('"slug-a"' in parts(inflected)))
os.environ["NUNCHI_QUERY_PREFIX"] = "1"
print("alias_on_keeps_legacy", int(parts(bare)[:len(legacy(bare))] == legacy(bare)
                                   and '"slug-a"' in parts(bare) and '"partner-term"' in parts(bare)))
print("alias_slug", int('"slug-a"' in parts(inflected)))
print("alias_slug_once", int(parts(inflected).count('"slug-a"') == 1 and parts(bare).count('"slug-a"') == 1))
print("alias_hallway", int('"partner-term"' in parts(inflected)))
print("alias_glued", int('"slug-a"' in parts(glued) and '"partner-term"' in parts(glued)))
os.environ["NUNCHI_HALLWAYS_FILE"] = os.path.join(sys.argv[3], "no-hallways.json")
nunchi.NODE_ALIASES.clear()
nunchi.NODE_ALIASES.update(saved)

# A `gotgan` module without the expected names is the same as none.
import types
sys.modules["gotgan"] = types.ModuleType("gotgan")
print("odd_gotgan_is_legacy", int(all(parts(q) == legacy(q) for q in queries)))

# Without the sibling module the query is what it was before.
sys.modules["gotgan"] = None
print("no_gotgan_is_legacy", int(all(parts(q) == legacy(q) for q in queries)))
print("no_gotgan_search_ok", int(exact_ids[0] in run("shelf-map backup", "1", limit=5)))
PY
rc=$?
val() { awk -v k="$1" '$1 == k { print $2 }' "$TMP/metrics"; }
ok "fixture and queries ran" '[ "$rc" = 0 ] && [ "$(val rows)" = 20 ]'

# The measurement, pinned: inflected keyword questions.
ok "old query misses most inflected keyword questions (fixture check: it finds only those sharing a word form)" \
  '[ "$(val kw_old_top5)" -le 8 ] && [ "$(val nat_old_top5)" -le 8 ]'
ok "new query finds every inflected keyword question in the top 5" '[ "$(val kw_new_top5)" = 20 ]'
ok "new query ranks the target first for nearly all of them" '[ "$(val kw_new_top1)" -ge 18 ]'
ok "natural-language questions: old query misses some, new query finds all" \
  '[ "$(val nat_old_top5)" -lt 20 ] && [ "$(val nat_new_top5)" = 20 ]'
ok "the recall path (history + recency rerank, 12 rows) finds all with the new query" \
  '[ "$(val kw_new_recall12)" = 20 ] && [ "$(val nat_new_recall12)" = 20 ] && [ "$(val kw_old_recall12)" -lt 20 ]'

# Nothing is lost.
ok "every row the old query matched is still matched (43 questions, whole result sets)" '[ "$(val superset_violations)" = 0 ]'
ok "same-form and ASCII questions are found before and after" '[ "$(val exact_old)" = 3 ] && [ "$(val exact_new)" = 3 ]'

# The stem rules.
ok "a two-syllable word ending in a particle-like syllable is kept whole and still matches its inflections" \
  '[ "$(val short_stem_found)" = 1 ]'
ok "and is not shortened to a one-syllable prefix that reaches unrelated words" '[ "$(val short_stem_overreach)" = 0 ]'
ok "stems: particles off, one-syllable words and question words dropped, duplicates merged, non-Hangul untouched" \
  '[ "$(val stem_cases_bad)" = 0 ]'
ok "stems are capped at 12 per query, counted after the skip rules, and the cap stops the scan" \
  '[ "$(val stem_cap)" = 12 ] && [ "$(val stem_cap_after_skips)" = 12 ] && [ "$(val stem_huge_fast)" = 1 ]'

# Mixed words.
ok "a Hangul ending glued to a non-Hangul word yields no stem; the head becomes a phrase" \
  '[ "$(val mixed_terms)" = 1 ] && [ "$(val mixed_parts)" = 1 ]'
ok "so \"PR에서\" finds the fact about PRs (the old query does not)" \
  '[ "$(val mixed_found)" = 1 ] && [ "$(val mixed_old_found)" = 0 ]'
ok "and its ending does not pull in a fact that merely contains that ending as a token" '[ "$(val mixed_particle_decoy)" = 0 ]'
ok "a three-syllable stem is used whole: it does not reach a word sharing its first two syllables" \
  '[ "$(val long_stem_decoy)" = 0 ]'

# The query shape and the switch.
ok "NUNCHI_QUERY_PREFIX=0 sends exactly the old query" '[ "$(val off_is_legacy)" = 1 ]'
ok "on: the old operands come first, unchanged, and the prefixes are appended" \
  '[ "$(val on_keeps_legacy_first)" = 1 ] && [ "$(val on_adds_prefix)" = 1 ]'
ok "a quote in the question keeps the MATCH expression valid and the search ranked" \
  '[ "$(val quote_safe)" = 1 ] && [ "$(val quote_found)" = 1 ]'
ok "the switch ignores case and spaces, and only an off word turns it off" '[ "$(val switch_values)" = 1 ]'
ok "searching writes nothing to the store" '[ "$(val no_writes)" = 1 ]'
ok "a query with no usable word still runs" '[ "$(val empty_operands_ok)" = 1 ]'
ok "off: alias slugs and hallway expansions are exactly the old ones" \
  '[ "$(val alias_off_is_legacy)" = 1 ] && [ "$(val alias_off_bare_has_both)" = 1 ] && [ "$(val alias_slug_off)" = 0 ]'
ok "on: the old alias slug and hallway expansion are kept" '[ "$(val alias_on_keeps_legacy)" = 1 ]'
ok "on: an alias with a particle resolves to its slug, once, with its hallway expansion" \
  '[ "$(val alias_slug)" = 1 ] && [ "$(val alias_slug_once)" = 1 ] && [ "$(val alias_hallway)" = 1 ]'
ok "on: an alias glued to a Hangul ending resolves through its head" '[ "$(val alias_glued)" = 1 ]'
ok "without gotgan.py, or with a module lacking its rules, the query is the old one and search still works" \
  '[ "$(val no_gotgan_is_legacy)" = 1 ] && [ "$(val no_gotgan_search_ok)" = 1 ] && [ "$(val odd_gotgan_is_legacy)" = 1 ]'

# End to end through the CLI, and the switch from the environment.
out_on="$(python3 "$NP" recall "오븐 예열" 2>/dev/null)"
out_off="$(NUNCHI_QUERY_PREFIX=0 python3 "$NP" recall "오븐 예열" 2>/dev/null)"
ok "recall CLI finds the inflected fact by default" 'grep -q "오븐을 예열하는 데" <<<"$out_on"'
ok "recall CLI with NUNCHI_QUERY_PREFIX=0 behaves as before (the fact is not found)" '! grep -q "오븐을 예열하는 데" <<<"$out_off"'
off_bad=0
for off in false off no; do
  NUNCHI_QUERY_PREFIX="$off" python3 "$NP" recall "오븐 예열" 2>/dev/null | grep -q "오븐을 예열하는 데" && off_bad=$((off_bad+1))
done
ok "the switch accepts false/off/no as well as 0" '[ "$off_bad" = 0 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
