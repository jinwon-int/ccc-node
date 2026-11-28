#!/usr/bin/env bash
# Hermetic tests for termux-dns-health-watch.sh (ccc-node#2937).
# Probe results are forced through the CCC_DNS_WATCH_TEST_* seams, so the
# suite needs no network and never touches the real push spool or state.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH="$ROOT/scripts/termux-dns-health-watch.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccc-dns-watch-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

export CCC_CLAUDE_DIR="$TMP/claude"
export CCC_STATE_DIR="$CCC_CLAUDE_DIR/state"
export CCC_PUSH_SPOOL="$CCC_STATE_DIR/telegram-spool"
export CCC_NODE="testnode"
mkdir -p "$CCC_PUSH_SPOOL"
STATE="$CCC_STATE_DIR/dns-health-watch.json"
LOG="$CCC_STATE_DIR/dns-health-watch.log"

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
spooled() { find "$CCC_PUSH_SPOOL" -maxdepth 1 -name 'dns-health-*.json' | wc -l | tr -d ' '; }
run() { CCC_DNS_WATCH_TEST_SYS="$1" CCC_DNS_WATCH_TEST_RAW="$2" CCC_DNS_WATCH_TEST_VPN="${3:-fail}" bash "$WATCH" >/dev/null 2>&1; }
state_field() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$STATE" "$1"; }

# healthy run: rc 0, no alert, state written
run ok ok; rc=$?
ok "healthy run exits 0" '[ "$rc" = 0 ]'
ok "healthy run writes state" '[ "$(state_field status)" = ok ]'
ok "healthy run sends nothing" '[ "$(spooled)" = 0 ]'
ok "healthy run logs a line" 'grep -q "status=ok sys=1 raw=1" "$LOG"'

# offline (both probes fail): rc 3, logged, never alerts
for _ in 1 2 3; do run fail fail; rc=$?; done
ok "offline exits 3" '[ "$rc" = 3 ]'
ok "offline never alerts" '[ "$(spooled)" = 0 ]'
ok "offline is logged" 'grep -q "status=offline" "$LOG"'
ok "offline does not build a broken streak" '[ "$(state_field streak)" = 0 ]'

# system DNS broken, raw OK: first run below threshold, second alerts once
run fail ok; rc=$?
ok "sysdns-broken exits 1" '[ "$rc" = 1 ]'
ok "first broken run stays below threshold" '[ "$(spooled)" = 0 ] && [ "$(state_field streak)" = 1 ]'
run fail ok
ok "second broken run alerts once" '[ "$(spooled)" = 1 ]'
ok "alert state is remembered" '[ "$(state_field alerted)" = True ]'
REC="$(find "$CCC_PUSH_SPOOL" -maxdepth 1 -name 'dns-health-*.json' | head -1)"
ok "alert record is a notice dns-health event" 'python3 -c "import json,sys;d=json.load(open(sys.argv[1]));assert d[\"event\"]==\"dns-health\" and d[\"tier\"]==\"notice\" and d[\"node\"]==\"testnode\"" "$REC"'
ok "alert text names the fix" 'grep -q "Tailscale" "$REC" && grep -q "시스템 DNS 고장" "$REC"'
ok "alert reports the VPN DNS probe" 'grep -q "무응답" "$REC"'
ok "alert record is owner-only" '[ "$(stat -c %a "$REC")" = 600 ]'
ok "no partial tmp file left in the spool" '[ -z "$(find "$CCC_PUSH_SPOOL" -maxdepth 1 -name ".*.tmp")" ]'
run fail ok; run fail ok
ok "persisting breakage does not re-alert" '[ "$(spooled)" = 1 ]'
ok "streak keeps counting while broken" '[ "$(state_field streak)" = 4 ]'

# recovery: one recovery notice, alert flag cleared
run ok ok
ok "recovery sends one notice" '[ "$(spooled)" = 2 ] && grep -lq "시스템 DNS 복구" "$CCC_PUSH_SPOOL"/dns-health-*.json'
ok "recovery clears the alert flag" '[ "$(state_field alerted)" = False ]'
run ok ok
ok "steady healthy runs stay quiet" '[ "$(spooled)" = 2 ]'

# threshold override
rm -f "$CCC_PUSH_SPOOL"/dns-health-*.json "$STATE"
CCC_DNS_WATCH_THRESHOLD=1 CCC_DNS_WATCH_TEST_SYS=fail CCC_DNS_WATCH_TEST_RAW=ok bash "$WATCH" >/dev/null 2>&1
ok "threshold 1 alerts on the first broken run" '[ "$(spooled)" = 1 ]'

# missing spool: no crash, logged as no-spool, alert stays pending
rm -rf "$CCC_PUSH_SPOOL" "$STATE"
CCC_DNS_WATCH_THRESHOLD=1 CCC_DNS_WATCH_TEST_SYS=fail CCC_DNS_WATCH_TEST_RAW=ok bash "$WATCH" >/dev/null 2>&1; rc=$?
ok "missing spool still exits with the state code" '[ "$rc" = 1 ]'
ok "missing spool is logged" 'grep -q "alert=no-spool" "$LOG"'
ok "undelivered alert stays pending" '[ "$(state_field alerted)" = False ]'

# corrupt state file is tolerated
printf 'not json' > "$STATE"
run ok ok; rc=$?
ok "corrupt state is tolerated" '[ "$rc" = 0 ] && [ "$(state_field status)" = ok ]'

# usage errors
bash "$WATCH" --bogus >/dev/null 2>&1; rc=$?
ok "unknown arg exits 2" '[ "$rc" = 2 ]'
CCC_DNS_WATCH_THRESHOLD=0 bash "$WATCH" >/dev/null 2>&1; rc=$?
ok "threshold 0 is rejected" '[ "$rc" = 2 ]'
ok "--help prints the header" 'bash "$WATCH" --help | grep -q "sysdns-broken"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
