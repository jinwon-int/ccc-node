#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$HERE/headless.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

FAKE="$TMP/codex"
cat > "$FAKE" <<'SH'
#!/usr/bin/env bash
set -u
printf '%s\n' "$@" > "$FAKE_CODEX_ARGS"
out=''
prev=''
for arg in "$@"; do
  if [ "$prev" = '--output-last-message' ]; then out="$arg"; fi
  prev="$arg"
done
if [ -n "$out" ] && [ "${FAKE_CODEX_NO_OUT:-0}" != 1 ]; then
  printf 'bounded final result\n' > "$out"
fi
if [ "${FAKE_CODEX_EMPTY_EVENTS:-0}" != 1 ]; then
  printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"fallback result"}}'
fi
SH
chmod +x "$FAKE"
export FAKE_CODEX_ARGS="$TMP/args"

out="$(CCC_CODEX_BIN="$FAKE" CCC_CODEX_SANDBOX=read-only \
  CCC_MODEL=gpt-test CCC_CODEX_REASONING_EFFORT=high \
  CCC_CODEX_WORKDIR="$TMP" bash "$RUNNER" 'inspect safely')"; rc=$?
ok "runner returns final message" '[ "$rc" = 0 ] && [ "$out" = "bounded final result" ]'
ok "runner uses ephemeral JSON execution" 'grep -qx -- "--ephemeral" "$FAKE_CODEX_ARGS" && grep -qx -- "--json" "$FAKE_CODEX_ARGS"'
ok "runner uses explicit read-only sandbox" 'grep -qx -- "read-only" "$FAKE_CODEX_ARGS"'
ok "runner disables interactive approvals" 'grep -qx -- "approval_policy=\"never\"" "$FAKE_CODEX_ARGS"'
ok "runner forwards model and reasoning" 'grep -qx -- "gpt-test" "$FAKE_CODEX_ARGS" && grep -qx -- "model_reasoning_effort=\"high\"" "$FAKE_CODEX_ARGS"'
ok "runner forwards prompt" 'grep -qx -- "inspect safely" "$FAKE_CODEX_ARGS"'

out="$(FAKE_CODEX_NO_OUT=1 CCC_CODEX_BIN="$FAKE" CCC_CODEX_WORKDIR="$TMP" bash "$RUNNER" fallback)"; rc=$?
ok "runner extracts a raw final message from JSONL fallback" '[ "$rc" = 0 ] && [ "$out" = "fallback result" ]'

out="$(FAKE_CODEX_NO_OUT=1 FAKE_CODEX_EMPTY_EVENTS=1 CCC_CODEX_BIN="$FAKE" CCC_CODEX_WORKDIR="$TMP" bash "$RUNNER" empty 2>&1)"; rc=$?
ok "runner fails closed when Codex exits zero without a final message" \
  '[ "$rc" = 1 ] && grep -q "exited successfully without a final message" <<<"$out"'

mkdir -p "$TMP/no-timeout-bin"
before="$(stat -c %Y "$FAKE_CODEX_ARGS")"
out="$(PATH="$TMP/no-timeout-bin" CCC_HEADLESS_TIMEOUT=1 CCC_CODEX_BIN="$FAKE" CCC_CODEX_WORKDIR="$TMP" /bin/bash "$RUNNER" timeout-required 2>&1)"; rc=$?
after="$(stat -c %Y "$FAKE_CODEX_ARGS")"
ok "runner fails closed before invocation when timeout enforcement is unavailable" \
  '[ "$rc" = 127 ] && grep -q "timeout command is required" <<<"$out" && [ "$before" = "$after" ]'

# shellcheck disable=SC2034 # consumed through eval in ok()
before="$(stat -c %Y "$FAKE_CODEX_ARGS")"
# shellcheck disable=SC2034 # consumed through eval in ok()
out="$(CCC_CODEX_BIN="$FAKE" CCC_CODEX_SANDBOX=invalid bash "$RUNNER" nope 2>&1)"
# shellcheck disable=SC2034 # consumed through eval in ok()
rc=$?
# shellcheck disable=SC2034 # consumed through eval in ok()
after="$(stat -c %Y "$FAKE_CODEX_ARGS")"
ok "invalid sandbox fails closed before provider invocation" '[ "$rc" = 2 ] && grep -q "invalid CCC_CODEX_SANDBOX" <<<"$out" && [ "$before" = "$after" ]'

out="$(CCC_CODEX_BIN="$FAKE" CCC_CODEX_REASONING_EFFORT='high\" approval_policy=\"on-request' bash "$RUNNER" nope 2>&1)"; rc=$?
ok "invalid reasoning fails closed before config parsing" '[ "$rc" = 2 ] && grep -q "invalid CCC_CODEX_REASONING_EFFORT" <<<"$out"'

# Regression (#869 sweep / #1076): an unchecked mktemp left OUT/EVENTS/ERR empty
# and the run redirected to empty filenames instead of failing closed.
# shellcheck disable=SC2034 # consumed through eval in ok()
before="$(stat -c %Y "$FAKE_CODEX_ARGS")"
# shellcheck disable=SC2034 # consumed through eval in ok()
out="$(CCC_CODEX_BIN="$FAKE" TMPDIR="$TMP/definitely-not-a-directory" bash "$RUNNER" nope 2>&1)"
# shellcheck disable=SC2034 # consumed through eval in ok()
rc=$?
# shellcheck disable=SC2034 # consumed through eval in ok()
after="$(stat -c %Y "$FAKE_CODEX_ARGS")"
ok "unusable TMPDIR fails closed before provider invocation" '[ "$rc" = 127 ] && grep -q "cannot create scratch file" <<<"$out" && [ "$before" = "$after" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
