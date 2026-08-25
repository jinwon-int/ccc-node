#!/usr/bin/env bash
# Tests for the Termux native A2A worker env checker/launcher.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL="$ROOT/scripts/a2a-termux-native-worker.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

mkdir -p "$TMP/bin" "$TMP/worker/dist" "$TMP/worker/scripts"
printf '#!/usr/bin/env bash\necho native-node "$@"\n' > "$TMP/bin/node-native"
printf '#!/usr/bin/env bash\necho claude "$@"\n' > "$TMP/bin/claude-native"
printf '#!/usr/bin/env bash\necho codex "$@"\n' > "$TMP/bin/codex-native"
printf 'console.log("worker fixture");\n' > "$TMP/worker/dist/worker.js"
printf 'console.log("bridge fixture");\n' > "$TMP/worker/scripts/claude-a2a-analysis-bridge.mjs"
printf 'console.log("patch bridge fixture");\n' > "$TMP/worker/scripts/claude-a2a-patch-bridge.mjs"
printf 'console.log("codex bridge fixture");\n' > "$TMP/worker/scripts/codex-a2a-analysis-bridge.mjs"
printf 'console.log("piri bridge fixture");\n' > "$TMP/worker/scripts/piri-a2a-analysis-bridge.mjs"
printf 'console.log("task handler fixture");\n' > "$TMP/worker/scripts/a2a-task-handler.mjs"
chmod +x "$TMP/bin/node-native" "$TMP/bin/claude-native" "$TMP/bin/codex-native"
printf '#!/usr/bin/env bash\necho piri "$@"\n' > "$TMP/bin/piri-cli"
chmod +x "$TMP/bin/piri-cli"

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

# Patch-bridge (a2a-nexus #1021) env: intent-aware superset wired as OPENCLAW_BIN,
# with adapter set to claude-a2a-patch-bridge and optional single-shot opt-in.
write_patch_env() {
  cat > "$1" <<EOF
A2A_TERMUX_NATIVE=1
A2A_NATIVE_NODE_BIN=$TMP/bin/node-native
A2A_WORKER_ROOT=$TMP/worker
A2A_CLAUDE_CODE_BIN=$TMP/bin/claude-native
OPENCLAW_BIN=$TMP/worker/scripts/claude-a2a-patch-bridge.mjs
A2A_OPENCLAW_ANALYSIS_BIN=$TMP/worker/scripts/claude-a2a-patch-bridge.mjs
A2A_CLAUDE_CODE_PATCH_MODE=single-shot
BROKER_URL=http://127.0.0.1:18790
WORKER_MODE=persistent
WORKER_METADATA_JSON={"runtime":"claude-code","harness":"claude","adapter":"claude-a2a-patch-bridge","nodeId":"mobile-native"}
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
DISABLE_GROWTHBOOK=1
USE_BUILTIN_RIPGREP=0
EOF
}

