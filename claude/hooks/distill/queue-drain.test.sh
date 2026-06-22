#!/usr/bin/env bash
# Tests for distill/queue-drain.sh — curl is stubbed; no Honcho/network calls.
#
# These tests cover the LOGIC of the drain worker end-to-end without touching
# real Honcho. They substitute for the "real Honcho outage" exercise in #83,
# which requires mutating ~/.hermes/honcho.json (forbidden by current A2A
# approval scope). When real-world drain verification is approved separately,
# this hermetic suite still applies as a regression guard.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DRAIN="$HERE/queue-drain.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# ---- per-scenario curl stub ------------------------------------------------
# Each test writes a $TMP/bin/curl that records args + simulates one or more
# HTTP responses. The stub logs every call to $CURL_STUB_LOG and message POST
# bodies land in $CURL_STUB_BODY_DIR/message-N.json (auto-incremented per call).
write_curl_stub() {
  mkdir -p "$TMP/bin"
  cat > "$TMP/bin/curl" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >> "${CURL_STUB_LOG:?}"
mode="${CURL_STUB_MODE:?}"
case "$*" in
  */health*)
    case "$mode" in
      health-down)        printf '503' ;;
      health-ok)          printf '204' ;;
      *)                  printf '204' ;;
    esac
    ;;
  *ensure-session*)
    # ensure-session call: status output, no body capture.
    printf 'ensure-session http=%s\n' "${CURL_STUB_ENSURE_HTTP:-201}"
    ;;
  *)
    # message POST — capture --data-binary @- body to disk, auto-increment idx.
    # CURL_STUB_FAIL_RE: if set, fail (return 503) when the URL matches this regex.
    if [[ "$*" == *"--data-binary @-"* ]]; then
      idx_file="${CURL_STUB_IDX_FILE:?}"
      idx=0
      [ -f "$idx_file" ] && idx="$(cat "$idx_file" 2>/dev/null)"
      idx=$((idx + 1))
      printf '%s' "$idx" > "$idx_file"
      if [ -n "${CURL_STUB_BODY_DIR:-}" ]; then
        mkdir -p "$CURL_STUB_BODY_DIR"
        cat > "$CURL_STUB_BODY_DIR/message-$idx.json"
      fi
    fi
    if [ -n "${CURL_STUB_FAIL_RE:-}" ] && [[ "$*" =~ $CURL_STUB_FAIL_RE ]]; then
      printf '503'
    else
      printf '%s' "${CURL_STUB_MSG_HTTP:-201}"
    fi
    ;;
esac
SH
  chmod +x "$TMP/bin/curl"
}

install_stub_env() {
  : > "$CURL_STUB_LOG"
  rm -rf "$CURL_STUB_BODY_DIR"
  mkdir -p "$CURL_STUB_BODY_DIR"
  : > "$CURL_STUB_IDX_FILE"
  export PATH="$TMP/bin:$PATH"
}

write_cfg() {
  CFG="$1/honcho.json"
  cat > "$CFG" <<'JSON'
{"baseUrl":"http://honcho.invalid","workspace":"test-ws","aiPeer":"seoseo-test","authToken":"secret-token"}
JSON
}

seed_queue() {
  # 3 lines, each with a unique session id.
  local q="$1"
  : > "$q"
  printf '%s\n' '{"session_id":"sess-a","trigger":"manual","distilled_at":"2026-01-01T00:00:00Z","honcho":[{"kind":"context","text":"fact a","subject":"session"}]}' >> "$q"
  printf '%s\n' '{"session_id":"sess-b","trigger":"manual","distilled_at":"2026-01-01T00:00:00Z","honcho":[{"kind":"context","text":"fact b","subject":"session"}]}' >> "$q"
  printf '%s\n' '{"session_id":"sess-c","trigger":"manual","distilled_at":"2026-01-01T00:00:00Z","honcho":[{"kind":"context","text":"fact c","subject":"session"}]}' >> "$q"
}

# ---- environment scaffold (one TMP per scenario) --------------------------
mkdir -p "$TMP/bin" "$TMP/state"
write_curl_stub
export PATH="$TMP/bin:$PATH"
write_cfg "$TMP"
export CCC_HONCHO_CFG="$TMP/honcho.json"
export CCC_STATE_DIR="$TMP/state"
export CURL_STUB_LOG="$TMP/curl.log"
export CURL_STUB_BODY_DIR="$TMP/bodies"
export CURL_STUB_IDX_FILE="$TMP/curl.idx"

PAYLOAD='{"session_id":"sess-a","trigger":"manual","distilled_at":"2026-01-01T00:00:00Z","honcho":[{"kind":"context","text":"fact a","subject":"session"}],"wiki_candidates":[]}'

