#!/usr/bin/env bash
# Point-in-time recall (ccc-node#2464 제안 3) + dialectic citations and `why` (#2466) —
# LLM-free and deterministic against a private temp DB; the synthesis backend is a stub.
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
python3 "$NP" init >/dev/null 2>&1
# #1 Seattle (user-stated, closed 10-15, pre-#1264 row: no refs) → superseded by
# #2 Austin (measured, refs + because) · #3 an open G3 review sibling of #2 · #4 unrelated.
python3 - "$NUNCHI_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
rows = [
    (1, "home address is Seattle downtown", "2026-09-01T00:00:00+00:00", "2026-10-15T00:00:00+00:00", None, 3, 0, None, None),
    (2, "home address is Austin north side", "2026-10-15T00:00:00+00:00", None, 1, 2, 0, "moved for work",
     '[{"type": "session", "ref": "session:abc123", "sha256_8": "deadbeef"}]'),
    (3, "home address is Austin south side", "2026-10-16T00:00:00+00:00", None, None, 1, 1, None, None),
    (4, "Coffee grinder broke.", "2026-08-01T00:00:00+00:00", None, None, 1, 0, None, None),
]
for fid, fact, vf, vt, sup, rank, review, because, refs in rows:
    c.execute("INSERT INTO peer_facts(id,observer,observed,kind,fact,valid_from,valid_to,supersedes,dedup,"
              "created_at,source_rank,review,because,source_refs)"
              " VALUES(?,'family-assistant','seo-jin-on','fact',?,?,?,?,?,?,?,?,?,?)",
              (fid, fact, vf, vt, sup, f"d{fid}", vf, rank, review, because, refs))
c.commit()
PY
q="home address"
# 1. --as-of: point-in-time view
out="$(python3 "$NP" recall "$q" --target seo-jin-on --as-of 2026-10-01 2>&1)"
ok "as-of before the move returns the Seattle fact" 'grep -q "Seattle" <<<"$out"'
ok "as-of before the move hides the later Austin facts" '! grep -q "Austin" <<<"$out"'
ok "as-of hides facts recorded after it (unrelated newer fact absent)" '! grep -q "#3 " <<<"$out"'
out="$(python3 "$NP" recall "$q" --target seo-jin-on --as-of 2026-10-20T12:00:00+09:00 2>&1)"
ok "ISO as-of after the move returns Austin" 'grep -q "Austin north" <<<"$out"'
ok "ISO as-of after the move hides the closed Seattle fact" '! grep -q "Seattle" <<<"$out"'
out="$(python3 "$NP" recall "$q" --target seo-jin-on 2>&1)"
ok "without as-of recall keeps history (Seattle) and current (Austin)" 'grep -q "Seattle" <<<"$out" && grep -q "Austin" <<<"$out"'
out="$(python3 "$NP" recall "$q" --as-of yesterday 2>&1)"; rc=$?
ok "invalid --as-of exits non-zero with a message" '[ "$rc" != 0 ] && grep -q "invalid --as-of" <<<"$out"'
# 2. dialectic: [#id|rank] labels in the prompt, deterministic citation footer
mkdir -p "$TMP/bin"
cat > "$TMP/bin/claude" <<'SH'
#!/usr/bin/env bash
# echo the prompt back, then answer citing one retrieved and one unknown fact id
printf '%s\n' "$2"
printf '현재 주소는 Austin이다 [#2]. 근거 외 [#99].\n'
SH
chmod 755 "$TMP/bin/claude"
run_dialectic() { PATH="$TMP/bin:/usr/bin:/bin" CCC_NUNCHI_MEMPALACE_HOME="$TMP/nopalace" python3 "$NP" dialectic "$@" 2>&1; }
out="$(run_dialectic "$q" --target seo-jin-on)"
ok "prompt labels facts with [#id|rank]" 'grep -q "^- \[#2|measured\] (2026-10-15) home address is Austin north" <<<"$out"'
ok "prompt labels a user-stated history fact" 'grep -q "^- \[#1|user-stated\] (2026-09-01~2026-10-15) home address is Seattle" <<<"$out"'
ok "prompt instructs [#id] citation" 'grep -q "문장 끝에 \`\[#id\]\`로 인용" <<<"$out"'
ok "footer lists the cited fact with kind·rank·span·supersedes" \
  'grep -qx "근거: #2 (fact·measured·2026-10-15~·supersedes #1)" <<<"$out"'
ok "footer flags a cited id that was never retrieved" 'grep -qx "미검증 인용: #99 (검색 집합에 없음)" <<<"$out"'
out2="$(run_dialectic "$q" --target seo-jin-on)"
ok "footer is deterministic (same DB + query → same footer)" \
  '[ "$(sed -n "/^---$/,\$p" <<<"$out")" = "$(sed -n "/^---$/,\$p" <<<"$out2")" ]'
