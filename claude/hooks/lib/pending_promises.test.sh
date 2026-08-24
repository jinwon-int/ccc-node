#!/usr/bin/env bash
# Unit tests for lib/pending_promises.py (#1258) — the SessionStart renderer for
# still-owed external-wait promises. Pins the two contracts the loader depends
# on: silence when nothing is owed (so load-memory.sh output stays
# byte-identical), and fail-open on every malformed input.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MOD="$HERE/pending_promises.py"
pass=0; fail=0
BASE_TMP="${TMPDIR:-/tmp}"; mkdir -p "$BASE_TMP"
TMP="$(mktemp -d "$BASE_TMP/ccc-pending-promises-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

w() { printf '%s' "$2" > "$TMP/$1.json"; }

# ---- silence contract -------------------------------------------------------
# These four are the whole reason the block can default to ON.
w empty '{}'
out="$(python3 "$MOD" "$TMP/empty.json")"
ok "empty registry prints nothing" '[ -z "$out" ]'

out="$(python3 "$MOD" "$TMP/does-not-exist.json")"
ok "missing registry prints nothing" '[ -z "$out" ]'

w broken 'not json at all{{{'
out="$(python3 "$MOD" "$TMP/broken.json")"
rc=$?
ok "malformed registry prints nothing" '[ -z "$out" ]'
ok "malformed registry still exits 0 (fail-open)" '[ "$rc" = 0 ]'

w wrongtype '"a bare string"'
out="$(python3 "$MOD" "$TMP/wrongtype.json")"
ok "non-object registry prints nothing" '[ -z "$out" ]'

# A terminal wait that DID continue is settled — reporting it would be noise.
w resumed '{"w1":{"wait_id":"w1","state":"completed","repo":"o/r","pr_number":1,
  "wake":{"state":"done","resumed":true}}}'
out="$(python3 "$MOD" "$TMP/resumed.json")"
ok "fulfilled promise prints nothing" '[ -z "$out" ]'

# resumed missing (pre-field record) must not be guessed into an alarm.
w legacy '{"w1":{"wait_id":"w1","state":"completed","repo":"o/r","pr_number":1,
  "wake":{"state":"done"}}}'
out="$(python3 "$MOD" "$TMP/legacy.json")"
ok "record predating resumed= is not reported as dropped" '[ -z "$out" ]'

# ---- monitoring section -----------------------------------------------------
w mon '{"w1":{"wait_id":"w1","state":"monitoring","repo":"jinwon-int/ccc-node",
  "pr_number":1257,"head_sha":"abcdef1234567890","summary":"path detection fix",
  "created_at":"2026-08-24T00:00:00Z"}}'
out="$(python3 "$MOD" "$TMP/mon.json")"
ok "monitoring wait is reported" 'grep -q "아직 대기 중" <<<"$out"'
ok "monitoring row carries repo#pr" 'grep -q "jinwon-int/ccc-node#1257" <<<"$out"'
ok "monitoring row shortens the sha to 8" 'grep -q "abcdef12\`" <<<"$out"'
ok "monitoring row carries the summary" 'grep -q "path detection fix" <<<"$out"'
ok "monitoring row carries the wait_id" 'grep -q "\[w1\]" <<<"$out"'
ok "monitoring section omits the skip-reason label" '! grep -q "skip:" <<<"$out"'

# ---- dropped section --------------------------------------------------------
# session_moved is precisely what AUTO_NEW_SESSION_AFTER_HOURS=4 produces.
w drop '{"w1":{"wait_id":"w1","state":"completed","repo":"o/r","pr_number":9,
  "head_sha":"deadbeefcafe","summary":"green, needs merge",
  "wake":{"state":"done","resumed":false,"skip_reason":"session_moved"},
  "created_at":"2026-08-24T00:00:00Z"}}'
out="$(python3 "$MOD" "$TMP/drop.json")"
ok "dropped promise is reported" 'grep -q "이어가지 못한 약속" <<<"$out"'
ok "dropped row surfaces skip_reason" 'grep -q "skip: session_moved" <<<"$out"'
ok "dropped row carries repo#pr" 'grep -q "o/r#9" <<<"$out"'

# ---- both sections ----------------------------------------------------------
w both '{"a":{"wait_id":"a","state":"monitoring","repo":"o/r","pr_number":1,
  "created_at":"2026-08-24T00:00:00Z"},
 "b":{"wait_id":"b","state":"completed","repo":"o/r","pr_number":2,
  "wake":{"state":"done","resumed":false,"skip_reason":"daily_cap"},
  "created_at":"2026-08-24T01:00:00Z"}}'
out="$(python3 "$MOD" "$TMP/both.json")"
ok "both sections render together" \
  'grep -q "아직 대기 중" <<<"$out" && grep -q "이어가지 못한 약속" <<<"$out"'

# ---- list-shaped registry ---------------------------------------------------
w listform '[{"wait_id":"w1","state":"monitoring","repo":"o/r","pr_number":3}]'
out="$(python3 "$MOD" "$TMP/listform.json")"
ok "list-shaped registry is accepted" 'grep -q "o/r#3" <<<"$out"'

# ---- row cap ----------------------------------------------------------------
python3 - "$TMP/many.json" <<'PY'
import json, sys
recs = {
    f"w{i}": {
        "wait_id": f"w{i}", "state": "monitoring", "repo": "o/r",
        "pr_number": i, "created_at": f"2026-08-24T00:{i:02d}:00Z",
    }
    for i in range(9)
}
json.dump(recs, open(sys.argv[1], "w"))
PY
out="$(python3 "$MOD" "$TMP/many.json")"
n="$(grep -c '^- o/r#' <<<"$out")"
ok "row count is capped at 5 per section" '[ "$n" = 5 ]'
ok "overflow is disclosed rather than silently dropped" 'grep -q "외 4건" <<<"$out"'

# ---- byte cap ---------------------------------------------------------------
n="$(python3 "$MOD" "$TMP/many.json" --max-bytes 80 | wc -c)"
ok "--max-bytes caps the block (newline included)" '[ "$n" -le 81 ]'
bad="$(python3 "$MOD" "$TMP/many.json" --max-bytes 60 | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1; echo $?)"
ok "byte-capped output stays valid UTF-8" '[ "$bad" = 0 ]'

# ---- env resolution ---------------------------------------------------------
mkdir -p "$TMP/home"
cp "$TMP/mon.json" "$TMP/home/waits.json"
out="$(CCC_EXTERNAL_WAIT_HOME="$TMP/home" python3 "$MOD")"
ok "path resolves from CCC_EXTERNAL_WAIT_HOME when argv is empty" \
  'grep -q "jinwon-int/ccc-node#1257" <<<"$out"'

out="$(env -u CCC_EXTERNAL_WAIT_HOME python3 "$MOD")"
ok "no argv and no env prints nothing" '[ -z "$out" ]'

# ---- no transcript leakage --------------------------------------------------
# summary is body-free by construction at registration (#740); pin that this
# renderer never reaches for any other free-text field.
w leak '{"w1":{"wait_id":"w1","state":"monitoring","repo":"o/r","pr_number":1,
  "summary":"ok","body":"SECRET-TRANSCRIPT","notes":"SECRET-NOTES"}}'
out="$(python3 "$MOD" "$TMP/leak.json")"
ok "unknown free-text fields are not rendered" '! grep -q "SECRET" <<<"$out"'

# The summary line format is load-bearing: validate-harness.sh's
# suite_summary() greps for exactly ^PASS=<n> FAIL=<n>$, so a lowercase variant
# reads as a suite that asserted nothing.
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
