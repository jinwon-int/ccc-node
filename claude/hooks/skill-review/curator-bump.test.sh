#!/usr/bin/env bash
# Tests for claude/hooks/skill-review/curator-bump.sh — the PostToolUse(Skill)
# degraded-path telemetry (#752) and its optional skill-name field (#1866).
# Hermetic: every case runs against a private CCC_CLAUDE_DIR, the
# missing-tool cases run against a restricted PATH, and the hook's
# always-exit-0 contract is asserted directly on every degraded branch.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/curator-bump.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"
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
export CCC_CLAUDE_DIR="$HOME_DIR/.claude"
export HOME="$HOME_DIR"
mkdir -p "$CCC_CLAUDE_DIR"
chmod 700 "$CCC_CLAUDE_DIR"
STATE="$CCC_CLAUDE_DIR/state/skill-usage"
LOG="$STATE/degraded.log"

nlines() { # degraded.log line count, 0 while the log does not exist
  if [ -f "$LOG" ]; then wc -l <"$LOG" | tr -d '[:space:]'; else echo 0; fi
}

last_nf() { # field count of the newest degraded line
  tail -n 1 "$LOG" 2>/dev/null | awk '{print NF}'
}

# Restricted PATH fixtures: exactly the non-builtin dependencies the hook
# reaches before (and inside) note_degraded, minus the one tool whose absence
# is under test, so the wrapper must classify the gap as wrapper:jq_missing /
# wrapper:python3_missing rather than fall through to the ambient toolchain.
# The wrapper checks jq before python3, so each branch needs its own PATH.
deps="cat mkdir date wc chmod dirname tr cut jq python3"
mkbin() { # mkbin <dir> — symlink every dependency into <dir>
  mkdir -m 700 "$1"
  for dep in $deps; do
    ln -s "$(command -v "$dep")" "$1/$dep"
  done
}
BIN="$TMP/bin" # no jq, no python3 -> wrapper:jq_missing
mkbin "$BIN"
rm "$BIN/jq" "$BIN/python3"
BIN_JQ="$TMP/bin-jq" # jq present, no python3 -> wrapper:python3_missing
mkbin "$BIN_JQ"
rm "$BIN_JQ/python3"
BIN_PYFAIL="$TMP/bin-pyfail" # python3 present but failing -> wrapper:curator_nonzero
mkbin "$BIN_PYFAIL"
# A python3 that fails, for the wrapper:curator_nonzero branch.
PYFAIL="$TMP/pyfail"
mkdir -m 700 "$PYFAIL"
write_exec_stub "$PYFAIL/python3" <<'SH'
exit 1
SH
rm "$BIN_PYFAIL/python3"
ln -s "$PYFAIL/python3" "$BIN_PYFAIL/python3"

run_hook() { # run_hook <stdin-json> — ambient PATH, real toolchain
  printf '%s' "$1" | bash "$HOOK"
}

BASH_BIN="$(command -v bash)"
run_hook_path() { # run_hook_path <PATH> <stdin-json>
  local path="$1"
  printf '%s' "$2" | env PATH="$path" "$BASH_BIN" "$HOOK"
}

# --- pre-name branches: the name is not known yet, lines stay nameless --------
rc=0
run_hook_path "$BIN" '{"tool_name":"Skill","tool_input":{"skill":"alpha"}}' || rc=$?
ok "jq_missing exits 0" '[ "$rc" -eq 0 ]'
ok "jq_missing line keeps the nameless three-field format" \
  '[ "$(nlines)" = 1 ] && [ "$(last_nf)" = 3 ] && grep -q " wrapper:jq_missing$" "$LOG"'

rc=0
run_hook_path "$BIN_JQ" '{"tool_name":"Skill","tool_input":{"skill":"alpha"}}' || rc=$?
ok "python3_missing exits 0 and stays nameless three-field" \
  '[ "$rc" -eq 0 ] && [ "$(nlines)" = 2 ] && [ "$(last_nf)" = 3 ] &&
   grep -q " wrapper:python3_missing$" "$LOG"'

# --- post-name branches: the extracted skill name rides along -----------------
rc=0
run_hook_path "$BIN_PYFAIL" '{"tool_name":"Skill","tool_input":{"skill":"alpha"}}' || rc=$?
ok "curator_nonzero exits 0" '[ "$rc" -eq 0 ]'
ok "curator_nonzero line carries the skill name as a fourth field" \
  '[ "$(nlines)" = 3 ] && [ "$(last_nf)" = 4 ] &&
   tail -n 1 "$LOG" | grep -q " wrapper:curator_nonzero alpha$"'

