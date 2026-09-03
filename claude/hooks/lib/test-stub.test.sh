#!/usr/bin/env bash
# Tests for lib/test-stub.sh — Termux-safe, fail-closed fixture writes.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

TMP="$(ccc_test_tmpdir)" || exit 1
OUTSIDE="$(ccc_test_tmpdir "$(dirname "$TMP")/ccc-test-outside.XXXXXX")" || exit 1
trap 'rm -rf "$TMP" "$OUTSIDE"' EXIT
mkdir -p "$TMP/bin"

write_exec_stub "$TMP/bin/s" <<'SH'
printf 'ran:%s\n' "$1"
SH

ok "stub is executable" '[ -x "$TMP/bin/s" ]'
# Termux/Android has no /usr/bin/env — the whole point of #472.
ok "shebang is NOT /usr/bin/env" '! head -1 "$TMP/bin/s" | grep -q "/usr/bin/env"'
ok "shebang points at a real, executable bash interpreter" \
  '[ -x "$(head -1 "$TMP/bin/s" | sed "s|^#!||")" ]'
ok "stub execs directly via its own shebang" '[ "$("$TMP/bin/s" hi)" = "ran:hi" ]'

write_exec_stub "$TMP/bin/s" <<'SH'
printf 'replaced\n'
SH
ok "single-link regular stub can be replaced atomically" \
  '[ "$("$TMP/bin/s")" = "replaced" ] && [ "$(_ccc_test_nlink "$TMP/bin/s")" = 1 ]'

if printf 'unsafe\n' | write_exec_stub "" 2>/dev/null; then
  empty_rc=0
else
  empty_rc=$?
fi
ok "empty destination is rejected before redirection" '[ "$empty_rc" -ne 0 ]'

if printf 'unsafe\n' | write_exec_stub "relative-stub" 2>/dev/null; then
  relative_rc=0
else
  relative_rc=$?
fi
ok "relative destination is rejected before redirection" \
  '[ "$relative_rc" -ne 0 ] && [ ! -e relative-stub ]'

if printf 'unsafe\n' | write_exec_stub "$TMP/missing-parent/stub" 2>/dev/null; then
  missing_parent_rc=0
else
  missing_parent_rc=$?
fi
ok "missing destination parent is rejected before redirection" \
  '[ "$missing_parent_rc" -ne 0 ] && [ ! -e "$TMP/missing-parent/stub" ]'

sentinel="$TMP/sentinel"
printf 'keep\n' > "$sentinel"
ln -s "$sentinel" "$TMP/bin/symlink-stub"
if write_exec_stub "$TMP/bin/symlink-stub" <<'SH' 2>/dev/null
printf 'clobbered\n'
SH
then symlink_rc=0; else symlink_rc=$?; fi
ok "symlink destination is rejected without touching its target" \
  '[ "$symlink_rc" -ne 0 ] && [ "$(cat "$sentinel")" = keep ]'

printf 'hardlink-keep\n' > "$TMP/bin/hardlink-source"
ln "$TMP/bin/hardlink-source" "$TMP/bin/hardlink-stub"
if write_exec_stub "$TMP/bin/hardlink-stub" <<'SH' 2>/dev/null
printf 'clobbered\n'
SH
then hardlink_rc=0; else hardlink_rc=$?; fi
ok "multiply-linked destination is rejected without touching its peer" \
  '[ "$hardlink_rc" -ne 0 ] && [ "$(cat "$TMP/bin/hardlink-source")" = hardlink-keep ]'

mkdir -p "$TMP/real-parent"
ln -s "$TMP/real-parent" "$TMP/link-parent"
if write_exec_stub "$TMP/link-parent/stub" <<'SH' 2>/dev/null
printf 'unsafe\n'
SH
then parent_link_rc=0; else parent_link_rc=$?; fi
ok "symlink destination parent is rejected" \
  '[ "$parent_link_rc" -ne 0 ] && [ ! -e "$TMP/real-parent/stub" ]'

mkdir -p "$OUTSIDE/bin"
if write_exec_stub "$OUTSIDE/bin/stub" <<'SH' 2>/dev/null
printf 'escaped\n'
SH
then outside_rc=0; else outside_rc=$?; fi
ok "destination outside fixture root is rejected" \
  '[ "$outside_rc" -ne 0 ] && [ ! -e "$OUTSIDE/bin/stub" ]'

ok "mktemp failure is rejected" \
  '! (mktemp() { return 1; }; ccc_test_tmpdir >/dev/null 2>&1)'
ok "mktemp partial-output failure is rejected" \
  '! (mktemp() { printf "%s\n" "$TMP"; return 1; }; ccc_test_tmpdir >/dev/null 2>&1)'
ok "mktemp empty success is rejected" \
  '! (mktemp() { return 0; }; ccc_test_tmpdir >/dev/null 2>&1)'
touch "$TMP/not-a-dir"
ok "mktemp file result is rejected" \
  '! (mktemp() { printf "%s\n" "$TMP/not-a-dir"; }; ccc_test_tmpdir >/dev/null 2>&1)'
