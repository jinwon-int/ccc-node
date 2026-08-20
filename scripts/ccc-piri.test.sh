#!/usr/bin/env bash
# Hermetic launch-surface tests for scripts/ccc-piri (node-global Piri
# counterpart of the ccc-codex launcher tests in ccc-codex.test.sh).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHER="$ROOT/scripts/ccc-piri"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

mat="$TMP/materializer"
real="$TMP/real-piri"
cat > "$mat" <<'SH'
#!/usr/bin/env bash
case "${1:-}" in
  materialize)
    printf 'materialize\n' >> "${ORDER_FILE:?}"
    printf 'CODEX_HOME=%s\nPROVIDER=%s\nMAX_BYTES=%s\n' "${CODEX_HOME:-}" "${CCC_MEMORY_MATERIALIZER_PROVIDER:-}" "${CCC_CODEX_MEMORY_MAX_BYTES:-}" >> "${ENV_FILE:?}"
    printf 'MATERIALIZER_BODY_SENTINEL\n'
    printf 'MATERIALIZER_ERROR_SENTINEL\n' >&2
    exit "${MAT_RC:-0}"
    ;;
  status)
    printf 'status\n' >> "${ORDER_FILE:?}"
    exit "${STATUS_RC:-1}"
    ;;
  *) exit 64 ;;
esac
SH
cat > "$real" <<'SH'
#!/usr/bin/env bash
printf 'real\n' >> "${ORDER_FILE:?}"
pwd > "${CWD_FILE:?}"
python3 - "$ARGV_FILE" "$@" <<'PY'
import json,sys
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[2:], fh)
PY
input="$(cat)"
printf 'REAL:%s' "$input"
exit "${REAL_RC:-0}"
SH
chmod 0700 "$mat" "$real"

piri_home="$TMP/piri-agent"; mkdir -p "$piri_home"
work="$TMP/work dir"; mkdir -p "$work"
order="$TMP/order"; argv="$TMP/argv.json"; cwd_file="$TMP/cwd"; err="$TMP/err"; env_file="$TMP/env"
set +e
out="$(cd "$work" && printf 'stdin data' | ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=0 STATUS_RC=1 REAL_RC=23 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat" CCC_PIRI_REAL_CLI_PATH="$real" "$LAUNCHER" --mode rpc --approve 2>"$err")"
rc=$?
set -e
ok "launcher preserves real Piri exit code and stdio" '[ "$rc" = 23 ] && [ "$out" = "REAL:stdin data" ]'
ok "launcher suppresses materializer body and error output" '! grep -q "MATERIALIZER_" "$err" && [[ "$out" != *MATERIALIZER_* ]]'
ok "launcher materializes before exec and preserves cwd" '[ "$(cat "$order")" = $'"'"'materialize\nreal'"'"' ] && [ "$(cat "$cwd_file")" = "$work" ]'
ok "launcher preserves argv boundaries" 'python3 - "$argv" <<'"'"'PY'"'"'
import json,sys
raise SystemExit(0 if json.load(open(sys.argv[1])) == ["--mode", "rpc", "--approve"] else 1)
PY'
ok "launcher points the materializer at the Piri agent dir with the piri provider" \
  'grep -Fx "CODEX_HOME='"$piri_home"'" "$env_file" >/dev/null && grep -Fx "PROVIDER=piri" "$env_file" >/dev/null'
ok "launcher defaults the snapshot cap to 16KiB so the nunchi block is not starved" \
  'grep -Fx "MAX_BYTES=16384" "$env_file" >/dev/null'

: > "$order"; : > "$env_file"; rm -f "$argv"
set +e
out="$(cd "$work" && printf '' | ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=0 STATUS_RC=1 REAL_RC=0 CCC_CODEX_MEMORY_MAX_BYTES=24576 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat" CCC_PIRI_REAL_CLI_PATH="$real" "$LAUNCHER" 2>"$err")"
rc=$?
set -e
ok "an explicit CCC_CODEX_MEMORY_MAX_BYTES always wins over the launcher default" \
  '[ "$rc" = 0 ] && grep -Fx "MAX_BYTES=24576" "$env_file" >/dev/null && ! grep -q "MAX_BYTES=16384" "$env_file"'

