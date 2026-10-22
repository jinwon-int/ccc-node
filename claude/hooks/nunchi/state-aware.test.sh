#!/usr/bin/env bash
# State-aware recall (ccc-node#2464 제안 1): recency re-rank, recent-state window, dialectic
# dates — LLM-free, deterministic, against a private temp DB. Vectors are optional and absent here.
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
# Three user facts: an OLD lexical match, a NEWER lexical match, and the NEWEST fact that shares
# no word with the query (the "current state" a stale-phrased query cannot reach lexically).
python3 - "$NUNCHI_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
rows = [
    ("I have been based in Seattle for the last few years.", "2026-10-01T00:00:00+00:00"),
    ("Seattle rain is fine, I still bike to the office.", "2026-10-10T00:00:00+00:00"),
    ("Austin address updated, utilities being set up.", "2026-10-20T00:00:00+00:00"),
    ("Coffee grinder broke.", "2026-09-01T00:00:00+00:00"),
]
for i, (fact, ts) in enumerate(rows):
    c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,valid_from,dedup,created_at,source_rank,review)"
              " VALUES('family-assistant','seo-jin-on','fact',?,?,?,?,1,0)", (fact, ts, f"d{i}", ts))
c.commit()
PY
q="does user still live Seattle"
# 1. production recall shows the newest (lexically unreachable) fact via the state window
out="$(python3 "$NP" recall "$q" --target seo-jin-on --limit 3 2>&1)"
ok "recent-state window surfaces the newest non-matching fact" 'grep -q "Austin" <<<"$out"'
ok "lexical hits still lead the list" '[ "$(sed -n 1p <<<"$out" | grep -c Seattle)" = 1 ]'
ok "the unrelated older fact is not pulled in" '! grep -q "coffee grinder" <<<"$out"'
# 2. window can be switched off
out="$(NUNCHI_STATE_WINDOW=0 python3 "$NP" recall "$q" --target seo-jin-on --limit 3 2>&1)"
ok "NUNCHI_STATE_WINDOW=0 restores lexical-only recall" '! grep -q "Austin" <<<"$out"'
# 3. recency re-rank: with equal lexical strength the newer Seattle fact ranks first; weight 0 keeps bm25 order
out="$(NUNCHI_STATE_WINDOW=0 python3 "$NP" recall "Seattle" --target seo-jin-on --limit 2 2>&1)"
ok "recency weight lifts the newer lexical hit" '[ "$(sed -n 1p <<<"$out" | grep -c "bike")" = 1 ]'
out0="$(NUNCHI_STATE_WINDOW=0 NUNCHI_RECENCY_WEIGHT=0 python3 "$NP" recall "Seattle" --target seo-jin-on --limit 2 2>&1)"
ok "NUNCHI_RECENCY_WEIGHT=0 is pure bm25 order" '[ "$(grep -c Seattle <<<"$out0")" = 2 ]'
# 4. without a target there is no window (nothing to scope it to), lexical hits only
out="$(python3 "$NP" recall "$q" --limit 3 2>&1)"
ok "no target → no window merge" '! grep -q "Austin" <<<"$out"'
# 5. dialectic prompt carries valid_from dates and the recency instruction (stubbed backend echoes the prompt)
mkdir -p "$TMP/bin"
cat > "$TMP/bin/claude" <<'SH'
#!/usr/bin/env bash
# echo the prompt back so the test can inspect what dialectic built
printf '%s\n' "$2"
SH
chmod 755 "$TMP/bin/claude"
out="$(PATH="$TMP/bin:/usr/bin:/bin" CCC_NUNCHI_MEMPALACE_HOME="$TMP/nopalace" python3 "$NP" dialectic "$q" --target seo-jin-on 2>&1)"
ok "dialectic lists current facts with their valid_from date" 'grep -q "^- \[#3|inferred\] (2026-10-20) Austin address updated" <<<"$out"'
ok "dialectic instructs to treat the most recent fact as current" 'grep -q "가장 최근 것을 현재 상태로" <<<"$out"'
# 6. reindex-vecs is a no-op without an embedder and never fails
out="$(python3 "$NP" reindex-vecs 2>&1)"; rc=$?
ok "reindex-vecs without model2vec reports and exits 0" '[ "$rc" = 0 ] && grep -q "no embedder" <<<"$out"'
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
