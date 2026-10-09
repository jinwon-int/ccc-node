#!/usr/bin/env bash
# Hermetic tests for skills/gh-pr-flow/merge-queue-watch.sh. A stub `gh` on
# PATH replays scripted answers per call kind; no network. Covers: merged,
# evicted (with failed group run + jobs named), closed, never-enqueued,
# timeout, usage errors, and the JSON verdict shape.
# shellcheck disable=SC2034  # test variables are read inside the eval'd ok() conditions
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HERE/merge-queue-watch.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# --- stub gh: answers come from $STUB_DIR/<kind>.<n> files, consumed in order
mkdir -p "$TMP/bin" "$TMP/stub"
cat > "$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
# kinds: prview (gh pr view), queue (gh api graphql), runs (gh run list), jobs (gh api .../jobs)
kind=""
case "$1 $2" in
  "pr view") kind=prview ;;
  "api graphql") kind=queue ;;
  "run list") kind=runs ;;
  "api repos"*) kind=jobs ;;
esac
[ -n "$kind" ] || { echo "stub gh: unexpected: $*" >&2; exit 1; }
cnt_file="$STUB_DIR/.count.$kind"; n=$(cat "$cnt_file" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$cnt_file"
f="$STUB_DIR/$kind.$n"; [ -f "$f" ] || f="$STUB_DIR/$kind.last"
[ -f "$f" ] || exit 0
cat "$f"
EOF
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH"

reset_stub() { rm -rf "$TMP/stub"; mkdir -p "$TMP/stub"; export STUB_DIR="$TMP/stub"; }

# 1. merged on the second poll
reset_stub
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.1"
printf 'MERGED UNKNOWN abc123abc123 deadbeefdead\n' > "$STUB_DIR/prview.last"
printf 'AWAITING_CHECKS 1\n' > "$STUB_DIR/queue.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0 --timeout 60)"; rc=$?
ok "merged → exit 0 with the merge sha" '[ "$rc" = 0 ] && printf "%s" "$out" | grep -q "merged pr=7 merge=deadbeefdead"'
ok "the queued transition was reported once" '[ "$(printf "%s\n" "$out" | grep -c "queued pr=7 entry=AWAITING_CHECKS 1")" = 1 ]'

# 2. evicted: entry disappears while the PR stays OPEN; failed group run + jobs are named
reset_stub
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.last"
printf 'AWAITING_CHECKS 1\n' > "$STUB_DIR/queue.1"
: > "$STUB_DIR/queue.last"
printf '36990888999 harness-ci failure\n' > "$STUB_DIR/runs.last"
printf '110786586317 validate-harness-shard (4)\n110788510595 validate-harness\n' > "$STUB_DIR/jobs.last"
out="$(bash "$W" --repo o/r --pr 2113 --interval 0 --timeout 60)"; rc=$?
ok "eviction → exit 10 naming the failed run and jobs" '[ "$rc" = 10 ] && printf "%s" "$out" | grep -q "evicted pr=2113" && printf "%s" "$out" | grep -q "run=36990888999 harness-ci=failure jobs=\[110786586317 validate-harness-shard (4);110788510595 validate-harness\]"'

# 2b. race (#2208): the queue squash-lands between the PR read (OPEN) and the
#     queue read (empty). The settle re-read sees MERGED → merged, never evicted.
reset_stub
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.1"
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.2"
printf 'MERGED UNKNOWN abc123abc123 99d2c8946b00\n' > "$STUB_DIR/prview.last"
printf 'QUEUED 1\n' > "$STUB_DIR/queue.1"
: > "$STUB_DIR/queue.last"
out="$(bash "$W" --repo o/r --pr 154 --interval 0 --timeout 60)"; rc=$?
ok "merged between PR read and queue read → exit 0 merged, not evicted" '[ "$rc" = 0 ] && printf "%s" "$out" | grep -q "merged pr=154 merge=99d2c8946b00" && ! printf "%s" "$out" | grep -q evicted'

# 2c. same race on the very first poll → merged, not not-enqueued
reset_stub
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.1"
printf 'MERGED UNKNOWN abc123abc123 deadbeefdead\n' > "$STUB_DIR/prview.last"
: > "$STUB_DIR/queue.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0)"; rc=$?
ok "merged before the first queue read → exit 0, not exit 12" '[ "$rc" = 0 ] && printf "%s" "$out" | grep -q "merged pr=7"'

# 2d. a transient empty queue answer while still queued → keep watching
reset_stub
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.1"
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.2"
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.3"
printf 'MERGED UNKNOWN abc123abc123 deadbeefdead\n' > "$STUB_DIR/prview.last"
printf 'QUEUED 1\n' > "$STUB_DIR/queue.1"
: > "$STUB_DIR/queue.2"
printf 'AWAITING_CHECKS 1\n' > "$STUB_DIR/queue.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0 --timeout 60)"; rc=$?
ok "entry missing once then back → not evicted, ends merged" '[ "$rc" = 0 ] && ! printf "%s" "$out" | grep -q evicted'

# 2e. eviction is still reported after the settle window, and only then
reset_stub
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.last"
printf 'QUEUED 1\n' > "$STUB_DIR/queue.1"
: > "$STUB_DIR/queue.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0 --settle-tries 3)"; rc=$?
ok "real eviction → exit 10 after re-reading the PR settle-tries times" '[ "$rc" = 10 ] && [ "$(cat "$STUB_DIR/.count.prview")" = 5 ]'

# 2f. --settle-tries 0 restores the old immediate verdict
reset_stub
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.last"
printf 'QUEUED 1\n' > "$STUB_DIR/queue.1"
: > "$STUB_DIR/queue.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0 --settle-tries 0)"; rc=$?
ok "--settle-tries 0 → immediate evicted" '[ "$rc" = 10 ] && [ "$(cat "$STUB_DIR/.count.prview")" = 2 ]'

# 3. closed without merge
reset_stub
printf 'CLOSED UNKNOWN abc123abc123 -\n' > "$STUB_DIR/prview.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0)"; rc=$?
ok "closed unmerged → exit 11" '[ "$rc" = 11 ] && printf "%s" "$out" | grep -q "closed pr=7"'

# 4. never enqueued: no entry on the first poll
reset_stub
printf 'OPEN BLOCKED abc123abc123 -\n' > "$STUB_DIR/prview.last"
: > "$STUB_DIR/queue.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0)"; rc=$?
ok "no entry on the first poll → exit 12 (not-enqueued), never 'evicted'" '[ "$rc" = 12 ] && printf "%s" "$out" | grep -q "not-enqueued" && ! printf "%s" "$out" | grep -q evicted'

# 5. timeout while queued
reset_stub
printf 'OPEN CLEAN abc123abc123 -\n' > "$STUB_DIR/prview.last"
printf 'QUEUED 2\n' > "$STUB_DIR/queue.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 1 --timeout 1)"; rc=$?
ok "timeout → exit 20 with the last entry" '[ "$rc" = 20 ] && printf "%s" "$out" | grep -q "timeout pr=7 entry=QUEUED 2"'

# 6. JSON verdict
reset_stub
printf 'MERGED UNKNOWN abc123abc123 deadbeefdead\n' > "$STUB_DIR/prview.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0 --json)"; rc=$?
ok "--json emits one verdict object" '[ "$rc" = 0 ] && printf "%s" "$out" | jq -e ".verdict == \"merged\" and .pr == 7 and .repo == \"o/r\"" >/dev/null'

# 7. usage errors
bash "$W" --pr 7 >/dev/null 2>&1; rc=$?
ok "missing --repo → exit 2" '[ "$rc" = 2 ]'
bash "$W" --repo o/r --pr x >/dev/null 2>&1; rc=$?
ok "non-numeric --pr → exit 2" '[ "$rc" = 2 ]'

# 8. gh failure → exit 3 (not a silent loop)
reset_stub
: > "$STUB_DIR/prview.last"
out="$(bash "$W" --repo o/r --pr 7 --interval 0 2>&1)"; rc=$?
ok "an empty gh answer twice in a row → exit 3" '[ "$rc" = 3 ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
