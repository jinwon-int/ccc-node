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
