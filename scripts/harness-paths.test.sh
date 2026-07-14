#!/usr/bin/env bash
# Regression tests for the shared setup/self-update path safety library.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIB="$ROOT/scripts/lib/harness-paths.sh"
SETUP="$ROOT/setup.sh"
SELFUP="$ROOT/scripts/ccc-self-update.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

ok "shared path-safety library exists" '[ -r "$LIB" ] && [ -r "$ROOT/scripts/lib/harness_paths.py" ]'
ok "setup no longer defines a private managed-path array" '! grep -q "^CLAUDE_MANAGED=(" "$SETUP"'
ok "self-update no longer defines a private managed-path array" '! grep -q "^INSTALLED_PATHS=(" "$SELFUP"'
ok "setup sources the shared library" 'grep -Fq "scripts/lib/harness-paths.sh" "$SETUP"'
ok "self-update sources the colocated shared library" 'grep -Fq "lib/harness-paths.sh" "$SELFUP"'
ok "legacy inline validators are removed" \
  'python3 - "$SETUP" "$SELFUP" <<'"'"'PY'"'"'
from pathlib import Path
import sys
setup, selfup = (Path(p).read_text() for p in sys.argv[1:])
assert "validate_install_roots()" not in setup
assert "validate_managed_artifacts()" not in setup
assert "validate_runtime_paths()" not in selfup
assert "validate_repo_path()" not in selfup
PY'
ok "shared implementation stays portable" \
  '[ -r "$LIB" ] && ! grep -Eq "(^|[^[:alnum:]_])(flock|readlink)([^[:alnum:]_]|$)" "$LIB"'

if [ -r "$LIB" ]; then
  # shellcheck source=/dev/null
  . "$LIB"
  expected="settings.json settings.local.json hooks output-styles headless.sh agents commands skills CLAUDE.md memories"
  ok "managed paths have one canonical ordered definition" '[ "${CCC_MANAGED_PATHS[*]}" = "$expected" ]'

  claude="$TMP/claude"; hermes="$TMP/hermes"; state="$claude/state"; repo="$TMP/repo"
  mkdir -p "$claude/hooks" "$hermes" "$state" "$repo"
  ok "setup roots accept distinct absolute paths" 'ccc_validate_setup_roots "$claude" "$hermes" >/dev/null 2>&1'
  ok "setup roots reject filesystem root" '! ccc_validate_setup_roots / "$hermes" >/dev/null 2>&1'
  ok "self-update roots require state under Claude root" '! ccc_validate_self_update_roots "$claude" "$hermes" "$TMP/outside" >/dev/null 2>&1'
  ok "repository cannot overlap install roots" '! ccc_validate_self_update_repo "$claude/repo" "$claude" "$hermes" >/dev/null 2>&1'
  ok "distinct repository path is accepted" 'ccc_validate_self_update_repo "$repo" "$claude" "$hermes" >/dev/null 2>&1'

  ln -s "$TMP/missing" "$claude/settings.local.json"
  ok "managed artifact symlink is rejected" \
    '! ccc_validate_managed_artifacts "ERROR:" "$claude" "$hermes" "${CCC_MANAGED_PATHS[@]}" >/dev/null 2>&1'
  rm -f "$claude/settings.local.json"
  printf x > "$TMP/hard-target"; ln "$TMP/hard-target" "$claude/settings.local.json"
  ok "managed artifact hardlink is rejected" \
    '! ccc_validate_managed_artifacts "self-update:" "$claude" "$hermes" "${CCC_MANAGED_PATHS[@]}" >/dev/null 2>&1'
fi

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