: > "$order"; rm -f "$argv"
set +e
out="$(ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=9 STATUS_RC=0 REAL_RC=0 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat" CCC_PIRI_REAL_CLI_PATH="$real" "$LAUNCHER" ready 2>"$err")"
rc=$?
set -e
ok "launcher uses last valid snapshot when refresh fails" '[ "$rc" = 0 ] && [ "$out" = "REAL:" ] && [ "$(cat "$order")" = $'"'"'materialize\nstatus\nreal'"'"' ]'

: > "$order"; rm -f "$argv"
set +e
ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=9 STATUS_RC=7 REAL_RC=0 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat" CCC_PIRI_REAL_CLI_PATH="$real" "$LAUNCHER" blocked >"$TMP/out" 2>"$err"
rc=$?
set -e
ok "launcher fails closed when no current or last-valid snapshot exists" '[ "$rc" = 78 ] && [ ! -e "$argv" ] && [ "$(cat "$order")" = $'"'"'materialize\nstatus'"'"' ]'
ok "fail-closed diagnostic is bounded and body-free" '[ "$(wc -c < "$err")" -lt 256 ] && ! grep -q "MATERIALIZER_\|SECRET" "$err"'

: > "$order"
set +e
ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=0 STATUS_RC=0 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat" CCC_PIRI_REAL_CLI_PATH="$LAUNCHER" "$LAUNCHER" >"$TMP/out" 2>"$err"
rc=$?
set -e
ok "launcher rejects recursive real-cli configuration" '[ "$rc" = 127 ]'
ok "launcher uses final exec rather than a child Piri process" 'grep -Fq '"'"'exec "$real_cli" "$@"'"'"' "$LAUNCHER"'

# Guard: the nunchi piri-feed extractor runs tool-free and must never receive
# user memory — it is routed straight to the real CLI with no materialize.
: > "$order"; rm -f "$argv"
set +e
out="$(ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=0 STATUS_RC=0 REAL_RC=0 PIRI_CODING_AGENT_SESSION_DIR="$TMP/nunchi/.piri-feed-extractor-sessions" CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat" CCC_PIRI_REAL_CLI_PATH="$real" "$LAUNCHER" --mode text --print x 2>"$err")"
rc=$?
set -e
ok "extractor-session guard bypasses the memory bootstrap entirely" '[ "$rc" = 0 ] && [ "$out" = "REAL:" ] && [ "$(cat "$order")" = "real" ]'

# Guard: audience-scoped sessions are bootstrapped by the bridge runtime
# itself; the node-global launcher must not touch global memory for them.
: > "$order"
set +e
out="$(ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=0 STATUS_RC=0 REAL_RC=0 CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat" CCC_PIRI_REAL_CLI_PATH="$real" "$LAUNCHER" 2>"$err")"
rc=$?
set -e
ok "audience-scoped guard bypasses the node-global memory bootstrap" '[ "$rc" = 0 ] && [ "$out" = "REAL:" ] && [ "$(cat "$order")" = "real" ]'

# Guard: explicit operator kill-switch.
: > "$order"
set +e
out="$(ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=0 STATUS_RC=0 REAL_RC=0 CCC_PIRI_MEMORY_SKIP=1 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat" CCC_PIRI_REAL_CLI_PATH="$real" "$LAUNCHER" 2>"$err")"
rc=$?
set -e
ok "CCC_PIRI_MEMORY_SKIP=1 bypasses the memory bootstrap" '[ "$rc" = 0 ] && [ "$out" = "REAL:" ] && [ "$(cat "$order")" = "real" ]'

# --- shebang-unresolvable materializer (Termux exit-78 class) ---
# Same incident class as ccc-codex: without libtermux-exec's LD_PRELOAD hook
# the kernel cannot exec `#!/usr/bin/env python3` on Android; a broken
# absolute interpreter reproduces the ENOENT class hermetically. The launcher
# must retry via an interpreter from PATH, preserving the env prefix.
mat_py="$TMP/materializer-py"
cat > "$mat_py" <<'PY'
#!/nonexistent/ccc-test-interp
import os, sys
sub = sys.argv[1] if len(sys.argv) > 1 else ""
order = os.environ["ORDER_FILE"]
with open(order, "a", encoding="utf-8") as fh:
    fh.write(sub + "\n")
