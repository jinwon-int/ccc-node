#!/usr/bin/env bash
# Tests for ccc memory cache/index/eval helpers.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

state="$TMP/state"
cache="$TMP/cache"
mem="$TMP/memories"
mkdir -p "$state" "$cache" "$mem"
printf 'test-node\n' > "$state/node.txt"
printf 'allowed operation policy\n' > "$mem/MEMORY.md"
printf 'user likes concise Korean reports\n' > "$mem/USER.md"
printf 'wiki cache contains Honcho hybrid memory profile\n' > "$cache/wiki.txt"
printf 'honcho cache contains practical evidence reports\n' > "$cache/honcho.txt"

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check json succeeds" '[ "$rc" = 0 ] && jq -e ".wiki.status == \"ok\" and .honcho.status == \"ok\"" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HONCHO_MEMORY_ENABLED=FALSE bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check treats uppercase FALSE as disabled" '[ "$rc" = 0 ] && jq -e ".honcho.status == \"disabled\"" >/dev/null <<<"$out"'

secret_a="VALUE_SHOULD_NOT_INDEX_A"
secret_b="VALUE_SHOULD_NOT_INDEX_B"
secret_c="VALUE_SHOULD_NOT_INDEX_C"
printf 'Authorization: Bearer %s\n' "$secret_a" >> "$mem/MEMORY.md"
printf 'api_key: %s\n' "$secret_b" >> "$mem/MEMORY.md"
printf 'https://x.test/?access_token=%s\n' "$secret_c" >> "$mem/MEMORY.md"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>&1)"; rc=$?
ok "memory index rebuild succeeds" '[ "$rc" = 0 ] && jq -e ".ok == true and .documents >= 2 and .distill_indexed == false" >/dev/null <<<"$out"'
mode="$(python3 - <<PY
import os, stat
p='$state/memory-index.sqlite'
print(oct(stat.S_IMODE(os.stat(p).st_mode)) if os.path.exists(p) else 'missing')
PY
)"
ok "memory index db is chmod 600" '[ "$mode" = "0o600" ]'
db_dump="$(python3 - <<PY
import sqlite3
con=sqlite3.connect('$state/memory-index.sqlite')
try:
    print('\n'.join(row[0] for row in con.execute('select content from memory_docs')))
finally:
    con.close()
PY
)"
ok "memory index redacts bearer/key/url secrets" '! grep -q "VALUE_SHOULD_NOT_INDEX_A\|VALUE_SHOULD_NOT_INDEX_B\|VALUE_SHOULD_NOT_INDEX_C" <<<"$db_dump"'

old_state="$TMP/old-state"
old_cache="$TMP/old-cache"
old_mem="$TMP/old-memories"
old_marker="LEAK_MARKER_OLD_DB_BYTES_SHOULD_DISAPPEAR"
mkdir -p "$old_state" "$old_cache" "$old_mem"
printf 'clean replacement memory\n' > "$old_mem/MEMORY.md"
printf 'clean replacement user\n' > "$old_mem/USER.md"
python3 - <<PY
import sqlite3
marker = '$old_marker'
con = sqlite3.connect('$old_state/memory-index.sqlite')
con.execute('CREATE TABLE memory_docs (source TEXT NOT NULL, path TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
con.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(path UNINDEXED, source UNINDEXED, content)')
content = marker * 400
con.execute('INSERT INTO memory_docs(source,path,content) VALUES(?,?,?)', ('old', '/tmp/old', content))
con.execute('INSERT INTO memory_fts(path,source,content) VALUES(?,?,?)', ('/tmp/old', 'old', content))
con.commit(); con.close()
PY
out="$(CCC_STATE_DIR="$old_state" CCC_MEMORY_CACHE_DIR="$old_cache" CCC_MEMORY_DIR="$old_mem" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>&1)"; rc=$?
old_marker_present="$(python3 - <<PY
from pathlib import Path
print('yes' if b'$old_marker' in Path('$old_state/memory-index.sqlite').read_bytes() else 'no')
PY
)"
ok "memory index rebuild scrubs old raw db bytes" '[ "$rc" = 0 ] && [ "$old_marker_present" = "no" ]'

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_DISABLE_FTS5=1 bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>&1)"; rc=$?
ok "memory index degrades to docs-only when FTS5 is disabled" '[ "$rc" = 0 ] && jq -e ".ok == true and .fts5_enabled == false" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" Honcho 2>&1)"; rc=$?
ok "memory search LIKE fallback works when FTS5 is unavailable" '[ "$rc" = 0 ] && jq -e ".results | length > 0" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" Honcho 2>&1)"; rc=$?
ok "memory search finds cache docs" '[ "$rc" = 0 ] && jq -e ".results | length > 0" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_PROFILE=hybrid CCC_LOCAL_MEMORY_ENABLED=1 CCC_MEMORY_QUERY=Honcho bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "load-memory emits hook json with bounded context" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext | contains(\"Local hot memory\")" >/dev/null <<<"$out"'

