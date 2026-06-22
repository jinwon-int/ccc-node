#!/usr/bin/env bash
# Tests for distill/honcho-push.sh — curl is stubbed; no Honcho/network calls.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PUSH="$HERE/honcho-push.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

mkdir -p "$TMP/bin" "$TMP/state"
cat > "$TMP/bin/curl" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >> "${CURL_STUB_LOG:?}"
http="${CURL_STUB_HTTP:-201}"
health_http="${CURL_STUB_HEALTH_HTTP:-204}"
if [ -n "${CURL_STUB_BODY_DIR:-}" ] && [[ "$*" == *"--data-binary @-"* ]]; then
  mkdir -p "$CURL_STUB_BODY_DIR"
  cat > "$CURL_STUB_BODY_DIR/message.json"
fi
# honcho-push has three curl calls: /health probe, ensure-session, and message POST.
case "$*" in
  */health*) printf '%s' "$health_http" ;;
  *__HTTP__*) printf '{"ok":true}\n__HTTP__%s__' "$http" ;;
  *ensure-session*) printf 'ensure-session http=%s\n' "$http" ;;
  *) printf '%s' "$http" ;;
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

PAYLOAD_WITH_FACTS='{"session_id":"sess-1","trigger":"manual","distilled_at":"2026-01-01T00:00:00Z","source_cwd":"/root/project-a","source_project":"-root-project-a","honcho":[{"kind":"context","text":"fact one","subject":"session"}],"wiki_candidates":[]}'
PAYLOAD_NO_FACTS='{"session_id":"sess-empty","trigger":"manual","honcho":[],"wiki_candidates":[]}'

# Force a deterministic node label for assertions. Default resolution is
# CCC_NODE -> state/node.txt -> hostname -s -> 'ccc-node' — we want to verify
# each branch is honored, not depend on the runner's actual hostname.
unset CCC_NODE
printf 'dungae\n' > "$TMP/state/node.txt"

out="$(printf '%s' "$PAYLOAD_WITH_FACTS" | CURL_STUB_HTTP=201 bash "$PUSH" 2>&1)"; rc=$?
ok "success exits 0" '[ "$rc" = 0 ]'
ok "success reports pushed fact count" 'grep -q "honcho push ok http=201 session=sess-1 facts=1" <<<"$out"'
ok "success does not create retry queue" '[ ! -s "$TMP/state/honcho-queue.jsonl" ]'
ok "success called health, ensure-session and messages endpoints" 'grep -q "/health" "$CURL_STUB_LOG" && grep -q "/v3/workspaces/test-ws/sessions" "$CURL_STUB_LOG" && grep -q "/v3/workspaces/test-ws/sessions/sess-1/messages" "$CURL_STUB_LOG"'
ok "success message metadata includes source cwd" 'jq -e ".messages[0].metadata.source_cwd == \"/root/project-a\" and .messages[0].metadata.source_project == \"-root-project-a\"" "$CURL_STUB_BODY_DIR/message.json" >/dev/null'
ok "success message metadata node resolves to node.txt value (dungae) when no CCC_NODE" 'jq -e ".messages[0].metadata.node == \"dungae\"" "$CURL_STUB_BODY_DIR/message.json" >/dev/null'
ok "auth token is passed only as header argument to curl stub, not stdout" '! grep -q "secret-token" <<<"$out"'

# CCC_NODE override — fleet rollout: each node carries its own name in metadata.
# CCC_NODE wins over node.txt and hostname.
: > "$CURL_STUB_LOG"; rm -f "$CURL_STUB_BODY_DIR/message.json"
out="$(printf '%s' "$PAYLOAD_WITH_FACTS" | CCC_NODE=seoseo CURL_STUB_HTTP=201 bash "$PUSH" 2>&1)"; rc=$?
ok "CCC_NODE=seoseo exits 0" '[ "$rc" = 0 ]'
ok "CCC_NODE=seoseo -> metadata.node == seoseo (CCC_NODE wins over node.txt)" 'jq -e ".messages[0].metadata.node == \"seoseo\"" "$CURL_STUB_BODY_DIR/message.json" >/dev/null'

# node.txt fallback when CCC_NODE is unset.
: > "$CURL_STUB_LOG"; rm -f "$CURL_STUB_BODY_DIR/message.json"
printf 'gwakga\n' > "$TMP/state/node.txt"
unset CCC_NODE
out="$(printf '%s' "$PAYLOAD_WITH_FACTS" | CURL_STUB_HTTP=201 bash "$PUSH" 2>&1)"; rc=$?
ok "node.txt=gwakga -> metadata.node == gwakga" 'jq -e ".messages[0].metadata.node == \"gwakga\"" "$CURL_STUB_BODY_DIR/message.json" >/dev/null'

# Restore default for downstream assertions.
printf 'dungae\n' > "$TMP/state/node.txt"

: > "$CURL_STUB_LOG"
out="$(printf '%s' "$PAYLOAD_WITH_FACTS" | CURL_STUB_HEALTH_HTTP=503 CURL_STUB_HTTP=201 bash "$PUSH" 2>&1)"; rc=$?
ok "health failure exits 0" '[ "$rc" = 0 ]'
ok "health failure reports skip" 'grep -q "honcho /health probe failed http=503 session=sess-1 facts=1; skipping push" <<<"$out"'
ok "health failure does not create retry queue" '[ ! -s "$TMP/state/honcho-queue.jsonl" ]'
ok "health failure does not call ensure-session or messages" 'grep -q "/health" "$CURL_STUB_LOG" && ! grep -q "/v3/workspaces/test-ws/sessions" "$CURL_STUB_LOG"'

: > "$CURL_STUB_LOG"
out="$(printf '%s' "$PAYLOAD_WITH_FACTS" | CURL_STUB_HEALTH_HTTP=204 CURL_STUB_HTTP=503 bash "$PUSH" 2>&1)"; rc=$?
ok "failed push exits non-zero" '[ "$rc" = 1 ]'
ok "failed push reports http code" 'grep -q "honcho push failed http=503 session=sess-1 facts=1" <<<"$out"'
ok "failed push appends retry queue" '[ "$(wc -l < "$TMP/state/honcho-queue.jsonl")" = 1 ] && jq -e ".session_id == \"sess-1\"" "$TMP/state/honcho-queue.jsonl" >/dev/null'

before="$(find "$TMP/state" -type f -printf '%P %s\n' | sort)"
out="$(printf '%s' "$PAYLOAD_NO_FACTS" | CURL_STUB_HTTP=201 bash "$PUSH" 2>&1)"; rc=$?
after="$(find "$TMP/state" -type f -printf '%P %s\n' | sort)"
ok "empty honcho facts exits 0" '[ "$rc" = 0 ] && grep -q "no honcho facts to push" <<<"$out"'
ok "empty honcho facts performs no new queue write" '[ "$before" = "$after" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
