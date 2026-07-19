#!/usr/bin/env bash
# Unit tests for lib/memory_render.py (#584 P2-1) — the extracted load-memory.sh
# python helpers. Invokes the module directly, pinning each subcommand's
# stdin/env/argv contract so the shell orchestrator can stay thin.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MOD="$HERE/memory_render.py"
pass=0; fail=0
BASE_TMP="${TMPDIR:-/tmp}"; mkdir -p "$BASE_TMP"
TMP="$(mktemp -d "$BASE_TMP/ccc-memory-render-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# ---- limit-bytes ------------------------------------------------------------
out="$(printf 'short text' | python3 "$MOD" limit-bytes 100)"
ok "limit-bytes passes small input through byte-exact" '[ "$out" = "short text" ]'

out="$(printf 'A%.0s' $(seq 1 500) | python3 "$MOD" limit-bytes 120)"
ok "limit-bytes appends the truncation marker" 'grep -q "truncated by CCC memory budget" <<<"$out"'
n="$(printf 'A%.0s' $(seq 1 500) | python3 "$MOD" limit-bytes 120 | wc -c)"
ok "limit-bytes stays within the declared byte cap (marker included)" '[ "$n" -le 120 ]'

# UTF-8 safety: cutting mid-multibyte-character must not emit broken bytes.
n_bad="$(printf '한%.0s' $(seq 1 200) | python3 "$MOD" limit-bytes 100 | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1; echo $?)"
ok "limit-bytes truncation keeps valid UTF-8" '[ "$n_bad" = 0 ]'

out="$(printf 'anything' | python3 "$MOD" limit-bytes 0)"
ok "limit-bytes 0 disables the cap" '[ "$out" = "anything" ]'

# ---- dedup-local-hot --------------------------------------------------------
dj='{"results":[{"source":"memory","snippet":"alpha bravo charlie delta echo"},{"source":"structured","snippet":"alpha bravo charlie delta echo"},{"source":"cache","snippet":"unique zulu yankee xray whiskey"}]}'
out="$(INJECTED='context alpha bravo charlie delta echo tail' SEARCH_JSON="$dj" python3 "$MOD" dedup-local-hot)"
ok "dedup drops memory hit already fully injected" '! grep -q "\"source\": \"memory\"" <<<"$out"'
ok "dedup always keeps structured (distilled-fact) hits" 'grep -q "structured" <<<"$out"'
ok "dedup keeps cache hit not present in injected text" 'grep -q "unique zulu" <<<"$out"'
ok "dedup records drop accounting" 'jq -e ".injectionDedup.dropped == 1 and .injectionDedup.kept == 2" >/dev/null <<<"$out"'

out="$(INJECTED='x' SEARCH_JSON='NOT_JSON{{' python3 "$MOD" dedup-local-hot)"; rc=$?
ok "dedup passes malformed JSON through raw (exit 0)" '[ "$rc" = 0 ] && [ "$out" = "NOT_JSON{{" ]'

# ---- filter-disabled-wiki-hits ---------------------------------------------
fj='{"results":[{"source":"cache","path":"/c/wiki.txt","snippet":"stale wiki"},{"source":"state","path":"/s/wiki-candidates.md","snippet":"cand"},{"source":"distill-local","path":"/s/facts.jsonl","snippet":"local fact"},{"source":"state","path":"/s/distill-last.json","snippet":"distill artifact"},{"source":"memory","path":"/m/MEMORY.md","snippet":"plain memory"}]}'
out="$(SEARCH_JSON="$fj" python3 "$MOD" filter-disabled-wiki-hits)"
ok "filter removes wiki.txt and wiki-candidates rows" '! grep -q "stale wiki" <<<"$out" && ! grep -q "wiki-candidates" <<<"$out"'
ok "filter keeps distill-local facts" 'grep -q "local fact" <<<"$out"'
ok "filter removes distill-last.json artifacts" '! grep -q "distill artifact" <<<"$out"'
ok "filter keeps plain memory rows" 'grep -q "plain memory" <<<"$out"'
out="$(SEARCH_JSON='NOT_JSON' python3 "$MOD" filter-disabled-wiki-hits)"
ok "filter fails closed to empty results on malformed JSON" '[ "$out" = "{\"results\":[]}" ]'

# ---- render-local-hot -------------------------------------------------------
rj='{"results":[{"source":"structured","snippet":"… [Fact] one  two …"},{"source":"cache","snippet":"cached line"},{"source":"weird","snippet":"other"}]}'
out="$(SEARCH_JSON="$rj" python3 "$MOD" render-local-hot)"
ok "render emits compact labelled lines" '[ "$(head -1 <<<"$out")" = "- (fact) Fact one two" ]'
ok "render maps cache source and unknown->memory" 'grep -q "^- (cache) cached line$" <<<"$out" && grep -q "^- (memory) other$" <<<"$out"'
out="$(SEARCH_JSON='BROKEN' python3 "$MOD" render-local-hot)"
ok "render passes malformed JSON through raw" '[ "$out" = "BROKEN" ]'