write_codex_env() {
  mkdir -p "$TMP/codex-dir"
  printf '{"test":true}\n' > "$TMP/codex-dir/auth.json"
  cat > "$1" <<EOF
A2A_TERMUX_NATIVE=1
A2A_NATIVE_NODE_BIN=$TMP/bin/node-native
A2A_WORKER_ROOT=$TMP/worker
OPENCLAW_BIN=$TMP/worker/scripts/codex-a2a-analysis-bridge.mjs
A2A_OPENCLAW_ANALYSIS_BIN=$TMP/worker/scripts/codex-a2a-analysis-bridge.mjs
A2A_CODEX_BIN=$TMP/bin/codex-native
A2A_CODEX_ANALYSIS_CONFIG_DIR=$TMP/codex-dir
A2A_CODEX_MODEL=gpt-5.6-sol
A2A_CODEX_REASONING_EFFORT=xhigh
BROKER_URL=http://127.0.0.1:18790
WORKER_MODE=persistent
WORKER_METADATA_JSON={"runtime":"codex","harness":"codex","adapter":"codex-a2a-analysis-bridge","nodeId":"mobile-native"}
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

# The launcher derives the worker.js external-handler wiring (WORKER_HANDLER_*)
# from OPENCLAW_BIN so newer worker.js builds run the real bridge, not the silent
# echo builtin. check surfaces it and analysisBridge=enabled.
out="$(bash "$TOOL" check --env-file "$good" 2>&1)"; rc=$?
ok "check derives task-handler wiring" '[ "$rc" = 0 ] && grep -q "taskHandler=$TMP/bin/node-native $TMP/worker/scripts/a2a-task-handler.mjs" <<<"$out" && grep -q "analysisBridge=enabled" <<<"$out"'

# An explicit WORKER_HANDLER_COMMAND in the env file wins over the derived value.
override="$TMP/override.env"
write_env "$override"
printf 'WORKER_HANDLER_COMMAND=/custom/node\n' >> "$override"
out="$(bash "$TOOL" check --env-file "$override" 2>&1)"; rc=$?
ok "explicit WORKER_HANDLER_COMMAND override respected" '[ "$rc" = 0 ] && grep -q "taskHandler=/custom/node" <<<"$out"'

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

# --- patch bridge (a2a-nexus #1021) drop-in cases ---
patch_good="$TMP/patch-good.env"
write_patch_env "$patch_good"
out="$(bash "$TOOL" check --env-file "$patch_good" 2>&1)"; rc=$?
ok "patch bridge + single-shot passes" '[ "$rc" = 0 ] && grep -q "safe to launch" <<<"$out" && grep -q "adapter=claude-a2a-patch-bridge" <<<"$out"'

write_piri_env() {
  cat > "$1" <<EOF
A2A_TERMUX_NATIVE=1
A2A_NATIVE_NODE_BIN=$TMP/bin/node-native
A2A_WORKER_ROOT=$TMP/worker
OPENCLAW_BIN=$TMP/worker/scripts/piri-a2a-analysis-bridge.mjs
A2A_OPENCLAW_ANALYSIS_BIN=$TMP/worker/scripts/piri-a2a-analysis-bridge.mjs
A2A_PIRI_CLI=$TMP/bin/piri-cli
A2A_PIRI_EXEC=native
A2A_PIRI_MODEL=zai/glm-5.3
A2A_PIRI_THINKING=high
BROKER_URL=http://127.0.0.1:18790
WORKER_MODE=persistent
WORKER_METADATA_JSON={"runtime":"piri","harness":"piri","adapter":"piri-a2a-analysis-bridge","nodeId":"mobile-native"}
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
DISABLE_GROWTHBOOK=1
USE_BUILTIN_RIPGREP=0
EOF
}

codex_good="$TMP/codex-good.env"
write_codex_env "$codex_good"
out="$(bash "$TOOL" check --env-file "$codex_good" 2>&1)"; rc=$?
ok "Codex analysis bridge passes with Codex runtime metadata" '[ "$rc" = 0 ] && grep -q "safe to launch" <<<"$out" && grep -q "runtime=codex,harness=codex,adapter=codex-a2a-analysis-bridge" <<<"$out"'

piri_good="$TMP/piri-good.env"
write_piri_env "$piri_good"
out="$(bash "$TOOL" check --env-file "$piri_good" 2>&1)"; rc=$?
ok "Piri analysis bridge passes with Piri runtime metadata" '[ "$rc" = 0 ] && grep -q "safe to launch" <<<"$out" && grep -q "runtime=piri,harness=piri,adapter=piri-a2a-analysis-bridge" <<<"$out"'

piri_missing_cli="$TMP/piri-missing-cli.env"
write_piri_env "$piri_missing_cli"
sed -i '/^A2A_PIRI_CLI=/d' "$piri_missing_cli"
out="$(bash "$TOOL" check --env-file "$piri_missing_cli" 2>&1)"; rc=$?
ok "Piri bridge without A2A_PIRI_CLI fails closed" '[ "$rc" = 2 ] && grep -q "A2A_PIRI_CLI" <<<"$out"'

codex_missing_bin="$TMP/codex-missing-bin.env"
write_codex_env "$codex_missing_bin"
sed -i '/^A2A_CODEX_BIN=/d' "$codex_missing_bin"
out="$(bash "$TOOL" check --env-file "$codex_missing_bin" 2>&1)"; rc=$?
ok "Codex bridge without A2A_CODEX_BIN fails closed" '[ "$rc" = 2 ] && grep -q "A2A_CODEX_BIN" <<<"$out"'

# single-shot opt-in requires the patch bridge, not the analysis bridge.
patch_on_analysis="$TMP/patch-on-analysis.env"
write_env "$patch_on_analysis"
printf 'A2A_CLAUDE_CODE_PATCH_MODE=single-shot\n' >> "$patch_on_analysis"
out="$(bash "$TOOL" check --env-file "$patch_on_analysis" 2>&1)"; rc=$?
ok "patch mode on analysis bridge fails closed" '[ "$rc" = 2 ] && grep -q "claude-a2a-patch-bridge.mjs" <<<"$out"'

# unrecognized patch-mode value fails closed.
patch_badmode="$TMP/patch-badmode.env"
write_patch_env "$patch_badmode"
python3 - "$patch_badmode" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read().replace('A2A_CLAUDE_CODE_PATCH_MODE=single-shot', 'A2A_CLAUDE_CODE_PATCH_MODE=turbo')
open(p, 'w', encoding='utf-8').write(s)
PY
out="$(bash "$TOOL" check --env-file "$patch_badmode" 2>&1)"; rc=$?
ok "bad patch-mode value fails closed" '[ "$rc" = 2 ] && grep -q "A2A_CLAUDE_CODE_PATCH_MODE" <<<"$out"'

# adapter must match the wired bridge: patch bridge with analysis adapter fails.
patch_mismatch="$TMP/patch-mismatch.env"
write_patch_env "$patch_mismatch"
python3 - "$patch_mismatch" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read().replace('"adapter":"claude-a2a-patch-bridge"', '"adapter":"claude-a2a-analysis-bridge"')
open(p, 'w', encoding='utf-8').write(s)
PY
out="$(bash "$TOOL" check --env-file "$patch_mismatch" 2>&1)"; rc=$?
ok "adapter/bridge mismatch fails closed" '[ "$rc" = 2 ] && grep -q "adapter" <<<"$out"'

# --- hardening: executable bins, worker-script containment, clean exec failure ---

# The exec'd native Node wrapper must be executable; a non-+x bin fails at check
# time (not later at exec).
non_exec_node="$TMP/non-exec-node.env"
write_env "$non_exec_node"
cp "$TMP/bin/node-native" "$TMP/bin/node-native-noexec"
chmod -x "$TMP/bin/node-native-noexec"
python3 - "$non_exec_node" "$TMP/bin/node-native" "$TMP/bin/node-native-noexec" <<'PY'
import sys
p, old, new = sys.argv[1:]
s = open(p, encoding='utf-8').read().replace('A2A_NATIVE_NODE_BIN=' + old, 'A2A_NATIVE_NODE_BIN=' + new)
open(p, 'w', encoding='utf-8').write(s)
PY
out="$(bash "$TOOL" check --env-file "$non_exec_node" 2>&1)"; rc=$?
ok "non-executable native node bin fails closed" '[ "$rc" = 2 ] && grep -q "must be executable" <<<"$out"'

# A worker.js override outside A2A_WORKER_ROOT is rejected (path-escape).
escape_worker="$TMP/escape-worker.env"
write_env "$escape_worker"
mkdir -p "$TMP/elsewhere"
printf 'console.log("rogue");\n' > "$TMP/elsewhere/worker.js"
printf 'A2A_WORKER_SCRIPT=%s\n' "$TMP/elsewhere/worker.js" >> "$escape_worker"
out="$(bash "$TOOL" check --env-file "$escape_worker" 2>&1)"; rc=$?
ok "worker script outside A2A_WORKER_ROOT fails closed" '[ "$rc" = 2 ] && grep -q "must live under A2A_WORKER_ROOT" <<<"$out"'

# A worker.js override INSIDE the root is still accepted.
inside_worker="$TMP/inside-worker.env"
write_env "$inside_worker"
mkdir -p "$TMP/worker/alt"
printf 'console.log("alt");\n' > "$TMP/worker/alt/worker.js"
printf 'A2A_WORKER_SCRIPT=%s\n' "$TMP/worker/alt/worker.js" >> "$inside_worker"
out="$(bash "$TOOL" check --env-file "$inside_worker" 2>&1)"; rc=$?
ok "worker script override inside the root is accepted" '[ "$rc" = 0 ] && grep -q "safe to launch" <<<"$out"'

# The versioned task handler is mandatory: without it worker.js silently runs its
# echo builtin, so a missing handler must fail closed at check time. Use a
# separate worker root that lacks a2a-task-handler.mjs.
nohandler="$TMP/nohandler.env"
mkdir -p "$TMP/worker-nh/dist" "$TMP/worker-nh/scripts"
printf 'console.log("worker fixture");\n' > "$TMP/worker-nh/dist/worker.js"
printf 'console.log("bridge fixture");\n' > "$TMP/worker-nh/scripts/claude-a2a-analysis-bridge.mjs"
cat > "$nohandler" <<EOF
A2A_TERMUX_NATIVE=1
A2A_NATIVE_NODE_BIN=$TMP/bin/node-native
A2A_WORKER_ROOT=$TMP/worker-nh
A2A_CLAUDE_CODE_BIN=$TMP/bin/claude-native
OPENCLAW_BIN=$TMP/worker-nh/scripts/claude-a2a-analysis-bridge.mjs
A2A_OPENCLAW_ANALYSIS_BIN=$TMP/worker-nh/scripts/claude-a2a-analysis-bridge.mjs
BROKER_URL=http://127.0.0.1:18790
WORKER_MODE=persistent
WORKER_METADATA_JSON={"runtime":"claude-code","harness":"claude","adapter":"claude-a2a-analysis-bridge","nodeId":"mobile-native"}
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
DISABLE_GROWTHBOOK=1
USE_BUILTIN_RIPGREP=0
EOF
out="$(bash "$TOOL" check --env-file "$nohandler" 2>&1)"; rc=$?
ok "missing task handler fails closed" '[ "$rc" = 2 ] && grep -q "task handler must exist" <<<"$out"'

# A failed exec stays fail-closed (clean error, exit 2) rather than a raw traceback.
# A directory as the node bin makes execve raise OSError; a forbidden-context name
# would trip earlier, so use a plain dir the validator accepts as a file? Instead,
# point the node bin at a non-ELF executable that execve refuses (ENOEXEC) — a
# script without a shebang, marked +x.
exec_fail="$TMP/exec-fail.env"
write_env "$exec_fail"
printf '\xff\xfenot a valid executable\n' > "$TMP/bin/bad-node"
chmod +x "$TMP/bin/bad-node"
python3 - "$exec_fail" "$TMP/bin/node-native" "$TMP/bin/bad-node" <<'PY'
import sys
p, old, new = sys.argv[1:]
s = open(p, encoding='utf-8').read().replace('A2A_NATIVE_NODE_BIN=' + old, 'A2A_NATIVE_NODE_BIN=' + new)
open(p, 'w', encoding='utf-8').write(s)
PY
out="$(bash "$TOOL" run --env-file "$exec_fail" 2>&1)"; rc=$?
ok "failed exec stays fail-closed (no traceback)" '[ "$rc" = 2 ] && grep -q "failed to exec native worker" <<<"$out" && ! grep -q "Traceback" <<<"$out"'

# -----------------------------------------------------------------------------
# Supervisor subcommands (supervise / stop / status)
# -----------------------------------------------------------------------------
# These absorb what used to live in a2a-termux-native-worker-supervisor.test.sh
# (deleted in the same PR).  The supervisor half is now dispatched by this
# script's own main(), and we mock the underlying Python worker via
# A2A_PYTHON_HARNESS + a bash executable stub so the tests need no real
# python3, ssh, or curl on the network side.

SUP_TMP="$(ccc_test_tmpdir)" || exit 1
# NB: don't cleanup SUP_TMP inside a nested EXIT trap — the outer trap already
# covers $TMP, and we bind SUP_TMP to $TMP so cleanup piggy-backs.
mv "$SUP_TMP" "$TMP/sup" && SUP_TMP="$TMP/sup"

# Mock Python harness: `check` always OK, `run` sleeps so the supervisor's
# worker loop has something to wait on.  Same shape as the real Python file:
# executable with a shebang, so the shell dispatcher (which calls it directly)
# doesn't care whether it's actually Python.
cat > "$SUP_TMP/mock-python-harness.sh" <<'MOCK_PY_EOF'
#!/usr/bin/env bash
case "${1:-}" in
    check) exit 0 ;;
    run)
        echo "MOCK_RUN args=$*"
        exec sleep 3600
        ;;
    *) echo "mock python harness: unknown $1" >&2; exit 2 ;;