if sub == "materialize":
    with open(os.environ["ENV_FILE"], "a", encoding="utf-8") as fh:
        fh.write("CODEX_HOME=%s\nPROVIDER=%s\nMAX_BYTES=%s\n" % (
            os.environ.get("CODEX_HOME", ""),
            os.environ.get("CCC_MEMORY_MATERIALIZER_PROVIDER", ""),
            os.environ.get("CCC_CODEX_MEMORY_MAX_BYTES", "")))
sys.exit(int(os.environ.get("MAT_RC", "0")))
PY
chmod 0700 "$mat_py"
: > "$order"; : > "$env_file"; rm -f "$argv"
set +e
out="$(printf 'stdin data' | ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=0 REAL_RC=23 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat_py" CCC_PIRI_REAL_CLI_PATH="$real" "$LAUNCHER" --mode rpc 2>"$err")"
rc=$?
set -e
ok "launcher falls back to an explicit interpreter when the materializer shebang cannot exec" \
  '[ "$rc" = 23 ] && grep -qx materialize "$order" && grep -qx real "$order" && ! grep -q "memory bootstrap unavailable" "$err"'
ok "shebang fallback preserves the materialize env prefix" \
  'grep -Fx "CODEX_HOME=$piri_home" "$env_file" >/dev/null && grep -Fx "PROVIDER=piri" "$env_file" >/dev/null && grep -Fx "MAX_BYTES=16384" "$env_file" >/dev/null'

: > "$order"; : > "$err"
set +e
out="$(printf '' | ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=5 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat_py" CCC_PIRI_REAL_CLI_PATH="$real" "$LAUNCHER" 2>"$err")"
rc=$?
set -e
ok "shebang-fallback materializer still fails closed on real script failures" '[ "$rc" = 78 ]'

# #1184: the final real-CLI exec is shebang-aware. A `#!/usr/bin/env X` real
# CLI runs through the interpreter resolved from PATH (the kernel cannot
# resolve /usr/bin/env on Android without libtermux-exec's hook); an
# absolute-shebang script still execs directly. The piri chain's later hops
# (pi-test.sh -> tsx) live in the piri repo and are out of scope here. The
# stub interpreter gets a unique name so the launcher/materializer's own
# `env bash` resolutions can never be mistaken for the real-CLI route.
stubdir="$TMP/stub-bin"; mkdir -p "$stubdir"
real_bash="$(command -v bash)"
stub_log="$TMP/stub-interp.log"
stub_name="bashstub-ccc1184"
cat > "$stubdir/$stub_name" <<SH
#!$real_bash
printf 'STUB:%s\n' "\$*" >> "$stub_log"
exec "$real_bash" "\$@"
SH
chmod 0700 "$stubdir/$stub_name"

real_env="$TMP/real-env-stub"
{ printf '#!/usr/bin/env %s\n' "$stub_name"; tail -n +2 "$real"; } > "$real_env"
chmod 0700 "$real_env"
: > "$order"; : > "$stub_log"
set +e
out="$(printf 'stdin data' | PATH="$stubdir:$PATH" ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=0 STATUS_RC=1 REAL_RC=23 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat" CCC_PIRI_REAL_CLI_PATH="$real_env" "$LAUNCHER" --mode rpc 2>"$err")"
rc=$?
set -e
ok "env-shebang real CLI execs through the PATH-resolved interpreter" \
  '[ "$rc" = 23 ] && [ "$out" = "REAL:stdin data" ] && grep -Fx "STUB:'"$real_env"' --mode rpc" "$stub_log" >/dev/null'

real_abs="$TMP/real-abs"
{ printf '#!%s\n' "$real_bash"; tail -n +2 "$real"; } > "$real_abs"
chmod 0700 "$real_abs"
: > "$order"; : > "$stub_log"
set +e
out="$(printf 'stdin data' | PATH="$stubdir:$PATH" ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" ENV_FILE="$env_file" MAT_RC=0 STATUS_RC=1 REAL_RC=23 CCC_PIRI_MEMORY_HOME="$piri_home" CCC_PIRI_MEMORY_MATERIALIZER_PATH="$mat" CCC_PIRI_REAL_CLI_PATH="$real_abs" "$LAUNCHER" --mode rpc 2>"$err")"
rc=$?
set -e
ok "absolute-shebang real CLI still execs directly" \
  '[ "$rc" = 23 ] && [ "$out" = "REAL:stdin data" ] && [ ! -s "$stub_log" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