ok "mktemp relative result is rejected" \
  '! (mktemp() { printf "relative-dir\n"; }; ccc_test_tmpdir >/dev/null 2>&1)'
mkdir "$TMP/open-dir"
chmod 755 "$TMP/open-dir"
ok "mktemp non-private directory is rejected" \
  '! (mktemp() { printf "%s\n" "$TMP/open-dir"; }; ccc_test_tmpdir >/dev/null 2>&1)'
mkdir "$TMP/link-target"
ln -s "$TMP/link-target" "$TMP/link-dir"
ok "mktemp symlink directory is rejected" \
  '! (mktemp() { printf "%s\n" "$TMP/link-dir"; }; ccc_test_tmpdir >/dev/null 2>&1)'

# Regression pin: the hermetic suites must not reintroduce a raw
# `#!/usr/bin/env bash` stub shebang — the only such line in each file is its
# own line-1 shebang (stubs go through write_exec_stub).
HOOKS="$(cd "$HERE/.." && pwd)"
for t in distill/extract.test.sh skill-review.test.sh distill-scope.test.sh; do
  # shellcheck disable=SC2034  # $n is consumed via eval in ok()
  n="$(grep -c '#!/usr/bin/env bash' "$HOOKS/$t")"
  ok "$t writes no /usr/bin/env stub shebang" '[ "$n" = 1 ]'
done

# Every test suite that writes through a literal $TMP/bin path must allocate
# TMP through the checked helper. This catches new direct writers even when
# they do not use write_exec_stub.
audit_fail=0
while IFS= read -r test_file; do
  if ! grep -q 'TMP="$(ccc_test_tmpdir' "$test_file"; then
    echo "unsafe TMP/bin fixture root: $test_file"
    audit_fail=1
  fi
done < <(grep -RIl '\$TMP/bin' --include='*.test.sh' "$ROOT/claude" "$ROOT/scripts")
ok "all TMP/bin fixture writers use checked temp roots" '[ "$audit_fail" = 0 ]'

# ccc_test_reset_hook_env — ambient harness variables must not reach fixtures (#1023).
reset_probe() {
  # Run in a child so the assertions below observe a known starting env.
  env CCC_BRIDGE_DISTILL_MANAGED=1 CCC_STATE_DIR=/ambient NUNCHI_HOME=/ambient \
    CCC_TEST_STUB_ROOT=/keep CCC_KEEP_ME=/keep PATH="$PATH" HOME="$HOME" \
    bash -c '
      . "$1/test-stub.sh"
      ccc_test_reset_hook_env CCC_KEEP_ME
      printf "managed=[%s] state=[%s] nunchi=[%s] stubroot=[%s] keep=[%s] home=[%s]\n" \
        "${CCC_BRIDGE_DISTILL_MANAGED:-}" "${CCC_STATE_DIR:-}" "${NUNCHI_HOME:-}" \
        "${CCC_TEST_STUB_ROOT:-}" "${CCC_KEEP_ME:-}" "${HOME:+set}"
    ' _ "$HERE"
}
# shellcheck disable=SC2034  # $reset_out is consumed via eval in ok()
reset_out="$(reset_probe)"

ok "reset clears the bridge-managed distill flag" \
  '[[ "$reset_out" == *"managed=[]"* ]]'
ok "reset clears ambient CCC_* fixture overrides" \
  '[[ "$reset_out" == *"state=[]"* ]]'
ok "reset clears ambient NUNCHI_* overrides" \
  '[[ "$reset_out" == *"nunchi=[]"* ]]'
ok "reset preserves CCC_TEST_* fixture plumbing" \
  '[[ "$reset_out" == *"stubroot=[/keep]"* ]]'
ok "reset preserves explicitly named variables" \
  '[[ "$reset_out" == *"keep=[/keep]"* ]]'
ok "reset leaves unrelated environment untouched" \
  '[[ "$reset_out" == *"home=[set]"* ]]'

# The helper only fixes suites that actually call it, so audit every suite that
# sources the stub -- not just the distill lane it was introduced for. The leak
# is invisible in CI (which exports no CCC_*) and only bites operators running
# validation on a real node, so a missing call has to fail as an assertion here
# rather than as scattered misses somewhere else. Surveyed on nosuk/vps2: six
# suites were losing ~165 assertions to inherited state, and every suite in this
# audit passes with no CCC_* set at all, so resetting is safe for all of them.
reset_audit_fail=0
while IFS= read -r test_file; do
  grep -q 'lib/test-stub.sh' "$test_file" || continue
  if ! grep -q '^ccc_test_reset_hook_env' "$test_file"; then
    echo "suite does not reset inherited hook env: $test_file"
    reset_audit_fail=1
  fi
done < <(find "$ROOT/claude" "$ROOT/scripts" -name '*.test.sh')
# shellcheck disable=SC2034  # $reset_audit_fail is consumed via eval in ok()
ok "every stub-sourcing suite resets inherited hook environment" '[ "$reset_audit_fail" = 0 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
