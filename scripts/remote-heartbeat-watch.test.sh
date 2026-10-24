#!/usr/bin/env bash
# shellcheck disable=SC2088,SC2120,SC2034  # literal ~ is expanded on the (fake) remote; out/rc are read via eval in ok()
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/remote-heartbeat-watch.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
PASS=0 FAIL=0
ok() { if eval "$2"; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); echo "FAIL: $1" >&2; fi; }

# Fake ssh: runs the remote command locally with HOME=$TMP/home, unless the
# target is "down" (unreachable).
mkdir -p "$TMP/bin" "$TMP/home/st"
cat > "$TMP/bin/ssh" <<'SH'
#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do case "$1" in -o) shift 2 ;; *) break ;; esac; done
host="$1"; shift
[ "$host" = down ] && exit 255
HOME="$FAKE_HOME" bash -c "$1"
SH
chmod +x "$TMP/bin/ssh"
export CCC_HEARTBEAT_SSH="$TMP/bin/ssh" FAKE_HOME="$TMP/home"
HB="$TMP/home/st/hb.json"; ST="$TMP/state.json"
run() { bash "$SCRIPT" --target node1 --path '~/st/hb.json' --max-age-min 60 --state-file "$ST" --label watch "$@"; }

touch "$HB"
out="$(run)"; rc=$?
ok "fresh heartbeat is silent" '[ "$rc" = 0 ] && [ -z "$out" ]'

touch -d '-3 hours' "$HB"
out="$(run)"; rc=$?
ok "stale heartbeat pages NEW once" '[ "$rc" = 1 ] && grep -q "^NEW stale heartbeat watch" <<<"$out"'
out="$(run)"; rc=$?
ok "still stale inside the reminder window is silent" '[ "$rc" = 0 ] && [ -z "$out" ]'
out="$(CCC_HEARTBEAT_NOW=$(( $(date +%s) + 7 * 3600 )) run)"; rc=$?
ok "still stale after the reminder window pages STILL" '[ "$rc" = 1 ] && grep -q "^STILL stale heartbeat watch" <<<"$out"'
out="$(CCC_HEARTBEAT_NOW=$(( $(date +%s) + 8 * 3600 )) run)"; rc=$?
ok "reminder resets the window" '[ "$rc" = 0 ] && [ -z "$out" ]'

touch "$HB"
out="$(run)"; rc=$?
ok "fresh again pages RECOVERED" '[ "$rc" = 1 ] && grep -q "^RECOVERED heartbeat watch" <<<"$out"'
out="$(run)"; rc=$?
ok "after recovery it is silent again" '[ "$rc" = 0 ] && [ -z "$out" ]'

rm -f "$HB"
out="$(run)"; rc=$?
ok "missing heartbeat file pages as stale" '[ "$rc" = 1 ] && grep -q "heartbeat file missing" <<<"$out"'
touch "$HB"; run >/dev/null

out="$(bash "$SCRIPT" --target down --path '~/st/hb.json' --max-age-min 60 --state-file "$TMP/s2.json" 2>"$TMP/err")"; rc=$?
ok "unreachable target is left to the bridge watch" '[ "$rc" = 0 ] && [ -z "$out" ] && grep -q unreachable "$TMP/err"'

bash "$SCRIPT" --target node1 --path '~/x;rm -rf /' --max-age-min 60 --state-file "$ST" >/dev/null 2>&1; rc=$?
ok "shell metacharacters in --path are rejected" '[ "$rc" = 2 ]'
bash "$SCRIPT" --target node1 --path '~/x' --max-age-min 0 --state-file "$ST" >/dev/null 2>&1; rc=$?
ok "zero max age is rejected" '[ "$rc" = 2 ]'

# Remote clock is authoritative: a far-future local clock must not page.
touch "$HB"
out="$(CCC_HEARTBEAT_NOW=$(( $(date +%s) + 99999 )) run)"; rc=$?
ok "age is measured on the target, not the local clock" '[ "$rc" = 0 ] && [ -z "$out" ]'

ok "state file is owner-only" '[ "$(stat -c %a "$ST")" = 600 ]'

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
