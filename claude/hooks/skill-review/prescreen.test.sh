#!/usr/bin/env bash
# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
# Tests for prescreen.py (#2183) — hermetic: the intake review handler is a
# stub that answers by skill name, no provider/network calls. Pins: dry-run
# calls nothing; reject+blocker is archived with a manifest row and can be
# restored through pending_expire.py; approve/revise/reject-without-blocker
# stay with prescreen.json; a failing handler never moves a draft (fail-open);
# three consecutive failures stop the run; the per-run cap defers the rest;
# an already-screened draft is not re-reviewed; name collision with an
# installed skill is a deterministic blocker; max-per-run=0 is off.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PS="$HERE/prescreen.py"
PE="$HERE/pending_expire.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
export HOME="$TMP/home"
export CCC_CLAUDE_DIR="$TMP/home/.claude"
STATE="$CCC_CLAUDE_DIR/state"; PENDING="$STATE/pending-skills"; ARCHIVE="$STATE/skill-autosave-archive"
SKILLS="$CCC_CLAUDE_DIR/skills"
mkdir -p "$PENDING" "$SKILLS"; chmod 700 "$STATE" "$PENDING"
export CCC_NODE=testnode
export CLAUDE_SKILLS_DIR="$SKILLS"

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# --- stub nexus docs (worker procedure section) -------------------------------
NEXUS="$TMP/nexus"; mkdir -p "$NEXUS/docs"
{ echo "# spec"; echo; echo "## Worker procedure"; echo; for i in $(seq 1 12); do echo "Step $i: apply rubric area in order and emit the verdict JSON only. Lorem ipsum dolor sit amet."; done; echo; echo "## Receipt projection"; echo "x"; } > "$NEXUS/docs/skills-intake-review.md"
export CCC_SKILL_PRESCREEN_NEXUS_DIR="$NEXUS"

# --- stub handler: verdict by skill name; records every call -------------------
HANDLER="$TMP/handler.sh"; CALLS="$TMP/calls.log"; : > "$CALLS"; export CALLS
cat > "$HANDLER" <<'SH'
#!/usr/bin/env bash
task="$(cat)"
name="$(printf '%s' "$task" | jq -r '.payload.skillName')"
intent="$(printf '%s' "$task" | jq -r '.intent')"
proc_len="$(printf '%s' "$task" | jq -r '.payload.workerProcedure | length')"
files="$(printf '%s' "$task" | jq -r '.payload.skillFiles | length')"
printf '%s intent=%s proc=%s files=%s\n' "$name" "$intent" "$proc_len" "$files" >> "$CALLS"
case "$name" in
  *fail*) echo "stub: provider down" >&2; exit 1 ;;
  *garbage*) echo "not json at all"; exit 0 ;;
  *bad*) v='{"verdict":"reject","findings":[{"severity":"blocker","area":"safety","note":"prints a token"}],"evidence":[{"kind":"grep","detail":"x"}]}' ;;
  *meh*) v='{"verdict":"revise","findings":[{"severity":"major","area":"spec","note":"vague trigger"}]}' ;;
  *soft*) v='{"verdict":"reject","findings":[{"severity":"minor","area":"quality","note":"weak"}]}' ;;
  *) v='{"verdict":"approve","findings":[]}' ;;
esac
jq -nc --argjson o "$v" --arg n "$name" '{summary:"stub", output:($o + {skillName:$n, review_agent:"stub", review_model:"stub-1", reviewer_node:"testnode"})}'
SH
chmod +x "$HANDLER"
export CCC_SKILL_PRESCREEN_HANDLER="$HANDLER"

draft() {  # <dir-name> <skill-name> <staged_at ISO>
  mkdir -p "$PENDING/$1"
  printf -- '---\nname: %s\ndescription: Use when testing %s.\n---\n# %s\n\nbody\n' "$2" "$2" "$2" > "$PENDING/$1/SKILL.md"
  printf '{"name":"%s","staged_at":"%s","status":"pending"}\n' "$2" "$3" > "$PENDING/$1/meta.json"
}
draft "20260901-000000-aaaa-good"    "good-skill"      "2026-09-01T00:00:00Z"
draft "20260902-000000-bbbb-bad"     "bad-skill"       "2026-09-02T00:00:00Z"
draft "20260903-000000-cccc-meh"     "meh-skill"       "2026-09-03T00:00:00Z"
draft "20260904-000000-dddd-soft"    "soft-reject"     "2026-09-04T00:00:00Z"
draft "20260905-000000-eeee-fail"    "fail-skill"      "2026-09-05T00:00:00Z"
draft "20260906-000000-ffff-dup"     "already-there"   "2026-09-06T00:00:00Z"
draft "20260907-000000-gggg-dup2"    "good-skill"      "2026-09-07T00:00:00Z"
# decided + proposal entries must be ignored
draft "20260801-000000-hhhh-done.approved-20260803000000" "done" "2026-08-01T00:00:00Z"
draft "20260801-000000-iiii-prop" "prop" "2026-08-01T00:00:00Z"; : > "$PENDING/20260801-000000-iiii-prop/proposal.json"
# installed skill that collides with the "dup" draft
mkdir -p "$SKILLS/already-there"; printf -- '---\nname: already-there\ndescription: Installed.\n---\n# x\n' > "$SKILLS/already-there/SKILL.md"

