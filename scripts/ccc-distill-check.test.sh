#!/usr/bin/env bash
# Tests for ccc-distill-check.sh — read-only status, no network.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECK="$ROOT/scripts/ccc-distill-check.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
mkdir -p "$TMP/state" "$TMP/hermes" "$TMP/common-journal"
export CCC_STATE_DIR="$TMP/state"
export CCC_DISTILL_JOURNAL_DIR="$TMP/common-journal"
out="$(bash "$CHECK" --json 2>&1)"; rc=$?
ok "empty state exits 0" '[ "$rc" = 0 ]'
ok "empty state reports live mode" 'jq -e ".mode == \"LIVE\" and .checkpoint.snapshots == 0 and .triggers.precompact == 0" <<<"$out" >/dev/null'
ok "empty state reports provider-neutral counters" 'jq -e ".provider_neutral.total == 0 and .provider_neutral.cooldown_files == 0" <<<"$out" >/dev/null'
# Timestamps must stay inside the checker's 14-day window on ANY run date —
# hardcoded dates rot and start failing two weeks after they were written.
D3="$(date -u -d '3 days ago' +%Y-%m-%dT%H:%M:%SZ)"
D2="$(date -u -d '2 days ago' +%Y-%m-%dT%H:%M:%SZ)"
D1="$(date -u -d '1 day ago'  +%Y-%m-%dT%H:%M:%SZ)"
cat > "$TMP/state/distill.log" <<LOG
$D3 start trigger=manual dryrun=0 pid=1
$D3 done trigger=manual pid=2 elapsed_s=1
$D2 start trigger=sessionend dryrun=0 pid=3
$D2 [drain] drained ok=2 failed=1 dropped=1 processed=4
$D1 start trigger=precompact dryrun=0 pid=5
LOG
cat > "$TMP/state/distill-last.json" <<'JSON'
{"session_id":"s1","trigger":"precompact","distilled_at":"2026-06-22T00:00:01Z","honcho":[{"text":"x"}],"wiki_candidates":[{"title":"w"}]}
JSON
mkdir -p "$TMP/state/checkpoints"
printf 'older\n' > "$TMP/state/checkpoints/working-state-20260621_000000.md"
printf 'newer\n' > "$TMP/state/checkpoints/working-state-20260622_000000.md"
touch -t 202606210000 "$TMP/state/checkpoints/working-state-20260621_000000.md"
touch -t 202606220000 "$TMP/state/checkpoints/working-state-20260622_000000.md"
out="$(bash "$CHECK" --json 2>&1)"; rc=$?
ok "populated state exits 0" '[ "$rc" = 0 ]'
ok "populated counts triggers" 'jq -e ".triggers.manual == 1 and .triggers.sessionend == 1 and .triggers.precompact == 1" <<<"$out" >/dev/null'
ok "populated reports checkpoints" 'jq -e ".checkpoint.snapshots == 2 and (.checkpoint.last | contains(\"working-state-20260622_000000.md\"))" <<<"$out" >/dev/null'
ok "populated counts drain" 'jq -e ".drain.ok == 2 and .drain.failed == 1 and .drain.dropped == 1" <<<"$out" >/dev/null'
ok "populated reports last summary" 'jq -e ".last | contains(\"session=s1\") and contains(\"trigger=precompact\")" <<<"$out" >/dev/null'
mkdir -p "$TMP/state/distill-provider-cooldowns"
printf '%s\n' '{"status":"snapshot_done"}' > "$TMP/common-journal/a.json"
printf '%s\n' '{"status":"extraction_retryable_failed"}' > "$TMP/common-journal/b.json"
printf '%s\n' '{}' > "$TMP/state/distill-provider-cooldowns/c.json"
out="$(bash "$CHECK" --json 2>&1)"; rc=$?
ok "provider-neutral queue and cooldown counts are body-free" '[ "$rc" = 0 ] && jq -e ".provider_neutral.total == 2 and .provider_neutral.ready == 1 and .provider_neutral.retryable == 1 and .provider_neutral.cooldown_files == 1" <<<"$out" >/dev/null'
touch "$TMP/state/distill.disabled"
out="$(bash "$CHECK" --json 2>&1)"; rc=$?
ok "disabled mode detected" '[ "$rc" = 0 ] && jq -e ".mode == \"OFF\"" <<<"$out" >/dev/null'
rm -f "$TMP/state/distill.disabled"
touch "$TMP/state/distill.dryrun"
# shellcheck disable=SC2034  # out is read via eval inside ok()
out="$(bash "$CHECK" 2>&1)"
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "text output exits 0" '[ "$rc" = 0 ]'
ok "text output shows dry-run" 'grep -q "mode:" <<<"$out" && grep -q "DRY-RUN" <<<"$out"'

