#!/usr/bin/env bash
# Tests for hook-common.sh — the canonical shared hook helpers (#584 P0-3).
#
# hook-common.sh is sourced by nine hooks and scripts and had no tests of its
# own, so the helpers that every one of them relies on were unpinned. The
# off-switch spellings matter most: is_disabled decides whether Wiki memory,
# Honcho memory, distill and the injection scanner run at all, so a change in
# what counts as "off" silently flips those gates fleet-wide.
#
# The suite also audits the remaining copies of is_disabled. #584 P0-3 folded
# six of them into this file; two standalone scripts still carry their own
# because they run from BOTH the repo (scripts/) and the deployed hooks tree
# (~/.claude/hooks/), where no single relative path reaches this file --
# sourcing it would cost a dual-path fallback to replace a one-line function.
# Keeping the copies is the cheaper trade, but only while they stay identical,
# which is what the audit below enforces.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/test-stub.sh"
# Fixtures supply every input; ambient harness variables must not reach the
# helpers under test (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# shellcheck source=claude/hooks/lib/hook-common.sh
. "$HERE/hook-common.sh"
ok "hook-common sources cleanly and defines every documented helper" \
  '[ "$(type -t is_disabled)" = function ] && [ "$(type -t ts)" = function ] \
    && [ "$(type -t log)" = function ] && [ "$(type -t find_memory_tool)" = function ]'

# --- is_disabled: the accepted "off" spellings -------------------------------
for v in 0 false FALSE off OFF no NO; do
  ok "is_disabled treats '$v' as off" "is_disabled '$v'"
done

# --- is_disabled: everything else is ON --------------------------------------
# Fail-open by design: an unset or unrecognized flag must leave the feature
# enabled rather than silently disabling memory or the injection scanner.
for v in 1 true TRUE on ON yes YES 2 enabled ''; do
  ok "is_disabled leaves '${v:-<empty>}' enabled" "! is_disabled '$v'"
done
ok "is_disabled with no argument at all leaves the feature enabled" '! is_disabled'

# Sharp edge worth stating outright: the match is a literal case list, not a
# case-insensitive comparison, so only the all-lower and all-upper spellings
# count. `False`/`Off`/`No` read as "off" to a human but keep the feature ON.
for v in False Off No fAlSe " 0" "0 " "0x0" "off " "no-thanks"; do
  ok "is_disabled does NOT accept '$v' as off (literal match only)" "! is_disabled '$v'"
done

# --- ts ----------------------------------------------------------------------
# shellcheck disable=SC2034  # consumed via eval in ok()
ts_out="$(ts)"
ok "ts emits UTC ISO-8601 with a Z suffix" \
  '[[ "$ts_out" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]'

# --- log ---------------------------------------------------------------------
LOG="$TMP/hook.log"
log "first entry"
log "second entry"
ok "log appends one timestamped line per call" '[ "$(wc -l < "$TMP/hook.log")" = 2 ]'
ok "log keeps the message intact after the timestamp" \
  'grep -q " first entry$" "$TMP/hook.log" && grep -q " second entry$" "$TMP/hook.log"'
ok "log lines carry a ts-formatted prefix" \
  '[[ "$(head -1 "$TMP/hook.log")" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\  ]]'

# A hook that never sets $LOG must not fail or leave files behind: log is
# best-effort and callers do not check its status.
unset LOG
# shellcheck disable=SC2034  # consumed via eval in ok()
log_rc=0
# shellcheck disable=SC2034  # consumed via eval in ok()
log "no destination configured" || log_rc=$?
ok "log with no \$LOG configured succeeds silently" '[ "$log_rc" = 0 ]'
# shellcheck disable=SC2034  # consumed via eval in ok()
LOG="$TMP/unwritable/nested.log"
# shellcheck disable=SC2034  # consumed via eval in ok()
log_rc=0
# shellcheck disable=SC2034  # consumed via eval in ok()
log "unwritable destination" || log_rc=$?
ok "log to an unwritable path still succeeds (best-effort contract)" '[ "$log_rc" = 0 ]'
ok "log to an unwritable path creates nothing" '[ ! -e "$TMP/unwritable" ]'
unset LOG

# --- find_memory_tool: search order and the executable requirement -----------
# Mirror the real layout: HOOKDIR is <repo>/claude/hooks (tests) or
# ~/.claude/hooks (deployed), so the third search dir resolves to <repo>/scripts.
tools="$TMP/tools"; hookdir="$TMP/repo/claude/hooks"; repo_scripts="$TMP/repo/scripts"
mkdir -p "$tools" "$hookdir" "$repo_scripts"
mk_tool() { printf '#!/usr/bin/env bash\nexit 0\n' > "$1"; chmod +x "$1"; }

# shellcheck disable=SC2034  # read from the environment by find_memory_tool
CCC_MEMORY_TOOLS_DIR="$tools" HOOKDIR="$hookdir"
mk_tool "$hookdir/ccc-probe.sh"
ok "finds a tool in HOOKDIR when the tools dir has none" \
  '[ "$(find_memory_tool ccc-probe.sh)" = "$hookdir/ccc-probe.sh" ]'

mk_tool "$tools/ccc-probe.sh"
ok "prefers CCC_MEMORY_TOOLS_DIR over HOOKDIR" \
  '[ "$(find_memory_tool ccc-probe.sh)" = "$tools/ccc-probe.sh" ]'

