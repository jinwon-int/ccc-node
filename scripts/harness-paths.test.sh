#!/usr/bin/env bash
# Regression tests for the shared setup/self-update path safety library.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIB="$ROOT/scripts/lib/harness-paths.sh"
# shellcheck disable=SC2034  # SETUP is read via eval inside ok()
SETUP="$ROOT/setup.sh"
# shellcheck disable=SC2034  # SELFUP is read via eval inside ok()
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
  # shellcheck disable=SC2034  # expected is read via eval inside ok()
  expected="settings.json hooks output-styles headless.sh agents commands skills CLAUDE.md memories"
  ok "managed paths have one canonical ordered definition" '[ "${CCC_MANAGED_PATHS[*]}" = "$expected" ]'
  # settings.local.json is the node-local approvals file and must NOT be managed (#454).
  ok "settings.local.json is excluded from managed paths" '[[ " ${CCC_MANAGED_PATHS[*]} " != *" settings.local.json "* ]]'

  claude="$TMP/claude"; hermes="$TMP/hermes"; state="$claude/state"; repo="$TMP/repo"
  mkdir -p "$claude/hooks" "$hermes" "$state" "$repo"
  ok "setup roots accept distinct absolute paths" 'ccc_validate_setup_roots "$claude" "$hermes" >/dev/null 2>&1'
  ok "setup roots reject filesystem root" '! ccc_validate_setup_roots / "$hermes" >/dev/null 2>&1'
  ok "self-update roots require state under Claude root" '! ccc_validate_self_update_roots "$claude" "$hermes" "$TMP/outside" >/dev/null 2>&1'
  ok "repository cannot overlap install roots" '! ccc_validate_self_update_repo "$claude/repo" "$claude" "$hermes" >/dev/null 2>&1'
  ok "distinct repository path is accepted" 'ccc_validate_self_update_repo "$repo" "$claude" "$hermes" >/dev/null 2>&1'

  ln -s "$TMP/missing" "$claude/settings.json"
  ok "managed artifact symlink is rejected" \
    '! ccc_validate_managed_artifacts "ERROR:" "$claude" "$hermes" "${CCC_MANAGED_PATHS[@]}" >/dev/null 2>&1'
  rm -f "$claude/settings.json"
  printf x > "$TMP/hard-target"; ln "$TMP/hard-target" "$claude/settings.json"
  ok "managed artifact hardlink is rejected" \
    '! ccc_validate_managed_artifacts "self-update:" "$claude" "$hermes" "${CCC_MANAGED_PATHS[@]}" >/dev/null 2>&1'

  # Install manifests roll back with the trees they describe (#2742). They live
  # under state/, which is not a managed path, so they have their own helper.
  rm -f "$claude/settings.json"
  # shellcheck disable=SC2034  # expected_state is read via eval inside ok()
  expected_state="state/repo-skills.manifest state/repo-commands.manifest state/repo-output-styles.manifest"
  ok "install manifests have one canonical definition" '[ "${CCC_MANAGED_STATE_FILES[*]}" = "$expected_state" ]'
  ok "state/ itself is still not a managed path" '[[ " ${CCC_MANAGED_PATHS[*]} " != *" state "* ]]'
  snap="$TMP/manifest-snap"; mkdir -p "$snap"
  printf 'alpha v1\n' > "$state/repo-skills.manifest"
  printf 'node-local\n' > "$state/other-node-state.json"
  ok "manifest snapshot captures existing manifests only" \
    'ccc_snapshot_install_manifests "$claude" "$snap" && [ "$(ls "$snap/manifests")" = "repo-skills.manifest" ] && [ "$(stat -c %a "$snap/manifests")" = 700 ]'
  printf 'alpha v2\n' > "$state/repo-skills.manifest"
  printf 'cmd v2\n' > "$state/repo-commands.manifest"
  printf 'half\n' > "$state/repo-output-styles.manifest.tmp"
  printf 'node-local changed\n' > "$state/other-node-state.json"
  ok "manifest restore succeeds" 'ccc_restore_install_manifests "$claude" "$snap"'
  ok "manifest restore puts a rewritten manifest back byte-for-byte" '[ "$(cat "$state/repo-skills.manifest")" = "alpha v1" ]'
  ok "manifest restore removes a manifest the interrupted run created" \
    '[ ! -e "$state/repo-commands.manifest" ] && [ ! -e "$state/repo-output-styles.manifest.tmp" ]'
  ok "manifest restore leaves every other state file alone" \
    '[ "$(cat "$state/other-node-state.json")" = "node-local changed" ] && [ -z "$(find "$state" -name "*.restore.*" -print -quit)" ]'
  ok "manifest restore without a manifests/ snapshot fails instead of deleting" \
    '! ccc_restore_install_manifests "$claude" "$TMP/no-such-snap" 2>/dev/null && [ "$(cat "$state/repo-skills.manifest")" = "alpha v1" ]'
  rm -f "$state/repo-skills.manifest"; ln -s "$TMP/elsewhere" "$state/repo-skills.manifest"
  ok "manifest snapshot refuses a symlinked manifest" \
    '! ccc_snapshot_install_manifests "$claude" "$TMP/manifest-snap-link" 2>/dev/null'
  rm -f "$state/repo-skills.manifest"; mkdir "$state/repo-skills.manifest"
  ok "manifest snapshot refuses a non-regular manifest" \
    '! ccc_snapshot_install_manifests "$claude" "$TMP/manifest-snap-dir" 2>/dev/null'
  rmdir "$state/repo-skills.manifest"
fi

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