run_ps() { python3 "$PS" "$@"; }
NOW=1791417600  # 2026-10-08T00:00:00Z

# --- 1) dry-run: lists candidates, calls nothing ---------------------------------
out="$(run_ps run --dry-run --now "$NOW")"; rc=$?
ok "dry-run exits 0" '[ "$rc" = 0 ]'
ok "dry-run status" 'printf "%s" "$out" | jq -e ".status == \"dry-run\" and .dry_run == true" >/dev/null'
ok "dry-run lists the 7 undecided drafts oldest first" '[ "$(printf "%s" "$out" | jq -r ".would_review | length")" = 7 ] && [ "$(printf "%s" "$out" | jq -r ".would_review[0].name")" = "good-skill" ]'
ok "dry-run skips decided and proposal entries" 'printf "%s" "$out" | jq -e ".skipped.decided == 1 and .skipped[\"incremental-proposal\"] == 1" >/dev/null'
ok "dry-run called no handler" '[ ! -s "$CALLS" ]'
ok "dry-run wrote no prescreen.json" '! ls "$PENDING"/*/prescreen.json >/dev/null 2>&1'

# --- 2) off switch ------------------------------------------------------------------
out="$(CCC_SKILL_PRESCREEN_MAX_PER_RUN=0 run_ps run --now "$NOW")"
ok "max-per-run=0 is off and calls nothing" 'printf "%s" "$out" | jq -e ".status == \"off\"" >/dev/null && [ ! -s "$CALLS" ]'

# --- 3) missing handler / procedure: fail-open, nothing moves --------------------------
out="$(CCC_SKILL_PRESCREEN_HANDLER="$TMP/nope.sh" run_ps run --now "$NOW")"
ok "missing handler reported, nothing moved" 'printf "%s" "$out" | jq -e ".status == \"handler-unavailable\"" >/dev/null && [ -d "$PENDING/20260902-000000-bbbb-bad" ]'
out="$(CCC_SKILL_PRESCREEN_NEXUS_DIR="$TMP/nonexus" run_ps run --now "$NOW")"
ok "missing procedure reported, nothing moved" 'printf "%s" "$out" | jq -e ".status == \"procedure-unavailable\"" >/dev/null && [ -d "$PENDING/20260902-000000-bbbb-bad" ]'

# --- 4) live run with cap=3: oldest three reviewed, rest deferred ---------------------------
: > "$CALLS"
out="$(CCC_SKILL_PRESCREEN_MAX_PER_RUN=3 run_ps run --now "$NOW")"; rc=$?
ok "capped run exits 0" '[ "$rc" = 0 ]'
ok "capped run reviewed 3, deferred 4" 'printf "%s" "$out" | jq -e ".reviewed == 3 and .deferred == 4 and .status == \"reviewed\"" >/dev/null'
ok "handler got exactly 3 calls with the intake intent and procedure" '[ "$(wc -l < "$CALLS" | tr -d " ")" = 3 ] && grep -q "intent=skills-intake-review proc=[0-9][0-9][0-9]" "$CALLS" && grep -q "files=1" "$CALLS"'
ok "approve stays with prescreen.json" 'jq -e ".verdict == \"approve\" and .status == \"ok\" and .max_severity == \"info\" and .review_agent == \"stub\"" "$PENDING/20260901-000000-aaaa-good/prescreen.json" >/dev/null'
ok "reject+blocker left the queue" '[ ! -e "$PENDING/20260902-000000-bbbb-bad" ]'
ADIR="$ARCHIVE/prescreen-reject-20261008"
ok "reject+blocker archived intact with its prescreen.json" '[ -f "$ADIR/20260902-000000-bbbb-bad/SKILL.md" ] && jq -e ".verdict == \"reject\" and .max_severity == \"blocker\"" "$ADIR/20260902-000000-bbbb-bad/prescreen.json" >/dev/null'
ok "manifest row names the blocker" 'jq -e "select(.name == \"20260902-000000-bbbb-bad\") | .verdict == \"reject\" and (.blockers[0] | test(\"token\")) and (.reason | test(\"#2183\"))" "$ADIR/manifest.jsonl" >/dev/null'
ok "revise stays with findings" 'jq -e ".verdict == \"revise\" and .max_severity == \"major\" and (.findings | length) == 1" "$PENDING/20260903-000000-cccc-meh/prescreen.json" >/dev/null'
ok "summary counts kept verdicts" 'printf "%s" "$out" | jq -e ".kept.approve == 1 and .kept.revise == 1 and (.archived | length) == 1" >/dev/null'
ok "prescreen-last.json written owner-only" '[ -f "$STATE/prescreen-last.json" ] && [ "$(stat -c %a "$STATE/prescreen-last.json")" = 600 ]'

