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
# crush 가 실제로 읽는 모양으로 설정이 왔는지 증거를 남긴다. 러너가 종료 시
# 임시 디렉터리를 지우므로, 살아 있는 동안 여기서 확인해야 한다.
{
  [ -d "$CRUSH_GLOBAL_CONFIG" ] && echo "DIR=yes" || echo "DIR=no"
  [ -f "$CRUSH_GLOBAL_CONFIG/crushrc" ] && echo "CRUSHRC=yes" || echo "CRUSHRC=no"
  echo "---"
  cat "$CRUSH_GLOBAL_CONFIG/crushrc" 2>/dev/null
} > "$FAKE_CRUSH_CFG"
printf 'bounded final result\n'
SH
chmod +x "$FAKE"
export FAKE_CRUSH_ARGS="$TMP/args" FAKE_CRUSH_ENV="$TMP/env" FAKE_CRUSH_CFG="$TMP/cfg"

out="$(CCC_CRUSH_BIN="$FAKE" CCC_CRUSH_MODEL=kimi/k3 CCC_CRUSH_WORKDIR="$TMP" \
  CCC_CRUSH_CONFIG="$TMP/crushrc.fake" bash "$RUNNER" 'inspect safely')"; rc=$?
ok "runner exits zero and passes output through" '[ "$rc" = 0 ] && [ "$out" = "bounded final result" ]'
ok "runner forces non-interactive quiet run" 'grep -qx "run" "$FAKE_CRUSH_ARGS" && grep -qx -- "-q" "$FAKE_CRUSH_ARGS"'
ok "runner forwards provider/model" 'grep -qx "kimi/k3" "$FAKE_CRUSH_ARGS"'
ok "runner forwards workdir" 'grep -qx "$TMP" "$FAKE_CRUSH_ARGS"'
ok "runner forwards prompt last" '[ "$(tail -1 "$FAKE_CRUSH_ARGS")" = "inspect safely" ]'
ok "runner pins metrics opt-out" 'grep -qx "CRUSH_DISABLE_METRICS=1" "$FAKE_CRUSH_ENV"'

# crush v0.88.0 은 CRUSH_GLOBAL_CONFIG 를 디렉터리로 보고 그 안의 crush.json /
# crushrc 를 읽는다. 파일을 직접 가리키면 모든 실행이 config 로드에서 죽는다
# ("not a directory", #936). 그래서 "디렉터리인가"와 "그 안에 운영자 설정이
# 그대로 들어갔는가"를 함께 못박는다. 경로만 보면 회귀를 놓친다.
ok "runner hands crush a config directory, not the config file" \
  'grep -q "^CRUSH_GLOBAL_CONFIG=" "$FAKE_CRUSH_ENV" && ! grep -qx "CRUSH_GLOBAL_CONFIG=$TMP/crushrc.fake" "$FAKE_CRUSH_ENV"'
ok "config directory holds the operator config as crushrc" \
  'grep -qx "DIR=yes" "$FAKE_CRUSH_CFG" && grep -qx "CRUSHRC=yes" "$FAKE_CRUSH_CFG" && grep -qx "# fake config for tests" "$FAKE_CRUSH_CFG"'

# 임시 설정 디렉터리는 실행 후 남으면 안 된다 — 설정에 키 확장이 들어가므로
# 유출 표면이 된다.
# shellcheck disable=SC2034 # consumed through eval in ok()
cfgdir="$(sed -n 's/^CRUSH_GLOBAL_CONFIG=//p' "$FAKE_CRUSH_ENV")"
ok "runner removes the temp config directory on exit" '[ -n "$cfgdir" ] && [ ! -e "$cfgdir" ]'

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
