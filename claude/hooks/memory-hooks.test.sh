#!/usr/bin/env bash
# No-network smoke tests for load-memory.sh and refresh-memory.sh.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
pass=0; fail=0
BASE_TMP="${TMPDIR:-/tmp}"; mkdir -p "$BASE_TMP"
TMP="$(ccc_test_tmpdir "$BASE_TMP/ccc-memory-hooks-test.XXXXXX")" || exit 1
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
if [ -n "${CCC_USAGE_MARKER:-}" ] && [ "${CCC_MEMORY_RECORD_USAGE:-0}" = 1 ]; then
  printf 'recorded\n' > "$CCC_USAGE_MARKER"
fi
if [ "${CCC_FAKE_STALL:-0}" = 1 ]; then
  sleep 2 &
  stall_pid=$!
  [ -n "${CCC_STALL_PID_FILE:-}" ] && printf '%s\n' "$stall_pid" > "$CCC_STALL_PID_FILE"
  wait "$stall_pid"
fi
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
ok "nunchi-off leaves Honcho title unchanged" '! grep -q "secondary — nunchi" <<<"$out"'
printf 'on' > "$state/nunchi.mode"
out="$(HOME="$TMP/home" CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" CCC_HONCHO_MEMORY_ENABLED=0 CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "nunchi-on labels Honcho secondary (#824)" '[ "$rc" = 0 ] && grep -q "secondary — nunchi snapshot is primary" <<<"$out"'
rm -f "$state/nunchi.mode"
out="$(HOME="$TMP/home" CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" CCC_WIKI_MEMORY_ENABLED=0 CCC_HONCHO_MEMORY_ENABLED=0 CCC_FAKE_MALFORMED=1 CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "wiki-disabled malformed local search fails closed without raw payload" '[ "$rc" = 0 ] && ! grep -q "MALFORMED_STALE_WIKI_PAYLOAD" <<<"$out"'

start_ns="$(python3 -c 'import time; print(time.monotonic_ns())')"
stall_pid_file="$TMP/stalled-search-child.pid"
out="$(HOME="$TMP/home" CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" CCC_HONCHO_MEMORY_ENABLED=0 CCC_FAKE_STALL=1 CCC_STALL_PID_FILE="$stall_pid_file" CCC_MEMORY_SEARCH_TIMEOUT_SEC=0.1 CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
end_ns="$(python3 -c 'import time; print(time.monotonic_ns())')"
elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))
stall_pid="$(cat "$stall_pid_file" 2>/dev/null || true)"
ok "stalled local search is bounded while canonical memory still injects" '[ "$rc" = 0 ] && [ "$elapsed_ms" -lt 1500 ] && grep -q "Node memory: safe fact" <<<"$out" && jq -e ".hookSpecificOutput.additionalContext" >/dev/null <<<"$out"'
ok "stalled local search process group is terminated and reaped" '[ -n "$stall_pid" ] && ! kill -0 "$stall_pid" 2>/dev/null'

usage_marker="$TMP/sessionstart-usage-recorded"
rm -f "$usage_marker"
out="$(HOME="$TMP/home" CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" CCC_HONCHO_MEMORY_ENABLED=0 CCC_USAGE_MARKER="$usage_marker" CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "SessionStart local search is read-only and does not record usage feedback" '[ "$rc" = 0 ] && [ ! -e "$usage_marker" ]'

legacy_home="$TMP/legacy home"
mkdir -p "$legacy_home/.hermes/memories"
printf 'Legacy path-with-spaces memory fact\n' > "$legacy_home/.hermes/memories/MEMORY.md"
printf 'Legacy path-with-spaces user fact\n' > "$legacy_home/.hermes/memories/USER.md"
out="$(HOME="$legacy_home" CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$TMP/missing-memory-dir" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" CCC_LOCAL_MEMORY_ENABLED=0 CCC_WIKI_MEMORY_ENABLED=0 CCC_HONCHO_MEMORY_ENABLED=0 CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "legacy Hermes fallback quotes HOME paths containing spaces" '[ "$rc" = 0 ] && grep -q "Legacy path-with-spaces memory fact" <<<"$out" && grep -q "Legacy path-with-spaces user fact" <<<"$out"'

