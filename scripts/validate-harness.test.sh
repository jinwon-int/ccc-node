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

# --- #1484: the plan is DISCOVERED, not hand-listed --------------------------
# HARNESS_SUITES / UMASK_SUITES / PY_COMPILE_FILES / SC_SCOPE used to be
# explicit arrays (almost every commit to the script in the 30 days before
# #1484 was list maintenance) with a registration guard proving the suite
# list was redundant with `git ls-files`. discover_plan now derives all four from the index plus
# small guarded manifests. Run the real extracted block inside a fixture repo
# with fixture manifests and pin every rule and guard.
DISCOVERY_SRC="$(sed -n '/^# --- discovery (#1484)/,/^# --- end discovery/p' "$VALIDATE")"
ok "discovery block is present in validate-harness.sh" \
  '[ -n "$DISCOVERY_SRC" ] && grep -q "^discover_plan() {" <<<"$DISCOVERY_SRC"'

DFIX="$HOSTILE/discovery-fixture"
mkdir -p "$DFIX/a" "$DFIX/b" "$DFIX/c" "$DFIX/bridge" "$DFIX/vendor" "$DFIX/extra"
printf '#!/usr/bin/env bash\n# harness: umask-rerun\n# more header\nset -u\n' > "$DFIX/a/one.test.sh"
# Marker AFTER the first non-comment line: not a header declaration, must not count.
printf '#!/usr/bin/env bash\nset -u\n# harness: umask-rerun\n' > "$DFIX/a/two.test.sh"
printf '#!/usr/bin/env bash\n' > "$DFIX/b/skip.test.sh"
printf 'echo no shebang here\n' > "$DFIX/c/noshebang.test.sh"
printf 'x = 1\n' > "$DFIX/x.py"
printf 'y = 1\n' > "$DFIX/bridge/y.py"
printf 'z = 1\n' > "$DFIX/vendor/z.py"
for f in "$DFIX/s1.sh" "$DFIX/s2.sh" "$DFIX/extra/hook"; do printf '#!/bin/sh\n' > "$f"; done
git -C "$DFIX" init -q 2>/dev/null
git -C "$DFIX" add -A 2>/dev/null
# An untracked suite must stay invisible: discovery reads the index, not the tree.
printf '#!/usr/bin/env bash\n' > "$DFIX/c/untracked.test.sh"

disco_probe() { # <manifest assignments> -> ERR/RC/array dump of one discover_plan run
  (
    cd "$DFIX" || exit 1
    err() { echo "ERR: $*"; }
    say() { echo "SAY: $*"; }
    fail=0
    eval "$1"
    eval "$DISCOVERY_SRC"
    discover_plan; echo "RC=$?"
    printf 'SUITE=%s\n' ${HARNESS_SUITES[@]+"${HARNESS_SUITES[@]}"}
    printf 'UMASK=%s\n' ${UMASK_SUITES[@]+"${UMASK_SUITES[@]}"}
    printf 'PY=%s\n' ${PY_COMPILE_FILES[@]+"${PY_COMPILE_FILES[@]}"}
    printf 'SC=%s\n' ${SC_SCOPE[@]+"${SC_SCOPE[@]}"}
    echo "EXCLUDED=$HARNESS_EXCLUDED_N"
  )
}
FIX_MANIFEST='HARNESS_EXCLUDE=(b/skip.test.sh); UMASK_MARKER="# harness: umask-rerun";
  PY_COMPILE_EXCLUDE=(bridge/ vendor/); SC_SCOPE_EXTRA=(extra/hook); SC_WARN_BASELINE=(s2.sh)'
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
disco_out="$(disco_probe "$FIX_MANIFEST")"
ok "suites = every tracked *.test.sh in index order minus HARNESS_EXCLUDE (untracked stays invisible)" \
  '[ "$(grep "^SUITE=" <<<"$disco_out" | tr "\n" " ")" = "SUITE=a/one.test.sh SUITE=a/two.test.sh SUITE=c/noshebang.test.sh " ]'
ok "excluded suite is counted, not run" 'grep -Fxq "EXCLUDED=1" <<<"$disco_out"'
ok "a suite without a shebang is a discovery finding" \
  'grep -Fxq "ERR: test suite has no shebang line: c/noshebang.test.sh" <<<"$disco_out" && grep -Fxq "RC=1" <<<"$disco_out"'
ok "umask set = suites with the marker in the leading comment block only" \
  '[ "$(grep "^UMASK=" <<<"$disco_out" | tr "\n" " ")" = "UMASK=a/one.test.sh " ]'
ok "py_compile = tracked *.py minus PY_COMPILE_EXCLUDE prefixes" \
  '[ "$(grep "^PY=" <<<"$disco_out" | tr "\n" " ")" = "PY=x.py " ]'