# Directly exercise the hook via a tiny budget and Korean memory; JSON must still parse.
printf '가나다라마바사아자차카타파하\n' > "$mem/USER.md"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_MAX_BYTES=90 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "load-memory byte budget remains valid JSON for UTF-8 text" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext" >/dev/null <<<"$out"'

caller_state="$TMP/caller-state"
mkdir -p "$caller_state"
printf 'keep\n' > "$caller_state/marker.txt"

query_state="$TMP/query-state"
query_cwd="$TMP/query-repo"
mkdir -p "$query_state" "$query_cwd"
printf 'query-node\n' > "$query_state/node.txt"
printf '%s\n' "$query_cwd" > "$query_state/cwd.txt"
printf 'Implement issue 186 memory roadmap with Honcho cache TTL\n' > "$query_state/current-task.txt"
( cd "$query_cwd" && git init -q && git config user.email test@example.invalid && git config user.name test && printf 'x\n' > changed-memory-file.txt && git add changed-memory-file.txt && git commit -q -m init && printf 'changed\n' >> changed-memory-file.txt )
out="$(CCC_STATE_DIR="$query_state" CCC_MEMORY_QUERY_EXTRA='Authorization: Bearer QUERY_SECRET_SHOULD_NOT_LEAK' bash "$ROOT/scripts/ccc-memory-query.sh" --mode remote 2>&1)"; rc=$?
ok "memory query helper builds redacted task-aware query" '[ "$rc" = 0 ] && grep -q "issue 186" <<<"$out" && grep -q "changed-memory-file.txt" <<<"$out" && ! grep -q "QUERY_SECRET_SHOULD_NOT_LEAK" <<<"$out"'

printf 'Implement issue 186 memory roadmap with Honcho cache TTL and changed-memory-file context\n' > "$mem/MEMORY.md"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>&1)"; rc=$?
helper_query="$(CCC_STATE_DIR="$query_state" CCC_WORKTREE="$query_cwd" bash "$ROOT/scripts/ccc-memory-query.sh" --mode local)"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" "$helper_query" 2>&1)"; rc=$?
ok "memory search tolerates task-aware helper query punctuation" '[ "$rc" = 0 ] && jq -e "(.results | length) > 0 and (.tokens | index(\"honcho\") != null)" >/dev/null <<<"$out"'

facts="$state/memory-facts.jsonl"
printf '%s\n' \
  '{"id":"fact-current","kind":"preference","text":"Current ccc-node editor fixture is Helix.","entities":["ccc-node","Helix"],"tags":["temporal"],"durability":"durable","privacy":"private","review":"auto-local"}' \
  '{"id":"fact-volatile","kind":"task-progress","text":"Volatile task progress mentions Helix PR pending and should be demoted.","durability":"volatile","privacy":"private","review":"auto-local"}' \
  '{"id":"fact-secret","kind":"risk","text":"api_key: VALUE_SHOULD_NOT_INDEX_FACT","durability":"durable","privacy":"sensitive-redacted","review":"auto-local"}' \
  > "$facts"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_FACTS_FILE="$facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>&1)"; rc=$?
ok "memory index includes structured facts" '[ "$rc" = 0 ] && jq -e ".documents >= 3" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" CCC_MEMORY_RETRIEVAL=hybrid-local bash "$ROOT/scripts/ccc-memory-search.sh" "current editor Helix" 2>&1)"; rc=$?
ok "hybrid-local search explains scoring signals" '[ "$rc" = 0 ] && jq -e ".retrievalMode == \"hybrid-local\" and (.results[0].signals.token_hits >= 1)" >/dev/null <<<"$out"'

