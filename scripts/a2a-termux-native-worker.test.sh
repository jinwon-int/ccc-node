#!/usr/bin/env bash
# Tests for the Termux native A2A worker env checker/launcher.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL="$ROOT/scripts/a2a-termux-native-worker.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

mkdir -p "$TMP/bin" "$TMP/worker/dist" "$TMP/worker/scripts"
printf '#!/usr/bin/env bash\necho native-node "$@"\n' > "$TMP/bin/node-native"
printf '#!/usr/bin/env bash\necho claude "$@"\n' > "$TMP/bin/claude-native"
printf 'console.log("worker fixture");\n' > "$TMP/worker/dist/worker.js"
printf 'console.log("bridge fixture");\n' > "$TMP/worker/scripts/claude-a2a-analysis-bridge.mjs"
chmod +x "$TMP/bin/node-native" "$TMP/bin/claude-native"

write_env() {
  cat > "$1" <<EOF
A2A_TERMUX_NATIVE=1
A2A_NATIVE_NODE_BIN=$TMP/bin/node-native
A2A_WORKER_ROOT=$TMP/worker
A2A_CLAUDE_CODE_BIN=$TMP/bin/claude-native
OPENCLAW_BIN=$TMP/worker/scripts/claude-a2a-analysis-bridge.mjs
A2A_OPENCLAW_ANALYSIS_BIN=$TMP/worker/scripts/claude-a2a-analysis-bridge.mjs
BROKER_URL=http://127.0.0.1:18790
WORKER_MODE=persistent
WORKER_METADATA_JSON={"runtime":"claude-code","harness":"claude","adapter":"claude-a2a-analysis-bridge","nodeId":"mobile-native"}
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
DISABLE_GROWTHBOOK=1
USE_BUILTIN_RIPGREP=0
EOF
}

good="$TMP/good.env"
write_env "$good"
out="$(bash "$TOOL" check --env-file "$good" 2>&1)"; rc=$?
ok "valid native worker env passes" '[ "$rc" = 0 ] && grep -q "safe to launch" <<<"$out" && grep -q "adapter=claude-a2a-analysis-bridge" <<<"$out"'

out="$(bash "$TOOL" print-command --env-file "$good" 2>&1)"; rc=$?
ok "print-command renders native node worker.js" '[ "$rc" = 0 ] && grep -q "$TMP/bin/node-native" <<<"$out" && grep -q "$TMP/worker/dist/worker.js" <<<"$out"'

bad_broker="$TMP/bad-broker.env"
write_env "$bad_broker"
python3 - "$bad_broker" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read().replace('BROKER_URL=http://127.0.0.1:18790', 'BROKER_URL=https://broker.example.invalid:8787')
open(p, 'w', encoding='utf-8').write(s)
PY
out="$(bash "$TOOL" check --env-file "$bad_broker" 2>&1)"; rc=$?
ok "remote broker URL fails closed" '[ "$rc" = 2 ] && grep -q "local Termux tunnel" <<<"$out"'

bad_meta="$TMP/bad-meta.env"
write_env "$bad_meta"
python3 - "$bad_meta" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read().replace('"adapter":"claude-a2a-analysis-bridge"', '"adapter":"other"')
open(p, 'w', encoding='utf-8').write(s)
PY
out="$(bash "$TOOL" check --env-file "$bad_meta" 2>&1)"; rc=$?
ok "wrong adapter metadata fails closed" '[ "$rc" = 2 ] && grep -q "adapter" <<<"$out"'

bad_context="$TMP/bad-context.env"
write_env "$bad_context"
printf 'context fixture\n' > "$TMP/worker/scripts/USER.md"
python3 - "$bad_context" "$TMP/bin/claude-native" "$TMP/worker/scripts/USER.md" <<'PY'
import sys
p, native_claude, user_md = sys.argv[1:]
s = open(p, encoding='utf-8').read().replace(
    'A2A_CLAUDE_CODE_BIN=' + native_claude,
    'A2A_CLAUDE_CODE_BIN=' + user_md,
)
open(p, 'w', encoding='utf-8').write(s)
PY
out="$(bash "$TOOL" check --env-file "$bad_context" 2>&1)"; rc=$?
ok "OpenClaw context path fails closed" '[ "$rc" = 2 ] && grep -q "forbidden OpenClaw" <<<"$out"'

bad_native="$TMP/bad-native.env"
write_env "$bad_native"
python3 - "$bad_native" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read().replace('A2A_TERMUX_NATIVE=1', 'A2A_TERMUX_NATIVE=0')
open(p, 'w', encoding='utf-8').write(s)
PY
out="$(bash "$TOOL" check --env-file "$bad_native" 2>&1)"; rc=$?
ok "non-native marker fails closed" '[ "$rc" = 2 ] && grep -q "A2A_TERMUX_NATIVE" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