# curator.py missing: run a copy of the hook from a directory that lacks it.
mkdir -m 700 "$TMP/nohost"
cp "$HOOK" "$TMP/nohost/curator-bump.sh"
rc=0
printf '%s' '{"tool_name":"Skill","tool_input":{"skill":"alpha"}}' |
  bash "$TMP/nohost/curator-bump.sh" || rc=$?
ok "curator_missing exits 0 and stays nameless three-field" \
  '[ "$rc" -eq 0 ] && [ "$(nlines)" = 4 ] && [ "$(last_nf)" = 3 ] &&
   grep -q " wrapper:curator_missing$" "$LOG"'

rc=0
run_hook 'not json at all' || rc=$?
ok "payload_unparsable exits 0 and stays nameless three-field" \
  '[ "$rc" -eq 0 ] && [ "$(nlines)" = 5 ] && [ "$(last_nf)" = 3 ] &&
   grep -q " wrapper:payload_unparsable$" "$LOG"'

rc=0
run_hook '{"tool_name":"Skill","tool_input":{}}' || rc=$?
ok "skill_name_absent exits 0 and stays nameless three-field" \
  '[ "$rc" -eq 0 ] && [ "$(nlines)" = 6 ] && [ "$(last_nf)" = 3 ] &&
   grep -q " wrapper:skill_name_absent$" "$LOG"'

# A fail-open curator bump (no such skill) degrades with its reason AND name.
rc=0
run_hook '{"tool_name":"Skill","tool_input":{"skill":"ghost-skill"}}' || rc=$?
ok "curator fail-open bump exits 0" '[ "$rc" -eq 0 ]'
ok "curator:<reason> line carries the skill name" \
  '[ "$(nlines)" = 7 ] && [ "$(last_nf)" = 4 ] &&
   tail -n 1 "$LOG" | grep -q " curator:contract:[a-z_]* ghost-skill$"'

# --- happy path: a recording bump must not degrade the log --------------------
SKILL="$CCC_CLAUDE_DIR/skills/alpha"
mkdir -p "$SKILL"
printf '# alpha\n' >"$SKILL/SKILL.md"
chmod 600 "$SKILL/SKILL.md"
before="$(nlines)"
rc=0
run_hook '{"tool_name":"Skill","tool_input":{"skill":"alpha"}}' || rc=$?
ok "recording bump exits 0 and writes no degraded line" \
  '[ "$rc" -eq 0 ] && [ "$(nlines)" = "$before" ]'

# --- sanitization: the name bypasses ownership._validate_name -----------------
before="$(nlines)"
rc=0
run_hook '{"tool_name":"Skill","tool_input":{"skill":"bad\nskill name"}}' || rc=$?
ok "multiline skill name exits 0" '[ "$rc" -eq 0 ]'
ok "multiline skill name is folded to one three-field line" \
  '[ "$(nlines)" = "$((before + 1))" ] &&
   tail -n 1 "$LOG" | grep -qE " curator:contract:[a-z_]* bad skill name$"'

long_name="$(printf 'a%.0s' $(seq 1 80))"
rc=0
# shellcheck disable=SC2034  # rc is read via eval inside ok()
run_hook "{\"tool_name\":\"Skill\",\"tool_input\":{\"skill\":\"$long_name\"}}" || rc=$?
ok "overlong skill name exits 0" '[ "$rc" -eq 0 ]'
ok "overlong skill name is capped at 64 bytes" \
  '[ "$(last_nf)" = 4 ] && [ "$(tail -n 1 "$LOG" | awk "{print length(\$NF)}")" = 64 ]'

# --- log hygiene: owner-only, hard size cap -----------------------------------
ok "degraded.log is owner-only" '[ "$(stat -c %a "$LOG")" = 600 ]'

head -c 262144 /dev/zero | tr '\0' 'x' >"$LOG"
printf '\n' >>"$LOG"
chmod 600 "$LOG"
# shellcheck disable=SC2034  # before is read via eval inside ok()
before="$(nlines)"
# shellcheck disable=SC2034  # size is read via eval inside ok()
size="$(wc -c <"$LOG" | tr -d '[:space:]')"
run_hook '{"tool_name":"Skill","tool_input":{}}'
ok "a log at the size cap refuses further appends" \
  '[ "$(nlines)" = "$before" ] && [ "$(wc -c <"$LOG" | tr -d "[:space:]")" = "$size" ]'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