# ---- scenario 1: empty queue — drain exits 0, no log noise -----------------
: > "$CURL_STUB_LOG"
out="$(CURL_STUB_MODE=health-ok bash "$DRAIN" 2>&1)"; rc=$?
ok "empty queue exits 0" '[ "$rc" = 0 ]'
ok "empty queue does not touch curl" '[ ! -s "$CURL_STUB_LOG" ]'

# ---- scenario 2: queue file missing — same early-exit semantics -----------
rm -f "$TMP/state/honcho-queue.jsonl"
out="$(CURL_STUB_MODE=health-ok bash "$DRAIN" 2>&1)"; rc=$?
ok "missing queue file exits 0" '[ "$rc" = 0 ]'
ok "missing queue file does not touch curl" '[ ! -s "$CURL_STUB_LOG" ]'

# ---- scenario 3: no honcho.cfg — skip with explicit reason ----------------
mv "$CFG" "$CFG.bak"
seed_queue "$TMP/state/honcho-queue.jsonl"
out="$(bash "$DRAIN" 2>&1)"; rc=$?
mv "$CFG.bak" "$CFG"
ok "no honcho.cfg exits 0" '[ "$rc" = 0 ]'
ok "no honcho.cfg leaves queue intact" '[ "$(wc -l < "$TMP/state/honcho-queue.jsonl")" = 3 ]'
ok "no honcho.cfg logs skip reason" 'grep -q "skip reason=no-honcho-cfg" "$TMP/state/distill.log"'
: > "$TMP/state/distill.log"

# ---- scenario 4: distill.disabled off-switch respected ---------------------
touch "$TMP/state/distill.disabled"
seed_queue "$TMP/state/honcho-queue.jsonl"
: > "$CURL_STUB_LOG"
out="$(CURL_STUB_MODE=health-ok bash "$DRAIN" 2>&1)"; rc=$?
ok "disabled exits 0" '[ "$rc" = 0 ]'
ok "disabled leaves queue intact" '[ "$(wc -l < "$TMP/state/honcho-queue.jsonl")" = 3 ]'
ok "disabled does not touch curl" '[ ! -s "$CURL_STUB_LOG" ]'
ok "disabled logs skip reason" 'grep -q "skip reason=distill-disabled" "$TMP/state/distill.log"'
rm -f "$TMP/state/distill.disabled"
: > "$TMP/state/distill.log"

# ---- scenario 5: /health probe fails — queue preserved, no retry burn -----
seed_queue "$TMP/state/honcho-queue.jsonl"
: > "$CURL_STUB_LOG"
out="$(CURL_STUB_MODE=health-down bash "$DRAIN" 2>&1)"; rc=$?
ok "honcho down exits 0" '[ "$rc" = 0 ]'
ok "honcho down leaves queue intact" '[ "$(wc -l < "$TMP/state/honcho-queue.jsonl")" = 3 ]'
ok "honcho down only probed /health once" '[ "$(grep -c "/health" "$CURL_STUB_LOG")" = 1 ]'
ok "honcho down never POSTed to sessions/messages" '! grep -q "/sessions/sess-" "$CURL_STUB_LOG"'
ok "honcho down logs skip reason" 'grep -q "skip reason=honcho-health http=503" "$TMP/state/distill.log"'
: > "$TMP/state/distill.log"

# ---- scenario 6: success path — drain all, queue truncated, replay flag ---
seed_queue "$TMP/state/honcho-queue.jsonl"
: > "$CURL_STUB_LOG"
rm -rf "$CURL_STUB_BODY_DIR" && mkdir -p "$CURL_STUB_BODY_DIR"
out="$(CURL_STUB_MODE=health-ok CURL_STUB_MSG_HTTP=201 bash "$DRAIN" 2>&1)"; rc=$?
ok "success exits 0" '[ "$rc" = 0 ]'
ok "success truncates queue to 0 lines" '[ ! -s "$TMP/state/honcho-queue.jsonl" ]'
ok "success called /health" 'grep -q "/health" "$CURL_STUB_LOG"'
ok "success POSTed 3 ensure-session" '[ "$(grep -c "/v3/workspaces/test-ws/sessions " "$CURL_STUB_LOG")" = 3 ]'
ok "success POSTed 3 messages" '[ "$(grep -c "/sessions/sess-.*/messages" "$CURL_STUB_LOG")" = 3 ]'
ok "success wrote 3 message bodies" '[ "$(ls "$CURL_STUB_BODY_DIR"/message-*.json 2>/dev/null | wc -l)" = 3 ]'
ok "success bodies carry metadata.replay=true" 'jq -e ".messages[0].metadata.replay == true" "$CURL_STUB_BODY_DIR/message-1.json" >/dev/null'
ok "success bodies carry (replayed) content marker" 'jq -r ".messages[0].content" "$CURL_STUB_BODY_DIR/message-1.json" | grep -q "(replayed)"'
ok "success bodies carry node label (CCC_NODE or node.txt or hostname)" 'jq -e ".messages[0].metadata.node | length > 0" "$CURL_STUB_BODY_DIR/message-1.json" >/dev/null'
ok "success logs drained ok=3" 'grep -q "drained ok=3 failed=0 dropped=0 processed=3" "$TMP/state/distill.log"'
: > "$TMP/state/distill.log"