# #2720 follow-up: outside a bridge session the selector falls back to the
# Telegram data dir, so a failing live hande journal was invisible. Every
# channel journal is listed with its owner and terminal failures are split
# into live (real loss) and orphaned (leftover nobody will retry).
out="$(bash "$CHECK" --json 2>&1)"; rc=$?
ok "override scope lists only the chosen journal (#2720)" '[ "$rc" = 0 ] && jq -e ".provider_neutral.journals | length == 1 and .[0].owner == \"override\"" <<<"$out" >/dev/null'
mc="$TMP/multi-home"
for d in .telegram_bot .ccc-hande .ccc-matrix; do mkdir -p "$mc/$d/distill-journal"; done
mkdir -p "$mc/.ccc-hande/danso-distill-journal"
printf '{"status":"extraction_terminal_failed"}\n' > "$mc/.telegram_bot/distill-journal/$(printf 'a%.0s' {1..64}).json"
printf '{"status":"extraction_terminal_failed"}\n' > "$mc/.ccc-hande/distill-journal/$(printf 'b%.0s' {1..64}).json"
printf '{"status":"extraction_done"}\n' > "$mc/.ccc-hande/distill-journal/$(printf 'c%.0s' {1..64}).json"
printf '{"status":"extraction_done"}\n' > "$mc/.ccc-matrix/distill-journal/$(printf 'd%.0s' {1..64}).json"
printf '{}\n' > "$mc/.ccc-hande/health.json"; touch -d @9990 "$mc/.ccc-hande/health.json"
printf '{}\n' > "$mc/.ccc-matrix/health.json"; touch -d @100 "$mc/.ccc-matrix/health.json"
mc_check() {
  (cd "$mc" && env -u CCC_DISTILL_JOURNAL_DIR -u BOT_DATA_DIR -u PROJECT_ROOT HOME="$mc" \
    CCC_DISTILL_CHECK_NOW_EPOCH=10000 CCC_CHECK_JOURNAL_LIVE_SEC=60 bash "$CHECK" "$@" 2>&1)
}
out="$(mc_check --json)"; rc=$?
ok "distill check lists every channel journal outside a bridge session (#2720)" '[ "$rc" = 0 ] && jq -e "[.provider_neutral.journals[].channel] | sort == [\"ccc-hande\", \"ccc-hande/danso\", \"ccc-matrix\", \"telegram_bot\"]" <<<"$out" >/dev/null'
ok "distill check splits terminal failures by owner (#2720)" 'jq -e ".provider_neutral.live_terminal == 1 and .provider_neutral.orphaned_terminal == 1 and .provider_neutral.live_threshold_seconds == 60" <<<"$out" >/dev/null'
ok "distill check owner comes from health.json age (#2720)" 'jq -e "(.provider_neutral.journals | map({(.channel): [.owner, .health_age_seconds]}) | add) == {\"telegram_bot\": [\"orphaned\", -1], \"ccc-hande\": [\"live\", 10], \"ccc-hande/danso\": [\"live\", 10], \"ccc-matrix\": [\"orphaned\", 9900]}" <<<"$out" >/dev/null'
ok "distill check keeps the single-journal fields unchanged (#2720)" 'jq -e ".provider_neutral.journal | endswith(\"/.telegram_bot/distill-journal\")" <<<"$out" >/dev/null && jq -e ".provider_neutral.total == 1 and .provider_neutral.terminal == 1" <<<"$out" >/dev/null'
# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
{ out="$(mc_check)"; rc=$?; }
ok "distill check text lists each channel journal (#2720)" '[ "$rc" = 0 ] && grep -q "^- journals:   live_terminal=1 orphaned_terminal=1" <<<"$out" && grep -q "^  - ccc-hande: owner=live jobs=2 .*terminal=1" <<<"$out" && grep -q "^  - telegram_bot: owner=orphaned" <<<"$out"'
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