esac
MOCK_PY_EOF
chmod +x "$SUP_TMP/mock-python-harness.sh"

# A second mock that fails `check` — used to verify supervise aborts cleanly.
cat > "$SUP_TMP/mock-python-harness-fail.sh" <<'MOCK_PYF_EOF'
#!/usr/bin/env bash
echo "MOCK_PY: forcing check failure" >&2
exit 2
MOCK_PYF_EOF
chmod +x "$SUP_TMP/mock-python-harness-fail.sh"

# Mock ssh that blocks so we can inspect the tunnel loop without real network
# I/O.  Touches a marker file so tests can wait for it to launch.
mkdir -p "$SUP_TMP/bin"
cat > "$SUP_TMP/bin/ssh" <<'SSH_EOF'
#!/usr/bin/env bash
touch "${A2A_TEST_SSH_MARKER:-/tmp/a2a-test-ssh}"
exec sleep 3600
SSH_EOF
chmod +x "$SUP_TMP/bin/ssh"

# Mock curl so `status`'s tunnel probe is deterministic.
cat > "$SUP_TMP/bin/curl" <<'CURL_EOF'
#!/usr/bin/env bash
if [[ "${A2A_TEST_CURL_HTTP_ERROR:-0}" == "1" ]]; then
    [[ " $* " == *" -f"* ]] && exit 22
    exit 0
