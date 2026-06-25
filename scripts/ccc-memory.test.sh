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
ok "setup installs memory helper tools beside hooks" 'grep -q "rc=0" <<<"$out" && [ -x "$install_claude/hooks/ccc-memory-index.sh" ] && [ -x "$install_claude/hooks/ccc-memory-search.sh" ]'
out="$(CCC_STATE_DIR="$TMP/install-eval-state" bash "$install_claude/hooks/ccc-memory-eval.sh" Honcho 2>&1)"; rc=$?
ok "installed memory eval finds helper tools beside hooks" '[ "$rc" = 0 ] && jq -e ".ok == true" >/dev/null <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
