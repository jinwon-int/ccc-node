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
# shellcheck disable=SC2034  # rc is read via eval inside ok()
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
# shellcheck disable=SC2034  # n is read via eval inside ok()
n="$(python3 "$MOD" "$TMP/many.json" --max-bytes 80 | wc -c)"
ok "--max-bytes caps the block (newline included)" '[ "$n" -le 81 ]'
# shellcheck disable=SC2034  # bad is read via eval inside ok()
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
# ---- #1408: superseded waits are not owed promises --------------------------
# Head moved → the older registration is superseded by a newer one for the same
# PR. The old record carries wake.state=done/resumed=false (its notification
# fired) but nobody owes it: the newer wait does. It must not surface.
w superseded '{"old":{"wait_id":"old1","state":"superseded","repo":"o/r","pr_number":7,"head_sha":"aaaaaaaa11","created_at":"2026-09-02T01:00:00Z",
  "wake":{"state":"done","resumed":false,"skip_reason":"non_terminal_rollup"}},
 "new":{"wait_id":"new1","state":"success","repo":"o/r","pr_number":7,"head_sha":"bbbbbbbb22","created_at":"2026-09-02T02:00:00Z",
  "wake":{"state":"done","resumed":true}}}'
out="$(python3 "$MOD" "$TMP/superseded.json")"
ok "superseded wait is not reported as dropped" '[ -z "$out" ]'

# A genuinely dropped promise that has NO newer wait for its PR still surfaces.
w dropped_only '{"d":{"wait_id":"d1","state":"failure","repo":"o/r","pr_number":8,"head_sha":"cccccccc33","created_at":"2026-09-02T01:00:00Z",
  "wake":{"state":"done","resumed":false,"skip_reason":"session_moved"}}}'
out="$(python3 "$MOD" "$TMP/dropped_only.json")"
ok "dropped promise without a newer wait still surfaces" 'grep -q "o/r#8" <<<"$out" && grep -q "session_moved" <<<"$out"'

# An older dropped record is hidden when a NEWER record for the same PR exists
# (whatever that newer record's own state is) — the newer one is the live promise.
w shadowed '{"a":{"wait_id":"a1","state":"failure","repo":"o/r","pr_number":9,"head_sha":"dddddddd44","created_at":"2026-09-02T01:00:00Z",
  "wake":{"state":"done","resumed":false,"skip_reason":"session_moved"}},
 "b":{"wait_id":"b1","state":"monitoring","repo":"o/r","pr_number":9,"head_sha":"eeeeeeee55","created_at":"2026-09-02T03:00:00Z"}}'
out="$(python3 "$MOD" "$TMP/shadowed.json")"
ok "older dropped record is hidden behind the newer wait for the same PR" '! grep -q "dddddddd" <<<"$out" && grep -q "아직 대기 중" <<<"$out" && grep -q "eeeeeeee" <<<"$out"'

# ---- #2730: PR-less superseded + manual ack ---------------------------------
# Measured shape (2026-11-10): a detached job ended `lost` with a dropped
# wake, then the same promise was re-registered and fulfilled (resumed=true).
# The older record has no (repo, pr) key, so the #1408 rule never hid it.
w detached_redo '{"ab9da4672ff4":{"wait_id":"ab9da4672ff4","source":"detached_job","state":"lost","repo":"","pr_number":null,
  "summary":"auto-distill TM-4477 플릿 11노드 설치 후 22:30·23:00 회차 관찰","created_at":"2026-11-09T13:13:47Z",
  "wake":{"state":"done","resumed":false,"skip_reason":"non_terminal_rollup"}},
 "51859192ca83":{"wait_id":"51859192ca83","source":"detached_job","state":"success","repo":"","pr_number":null,
  "summary":"auto-distill  TM-4477 플릿 11노드 설치 후 22:30·23:00 회차 관찰","created_at":"2026-11-09T13:31:32Z",
  "wake":{"state":"done","resumed":true}}}'
out="$(python3 "$MOD" "$TMP/detached_redo.json")"
ok "PR-less dropped record is hidden by a later resumed same-summary record" '[ -z "$out" ]'

# A later same-summary record that ALSO dropped must not hide the earlier one.
w detached_both_dropped '{"a":{"wait_id":"pa1","source":"detached_job","state":"lost","summary":"nightly compare",
  "created_at":"2026-11-09T01:00:00Z","wake":{"state":"done","resumed":false,"skip_reason":"non_terminal_rollup"}},
 "b":{"wait_id":"pb1","source":"detached_job","state":"failure","summary":"nightly compare",
  "created_at":"2026-11-09T02:00:00Z","wake":{"state":"done","resumed":false,"skip_reason":"daily_cap"}}}'
out="$(python3 "$MOD" "$TMP/detached_both_dropped.json")"
ok "a later dropped successor does not hide the earlier PR-less drop" 'grep -q "\[pa1\]" <<<"$out" && grep -q "\[pb1\]" <<<"$out"'

# The resumed record must be LATER; an earlier fulfilment says nothing about a newer drop.
w detached_older_ok '{"a":{"wait_id":"pc1","source":"detached_job","state":"success","summary":"nightly compare",
  "created_at":"2026-11-09T01:00:00Z","wake":{"state":"done","resumed":true}},
 "b":{"wait_id":"pd1","source":"detached_job","state":"lost","summary":"nightly compare",
  "created_at":"2026-11-09T02:00:00Z","wake":{"state":"done","resumed":false,"skip_reason":"non_terminal_rollup"}}}'