fi
if [[ "${A2A_TEST_CURL_OK:-0}" == "1" ]]; then
    exit 0
fi
exit 7
CURL_EOF
chmod +x "$SUP_TMP/bin/curl"

# Minimal env file the mock Python accepts.  We only need the tunnel target
# key for supervise; other keys are irrelevant because `check` is mocked.
ENVF="$SUP_TMP/canonical.env"
cat > "$ENVF" <<EOF
A2A_TUNNEL_SSH_TARGET=fake-target
A2A_WORKER_ROOT=$SUP_TMP/worker-root
EOF
mkdir -p "$SUP_TMP/worker-root/dist"

# Isolate lock / log paths so we never touch \$HOME/.a2a or \$HOME/.hermes/logs.
export A2A_SUPERVISOR_LOCK_DIR="$SUP_TMP"
export A2A_SUPERVISOR_LOG_DIR="$SUP_TMP"
export A2A_SUPERVISOR_LOCK="$SUP_TMP/sup.lock"
export A2A_SUPERVISOR_LOG="$SUP_TMP/sup.log"
export A2A_TEST_SSH_MARKER="$SUP_TMP/ssh-started"
export A2A_TEST_CURL_OK=0
export A2A_PYTHON_HARNESS="$SUP_TMP/mock-python-harness.sh"
export PATH="$SUP_TMP/bin:$PATH"
SUP_PIDFILE="$A2A_SUPERVISOR_LOCK.pid"