# Audience-scoped mode: public surfaces get only shared memory. A private DM
# gets its own memory plus shared and private-only legacy input.
audroot="$TMP/audiences"
private_scope="private-00000000000000000000000000000000"
legacy_mem="$TMP/legacy-private/memories"
legacy_hermes_mem="$TMP/legacy-private/hermes-memories"
legacy_cache="$TMP/legacy-private/cache"
shared_mem="$audroot/shared/memories"
private_mem="$audroot/$private_scope/memories"
mkdir -p "$legacy_mem" "$legacy_hermes_mem" "$legacy_cache" \
  "$shared_mem" "$private_mem" \
  "$audroot/shared/state" "$audroot/shared/cache" \
  "$audroot/$private_scope/state" "$audroot/$private_scope/cache"
printf 'LEGACY_PRIVATE_ONLY marker\n' > "$legacy_mem/MEMORY.md"
printf 'LEGACY_HERMES_PRIVATE_ONLY marker\n' > "$legacy_hermes_mem/MEMORY.md"
printf 'LEGACY_WIKI_PRIVATE_ONLY marker\n' > "$legacy_cache/wiki.txt"
printf 'LEGACY_HONCHO_PRIVATE_ONLY marker\n' > "$legacy_cache/honcho.txt"
printf 'SHARED_PUBLIC marker\n' > "$shared_mem/MEMORY.md"
printf 'DM_PRIVATE_ONLY marker\n' > "$private_mem/MEMORY.md"

out="$(HOME="$TMP/home" \
  CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=shared CCC_MEMORY_SCOPE=shared \
  CCC_MEMORY_AUDIENCE_ROOT="$audroot" \
  CCC_STATE_DIR="$audroot/shared/state" CCC_MEMORY_CACHE_DIR="$audroot/shared/cache" \
  CCC_RESUME_FILE="$audroot/shared/state/resume.md" \
  CCC_MEMORY_DIR="$shared_mem" CCC_MEMORY_SHARED_STATE_DIR="$audroot/shared/state" \
  CCC_MEMORY_SHARED_CACHE_DIR="$audroot/shared/cache" \
  CCC_MEMORY_SHARED_DIR="$shared_mem" CCC_MEMORY_LEGACY_DIR="$legacy_mem" \
  CCC_MEMORY_LEGACY_HERMES_DIR="$legacy_hermes_mem" \
  CCC_MEMORY_LEGACY_CACHE_DIR="$legacy_cache" \
  CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" \
  CCC_LOCAL_MEMORY_ENABLED=0 CCC_WIKI_MEMORY_ENABLED=1 CCC_HONCHO_MEMORY_ENABLED=1 \
  CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "shared audience injects only public memory" '[ "$rc" = 0 ] && grep -q "SHARED_PUBLIC marker" <<<"$out" && ! grep -q "LEGACY_PRIVATE_ONLY\|LEGACY_HERMES_PRIVATE_ONLY\|LEGACY_WIKI_PRIVATE_ONLY\|LEGACY_HONCHO_PRIVATE_ONLY\|DM_PRIVATE_ONLY" <<<"$out"'
ok "shared audience force-disables unscoped remote sources" '! grep -q "Cached wiki fact\|Cached honcho fact" <<<"$out" && grep -q "shared public facts only" <<<"$out"'

