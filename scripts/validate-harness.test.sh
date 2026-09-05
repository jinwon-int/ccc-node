#!/usr/bin/env bash
# Regression tests for validate-harness.sh scratch-dir hermeticity (#565).
# Usage: bash scripts/validate-harness.test.sh   (exit 0 = all pass)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VALIDATE="$ROOT/scripts/validate-harness.sh"
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

HOSTILE="$(mktemp -d)"
trap 'rm -rf "$HOSTILE" 2>/dev/null || true' EXIT
# A stale fixed-name artifact left by "another account" in the shared temp dir.
# Before #565, both the harness (rendered.json) and its child tests
# (checkpoint-guard.out) opened fixed names directly in ${TMPDIR:-/tmp} and
# false-FAILed on such hostile state.
touch "$HOSTILE/rendered.json" "$HOSTILE/checkpoint-guard.out"
chmod 000 "$HOSTILE/rendered.json" "$HOSTILE/checkpoint-guard.out" 2>/dev/null || true

# --print-scratch emits one path per line so whitespace in a valid scratch
# root cannot break parsing (review finding on #565).
mapfile -t scratch < <(TMPDIR="$HOSTILE" bash "$VALIDATE" --print-scratch)
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
tmp="${scratch[0]:-}"
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
tmpdir="${scratch[1]:-}"
# NOTE: mktemp -d honours the caller TMPDIR, so the private dir may be NESTED
# under it — that is fine: hermeticity comes from the fresh unique 0700 dir,
# not its parent. What must never happen is using the caller dir ITSELF (where
# the stale fixed-name artifacts live).
ok "validate resolves a private scratch dir, never the caller TMPDIR itself" \
  '[ -n "$tmp" ] && [ "$tmp" != "$HOSTILE" ]'
ok "validate exports the private scratch as TMPDIR for child tests" \
  '[ "$tmpdir" = "$tmp" ]'
ok "scratch dir is cleaned up on exit" '[ ! -d "$tmp" ]'

# A valid scratch root MAY contain whitespace; the contract must hold there too.
HOSTILE_WS="$HOSTILE/review tmp with spaces"
mkdir -p "$HOSTILE_WS"
mapfile -t scratch_ws < <(TMPDIR="$HOSTILE_WS" bash "$VALIDATE" --print-scratch)
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
tmp_ws="${scratch_ws[0]:-}"
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
tmpdir_ws="${scratch_ws[1]:-}"
ok "whitespace scratch root: private dir resolved, not the caller dir itself" \
  '[ -n "$tmp_ws" ] && [ "$tmp_ws" != "$HOSTILE_WS" ]'
ok "whitespace scratch root: TMPDIR exported and parsed intact" \
  '[ "$tmpdir_ws" = "$tmp_ws" ]'

# --- #1064: the runner scrubs the node's harness env before each suite --------
# Three suites failed on every live node while CI stayed green, because the
# per-suite guard (#1023) only reaches suites that source test-stub.sh. The
# isolation now lives in the runner, so assert it there.
RUN_SUITE_SRC="$(sed -n '/^run_suite() {/,/^}/p' "$VALIDATE")"
# If run_suite is renamed or removed, fail loudly instead of silently passing an
# empty extraction.
ok "run_suite helper is present in validate-harness.sh" \
  '[ -n "$RUN_SUITE_SRC" ] && grep -q "env \${scrub\[@\]" <<<"$RUN_SUITE_SRC"'

PROBE="$HOSTILE/probe.sh"
cat > "$PROBE" <<'PROBE_EOF'
printf 'CCC=[%s] NUNCHI=[%s] KEEP=[%s] TMPDIR=[%s]\n' \
  "${CCC_PROBE_LEAK:-unset}" "${NUNCHI_PROBE_LEAK:-unset}" \
  "${UNRELATED_PROBE_KEEP:-unset}" "${TMPDIR:-unset}"