# ---- read-only paths first (fast, no side effects) ----

out="$(bash "$TOOL" 2>&1)"; rc=$?
ok "no command prints usage rc=2" '[ "$rc" = 2 ] && grep -q "Usage:" <<<"$out"'

out="$(bash "$TOOL" --help 2>&1)"; rc=$?
ok "--help prints usage rc=2" '[ "$rc" = 2 ] && grep -q "supervise" <<<"$out"'

out="$(bash "$TOOL" supervise 2>&1)"; rc=$?
ok "supervise without --env-file fails" '[ "$rc" = 2 ] && grep -q -- "--env-file required" <<<"$out"'

out="$(bash "$TOOL" bogus --env-file "$ENVF" 2>&1)"; rc=$?
ok "unknown command exits nonzero" '[ "$rc" = 2 ] && grep -q "unknown command" <<<"$out"'

# stop / status with no supervisor running.
out="$(bash "$TOOL" stop 2>&1)"; rc=$?
ok "stop with no supervisor is a no-op" '[ "$rc" = 0 ] && grep -q "no supervisor" <<<"$out"'

out="$(bash "$TOOL" status 2>&1)"; rc=$?
ok "status with no supervisor reports none" '[ "$rc" = 0 ] && grep -q "supervisor: none" <<<"$out"'
ok "status reports tunnel DOWN when curl fails" 'grep -q "tunnel: DOWN" <<<"$out"'

out="$(A2A_TEST_CURL_HTTP_ERROR=1 bash "$TOOL" status 2>&1)"
ok "status treats an HTTP error response as tunnel DOWN" \
    'grep -q "tunnel: DOWN" <<<"$out"'

