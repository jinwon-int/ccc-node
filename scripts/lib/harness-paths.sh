#!/usr/bin/env bash
# Shared managed-path inventory and safety checks for setup/self-update.
# This file is sourced: do not change the caller's shell options.

_CCC_HARNESS_PATHS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
_CCC_HARNESS_PATHS_PY="$_CCC_HARNESS_PATHS_DIR/harness_paths.py"

# settings.local.json is intentionally EXCLUDED: it is the node-local Claude Code
# approvals file, not a managed artifact. setup.sh seeds it from
# claude/settings.local.template.json only when absent; self-update must never
# redeploy, snapshot, or roll it back over a node's accumulated approvals (#454).
# shellcheck disable=SC2034  # sourced library: consumed by setup.sh / ccc-self-update.sh
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

# Codex GitHub-transport policy state (#1131). ccc_codex_github_policy.py
# CREATES $CODEX_DIR (0700) when absent, leaves a policy lock file behind, and
# atomically REPLACES config.toml keeping no backup — all inside setup.sh's
# install-transaction window. Unless the transaction (and self-update's
# artifact snapshot) capture this state explicitly, a rollback strands the new
# policy while reporting "restored previous installed artifacts". The
# managed-skills tree is NOT covered here: ccc_codex_skills.py carries its own
# fail-closed transaction scoped to $CODEX_DIR/skills.
CCC_CODEX_CONFIG_NAME="config.toml"
CCC_CODEX_POLICY_LOCK_NAME=".ccc-github-cli-policy.lock"

# Capture the policy-relevant state of <codex-dir> into <snapshot-dir>:
#   codex.tar.gz       — config.toml when present, an empty archive otherwise
#   codex-dir-existed  — marker, written only when <codex-dir> already existed
ccc_snapshot_codex_policy_state() { # <codex-dir> <snapshot-dir>
  local codex_dir="$1" snapshot_dir="$2"
  if [ -d "$codex_dir" ]; then
    : >"$snapshot_dir/codex-dir-existed" || return 1
  fi
  if [ -e "$codex_dir/$CCC_CODEX_CONFIG_NAME" ] || [ -L "$codex_dir/$CCC_CODEX_CONFIG_NAME" ]; then
    tar -czf "$snapshot_dir/codex.tar.gz" -C "$codex_dir" "$CCC_CODEX_CONFIG_NAME" || return 1
  else
    tar -czf "$snapshot_dir/codex.tar.gz" --files-from /dev/null || return 1
  fi
  tar -tzf "$snapshot_dir/codex.tar.gz" >/dev/null
}

# Restore what ccc_snapshot_codex_policy_state captured. config.toml returns
# byte-for-byte (or vanishes when the snapshot proves it did not exist); a
# $CODEX_DIR the interrupted run created is removed once empty. The policy
# lock file is unlinked only in that we-created-it case: unlinking a lock
# another process may hold open would break future locking, and skills
# residue from the separate skills transaction rightly keeps the directory.
ccc_restore_codex_policy_state() { # <codex-dir> <snapshot-dir>
  local codex_dir="$1" snapshot_dir="$2" failed=0
  rm -f -- "$codex_dir/$CCC_CODEX_CONFIG_NAME" || failed=1
  if [ -f "$snapshot_dir/codex-dir-existed" ]; then
    mkdir -p "$codex_dir" || failed=1
    tar -xzf "$snapshot_dir/codex.tar.gz" -C "$codex_dir" || failed=1
  elif [ -d "$codex_dir" ]; then
    rm -f -- "$codex_dir/$CCC_CODEX_POLICY_LOCK_NAME" || failed=1
    # A non-empty dir (e.g. managed-skills residue) is kept — that tree
    # belongs to the skills transaction, not to this rollback.
    rmdir -- "$codex_dir" 2>/dev/null || true
  fi
  [ "$failed" = 0 ]
}

