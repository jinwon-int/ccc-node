#!/usr/bin/env bash
# Hermetic Honcho freshness tests. No provider or Wiki network calls.
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

write_exec_stub "$bin/curl" <<'SH'
printf 'honcho\n' >> "${HONCHO_CALL_LOG:?}"
case "${HONCHO_STUB_MODE:-ok}" in
  ok) printf '{"content":"task-aware Honcho fixture"}\n' ;;
  empty) printf '{}\n' ;;
  error) printf 'fixture failure\n' >&2; exit 1 ;;
  *) exit 2 ;;
esac
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

cfg="$TMP/honcho.json"
printf '%s\n' \
  '{"baseUrl":"https://honcho.invalid","workspace":"fixture","peerName":"peer-a","target":"owner-a","reasoningLevel":"low","authToken":"token-a"}' \
  > "$cfg"
chmod 600 "$cfg"
printf 'task alpha\n' > "$state/current-task.txt"
printf 'prompt one\n' > "$state/current-prompt.txt"

export WIKI_CALL_LOG="$TMP/wiki-calls.log"
export HONCHO_CALL_LOG="$TMP/honcho-calls.log"
: > "$WIKI_CALL_LOG"
: > "$HONCHO_CALL_LOG"

run_refresh() {
  PATH="$bin:$PATH" \
  HOME="$TMP/home" \
  CCC_STATE_DIR="$state" \
  CCC_MEMORY_CACHE_DIR="$cache" \
  CCC_HOOK_DIR="$ROOT/claude/hooks" \
  CCC_MEMORY_TOOLS_DIR="$tools" \
  CCC_WIKI_AGENT_BIN="$bin/wiki-agent" \
  CCC_HONCHO_CFG="$cfg" \
  CCC_HONCHO_CACHE_MAX_AGE_SEC=21600 \
    bash "$REFRESH" 2>&1
}

out="$(run_refresh)"; rc=$?
ok "first refresh calls both sources" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 1 ] && [ "$(wc -l < "$WIKI_CALL_LOG")" = 1 ]'
ok "first refresh records only hashes for the Honcho freshness key" \
  'jq -e ".status == \"ok\" and (.query_hash | length) == 64 and (.config_hash | length) == 64 and has(\"query\") == false" "$cache/.honcho.status.json" >/dev/null'
first_refreshed_at="$(jq -r '.refreshed_at' "$cache/.honcho.status.json")"

printf 'prompt two should not churn the stable task key\n' > "$state/current-prompt.txt"
out="$(run_refresh)"; rc=$?
ok "fresh same-task refresh skips Honcho but still refreshes Wiki" \
  '[ "$rc" = 0 ] && grep -q "honcho refresh skipped reason=fresh" <<<"$out" && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 1 ] && [ "$(wc -l < "$WIKI_CALL_LOG")" = 2 ]'
ok "fresh skip does not advance refreshed_at" \
  '[ "$(jq -r ".refreshed_at" "$cache/.honcho.status.json")" = "$first_refreshed_at" ]'

out="$(CCC_HONCHO_FORCE_REFRESH=1 run_refresh)"; rc=$?
ok "explicit force refresh bypasses freshness" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 2 ]'

printf 'task beta\n' > "$state/current-task.txt"
out="$(run_refresh)"; rc=$?
ok "material task change refreshes Honcho" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 3 ]'

jq '.reasoningLevel = "medium"' "$cfg" > "$cfg.tmp" && mv "$cfg.tmp" "$cfg"
chmod 600 "$cfg"
out="$(run_refresh)"; rc=$?
ok "non-secret config change refreshes Honcho" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 4 ]'

printf 'distill-success:fixture\n' > "$state/honcho-refresh.invalidate"
out="$(run_refresh)"; rc=$?
ok "distill invalidation forces one refresh and is consumed on success" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 5 ] && [ ! -e "$state/honcho-refresh.invalidate" ]'

jq '.refreshed_at = "2000-01-01T00:00:00Z"' "$cache/.honcho.status.json" \
  > "$cache/.honcho.status.json.tmp" \
  && mv "$cache/.honcho.status.json.tmp" "$cache/.honcho.status.json"
out="$(run_refresh)"; rc=$?
ok "expired success refreshes Honcho" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 6 ]'

printf 'distill-success-before-error:fixture\n' > "$state/honcho-refresh.invalidate"
out="$(HONCHO_STUB_MODE=error run_refresh)"; rc=$?
ok "provider error is recorded without failing the warmer or consuming invalidation" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 7 ] && [ -s "$state/honcho-refresh.invalidate" ] && jq -e ".status == \"error\"" "$cache/.honcho.status.json" >/dev/null'
out="$(run_refresh)"; rc=$?
ok "error state is retried even inside the TTL" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 8 ] && [ ! -e "$state/honcho-refresh.invalidate" ] && jq -e ".status == \"ok\"" "$cache/.honcho.status.json" >/dev/null'

out="$(CCC_HONCHO_FORCE_REFRESH=1 HONCHO_STUB_MODE=empty run_refresh)"; rc=$?
ok "empty provider result is a successful freshness state" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 9 ] && jq -e ".status == \"empty\"" "$cache/.honcho.status.json" >/dev/null'
out="$(HONCHO_STUB_MODE=empty run_refresh)"; rc=$?
ok "fresh empty result is reused inside the TTL" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 9 ]'

jq '.authToken = "rotated-token-not-in-fingerprint"' "$cfg" > "$cfg.tmp" \
  && mv "$cfg.tmp" "$cfg"
chmod 600 "$cfg"
out="$(HONCHO_STUB_MODE=empty run_refresh)"; rc=$?
ok "credential rotation alone does not expose or churn the non-secret fingerprint" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$HONCHO_CALL_LOG")" = 9 ] && ! grep -q "rotated-token-not-in-fingerprint" "$cache/.honcho.status.json"'

ok "Wiki remains independent across every warmer invocation" \
  '[ "$(wc -l < "$WIKI_CALL_LOG")" = 12 ]'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
