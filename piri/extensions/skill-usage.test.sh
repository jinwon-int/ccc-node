#!/usr/bin/env bash
# harness: umask-rerun
# Tests for piri/extensions/skill-usage.ts — the piri skill-use telemetry
# extension (#1692 B). Hermetic and isolated: no pi runtime, no provider, no
# network, no ~/.piri or ~/.claude mutation. The extension module loads as a
# plain TS module under node --experimental-strip-types and its handlers are
# driven with synthetic events; the last group also runs against the REAL
# repo logger in a fixture HOME.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0
fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { # ok <label> <condition>
	if eval "$2"; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		echo "FAIL: $1"
	fi
}

HARNESS="$HERE/skill-usage.test.ts"
# shellcheck disable=SC2034  # Read through ok() eval assertions.
EXTENSION="$HERE/skill-usage.ts"
REAL_LOGGER="$ROOT/claude/hooks/skill-usage-log.sh"

# Type stripping needs node >= 22.6; missing test capability fails explicitly.
node_ok() {
	command -v node >/dev/null 2>&1 || return 1
	local major minor
	major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null)" || return 1
	minor="$(node -p 'process.versions.node.split(".")[1]' 2>/dev/null)" || return 1
	[ "$major" -gt 22 ] || { [ "$major" = 22 ] && [ "$minor" -ge 6 ]; }
}

run_group() { # run_group <name> <home-dir> [VAR=value ...]
	local name="$1" home_dir="$2"
	shift 2
	mkdir -p "$home_dir"
	# shellcheck disable=SC2086  # assignments arrive as literal env words
	env -i HOME="$home_dir" PATH="${PATH:-}" CCC_TEST_CAPTURE_FILE="$TMP/$name.capture.jsonl" "$@" \
		node --experimental-strip-types "$HARNESS" "$name" >"$TMP/$name.out" 2>"$TMP/$name.err"
}

group_green() { # group_green <name> — the group's own checks all passed
	local name="$1"
	grep -q "^${name}: PASS=[0-9]* FAIL=0$" "$TMP/$name.out" && ! grep -q "^FAIL" "$TMP/$name.out"
}

# ---- guard: runtime availability -------------------------------------------
if ! node_ok; then
	echo "FAIL: piri skill-usage extension tests need node >= 22.6 (type stripping)"
	exit 1
fi
ok "extension module exists beside its harness" '[ -f "$EXTENSION" ] && [ -f "$HARNESS" ]'

# ---- unit: pure helpers + logger resolution in a fixture tree ---------------
run_group unit "$TMP/unit-home"
ok "unit group green" 'group_green unit'

# ---- semantics: synthetic events vs a capturing stub logger ------------------
sem_home="$TMP/sem-home"
mkdir -p "$sem_home/.claude/hooks"
printf '%s\n' '#!/usr/bin/env bash' 'tee -a "$CCC_TEST_CAPTURE_FILE" >/dev/null' \
	>"$sem_home/.claude/hooks/skill-usage-log.sh"
chmod +x "$sem_home/.claude/hooks/skill-usage-log.sh"
run_group semantics "$sem_home"
ok "semantics group green" 'group_green semantics'
ok "stub logger received Read JSON on stdin" \
	'[ "$(jq -s "[.[] | select(.tool_name == \"Read\" and .tool_input.file_path != null)] | length" "$TMP/semantics.capture.jsonl")" -ge 1 ]'
ok "stub logger received Skill JSON on stdin" \
	'[ "$(jq -s "[.[] | select(.tool_name == \"Skill\" and .tool_input.skill != null)] | length" "$TMP/semantics.capture.jsonl")" -ge 1 ]'
ok "stdin payloads carry no prompt bodies" \
	'! grep -q "\"text\"" "$TMP/semantics.capture.jsonl"'

# ---- missing logger: fail-open, no crash, no files ---------------------------
none_home="$TMP/none-home"
run_group missing-logger "$none_home"
ok "missing-logger group green" 'group_green missing-logger'
ok "missing logger leaves the HOME untouched" \
	'[ ! -e "$none_home/.claude" ] && [ ! -e "$none_home/.piri" ]'
ok "missing logger stays silent without an override" '[ ! -s "$TMP/missing-logger.err" ]'
# An explicit override that resolves to nothing gets ONE bounded stderr note —
# silent total telemetry loss is how #1692 happened.
mkdir -p "$TMP/none-home2"
env -i HOME="$TMP/none-home2" PATH="${PATH:-}" CCC_TEST_CAPTURE_FILE="$TMP/override.capture.jsonl" \
	CCC_SKILL_USAGE_LOGGER="$TMP/absent-logger.sh" \
	node --experimental-strip-types "$HARNESS" missing-logger \
	>"$TMP/override.out" 2>"$TMP/override.err"
ok "override run stays green" 'grep -q "^missing-logger: PASS=[0-9]* FAIL=0$" "$TMP/override.out"'
ok "unresolvable override notes on stderr exactly once" \
	'[ "$(grep -c "SKILL_USAGE_LOGGER" "$TMP/override.err")" = 1 ]'

# ---- timeout: a hung logger is killed on the bound ---------------------------
hung_home="$TMP/hung-home"
mkdir -p "$hung_home"
printf '%s\n' '#!/usr/bin/env bash' 'sleep 30' > "$TMP/hung-logger.sh"
chmod +x "$TMP/hung-logger.sh"
# shellcheck disable=SC2034  # Read through ok() eval assertions.
t0=$SECONDS
env -i HOME="$hung_home" PATH="${PATH:-}" CCC_TEST_CAPTURE_FILE="$TMP/timeout.capture.jsonl" \
	CCC_TEST_HUNG_LOGGER="$TMP/hung-logger.sh" \
	node --experimental-strip-types "$HARNESS" timeout \
	>"$TMP/timeout.out" 2>"$TMP/timeout.err"
ok "timeout group green" 'group_green timeout'
ok "timeout group finished quickly" '[ $((SECONDS - t0)) -lt 15 ]'

run_group resource-bounds "$TMP/bounds-home"
ok "resource-bounds group green" 'group_green resource-bounds'

# ---- integration: the REAL repo logger in a fixture HOME ---------------------
int_home="$TMP/int-home"
mkdir -p "$int_home/.claude/hooks" "$int_home/.piri/agent"
cp "$REAL_LOGGER" "$int_home/.claude/hooks/skill-usage-log.sh"
run_group integration "$int_home" "PIRI_CODING_AGENT_DIR=$int_home/.piri/agent"
ok "integration group green" 'group_green integration'
# shellcheck disable=SC2034  # Read through ok() eval assertions.
LEDGER="$int_home/.claude/state/skill-usage/usage.jsonl"
ok "real logger ledger holds exactly two truthful lines" '[ "$(wc -l < "$LEDGER")" = 2 ]'
ok "ledger records the Read of the SKILL.md" \
	'[ "$(jq -s "[.[] | select(.skill == \"web\" and .tool == \"Read\" and .ts != null)] | length" "$LEDGER")" = 1 ]'
ok "ledger records the explicit /skill: request" \
	'[ "$(jq -s "[.[] | select(.skill == \"web\" and .tool == \"Skill\")] | length" "$LEDGER")" = 1 ]'
ok "ledger line stays path-free (privacy)" '! grep -q "SKILL.md" "$LEDGER"'

if [ "$fail" != 0 ]; then
  cat "$TMP/"*.out "$TMP/"*.err
fi
echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
