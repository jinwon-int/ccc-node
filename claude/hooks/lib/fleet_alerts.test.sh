#!/usr/bin/env bash
# Unit tests for lib/fleet_alerts.py — the SessionStart renderer for fleet alert
# issues nobody has answered. Pins the contracts load-memory.sh depends on:
# silence when nothing is unacknowledged (so the loader's output stays
# byte-identical), fail-open on every malformed input, and the human/bot split
# that decides whether an alert nags at all.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MOD="$HERE/fleet_alerts.py"
pass=0; fail=0
BASE_TMP="${TMPDIR:-/tmp}"; mkdir -p "$BASE_TMP"
TMP="$(mktemp -d "$BASE_TMP/ccc-fleet-alerts-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

w() { printf '%s' "$2" > "$TMP/$1.json"; }
# Ages are relative so the suite never rots against a wall clock.
iso() { python3 -c "
import sys
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) - timedelta(hours=float(sys.argv[1]))).isoformat())
" "$1"; }

OLD="$(iso 96)"    # the #5069 shape: four days unanswered
FRESH="$(iso 1)"   # under MIN_AGE_HOURS

alert() { # alert <created> [last_human] [state] [bots]
  python3 -c "
import json, sys
rec = {'repo': 'jinwon-int/seoyoon-family-wiki', 'number': 5069,
       'title': 'wiki-log-rotate 실패 — 로그 이관이 멈춰 있습니다',
       'state': sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else 'open',
       'created_at': sys.argv[1],
       'bot_comments': int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else 4,
       'last_human_comment_at': sys.argv[2] or None}
print(json.dumps({'alerts': [rec]}, ensure_ascii=False))
" "$1" "${2:-}" "${3:-}" "${4:-}"
}

# ---- silence contract -------------------------------------------------------
# These are the whole reason the block can default to ON.
w empty '{"alerts": []}'
out="$(python3 "$MOD" "$TMP/empty.json")"
ok "empty cache prints nothing" '[ -z "$out" ]'

out="$(python3 "$MOD" "$TMP/does-not-exist.json")"
ok "missing cache prints nothing" '[ -z "$out" ]'

w broken '{not json'
out="$(python3 "$MOD" "$TMP/broken.json")"
ok "unparseable cache prints nothing" '[ -z "$out" ]'

w wrongshape '{"alerts": "not-a-list"}'
out="$(python3 "$MOD" "$TMP/wrongshape.json")"
ok "wrong-typed alerts prints nothing" '[ -z "$out" ]'

# Exit status, not just empty stdout. A raised exception also yields empty
# stdout, so an output-only assertion cannot tell fail-open from a crash. Caught
# by mutation: removing the `except` in main() left an output-only suite green.
# These run after the fixtures exist so they exercise malformed CONTENT, not
# just a missing file.
python3 "$MOD" "$TMP/does-not-exist.json" >/dev/null 2>&1; rc=$?
ok "missing cache exits 0 (fail-open, not a crash)" '[ "$rc" = 0 ]'
python3 "$MOD" "$TMP/broken.json" >/dev/null 2>&1; rc=$?
ok "unparseable cache exits 0" '[ "$rc" = 0 ]'
python3 "$MOD" "$TMP/wrongshape.json" >/dev/null 2>&1; rc=$?
ok "wrong-typed alerts exits 0" '[ "$rc" = 0 ]'

w norows '{"alerts": [{"repo": "x/y"}]}'
out="$(python3 "$MOD" "$TMP/norows.json")"
ok "alert with no created_at prints nothing" '[ -z "$out" ]'

# ---- the acknowledgement split ---------------------------------------------
# This is the judgement the module exists to make. A human voice means someone
# is on it; only-bots is the case that went unread for four days.
alert "$OLD" "" > "$TMP/unanswered.json"
out="$(python3 "$MOD" "$TMP/unanswered.json")"
ok "bot-only alert is reported" 'printf "%s" "$out" | grep -q "5069"'
ok "reported alert shows its age" 'printf "%s" "$out" | grep -q "4일 경과"'
ok "reported alert shows the bot comment count" 'printf "%s" "$out" | grep -q "봇 코멘트 4건"'

alert "$OLD" "$(iso 2)" > "$TMP/answered.json"
out="$(python3 "$MOD" "$TMP/answered.json")"
ok "alert with a human comment is silent" '[ -z "$out" ]'

alert "$FRESH" "" > "$TMP/fresh.json"
out="$(python3 "$MOD" "$TMP/fresh.json")"
ok "alert younger than the grace window is silent" '[ -z "$out" ]'

alert "$OLD" "" closed > "$TMP/closed.json"
out="$(python3 "$MOD" "$TMP/closed.json")"
ok "closed alert is silent" '[ -z "$out" ]'

# ---- argv parsing -----------------------------------------------------------
# Regression: the positional scan used to claim `--max-bytes`'s VALUE as the
# cache path, so the module went silent under the loader's exact invocation
# while rendering fine from the command line. Caught end-to-end, not by a unit.
out="$(CCC_FLEET_ALERTS_CACHE="$TMP/unanswered.json" python3 "$MOD" --max-bytes 1024)"
ok "option value is not mistaken for the cache path" 'printf "%s" "$out" | grep -q "5069"'
out="$(python3 "$MOD" "$TMP/unanswered.json" --max-bytes 1024)"
ok "explicit path still wins over the env default" 'printf "%s" "$out" | grep -q "5069"'

# ---- identity + bounding ----------------------------------------------------
python3 -c "
import json
print(json.dumps({'alerts': [{'repo': 'x/y', 'number': 0, 'title': 't',
                              'state': 'open', 'created_at': '2020-01-01T00:00:00Z'}]}))
" > "$TMP/zero.json"
out="$(python3 "$MOD" "$TMP/zero.json")"
ok "issue number 0 still renders an identity" 'printf "%s" "$out" | grep -q "x/y#0"'

python3 -c "
import json
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
rows = [{'repo': 'x/y', 'number': n, 'title': 'alert %d' % n, 'state': 'open',
         'created_at': (now - timedelta(hours=100 + n)).isoformat()}
        for n in range(9)]
print(json.dumps({'alerts': rows}))
" > "$TMP/many.json"
out="$(python3 "$MOD" "$TMP/many.json")"
ok "row count is bounded" '[ "$(printf "%s" "$out" | grep -c "^- x/y#")" = 6 ]'
ok "hidden rows are counted, not dropped silently" 'printf "%s" "$out" | grep -q "외 3건"'
ok "oldest alert sorts first" 'printf "%s" "$out" | grep -m1 "^- x/y#" | grep -q "x/y#8"'

out="$(python3 "$MOD" "$TMP/many.json" --max-bytes 80)"
ok "--max-bytes truncates" '[ "$(printf "%s" "$out" | wc -c)" -le 100 ]'
ok "--max-bytes marks the truncation" 'printf "%s" "$out" | grep -q "truncated"'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