# A live, unrelated process with a correct numeric PID/start token is still not
# a canonical supervisor and must never be signaled by stop.
sleep 3600 >/dev/null 2>&1 &
STALE_PID=$!
STALE_TOKEN=$(bash -c 'source "$1"; a2a_process_start_token "$2"' _ \
    "$ROOT/scripts/lib/a2a-supervisor-identity.sh" "$STALE_PID")
printf '%s %s\n' "$STALE_PID" "$STALE_TOKEN" > "$SUP_PIDFILE"
out="$(bash "$TOOL" stop 2>&1)"; rc=$?
ok "stop ignores a reused PID owned by an unrelated live process" \
    '[ "$rc" = 0 ] && kill -0 "$STALE_PID" 2>/dev/null && grep -q "no supervisor" <<<"$out"'
kill -KILL "$STALE_PID" 2>/dev/null || true
wait "$STALE_PID" 2>/dev/null || true
rm -f "$SUP_PIDFILE"

# A bash process can also place the canonical path and `supervise` later in
# argv without using the supervisor grammar.  A legacy one-field PID record
# must not turn that argv spoof into a kill target.
bash -c 'while :; do :; done' "$TOOL" supervise >/dev/null 2>&1 &
SPOOF_PID=$!
printf '%s\n' "$SPOOF_PID" > "$SUP_PIDFILE"
out="$(bash "$TOOL" stop 2>&1)"; rc=$?
ok "stop rejects misplaced supervisor argv from a legacy PID file" \
    '[ "$rc" = 0 ] && kill -0 "$SPOOF_PID" 2>/dev/null && grep -q "no supervisor" <<<"$out"'
kill -KILL "$SPOOF_PID" 2>/dev/null || true
wait "$SPOOF_PID" 2>/dev/null || true
rm -f "$SUP_PIDFILE"

# Even canonical-looking argv plus the real exclusive flock is not enough when
# the executable is not the verifier's Bash (or its exact dpkg-deleted path).
# Acquire the configured lock with a different binary named `bash` and verify
# the process still survives.
mkdir -p "$SUP_TMP/fake-bin"
cp "$(command -v yes)" "$SUP_TMP/fake-bin/bash"
(
    exec 200>"$A2A_SUPERVISOR_LOCK"
    flock -n 200
    exec "$SUP_TMP/fake-bin/bash" "$TOOL" supervise
) >/dev/null 2>&1 &
FAKE_BASH_PID=$!
printf '%s\n' "$FAKE_BASH_PID" > "$SUP_PIDFILE"
out="$(bash "$TOOL" stop 2>&1)"; rc=$?
ok "stop rejects a fake Bash holding the canonical fd 200 flock" \
    '[ "$rc" = 0 ] && kill -0 "$FAKE_BASH_PID" 2>/dev/null && grep -q "no supervisor" <<<"$out"'
kill -KILL "$FAKE_BASH_PID" 2>/dev/null || true
wait "$FAKE_BASH_PID" 2>/dev/null || true
rm -f "$SUP_PIDFILE"

# Missing tunnel SSH target: supervise refuses even if validation would pass.
ENV_NOSSH="$SUP_TMP/nossh.env"
grep -v '^A2A_TUNNEL_SSH_TARGET=' "$ENVF" > "$ENV_NOSSH"
out="$(bash "$TOOL" supervise --env-file "$ENV_NOSSH" 2>&1)"; rc=$?
ok "supervise requires A2A_TUNNEL_SSH_TARGET" '[ "$rc" = 2 ] && grep -q "A2A_TUNNEL_SSH_TARGET" <<<"$out"'

# Validation failure in the Python harness surfaces from supervise.
A2A_PYTHON_HARNESS="$SUP_TMP/mock-python-harness-fail.sh" \
    bash "$TOOL" supervise --env-file "$ENVF" >/dev/null 2>&1
rc=$?
ok "harness check failure aborts supervise" '[ "$rc" = 2 ]'

# ---- singleton via flock: start supervisor #1, then verify #2 refuses ----

# Start supervisor #1 in a subshell backgrounded.  It will run our mock ssh
# forever and our mock harness `run` forever; both are killable.
bash "$TOOL" supervise --env-file "$ENVF" >/dev/null 2>&1 &
SUP1_PID=$!

