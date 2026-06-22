#!/usr/bin/env bash
# Tests for distill/queue-drain.sh — curl is stubbed; no Honcho/network calls.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DRAIN="$HERE/queue-drain.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

mkdir -p "$TMP/bin" "$TMP/state" "$TMP/bodies"
cat > "$TMP/bin/curl" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >> "${CURL_STUB_LOG:?}"
if [[ "$*" == *"--data-binary @-"* ]]; then
  mkdir -p "${CURL_STUB_BODY_DIR:?}"
  cat > "$CURL_STUB_BODY_DIR/message.json"
fi
case "$*" in
  */health*) printf '%s' "${CURL_STUB_HEALTH_HTTP:-204}" ;;
  */messages*) printf '%s' "${CURL_STUB_MESSAGE_HTTP:-201}" ;;
  *) printf '%s' "${CURL_STUB_HTTP:-201}" ;;
esac
SH
chmod +x "$TMP/bin/curl"
export PATH="$TMP/bin:$PATH"
export CURL_STUB_LOG="$TMP/curl.log"
export CURL_STUB_BODY_DIR="$TMP/bodies"

CFG="$TMP/honcho.json"
cat > "$CFG" <<'JSON'
{"baseUrl":"http://honcho.invalid","workspace":"test-ws","aiPeer":"seoseo-test","authToken":"secret-token"}
JSON
export CCC_HONCHO_CFG="$CFG"
export CCC_STATE_DIR="$TMP/state"

PAYLOAD='{"session_id":"sess-queued","trigger":"manual","distilled_at":"2026-01-01T00:00:00Z","honcho":[{"kind":"context","text":"queued fact","subject":"session"}],"wiki_candidates":[]}'
QUEUE="$TMP/state/honcho-queue.jsonl"
DEAD="$TMP/state/honcho-queue.jsonl.dead"
LOG="$TMP/state/distill.log"

printf '%s\n' "$PAYLOAD" > "$QUEUE"
: > "$CURL_STUB_LOG"
CURL_STUB_HEALTH_HTTP=503 bash "$DRAIN"; rc=$?
ok "health failure exits 0" '[ "$rc" = 0 ]'
ok "health failure leaves queue intact" '[ "$(wc -l < "$QUEUE")" = 1 ] && ! jq -e "has(\"_attempts\")" "$QUEUE" >/dev/null'
ok "health failure does not call messages endpoint" 'grep -q "/health" "$CURL_STUB_LOG" && ! grep -q "/messages" "$CURL_STUB_LOG"'
ok "health failure logs skip" 'grep -q "\[drain\] skip reason=honcho-health http=503" "$LOG"'

printf '%s\n' "$PAYLOAD" > "$QUEUE"
: > "$CURL_STUB_LOG"
rm -f "$TMP/bodies/message.json"
CURL_STUB_HEALTH_HTTP=204 CURL_STUB_MESSAGE_HTTP=201 bash "$DRAIN"; rc=$?
ok "successful drain exits 0" '[ "$rc" = 0 ]'
ok "successful drain truncates queue" '[ ! -s "$QUEUE" ]'
ok "successful drain logs ok=1" 'grep -q "\[drain\] drained ok=1 failed=0 dropped=0 processed=1" "$LOG"'
ok "successful drain posts replay metadata" 'jq -e ".messages[0].metadata.replay == true and .messages[0].metadata.trigger == \"manual\" and (.messages[0].content | contains(\"(replayed)\"))" "$TMP/bodies/message.json" >/dev/null'

printf '%s\n' "$PAYLOAD" > "$QUEUE"
: > "$CURL_STUB_LOG"
CURL_STUB_HEALTH_HTTP=204 CURL_STUB_MESSAGE_HTTP=503 bash "$DRAIN"; rc=$?
ok "failed message exits 0" '[ "$rc" = 0 ]'
ok "failed message increments attempts" '[ "$(wc -l < "$QUEUE")" = 1 ] && jq -e "._attempts == 1" "$QUEUE" >/dev/null'
ok "failed message logs failed=1" 'grep -q "\[drain\] drained ok=0 failed=1 dropped=0 processed=1" "$LOG"'

jq -c '. + {_attempts:3}' <<<"$PAYLOAD" > "$QUEUE"
: > "$CURL_STUB_LOG"
rm -f "$DEAD"
CURL_STUB_HEALTH_HTTP=204 CURL_STUB_MESSAGE_HTTP=201 bash "$DRAIN"; rc=$?
ok "max-attempt payload exits 0" '[ "$rc" = 0 ]'
ok "max-attempt payload moves to dead letter" '[ ! -s "$QUEUE" ] && [ "$(wc -l < "$DEAD")" = 1 ] && jq -e "._attempts == 3" "$DEAD" >/dev/null'
ok "max-attempt payload does not call messages" '! grep -q "/messages" "$CURL_STUB_LOG"'
ok "max-attempt payload logs dropped=1" 'grep -q "\[drain\] drained ok=0 failed=0 dropped=1 processed=1" "$LOG"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