# ---- scenario 7: message POST fails — _attempts incremented, kept for retry
seed_queue "$TMP/state/honcho-queue.jsonl"
: > "$CURL_STUB_LOG"
out="$(CURL_STUB_MODE=health-ok CURL_STUB_MSG_HTTP=503 bash "$DRAIN" 2>&1)"; rc=$?
ok "msg-fail exits 0" '[ "$rc" = 0 ]'
ok "msg-fail keeps all 3 lines (none succeeded)" '[ "$(wc -l < "$TMP/state/honcho-queue.jsonl")" = 3 ]'
ok "msg-fail incremented _attempts to 1 on every line" 'jq -se "all(.[]; ._attempts == 1)" "$TMP/state/honcho-queue.jsonl" >/dev/null'
ok "msg-fail logs drained ok=0 failed=3" 'grep -q "drained ok=0 failed=3 dropped=0 processed=3" "$TMP/state/distill.log"'
: > "$TMP/state/distill.log"

# ---- scenario 8: partial — first line succeeds, rest fail -----------------
# Build a queue where one line is already at attempts=2 and the rest at 0,
# then stub: line1 succeeds, line2/line3 fail. We expect: line1 drained,
# line2/line3 incremented to 1.
# Note: the queue-drain.sh script POSTs sess-a then sess-b in order; we make
# the stub fail any session id containing "sess-b" or "sess-c" so sess-a
# drains successfully and the others increment.
cat > "$TMP/state/honcho-queue.jsonl" <<'JSONL'
{"session_id":"sess-a","trigger":"manual","distilled_at":"2026-01-01T00:00:00Z","honcho":[{"kind":"context","text":"a","subject":"session"}]}
{"session_id":"sess-b","trigger":"manual","distilled_at":"2026-01-01T00:00:00Z","_attempts":2,"honcho":[{"kind":"context","text":"b","subject":"session"}]}
JSONL
: > "$CURL_STUB_LOG"; : > "$CURL_STUB_IDX_FILE"
rm -rf "$CURL_STUB_BODY_DIR" && mkdir -p "$CURL_STUB_BODY_DIR"
out="$(CURL_STUB_MODE=health-ok CURL_STUB_MSG_HTTP=201 CURL_STUB_FAIL_RE='sess-b|sess-c' bash "$DRAIN" 2>&1)"; rc=$?
ok "partial exits 0" '[ "$rc" = 0 ]'
ok "partial drained 1, failed 1" 'grep -q "drained ok=1 failed=1 dropped=0 processed=2" "$TMP/state/distill.log"'
ok "partial kept failed line at attempts=3 (now at cap)" 'jq -se ".[] | select(.session_id == \"sess-b\") | ._attempts == 3" "$TMP/state/honcho-queue.jsonl" >/dev/null'
ok "partial dropped succeeded line" '[ "$(wc -l < "$TMP/state/honcho-queue.jsonl")" = 1 ]'
: > "$TMP/state/distill.log"

# ---- scenario 9: dead-letter — line already at MAX_ATTEMPTS moves to .dead
cat > "$TMP/state/honcho-queue.jsonl" <<'JSONL'
{"session_id":"sess-dead","trigger":"manual","distilled_at":"2026-01-01T00:00:00Z","_attempts":3,"honcho":[{"kind":"context","text":"x","subject":"session"}]}
JSONL
rm -f "$TMP/state/honcho-queue.jsonl.dead"
: > "$CURL_STUB_LOG"
out="$(CURL_STUB_MODE=health-ok CURL_STUB_MSG_HTTP=201 bash "$DRAIN" 2>&1)"; rc=$?
ok "dead-letter exits 0" '[ "$rc" = 0 ]'
ok "dead-letter moved line to .dead file" '[ -s "$TMP/state/honcho-queue.jsonl.dead" ]'
ok "dead-letter left live queue empty" '[ ! -s "$TMP/state/honcho-queue.jsonl" ]'
ok "dead-letter never POSTed to /messages" '! grep -q "/sessions/sess-dead/messages" "$CURL_STUB_LOG"'
ok "dead-letter logs drained ok=0 failed=0 dropped=1" 'grep -q "drained ok=0 failed=0 dropped=1 processed=1" "$TMP/state/distill.log"'
: > "$TMP/state/distill.log"