PROBE_EOF

# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
probe_out="$(
  export CCC_PROBE_LEAK=leaked NUNCHI_PROBE_LEAK=leaked \
         UNRELATED_PROBE_KEEP=kept TMPDIR="$HOSTILE"
  eval "$RUN_SUITE_SRC"
  run_suite "$PROBE"
)"
ok "CCC_* from the live node does not reach the suite" \
  '[[ "$probe_out" == *"CCC=[unset]"* ]]'
ok "NUNCHI_* from the live node does not reach the suite" \
  '[[ "$probe_out" == *"NUNCHI=[unset]"* ]]'
ok "unrelated environment is left alone" \
  '[[ "$probe_out" == *"KEEP=[kept]"* ]]'
# The runner deliberately exports its private scratch as TMPDIR (#565); the
# scrub must not take it out along with the harness vars.
ok "TMPDIR still reaches the suite" \
  '[[ "$probe_out" == *"TMPDIR=[$HOSTILE]"* ]]'

# With nothing to scrub the helper must still run the suite (empty-array
# expansion under `set -u`).
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
probe_clean="$(
  eval "$RUN_SUITE_SRC"
  env -u CCC_PROBE_LEAK -u NUNCHI_PROBE_LEAK bash -c "$(declare -f run_suite); run_suite '$PROBE'"
)"
ok "no harness vars set: suite still runs" '[[ "$probe_clean" == *"CCC=[unset]"* ]]'

# --- test-suite registration guard -----------------------------------------
# A *.test.sh that exists but is not in HARNESS_SUITES never runs, so the tree
# looks covered while its assertions are dead. Seven suites had drifted into
# that state; these pin both the list and the guard that protects it.
SUITES_SRC="$(sed -n '/^HARNESS_SUITES=(/,/)$/p' "$VALIDATE")"
GUARD_SRC="$(sed -n '/^# Registration guard/,/^fi$/p' "$VALIDATE")"
# Fail loudly if either extraction breaks, instead of silently passing on empty.
ok "HARNESS_SUITES array is present in validate-harness.sh" \
  '[ -n "$SUITES_SRC" ] && grep -q "observability.test.sh" <<<"$SUITES_SRC"'
ok "registration guard is present in validate-harness.sh" \
  '[ -n "$GUARD_SRC" ] && grep -q "unregistered test suite" <<<"$GUARD_SRC"'

# Every tracked suite must be registered. This is the property the guard
# enforces, asserted here directly so it fails in milliseconds rather than
# only at the end of a full harness run.
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
missing_suites="$(
  eval "$SUITES_SRC"
  cd "$ROOT" || exit 1
  git ls-files '*.test.sh' | while IFS= read -r s; do
    case " ${HARNESS_SUITES[*]} " in *" $s "*) ;; *) echo "$s" ;; esac
  done
)"
ok "every tracked *.test.sh is registered in HARNESS_SUITES" '[ -z "$missing_suites" ]'

# The guard itself must actually fire. Run the real extracted block against a
# stubbed git that reports one suite the list does not contain.
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
guard_out="$(
  eval "$SUITES_SRC"
  err() { echo "ERR: $*"; }
  say() { echo "SAY: $*"; }
  git() {
    case "$1" in
      rev-parse) return 0 ;;
      ls-files) printf '%s\n' "${HARNESS_SUITES[0]}" "scripts/never-registered.test.sh" ;;
      *) return 1 ;;
    esac
  }
  eval "$GUARD_SRC"
)"
ok "guard flags an unregistered suite" \
  '[ -n "$guard_out" ] && grep -q "ERR: unregistered test suite (never runs): scripts/never-registered.test.sh" <<<"$guard_out"'
