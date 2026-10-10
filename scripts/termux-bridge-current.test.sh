#!/usr/bin/env bash
# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
# Tests for termux-bridge-current.sh — hermetic: fake HOME, a fake generation
# whose start.sh records argv + the environment it received, a fake flock in
# PATH. No Termux, nothing real launched.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/termux-bridge-current.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
TMP="$(ccc_test_tmpdir)" || exit 1
cleanup() { [ -n "${HOLDER:-}" ] && kill "$HOLDER" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT
export HOME="$TMP/home"
unset CCC_TERMUX_CCC_NODE_DIR CCC_TERMUX_LAUNCH_LOCK CCC_TERMUX_LAUNCH_LOCK_WAIT_SECONDS CCC_TERMUX_LAUNCH_FLOCK
CN="$HOME/.ccc-node"; PREP="$CN/preparations"
CALLS="$TMP/calls"; export CALLS
ENVDUMP="$TMP/envdump"; export ENVDUMP

# --- a generation: source/bridge/start.sh records argv + env; job/runtime/bin/python exists
mkgen() { # <name> [channel-aware:1|0] [job-name]
  local g="$PREP/$1" job="${3:-job}"
  mkdir -p "$g/source/bridge" "$g/$job/runtime/bin"
  cat > "$g/source/bridge/start.sh" <<'SH'
#!/usr/bin/env bash
printf 'start.sh %s\n' "$*" >> "$CALLS"
env | grep -E '^(CCC_CHANNEL|BOT_DATA_DIR|LOGS_DIR|SESSION_STORE_PATH|CCC_BOT_ENV_FILE|CCC_MATRIX_[A-Z_]+|VIRTUAL_ENV|ANDROID_API_LEVEL|KEEP_ME)=' > "$ENVDUMP" || :
exit "${FAKE_START_RC:-0}"
SH
  if [ "${2:-1}" = 1 ]; then
    # the marker termux-bridge-current.sh probes for (#2177 start.sh parses `--channel)`)
    printf '# case "$1" in --channel) ;; esac\n' >> "$g/source/bridge/start.sh"
  fi
  chmod +x "$g/source/bridge/start.sh"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$g/$job/runtime/bin/python"; chmod +x "$g/$job/runtime/bin/python"
}
point() { ln -sfn "preparations/$1" "$CN/bridge-current"; }
reset() { : > "$CALLS"; : > "$ENVDUMP"; }
run() { bash "$SCRIPT" "$@" 2>&1; }

# fake flock: records the call, then runs the command
mkdir -p "$TMP/bin"
cat > "$TMP/bin/flock" <<'SH'
#!/usr/bin/env bash
printf 'flock %s\n' "$*" >> "$CALLS"
while [ $# -gt 0 ]; do case "$1" in --exclusive|--close) shift ;; --timeout) shift 2 ;; *) break ;; esac; done
shift   # lock file
exec "$@"
SH
chmod +x "$TMP/bin/flock"
export PATH="$TMP/bin:$PATH"

mkdir -p "$CN"
mkgen gen-A 1
mkgen gen-old 0
mkgen gen-S 1 job2; ln -s job2 "$PREP/gen-S/serving"

# ---------------------------------------------------------------- usage / layout
out="$(run)"; rc=$?
ok "no arguments → exit 2" '[ "$rc" = 2 ]'
out="$(run --print-source)"; rc=$?
ok "missing pointer → exit 3" '[ "$rc" = 3 ] && [[ "$out" == *"pointer"* ]]'
point gen-A
out="$(run --print-source)"; rc=$?
ok "--print-source resolves the pointer to the REAL source dir" '[ "$rc" = 0 ] && [ "$out" = "$(readlink -e "$PREP/gen-A/source")" ]'
out="$(run --print-job)"; rc=$?
ok "--print-job resolves to the job dir" '[ "$rc" = 0 ] && [ "$out" = "$(readlink -e "$PREP/gen-A/job")" ]'
out="$(run --print-generation)"
ok "--print-generation resolves to the generation dir" '[ "$out" = "$(readlink -e "$PREP/gen-A")" ]'
point gen-S
out="$(run --print-job)"
ok "a serving link selects its job dir" '[ "$out" = "$(readlink -e "$PREP/gen-S/job2")" ]'
rm "$PREP/gen-S/serving"; ln -s nowhere "$PREP/gen-S/serving"
out="$(run --print-job)"; rc=$?
ok "dangling serving link → exit 3" '[ "$rc" = 3 ]'
rm -f "$PREP/gen-S/serving"
out="$(run --print-job)"; rc=$?
ok "serving link gone and no job dir → exit 3" '[ "$rc" = 3 ] && [[ "$out" == *"job missing"* ]]'
point gen-A; chmod -x "$PREP/gen-A/job/runtime/bin/python"
out="$(run --print-job)"; rc=$?
ok "generation without a runtime python → exit 3" '[ "$rc" = 3 ] && [[ "$out" == *"no runtime"* ]]'
chmod +x "$PREP/gen-A/job/runtime/bin/python"
ok "print-* never touch the lock or start.sh" '[ ! -s "$CALLS" ]'

# ---------------------------------------------------------------- launch through flock
reset; point gen-A
out="$(CCC_CHANNEL=matrix BOT_DATA_DIR=/x/.ccc-matrix LOGS_DIR=/x/logs SESSION_STORE_PATH=/x/s.json CCC_BOT_ENV_FILE=/x/.env CCC_MATRIX_HOMESERVER=https://m CCC_MATRIX_ROOM=!r VIRTUAL_ENV=/x/venv ANDROID_API_LEVEL=24 KEEP_ME=1 run --path "$HOME" --restart -d)"; rc=$?
ok "launch exits with start.sh's status (0)" '[ "$rc" = 0 ]'
ok "flock serialises the call on the lifecycle lock" 'grep -q -- "^flock --exclusive --timeout 300 --close $CN/bridge-prepared-lifecycle.lock bash " "$CALLS"'
ok "start.sh gets --prepared-runtime <job> first, then the caller args" 'grep -q -- "^start.sh --prepared-runtime $(readlink -e "$PREP/gen-A/job") --path $HOME --restart -d" "$CALLS"'
ok "--channel telegram appended when start.sh understands it" 'grep -q -- "--restart -d --channel telegram$" "$CALLS"'
ok "Matrix-scoped and venv variables are scrubbed" '! grep -qE "^(CCC_CHANNEL|BOT_DATA_DIR|LOGS_DIR|SESSION_STORE_PATH|CCC_BOT_ENV_FILE|CCC_MATRIX_|VIRTUAL_ENV|ANDROID_API_LEVEL)" "$ENVDUMP"'
ok "unrelated variables survive" 'grep -q "^KEEP_ME=1$" "$ENVDUMP"'

reset
out="$(run --path "$HOME" --status --channel telegram)"
ok "caller's own --channel is not duplicated" '[ "$(grep "^start.sh" "$CALLS" | grep -o -- "--channel" | wc -l)" = 1 ]'

reset; point gen-old
out="$(run --path "$HOME" --restart -d)"
ok "older start.sh without --channel support gets no --channel flag" 'grep -q -- "^start.sh --prepared-runtime .* --restart -d$" "$CALLS" && ! grep -q -- "--channel" "$CALLS"'

reset; point gen-A
out="$(FAKE_START_RC=7 run --path "$HOME" --restart -d)"; rc=$?
ok "start.sh's non-zero status propagates (7)" '[ "$rc" = 7 ]'

# ---------------------------------------------------------------- mkdir fallback (no flock)
reset
export CCC_TERMUX_LAUNCH_FLOCK=0 CCC_TERMUX_LAUNCH_LOCK_WAIT_SECONDS=2
out="$(run --path "$HOME" --restart -d)"; rc=$?
ok "without flock the launch still happens and succeeds" '[ "$rc" = 0 ] && ! grep -q "^flock" "$CALLS" && grep -q "^start.sh --prepared-runtime" "$CALLS"'
ok "the mkdir claim is released afterwards" '[ ! -e "$CN/bridge-prepared-lifecycle.lock.d" ]'

# stale claim (dead pid) is reclaimed
reset; mkdir -p "$CN/bridge-prepared-lifecycle.lock.d"
DEAD=$(bash -c 'echo $$'); printf '%s\n' "$DEAD" > "$CN/bridge-prepared-lifecycle.lock.d/pid"
out="$(run --path "$HOME" --restart -d)"; rc=$?
ok "a claim left by a dead pid is reclaimed" '[ "$rc" = 0 ] && grep -q "^start.sh" "$CALLS" && [ ! -e "$CN/bridge-prepared-lifecycle.lock.d" ]'

# live holder: wait expires → exit 1, nothing launched
reset; sleep 600 & HOLDER=$!
mkdir -p "$CN/bridge-prepared-lifecycle.lock.d"; printf '%s\n' "$HOLDER" > "$CN/bridge-prepared-lifecycle.lock.d/pid"
out="$(run --path "$HOME" --restart -d)"; rc=$?
ok "a live holder beyond the wait → exit 1, start.sh not called" '[ "$rc" = 1 ] && ! grep -q "^start.sh" "$CALLS" && [[ "$out" == *"held by pid $HOLDER"* ]]'
ok "the live holder's claim is left in place" '[ "$(cat "$CN/bridge-prepared-lifecycle.lock.d/pid")" = "$HOLDER" ]'
kill "$HOLDER" 2>/dev/null; HOLDER=""; rm -rf "$CN/bridge-prepared-lifecycle.lock.d"
unset CCC_TERMUX_LAUNCH_FLOCK CCC_TERMUX_LAUNCH_LOCK_WAIT_SECONDS

# ---------------------------------------------------------------- env override of the state dir
reset; mkdir -p "$TMP/alt/preparations"; cp -a "$PREP/gen-A" "$TMP/alt/preparations/gen-A"; ln -sfn preparations/gen-A "$TMP/alt/bridge-current"
out="$(CCC_TERMUX_CCC_NODE_DIR="$TMP/alt" run --print-generation)"
ok "CCC_TERMUX_CCC_NODE_DIR relocates the pointer" '[ "$out" = "$(readlink -e "$TMP/alt/preparations/gen-A")" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
