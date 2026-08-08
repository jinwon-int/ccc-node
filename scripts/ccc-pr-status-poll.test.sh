#!/usr/bin/env bash
# Tests for ccc-pr-status-poll.sh — hermetic: fake `gh`, temp state/spool dirs.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
POLL="$HERE/ccc-pr-status-poll.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

CLAUDE="$TMP/claude"
STATE="$CLAUDE/state"
SPOOL="$TMP/spool"
mkdir -p "$STATE" "$CLAUDE" "$SPOOL"

FAKEBIN="$TMP/bin"; mkdir -p "$FAKEBIN"
CALLS="$TMP/gh.calls"
# Shebang must resolve to a real path: this sandbox has no /usr/bin/env (same
# constraint bridge/start.sh works around for its own restart-spawn target).
BASH_BIN="$(command -v bash)"
cat > "$FAKEBIN/fakegh" <<SH
#!$BASH_BIN
printf '%s\n' "\$*" >> "$CALLS"
case "\$1 \$2" in
  "pr list") cat "\${GH_FAKE_LIST_JSON:?unset}" ;;
  "pr view") cat "\${GH_FAKE_VIEW_JSON:?unset}" ;;
  *) exit 1 ;;
esac
SH
chmod +x "$FAKEBIN/fakegh"

run_poll() {
  CCC_CLAUDE_DIR="$CLAUDE" CCC_STATE_DIR="$STATE" CCC_PUSH_SPOOL="$SPOOL" \
  CCC_PR_STATUS_POLL_GH="$FAKEBIN/fakegh" CCC_NODE=testnode \
  GH_FAKE_LIST_JSON="${GH_FAKE_LIST_JSON:-}" GH_FAKE_VIEW_JSON="${GH_FAKE_VIEW_JSON:-}" \
  bash "$POLL" "$@"
}

