#!/usr/bin/env bash
# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
# Tests for pending_expire.py (#2184) — hermetic, no provider/network calls.
# Pins: dry-run moves nothing; only undecided drafts past the cutoff move;
# decided/approved/proposal entries and unknown-age entries are skipped;
# manifest rows carry the 2026-10-07 4c fields; restore puts a draft back;
# CCC_SKILL_PENDING_EXPIRE_DAYS=0 is a no-op; malformed days fall back to 90.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PE="$HERE/pending_expire.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
export HOME="$TMP/home"
export CCC_CLAUDE_DIR="$TMP/home/.claude"
STATE="$CCC_CLAUDE_DIR/state"
PENDING="$STATE/pending-skills"
ARCHIVE="$STATE/skill-autosave-archive"
mkdir -p "$PENDING"; chmod 700 "$STATE" "$PENDING"
export CCC_NODE=testnode

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# Fixed "now": 2026-10-08T00:00:00Z
NOW=1791417600
# draft <name> <staged_at ISO or ''> [extra file]
draft() {
  local name="$1" staged="$2" extra="${3:-}"
  mkdir -p "$PENDING/$name"
  printf -- '---\nname: x\ndescription: Use when testing.\n---\n# x\n' > "$PENDING/$name/SKILL.md"
  if [ -n "$staged" ]; then
    printf '{"name":"x","staged_at":"%s","status":"pending"}\n' "$staged" > "$PENDING/$name/meta.json"
  fi
  [ -n "$extra" ] && : > "$PENDING/$name/$extra"
  return 0
}

# 120d old (meta) -> expires
draft "20260610-000000-aaaa-old-meta" "2026-06-10T00:00:00Z"
# 95d old, no meta -> dir-name stamp -> expires
draft "20260705-000000-bbbb-old-stamp" ""
# 89d old -> stays (boundary: cutoff is >= 90d)
draft "20260711-000000-cccc-fresh-89d" "2026-07-11T00:00:00Z"
# 3d old -> stays
draft "20261005-000000-dddd-fresh-3d" "2026-10-05T00:00:00Z"
# decided suffix, old -> skipped
draft "20260601-000000-eeee-done.approved-20260603120000" "2026-06-01T00:00:00Z"
# human-approved awaiting install, old -> skipped
draft "20260601-000000-ffff-approved-wait" "2026-06-01T00:00:00Z" "meta.approved.json"
# incremental proposal, old -> skipped
draft "20260601-000000-gggg-proposal" "2026-06-01T00:00:00Z" "proposal.json"
# unknown age: no meta, no stamp, but mtime is "now" -> stays (mtime fallback is fresh)
mkdir -p "$PENDING/no-stamp-dir"; : > "$PENDING/no-stamp-dir/SKILL.md"
# a stray file (not a directory) -> skipped, never crashes the run
: > "$PENDING/stray.txt"

run_pe() { python3 "$PE" "$@"; }

# --- 1) dry-run: reports the two eligible drafts, moves nothing ---------------
out="$(run_pe run --dry-run --now "$NOW")"; rc=$?
ok "dry-run exits 0" '[ "$rc" = 0 ]'
ok "dry-run status" 'printf "%s" "$out" | jq -e ".status == \"dry-run\" and .dry_run == true" >/dev/null'
ok "dry-run names exactly the two old drafts" \
  '[ "$(printf "%s" "$out" | jq -r ".moved | sort | join(\",\")")" = "20260610-000000-aaaa-old-meta,20260705-000000-bbbb-old-stamp" ]'
ok "dry-run moved nothing" '[ -d "$PENDING/20260610-000000-aaaa-old-meta" ] && [ ! -d "$ARCHIVE" ]'
ok "dry-run counts skips by reason" \
  'printf "%s" "$out" | jq -e ".skipped.decided == 1 and .skipped[\"approved-awaiting-install\"] == 1 and .skipped[\"incremental-proposal\"] == 1 and .skipped[\"not-a-directory\"] == 1" >/dev/null'
ok "dry-run reports the oldest remaining age (89d)" 'printf "%s" "$out" | jq -e ".oldest_remaining_days == 89" >/dev/null'

# --- 2) off switch --------------------------------------------------------------
out="$(CCC_SKILL_PENDING_EXPIRE_DAYS=0 run_pe run --now "$NOW")"
ok "days=0 is off" 'printf "%s" "$out" | jq -e ".status == \"off\" and .expire_days == null" >/dev/null'
ok "days=0 moved nothing" '[ -d "$PENDING/20260610-000000-aaaa-old-meta" ] && [ ! -d "$ARCHIVE" ]'
out="$(CCC_SKILL_PENDING_EXPIRE_DAYS=abc run_pe run --dry-run --now "$NOW")"
ok "malformed days falls back to 90" 'printf "%s" "$out" | jq -e ".expire_days == 90" >/dev/null'