# Operator-owned per-node Termux preparation budget (#2487). A slow Termux node
# needs ~50+ min for a full native wheel build whenever
# bridge/requirements.lock.txt changes (no wheel reuse), which overruns the
# shipped defaults. One file per node,
#   ${CCC_TERMUX_PREPARE_ENV_FILE:-${XDG_CONFIG_HOME:-~/.config}/ccc-node/termux-prepare.env}
# owned by the running user, mode 0600 (no group/world bits), holding
#   CCC_TERMUX_PREPARE_TIMEOUT=<seconds>      # integer 1..6900
# is read by scripts/termux-prepare-generation.sh (the --timeout-seconds it
# passes to termux_prepare.py) and by ccc-self-update.sh (the prepare-cmd
# budget, never below that value + CCC_TERMUX_PREPARE_BUDGET_MARGIN), so the two
# agree. The cap is self-update's 7200 s prepare maximum minus the margin.
# The file is parsed as KEY=VALUE text and NEVER sourced: blank lines and `#`
# comments are skipped, other keys are ignored with a warning, and a wrong
# owner, group/world permission bits, a symlink or any invalid
# CCC_TERMUX_PREPARE_TIMEOUT assignment makes the whole file be ignored with a
# warning (callers fall back to their defaults; an update is never aborted).
CCC_TERMUX_PREPARE_TIMEOUT_MAX=6900
# shellcheck disable=SC2034  # read by ccc-self-update.sh
CCC_TERMUX_PREPARE_BUDGET_MARGIN=300

ccc_termux_prepare_env_file() { # print the operator file path
  if [ -n "${CCC_TERMUX_PREPARE_ENV_FILE:-}" ]; then printf '%s' "$CCC_TERMUX_PREPARE_ENV_FILE"; return 0; fi
  local base="${XDG_CONFIG_HOME:-}"
  case "$base" in /*) ;; *) base="${HOME:-/root}/.config" ;; esac   # XDG: relative values are invalid
  printf '%s/ccc-node/termux-prepare.env' "$base"
}

# Sets CCC_TERMUX_PREPARE_FILE_TIMEOUT (validated seconds, or empty) and the
# array CCC_TERMUX_PREPARE_FILE_WARNINGS (why the file or a line was ignored;
# empty when absent or clean). Always returns 0, safe under `set -e`.
# shellcheck disable=SC2034  # results are read by the sourcing scripts
ccc_termux_prepare_read_env_file() {
  local f meta uid mode me line key val n=0 found="" bad=0
  CCC_TERMUX_PREPARE_FILE_TIMEOUT=""
  CCC_TERMUX_PREPARE_FILE_WARNINGS=()
  f="$(ccc_termux_prepare_env_file)"
  [ -e "$f" ] || [ -L "$f" ] || return 0
  if [ -L "$f" ] || [ ! -f "$f" ]; then
    CCC_TERMUX_PREPARE_FILE_WARNINGS+=("$f is not a regular file (symlinks are refused); file ignored")
    return 0
  fi
  me="$(id -u 2>/dev/null)" || me=""
  if ! meta="$(stat -c '%u %a' -- "$f" 2>/dev/null)" || [ -z "$me" ]; then
    CCC_TERMUX_PREPARE_FILE_WARNINGS+=("$f: owner/mode unreadable; file ignored")
    return 0
  fi
  uid="${meta%% *}"; mode="${meta##* }"
  if [ "$uid" != "$me" ]; then
    CCC_TERMUX_PREPARE_FILE_WARNINGS+=("$f is owned by uid $uid, not $me; file ignored")
    return 0
  fi
  if [[ ! "$mode" =~ ^[0-7]{3,4}$ ]] || (( (8#$mode & 8#077) != 0 )); then
    CCC_TERMUX_PREPARE_FILE_WARNINGS+=("$f has mode $mode (group/world access); chmod 600 it; file ignored")
    return 0
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
    case "$line" in ''|'#'*) continue ;; esac
    if [[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      CCC_TERMUX_PREPARE_FILE_WARNINGS+=("$f line $n is not KEY=VALUE; line ignored"); continue
    fi
    key="${line%%=*}"; val="${line#*=}"
    if [ "$key" != CCC_TERMUX_PREPARE_TIMEOUT ]; then
      CCC_TERMUX_PREPARE_FILE_WARNINGS+=("$f line $n: unsupported key ${key:0:64}; line ignored"); continue
    fi
    if [[ "$val" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$val" -le "$CCC_TERMUX_PREPARE_TIMEOUT_MAX" ]; then
      found="$val"
    else
      bad=1
      CCC_TERMUX_PREPARE_FILE_WARNINGS+=("$f line $n: CCC_TERMUX_PREPARE_TIMEOUT='${val:0:32}' is not an integer in 1..$CCC_TERMUX_PREPARE_TIMEOUT_MAX; file ignored")
    fi
  done < "$f"
  [ "$bad" = 0 ] && CCC_TERMUX_PREPARE_FILE_TIMEOUT="$found"
  return 0
}