spool_count() { find "$SPOOL" -name '*.json' 2>/dev/null | wc -l | tr -d ' '; }
spool_text_contains() { grep -l "$1" "$SPOOL"/*.json 2>/dev/null | head -1; }

# --- 1) no repos file configured: exits 0, no-op, no notifications -----------
out="$(run_poll run 2>&1)"; rc=$?
ok "no-repos exits 0" '[ "$rc" = 0 ]'
ok "no-repos says nothing configured" 'grep -q "no repos configured" <<<"$out"'
ok "no-repos sends no notifications" '[ "$(spool_count)" = 0 ]'

# --- 2) first sighting of a repo/author seeds state silently ------------------
printf '%s\n' 'acme/repo bot' '# a comment' '' > "$CLAUDE/pr-status-poll.repos"
cat > "$TMP/list1.json" <<'JSON'
[{"number":10,"url":"https://example.com/pr/10","title":"fix thing","statusCheckRollup":[],"updatedAt":"2026-08-05T00:00:00Z"}]
JSON
GH_FAKE_LIST_JSON="$TMP/list1.json" out="$(run_poll run 2>&1)"; rc=$?
ok "seed run exits 0" '[ "$rc" = 0 ]'
ok "seed run reports one repo" 'grep -q "repos=1" <<<"$out"'
ok "seed run sends no notifications" '[ "$(spool_count)" = 0 ]'
ok "seed run wrote state" 'jq -e ".\"acme/repo\".\"10\".checkStatus == \"PENDING\"" "$STATE/pr-status-poll.json" >/dev/null'

# --- 3) check rollup transitions PENDING -> SUCCESS: one notification --------
cat > "$TMP/list2.json" <<'JSON'
[{"number":10,"url":"https://example.com/pr/10","title":"fix thing","statusCheckRollup":[{"conclusion":"SUCCESS"},{"conclusion":"SUCCESS"}],"updatedAt":"2026-08-05T00:05:00Z"}]
JSON
GH_FAKE_LIST_JSON="$TMP/list2.json" out="$(run_poll run 2>&1)"; rc=$?
ok "transition run exits 0" '[ "$rc" = 0 ]'
ok "transition run reports one transition" 'grep -q "transitions=1" <<<"$out"'
ok "transition run sends exactly one notification" '[ "$(spool_count)" = 1 ]'
ok "notification mentions SUCCESS" '[ -n "$(spool_text_contains SUCCESS)" ]'
ok "notification dedup key includes PR number" 'grep -q "acme/repo:10:SUCCESS" "$SPOOL"/*.json'
ok "state updated to SUCCESS" 'jq -e ".\"acme/repo\".\"10\".checkStatus == \"SUCCESS\"" "$STATE/pr-status-poll.json" >/dev/null'

# --- 4) re-running with no change sends no further notifications -------------
GH_FAKE_LIST_JSON="$TMP/list2.json" out="$(run_poll run 2>&1)"; rc=$?
ok "steady-state run sends no new notifications" '[ "$(spool_count)" = 1 ]'
ok "steady-state run reports zero transitions" 'grep -q "transitions=0" <<<"$out"'

# --- 5) PR disappears from open list: reported as closed/merged --------------
printf '%s' '[]' > "$TMP/list3.json"
cat > "$TMP/view10.json" <<'JSON'
{"state":"MERGED","title":"fix thing","url":"https://example.com/pr/10","mergedAt":"2026-08-05T00:10:00Z"}
JSON
GH_FAKE_LIST_JSON="$TMP/list3.json" GH_FAKE_VIEW_JSON="$TMP/view10.json" out="$(run_poll run 2>&1)"; rc=$?
ok "closed run exits 0" '[ "$rc" = 0 ]'
ok "closed run reports one closed" 'grep -q "closed=1" <<<"$out"'
ok "closed run sends a second notification" '[ "$(spool_count)" = 2 ]'
ok "notification mentions MERGED" '[ -n "$(spool_text_contains MERGED)" ]'
ok "state no longer tracks PR 10" 'jq -e ".\"acme/repo\" | has(\"10\") | not" "$STATE/pr-status-poll.json" >/dev/null'

# --- 6) invalid repo-line is skipped without aborting the run ----------------
printf '%s\n' 'acme/repo bot' 'not-a-valid-line' > "$CLAUDE/pr-status-poll.repos"
GH_FAKE_LIST_JSON="$TMP/list3.json" out="$(run_poll run 2>&1)"; rc=$?
ok "invalid line does not abort the run" '[ "$rc" = 0 ]'

# --- 7) a NEUTRAL check conclusion counts as SUCCESS, not PENDING ------------
# Regression for a real bug caught via a live smoke test against ccc-node#965:
# CodeQL legitimately concludes NEUTRAL (not SUCCESS) on a clean run, and an
# all-conclusions-must-equal-SUCCESS comparison misreported a fully green,
# fully COMPLETED PR as still PENDING.
CLAUDE2="$TMP/claude2"; STATE2="$CLAUDE2/state"; SPOOL2="$TMP/spool2"
mkdir -p "$STATE2" "$CLAUDE2" "$SPOOL2"
printf '%s\n' 'acme/repo2 bot' > "$CLAUDE2/pr-status-poll.repos"
cat > "$TMP/list_neutral.json" <<'JSON'
[{"number":20,"url":"https://example.com/pr/20","title":"neutral check case","statusCheckRollup":[{"status":"COMPLETED","conclusion":"SUCCESS"},{"status":"COMPLETED","conclusion":"NEUTRAL"}],"updatedAt":"2026-08-05T00:00:00Z"}]
JSON
CCC_CLAUDE_DIR="$CLAUDE2" CCC_STATE_DIR="$STATE2" CCC_PUSH_SPOOL="$SPOOL2" \
  CCC_PR_STATUS_POLL_GH="$FAKEBIN/fakegh" CCC_NODE=testnode \
  GH_FAKE_LIST_JSON="$TMP/list_neutral.json" \
  bash "$POLL" run >/dev/null 2>&1
ok "NEUTRAL conclusion counts as SUCCESS, not PENDING" \
  'jq -e ".\"acme/repo2\".\"20\".checkStatus == \"SUCCESS\"" "$STATE2/pr-status-poll.json" >/dev/null'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