# --- 3) live run: moves the two, writes manifest, leaves the rest ---------------
out="$(run_pe run --now "$NOW")"; rc=$?
ok "run exits 0" '[ "$rc" = 0 ]'
ok "run status moved" 'printf "%s" "$out" | jq -e ".status == \"moved\" and (.moved | length) == 2 and (.failed | length) == 0" >/dev/null'
ADIR="$ARCHIVE/pending-90d-20261008"
ok "archive dir named by date" '[ -d "$ADIR" ]'
ok "archive dirs are private" '[ "$(stat -c %a "$ARCHIVE")" = 700 ] && [ "$(stat -c %a "$ADIR")" = 700 ]'
ok "old drafts left the queue" '[ ! -e "$PENDING/20260610-000000-aaaa-old-meta" ] && [ ! -e "$PENDING/20260705-000000-bbbb-old-stamp" ]'
ok "old drafts are in the archive intact" '[ -f "$ADIR/20260610-000000-aaaa-old-meta/SKILL.md" ] && [ -f "$ADIR/20260705-000000-bbbb-old-stamp/SKILL.md" ]'
ok "fresh/decided/approved/proposal entries untouched" \
  '[ -d "$PENDING/20260711-000000-cccc-fresh-89d" ] && [ -d "$PENDING/20261005-000000-dddd-fresh-3d" ] && [ -d "$PENDING/20260601-000000-eeee-done.approved-20260603120000" ] && [ -d "$PENDING/20260601-000000-ffff-approved-wait" ] && [ -d "$PENDING/20260601-000000-gggg-proposal" ] && [ -d "$PENDING/no-stamp-dir" ]'
ok "manifest has one row per move" '[ "$(wc -l < "$ADIR/manifest.jsonl" | tr -d " ")" = 2 ]'
ok "manifest is owner-only" '[ "$(stat -c %a "$ADIR/manifest.jsonl")" = 600 ]'
ok "manifest row carries the 4c fields" \
  'jq -e "select(.name == \"20260610-000000-aaaa-old-meta\") | .node == \"testnode\" and (.from | endswith(\"/pending-skills\")) and .age_days == 120 and (.reason | test(\"older than 90d\"))" "$ADIR/manifest.jsonl" >/dev/null'
ok "stamp-only draft age from directory name" \
  'jq -e "select(.name == \"20260705-000000-bbbb-old-stamp\") | .staged_at == \"2026-07-05T00:00:00Z\"" "$ADIR/manifest.jsonl" >/dev/null'

# --- 4) idempotent: second run finds nothing ------------------------------------
out="$(run_pe run --now "$NOW")"
ok "second run is clean" 'printf "%s" "$out" | jq -e ".status == \"clean\" and .eligible == 0" >/dev/null'

# --- 5) status: age buckets ---------------------------------------------------------
out="$(run_pe status)"
ok "status counts undecided only" 'printf "%s" "$out" | jq -e ".undecided == 3 and .archived == 2 and .expire_days == 90" >/dev/null'

# --- 6) restore puts a draft back and logs it ------------------------------------------
out="$(run_pe restore 20260610-000000-aaaa-old-meta)"; rc=$?
ok "restore exits 0" '[ "$rc" = 0 ]'
ok "restore status" 'printf "%s" "$out" | jq -e ".status == \"restored\"" >/dev/null'
ok "restored draft is back in the queue" '[ -f "$PENDING/20260610-000000-aaaa-old-meta/SKILL.md" ] && [ ! -e "$ADIR/20260610-000000-aaaa-old-meta" ]'
ok "restore appended a manifest row" 'jq -e "select(.reason == \"restore\") | .name == \"20260610-000000-aaaa-old-meta\"" "$ADIR/manifest.jsonl" >/dev/null'
out="$(run_pe restore does-not-exist)"; rc=$?
ok "restore of unknown name fails closed" '[ "$rc" = 1 ] && printf "%s" "$out" | jq -e ".status == \"not-found\"" >/dev/null'
out="$(run_pe restore "../etc")"; rc=$?
ok "restore rejects unsafe names" '[ "$rc" = 1 ] && printf "%s" "$out" | jq -e ".status == \"unsafe-name\"" >/dev/null'

# --- 7) archive name collision is reported, not overwritten ----------------------
mkdir -p "$ADIR/20260610-000000-aaaa-old-meta"; : > "$ADIR/20260610-000000-aaaa-old-meta/KEEP"
out="$(run_pe run --now "$NOW")"
ok "collision reported as failed" 'printf "%s" "$out" | jq -e ".failed[0].code == \"archive-name-taken\" and (.moved | length) == 0" >/dev/null'
ok "collision leaves both sides intact" '[ -f "$PENDING/20260610-000000-aaaa-old-meta/SKILL.md" ] && [ -f "$ADIR/20260610-000000-aaaa-old-meta/KEEP" ]'

# --- 8) missing queue is not an error ------------------------------------------------
out="$(CCC_STATE_DIR="$TMP/nostate" run_pe run --now "$NOW")"; rc=$?
ok "no queue -> no-queue, exit 0" '[ "$rc" = 0 ] && printf "%s" "$out" | jq -e ".status == \"no-queue\"" >/dev/null'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
