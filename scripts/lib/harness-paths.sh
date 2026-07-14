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