mk_tool "$repo_scripts/ccc-repo-only.sh"
ok "falls back to the repo scripts/ dir beside the hooks tree" \
  '[ "$(find_memory_tool ccc-repo-only.sh)" = "$hookdir/../../scripts/ccc-repo-only.sh" ]'

# A non-executable file is not a usable tool; returning it would make callers
# fail later with a confusing permission error instead of taking their fallback.
printf 'not executable\n' > "$tools/ccc-plain.sh"
ok "ignores a non-executable file of the right name" '! find_memory_tool ccc-plain.sh'
ok "returns non-zero and prints nothing when the tool is absent" \
  '[ -z "$(find_memory_tool ccc-absent.sh 2>/dev/null)" ] && ! find_memory_tool ccc-absent.sh'

# An unset tools dir must be skipped, not treated as the current directory.
unset CCC_MEMORY_TOOLS_DIR
ok "an unset tools dir is skipped rather than searched as ''" \
  '[ "$(find_memory_tool ccc-probe.sh)" = "$hookdir/ccc-probe.sh" ]'
unset HOOKDIR
ok "with neither dir set, lookup simply fails" '! find_memory_tool ccc-probe.sh'

# --- drift audit over the remaining copies -----------------------------------
# The copy-paste this file was created to end (#584 P0-3). CI never runs the
# two standalone scripts against a hook, so a divergent off-switch there would
# surface only as a feature quietly refusing to turn off on a real node.
canonical="$(grep -h '^is_disabled()' "$HERE/hook-common.sh")"
ok "canonical definition is present and unique in hook-common.sh" \
  '[ -n "$canonical" ] && [ "$(grep -c "^is_disabled()" "$HERE/hook-common.sh")" = 1 ]'
drift=0
while IFS= read -r f; do
  [ "$f" = "$HERE/hook-common.sh" ] && continue
  while IFS= read -r line; do
    [ "$line" = "$canonical" ] && continue
    echo "is_disabled copy differs from hook-common.sh: $f"
    # shellcheck disable=SC2034  # consumed via eval in ok()
    drift=1
  done < <(grep -h '^is_disabled()' "$f")
done < <(grep -rl '^is_disabled()' "$ROOT/claude" "$ROOT/scripts" 2>/dev/null)
# shellcheck disable=SC2034  # $drift is consumed via eval in ok()
ok "every remaining is_disabled copy is byte-identical to the canonical one" '[ "$drift" = 0 ]'

# --- redirect-order audit over every log() in the repo -----------------------
# `printf ... >> "$LOG" 2>/dev/null` does not do what it looks like: shells
# apply redirections left to right, so a destination that cannot be opened is
# reported to the real stderr before it is silenced, and the failure becomes
# the function's exit status -- several callers invoke log as the last
# statement of a function. The same ordering bug appeared independently in
# start.sh (#1054) and hook-common.sh (#1055), then in six more log() copies,
# so pin the shape rather than wait for the next one.
#
# A log() that never redirects stderr at all is out of scope: it makes no
# silence promise, and this audit only holds the ones that do to it.
# The first version of this audit matched only single-line `log() { … }`, and
# that gap immediately cost a follow-up: the very files it cleared still held
# multi-line `audit()` writers with the identical ordering. So match the
# redirection itself wherever it appears, not the function that wraps it.
#
# Every appearance of `>> <path> 2>/dev/null` (or `> <path> 2>/dev/null`) is
# reported. Sites that legitimately need the ordering can be listed in
# ALLOWED_ORDER below with a reason; today there are none.
# Scope: functions named log() or audit(). Those two names carry an explicit
# "stay quiet and never fail the caller" contract, which this ordering breaks
# outright -- unlike the ~20 guarded call sites elsewhere (`… 2>/dev/null &&
# mv`, `… || true`), where the guard preserves correctness and only the message
# leaks. Those are tracked separately rather than swept into this assertion.
#
# The body is scanned whole, single- or multi-line: the first version of this
# audit matched only `log() { … }` on one line, and that gap immediately cost a
# follow-up -- the very files it cleared still held multi-line audit() writers
# with the identical ordering.
log_order=0
while IFS= read -r f; do
  while IFS= read -r hit; do
    echo "log()/audit() silences stderr after opening its destination: $f: $hit"
    # shellcheck disable=SC2034  # consumed via eval in ok()
    log_order=1
  done < <(awk '
      function bad(s) { return s ~ /(>>?)[[:space:]]+"[^"]+"[[:space:]]+2>\/dev\/null/ }
      # Single-line definition: it never reaches a bare `}`, so close it here --
      # leaving the flag set would spill the scan into the next function.
      /^(log|audit)\(\)[[:space:]]*\{.*\}[[:space:]]*$/ { if (bad($0)) print; next }
      /^(log|audit)\(\)[[:space:]]*\{/ { infn = 1; next }
      infn && /^\}/ { infn = 0; next }
      infn && $0 !~ /^[[:space:]]*#/ && bad($0) { print }
    ' "$f")
done < <(grep -rlE '^(log|audit)\(\)' --include='*.sh' \
           "$ROOT/claude/hooks" "$ROOT/scripts" 2>/dev/null | grep -v '\.test\.sh$')
# shellcheck disable=SC2034  # $log_order is consumed via eval in ok()
ok "no log()/audit() silences stderr after opening its destination" '[ "$log_order" = 0 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
