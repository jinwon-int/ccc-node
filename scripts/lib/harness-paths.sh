#!/usr/bin/env bash
# Shared managed-path inventory and safety checks for setup/self-update.
# This file is sourced: do not change the caller's shell options.

_CCC_HARNESS_PATHS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
_CCC_HARNESS_PATHS_PY="$_CCC_HARNESS_PATHS_DIR/harness_paths.py"

# settings.local.json is intentionally EXCLUDED: it is the node-local Claude Code
# approvals file, not a managed artifact. setup.sh seeds it from
# claude/settings.local.template.json only when absent; self-update must never
# redeploy, snapshot, or roll it back over a node's accumulated approvals (#454).
CCC_MANAGED_PATHS=(
  settings.json hooks output-styles headless.sh
  agents commands skills CLAUDE.md memories
)

# Deployable hook inventory (#569) — the SINGLE convention for which files under
# claude/hooks/ are installed to <claude-dir>/hooks. setup.sh deploys exactly
# this set (recursively, preserving structure, every file executable) and
# scripts/validate-harness.sh derives its expected-hooks set from the same walk,
# so adding a hook or lib file to the tree ships it everywhere with no list to
# update (the old 3-place hand list silently dropped lib/mtime-prune.sh, #564).
#
# A file under claude/hooks/ is DEPLOYED unless it is:
#   - a test suite (*.test.sh) or the shared test fixture (lib/test-stub.sh),
#   - Python bytecode (__pycache__/ directories, *.pyc),
#   - documentation (*.md — tools-cheatsheet.md is SEEDED separately, only when
#     absent, because nodes may customize it),
#   - hook WIRING consumed at settings-compose/plugin time, never deployed as a
#     file (hooks.json, enforcement-overlay.json).
# validate-harness.sh cross-checks this exclusion list against the walk output,
# so an accidental new exclusion fails CI instead of silently not shipping.
ccc_hook_tree_files() { # <repo-root> — emit deployable paths relative to claude/hooks/, sorted
  local hooks_root="$1/claude/hooks"
  [ -d "$hooks_root" ] || return 1
  (
    cd "$hooks_root" || exit 1
    find . -name __pycache__ -prune -o -type f \
      ! -name '*.test.sh' \
      ! -name 'test-stub.sh' \
      ! -name '*.pyc' \
      ! -name '*.md' \
      ! -name 'hooks.json' \
      ! -name 'enforcement-overlay.json' \
      -print
  ) | sed 's|^\./||' | LC_ALL=C sort
}

_ccc_require_path_validator() {
  command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' "$1" >&2
    return 1
  }
  [ -r "$_CCC_HARNESS_PATHS_PY" ] || {
    printf '%s\n' "$2" >&2
    return 1
  }
}

ccc_validate_setup_roots() {
  _ccc_require_path_validator \
    "ERROR: python3 is required to validate install paths" \
    "ERROR: shared install path validator is missing" || return 1
  python3 "$_CCC_HARNESS_PATHS_PY" setup-roots "$@"
}

ccc_validate_self_update_roots() {
  _ccc_require_path_validator \
    "self-update: python3 is required to validate runtime paths" \
    "self-update: shared runtime path validator is missing" || return 1
  python3 "$_CCC_HARNESS_PATHS_PY" self-update-roots "$@"
}

ccc_validate_self_update_repo() {
  _ccc_require_path_validator \
    "self-update: python3 is required to validate repository path" \
    "self-update: shared repository path validator is missing" || return 1
  python3 "$_CCC_HARNESS_PATHS_PY" self-update-repo "$@"
}

ccc_validate_managed_artifacts() {
  _ccc_require_path_validator \
    "${1:-ERROR:} python3 is required to validate managed artifacts" \
    "${1:-ERROR:} shared managed-artifact validator is missing" || return 1
  python3 "$_CCC_HARNESS_PATHS_PY" managed-artifacts "$@"
}