# ---- scenario 10: empty facts — dropped quietly, no POST ------------------
cat > "$TMP/state/honcho-queue.jsonl" <<'JSONL'
{"session_id":"sess-empty","trigger":"manual","distilled_at":"2026-01-01T00:00:00Z","honcho":[]}
JSONL
: > "$CURL_STUB_LOG"
out="$(CURL_STUB_MODE=health-ok CURL_STUB_MSG_HTTP=201 bash "$DRAIN" 2>&1)"; rc=$?
ok "empty-facts exits 0" '[ "$rc" = 0 ]'
ok "empty-facts dropped quietly" '[ ! -s "$TMP/state/honcho-queue.jsonl" ]'
ok "empty-facts never POSTed message" '! grep -q "/sessions/sess-empty/messages" "$CURL_STUB_LOG"'
: > "$TMP/state/distill.log"

# ---- scenario 11: MAX_BATCH caps processed lines, rest preserved ---------
# Build 25 lines with CCC_DISTILL_DRAIN_BATCH=2. Expect 2 processed, 23 left.
python3 -c '
import json, sys
for i in range(25):
    print(json.dumps({"session_id": f"sess-{i:02d}", "trigger": "manual",
                      "distilled_at": "2026-01-01T00:00:00Z",
                      "honcho":[{"kind":"context","text":f"f{i}","subject":"session"}]}))
' > "$TMP/state/honcho-queue.jsonl"
: > "$CURL_STUB_LOG"
out="$(CURL_STUB_MODE=health-ok CURL_STUB_MSG_HTTP=201 CCC_DISTILL_DRAIN_BATCH=2 bash "$DRAIN" 2>&1)"; rc=$?
ok "batch-cap exits 0" '[ "$rc" = 0 ]'
ok "batch-cap processed exactly 2" 'grep -q "processed=2" "$TMP/state/distill.log"'
ok "batch-cap preserved 23 remaining lines" '[ "$(wc -l < "$TMP/state/honcho-queue.jsonl")" = 23 ]'
: > "$TMP/state/distill.log"

# ---- scenario 12: auth token handled, never echoed in queue/log -----------
seed_queue "$TMP/state/honcho-queue.jsonl"
: > "$CURL_STUB_LOG"
out="$(CURL_STUB_MODE=health-ok CURL_STUB_MSG_HTTP=201 bash "$DRAIN" 2>&1)"; rc=$?
ok "auth never appears in stdout or honcho-queue.jsonl" '! grep -q "secret-token" <<<"$out" && ! grep -q "secret-token" "$TMP/state/honcho-queue.jsonl" 2>/dev/null'
ok "auth header passed as -H argument" 'grep -q "Authorization: Bearer" "$CURL_STUB_LOG"'
: > "$TMP/state/distill.log"

# ---- scenario 13: node label follows CCC_NODE / node.txt convention -------
seed_queue "$TMP/state/honcho-queue.jsonl"
: > "$CURL_STUB_LOG"; : > "$CURL_STUB_IDX_FILE"
rm -rf "$CURL_STUB_BODY_DIR" && mkdir -p "$CURL_STUB_BODY_DIR"
out="$(CURL_STUB_MODE=health-ok CURL_STUB_MSG_HTTP=201 CCC_NODE=bangtong bash "$DRAIN" 2>&1)"; rc=$?
ok "CCC_NODE=bangtong -> metadata.node == bangtong" 'jq -e ".messages[0].metadata.node == \"bangtong\"" "$CURL_STUB_BODY_DIR/message-1.json" >/dev/null'

printf 'sogyo\n' > "$TMP/state/node.txt"
unset CCC_NODE
: > "$CURL_STUB_LOG"; : > "$CURL_STUB_IDX_FILE"
rm -rf "$CURL_STUB_BODY_DIR" && mkdir -p "$CURL_STUB_BODY_DIR"
seed_queue "$TMP/state/honcho-queue.jsonl"
out="$(CURL_STUB_MODE=health-ok CURL_STUB_MSG_HTTP=201 bash "$DRAIN" 2>&1)"; rc=$?
ok "node.txt=sogyo -> metadata.node == sogyo" 'jq -e ".messages[0].metadata.node == \"sogyo\"" "$CURL_STUB_BODY_DIR/message-1.json" >/dev/null'
rm -f "$TMP/state/node.txt"

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
