#!/usr/bin/env bash
# Tests for distill/queue-drain.sh — stub curl; no network calls.
# Exercises: drain success, failure retry, dead-letter, health probe skip,
# lock guard, empty-facts drop, off-switch.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DRAIN="$HERE/queue-drain.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# ---- stub curl --------------------------------------------------------------
mkdir -p "$TMP/bin" "$TMP/state"
cat > "$TMP/bin/curl" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
log="${CURL_STUB_LOG:?}"
health_http="${CURL_STUB_HEALTH_HTTP:-204}"
msg_http="${CURL_STUB_MSG_HTTP:-201}"
session_http="${CURL_STUB_SESSION_HTTP:-201}"
printf '%s\n' "$*" >> "$log"
case "$*" in
  */health*)
    printf '%s' "$health_http"
    ;;
  */sessions*ensure-session*|*/sessions*http=%*)
    printf 'ensure-session http=%s\n' "$session_http"
    ;;
  *__HTTP__*)
    printf '{"ok":true}\n__HTTP__%s__' "$msg_http"
    ;;
  *)
    printf '%s' "$msg_http"
    ;;
esac
SH
chmod +x "$TMP/bin/curl"
export PATH="$TMP/bin:$PATH"
export CURL_STUB_LOG="$TMP/curl.log"

# ---- fixtures ---------------------------------------------------------------
CFG="$TMP/honcho.json"
cat > "$CFG" <<'JSON'
{"baseUrl":"http://honcho.invalid","workspace":"test-ws","aiPeer":"test-node","authToken":"secret-token"}
JSON
export CCC_HONCHO_CFG="$CFG"
export CCC_STATE_DIR="$TMP/state"
QUEUE="$TMP/state/honcho-queue.jsonl"
DEAD="$TMP/state/honcho-queue.jsonl.dead"
LOG="$TMP/state/distill.log"

PAYLOAD='{"session_id":"sess-1","trigger":"sessionend","distilled_at":"2026-01-01T00:00:00Z","honcho":[{"kind":"context","text":"fact one","subject":"session"}],"wiki_candidates":[]}'
PAYLOAD_NO_FACTS='{"session_id":"sess-empty","trigger":"sessionend","distilled_at":"2026-01-01T00:00:00Z","honcho":[],"wiki_candidates":[]}'

# ---- test: empty queue exits clean ------------------------------------------
: > "$LOG"
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "empty queue exits 0" '[ "$rc" = 0 ] && ! grep -q "drained" "$LOG"'

# ---- test: single entry, drain success --------------------------------------
: > "$LOG"
printf '%s\n' "$PAYLOAD" > "$QUEUE"
export CURL_STUB_MSG_HTTP=201
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "drain success exits 0" '[ "$rc" = 0 ]'
ok "drain success logs ok=1" 'grep -q "drained ok=1 failed=0 dropped=0" "$LOG"'
ok "drain success empties queue" '[ ! -s "$QUEUE" ]'

# ---- test: drain failure increments attempts --------------------------------
: > "$LOG"
printf '%s\n' "$PAYLOAD" > "$QUEUE"
export CURL_STUB_MSG_HTTP=503
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "drain fail exits 0 (fail-open)" '[ "$rc" = 0 ]'
ok "drain fail logs failed=1" 'grep -q "drained ok=0 failed=1 dropped=0" "$LOG"'
ok "drain fail keeps entry with _attempts=1" 'jq -e "._attempts == 1" "$QUEUE" >/dev/null'

# ---- test: second failure increments attempts -------------------------------
: > "$LOG"
export CURL_STUB_MSG_HTTP=503
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "second fail increments _attempts to 2" 'jq -e "._attempts == 2" "$QUEUE" >/dev/null && grep -q "drained ok=0 failed=1" "$LOG"'

# ---- test: third failure still tries (attempts=2 < 3), then 4th run dead-letters
: > "$LOG"
export CURL_STUB_MSG_HTTP=503
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "third fail still retries (attempts=2 -> 3)" 'grep -q "drained ok=0 failed=1 dropped=0" "$LOG" && jq -e "._attempts == 3" "$QUEUE" >/dev/null'
: > "$LOG"
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "fourth run dead-letters (attempts=3 >= 3)" '[ -f "$DEAD" ] && [ -s "$DEAD" ] && grep -q "drained ok=0 failed=0 dropped=1" "$LOG"'
ok "dead entry preserves session_id" 'jq -e ".session_id == \"sess-1\"" "$DEAD" >/dev/null'
ok "dead-letter empties queue" '[ ! -s "$QUEUE" ]'
rm -f "$DEAD"