ok "guard stays quiet when every suite is registered" \
  '! grep -q "ERR:" <<<"$(
     eval "$SUITES_SRC"
     err() { echo "ERR: $*"; }
     say() { echo "SAY: $*"; }
     git() { case "$1" in rev-parse) return 0 ;; ls-files) printf "%s\n" "${HARNESS_SUITES[0]}" ;; *) return 1 ;; esac; }
     eval "$GUARD_SRC"
   )"'

# --- suite summary-line contract ------------------------------------------
# The harness echoes each suite's own `PASS=<n> FAIL=<n>` tally. A suite that
# omits it used to still print "ok" with a blank count, so an empty run looked
# like a passing one. suite_summary() now demands the line.
SUMMARY_SRC="$(sed -n '/^suite_summary() {/,/^}$/p' "$VALIDATE")"
ok "suite_summary is present in validate-harness.sh" \
  '[ -n "$SUMMARY_SRC" ] && grep -q "PASS=\[0-9\]" <<<"$SUMMARY_SRC"'

summary_probe() { # <suite-output> -> "OK ..." / "ERR ..."
  (
    eval "$SUMMARY_SRC"
    err() { echo "ERR: $*"; }
    say() { echo "SAY: $*"; }
    printf '%s\n' "$1" >"$TMPD/probe.out"
    suite_summary "$TMPD/probe.out" "some/suite.test.sh"
  )
}
TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT

# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
conforming="$(summary_probe 'PASS=12 FAIL=0')"
ok "a conforming tally is reported next to the suite name" \
  '[[ "$conforming" == "SAY:   ok PASS=12 FAIL=0 some/suite.test.sh" ]]'

# The exact defect this guard exists for: six suites printed the lowercase
# variant, which the old `grep -E 'PASS='` missed and rendered as a blank.
# shellcheck disable=SC2034
lowercase="$(summary_probe 'pass=12 fail=0')"
ok "a lowercase pass=/fail= variant is rejected, not silently blank" \
  '[[ "$lowercase" == ERR:* ]] && grep -q "no .PASS=<n> FAIL=<n>. summary line" <<<"$lowercase"'

# shellcheck disable=SC2034
missing="$(summary_probe 'some unrelated output')"
ok "a suite with no summary line at all is rejected" '[[ "$missing" == ERR:* ]]'

# Guard against a partial match resurrecting the blank-count bug — e.g. a
# suite that mentions PASS= mid-line without a real tally.
# shellcheck disable=SC2034
partial="$(summary_probe 'checking PASS= handling')"
ok "a partial PASS= mention does not count as a summary" '[[ "$partial" == ERR:* ]]'

# --- #1476: subdirectory hooks reach the referenced-hook checks ---------------
# The REFS regex used to exclude `/`, so a hook wired as
# /root/.claude/hooks/distill/pending-drain.sh never entered REFS and checks 6/6a
# silently skipped it. Run the real extracted blocks against a fixture tree.
REFS_SRC="$(sed -n '/^# 6) hooks referenced by settings/,/^done$/p' "$VALIDATE")"
INSTALL_SRC="$(sed -n '/^mapfile -t DEPLOYED /,/^done$/p' "$VALIDATE")"
ok "referenced-hooks block (check 6) is present in validate-harness.sh" \
  '[ -n "$REFS_SRC" ] && grep -q "^mapfile -t REFS " <<<"$REFS_SRC"'
ok "installed-hooks block (check 6a) is present in validate-harness.sh" \
  '[ -n "$INSTALL_SRC" ] && grep -q "setup.sh installs" <<<"$INSTALL_SRC"'

FIX="$TMPD/hooks-fixture"
mkdir -p "$FIX/claude/hooks/sub"
printf '#!/bin/sh\n' > "$FIX/claude/hooks/top.sh"
printf '#!/bin/sh\n' > "$FIX/claude/hooks/sub/dir-hook.sh"
cat > "$FIX/claude/settings.base.json" <<'JSON'
{"hooks":{"SessionStart":[{"hooks":[
  {"type":"command","command":"bash /root/.claude/hooks/top.sh"},
  {"type":"command","command":"bash /root/.claude/hooks/sub/dir-hook.sh >/dev/null 2>&1 &"}
]}]}}
JSON
cat > "$FIX/claude/hooks/enforcement-overlay.json" <<'JSON'
{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"bash /root/.claude/hooks/../escape.sh"}]}]}}
JSON