# --- 5) second run: already-screened skipped; reject-without-blocker stays; error is fail-open
: > "$CALLS"
out="$(run_ps run --now "$NOW")"
ok "already-screened drafts not re-reviewed" 'printf "%s" "$out" | jq -e ".skipped[\"already-screened\"] == 2" >/dev/null'
ok "reject without blocker stays in the queue" '[ -d "$PENDING/20260904-000000-dddd-soft" ] && jq -e ".verdict == \"reject\" and .max_severity == \"minor\"" "$PENDING/20260904-000000-dddd-soft/prescreen.json" >/dev/null'
ok "handler failure recorded as error, draft kept" '[ -d "$PENDING/20260905-000000-eeee-fail" ] && jq -e ".status == \"error\" and (.error | startswith(\"handler-exit\"))" "$PENDING/20260905-000000-eeee-fail/prescreen.json" >/dev/null'
ok "installed-name collision is a deterministic blocker (no handler call)" '[ ! -e "$PENDING/20260906-000000-ffff-dup" ] && ! grep -q "already-there" "$CALLS" && jq -e "select(.name == \"20260906-000000-ffff-dup\") | .blockers[0] | test(\"installed skill\")" "$ADIR/manifest.jsonl" >/dev/null'
ok "duplicate pending name marked revise and kept" 'jq -e ".verdict == \"revise\" and .source == \"deterministic\"" "$PENDING/20260907-000000-gggg-dup2/prescreen.json" >/dev/null'
ok "errored draft is retried on the next run (not already-screened)" 'printf "%s" "$out" | jq -e ".errors == 1" >/dev/null'

# --- 6) restore an archived reject through pending_expire.py -------------------------
out="$(python3 "$PE" restore 20260902-000000-bbbb-bad)"; rc=$?
ok "restore brings a prescreen-reject back" '[ "$rc" = 0 ] && [ -f "$PENDING/20260902-000000-bbbb-bad/SKILL.md" ]'
ok "restored draft is screened again next run (prescreen.json still ok+same tree => skipped)" 'jq -e ".status == \"ok\"" "$PENDING/20260902-000000-bbbb-bad/prescreen.json" >/dev/null'

# --- 7) reviewer down: three consecutive failures stop the run ------------------------
STATE2="$TMP/s2"; mkdir -p "$STATE2/pending-skills"
for i in 1 2 3 4; do d="$STATE2/pending-skills/2026090$i-000000-f$i-fail$i"; mkdir -p "$d"; printf -- '---\nname: fail-%s\ndescription: x.\n---\n# x\n' "$i" > "$d/SKILL.md"; done
: > "$CALLS"
out="$(CCC_STATE_DIR="$STATE2" run_ps run --now "$NOW")"
ok "three consecutive errors stop the run" 'printf "%s" "$out" | jq -e ".status == \"reviewer-down\" and .errors == 3" >/dev/null && [ "$(wc -l < "$CALLS" | tr -d " ")" = 3 ]'
ok "nothing moved while the reviewer is down" '[ "$(ls -d "$STATE2"/pending-skills/*/ | wc -l | tr -d " ")" = 4 ]'

# --- 8) status is read-only and summarises the queue --------------------------------------
out="$(run_ps status)"
ok "status reports last run and verdict counts" 'printf "%s" "$out" | jq -e ".last.status == \"reviewed\" and .queue.approve == 1 and .queue.revise == 2 and .queue.reject == 2 and .queue.error == 1" >/dev/null'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