out="$(python3 "$MOD" "$TMP/detached_older_ok.json")"
ok "an earlier resumed record does not hide a newer PR-less drop" 'grep -q "\[pd1\]" <<<"$out"'

# Different source or a different summary is a different promise.
w detached_other '{"a":{"wait_id":"pe1","source":"detached_job","state":"lost","summary":"job A",
  "created_at":"2026-11-09T01:00:00Z","wake":{"state":"done","resumed":false,"skip_reason":"non_terminal_rollup"}},
 "b":{"wait_id":"pf1","source":"local_task","state":"success","summary":"job A",
  "created_at":"2026-11-09T02:00:00Z","wake":{"state":"done","resumed":true}},
 "c":{"wait_id":"pg1","source":"detached_job","state":"success","summary":"job B",
  "created_at":"2026-11-09T03:00:00Z","wake":{"state":"done","resumed":true}}}'
out="$(python3 "$MOD" "$TMP/detached_other.json")"
ok "summary match is scoped to the same source and exact summary" 'grep -q "\[pe1\]" <<<"$out"'

# An empty summary names nothing: two empty-summary records never pair up.
w detached_nosummary '{"a":{"wait_id":"ph1","source":"detached_job","state":"lost","summary":"",
  "created_at":"2026-11-09T01:00:00Z","wake":{"state":"done","resumed":false,"skip_reason":"non_terminal_rollup"}},
 "b":{"wait_id":"pi1","source":"detached_job","state":"success","summary":"",
  "created_at":"2026-11-09T02:00:00Z","wake":{"state":"done","resumed":true}}}'
out="$(python3 "$MOD" "$TMP/detached_nosummary.json")"
ok "empty summaries never supersede each other" 'grep -q "\[ph1\]" <<<"$out"'

# Measured shape: merge-queue wait whose cleanup was done by hand, then acked
# through `external_wait_cli ack` (wake.resumed=true + manual_ack).
w acked '{"9209cb9d049a":{"wait_id":"9209cb9d049a","source":"github_merge_queue","state":"merged","repo":"jinwon-int/a2a-nexus",
  "pr_number":2532,"head_sha":"bc81d214aaaaaaaa","created_at":"2026-11-08T22:00:00Z",
  "wake":{"state":"done","resumed":true,"skip_reason":"daily_cap","manual_ack":{"at":"2026-11-10T05:00:00Z","prior_resumed":false}}}}'
out="$(python3 "$MOD" "$TMP/acked.json")"
ok "manually acked promise is not reported" '[ -z "$out" ]'

# The dropped section tells the reader how to close a row they already fulfilled.
out="$(python3 "$MOD" "$TMP/dropped_only.json")"
ok "dropped section names the ack command" 'grep -q "external_wait_cli ack" <<<"$out"'

# ---- #2740: a wake whose delivery gave up -----------------------------------
# wake.state == "failed" had no reader: the owner was never notified and the
# record then vanished at retention. It must surface here, under its own
# heading ("the notice went out" would be untrue for it).
w failed '{"f1":{"wait_id":"f1","state":"success","repo":"o/r","pr_number":21,"head_sha":"dddddddd44",
  "summary":"merge when green","created_at":"2026-11-10T01:00:00Z",
  "wake":{"state":"failed","attempts":10,"resumed":false,"skip_reason":"notify_failed"}}}'
out="$(python3 "$MOD" "$TMP/failed.json")"
ok "#2740 a wake that gave up is reported" 'grep -q "o/r#21" <<<"$out" && grep -q "skip: notify_failed" <<<"$out"'
ok "#2740 it is listed under the undelivered heading, not the delivered one" \
  'grep -q "알림조차 전달되지 못한 약속" <<<"$out" && ! grep -q "알림은 갔으나" <<<"$out"'
ok "#2740 the undelivered heading names the ack command" 'grep -q "external_wait_cli ack" <<<"$out"'
w failed_acked '{"f1":{"wait_id":"f1","state":"success","repo":"o/r","pr_number":21,"head_sha":"dddddddd44",
  "created_at":"2026-11-10T01:00:00Z",
  "wake":{"state":"failed","attempts":10,"resumed":true,"skip_reason":"notify_failed","manual_ack":{"at":"2026-11-10T02:00:00Z"}}}}'
out="$(python3 "$MOD" "$TMP/failed_acked.json")"
ok "#2740 an acknowledged gave-up wake is silent" '[ -z "$out" ]'
w failed_legacy '{"f1":{"wait_id":"f1","state":"success","repo":"o/r","pr_number":21,
  "wake":{"state":"failed","attempts":10}}}'
out="$(python3 "$MOD" "$TMP/failed_legacy.json")"
ok "#2740 a pre-fix failed record without resumed= is not guessed into an alarm" '[ -z "$out" ]'
w both '{"d1":{"wait_id":"d1","state":"success","repo":"o/r","pr_number":30,"created_at":"2026-11-10T01:00:00Z",
  "wake":{"state":"done","resumed":false,"skip_reason":"session_moved"}},
 "f1":{"wait_id":"f1","state":"success","repo":"o/r","pr_number":31,"created_at":"2026-11-10T01:05:00Z",
  "wake":{"state":"failed","attempts":10,"resumed":false,"skip_reason":"notify_failed"}}}'
out="$(python3 "$MOD" "$TMP/both.json")"
ok "#2740 delivered and undelivered drops are listed under separate headings" \
  '[ "$(grep -c "알림은 갔으나\|알림조차" <<<"$out")" = 2 ] && grep -q "o/r#30" <<<"$out" && grep -q "o/r#31" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
