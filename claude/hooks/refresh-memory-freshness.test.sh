#!/usr/bin/env bash
# Hermetic Wiki freshness tests. No Wiki network calls.
# (The Honcho freshness half retired with the Honcho plumbing, #1436.)
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REFRESH="$ROOT/claude/hooks/refresh-memory.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Inherited CCC_MEMORY_* paths point refresh-memory.sh at the real node state
# instead of the fixture: 14 of 15 assertions miss on a live node (#1023).
ccc_test_reset_hook_env

pass=0
fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

state="$TMP/state"
cache="$TMP/cache"
tools="$TMP/tools"
bin="$TMP/bin"
mkdir -p "$state" "$cache" "$tools" "$bin"

write_exec_stub "$bin/timeout" <<'SH'
shift
exec "$@"
SH

write_exec_stub "$bin/wiki-agent" <<'SH'
printf 'wiki\n' >> "${WIKI_CALL_LOG:?}"
printf 'fresh Wiki fixture\n'
SH

write_exec_stub "$tools/ccc-memory-query.sh" <<'SH'
task="$(sed -n '1,40p' "${CCC_STATE_DIR:?}/current-task.txt" 2>/dev/null | tr '\n' ' ')"
printf 'task: %s; node: fixture' "${task:-current task}"
case "${CCC_MEMORY_QUERY_INCLUDE_PROMPT:-1}" in
  0|false|FALSE|off|OFF|no|NO) ;;
  *)
    prompt="$(sed -n '1,40p' "${CCC_STATE_DIR:?}/current-prompt.txt" 2>/dev/null | tr '\n' ' ')"
    [ -z "$prompt" ] || printf '; prompt: %s' "$prompt"
    ;;
esac
SH

write_exec_stub "$tools/ccc-memory-consolidate.sh" <<'SH'
exit 0
SH

write_exec_stub "$tools/ccc-memory-index.sh" <<'SH'
exit 0
SH

printf 'task alpha\n' > "$state/current-task.txt"
printf 'prompt one\n' > "$state/current-prompt.txt"

export WIKI_CALL_LOG="$TMP/wiki-calls.log"
: > "$WIKI_CALL_LOG"

run_refresh() {
  PATH="$bin:$PATH" \
  HOME="$TMP/home" \
  CCC_STATE_DIR="$state" \
  CCC_MEMORY_CACHE_DIR="$cache" \
  CCC_HOOK_DIR="$ROOT/claude/hooks" \
  CCC_MEMORY_TOOLS_DIR="$tools" \
  CCC_WIKI_AGENT_BIN="$bin/wiki-agent" \
  CCC_WIKI_CACHE_MAX_AGE_SEC=21600 \
    bash "$REFRESH" 2>&1
}

out="$(run_refresh)"; rc=$?
ok "first refresh calls the wiki prefetch" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$WIKI_CALL_LOG")" = 1 ]'
ok "first refresh records only hashes, never the query" \
  'jq -e ".status == \"ok\" and (.query_hash | length) == 64 and has(\"query\") == false" "$cache/.wiki.status.json" >/dev/null'
first_refreshed_at="$(jq -r '.refreshed_at' "$cache/.wiki.status.json")"

printf 'prompt two should not churn the stable task key\n' > "$state/current-prompt.txt"
out="$(run_refresh)"; rc=$?
ok "fresh same-task refresh skips the wiki prefetch" \
  '[ "$rc" = 0 ] && grep -q "wiki refresh skipped reason=fresh" <<<"$out" && [ "$(wc -l < "$WIKI_CALL_LOG")" = 1 ]'
ok "fresh skip does not advance refreshed_at" \
  '[ "$(jq -r ".refreshed_at" "$cache/.wiki.status.json")" = "$first_refreshed_at" ]'

printf 'task beta\n' > "$state/current-task.txt"
out="$(run_refresh)"; rc=$?
ok "material task change refreshes the wiki prefetch" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$WIKI_CALL_LOG")" = 2 ]'

out="$(CCC_WIKI_FORCE_REFRESH=1 run_refresh)"; rc=$?
ok "explicit wiki force refresh bypasses freshness" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$WIKI_CALL_LOG")" = 3 ]'

jq '.refreshed_at = "2000-01-01T00:00:00Z"' "$cache/.wiki.status.json" \
  > "$cache/.wiki.status.json.tmp" \
  && mv "$cache/.wiki.status.json.tmp" "$cache/.wiki.status.json"
out="$(run_refresh)"; rc=$?
ok "expired wiki status refreshes the prefetch" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$WIKI_CALL_LOG")" = 4 ]'

rm -f "$cache/wiki.txt"
out="$(run_refresh)"; rc=$?
ok "cleared wiki cache file repopulates despite a fresh ok status" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$WIKI_CALL_LOG")" = 5 ] && [ -s "$cache/wiki.txt" ]'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