# ---- merge-local-hot --------------------------------------------------------
pj='{"results":[{"path":"/p/a","snippet":"s1","score":0.2}]}'
sj='{"results":[{"path":"/p/a","snippet":"s1","score":0.9},{"path":"/p/b","snippet":"s2","score":0.8}]}'
lj='{"results":[{"path":"/p/c","snippet":"s3","score":0.5}]}'
out="$(PRIMARY_JSON="$pj" SHARED_JSON="$sj" LEGACY_JSON="$lj" python3 "$MOD" merge-local-hot)"
ok "merge dedupes by (path,snippet) with private precedence" 'jq -e "[.results[] | select(.path==\"/p/a\")] | length == 1 and .[0].memoryAudience == \"private\"" >/dev/null <<<"$out"'
ok "merge tags shared and legacy audiences" 'jq -e "(.results[] | select(.path==\"/p/b\").memoryAudience) == \"shared\" and (.results[] | select(.path==\"/p/c\").memoryAudience) == \"private-legacy\"" >/dev/null <<<"$out"'
ok "merge sorts by score descending" 'jq -e ".results | map(.path) == [\"/p/b\",\"/p/c\",\"/p/a\"]" >/dev/null <<<"$out"'
out="$(PRIMARY_JSON='junk' SHARED_JSON="$sj" LEGACY_JSON='' python3 "$MOD" merge-local-hot)"
ok "merge tolerates a malformed source (others still merged)" 'jq -e ".results | length == 2" >/dev/null <<<"$out"'

# ---- dynamic-budget ---------------------------------------------------------
# alloc = max(maxlocal, total - reserve - m - r - w - h); limit clamped [base,maxlim].
out="$(python3 "$MOD" dynamic-budget 12000 1000 3000 180 5 25 2000 500 0 0)"
ok "budget reclaims slack for the local block" '[ "$out" = "8500 25" ]'
out="$(python3 "$MOD" dynamic-budget 12000 1000 500 180 5 25 4000 2000 5000 4000)"
ok "budget never drops below the static floor / base limit" '[ "$out" = "500 5" ]'
out="$(python3 "$MOD" dynamic-budget 12000 1000 3000 180 5 25 2000 500 4000 3000)"
ok "budget mid-range limit follows ~180B/result" '[ "$out" = "3000 16" ]'

# ---- run-memory-search-bounded ---------------------------------------------
cat > "$TMP/fast-tool.sh" <<'SH'
#!/usr/bin/env bash
printf '{"q":"%s","limit":"%s","state":"%s","usage":"%s"}' \
  "$1" "${CCC_MEMORY_SEARCH_LIMIT:-}" "${CCC_STATE_DIR:-}" "${CCC_MEMORY_RECORD_USAGE:-}"
SH
chmod +x "$TMP/fast-tool.sh"
out="$(python3 "$MOD" run-memory-search-bounded "$TMP/fast-tool.sh" myquery 7 3 "$TMP/state-x")"
ok "bounded runner passes query/limit/state env to the tool" '[ "$out" = "{\"q\":\"myquery\",\"limit\":\"7\",\"state\":\"'"$TMP"'/state-x\",\"usage\":\"0\"}" ]'

cat > "$TMP/fail-tool.sh" <<'SH'
#!/usr/bin/env bash
printf 'partial output'
exit 3
SH
chmod +x "$TMP/fail-tool.sh"
out="$(python3 "$MOD" run-memory-search-bounded "$TMP/fail-tool.sh" q 5 3 "")"
ok "bounded runner suppresses output on nonzero tool exit" '[ -z "$out" ]'

cat > "$TMP/stall-tool.sh" <<'SH'
#!/usr/bin/env bash
sleep 30 &
printf '%s\n' "$!" > "$STALL_PID_FILE"
wait
SH
chmod +x "$TMP/stall-tool.sh"
start="$(date +%s)"
out="$(STALL_PID_FILE="$TMP/stall.pid" python3 "$MOD" run-memory-search-bounded "$TMP/stall-tool.sh" q 5 0.3 "")"; rc=$?
elapsed=$(( $(date +%s) - start ))
ok "bounded runner enforces the deadline (exit 0, no output)" '[ "$rc" = 0 ] && [ -z "$out" ] && [ "$elapsed" -le 5 ]'
sleep 0.2
stall_pid="$(cat "$TMP/stall.pid" 2>/dev/null || true)"
ok "bounded runner killpg reaps the whole tool process group" '[ -n "$stall_pid" ] && ! kill -0 "$stall_pid" 2>/dev/null'

out="$(python3 "$MOD" run-memory-search-bounded "$TMP/does-not-exist" q 5 3 "")"; rc=$?
ok "bounded runner exits 0 quietly when the tool is missing" '[ "$rc" = 0 ] && [ -z "$out" ]'

# ---- dispatcher -------------------------------------------------------------
python3 "$MOD" no-such-subcommand </dev/null >/dev/null 2>&1; rc=$?
ok "dispatcher rejects unknown subcommands (exit 2 -> shell fallback path)" '[ "$rc" = 2 ]'
python3 "$MOD" </dev/null >/dev/null 2>&1; rc=$?
ok "dispatcher rejects missing subcommand" '[ "$rc" = 2 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