# *.test.sh files are *.sh too, so suites are linted as well (as on main).
ok "shellcheck scope = tracked *.sh + SC_SCOPE_EXTRA - SC_WARN_BASELINE" \
  '[ "$(grep "^SC=" <<<"$disco_out" | tr "\n" " ")" = "SC=a/one.test.sh SC=a/two.test.sh SC=b/skip.test.sh SC=c/noshebang.test.sh SC=extra/hook SC=s1.sh " ]'
ok "no stale-manifest finding when every manifest entry is tracked" \
  '! grep -q "^ERR: stale" <<<"$disco_out"'

# Stale manifest entries (file gone / never tracked) must fail, not rot.
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
stale_out="$(disco_probe 'HARNESS_EXCLUDE=(b/gone.test.sh); UMASK_MARKER="# harness: umask-rerun";
  PY_COMPILE_EXCLUDE=(bridge/); SC_SCOPE_EXTRA=(extra/nope); SC_WARN_BASELINE=(s2.sh missing.sh)')"
ok "stale HARNESS_EXCLUDE entry is a finding" \
  'grep -Fxq "ERR: stale HARNESS_EXCLUDE entry (not tracked): b/gone.test.sh" <<<"$stale_out"'
ok "stale SC_SCOPE_EXTRA entry is a finding" \
  'grep -Fxq "ERR: stale SC_SCOPE_EXTRA entry (not tracked): extra/nope" <<<"$stale_out"'
ok "stale SC_WARN_BASELINE entry is a finding; tracked entries are not" \
  'grep -Fxq "ERR: stale SC_WARN_BASELINE entry (not tracked): missing.sh" <<<"$stale_out" && ! grep -Fq "not tracked): s2.sh" <<<"$stale_out"'

# The umask-0002 contract (#770) must never silently lose all coverage.
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
nomarker_out="$(disco_probe 'HARNESS_EXCLUDE=(); UMASK_MARKER="# harness: never-declared";
  PY_COMPILE_EXCLUDE=(bridge/); SC_SCOPE_EXTRA=(); SC_WARN_BASELINE=(s2.sh)')"
ok "an empty umask-rerun set is a finding" \
  'grep -q "^ERR: no suite declares .# harness: never-declared." <<<"$nomarker_out"'

# Without git there is no index to discover from: fail closed (empty plan +
# finding), never a silently green run with zero suites.
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
nogit_out="$(
  cd "$DFIX" || exit 1
  err() { echo "ERR: $*"; }
  say() { echo "SAY: $*"; }
  git() { return 1; }
  eval "$FIX_MANIFEST"
  eval "$DISCOVERY_SRC"
  discover_plan; echo "RC=$?"
  echo "N=${#HARNESS_SUITES[@]}"
)"
ok "git unavailable: discovery fails closed with an empty plan" \
  'grep -q "^ERR: git is required" <<<"$nogit_out" && grep -Fxq "RC=1" <<<"$nogit_out" && grep -Fxq "N=0" <<<"$nogit_out"'

# Real repo: the discovered plan must equal the tracked set (HARNESS_EXCLUDE is
# empty today), carry no finding, and every umask-rerun suite must really
# declare the marker in its header. Asserted via the --dump-plan seam so it
# fails in milliseconds rather than only at the end of a full harness run.
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
plan_out="$(cd "$ROOT" && bash "$VALIDATE" --dump-plan; echo "RC=$?")"
ok "--dump-plan on the real repo exits 0 with no FAIL line" \
  'grep -Fxq "RC=0" <<<"$plan_out" && ! grep -q "^FAIL:" <<<"$plan_out"'
ok "real repo: discovered suites == git ls-files *.test.sh" \
  'diff <(awk -F"\t" "\$1==\"suite\"{print \$2}" <<<"$plan_out") <(cd "$ROOT" && git ls-files "*.test.sh") >/dev/null'
ok "real repo: every umask-rerun suite carries the marker on a header line" \
  '[ "$(grep -c "^umask-rerun" <<<"$plan_out")" -gt 0 ] && ! awk -F"\t" "\$1==\"umask-rerun\"{print \$2}" <<<"$plan_out" | while IFS= read -r s; do grep -Fxq "# harness: umask-rerun" "$ROOT/$s" || echo "$s"; done | grep -q .'
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
TAB=$'\t'
ok "real repo: py_compile scope excludes bridge/ and is non-empty" \
  '[ "$(grep -c "^py_compile" <<<"$plan_out")" -gt 0 ] && ! grep -q "^py_compile${TAB}bridge/" <<<"$plan_out"'
ok "real repo: warning-level shellcheck scope includes the suffix-less git hook" \
  'grep -Fxq "shellcheck-warning${TAB}scripts/git-hooks/managed-checkout-guard" <<<"$plan_out"'

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