out="$(run_dialectic "$q" --target seo-jin-on --as-of 2026-10-01)"
current="$(sed -n '/^\[peer 사실 — 현재 유효\]/,/^\[peer 사실 — 과거 이력\]/p' <<<"$out")"
ok "dialectic --as-of states the reference time" 'grep -q "^기준 시점: 2026-10-01T23:59:59+00:00" <<<"$out"'
ok "dialectic --as-of treats a fact closed later as current at that time" 'grep -q "Seattle" <<<"$current"'
cat > "$TMP/bin/claude" <<'SH'
#!/usr/bin/env bash
printf '기록 없음\n'
SH
out="$(run_dialectic "$q" --target seo-jin-on)"
ok "an answer without citations says so in the footer" 'grep -q "^인용 없음" <<<"$out"'
# The footer must never read as a refusal to bench.sh, or every uncited answer grades as "기록 없음".
no_record_re="$(sed -n 's/^NO_RECORD_RE="\${NUNCHI_BENCH_NO_RECORD_RE:-\(.*\)}"$/\1/p' "$ROOT/claude/hooks/nunchi/bench.sh")"
footer="$(sed -n '/^---$/,$p' <<<"$out")"
ok "bench NO_RECORD_RE is readable from bench.sh" '[ -n "$no_record_re" ]'
ok "the uncited footer does not match bench NO_RECORD_RE" '! grep -qE "$no_record_re" <<<"$footer"'
# A blank answer (llm_synthesize never returns one today — it falls back) must not grow a
# footer: bench.sh grades a blank answer INVALID(empty) and a footer alone would make it usable.
cat > "$TMP/blank.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("nunchi_under_test", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.llm_synthesize = lambda prompt: "  \n"
m.dialectic(sys.argv[2], "seo-jin-on")
PY
out="$(CCC_NUNCHI_MEMPALACE_HOME="$TMP/nopalace" python3 "$TMP/blank.py" "$NP" "$q" 2>&1)"
ok "a blank answer gets no footer (bench must still grade it empty)" '[ -z "${out//[[:space:]]/}" ]'
# 3. why <id>: refs, because, supersedes both ways, open G3 siblings
out="$(python3 "$NP" why 2 2>&1)"
ok "why shows rank label and current validity" 'grep -q "^#2 \[fact·measured\] 2026-10-15~ (현재 유효)" <<<"$out"'
ok "why shows because" 'grep -q "because: moved for work" <<<"$out"'
ok "why shows source_refs" 'grep -q "session: session:abc123 (sha256_8=deadbeef)" <<<"$out"'
ok "why shows the fact it replaced" 'grep -q "⟵ #1 \[fact\]" <<<"$out"'
ok "why shows the open G3 review sibling" 'grep -q "⚠ #3 " <<<"$out"'
out="$(python3 "$NP" why "#1" 2>&1)"; rc=$?
ok "why on a pre-#1264 row succeeds and says refs 없음" '[ "$rc" = 0 ] && grep -q "refs 없음" <<<"$out"'
ok "why shows what replaced it" 'grep -q "⟶ #2 \[fact\]" <<<"$out"'
out="$(python3 "$NP" why 2 --json 2>&1)"
ok "why --json is machine-readable with the chain" \
  'python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert d[\"replaced\"][0][\"id\"]==1 and [s[\"id\"] for s in d[\"review_siblings\"]]==[3] and d[\"source_refs\"][0][\"ref\"]==\"session:abc123\"" "$out"'
out="$(python3 "$NP" why 999 2>&1)"; rc=$?
ok "why on a missing id fails cleanly" '[ "$rc" != 0 ] && grep -q "no fact #999" <<<"$out"'
out="$(python3 "$NP" why nonsense 2>&1)"; rc=$?
ok "why with a non-numeric id prints usage, no traceback" '[ "$rc" != 0 ] && grep -q "usage: nunchi.py why" <<<"$out" && ! grep -q Traceback <<<"$out"'
# 4. why --query: footer-format view of what dialectic would retrieve, no LLM
out="$(PATH="/usr/bin:/bin" python3 "$NP" why --query "$q" --target seo-jin-on 2>&1)"
ok "why --query renders retrieved facts as citation labels" 'grep -q "^- #2 (fact·measured·2026-10-15~·supersedes #1) home address is Austin north" <<<"$out"'
out="$(python3 "$NP" why --query "$q" --target seo-jin-on --as-of 2026-10-01 2>&1)"
ok "why --query honours --as-of" 'grep -q "Seattle" <<<"$out" && ! grep -q "Austin" <<<"$out"'
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
