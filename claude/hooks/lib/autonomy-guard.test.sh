#!/usr/bin/env bash
# Tests for lib/autonomy-guard.sh — fleet kill-switch/dry-run resolver + the
# shared body-free autonomy ledger (#386). No network / provider calls.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=claude/hooks/lib/autonomy-guard.sh
. "$HERE/autonomy-guard.sh"

pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

STATE="$TMP/state"; mkdir -p "$STATE"
export CCC_STATE_DIR="$STATE"
LEDGER="$STATE/autonomy-ledger.jsonl"

# ---- ccc_autonomy_state: env precedence ------------------------------------
ok "env kill"        '[ "$(CCC_AUTONOMY=kill    ccc_autonomy_state)" = kill ]'
ok "env off=kill"    '[ "$(CCC_AUTONOMY=off     ccc_autonomy_state)" = kill ]'
ok "env dry-run"     '[ "$(CCC_AUTONOMY=dry-run ccc_autonomy_state)" = dry-run ]'
ok "env dry=dry-run" '[ "$(CCC_AUTONOMY=dry     ccc_autonomy_state)" = dry-run ]'
ok "env active"      '[ "$(CCC_AUTONOMY=active  ccc_autonomy_state)" = active ]'
ok "env unknown->active" '[ "$(CCC_AUTONOMY=wat ccc_autonomy_state)" = active ]'
ok "unset->active"   '[ "$(ccc_autonomy_state)" = active ]'

# ---- file switches (only when env is active/empty) -------------------------
touch "$STATE/autonomy.kill"
ok "file kill"            '[ "$(ccc_autonomy_state)" = kill ]'
ok "env active cannot override kill file" '[ "$(CCC_AUTONOMY=active ccc_autonomy_state)" = kill ]'
ok "env kill wins with no file too"       '[ "$(CCC_AUTONOMY=kill ccc_autonomy_state)" = kill ]'
rm -f "$STATE/autonomy.kill"
touch "$STATE/autonomy.dry-run"
ok "file dry-run"        '[ "$(ccc_autonomy_state)" = dry-run ]'
rm -f "$STATE/autonomy.dry-run"
ok "no file->active"     '[ "$(ccc_autonomy_state)" = active ]'

# ---- ccc_autonomy_record: append, body-free, owner-only --------------------
rm -f "$LEDGER"
ccc_autonomy_record distill kill sessionend
ccc_autonomy_record skill-autosave kill sweep
ok "ledger created"          '[ -f "$LEDGER" ]'
ok "ledger is 0600"          '[ "$(stat -c %a "$LEDGER" 2>/dev/null)" = 600 ]'
ok "two records appended"    '[ "$(wc -l < "$LEDGER" | tr -d " ")" = 2 ]'
ok "record has layer"        'grep -q "\"layer\":\"distill\"" "$LEDGER"'
ok "record has state"        'grep -q "\"state\":\"kill\"" "$LEDGER"'
ok "record has detail"       'grep -q "\"detail\":\"sweep\"" "$LEDGER"'
ok "each line is one json object" 'while IFS= read -r l; do printf "%s" "$l" | jq -e . >/dev/null 2>&1 || exit 1; done < "$LEDGER"'

# dry-run state has a hyphen and must survive sanitization
ccc_autonomy_record autoinstall dry-run hook-manual
ok "dry-run state preserved" 'grep -q "\"state\":\"dry-run\"" "$LEDGER"'

# ---- sanitization: hostile detail cannot break the JSON --------------------
ccc_autonomy_record 'ev"il' 'k"ill' 'a","x":"pwned
newline'
ok "still valid json after hostile input" 'tail -1 "$LEDGER" | jq -e . >/dev/null'
ok "no injected key smuggled in"          '! tail -1 "$LEDGER" | jq -e ".x" >/dev/null 2>&1'

# ---- bounded to newest N ----------------------------------------------------
rm -f "$LEDGER"
i=0; while [ "$i" -lt 30 ]; do CCC_AUTONOMY_LEDGER_MAX=10 ccc_autonomy_record distill kill "t$i"; i=$((i+1)); done
ok "ledger bounded to max"   '[ "$(wc -l < "$LEDGER" | tr -d " ")" = 10 ]'
ok "bound keeps newest"      'grep -q "\"detail\":\"t29\"" "$LEDGER" && ! grep -q "\"detail\":\"t0\"" "$LEDGER"'

# ---- concurrency: parallel writers never corrupt / tear the ledger ----------
# Many layers write the one shared file; the append+trim must stay lock-safe.
rm -f "$LEDGER" "$LEDGER.lock"
i=0; while [ "$i" -lt 25 ]; do
  ( CCC_AUTONOMY_LEDGER_MAX=8 ccc_autonomy_record distill kill "p$i" ) &
  i=$((i+1))
done
wait
ok "concurrent writers: every line is valid json (no torn writes)" \
  'while IFS= read -r l; do printf "%s" "$l" | jq -e . >/dev/null 2>&1 || exit 1; done < "$LEDGER"'
ok "concurrent writers: bound is respected" \
  '[ "$(wc -l < "$LEDGER" | tr -d " ")" -le 8 ] && [ "$(wc -l < "$LEDGER" | tr -d " ")" -ge 1 ]'

# ---- fail-open: unwritable dir never affects the caller ---------------------
BAD="$TMP/bad"; : > "$BAD"   # a regular file where a dir is expected
rc=0; CCC_STATE_DIR="$BAD/nope" ccc_autonomy_record distill kill x || rc=$?
ok "record returns 0 even when dir unwritable" '[ "$rc" = 0 ]'

# ---- no umask/env leak into caller -----------------------------------------
before="$(umask)"
ccc_autonomy_record distill kill leaktest
ok "caller umask unchanged after record" '[ "$(umask)" = "$before" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
