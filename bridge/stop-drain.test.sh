#!/usr/bin/env bash
# Hermetic stop/drain ordering: source actual do_stop, model owned PIDs/signals
# and a virtual clock. No live process lookup, signals, services or bot calls.
# harness: umask-rerun
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/project"
set +u
CCC_START_SH_LIB_ONLY=1 . "$HERE/start.sh" --path "$TMP/project" >/dev/null
set -u
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
# Shell functions execute inside do_stop's real subshell; files share state
# through its command substitutions, as real kernel process state would.
read_pid() { [ "$CASE" = unmanaged ] || echo 101; }
read_supervisor_pid() { [ "$CASE" != daemon ] || echo 100; }
_parent_pid_of() { echo 100; }
_stop_process_state() {
    case "$CASE" in zombie) echo Z ;; unknown_state) return 1 ;; *) echo S ;; esac
}
find_project_bot_pids() {
    if [ "$CASE" = unmanaged ] && [ -f "$STATE/101" ]; then echo 101; fi
    if [ "$CASE" = discovered ] && [ ! -f "$STATE/101" ]; then touch "$STATE/102"; echo 102; fi
}
launchctl() {
    [ "$CASE" != launchd_failure ] || return 1
    kill 101
}
cleanup_pid() { echo pid >> "$STATE/cleaned"; }
cleanup_supervisor_pid() { echo supervisor >> "$STATE/cleaned"; }
cleanup_token_lock_if_safe() { echo token >> "$STATE/cleaned"; }
kill() {
    local sig=TERM target
    case "${1:-}" in -0) sig=probe; shift ;; -9) sig=KILL; shift ;; esac
    target="$1"
    if [ "$sig" = probe ]; then [ -f "$STATE/$target" ]; return; fi
    printf '%s %s %s\n' "$SECONDS" "$sig" "$target" >> "$STATE/signals"
    if [ "$sig" = KILL ]; then
        [ "$CASE" = stuck ] || rm -f "$STATE/$target"
    elif [ "$target" = 100 ]; then
        # The real daemon EXIT handler forwards once to its direct child.
        printf '%s TERM 101\n' "$SECONDS" >> "$STATE/signals"
        touch "$STATE/draining"
        # Model a supervisor that exits before its draining child. A second
        # outer TERM would force the Python handler before work completes.
        rm -f "$STATE/100"
    else
        touch "$STATE/draining"
    fi
    return 0
}
sleep() {
    SECONDS=$((SECONDS + 1))
    if [ -f "$STATE/draining" ] && [ "$SECONDS" -ge 12 ] \
        && [ "$CASE" != timeout ] && [ "$CASE" != stuck ]; then
        rm -f "$STATE/101" "$STATE/100"
        echo complete > "$STATE/work"
    fi
}
run_case() {
    CASE="$1"; STATE="$TMP/$CASE"; mkdir -p "$STATE"
    touch "$STATE/101" "$STATE/signals"
    [ "$CASE" != daemon ] || touch "$STATE/100"
    # shellcheck disable=SC2034  # sourced do_stop reads this path
    PLIST_FILE="$STATE/absent.plist"
    case "$CASE" in launchd*) touch "$PLIST_FILE" ;; esac
    RC=0
    # shellcheck disable=SC2034  # assertions evaluate RC via ok()
    # Unset removes Bash's wall-clock specialness: only fake sleep advances it.
    ( unset SECONDS; SECONDS=0; do_stop ) > "$STATE/output" 2>&1 || RC=$?
}
for mode in foreground daemon unmanaged; do
    run_case "$mode"
    ok "$mode completes accepted work after old10s boundary" '[ "$RC" = 0 ] && [ -f "$STATE/work" ]'
    ok "$mode sends exactly one TERM to bridge" '[ "$(grep -c " TERM 101$" "$STATE/signals")" = 1 ]'
    ok "$mode never prematurely KILLs bridge" '! grep -q KILL "$STATE/signals"'
    ok "$mode cleans bookkeeping after exit" '[ "$(wc -l < "$STATE/cleaned")" = 3 ]'
done
run_case launchd
ok 'launchd drain gets no repeated direct TERM' '[ "$RC" = 0 ] && [ "$(grep -c " TERM 101$" "$STATE/signals")" = 1 ] && [ -f "$STATE/work" ]'
run_case launchd_failure
ok 'failed service unload retains state without direct signalling' '[ "$RC" = 1 ] && [ ! -s "$STATE/signals" ] && [ ! -e "$STATE/cleaned" ]'
run_case zombie
ok 'zombie target is already stopped without signalling' '[ "$RC" = 0 ] && [ ! -s "$STATE/signals" ] && [ -f "$STATE/cleaned" ]'
run_case unknown_state
ok 'unknown process state retains live-process semantics' '[ "$RC" = 0 ] && [ -f "$STATE/work" ]'
run_case timeout
ok 'default70s grace precedes escalation' 'grep -q "^70 KILL 101$" "$STATE/signals"'
ok 'successful escalation completes stop' '[ "$RC" = 0 ] && [ -f "$STATE/cleaned" ]'
run_case stuck
ok 'surviving PID produces failure' '[ "$RC" = 1 ]'
ok 'surviving PID retains all bookkeeping' '[ ! -e "$STATE/cleaned" ]'
ok 'failure names remaining PID' 'grep -q "refuses to exit.*101" "$STATE/output"'
run_case discovered
ok 'newly discovered project bot blocks success' '[ "$RC" = 1 ] && [ ! -e "$STATE/cleaned" ]'
for invalid in 0 -1 1.5 01 3601 999999999999999999999 '1+1'; do
    CCC_BRIDGE_STOP_GRACE_SECONDS="$invalid" run_case invalid
    ok "invalid grace rejected before signalling: $invalid" '[ "$RC" = 2 ] && [ ! -s "$STATE/signals" ]'
done
CCC_BRIDGE_STOP_GRACE_SECONDS=2 run_case override
ok 'explicit short grace is bounded' 'grep -q "^2 KILL 101$" "$STATE/signals"'
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