VALIDATE_LIB="$ROOT/scripts/lib/harness-paths.sh"
refs_probe() { # <fixture-root> -> check 6 + 6a output with stubbed say/err
  (
    cd "$1" || exit 1
    # The extracted 6a block walks "$ROOT"; point it at the fixture for this
    # subshell only (SC2030/SC2031: that locality is the intent).
    # shellcheck disable=SC2030,SC2031
    ROOT="$1"
    err() { echo "ERR: $*"; }
    say() { echo "SAY: $*"; }
    # shellcheck source=lib/harness-paths.sh disable=SC1091
    . "$VALIDATE_LIB"
    eval "$REFS_SRC"
    eval "$INSTALL_SRC"
    printf 'REFS=%s\n' "${REFS[@]}"
  )
}
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
subdir_out="$(refs_probe "$FIX")"
ok "subdirectory hook referenced in settings enters REFS" \
  'grep -Fxq "REFS=/root/.claude/hooks/sub/dir-hook.sh" <<<"$subdir_out"'
ok "top-level hook still enters REFS" \
  'grep -Fxq "REFS=/root/.claude/hooks/top.sh" <<<"$subdir_out"'
ok "subdirectory hook present on disk passes check 6 by its relative path" \
  'grep -Fxq "SAY:   ok claude/hooks/sub/dir-hook.sh" <<<"$subdir_out"'
ok "subdirectory hook is matched against the hook-tree walk by its relative path (6a)" \
  'grep -Fxq "SAY:   ok setup.sh installs sub/dir-hook.sh" <<<"$subdir_out"'
ok "top-level hook still passes 6/6a" \
  'grep -Fxq "SAY:   ok claude/hooks/top.sh" <<<"$subdir_out" && grep -Fxq "SAY:   ok setup.sh installs top.sh" <<<"$subdir_out"'
ok "a .. segment in a hook reference is an error, not a silent skip" \
  'grep -Fq "ERR: settings hook reference escapes the hook tree: /root/.claude/hooks/../escape.sh" <<<"$subdir_out"'
# Every ERR line concerns the traversal reference (6 rejects it, 6a cannot find it
# in the walk); the well-formed top-level and subdirectory hooks raise none.
ok "no error is raised for the well-formed hooks in the fixture" \
  '! grep "^ERR:" <<<"$subdir_out" | grep -Fv "../escape.sh" | grep -q .'

# Remove the subdirectory hook: it must now be reported missing by 6 AND 6a.
rm -f "$FIX/claude/hooks/sub/dir-hook.sh"
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
missing_out="$(refs_probe "$FIX")"
ok "a referenced subdirectory hook missing from disk fails check 6" \
  'grep -Fq "ERR: settings references missing hook: /root/.claude/hooks/sub/dir-hook.sh (claude/hooks/sub/dir-hook.sh)" <<<"$missing_out"'
ok "a referenced subdirectory hook missing from the walk fails check 6a" \
  'grep -Fq "ERR: setup.sh does not install referenced hook: sub/dir-hook.sh" <<<"$missing_out"'

# Deliberately NOT asserted here: that all 81 registered suites honour the
# contract. Conformance is a property of a suite's *output*, and the source
# spellings vary legitimately (`echo "----"; echo "PASS=..."` on one line,
# `printf 'PASS=%d FAIL=%d\n'`, uppercase `$PASS`). A source grep gets that
# wrong in both directions. The harness enforces it on output, for every
# suite, on every run — that is the stronger check, so it is not duplicated
# with a weaker static approximation here.

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = "0" ]