# Default retrieval must apply the durability/source boosts too (not raw bm25),
# so a keyword-dense volatile fact with EQUAL coverage can't outrank a durable
# one. Distinct fixture so the only differentiator is the boost.
rank_facts="$state/rank-facts.jsonl"
printf '%s\n' \
  '{"id":"durable-policy","kind":"decision","text":"durable operating policy memory ranking default mode evidence.","durability":"durable","privacy":"private","review":"auto-local"}' \
  '{"id":"volatile-dense","kind":"task-progress","text":"durable operating policy memory ranking default mode durable operating policy memory ranking default mode volatile draft pending.","durability":"volatile","privacy":"private","review":"auto-local"}' \
  > "$rank_facts"
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_FACTS_FILE="$rank_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" "durable operating policy memory ranking" 2>&1)"; rc=$?
ok "default retrieval reranks with boosts (rerank/fusion mode)" '[ "$rc" = 0 ] && jq -e "(.retrievalMode == \"fts-rerank\") or (.retrievalMode == \"fusion-rrf\")" >/dev/null <<<"$out"'
ok "default retrieval demotes keyword-dense volatile below durable" '[ "$rc" = 0 ] && jq -e "(.results[0].path | contains(\"durable-policy\")) and (.results[0].signals.durability_penalty == 0) and ((.results | map(select(.path | contains(\"volatile-dense\")))[0].signals.durability_penalty) == -3.0)" >/dev/null <<<"$out"'
# Fusion lane: a char-ngram fuzzy lane recalls a doc when EVERY query token is
# typo'd/transposed so both FTS and the LIKE substring fallback miss it. Set
# CCC_MEMORY_FUSION=0 to fall back to the lexical lane only.
fuzz_facts="$state/fuzz-facts.jsonl"
printf '%s\n' \
  '{"id":"fuzzdoc","kind":"decision","text":"memory ranking default behaviour configuration.","durability":"durable","privacy":"private","review":"auto-local"}' \
  > "$fuzz_facts"
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_FACTS_FILE="$fuzz_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
fuzz_q="memmory rankng behaviuor configuratoin"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" CCC_MEMORY_FUSION=0 bash "$ROOT/scripts/ccc-memory-search.sh" "$fuzz_q" 2>&1)"; rc=$?
ok "lexical-only misses all-typo query" '[ "$rc" = 0 ] && jq -e "(.results | length) == 0" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" "$fuzz_q" 2>&1)"; rc=$?
ok "fusion fuzzy lane recalls all-typo query" '[ "$rc" = 0 ] && jq -e ".retrievalMode == \"fusion-rrf\" and (.results[0].path | contains(\"fuzzdoc\"))" >/dev/null <<<"$out"'
# restore the structured-fact index for the secret-redaction test below
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_FACTS_FILE="$facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
ok "structured fact indexing redacts secrets" '! python3 - <<PY | grep -q VALUE_SHOULD_NOT_INDEX_FACT
import sqlite3
con=sqlite3.connect("$state/memory-index.sqlite")
print("\n".join(r[0] for r in con.execute("select content from memory_docs")))
PY
'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_FACTS_FILE="$facts" CCC_MEMORY_RETRIEVAL=hybrid-local bash "$ROOT/scripts/ccc-memory-explain.sh" --json --query "current editor Helix" 2>&1)"; rc=$?
ok "memory explain emits read-only diagnostics" '[ "$rc" = 0 ] && jq -e ".ok == true and .safety.no_network == true and .search.retrievalMode == \"hybrid-local\"" >/dev/null <<<"$out"'
out="$(bash "$ROOT/scripts/ccc-memory-benchmark-export.sh" --json 2>&1)"; rc=$?
ok "benchmark export defaults to synthetic fixtures only" '[ "$rc" = 0 ] && jq -e ".ok == true and .real_memory_read == false and (.items | length) >= 3" >/dev/null <<<"$out"'