# Wait for the PID file to be written and the mock ssh to actually launch,
# so #2's flock -n meaningfully contends with a live holder.
for _ in $(seq 1 40); do
    if [[ -f "$A2A_TEST_SSH_MARKER" && -s "$SUP_PIDFILE" ]]; then
        break
    fi
    sleep 0.1
done

ok "supervisor #1 started (mock ssh launched)" '[ -f "$A2A_TEST_SSH_MARKER" ]'
ok "supervisor #1 wrote PID file" '[ -s "$SUP_PIDFILE" ]'
ok "PID file records PID + start token owner-only" \
    '[ "$(wc -w < "$SUP_PIDFILE")" = 2 ] && [ "$(stat -c %a "$SUP_PIDFILE")" = 600 ]'

# Second supervise call must fail fast with rc=3 (flock -n contention).
out="$(bash "$TOOL" supervise --env-file "$ENVF" 2>&1)"; rc=$?
ok "second supervise fails rc=3 while lock held" '[ "$rc" = 3 ]'

# status now reports the running supervisor's PID.
out="$(bash "$TOOL" status 2>&1)"
ok "status shows running supervisor pid" 'grep -qE "supervisor: [0-9]+" <<<"$out"'

# stop cleanly tears the supervisor down.
out="$(bash "$TOOL" stop 2>&1)"
# shellcheck disable=SC2034  # rc is consumed by the eval-based ok() assertion.
rc=$?
ok "stop terminates the running supervisor" '[ "$rc" = 0 ] && grep -qE "(stopped|killed) sup=" <<<"$out"'

# Give the wait / kill_tree cleanup a beat, then confirm supervisor #1 exit.
for _ in $(seq 1 40); do
    kill -0 "$SUP1_PID" 2>/dev/null || break
    sleep 0.1
done
ok "supervisor #1 exited after stop" '! kill -0 "$SUP1_PID" 2>/dev/null'

# After stop, another supervise can proceed (lock released).
bash "$TOOL" supervise --env-file "$ENVF" >/dev/null 2>&1 &
SUP2_PID=$!
for _ in $(seq 1 40); do
    [[ -s "$SUP_PIDFILE" ]] && break
    sleep 0.1
done
ok "supervisor #2 acquires released lock" '[ -s "$SUP_PIDFILE" ]'
bash "$TOOL" stop >/dev/null 2>&1 || true
wait "$SUP2_PID" 2>/dev/null || true

# ---- unit-level: source the script to exercise kill_tree without run-loop ----
# We source it, then invoke helpers directly.  main() is guarded by a
# BASH_SOURCE / $0 check so sourcing doesn't trigger the dispatcher.
(
    export A2A_SUPERVISOR_LOCK_DIR="$SUP_TMP"
    export A2A_SUPERVISOR_LOG_DIR="$SUP_TMP"
    # shellcheck disable=SC1090
    source "$TOOL"

    # kill_tree on a parent whose child is `sleep 3600`.
    (
        sleep 3600 &
        wait
    ) &
    parent=$!
    for _ in $(seq 1 20); do
        [[ -n "$(pgrep -P "$parent" 2>/dev/null)" ]] && break
        sleep 0.1
    done
    kill_tree "$parent"
    for _ in $(seq 1 20); do
        kill -0 "$parent" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$parent" 2>/dev/null; then
        echo "KILL_TREE_LEFT_PARENT" > "$SUP_TMP/kill_tree.marker"
    else
        echo "OK" > "$SUP_TMP/kill_tree.marker"
    fi
) || true
ok "kill_tree removes parent + child" 'grep -q "^OK$" "$SUP_TMP/kill_tree.marker" 2>/dev/null'

# Exact tunnel ownership: a same-port forward to another target must survive a
# sweep of the canonical target.
bash -c 'exec -a ssh python3 -c "import time; time.sleep(3600)" -N -L "$1" "$2"' \
    _ '127.0.0.1:18790:127.0.0.1:8787' target-owned &
OWNED_TUNNEL_PID=$!
bash -c 'exec -a ssh python3 -c "import time; time.sleep(3600)" -N -L "$1" "$2"' \
    _ '127.0.0.1:18790:127.0.0.1:8787' target-other &
