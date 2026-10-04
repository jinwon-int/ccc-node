#!/usr/bin/env bash
# Tests for claude/hooks/report-style-canary.sh — the owner-facing report-style
# A/B canary (#2109 A). Hermetic: private CCC_CLAUDE_DIR / CCC_HOOK_DIR, the
# hook must stay silent without the flag, inject the rule text with it, echo a
# bounded operator note, ignore other events, and always exit 0.
# shellcheck disable=SC2034  # test variables are read inside the eval'd ok() conditions
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/report-style-canary.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0
fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

HOME_DIR="$TMP/home"
export HOME="$HOME_DIR"
export CCC_CLAUDE_DIR="$HOME_DIR/.claude"
export CCC_HOOK_DIR="$TMP/hooks"
mkdir -p "$CCC_CLAUDE_DIR/state" "$CCC_HOOK_DIR/lib"
cp "$HERE/lib/report-style-ste.txt" "$CCC_HOOK_DIR/lib/report-style-ste.txt"
cp "$HERE/../../bridge/utils/report_style.py" "$CCC_HOOK_DIR/ccc_report_style.py"
FLAG="$CCC_CLAUDE_DIR/state/report-style-canary.flag"

run_hook() { bash "$HOOK" "$@"; }

# 1. no flag → silent, exit 0
out="$(run_hook SessionStart)"; rc=$?
ok "without the flag the hook prints nothing and exits 0" '[ "$rc" = 0 ] && [ -z "$out" ]'

# 2. flag present → additionalContext carries the rule block
: > "$FLAG"
out="$(run_hook SessionStart)"; rc=$?
ok "with the flag the hook exits 0 and emits hookSpecificOutput" \
  '[ "$rc" = 0 ] && printf "%s" "$out" | jq -e ".hookSpecificOutput.hookEventName == \"SessionStart\"" >/dev/null'
ok "the injected context is the STE rule block" \
  'printf "%s" "$out" | jq -r ".hookSpecificOutput.additionalContext" | grep -q "STE 80% 규칙"'
ok "no operator note line is appended for an empty flag" \
  '! printf "%s" "$out" | jq -r ".hookSpecificOutput.additionalContext" | grep -q "카나리 메모"'

# 3. operator note inside the flag is echoed, bounded to 160 chars
{ printf '\n'; printf 'end: 2026-10-09 18:00 KST %s\n' "$(printf 'x%.0s' $(seq 1 400))"; } > "$FLAG"
out="$(run_hook SessionStart)"
# shellcheck disable=SC2034  # read inside the eval'd ok() conditions below
note="$(printf "%s" "$out" | jq -r ".hookSpecificOutput.additionalContext" | grep "카나리 메모")"
ok "the first non-empty flag line is echoed as the canary note" 'printf "%s" "$note" | grep -q "end: 2026-10-09 18:00 KST"'
ok "the note is bounded (≤ 160 chars of flag text)" '[ "${#note}" -lt 200 ]'

# 4. PostCompact also re-injects; other events are ignored
out="$(run_hook PostCompact)"
ok "PostCompact re-injects the rule" 'printf "%s" "$out" | jq -e ".hookSpecificOutput.hookEventName == \"PostCompact\"" >/dev/null'
out="$(run_hook PreToolUse)"; rc=$?
ok "an unrelated event is ignored with exit 0" '[ "$rc" = 0 ] && [ -z "$out" ]'

# 5. missing rule text → silent (never inject a stub), exit 0
rm -f "$CCC_HOOK_DIR/lib/report-style-ste.txt"
out="$(run_hook SessionStart)"; rc=$?
ok "a missing rule file keeps the hook silent and exit 0" '[ "$rc" = 0 ] && [ -z "$out" ]'

# 6. distill subprocess guard
cp "$HERE/lib/report-style-ste.txt" "$CCC_HOOK_DIR/lib/report-style-ste.txt"
out="$(CLAUDE_DISTILL_INFLIGHT=1 run_hook SessionStart)"
# shellcheck disable=SC2034  # rc is read inside the eval'd ok() condition
rc=$?
ok "distill subprocesses get nothing" '[ "$rc" = 0 ] && [ -z "$out" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