out="$(HOME="$TMP/home" \
  CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=private CCC_MEMORY_SCOPE="$private_scope" \
  CCC_MEMORY_AUDIENCE_ROOT="$audroot" \
  CCC_STATE_DIR="$audroot/$private_scope/state" CCC_MEMORY_CACHE_DIR="$audroot/$private_scope/cache" \
  CCC_RESUME_FILE="$audroot/$private_scope/state/resume.md" \
  CCC_MEMORY_DIR="$private_mem" CCC_MEMORY_SHARED_STATE_DIR="$audroot/shared/state" \
  CCC_MEMORY_SHARED_CACHE_DIR="$audroot/shared/cache" \
  CCC_MEMORY_SHARED_DIR="$shared_mem" CCC_MEMORY_LEGACY_DIR="$legacy_mem" \
  CCC_MEMORY_LEGACY_HERMES_DIR="$legacy_hermes_mem" \
  CCC_MEMORY_LEGACY_CACHE_DIR="$legacy_cache" \
  CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" \
  CCC_LOCAL_MEMORY_ENABLED=0 CCC_WIKI_MEMORY_ENABLED=0 CCC_HONCHO_MEMORY_ENABLED=1 \
  CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "private audience injects private legacy, private scoped, and shared memory" '[ "$rc" = 0 ] && grep -q "LEGACY_PRIVATE_ONLY marker" <<<"$out" && grep -q "DM_PRIVATE_ONLY marker" <<<"$out" && grep -q "SHARED_PUBLIC marker" <<<"$out"'
ok "private audience still force-disables unscoped Honcho" 'grep -q "Honcho disabled" <<<"$out" && grep -q "private DM plus explicitly shared" <<<"$out"'

out="$(HOME="$TMP/home" \
  CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=private CCC_MEMORY_SCOPE="$private_scope" \
  CCC_MEMORY_AUDIENCE_ROOT="$audroot" \
  CCC_STATE_DIR="$audroot/$private_scope/state" CCC_MEMORY_CACHE_DIR="$audroot/$private_scope/cache" \
  CCC_RESUME_FILE="$audroot/$private_scope/state/resume.md" \
  CCC_MEMORY_DIR="$private_mem" CCC_MEMORY_SHARED_STATE_DIR="$audroot/shared/state" \
  CCC_MEMORY_SHARED_CACHE_DIR="$audroot/shared/cache" \
  CCC_MEMORY_SHARED_DIR="$shared_mem" CCC_MEMORY_LEGACY_DIR="$legacy_mem" \
  CCC_MEMORY_LEGACY_HERMES_DIR="$legacy_hermes_mem" \
  CCC_MEMORY_LEGACY_CACHE_DIR="$legacy_cache" \
  CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" \
  CCC_LOCAL_MEMORY_ENABLED=0 CCC_WIKI_MEMORY_ENABLED=1 CCC_HONCHO_MEMORY_ENABLED=1 \
  CCC_HONCHO_AUDIENCE_SCOPED=1 CCC_HONCHO_WORKSPACE_SCOPE="$private_scope" \
  CCC_HONCHO_SHARED_WORKSPACE_SCOPE=shared CCC_MEMORY_NO_REFRESH=1 \
  bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "private compatibility route inherits both Claude and Hermes built-ins" \
  '[ "$rc" = 0 ] && grep -q "LEGACY_PRIVATE_ONLY marker" <<<"$out" && grep -q "LEGACY_HERMES_PRIVATE_ONLY marker" <<<"$out"'
ok "private compatibility route falls back to pre-scope Wiki and Honcho caches" \
  'grep -q "LEGACY_WIKI_PRIVATE_ONLY marker" <<<"$out" && grep -q "LEGACY_HONCHO_PRIVATE_ONLY marker" <<<"$out"'

out="$(HOME="$TMP/home" \
  CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=shared CCC_MEMORY_SCOPE=shared \
  CCC_MEMORY_AUDIENCE_ROOT="$audroot" \
  CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_SHARED_STATE_DIR="$audroot/shared/state" \
  CCC_MEMORY_SHARED_CACHE_DIR="$audroot/shared/cache" \
  CCC_MEMORY_SHARED_DIR="$shared_mem" CCC_HOOK_DIR="$ROOT/claude/hooks" \
  CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "incomplete scoped paths fail closed before global memory read" '[ "$rc" = 0 ] && grep -q "invalid audience metadata" <<<"$out" && ! grep -q "Node memory: safe fact\|Cached wiki fact\|Cached honcho fact" <<<"$out"'

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