# Default profile (honcho) now queries the local hot-memory index too (was
# hybrid/max-perf-only). The structured-fact index built above is still present.
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_QUERY="current editor Helix" bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "default profile queries the local hot-memory index" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext | contains(\"\\\"results\\\"\")" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_LOCAL_MEMORY_ENABLED=0 CCC_MEMORY_QUERY="current editor Helix" bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "CCC_LOCAL_MEMORY_ENABLED=0 opts out of local hot memory" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext | (contains(\"local hot memory disabled\") and (contains(\"\\\"results\\\"\") | not))" >/dev/null <<<"$out"'

# Embedding (semantic) lane — opt-in via CCC_MEMORY_EMBED_CMD. Uses a local,
# no-network fake embedder with a tiny concept map so a synonym query recalls a
# doc that the surface-form lexical + fuzzy lanes both miss.
estate="$TMP/embed-state"; rm -rf "$estate"; mkdir -p "$estate/cache" "$estate/memories"
printf 'x\n' > "$estate/memories/MEMORY.md"; printf 'x\n' > "$estate/memories/USER.md"
printf '%s\n' '{"id":"autodoc","kind":"decision","text":"The automobile parking guideline for the node.","durability":"durable","privacy":"private","review":"auto-local"}' > "$estate/facts.jsonl"
cat > "$estate/fake-embed.py" <<'PYEMB'
import sys, json, re
text = sys.stdin.read().lower()
concepts = [["car","automobile","vehicle"],["policy","rule","guideline","rules","parking"]]
toks = set(re.findall(r"[a-z]+", text))
vec = [0.0]*(len(concepts)+1)
for i, ws in enumerate(concepts):
    for w in ws:
        if w in toks:
            vec[i] += 1.0
vec[-1] = 0.01  # baseline on a dedicated axis so unrelated docs don't false-match
print(json.dumps(vec))
PYEMB
embcmd="python3 $estate/fake-embed.py"
CCC_STATE_DIR="$estate" CCC_MEMORY_CACHE_DIR="$estate/cache" CCC_MEMORY_DIR="$estate/memories" CCC_MEMORY_FACTS_FILE="$estate/facts.jsonl" CCC_MEMORY_EMBED_CMD="$embcmd" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
ok "index precomputes embedding vectors when CCC_MEMORY_EMBED_CMD is set" 'python3 - "$estate/memory-index.sqlite" <<PY >/dev/null 2>&1
import sqlite3,sys
c=sqlite3.connect(sys.argv[1])
n=c.execute("select count(*) from memory_vectors").fetchone()[0]
sys.exit(0 if n>=1 else 1)
PY'
out="$(CCC_STATE_DIR="$estate" CCC_MEMORY_INDEX_DB="$estate/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" "car rules" 2>&1)"; rc=$?
ok "surface-form lanes miss the synonym query" '[ "$rc" = 0 ] && jq -e "(.results | length) == 0" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$estate" CCC_MEMORY_INDEX_DB="$estate/memory-index.sqlite" CCC_MEMORY_EMBED_CMD="$embcmd" bash "$ROOT/scripts/ccc-memory-search.sh" "car rules" 2>&1)"; rc=$?
ok "embedding lane recalls the synonym query" '[ "$rc" = 0 ] && jq -e "(.lanes | index(\"embedding\") != null) and (.results[0].path | contains(\"autodoc\"))" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$estate" CCC_MEMORY_INDEX_DB="$estate/memory-index.sqlite" CCC_MEMORY_EMBED_CMD=/bin/false bash "$ROOT/scripts/ccc-memory-search.sh" "automobile guideline" 2>&1)"; rc=$?
ok "embedding lane fails open when the provider errors" '[ "$rc" = 0 ] && jq -e "(.lanes | index(\"embedding\")) == null and (.results | length) >= 1" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$TMP/golden-state" bash "$ROOT/scripts/ccc-memory-eval.sh" --golden 2>&1)"; rc=$?
ok "memory eval golden-set reports precision recall mrr" '[ "$rc" = 0 ] && jq -e ".ok == true and .mode == \"golden\" and .metrics.precision_at_1 >= 0.5 and .metrics.recall_at_5 >= 0.5 and .metrics.mrr > 0 and .metrics.latency_p95_ms >= .metrics.latency_p50_ms" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$TMP/scenario-state" bash "$ROOT/scripts/ccc-memory-eval.sh" --scenario 2>&1)"; rc=$?
ok "memory eval scenario covers temporal conflict and volatile demotion" '[ "$rc" = 0 ] && jq -e ".ok == true and .mode == \"scenario\" and .metrics.temporal_current_accuracy == 1 and .metrics.volatile_exclusion_accuracy == 1" >/dev/null <<<"$out"'


printf '%s\n' '{"source":"wiki","status":"ok","refreshed_at":"2000-01-01T00:00:00Z","duration_ms":1,"bytes":10,"error":"","query_hash":"abc","stale":false,"max_age_sec":1}' > "$cache/wiki.meta.json"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_WIKI_CACHE_MAX_AGE_SEC=1 bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check exposes cache metadata and recomputes stale flag" '[ "$rc" = 0 ] && jq -e ".wiki.meta.stale == true and .wiki.meta.query_hash == \"abc\"" >/dev/null <<<"$out"'

printf '## CAND-001\nProposed wiki fact\n\n## CAND-002\nSecond fact\n' > "$state/wiki-candidates.md"
out="$(CCC_STATE_DIR="$state" bash "$ROOT/scripts/ccc-wiki-triage.sh" list 2>&1)"; rc=$?
ok "wiki triage lists local candidates without writing Wiki" '[ "$rc" = 0 ] && jq -e "(.candidates | length) == 2 and .candidates[0].id == \"CAND-001\"" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$caller_state" CCC_MEMORY_EVAL_KEEP_TMP=0 bash "$ROOT/scripts/ccc-memory-eval.sh" Honcho 2>&1)"; rc=$?
ok "memory eval harness succeeds with caller state" '[ "$rc" = 0 ] && jq -e ".ok == true" >/dev/null <<<"$out"'
ok "memory eval does not delete caller-provided state" '[ -f "$caller_state/marker.txt" ]'
ok "memory eval cleans only its internal temp dir" '! compgen -G "$caller_state/ccc-memory-eval.*" >/dev/null'

real_mem="$TMP/real-memories"
real_cache="$TMP/real-cache"
mkdir -p "$real_mem" "$real_cache"
printf 'DO_NOT_OVERWRITE_REAL_MEMORY\n' > "$real_mem/MEMORY.md"
printf 'DO_NOT_OVERWRITE_REAL_USER\n' > "$real_mem/USER.md"
printf 'DO_NOT_OVERWRITE_REAL_WIKI\n' > "$real_cache/wiki.txt"
printf 'DO_NOT_OVERWRITE_REAL_HONCHO\n' > "$real_cache/honcho.txt"
out="$(CCC_STATE_DIR="$TMP/eval-external-state" CCC_MEMORY_DIR="$real_mem" CCC_MEMORY_CACHE_DIR="$real_cache" CCC_MEMORY_EVAL_KEEP_TMP=0 bash "$ROOT/scripts/ccc-memory-eval.sh" Honcho 2>&1)"; rc=$?
ok "memory eval succeeds while external memory/cache env vars are set" '[ "$rc" = 0 ] && jq -e ".ok == true" >/dev/null <<<"$out"'
ok "memory eval does not overwrite external memory/cache dirs by default" 'grep -q DO_NOT_OVERWRITE_REAL_MEMORY "$real_mem/MEMORY.md" && grep -q DO_NOT_OVERWRITE_REAL_USER "$real_mem/USER.md" && grep -q DO_NOT_OVERWRITE_REAL_WIKI "$real_cache/wiki.txt" && grep -q DO_NOT_OVERWRITE_REAL_HONCHO "$real_cache/honcho.txt"'

install_home="$TMP/install-home"
install_claude="$TMP/install-claude"
install_hermes="$TMP/install-hermes"
out="$(HOME="$install_home" CCC_CLAUDE_DIR="$install_claude" CCC_HERMES_DIR="$install_hermes" bash "$ROOT/setup.sh" --no-backup >/dev/null 2>&1; echo rc=$?)"
ok "setup installs memory helper tools beside hooks" 'grep -q "rc=0" <<<"$out" && [ -x "$install_claude/hooks/ccc-memory-index.sh" ] && [ -x "$install_claude/hooks/ccc-memory-search.sh" ] && [ -x "$install_claude/hooks/ccc-memory-query.sh" ] && [ -x "$install_claude/hooks/ccc-memory-explain.sh" ] && [ -x "$install_claude/hooks/ccc-wiki-triage.sh" ] && [ -x "$install_claude/hooks/ccc-memory-benchmark-export.sh" ]'
out="$(CCC_STATE_DIR="$TMP/install-eval-state" bash "$install_claude/hooks/ccc-memory-eval.sh" Honcho 2>&1)"; rc=$?
ok "installed memory eval finds helper tools beside hooks" '[ "$rc" = 0 ] && jq -e ".ok == true" >/dev/null <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
