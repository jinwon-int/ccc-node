#!/usr/bin/env bash
# Exercise the actual foreground/daemon paths with a fixture interpreter.
# No provider or Telegram requests; every project/process belongs to this test.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
TMP="$(ccc_test_tmpdir)" || exit 1
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
cleanup() {
    if [ -f "$TMP/project/.telegram_bot/supervisor.pid" ]; then
        HOME="$TMP/home" CCC_SYSTEMD_DIR="$TMP/sd" bash "$HERE/start.sh" --path "$TMP/project" --stop >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT
mkdir -p "$TMP/home" "$TMP/sd" "$TMP/project/.telegram_bot" "$TMP/job/runtime/bin"
cat > "$TMP/project/.telegram_bot/.env" <<'ENV'
TELEGRAM_BOT_TOKEN=123456:TEST-only-prepared-launch
CLAUDE_CLI_PATH=/test/nonexecuted-provider
ENV
: > "$TMP/job/runtime/bin/activate"
export PREPARED_TEST_LOG="$TMP/calls" PREPARED_TEST_PROJECT="$TMP/project"
write_exec_stub "$TMP/job/runtime/bin/python" <<'STUB'
printf '%s\n' "$*" >> "$PREPARED_TEST_LOG"
case " $* " in
    *dependency_bootstrap.py*) exit 99 ;;
    *prepared_runtime.py*) exit 0 ;;
    *" -m telegram_bot "*)
        if [ "${PREPARED_TEST_DAEMON:-0}" = 1 ]; then
            exec sleep 120
        fi
        exit 0 ;;
    *) exit 98 ;;
esac
STUB
rc=0
HOME="$TMP/home" CCC_SYSTEMD_DIR="$TMP/sd" bash "$HERE/start.sh" \
    --path "$TMP/project" --prepared-runtime "$TMP/job" > "$TMP/foreground.out" 2>&1 || rc=$?
ok "prepared foreground start reaches fixture bot" '[ "$rc" = 0 ] && grep -q -- "-m telegram_bot" "$TMP/calls"'
ok "prepared start validates and requests a prelaunch record" 'grep -q -- "--record-dir" "$TMP/calls"'
ok "prepared start never runs dependency bootstrap" '! grep -q dependency_bootstrap "$TMP/calls"'
# The fixture bot exited normally; remove only its stale test PID marker.
rm -f "$TMP/project/.telegram_bot/bot.pid"
: > "$TMP/calls"
rc=0
HOME="$TMP/home" CCC_SYSTEMD_DIR="$TMP/sd" PREPARED_TEST_DAEMON=1 \
    bash "$HERE/start.sh" --path "$TMP/project" --prepared-runtime "$TMP/job" --daemon \
    > "$TMP/daemon.out" 2>&1 || rc=$?
for _ in $(seq 1 60); do
    grep -q -- '-m telegram_bot' "$TMP/calls" && break
    sleep 0.1
done
ok "daemon child retains prepared runtime selection" '[ "$rc" = 0 ] && grep -q -- "prepared_runtime.py" "$TMP/calls" && grep -q -- "-m telegram_bot" "$TMP/calls"'
ok "daemon child also skips installation" '! grep -q dependency_bootstrap "$TMP/calls"'
rc=0
# shellcheck disable=SC2034 # rc is consumed by the eval-based assertion below.
HOME="$TMP/home" bash "$HERE/start.sh" --path "$TMP/project" --prepared-runtime "$TMP/job" --install-systemd > "$TMP/install.out" 2>&1 || rc=$?
ok "unsupported service-install combination is rejected" '[ "$rc" = 2 ]'
printf 'PASS=%s FAIL=%s\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