# ---- test: successful drain of entry with _attempts existing ----------------
: > "$LOG"
printf '%s\n' "$PAYLOAD" | jq -c '. + {_attempts:2}' > "$QUEUE"
export CURL_STUB_MSG_HTTP=201
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "pre-attempted entry drains successfully" 'grep -q "drained ok=1 failed=0 dropped=0" "$LOG"'
ok "pre-attempted entry empties queue" '[ ! -s "$QUEUE" ]'

# ---- test: health probe skip ------------------------------------------------
: > "$LOG"
printf '%s\n' "$PAYLOAD" > "$QUEUE"
export CURL_STUB_HEALTH_HTTP=503 CURL_STUB_MSG_HTTP=201
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "health down exits 0" '[ "$rc" = 0 ]'
ok "health down skips drain with reason" 'grep -q "skip reason=honcho-health http=503" "$LOG"'
ok "health down does not touch queue" '[ -s "$QUEUE" ] && [ "$(wc -l < "$QUEUE" | tr -d " ")" = 1 ]'

# ---- test: lock-hold guard --------------------------------------------------
: > "$LOG"
printf '%s\n' "$PAYLOAD" > "$QUEUE"
export CURL_STUB_HEALTH_HTTP=204 CURL_STUB_MSG_HTTP=201
# Hold lock via background subshell.
LOCK="$TMP/state/.honcho-queue.lock"
exec 8>"$LOCK"
flock -x 8
out="$(bash "$DRAIN" 2>&1)"; rc=$?
exec 8>&-  # release
ok "lock-held exits 0" '[ "$rc" = 0 ]'
ok "lock-held logs skip" 'grep -q "skip reason=lock-held" "$LOG"'
ok "lock-held does not touch queue" '[ "$(wc -l < "$QUEUE" | tr -d " ")" = 1 ]'

# ---- test: empty-facts entry is dropped -------------------------------------
: > "$LOG"
printf '%s\n' "$PAYLOAD_NO_FACTS" > "$QUEUE"
export CURL_STUB_MSG_HTTP=201
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "empty-facts entry exits 0" '[ "$rc" = 0 ]'
ok "empty-facts entry is dropped not failed" 'grep -q "drained ok=0 failed=0 dropped=1" "$LOG"'
ok "empty-facts entry empties queue" '[ ! -s "$QUEUE" ]'

# ---- test: distill.disabled off-switch respected ----------------------------
: > "$LOG"
printf '%s\n' "$PAYLOAD" > "$QUEUE"
touch "$TMP/state/distill.disabled"
export CURL_STUB_HEALTH_HTTP=204 CURL_STUB_MSG_HTTP=201
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "distill-disabled exits 0" '[ "$rc" = 0 ]'
ok "distill-disabled logs skip reason" 'grep -q "skip reason=distill-disabled" "$LOG"'
ok "distill-disabled does not drain queue" '[ "$(wc -l < "$QUEUE" | tr -d " ")" = 1 ]'
rm -f "$TMP/state/distill.disabled"

# ---- test: max batch limit keeps remaining entries --------------------------
: > "$LOG"
rm -f "$QUEUE"
for i in $(seq 1 5); do
  printf '%s\n' "$PAYLOAD" | jq -c --arg sid "sess-$i" '.session_id = $sid' >> "$QUEUE"
done
export CCC_DISTILL_DRAIN_BATCH=2
export CURL_STUB_HEALTH_HTTP=204 CURL_STUB_MSG_HTTP=201
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "batch limit exits 0" '[ "$rc" = 0 ]'
ok "batch limit drains 2" 'grep -q "drained ok=2 failed=0 dropped=0 processed=2" "$LOG"'
ok "batch limit keeps remaining 3" '[ "$(wc -l < "$QUEUE" | tr -d " ")" = 3 ]'
unset CCC_DISTILL_DRAIN_BATCH

# ---- test: missing honcho config exits clean --------------------------------
: > "$LOG"
printf '%s\n' "$PAYLOAD" > "$QUEUE"
rm -f "$CFG"
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "missing honcho cfg exits 0" '[ "$rc" = 0 ]'
ok "missing honcho cfg logs skip" 'grep -q "skip reason=no-honcho-cfg" "$LOG"'

# ---- test: missing baseUrl exits clean --------------------------------------
: > "$LOG"
printf '%s\n' '{}' > "$CFG"
printf '%s\n' "$PAYLOAD" > "$QUEUE"
out="$(bash "$DRAIN" 2>&1)"; rc=$?
ok "missing baseUrl exits 0" '[ "$rc" = 0 ]'
ok "missing baseUrl logs skip" 'grep -q "skip reason=no-baseUrl" "$LOG"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
