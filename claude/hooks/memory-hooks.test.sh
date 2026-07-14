#!/usr/bin/env bash
# No-network smoke tests for load-memory.sh and refresh-memory.sh.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
pass=0; fail=0
BASE_TMP="${TMPDIR:-/tmp}"; mkdir -p "$BASE_TMP"
TMP="$(mktemp -d "$BASE_TMP/ccc-memory-hooks-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

state="$TMP/state"; cache="$TMP/cache"; mem="$TMP/mem"; tools="$TMP/tools"
mkdir -p "$state" "$cache" "$mem" "$tools"
printf 'Node memory: safe fact\n' > "$mem/MEMORY.md"
printf 'User memory: Korean concise\n' > "$mem/USER.md"
printf 'Cached wiki fact\n' > "$cache/wiki.txt"
printf 'Cached honcho fact\n' > "$cache/honcho.txt"
cat > "$tools/ccc-memory-query.sh" <<'SH'
#!/usr/bin/env bash
printf 'current task query'
SH
chmod +x "$tools/ccc-memory-query.sh"
cat > "$tools/ccc-memory-search.sh" <<'SH'
#!/usr/bin/env bash
if [ "${CCC_FAKE_MALFORMED:-0}" = 1 ]; then
  printf 'MALFORMED_STALE_WIKI_PAYLOAD'
  exit 0
fi
cat <<'JSON'
{"results":[{"source":"cache","path":"/stale/cache/wiki.txt","snippet":"Stale wiki index hit"},{"source":"structured","path":"/state/facts.jsonl","snippet":"Local hot memory result"}]}
JSON
SH
chmod +x "$tools/ccc-memory-search.sh"

out="$(HOME="$TMP/home" CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" CCC_HONCHO_MEMORY_ENABLED=0 CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "load-memory exits 0 with local caches only" '[ "$rc" = 0 ]'
ok "load-memory injects bounded local sources" 'grep -q "Node memory: safe fact" <<<"$out" && grep -q "Cached wiki fact" <<<"$out" && grep -q "Local hot memory result" <<<"$out"'
ok "load-memory does not require network credentials" '! grep -qi "token\|authorization\|Traceback" <<<"$out"'

out="$(HOME="$TMP/home" CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 CCC_HONCHO_MEMORY_ENABLED=0 CCC_MEMORY_USER_LABEL='External Owner' CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "wiki-disabled load-memory keeps non-Wiki sources and custom identity" '[ "$rc" = 0 ] && grep -q "Node memory: safe fact" <<<"$out" && grep -q "Local hot memory result" <<<"$out" && grep -q "Honcho working memory — External Owner" <<<"$out"'
ok "wiki-disabled load-memory drops direct and stale-index Wiki content" '! grep -q "Cached wiki fact\|Stale wiki index hit\|## Family Wiki\|verify Wiki source" <<<"$out"'
out="$(HOME="$TMP/home" CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" CCC_WIKI_MEMORY_ENABLED=0 CCC_HONCHO_MEMORY_ENABLED=0 CCC_FAKE_MALFORMED=1 CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "wiki-disabled malformed local search fails closed without raw payload" '[ "$rc" = 0 ] && ! grep -q "MALFORMED_STALE_WIKI_PAYLOAD" <<<"$out"'

fakebin="$TMP/bin"; mkdir -p "$fakebin"
cat > "$fakebin/timeout" <<'SH'
#!/usr/bin/env bash
shift
exec "$@"
SH
chmod +x "$fakebin/timeout"
cat > "$tools/ccc-memory-index.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat > "$tools/ccc-memory-consolidate.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$tools/ccc-memory-index.sh" "$tools/ccc-memory-consolidate.sh"
out="$(PATH="$fakebin:$PATH" HOME="$TMP/home" CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" CCC_WIKI_AGENT_BIN="$TMP/missing/wiki-agent" CCC_HONCHO_MEMORY_ENABLED=0 bash "$ROOT/claude/hooks/refresh-memory.sh" 2>&1)"; rc=$?
ok "refresh-memory exits 0 when wiki missing and honcho disabled" '[ "$rc" = 0 ]'
ok "refresh-memory writes source meta without network success" 'jq -e ".sources.wiki.status == \"missing\" and .sources.honcho.status == \"disabled\" and .sources.local_index.status == \"ok\"" "$cache/meta.json" >/dev/null'
ok "refresh-memory lock and meta stay local" '[ -f "$cache/.refresh.lock" ] && [ -f "$cache/.last-refresh" ]'

cat > "$fakebin/wiki-agent" <<'SH'
#!/usr/bin/env bash
printf called > "${WIKI_CALL_MARKER:?}"
printf 'unexpected wiki payload\n'
SH
chmod +x "$fakebin/wiki-agent"
rm -f "$TMP/wiki-called"
out="$(PATH="$fakebin:$PATH" WIKI_CALL_MARKER="$TMP/wiki-called" HOME="$TMP/home" CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" CCC_WIKI_AGENT_BIN="$fakebin/wiki-agent" CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 CCC_HONCHO_MEMORY_ENABLED=0 bash "$ROOT/claude/hooks/refresh-memory.sh" 2>&1)"; rc=$?
ok "wiki-disabled refresh does not invoke wiki-agent" '[ "$rc" = 0 ] && [ ! -e "$TMP/wiki-called" ]'
ok "wiki-disabled refresh reports effective disabled status" 'jq -e ".sources.wiki.status == \"disabled\" and .sources.wiki.bytes == 0" "$cache/meta.json" >/dev/null'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