OTHER_TUNNEL_PID=$!
sleep 0.2
(
    # shellcheck disable=SC1090
    source "$TOOL"
    sweep_lingering_ssh 18790 127.0.0.1:8787 target-owned
)
wait "$OWNED_TUNNEL_PID" 2>/dev/null || true
ok "tunnel sweep kills only the exact owned target" \
    '! kill -0 "$OWNED_TUNNEL_PID" 2>/dev/null && kill -0 "$OTHER_TUNNEL_PID" 2>/dev/null'
kill -KILL "$OTHER_TUNNEL_PID" 2>/dev/null || true
wait "$OTHER_TUNNEL_PID" 2>/dev/null || true

# ---- curl UP path in status ----
# shellcheck disable=SC2034  # out is consumed by the eval-based ok() assertion.
out=$(A2A_TEST_CURL_OK=1 bash "$TOOL" status 2>&1)
ok "status reports tunnel UP when curl returns 0" 'grep -q "tunnel: UP" <<<"$out"'

# ---- unit-level: sanitize_termux_env scrubs a poisoned LD_LIBRARY_PATH ----
# Regression guard for Wiki ND-1236: a leaked glibc LD_LIBRARY_PATH crashed the
# Termux tunnel ssh (rc=139) and worker node (rc=103) in a retry loop.
# supervise/run now scrub it defensively (preserving the Termux exec preload)
# instead of relying on the boot/cron launcher.
(
    export A2A_SUPERVISOR_LOCK_DIR="$SUP_TMP"
    export A2A_SUPERVISOR_LOG_DIR="$SUP_TMP"
    # shellcheck disable=SC1090
    source "$TOOL"

    fake_prefix="$SUP_TMP/tx-prefix"
    mkdir -p "$fake_prefix/lib"
    : > "$fake_prefix/lib/libtermux-exec-ld-preload.so"

    # Termux runtime + poisoned LD_LIBRARY_PATH → cleared, preload asserted.
    export PREFIX="$fake_prefix"
    export TERMUX_VERSION="test"
    export LD_LIBRARY_PATH="/leaked/glibc/lib"
    unset LD_PRELOAD
    sanitize_termux_env
    if [[ -z "${LD_LIBRARY_PATH:-}" && "${LD_PRELOAD:-}" == *"libtermux-exec-ld-preload.so"* ]]; then
        echo OK > "$SUP_TMP/sanitize.termux.marker"
    else
        echo "BAD ld=[${LD_LIBRARY_PATH:-}] preload=[${LD_PRELOAD:-}]" > "$SUP_TMP/sanitize.termux.marker"
    fi

    # Off Termux → no-op: an inherited LD_LIBRARY_PATH is left untouched.
    unset PREFIX TERMUX_VERSION LD_PRELOAD
    export LD_LIBRARY_PATH="/host/glibc/lib"
    sanitize_termux_env
    if [[ "${LD_LIBRARY_PATH:-}" == "/host/glibc/lib" ]]; then
        echo OK > "$SUP_TMP/sanitize.nontermux.marker"
    else
        echo "BAD ld=[${LD_LIBRARY_PATH:-}]" > "$SUP_TMP/sanitize.nontermux.marker"
    fi
) || true
ok "sanitize_termux_env clears poisoned LD_LIBRARY_PATH, keeps Termux preload" 'grep -q "^OK$" "$SUP_TMP/sanitize.termux.marker" 2>/dev/null'
ok "sanitize_termux_env is a no-op off Termux" 'grep -q "^OK$" "$SUP_TMP/sanitize.nontermux.marker" 2>/dev/null'

# -- argument parsing terminates (regression: unknown arg / valueless --env-file
# spun the dispatch loop forever, flooding the redirected log) ---------------
timeout 5 "$TOOL" status bogus-arg > "$TMP/args.unknown.out" 2>&1
rc_unknown=$?
ok "unknown argument exits 2 instead of looping" '[ "$rc_unknown" = 2 ]'
ok "unknown argument names the offender" 'grep -q "unknown arg: bogus-arg" "$TMP/args.unknown.out"'
timeout 5 "$TOOL" check --env-file > "$TMP/args.novalue.out" 2>&1
rc_novalue=$?
ok "valueless --env-file exits 2 instead of looping" '[ "$rc_novalue" = 2 ]'
ok "valueless --env-file says a value is required" 'grep -q -- "--env-file requires a value" "$TMP/args.novalue.out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
