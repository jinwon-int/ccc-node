#!/usr/bin/env bash
# Hermetic tests for scripts/lib/installer-gen-stamp.sh (#1081).
# shellcheck disable=SC2034  # stamp_*/rc variables are consumed via eval in ok()
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIB="$ROOT/scripts/lib/installer-gen-stamp.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccc-gen-stamp-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# shellcheck source=/dev/null
. "$LIB"

printf 'alpha\n' > "$TMP/a.sh"
printf 'beta\n'  > "$TMP/b.sh"

# Format: h_ + 12 lowercase hex.
stamp_a="$(ccc_installer_gen_stamp "$TMP/a.sh")"
ok "stamp format is h_<12 hex>" 'printf "%s" "$stamp_a" | grep -qE "^h_[0-9a-f]{12}$"'

# Deterministic for identical content.
ok "same file => same stamp" '[ "$(ccc_installer_gen_stamp "$TMP/a.sh")" = "$stamp_a" ]'

# Path-independent: identical content under a different path stamps the same.
mkdir -p "$TMP/elsewhere"
cp "$TMP/a.sh" "$TMP/elsewhere/renamed.sh"
ok "content-only digest ignores path" '[ "$(ccc_installer_gen_stamp "$TMP/elsewhere/renamed.sh")" = "$stamp_a" ]'

# Content-sensitive: any byte change re-stamps.
printf 'alpha2\n' > "$TMP/a2.sh"
ok "content change re-stamps" '[ "$(ccc_installer_gen_stamp "$TMP/a2.sh")" != "$stamp_a" ]'

# Distinct inputs stamp distinctly; input order matters (ordered inputs).
stamp_b="$(ccc_installer_gen_stamp "$TMP/b.sh")"
ok "distinct content => distinct stamp" '[ "$stamp_b" != "$stamp_a" ]'
stamp_ab="$(ccc_installer_gen_stamp "$TMP/a.sh" "$TMP/b.sh")"
ok "multi-input differs from single" '[ "$stamp_ab" != "$stamp_a" ] && [ "$stamp_ab" != "$stamp_b" ]'
ok "multi-input deterministic" '[ "$(ccc_installer_gen_stamp "$TMP/a.sh" "$TMP/b.sh")" = "$stamp_ab" ]'
ok "input order changes stamp" '[ "$(ccc_installer_gen_stamp "$TMP/b.sh" "$TMP/a.sh")" != "$stamp_ab" ]'

# sha256sum and the python3 fallback must agree: doctor and the installer may
# run in different environments. Force each path via a restricted PATH stub.
if command -v sha256sum >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
  # A PATH carrying only python3 forces the fallback branch.
  stub_bin="$TMP/stub-bin"; mkdir -p "$stub_bin"
  ln -s "$(command -v python3)" "$stub_bin/python3"
  py_stamp="$(PATH="$stub_bin" "$(command -v bash)" -c '. "$1"; ccc_installer_gen_stamp "$2" "$3"' _ "$LIB" "$TMP/a.sh" "$TMP/b.sh")"
  ok "python3 fallback agrees with sha256sum" '[ "$py_stamp" = "$stamp_ab" ]'
else
  ok "python3 fallback agrees with sha256sum (skipped: tool missing)" 'true'
fi

# Unsafe inputs are refused.
ccc_installer_gen_stamp "$TMP/missing.sh" >/dev/null 2>&1; rc=$?
ok "missing input fails" '[ "$rc" != 0 ]'
ln -s "$TMP/a.sh" "$TMP/link.sh"
ccc_installer_gen_stamp "$TMP/link.sh" >/dev/null 2>&1; rc=$?
ok "symlink input fails" '[ "$rc" != 0 ]'
ccc_installer_gen_stamp >/dev/null 2>&1; rc=$?
ok "no input fails" '[ "$rc" != 0 ]'
mkdir "$TMP/adir"
ccc_installer_gen_stamp "$TMP/adir" >/dev/null 2>&1; rc=$?
ok "directory input fails" '[ "$rc" != 0 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
