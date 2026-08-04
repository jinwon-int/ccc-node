#!/usr/bin/env bash
# crush/headless.test.sh — fake-crush arg-capture tests for ccc-crush-headless.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$HERE/headless.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

cat > "$TMP/crushrc.fake" <<'EOF'
# fake config for tests
EOF

FAKE="$TMP/crush"
cat > "$FAKE" <<'SH'
#!/usr/bin/env bash
set -u
printf '%s\n' "$@" > "$FAKE_CRUSH_ARGS"
env | grep -E '^CRUSH_(GLOBAL_CONFIG|DISABLE_METRICS)=' > "$FAKE_CRUSH_ENV"
printf 'bounded final result\n'
SH
chmod +x "$FAKE"
export FAKE_CRUSH_ARGS="$TMP/args" FAKE_CRUSH_ENV="$TMP/env"

out="$(CCC_CRUSH_BIN="$FAKE" CCC_CRUSH_MODEL=kimi/k3 CCC_CRUSH_WORKDIR="$TMP" \
  CCC_CRUSH_CONFIG="$TMP/crushrc.fake" bash "$RUNNER" 'inspect safely')"; rc=$?
ok "runner exits zero and passes output through" '[ "$rc" = 0 ] && [ "$out" = "bounded final result" ]'
ok "runner forces non-interactive quiet run" 'grep -qx "run" "$FAKE_CRUSH_ARGS" && grep -qx -- "-q" "$FAKE_CRUSH_ARGS"'
ok "runner forwards provider/model" 'grep -qx "kimi/k3" "$FAKE_CRUSH_ARGS"'
ok "runner forwards workdir" 'grep -qx "$TMP" "$FAKE_CRUSH_ARGS"'
ok "runner forwards prompt last" '[ "$(tail -1 "$FAKE_CRUSH_ARGS")" = "inspect safely" ]'
ok "runner pins global config and metrics opt-out" 'grep -qx "CRUSH_GLOBAL_CONFIG=$TMP/crushrc.fake" "$FAKE_CRUSH_ENV" && grep -qx "CRUSH_DISABLE_METRICS=1" "$FAKE_CRUSH_ENV"'

# ambient 환경의 CCC_CRUSH_MODEL이 새어 들어와도 "missing model"이 성립하도록 밀폐
out="$(env -u CCC_CRUSH_MODEL CCC_CRUSH_BIN="$FAKE" CCC_CRUSH_WORKDIR="$TMP" CCC_CRUSH_CONFIG="$TMP/crushrc.fake" bash "$RUNNER" nope 2>&1)"; rc=$?
ok "missing model fails closed before invocation" '[ "$rc" = 2 ] && grep -q "no model set" <<<"$out"'

out="$(CCC_CRUSH_BIN="$FAKE" CCC_CRUSH_MODEL=bare-model CCC_CRUSH_WORKDIR="$TMP" CCC_CRUSH_CONFIG="$TMP/crushrc.fake" bash "$RUNNER" nope 2>&1)"; rc=$?
ok "bare model without provider prefix fails closed" '[ "$rc" = 2 ] && grep -q "provider/model" <<<"$out"'

# shellcheck disable=SC2034 # consumed through eval in ok()
out="$(CCC_CRUSH_BIN="$FAKE" CCC_CRUSH_MODEL=kimi/k3 CCC_CRUSH_WORKDIR="$TMP" CCC_CRUSH_CONFIG="$TMP/missing" bash "$RUNNER" nope 2>&1)"
# shellcheck disable=SC2034 # consumed through eval in ok()
rc=$?
ok "missing config fails closed" '[ "$rc" = 2 ] && grep -q "config missing" <<<"$out"'

ok "missing binary exits 127" 'CCC_CRUSH_BIN="$TMP/no-such-bin" CCC_CRUSH_MODEL=kimi/k3 CCC_CRUSH_WORKDIR="$TMP" CCC_CRUSH_CONFIG="$TMP/crushrc.fake" bash "$RUNNER" nope >/dev/null 2>&1; [ $? = 127 ]'

# shellcheck disable=SC2034 # consumed through eval in ok()
before_env="$([ -f "$FAKE_CRUSH_ENV" ] && cat "$FAKE_CRUSH_ENV" || true)"
# shellcheck disable=SC2034 # consumed through eval in ok()
out="$(CCC_CRUSH_BIN="$FAKE" CCC_CRUSH_MODEL=kimi/k3 CCC_CRUSH_WORKDIR="$TMP" CCC_CRUSH_CONFIG="$TMP/missing" bash "$RUNNER" nope 2>&1)"
# shellcheck disable=SC2034 # consumed through eval in ok()
after_env="$(cat "$FAKE_CRUSH_ENV")"
ok "failed validation never invokes provider" '[ "$before_env" = "$after_env" ]'

echo "pass=$pass fail=$fail"
[ "$fail" = 0 ]
