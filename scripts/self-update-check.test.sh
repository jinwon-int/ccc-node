#!/usr/bin/env bash
# Tests for claude/skills/self-update/check.sh — the self-update detection step.
#
# The regression this pins (#1033): check.sh answered "is the checkout current"
# while printing a verdict operators read as "is the harness current". A
# `git pull` with no `setup.sh` left ~/.claude serving old code and this script
# still said "up to date", so the stale install was never surfaced.
set -uo pipefail
umask 077
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CHECK="$ROOT/claude/skills/self-update/check.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

# A fixture repo: a real git checkout with an origin, so check.sh's git probes
# behave exactly as they do on a node.
UPSTREAM="$TMP/upstream.git"
REPO="$TMP/repo"
git init --quiet --bare "$UPSTREAM"
git init --quiet "$REPO"
(
  cd "$REPO" || exit 1
  git config user.email t@example.com
  git config user.name t
  git checkout -q -b main
  mkdir -p scripts
  echo seed > seed.txt
  git add -A && git commit -q -m seed
  git remote add origin "$UPSTREAM"
  git push -q -u origin main
) || exit 1

# Stub ccc_doctor.py: check.sh must treat its JSON as data and its exit status
# as meaningless, because the real doctor exits non-zero exactly when it finds
# the drift we care about.
write_doctor_stub() {
  local body="$1" rc="$2"
  mkdir -p "$REPO/scripts"
  cat > "$REPO/scripts/ccc_doctor.py" <<PY
import sys
sys.stdout.write('''$body''')
sys.exit($rc)
PY
}

run_check() { CCC_REPO_DIR="$REPO" bash "$CHECK" 2>&1; }

CLEAN_JSON='{"rows": [{"item": "hooks/a.sh", "status": "installed"}]}'
STALE_JSON='{"rows": [{"item": "hooks/a.sh", "status": "installed"}, {"item": "hooks/nunchi/nunchi.py", "status": "drifted"}, {"item": "hooks/b.sh", "status": "missing"}]}'

# --- checkout current + installed copies match -------------------------------
write_doctor_stub "$CLEAN_JSON" 0
out="$(run_check)"
ok "clean install does not claim more than the checkout" \
  '[[ "$out" == *"checkout up to date"* ]] && [[ "$out" != *"no harness update needed"* ]]'
ok "clean install reports the installed harness explicitly" \
  '[[ "$out" == *"installed harness (~/.claude): matches this checkout."* ]]'

# --- checkout current + installed copies stale (the #1033 regression) --------
write_doctor_stub "$STALE_JSON" 1
out="$(run_check)"
ok "stale install is not reported as up to date" \
  '[[ "$out" != *"STATUS: checkout up to date — no repo update needed"* ]]'
ok "stale install names setup.sh in the STATUS line" \
  '[[ "$out" == *"INSTALLED harness is stale"* ]] && [[ "$out" == *"setup.sh"* ]]'
ok "stale install lists the drifted file" \
  '[[ "$out" == *"drifted"*"hooks/nunchi/nunchi.py"* ]]'
ok "stale install lists the missing file" \
  '[[ "$out" == *"missing"*"hooks/b.sh"* ]]'
# doctor exits 1 whenever it classifies drift; a pipefail-style read of that
# status silently degraded the check to "not checked" during development.
ok "doctor's non-zero exit is data, not a failure signal" \
  '[[ "$out" != *"NOT CHECKED"* ]]'

# --- doctor unavailable ------------------------------------------------------
rm -f "$REPO/scripts/ccc_doctor.py"
out="$(run_check)"
ok "missing doctor degrades to an explicit unverified state" \
  '[[ "$out" == *"NOT CHECKED"* ]]'
ok "missing doctor never claims the installed harness is fine" \
  '[[ "$out" != *"matches this checkout"* ]]'

# --- doctor emits unparseable output ----------------------------------------
write_doctor_stub 'not json at all' 0
out="$(run_check)"
ok "unparseable doctor output degrades to unverified" \
  '[[ "$out" == *"NOT CHECKED"* ]]'

# --- checkout behind: the update path still reports -------------------------
write_doctor_stub "$CLEAN_JSON" 0
(
  cd "$REPO" || exit 1
  echo next >> seed.txt
  git add -A && git commit -q -m next
  git push -q origin main
  git reset -q --hard HEAD~1
) || exit 1
# shellcheck disable=SC2034  # $out is consumed via eval in ok()
out="$(run_check)"
ok "behind checkout still reports an available update" \
  '[[ "$out" == *"commit(s) behind origin/main"* ]]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