cat > "$fakebin/curl" <<'SH'
#!/usr/bin/env bash
url=""
for arg in "$@"; do
  case "$arg" in http://*|https://*) url="$arg" ;; esac
done
printf '%s\n' "$url" >> "${HONCHO_URL_LOG:?}"
case "$url" in
  *family--ccc-private-00000000000000000000000000000000*)
    printf '{"content":"PRIVATE_HONCHO_ONLY"}\n'
    ;;
  *family--ccc-shared*)
    printf '{"content":"SHARED_HONCHO_PUBLIC"}\n'
    ;;
  */workspaces/family/peers/*)
    printf '{"content":"LEGACY_HONCHO_PRIVATE_ONLY"}\n'
    ;;
  *)
    printf '{"content":""}\n'
    ;;
esac
SH
chmod +x "$fakebin/curl"
honcho_cfg="$TMP/honcho.json"
printf '%s\n' '{"baseUrl":"https://honcho.invalid","workspace":"family","peerName":"peer-a"}' > "$honcho_cfg"
chmod 600 "$honcho_cfg"
honcho_url_log="$TMP/honcho-urls.log"

: > "$honcho_url_log"
rm -f "$audroot/shared/cache/honcho.txt"
out="$(PATH="$fakebin:$PATH" HONCHO_URL_LOG="$honcho_url_log" HOME="$TMP/home" \
  CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=shared CCC_MEMORY_SCOPE=shared \
  CCC_MEMORY_AUDIENCE_ROOT="$audroot" \
  CCC_STATE_DIR="$audroot/shared/state" CCC_MEMORY_CACHE_DIR="$audroot/shared/cache" \
  CCC_MEMORY_DIR="$shared_mem" CCC_MEMORY_INDEX_DB="$audroot/shared/state/memory-index.sqlite" \
  CCC_MEMORY_FACTS_FILE="$audroot/shared/state/memory-facts.jsonl" \
  CCC_MEMORY_SHARED_STATE_DIR="$audroot/shared/state" \
  CCC_MEMORY_SHARED_CACHE_DIR="$audroot/shared/cache" \
  CCC_MEMORY_SHARED_DIR="$shared_mem" \
  CCC_MEMORY_SHARED_FACTS_FILE="$audroot/shared/state/memory-facts.jsonl" \
  CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" \
  CCC_WIKI_MEMORY_ENABLED=0 CCC_HONCHO_MEMORY_ENABLED=1 \
  CCC_HONCHO_AUDIENCE_SCOPED=1 CCC_HONCHO_WORKSPACE_SCOPE=shared \
  CCC_HONCHO_SHARED_WORKSPACE_SCOPE=shared CCC_HONCHO_CFG="$honcho_cfg" \
  bash "$ROOT/claude/hooks/refresh-memory.sh" 2>&1)"; rc=$?
ok "shared audience refresh queries only its physical Honcho workspace" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$honcho_url_log")" = 1 ] && grep -q "family--ccc-shared" "$honcho_url_log" && ! grep -q "private-\|/workspaces/family/" "$honcho_url_log"'
ok "shared audience cache contains no private or legacy Honcho result" \
  'grep -q "SHARED_HONCHO_PUBLIC" "$audroot/shared/cache/honcho.txt" && ! grep -q "PRIVATE_HONCHO_ONLY\|LEGACY_HONCHO_PRIVATE_ONLY" "$audroot/shared/cache/honcho.txt"'

: > "$honcho_url_log"
rm -f "$audroot/$private_scope/cache/honcho.txt"
out="$(PATH="$fakebin:$PATH" HONCHO_URL_LOG="$honcho_url_log" HOME="$TMP/home" \
  CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=private CCC_MEMORY_SCOPE="$private_scope" \
  CCC_MEMORY_AUDIENCE_ROOT="$audroot" \
  CCC_STATE_DIR="$audroot/$private_scope/state" CCC_MEMORY_CACHE_DIR="$audroot/$private_scope/cache" \
  CCC_MEMORY_DIR="$private_mem" CCC_MEMORY_INDEX_DB="$audroot/$private_scope/state/memory-index.sqlite" \
  CCC_MEMORY_FACTS_FILE="$audroot/$private_scope/state/memory-facts.jsonl" \
  CCC_MEMORY_SHARED_STATE_DIR="$audroot/shared/state" \
  CCC_MEMORY_SHARED_CACHE_DIR="$audroot/shared/cache" \
  CCC_MEMORY_SHARED_DIR="$shared_mem" \
  CCC_MEMORY_SHARED_FACTS_FILE="$audroot/shared/state/memory-facts.jsonl" \
  CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" \
  CCC_WIKI_MEMORY_ENABLED=0 CCC_HONCHO_MEMORY_ENABLED=1 \
  CCC_HONCHO_AUDIENCE_SCOPED=1 CCC_HONCHO_WORKSPACE_SCOPE="$private_scope" \
  CCC_HONCHO_SHARED_WORKSPACE_SCOPE=shared CCC_HONCHO_CFG="$honcho_cfg" \
  bash "$ROOT/claude/hooks/refresh-memory.sh" 2>&1)"; rc=$?
ok "private audience refresh queries private shared and private-only legacy workspaces" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$honcho_url_log")" = 3 ] && grep -q "family--ccc-$private_scope" "$honcho_url_log" && grep -q "family--ccc-shared" "$honcho_url_log" && grep -q "/workspaces/family/peers/" "$honcho_url_log"'
ok "private audience cache labels all allowed Honcho sources" \
  'grep -q "PRIVATE_HONCHO_ONLY" "$audroot/$private_scope/cache/honcho.txt" && grep -q "SHARED_HONCHO_PUBLIC" "$audroot/$private_scope/cache/honcho.txt" && grep -q "LEGACY_HONCHO_PRIVATE_ONLY" "$audroot/$private_scope/cache/honcho.txt"'

out="$(HOME="$TMP/home" \
  CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=shared CCC_MEMORY_SCOPE=shared \
  CCC_MEMORY_AUDIENCE_ROOT="$audroot" \
  CCC_STATE_DIR="$audroot/shared/state" CCC_MEMORY_CACHE_DIR="$audroot/shared/cache" \
  CCC_RESUME_FILE="$audroot/shared/state/resume.md" CCC_MEMORY_DIR="$shared_mem" \
  CCC_MEMORY_SHARED_STATE_DIR="$audroot/shared/state" \
  CCC_MEMORY_SHARED_CACHE_DIR="$audroot/shared/cache" \
  CCC_MEMORY_SHARED_DIR="$shared_mem" \
  CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" \
  CCC_LOCAL_MEMORY_ENABLED=0 CCC_WIKI_MEMORY_ENABLED=0 CCC_HONCHO_MEMORY_ENABLED=1 \
  CCC_HONCHO_AUDIENCE_SCOPED=1 CCC_HONCHO_WORKSPACE_SCOPE=shared \
  CCC_HONCHO_SHARED_WORKSPACE_SCOPE=shared CCC_MEMORY_NO_REFRESH=1 \
  bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "shared audience injects only its physically scoped Honcho cache" \
  '[ "$rc" = 0 ] && grep -q "SHARED_HONCHO_PUBLIC" <<<"$out" && ! grep -q "PRIVATE_HONCHO_ONLY\|LEGACY_HONCHO_PRIVATE_ONLY" <<<"$out"'

out="$(HOME="$TMP/home" \
  CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=private CCC_MEMORY_SCOPE="$private_scope" \
  CCC_MEMORY_AUDIENCE_ROOT="$audroot" \
  CCC_STATE_DIR="$audroot/$private_scope/state" CCC_MEMORY_CACHE_DIR="$audroot/$private_scope/cache" \
  CCC_RESUME_FILE="$audroot/$private_scope/state/resume.md" CCC_MEMORY_DIR="$private_mem" \
  CCC_MEMORY_SHARED_STATE_DIR="$audroot/shared/state" \
  CCC_MEMORY_SHARED_CACHE_DIR="$audroot/shared/cache" \
  CCC_MEMORY_SHARED_DIR="$shared_mem" CCC_MEMORY_LEGACY_DIR="$legacy_mem" \
  CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$tools" \
  CCC_LOCAL_MEMORY_ENABLED=0 CCC_WIKI_MEMORY_ENABLED=0 CCC_HONCHO_MEMORY_ENABLED=1 \
  CCC_HONCHO_AUDIENCE_SCOPED=1 CCC_HONCHO_WORKSPACE_SCOPE="$private_scope" \
  CCC_HONCHO_SHARED_WORKSPACE_SCOPE=shared CCC_MEMORY_NO_REFRESH=1 \
  bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "private audience injects private shared and private-only legacy Honcho cache" \
  '[ "$rc" = 0 ] && grep -q "PRIVATE_HONCHO_ONLY" <<<"$out" && grep -q "SHARED_HONCHO_PUBLIC" <<<"$out" && grep -q "LEGACY_HONCHO_PRIVATE_ONLY" <<<"$out"'


# --- #781: a dead endpoint must not read like a quiet one ------------------
# honcho_chat used to pipe curl into jq, so the function returned JQ's status.
# jq exits 0 on empty input, which made a timeout, a refused connection and a
# genuine empty answer produce one indistinguishable outcome: "empty Honcho
# response". Operators chasing a stale cache were handed the least useful of
# the three readings. Each case now has its own status.
nc_home="$TMP/nchome"; mkdir -p "$nc_home"
nc_cache="$TMP/nccache"; nc_state="$TMP/ncstate"
nc_cfg="$TMP/nc-honcho.json"
printf '%s\n' '{"baseUrl":"https://honcho.invalid","workspace":"family","peerName":"peer-a"}' > "$nc_cfg"
chmod 600 "$nc_cfg"
nc_bin="$TMP/ncbin"; mkdir -p "$nc_bin"

run_honcho() { # <curl-body-script>
  rm -rf "$nc_cache" "$nc_state"; mkdir -p "$nc_cache" "$nc_state"
  # /bin/sh, not `#!/usr/bin/env bash`: Termux has no /usr/bin/env, and execvp
  # treats the resulting ENOENT as "not found here" and keeps walking PATH —
  # so a stub with that shebang silently hands the call to the real curl and the
  # test passes against the network instead of the fixture.
  printf '#!/bin/sh\n%s\n' "$1" > "$nc_bin/curl"
  chmod +x "$nc_bin/curl"
  PATH="$nc_bin:$PATH" HOME="$nc_home" \
    CCC_STATE_DIR="$nc_state" CCC_MEMORY_CACHE_DIR="$nc_cache" \
    CCC_HOOK_DIR="$ROOT/claude/hooks" \
    CCC_WIKI_MEMORY_ENABLED=0 CCC_HONCHO_MEMORY_ENABLED=1 \
    CCC_MEMORY_AUDIENCE_SCOPED=0 CCC_HONCHO_AUDIENCE_SCOPED=0 \
    CCC_HONCHO_CFG="$nc_cfg" \
    bash "$ROOT/claude/hooks/refresh-memory.sh" >/dev/null 2>&1
  jq -r '.sources.honcho.status' "$nc_cache/meta.json" 2>/dev/null
}

st="$(run_honcho 'printf "{\"content\":null}\n"')"
ok "content:null is reported as no-content, not as an empty/transport failure" '[ "$st" = "no-content" ]'

st="$(run_honcho 'exit 7')"
ok "a failing curl is reported as error, not as an empty response" '[ "$st" = "error" ]'

st="$(run_honcho 'exit 124')"
ok "a timed-out curl is reported as error, not as an empty response" '[ "$st" = "error" ]'

st="$(run_honcho 'printf "not json at all\n"')"
ok "a non-JSON body is reported as error, not as an empty response" '[ "$st" = "error" ]'

st="$(run_honcho 'printf "{\"content\":\"real recalled memory\"}\n"')"
ok "a real answer still succeeds" '[ "$st" = "ok" ]'
ok "a real answer is written to the cache" 'grep -q "real recalled memory" "$nc_cache/honcho.txt"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
