#!/usr/bin/env bash
# Bootstrap a new "Claude Code node" (클코 노드) from this template.
# Installs the SessionStart memory + tool-cheatsheet hooks, the PreCompact/PostCompact
# working-state checkpoint hook, and sanitized settings into ~/.claude,
# and lays down per-node templates you then fill in. NEVER writes secrets.
#
# Usage:
#   ./setup.sh                 # standalone: install full settings (portable hooks included)
#   ./setup.sh --with-plugin   # plugin mode: lean settings; the ccc-node PLUGIN owns the
#                              #   portable hooks (audit/redact/notify) — avoids the
#                              #   double-firing you'd get if both settings.json and the
#                              #   plugin registered them. Node-local hooks stay in settings.
#   ./setup.sh --dry-run       # show what would happen, change nothing (combine with above)
#   ./setup.sh --no-backup     # skip the durable operator backup (failure rollback remains enabled)
#
# Node-identity seeding (optional): when these are given, freshly-seeded CLAUDE.md / MEMORY.md /
# USER.md have their <PLACEHOLDER> tokens substituted automatically (existing files are never
# touched). Anything you omit is left as <PLACEHOLDER> for you to fill in by hand.
#   --node <name>            e.g. soonwook      -> <NODE_NAME>
#   --display <name>         e.g. 순욱           -> <NODE_DISPLAY_NAME>
#   --slot <slot>            e.g. VPS6          -> <PHYSICAL_SLOT>
#   --fleet-role <role>      e.g. "Team2 worker" -> <FLEET_ROLE>
#   --lang <language>        e.g. Korean        -> <LANGUAGE>
#   --user-name <name>                          -> <USER_NAME>
#   --user-gh <handle>                          -> <USER_GH>
#   --user-tz <tz>           e.g. Asia/Seoul    -> <USER_TZ>
#   --user-context <text>                       -> <USER_CONTEXT>
set -euo pipefail

# Non-login execution contexts may leave HOME unset (systemd exports HOME only
# when User= is explicit, so transient units and timers running root get none).
# ccc-self-update runs this script headless, and `set -u` would abort at the
# first $HOME default below — seen live on gwakga 2026-08-02
# ("setup.sh: line 87: HOME: unbound variable" → fail-closed rollback).
# Derive the invoking user's home instead of dying.
if [ -z "${HOME:-}" ]; then
  HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)"
  # Last resort mirrors ccc-self-update's own `${HOME:-/root}` convention.
  [ -n "$HOME" ] || HOME=/root
  export HOME
fi

DRY=0; WITH_PLUGIN=0; BACKUP=1
OPT_NODE=""; OPT_DISPLAY=""; OPT_SLOT=""; OPT_FLEET_ROLE=""; OPT_LANG=""
OPT_USER_NAME=""; OPT_USER_GH=""; OPT_USER_TZ=""; OPT_USER_CONTEXT=""
need_val() { [ -n "${2:-}" ] || { echo "Flag $1 requires a value" >&2; exit 2; }; }
_ccc_is_root() {
  local uid="" test_root="" test_target="" readlink_bin="" candidate
  # Deterministic CI seam for the root-aware bypassPermissions neutralization,
  # accepted only when the install target resolves beneath the caller's existing
  # writable temp root. Resolve readlink only from exact system paths because
  # distro layouts may place coreutils in /bin or /usr/bin; never trust PATH for
  # this security boundary. A production install target (e.g. /root/.claude)
  # never resolves under TMPDIR, so the seam cannot activate outside tests.
  if [ -n "${CCC_SETUP_TEST_EUID:-}" ] && [ -n "${CCC_CLAUDE_DIR:-}" ]; then
    for candidate in /usr/bin/readlink /bin/readlink; do
      if [ -f "$candidate" ] && [ -x "$candidate" ] && [ ! -L "$candidate" ]; then
        readlink_bin="$candidate"
        break
      fi
    done
    if [ -n "$readlink_bin" ]; then
      test_root="$("$readlink_bin" -m -- "${TMPDIR:-/tmp}" 2>/dev/null || true)"
      test_target="$("$readlink_bin" -m -- "$CCC_CLAUDE_DIR" 2>/dev/null || true)"
    fi
    if [ -n "$test_root" ] && [ -d "$test_root" ] && [ -w "$test_root" ]; then
      case "$test_target" in
        "$test_root"/*) uid="$CCC_SETUP_TEST_EUID" ;;
      esac
    fi
  fi
  [ -n "$uid" ] || uid="$(/usr/bin/id -u 2>/dev/null || echo invalid)"
  case "$uid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$uid" -eq 0 ]
}
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --with-plugin) WITH_PLUGIN=1 ;;
    --no-backup) BACKUP=0 ;;
    --node)         need_val "$1" "${2:-}"; OPT_NODE="$2"; shift ;;
    --display)      need_val "$1" "${2:-}"; OPT_DISPLAY="$2"; shift ;;
    --slot)         need_val "$1" "${2:-}"; OPT_SLOT="$2"; shift ;;
    --fleet-role)   need_val "$1" "${2:-}"; OPT_FLEET_ROLE="$2"; shift ;;
    --lang)         need_val "$1" "${2:-}"; OPT_LANG="$2"; shift ;;
    --user-name)    need_val "$1" "${2:-}"; OPT_USER_NAME="$2"; shift ;;
    --user-gh)      need_val "$1" "${2:-}"; OPT_USER_GH="$2"; shift ;;
    --user-tz)      need_val "$1" "${2:-}"; OPT_USER_TZ="$2"; shift ;;
    --user-context) need_val "$1" "${2:-}"; OPT_USER_CONTEXT="$2"; shift ;;
    *) echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done
SRC="$(cd "$(dirname "$0")" && pwd)"
# Path overrides are explicit so non-root nodes can dry-run/install without
# inheriting root-only assumptions. Defaults preserve the existing root VPS
# layout when HOME=/root.
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
MEM_DIR="$CLAUDE_DIR/memories"          # node-owned memory (Hermes-independent)
HERMES_ROOT="${CCC_HERMES_DIR:-$HOME/.hermes}"
HERMES_DIR="$HERMES_ROOT/memories"      # legacy memory location (fallback only)
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
WIKI_AGENT_BIN="${CCC_WIKI_AGENT_BIN:-$HOME/.wiki-agent/bin/wiki-agent}"
BRIDGE_DEFAULT_PATH="${CCC_BRIDGE_DEFAULT_PATH:-$HOME}"
HARNESS_PATHS_LIB="$SRC/scripts/lib/harness-paths.sh"
if [ ! -r "$HARNESS_PATHS_LIB" ]; then
  echo "ERROR: shared harness path library is missing: $HARNESS_PATHS_LIB" >&2
  exit 2
fi
# shellcheck source=/dev/null
. "$HARNESS_PATHS_LIB"

ccc_validate_setup_roots "$CLAUDE_DIR" "$HERMES_ROOT" || exit 2

# The canonical-path rewrite embeds $SRC verbatim into installed slash-command
# shell text (allowed-tools patterns and !`...` inline commands), where quoting
# is not uniformly available. Refuse checkout paths that cannot be embedded
# safely instead of installing broken, unquoted command paths.
case "$SRC" in
  *[!A-Za-z0-9/._-]*)
    echo "ERROR: checkout path contains characters unsafe for installed slash commands: $SRC" >&2
    echo "       move the checkout to a path matching [A-Za-z0-9/._-] (canonical: /opt/ccc-node)" >&2
    exit 2 ;;
esac

render_command() {
  printf '[dry-run]'
  printf ' %q' "$@"
  printf '\n'
}
run() {
  if [ "$DRY" = 1 ]; then
    render_command "$@"
  else
    "$@"
  fi
}
note() { printf '  - %s\n' "$*"; }

# Atomic single-file install (#1042). A plain `cp` truncates and rewrites the
# DESTINATION INODE in place; bash reads scripts incrementally, so a process
# currently executing that file keeps reading the new bytes at a stale offset
# and dies mid-run with a spurious syntax error. Proven live 2026-08-07 04:45
# KST: the cron-run installed ccc-self-update.sh invoked setup.sh, which
# overwrote the hook mid-execution — 9/12 fleet nodes crashed after the repo
# update but BEFORE service restart / audit record / owner notify (silent
# half-apply). Stage into a hidden temp in the destination directory, then
# rename(2) over the target: the running reader keeps its old inode for the
# rest of its life while new opens see the new file. Mode: an existing
# destination keeps its mode (matches cp-over-existing, so a reinstalled hook
# is never momentarily non-executable before the later chmod pass); a fresh
# install takes the repo source's mode (matches cp-to-new).
atomic_install() { # <src> <dest>
  local src="$1" dest="$2" tmp mode
  if [ -e "$dest" ]; then mode="$(stat -c '%a' "$dest")"; else mode="$(stat -c '%a' "$src")"; fi
  tmp="$(mktemp "${dest%/*}/.${dest##*/}.XXXXXX")" \
    || { echo "ERROR: mktemp failed for $dest" >&2; return 1; }
  if cp "$src" "$tmp" && chmod "$mode" "$tmp" && mv -f "$tmp" "$dest"; then
    return 0
  fi
  rm -f "$tmp"
  echo "ERROR: atomic install failed: $src -> $dest" >&2
  return 1
}

# setup is transactional even with --no-backup. The operator backup is a
# durable restore point; this private snapshot exists only long enough to undo
# a failed install. Exact managed paths are archived so credentials, projects,
# transcripts, state, and other node-local data never enter the snapshot.
ccc_validate_managed_artifacts "ERROR:" "$CLAUDE_DIR" "$HERMES_ROOT" "${CCC_MANAGED_PATHS[@]}" || exit 2
SETUP_TXN_DIR=""
SETUP_TXN_ACTIVE=0

snapshot_paths() { # <root> <archive> <path>...
  local root="$1" archive="$2"; shift 2
  local existing=() item
  for item in "$@"; do
    { [ -e "$root/$item" ] || [ -L "$root/$item" ]; } && existing+=("$item")
  done
  if [ "${#existing[@]}" -gt 0 ]; then
    tar -czf "$archive" -C "$root" "${existing[@]}"
  else
    tar -czf "$archive" --files-from /dev/null
  fi
}

begin_install_transaction() {
  [ "$DRY" = 1 ] && return 0
  local parent
  parent="$(dirname "$CLAUDE_DIR")"
  mkdir -p "$parent"
  SETUP_TXN_DIR="$(mktemp -d "$parent/.ccc-node-setup-rollback.XXXXXX")"
  snapshot_paths "$CLAUDE_DIR" "$SETUP_TXN_DIR/claude.tar.gz" "${CCC_MANAGED_PATHS[@]}"
  if [ -e "$HERMES_ROOT/honcho.json" ] || [ -L "$HERMES_ROOT/honcho.json" ]; then
    tar -czf "$SETUP_TXN_DIR/hermes.tar.gz" -C "$HERMES_ROOT" honcho.json
  else
    tar -czf "$SETUP_TXN_DIR/hermes.tar.gz" --files-from /dev/null
  fi
  tar -tzf "$SETUP_TXN_DIR/claude.tar.gz" >/dev/null
  tar -tzf "$SETUP_TXN_DIR/hermes.tar.gz" >/dev/null
  ccc_snapshot_codex_policy_state "$CODEX_DIR" "$SETUP_TXN_DIR"
  SETUP_TXN_ACTIVE=1
}

rollback_install_transaction() {
  local item failed=0
  trap - EXIT
  for item in "${CCC_MANAGED_PATHS[@]}"; do rm -rf -- "$CLAUDE_DIR/$item" || failed=1; done
  mkdir -p "$CLAUDE_DIR" "$HERMES_ROOT" || failed=1
  tar -xzf "$SETUP_TXN_DIR/claude.tar.gz" -C "$CLAUDE_DIR" || failed=1
  rm -f -- "$HERMES_ROOT/honcho.json" || failed=1
  tar -xzf "$SETUP_TXN_DIR/hermes.tar.gz" -C "$HERMES_ROOT" || failed=1
  ccc_restore_codex_policy_state "$CODEX_DIR" "$SETUP_TXN_DIR" || failed=1
  if [ "$failed" = 0 ]; then
    echo "ERROR: setup failed; restored previous installed artifacts (Claude harness, honcho.json, Codex GitHub policy config)" >&2
  else
    echo "ERROR: setup failed and artifact rollback was degraded; inspect $SETUP_TXN_DIR" >&2
    return 1
  fi
}

finish_install_transaction() {
  local rc=$? keep_snapshot=0
  trap - EXIT
  if [ "$SETUP_TXN_ACTIVE" = 1 ] && [ "$rc" -ne 0 ]; then
    if ! rollback_install_transaction; then
      rc=70
      keep_snapshot=1
    fi
  fi
  if [ "$keep_snapshot" = 0 ] && [ -n "$SETUP_TXN_DIR" ]; then
    rm -rf -- "$SETUP_TXN_DIR"
  fi
  exit "$rc"
}

trap finish_install_transaction EXIT

# Merge base + enforcement-overlay into settings.json ATOMICALLY: render to a temp
# file, validate it parses, then mv into place. The old `jq ... > settings.json`
# form pre-truncates the destination via the `>` redirect, so a jq failure (bad
# input, jq missing) left a 0-byte settings.json with no detection — bricking the
# node's hooks/permissions. Here a failure leaves any existing file untouched.
merge_settings_json() {
  local base="$1" overlay="$2" dest="$3"
  if [ "$DRY" = 1 ]; then
    echo "[dry-run] merge (atomic+validated) '$base' + '$overlay' -> '$dest'"
    return 0
  fi
  local tmp; tmp="$(mktemp "${dest}.XXXXXX")" || { echo "ERROR: mktemp failed for $dest" >&2; return 1; }
  if jq -s -f "$SRC/scripts/merge-settings.jq" "$base" "$overlay" > "$tmp" 2>/dev/null \
     && jq -e . "$tmp" >/dev/null 2>&1; then
    mv "$tmp" "$dest"
  else
    rm -f "$tmp"
    echo "ERROR: failed to merge settings.json from '$base' + '$overlay' (existing file left untouched)" >&2
    return 1
  fi
}

# Claude Code refuses --dangerously-skip-permissions (the `bypassPermissions`
# permission mode) when it runs with root/sudo privileges, so a node whose
# Claude runs as root would reject every new session if it inherited the
# `bypassPermissions` default. On such a node, drop the installed default so
# Claude falls back to its normal prompting mode (the native Claude Code
# posture). Non-root nodes keep the no-prompt default. The setup user is used
# as the proxy for the run user (the dominant
# case is setup-as-root == service-as-root); the bridge additionally enforces
# this at runtime for its own SDK path.

neutralize_bypass_if_root() {
  local dest="$1"
  _ccc_is_root || return 0
  if [ "$DRY" = 1 ]; then
    echo "[dry-run] root node: drop bypassPermissions defaultMode from $dest"
    return 0
  fi
  [ -f "$dest" ] || return 0
  jq -e '.permissions.defaultMode == "bypassPermissions"' "$dest" >/dev/null 2>&1 || return 0
  local tmp; tmp="$(mktemp "${dest}.XXXXXX")" || { echo "ERROR: mktemp failed for $dest" >&2; return 1; }
  if jq 'if (.permissions? and .permissions.defaultMode == "bypassPermissions")
         then .permissions |= del(.defaultMode) else . end' "$dest" > "$tmp" 2>/dev/null \
     && jq -e . "$tmp" >/dev/null 2>&1; then
    mv "$tmp" "$dest"
    note "root node: dropped bypassPermissions defaultMode (native Claude Code posture)"
  else
    rm -f "$tmp"
    echo "ERROR: failed to neutralize bypassPermissions for root at '$dest' (existing file left untouched)" >&2
    return 1
  fi
}

# `model` is the node's model pin and the bridge reads it from settings.json as
# the canonical source (bridge/core/bot_commands.py `_get_real_model`: session
# model -> settings.json `model` -> literal fallback). But settings.json is a
# managed artifact recomposed from repo templates on every setup run, and
# self-update runs setup on every changed tick — so a pin set on a node survived
# only until the next code change, then vanished with no warning. Worse than a
# plain reset: `/new` syncs the session model FROM settings.json, so an absent
# key overwrites the live session pin with null and the account default gets
# served (#1235; measured 2026-08-22, 7/7 nodes had lost the key).
#
# So carve out this one key, the same node-local-survives-setup contract
# settings.local.json already has (#454). Everything else in the file stays
# repo-owned.
read_node_local_model() { # <settings-path> -> pin on stdout (empty if none)
  local src="$1"
  [ -f "$src" ] || return 0
  jq -r 'if has("model") and (.model | type) == "string" then .model else empty end' \
    "$src" 2>/dev/null || true
}

restore_node_local_model() { # <settings-path> <pin>
  local dest="$1" model="$2"
  [ -n "$model" ] || return 0
  if [ "$DRY" = 1 ]; then
    echo "[dry-run] preserve node-local model pin '$model' in $dest"
    return 0
  fi
  [ -f "$dest" ] || return 0
  # A template that ships its own pin wins: the repo still owns anything it
  # actually declares, and this is only meant to stop silent erasure.
  if jq -e 'has("model")' "$dest" >/dev/null 2>&1; then
    note "settings.json ships a model pin — node-local '$model' not reapplied"
    return 0
  fi
  local tmp; tmp="$(mktemp "${dest}.XXXXXX")" || { echo "ERROR: mktemp failed for $dest" >&2; return 1; }
  if jq --arg m "$model" '.model = $m' "$dest" > "$tmp" 2>/dev/null \
     && jq -e . "$tmp" >/dev/null 2>&1; then
    mv "$tmp" "$dest"
    note "preserved node-local model pin: $model"
  else
    rm -f "$tmp"
    echo "ERROR: failed to preserve node-local model pin at '$dest' (existing file left untouched)" >&2
    return 1
  fi
}

# Snapshot the existing ~/.claude config BEFORE we overwrite anything. setup.sh unconditionally
# overwrites settings.json and the hook/output-style/agent/command/skill dirs — on a node that
# already has a configured identity that is destructive, so we tar a restore point first.
# settings.local.json is NOT backed up here: it is node-local and only seeded when absent, so
# setup never overwrites it (#454). Credentials (~/.claude/.credentials.json) are also NOT included.
backup_claude_dir() {
  if [ "$BACKUP" != 1 ]; then note "backup skipped (--no-backup)"; return 0; fi
  [ -d "$CLAUDE_DIR" ] || { note "no existing $CLAUDE_DIR — nothing to back up"; return 0; }
  local items=() p
  for p in settings.json hooks output-styles agents commands skills; do
    [ -e "$CLAUDE_DIR/$p" ] && items+=("$p")
  done
  if [ "${#items[@]}" -eq 0 ]; then note "fresh install — no overwritable config to back up"; return 0; fi
  local ts archive
  ts="$(date +%Y%m%d-%H%M%S)"
  archive="$CLAUDE_DIR/backups/ccc-node-setup-$ts.tar.gz"
  run mkdir -p "$CLAUDE_DIR/backups"
  if ! run tar -czf "$archive" -C "$CLAUDE_DIR" "${items[@]}"; then
    echo "Backup creation failed: $archive" >&2
    return 1
  fi
  if [ "$DRY" != 1 ] && ! tar -tzf "$archive" "${items[@]}" >/dev/null 2>&1; then
    echo "Backup validation failed: $archive" >&2
    return 1
  fi
  note "backed up existing config -> $archive (restore: tar -xzf <archive> -C '$CLAUDE_DIR')"
}

echo "==> Installing Claude Code node setup from: $SRC"
backup_claude_dir
begin_install_transaction

# 1) Claude harness config + hooks (safe, secret-free)
run mkdir -p "$CLAUDE_DIR/hooks" "$CLAUDE_DIR/hooks/lib"
# settings.json is composed from two sources so the portable enforcement/observability
# hooks have a SINGLE owner (no double-firing):
#   - claude/settings.base.json          : node-local hooks + statusLine + outputStyle (always)
#   - claude/hooks/enforcement-overlay.json : portable hooks audit/redact/notify
# Standalone (default): base + overlay merged → settings.json owns everything.
# --with-plugin: base only → the ccc-node plugin's hooks/hooks.json owns the portable hooks.
# Read the node-local model pin BEFORE either path replaces the file — both
# render from repo templates and mv over the destination, so nothing survives
# unless it was captured first (#1235).
NODE_LOCAL_MODEL="$(read_node_local_model "$CLAUDE_DIR/settings.json")"
if [ "$WITH_PLUGIN" = 1 ]; then
  note "plugin mode: lean settings (portable hooks come from the ccc-node plugin)"
  run atomic_install "$SRC/claude/settings.base.json" "$CLAUDE_DIR/settings.json"
else
  merge_settings_json "$SRC/claude/settings.base.json" "$SRC/claude/hooks/enforcement-overlay.json" "$CLAUDE_DIR/settings.json"
fi
restore_node_local_model "$CLAUDE_DIR/settings.json" "$NODE_LOCAL_MODEL"
neutralize_bypass_if_root "$CLAUDE_DIR/settings.json"
# settings.local.json is the NODE-LOCAL approvals file — seed it from the
# template ONLY when absent so a node's accumulated/hand-added approvals are
# never clobbered by setup or self-update (#454). It is not a managed artifact.
if [ ! -e "$CLAUDE_DIR/settings.local.json" ]; then
  run atomic_install "$SRC/claude/settings.local.template.json" "$CLAUDE_DIR/settings.local.json"
else
  note "settings.local.json already present — left untouched (node-local approvals)"
fi
# Install standalone imports before their hook-tree callers. Old hooks ignore
# them; once the new adapter is copied below, both modules are already present.
# The setup transaction restores the previous hooks tree on any later failure.
run atomic_install "$SRC/bridge/utils/secure_fs.py" "$CLAUDE_DIR/hooks/ccc_secure_fs.py"
run chmod 644 "$CLAUDE_DIR/hooks/ccc_secure_fs.py"
run atomic_install "$SRC/bridge/memory/journal_core.py" "$CLAUDE_DIR/hooks/ccc_journal_core.py"
run chmod 644 "$CLAUDE_DIR/hooks/ccc_journal_core.py"
# Hook tree deployment (#569): every deployable file under claude/hooks/ is
# discovered by the shared walk (ccc_hook_tree_files in scripts/lib/harness-paths.sh)
# instead of a hand-maintained list — the same convention validate-harness.sh
# checks, so a new hook or lib file ships the moment it lands in the tree
# (lib/mtime-prune.sh was missed by the old 3-place hand list, silently
# disabling standalone pruning fleet-wide, #564). Subdirectories (lib/, distill/,
# skill-review/) deploy recursively preserving structure; every deployed file is
# installed executable, matching the historical per-file chmod list.
mapfile -t hook_tree_files < <(ccc_hook_tree_files "$SRC")
if [ "${#hook_tree_files[@]}" -eq 0 ]; then
  echo "ERROR: hook-tree walk found no deployable hooks under $SRC/claude/hooks" >&2
  exit 2
fi
# Dedup on RELATIVE dirs (repo-controlled, newline-free): $CLAUDE_DIR is
# operator input and may contain characters a line-based dedup would mangle.
hook_tree_rel_dirs=()
for _hook_rel in "${hook_tree_files[@]}"; do
  case "$_hook_rel" in
    */*) hook_tree_rel_dirs+=("${_hook_rel%/*}") ;;
  esac
done
hook_tree_dirs=("$CLAUDE_DIR/hooks")
if [ "${#hook_tree_rel_dirs[@]}" -gt 0 ]; then
  while IFS= read -r _hook_rel_dir; do
    hook_tree_dirs+=("$CLAUDE_DIR/hooks/$_hook_rel_dir")
  done < <(printf '%s\n' "${hook_tree_rel_dirs[@]}" | LC_ALL=C sort -u)
fi
run mkdir -p "${hook_tree_dirs[@]}"
hook_tree_targets=()
for _hook_rel in "${hook_tree_files[@]}"; do
  run atomic_install "$SRC/claude/hooks/$_hook_rel" "$CLAUDE_DIR/hooks/$_hook_rel"
  hook_tree_targets+=("$CLAUDE_DIR/hooks/$_hook_rel")
done
# Files deployed INTO hooks/ from OUTSIDE claude/hooks/ keep explicit cp lines —
# the walk covers only the claude/hooks/ tree.
run atomic_install "$SRC/scripts/lib/harness-paths.sh" "$CLAUDE_DIR/hooks/lib/harness-paths.sh"
run atomic_install "$SRC/scripts/lib/harness_paths.py" "$CLAUDE_DIR/hooks/lib/harness_paths.py"
# Codex launch boundary: the launcher and materializer are installed beside
# load-memory.sh so every direct/app-server run reuses the same snapshot policy.
run atomic_install "$SRC/scripts/ccc-codex" "$CLAUDE_DIR/hooks/ccc-codex"
run atomic_install "$SRC/scripts/ccc_codex_memory.py" "$CLAUDE_DIR/hooks/ccc_codex_memory.py"
# Piri launch boundary: node-global counterpart of ccc-codex — materializes the
# same snapshot into the Piri global context file (<piri-agent-dir>/AGENTS.md).
run atomic_install "$SRC/scripts/ccc-piri" "$CLAUDE_DIR/hooks/ccc-piri"
# Claude's distill committer imports the same crash-recoverable transaction
# implementation as the Codex bridge.  Install the canonical module beside the
# existing standalone secure-fs copy instead of forking provider logic.
run atomic_install "$SRC/bridge/memory/local_memory_transaction.py" "$CLAUDE_DIR/hooks/ccc_local_memory_transaction.py"
run chmod 644 "$CLAUDE_DIR/hooks/ccc_local_memory_transaction.py"
# Memory helper tools used by load-memory.sh / refresh-memory.sh in standalone installs.
run atomic_install "$SRC/scripts/ccc-memory-index.sh" "$CLAUDE_DIR/hooks/ccc-memory-index.sh"
run atomic_install "$SRC/scripts/ccc_memory_index.py" "$CLAUDE_DIR/hooks/ccc_memory_index.py"
run atomic_install "$SRC/scripts/ccc-memory-search.sh" "$CLAUDE_DIR/hooks/ccc-memory-search.sh"
run atomic_install "$SRC/scripts/ccc_memory_search.py" "$CLAUDE_DIR/hooks/ccc_memory_search.py"
run atomic_install "$SRC/scripts/ccc-memory-consolidate.sh" "$CLAUDE_DIR/hooks/ccc-memory-consolidate.sh"
run atomic_install "$SRC/scripts/ccc-memory-query.sh" "$CLAUDE_DIR/hooks/ccc-memory-query.sh"
run atomic_install "$SRC/scripts/ccc-memory-check.sh" "$CLAUDE_DIR/hooks/ccc-memory-check.sh"
run atomic_install "$SRC/scripts/ccc_memory_probe.py" "$CLAUDE_DIR/hooks/ccc_memory_probe.py"
run chmod 644 "$CLAUDE_DIR/hooks/ccc_memory_probe.py"
run atomic_install "$SRC/scripts/ccc-memory-explain.sh" "$CLAUDE_DIR/hooks/ccc-memory-explain.sh"
run atomic_install "$SRC/scripts/ccc-wiki-triage.sh" "$CLAUDE_DIR/hooks/ccc-wiki-triage.sh"
run atomic_install "$SRC/scripts/ccc-memory-eval.sh" "$CLAUDE_DIR/hooks/ccc-memory-eval.sh"
run atomic_install "$SRC/scripts/ccc-memory-benchmark-export.sh" "$CLAUDE_DIR/hooks/ccc-memory-benchmark-export.sh"
# Skill autosave sweep — covers bridge/SDK sessions that never fire SessionEnd
# hooks; scheduled separately via scripts/install-skill-autosave-cron.sh.
run atomic_install "$SRC/scripts/ccc-skill-autosave.sh" "$CLAUDE_DIR/hooks/ccc-skill-autosave.sh"
# Opt-in autosave skill intake boundary. Nodes stage owner-only outboxes; only
# the separately enabled central publisher may open private draft intake PRs.
run atomic_install "$SRC/scripts/ccc-skill-promotion.py" "$CLAUDE_DIR/hooks/ccc-skill-promotion.py"
# Exact-commit private approved-skill consumer. It refuses floating refs,
# non-private repositories, user-owned target conflicts, and unverified trees.
run atomic_install "$SRC/scripts/ccc-fleet-skills-sync.py" "$CLAUDE_DIR/hooks/ccc-fleet-skills-sync.py"
# Self-update — the pre-approved node maintenance procedure (pull + setup +
# restart of operator-allowlisted services only; see docs/self-update.md).
run atomic_install "$SRC/scripts/ccc-self-update.sh" "$CLAUDE_DIR/hooks/ccc-self-update.sh"
# PR/issue status poll (ccc-node#962) — notices when a PR this node's bridge
# identity opened changes state (CI done, closed, merged); scheduled
# separately via scripts/install-pr-status-poll-cron.sh, tracks only the
# repos an operator lists in ~/.claude/pr-status-poll.repos.
run atomic_install "$SRC/scripts/ccc-pr-status-poll.sh" "$CLAUDE_DIR/hooks/ccc-pr-status-poll.sh"
# #958: record the install-source repo path so ccc-self-update's resolve_repo()
# (env > this file > script-location inference > ~/ccc-node) never falls back
# to a nonexistent ~/ccc-node on /opt installs — 8 nodes aborted no-repo until
# hand-written overrides were dropped in (LOG-20260805-gwakga-13). The file is
# operator-owned state: an identical entry is a no-op, a DIFFERENT entry is
# preserved with a warning — setup never silently rewrites operator state.
SELF_UPDATE_REPO_FILE="$CLAUDE_DIR/self-update.repo"
if [ "$DRY" != 1 ]; then
  if [ ! -f "$SELF_UPDATE_REPO_FILE" ]; then
    printf '%s\n' "$SRC" > "$SELF_UPDATE_REPO_FILE"
    chmod 600 "$SELF_UPDATE_REPO_FILE"
    note "recorded install-source repo for self-update: $SRC"
  elif [ "$(head -1 "$SELF_UPDATE_REPO_FILE" 2>/dev/null | tr -d '[:space:]')" = "$SRC" ]; then
    note "self-update.repo already records this repo ($SRC)"
  else
    note "WARNING: self-update.repo points at $(head -1 "$SELF_UPDATE_REPO_FILE" 2>/dev/null) but setup ran from $SRC — preserving the operator override"
  fi
else
  note "would record install-source repo for self-update: $SRC"
fi
# #973: version the daily live-backups rotate script — presence becomes a
# setup-managed property instead of an out-of-band deployment artifact (the
# fleet cron calls $HOME/.ccc-node/scripts/ccc-live-backups-rotate.sh).
run mkdir -p "$HOME/.ccc-node/scripts"
run atomic_install "$SRC/scripts/ccc-live-backups-rotate.sh" "$HOME/.ccc-node/scripts/ccc-live-backups-rotate.sh"
run chmod 700 "$HOME/.ccc-node/scripts/ccc-live-backups-rotate.sh"
# gongyung 2026-08-07: this Android resource-pressure guard was a node-local,
# untracked file (self-update couldn't reach it, so a stale-provider bug that
# killed healthy long-running turns sat unfixed). Same versioning pattern as
# ccc-live-backups-rotate.sh above — it just keeps the file current wherever
# it's already cron'd. Not wired into any cron by setup.sh: this is a
# resource-constrained-device tool, not a fleet default. Enable it per node
# only where the same low-RAM/thermal constraints apply.
run atomic_install "$SRC/scripts/resource-pressure-guard.sh" "$HOME/.ccc-node/scripts/resource-pressure-guard.sh"
run chmod 700 "$HOME/.ccc-node/scripts/resource-pressure-guard.sh"
# Executable files copied into hooks/ from OUTSIDE the claude/hooks/ tree.
# (ccc_memory_index.py / ccc_memory_search.py are deliberately NOT here: they
# are python modules invoked via their .sh wrappers and are installed 644.)
# The hook-tree files discovered by the walk above are chmod'd alongside them —
# every deployed hook-tree file is executable by convention (#569).
installed_hook_scripts=(
  "$CLAUDE_DIR/hooks/lib/harness-paths.sh"
  "$CLAUDE_DIR/hooks/lib/harness_paths.py"
  "$CLAUDE_DIR/hooks/ccc-codex"
  "$CLAUDE_DIR/hooks/ccc_codex_memory.py"
  "$CLAUDE_DIR/hooks/ccc-piri"
  "$CLAUDE_DIR/hooks/ccc-memory-index.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-search.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-consolidate.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-query.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-check.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-explain.sh"
  "$CLAUDE_DIR/hooks/ccc-wiki-triage.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-eval.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-benchmark-export.sh"
  "$CLAUDE_DIR/hooks/ccc-skill-autosave.sh"
  "$CLAUDE_DIR/hooks/ccc-skill-promotion.py"
  "$CLAUDE_DIR/hooks/ccc-fleet-skills-sync.py"
  "$CLAUDE_DIR/hooks/ccc-self-update.sh"
  "$CLAUDE_DIR/hooks/ccc-pr-status-poll.sh"
)
run chmod +x "${installed_hook_scripts[@]}" "${hook_tree_targets[@]}"
# #909: register a self-update agent-cron task so the harness auto-updates on
# nodes that run the agent-cron timer. `add` rejects a duplicate id, so this is
# idempotent (a re-run is a no-op once the task exists). Opt out with
# CCC_SELF_UPDATE_REGISTER_CRON=false. successExitCodes 0,8,11 treats a clean
# update, a bridge-busy defer (8), and a no-services-allowlist degraded run
# (11) as non-failures so on-failure alerts fire only for real aborts.
if [ "${CCC_SELF_UPDATE_REGISTER_CRON:-true}" != "false" ] && [ "$DRY" != 1 ] && [ -x "$SRC/scripts/agent-cron.sh" ]; then
  agent_cron_store="$CLAUDE_DIR/state/agent-cron/tasks.json"
  if CCC_AGENT_CRON_STORE="$agent_cron_store" \
       bash "$SRC/scripts/agent-cron.sh" list --json 2>/dev/null \
       | jq -e 'any(.tasks[]?; .id == "self-update")' >/dev/null; then
    note "self-update agent-cron task already registered (id=self-update)"
  elif CCC_AGENT_CRON_STORE="$agent_cron_store" \
       bash "$SRC/scripts/agent-cron.sh" add self-update \
         --schedule "${CCC_SELF_UPDATE_CRON:-17 4,10,16,22 * * *}" \
         --prompt "Update the local ccc-node harness and restart only operator-allowlisted services." \
         --notify telegram-owner-on-failure \
         --success-exit-codes 0,8,11 \
         --argv "$CLAUDE_DIR/hooks/ccc-self-update.sh" --argv run >/dev/null 2>&1; then
    note "registered self-update agent-cron task (id=self-update; timer must be installed separately)"
  else
    note "WARNING: failed to register self-update agent-cron task (id=self-update)"
  fi
fi
# Tier 3: status line (node·model·git·context·cost·A2A) wired via settings.json statusLine.
# Output style (한국어 구조화 보고) — node-agnostic; settings.json activates it as outputStyle.
run mkdir -p "$CLAUDE_DIR/output-styles"
run cp "$SRC/claude/output-styles/"*.md "$CLAUDE_DIR/output-styles/"
# Headless runner for cron/A2A/CI (`claude -p` wrapper).
run atomic_install "$SRC/claude/headless.sh" "$CLAUDE_DIR/headless.sh"
run chmod +x "$CLAUDE_DIR/headless.sh"
# checkpoint.sh creates its runtime state directory on demand. setup.sh must not
# mutate state/checkpoints because runtime state is outside the install transaction.
# Node-agnostic sub-agents are always installed. The A2A worker sub-agent roster
# (a2a-explorer/implementer/verifier/researcher) is a WORKER-role capability and
# is gated below: a node opts in with CCC_A2A_ROLE=worker. On a broker or any
# unconfigured node the roster is NOT installed, so the only A2A entry point
# stays the nexus/broker flow — not a free-standing local sub-agent route.
run mkdir -p "$CLAUDE_DIR/agents"
for _agent_src in "$SRC/claude/agents/"*.md; do
  [ -e "$_agent_src" ] || continue
  case "$(basename "$_agent_src")" in
    a2a-*) : ;;  # worker roster — installed only by the role gate below
    *) run atomic_install "$_agent_src" "$CLAUDE_DIR/agents/$(basename "$_agent_src")" ;;
  esac
done
# Persist an explicit role choice to a node-local (unmanaged) marker so an
# unattended self-update keeps honoring it without the operator's env.
if [ -n "${CCC_A2A_ROLE:-}" ] && [ "$DRY" != 1 ]; then
  printf '%s\n' "$CCC_A2A_ROLE" > "$CLAUDE_DIR/a2a-role"
fi
_a2a_role="${CCC_A2A_ROLE:-}"
if [ -z "$_a2a_role" ] && [ -r "$CLAUDE_DIR/a2a-role" ]; then
  _a2a_role="$(tr -d '[:space:]' < "$CLAUDE_DIR/a2a-role")"
fi
if [ "$_a2a_role" = worker ]; then
  for _agent_src in "$SRC/claude/agents/"a2a-*.md; do
    [ -e "$_agent_src" ] || continue
    run atomic_install "$_agent_src" "$CLAUDE_DIR/agents/$(basename "$_agent_src")"
  done
  note "A2A worker sub-agent roster installed (CCC_A2A_ROLE=worker)"
else
  for _stale in "$CLAUDE_DIR/agents/"a2a-*.md; do
    [ -e "$_stale" ] && run rm -f "$_stale"
  done
  note "A2A worker sub-agent roster not installed (non-worker role); A2A runs through the nexus/broker flow"
fi
# Slash commands (quick prompt templates: /node-status, /a2a-claim, /wiki-log) — node-agnostic
run mkdir -p "$CLAUDE_DIR/commands"
run cp "$SRC/claude/commands/"*.md "$CLAUDE_DIR/commands/"
# Custom skills (reusable procedures) — refreshed as near-atomic per-skill
# copies on every setup run (stage + single rename), from two trees:
# claude/skills (harness-coupled) and skills/shared (runtime-agnostic, Wiki
# TM-2331 superseded note). Real dirs only by design: the managed-artifact
# guard (scripts/lib/harness_paths.py) refuses symlinks in managed paths, so
# skills are never symlinked from the checkout. Freshness comes from
# self-update running setup.sh — 2026-08-07 gongmyoung drift was a dead
# updater, not a copy-format flaw. A manifest ($state/repo-skills.manifest)
# records what we installed: repo-removed skills are pruned when the copy is
# unmodified, kept with a warning when the node edited it; skills whose names
# are not in the repo set (node-local/autosave) are never touched.
skill_tree_hash() { # <dir> — deterministic content hash over file contents
  # LC_ALL=C: collation must not depend on the caller's locale, or the same
  # pristine tree hashes differently between an operator shell (UTF-8) and
  # cron/systemd (C) — flipping the manifest's prune/kept-modified verdicts
  # (the hook-tree dedup above pins its sort the same way).
  (cd "$1" && find . -type f -exec sha256sum {} + | LC_ALL=C sort -k2 | sha256sum | awk '{print $1}')
}
install_repo_skills_into() { # install_repo_skills_into <dest-root> <manifest> <source-root>...
  local dest_root="$1" manifest="$2"; shift 2
  local root source name target stage retired current_hash recorded_hash
  local -a current=()
  INSTALLED_REPO_SKILLS=()
  if [ "$DRY" = 1 ]; then
    note "repo skills: would refresh copies in $dest_root (atomic copy + manifest prune)"
    return 0
  fi
  for root in "$@"; do
    [ -d "$root" ] || continue
    for source in "$root"/*/; do
      [ -d "$source" ] || continue
      name="$(basename "$source")"
      target="$dest_root/$name"
      stage="$dest_root/.stage-$name"
      run rm -rf "$stage"
      run cp -r "$source" "$stage"
      if [ -d "$target" ]; then
        run rm -rf "$target.prev"
        run mv "$target" "$target.prev"
      fi
      run mv "$stage" "$target"
      run rm -rf "$target.prev"
      current+=("$name")
    done
  done
  # Prune repo-removed skills recorded in the manifest; keep node-edited ones.
  if [ -f "$manifest" ]; then
    while read -r retired recorded_hash; do
      [ -n "$retired" ] || continue
      case " ${current[*]} " in *" $retired "*) continue;; esac
      [ -d "$dest_root/$retired" ] || continue
      current_hash="$(skill_tree_hash "$dest_root/$retired")"
      if [ "$current_hash" = "$recorded_hash" ]; then
        run rm -rf "$dest_root/$retired"
        note "pruned repo-removed skill $retired (copy was unmodified)"
      else
        note "kept $retired: repo removed it but the installed copy was modified locally"
      fi
    done < "$manifest"
  fi
  if [ "${#current[@]}" -gt 0 ]; then
    : > "$manifest.tmp"
    for name in "${current[@]}"; do
      printf '%s %s\n' "$name" "$(skill_tree_hash "$dest_root/$name")" >> "$manifest.tmp"
    done
    run mv "$manifest.tmp" "$manifest"
  fi
  # Publish what was actually installed so the canonical-path rewrite (step 2b)
  # can target exactly these directories (#1072). Deriving the rewrite set from
  # the install result — rather than re-enumerating source trees beside it — is
  # what keeps the two from drifting apart: ccc_doctor applies the same rewrite
  # to everything in SKILL_SOURCE_ROOTS before comparing, so any tree this
  # installs but the rewrite skips reads as permanent phantom drift on every
  # non-canonical node, and --fix refuses skill paths so doctor cannot clear it.
  INSTALLED_REPO_SKILLS=(${current[@]+"${current[@]}"})
}
# The canonical-path rewrite (step 2b) edits installed skill files AFTER the
# copy above hashed them, so on a non-canonical node the manifest describes
# pre-rewrite bytes and every rewritten skill reads as "modified locally"
# forever — it can never be pruned when the repo drops it. Re-record hashes
# once the rewrite has run. Entries whose directory is gone keep their recorded
# hash, so a later prune can still tell an edited copy from a pristine one.
refresh_skill_manifest_hashes() { # refresh_skill_manifest_hashes <dest-root> <manifest>
  local dest_root="$1" manifest="$2" name recorded
  if [ "$DRY" = 1 ]; then
    note "repo skills: would re-record $manifest hashes after the canonical-path rewrite"
    return 0
  fi
  [ -f "$manifest" ] || return 0
  : > "$manifest.tmp"
  while read -r name recorded; do
    [ -n "$name" ] || continue
    if [ -d "$dest_root/$name" ]; then
      printf '%s %s\n' "$name" "$(skill_tree_hash "$dest_root/$name")" >> "$manifest.tmp"
    else
      printf '%s %s\n' "$name" "$recorded" >> "$manifest.tmp"
    fi
  done < "$manifest"
  mv "$manifest.tmp" "$manifest"
}
run mkdir -p "$CLAUDE_DIR/skills" "$CLAUDE_DIR/state"
install_repo_skills_into "$CLAUDE_DIR/skills" "$CLAUDE_DIR/state/repo-skills.manifest" "$SRC/claude/skills" "$SRC/skills/shared"
# Snapshot immediately: the Piri install below reuses the function and would
# otherwise leave its own set in INSTALLED_REPO_SKILLS by the time the
# canonical-path rewrite reads it. Piri skills install outside $CLAUDE_DIR and
# are not rewrite targets.
CLAUDE_REPO_SKILLS=(${INSTALLED_REPO_SKILLS[@]+"${INSTALLED_REPO_SKILLS[@]}"})

# Piri skills (web search/fetch helpers) — only on nodes that already have a
# Piri agent dir, so non-Piri nodes stay untouched.
PIRI_AGENT_DIR="${PIRI_CODING_AGENT_DIR:-$HOME/.piri/agent}"
if [ -d "$PIRI_AGENT_DIR" ]; then
  run mkdir -p "$PIRI_AGENT_DIR/skills" "$PIRI_AGENT_DIR/state"
  install_repo_skills_into "$PIRI_AGENT_DIR/skills" "$PIRI_AGENT_DIR/state/repo-skills.manifest" "$SRC/piri/skills"
fi

# 2) Per-node files — only seed templates if a real one is NOT already present.
SEEDED=()  # files freshly created from a template this run (safe to placeholder-substitute)
seed() { # seed <template> <dest>
  if [ -e "$2" ]; then note "kept existing $2 (not overwritten)";
  else run cp "$1" "$2"; SEEDED+=("$2"); note "seeded template -> $2 (EDIT ME)"; fi
}
run mkdir -p "$MEM_DIR"
run mkdir -p "$HERMES_ROOT"
seed "$SRC/claude/CLAUDE.md.template"             "$CLAUDE_DIR/CLAUDE.md"
seed "$SRC/claude/hooks/tools-cheatsheet.md"      "$CLAUDE_DIR/hooks/tools-cheatsheet.md"
# Node-owned memory (Hermes-independent): seed into ~/.claude/memories.
# load-memory.sh reads here first, falling back to ~/.hermes/memories only if absent.
seed "$SRC/hermes/memories/MEMORY.template.md"    "$MEM_DIR/MEMORY.md"
seed "$SRC/hermes/memories/USER.template.md"      "$MEM_DIR/USER.md"
# honcho.json stays node-local under ~/.hermes (documentation/Hermes-side; not a hard CC dep).
seed "$SRC/hermes/honcho.template.json"           "$HERMES_ROOT/honcho.json"

# 2b) Canonical-path rewrite — the settings/hook/skill/command templates use
# /root/.claude as the canonical harness path and /opt/ccc-node as the canonical
# repo checkout. On nodes where either differs (e.g. Termux HOME, or a
# /root/ccc-node checkout like gwakga), rewrite the installed files so
# settings.json hook *command* paths and hook internal defaults
# (${CCC_*:-/root/.claude/...}) resolve, AND so the slash commands that invoke
# repo scripts verbatim (/doctor, /node-status, /security-audit, /agent-cron)
# point at this node's real checkout instead of a nonexistent /opt/ccc-node.
# Repo templates stay canonical; only installed copies are rewritten. Both
# pairs are substituted in a SINGLE non-cascading pass (one regex alternation
# over the original text), so a replacement value is never rescanned — e.g. a
# checkout under a path containing /root/.claude cannot have its freshly
# inserted $SRC corrupted by the harness-dir pair.
# No-op on standard nodes (CLAUDE_DIR == /root/.claude, SRC == /opt/ccc-node).
if [ "$CLAUDE_DIR" != "/root/.claude" ] || [ "$SRC" != "/opt/ccc-node" ]; then
  note "rewrite canonical paths (/opt/ccc-node -> $SRC, /root/.claude -> $CLAUDE_DIR) in installed harness files"
  if [ "$DRY" = 1 ]; then
    render_command rewrite-canonical-paths "/opt/ccc-node" "$SRC" "/root/.claude" "$CLAUDE_DIR"
  else
    rewrite_targets=(
      "$CLAUDE_DIR/settings.json"
      "$CLAUDE_DIR/headless.sh"
      "$CLAUDE_DIR/hooks/ccc_memory_index.py"
      "$CLAUDE_DIR/hooks/ccc_memory_search.py"
      "${installed_hook_scripts[@]}" "${hook_tree_targets[@]}" "${SEEDED[@]}"
    )
    for source_tree in output-styles agents commands; do
      while IFS= read -r -d '' source_file; do
        rewrite_targets+=("$CLAUDE_DIR/$source_tree/${source_file#"$SRC/claude/$source_tree/"}")
      done < <(find "$SRC/claude/$source_tree" -type f -print0)
    done
    # Skills come from more than one repo root (claude/skills + skills/shared)
    # and land flat in $CLAUDE_DIR/skills, so walk what install_repo_skills_into
    # actually installed instead of re-listing the roots here (#1072). Node-local
    # and autosave skills are absent from that list and stay untouched.
    for skill_name in ${CLAUDE_REPO_SKILLS[@]+"${CLAUDE_REPO_SKILLS[@]}"}; do
      [ -d "$CLAUDE_DIR/skills/$skill_name" ] || continue
      while IFS= read -r -d '' source_file; do
        rewrite_targets+=("$source_file")
      done < <(find "$CLAUDE_DIR/skills/$skill_name" -type f -print0)
    done
    # The transform itself lives in scripts/lib/canonical_paths.py because
    # ccc-doctor must apply the IDENTICAL rewrite before comparing installed
    # files against these templates. While this was an inline heredoc, doctor
    # compared byte-exact and reported permanent phantom drift on every
    # non-canonical node (2026-07-30 fleet sweep).
    for rewrite_file in "${rewrite_targets[@]}"; do
      [ -f "$rewrite_file" ] || continue
      python3 "$SRC/scripts/lib/canonical_paths.py" "$rewrite_file" \
        "/opt/ccc-node" "$SRC" "/root/.claude" "$CLAUDE_DIR"
    done
    # Skill copies were hashed before this rewrite touched them — re-record so
    # the manifest matches the bytes on disk. Only the Claude tree needs it:
    # $PIRI_AGENT_DIR/skills is not in rewrite_targets, so those hashes stand.
    refresh_skill_manifest_hashes "$CLAUDE_DIR/skills" "$CLAUDE_DIR/state/repo-skills.manifest"
  fi
fi

# 3) Node-identity substitution — fill <PLACEHOLDER> tokens in the files we just seeded.
# Only freshly-seeded files are touched (existing identity is never rewritten). Tokens for which
# no flag was given are left intact so the manual checklist below still applies to them.
apply_node_identity() {
  local any=0 v
  for v in "$OPT_NODE" "$OPT_DISPLAY" "$OPT_SLOT" "$OPT_FLEET_ROLE" "$OPT_LANG" \
           "$OPT_USER_NAME" "$OPT_USER_GH" "$OPT_USER_TZ" "$OPT_USER_CONTEXT"; do
    [ -n "$v" ] && any=1
  done
  [ "$any" = 1 ] || return 0
  if [ "${#SEEDED[@]}" -eq 0 ]; then
    note "identity flags given but all target files already existed — left untouched"; return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    note "python3 not found — cannot auto-substitute placeholders; edit seeded files by hand"; return 0
  fi
  local f
  for f in "${SEEDED[@]}"; do
    if [ "$DRY" = 1 ]; then echo "[dry-run] substitute provided placeholders in $f"; continue; fi
    NODE_NAME="$OPT_NODE" NODE_DISPLAY_NAME="$OPT_DISPLAY" PHYSICAL_SLOT="$OPT_SLOT" \
    FLEET_ROLE="$OPT_FLEET_ROLE" LANGUAGE="$OPT_LANG" USER_NAME="$OPT_USER_NAME" \
    USER_GH="$OPT_USER_GH" USER_TZ="$OPT_USER_TZ" USER_CONTEXT="$OPT_USER_CONTEXT" \
    python3 - "$f" <<'PY'
import os, sys
path = sys.argv[1]
keys = ["NODE_NAME","NODE_DISPLAY_NAME","PHYSICAL_SLOT","FLEET_ROLE","LANGUAGE",
        "USER_NAME","USER_GH","USER_TZ","USER_CONTEXT"]
with open(path, encoding="utf-8") as fh:
    s = fh.read()
for k in keys:
    val = os.environ.get(k, "")
    if val:
        s = s.replace("<%s>" % k, val)
with open(path, "w", encoding="utf-8") as fh:
    fh.write(s)
PY
    note "applied node identity to $f"
  done
}
apply_node_identity

# 3c) Placeholder-residue warning — a config left with unresolved <TOKEN> placeholders
# silently breaks fail-open consumers. Worst case is honcho.json: refresh-memory.sh /
# distill read baseUrl, treat the placeholder as a value, fail with curl errors, and the
# memory pipeline goes dark with NO alert. This happened fleet-wide on 2026-07-08 when a
# retirement sweep removed ~/.hermes and this seed step quietly reinstated the template
# on 3 nodes (seoyoon-family-wiki LOG-1579). Warn loudly so the operator fills values now.
warn_placeholder_residue() {
  local f residue found=0 honcho_checked=0
  # honcho.json is checked even when it was NOT freshly seeded this run — an old
  # placeholder left from a previous run is just as fatal to the memory pipeline.
  for f in "$HERMES_ROOT/honcho.json" ${SEEDED[@]+"${SEEDED[@]}"}; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in
      # Documentation ships literal <NODE>/<USER_PEER>/<PLACEHOLDER> as
      # examples; flagging it on every fresh install buried the honcho.json
      # warning this banner exists for.
      tools-cheatsheet.md) continue ;;
    esac
    if [ "$f" = "$HERMES_ROOT/honcho.json" ]; then          # dedupe when freshly seeded
      [ "$honcho_checked" = 1 ] && continue
      honcho_checked=1
    fi
    residue="$(grep -hoE '<[A-Z][A-Z0-9_]+>' "$f" 2>/dev/null | sort -u | tr '\n' ' ' || true)"
    [ -n "${residue// /}" ] || continue
    if [ "$found" = 0 ]; then
      printf '\n==> WARNING: unresolved template placeholders detected:\n'
      found=1
    fi
    printf '      %s : %s\n' "$f" "$residue"
  done
  if [ "$found" = 1 ]; then
    cat <<'WEOF'
    A placeholder baseUrl in honcho.json DISABLES the Honcho memory pipeline
    silently (refresh/distill are fail-open — they log an error and move on).
    Fill in real values before relying on memory recall on this node.
    If this node had a working config that a cleanup/retirement sweep moved away,
    look for it under backup/quarantine dirs (e.g. /root/hermes-retired-*/root.hermes/)
    and restore it instead of re-filling by hand.
WEOF
  fi
}
warn_placeholder_residue

# GitHub transport policy: Codex's OpenAI-curated GitHub plugin carries a
# connector-first skill, while ccc-node standardizes on the node-local `gh`
# identity. Persist the supported per-plugin toggle without re-rendering the
# rest of config.toml. The helper is fail-closed and atomic; it never prints
# config contents or follows a config symlink.
run python3 "$SRC/scripts/ccc_codex_github_policy.py" apply --codex-home "$CODEX_DIR"
note "Codex GitHub transport pinned to local gh CLI (plugin disabled)"

# Repo-shipped Codex skills are installed through a separate fail-closed
# transaction. The provisioner preflights every target before mutating any,
# distinguishes ccc-managed provenance from user-authored skills, and rolls the
# whole set back on a partial update. Plan mode is genuinely read-only and
# reports only skill names/actions, never skill bodies.
if [ "$DRY" = 1 ]; then
  python3 "$SRC/scripts/ccc_codex_skills.py" plan \
    --repo-root "$SRC" --codex-home "$CODEX_DIR"
else
  python3 "$SRC/scripts/ccc_codex_skills.py" apply \
    --repo-root "$SRC" --codex-home "$CODEX_DIR"
fi
note "Codex managed skills reconciled from compatibility catalog"

# Reconcile only an already-installed, recognizably ccc-generated bridge unit.
# service-systemd.sh renders the canonical bytes, leaves an identical file
# untouched, and on drift performs only an atomic main-unit replacement plus
# daemon-reload. It never restarts/enables/starts/stops the bridge, so an active
# request is not interrupted and an operator-stopped unit remains stopped.
# Bespoke units are left for explicit normalization; node-local systemd policy
# belongs in <unit>.d/*.conf drop-ins, not copied legacy main-unit lines.
#
# It also refuses to RELOCATE: if the installed unit boots from a different
# checkout than this one, it warns and leaves the unit alone (#842). Without
# that, running setup.sh inside a PR/issue work tree would silently repoint the
# node's live bridge at unreviewed code — which is how seoseo came to serve from
# /work/agent-codebench/ccc-node-pr833 on 2026-08-01. Reclaiming a unit that has
# already been taken over is deliberate and stays a separate, explicit step:
#   <canonical checkout>/bridge/service-systemd.sh reconcile --allow-relocate
if [ "$DRY" = 1 ]; then
  bash "$SRC/bridge/service-systemd.sh" reconcile --dry-run
else
  bash "$SRC/bridge/service-systemd.sh" reconcile
fi
note "Existing ccc-telegram-bridge systemd unit checked against the canonical renderer"

# #968: Termux/Android hash-locked installs may need to build packages from
# source (cryptography 50 has no Android wheel -> maturin -> Rust). A missing
# toolchain killed the daegyo bridge on 2026-08-06 and the prerequisite lived
# only in prose. Ensure it here so it is a setup-managed property; when the
# install cannot run, say so loudly with the exact pkg line.
IS_TERMUX=0
[ -n "${TERMUX_VERSION:-}" ] && IS_TERMUX=1
case "${PREFIX:-}" in */com.termux/*) IS_TERMUX=1 ;; esac
if [ "$IS_TERMUX" = 1 ]; then
  if command -v cargo >/dev/null 2>&1; then
    note "Termux Rust toolchain present ($(cargo --version 2>/dev/null | head -1))"
  elif [ "$DRY" = 1 ]; then
    echo "[dry-run] pkg install -y rust rust-std-aarch64-linux-android"
  elif command -v pkg >/dev/null 2>&1; then
    if pkg install -y rust rust-std-aarch64-linux-android; then
      note "installed Termux Rust toolchain (rust + rust-std-aarch64-linux-android)"
    else
      note "WARNING: Rust toolchain install failed — hash-locked dependency builds (e.g. cryptography via maturin) will fail. Run: pkg install -y rust rust-std-aarch64-linux-android"
    fi
  else
    note "WARNING: pkg not found — install the Rust toolchain manually: pkg install -y rust rust-std-aarch64-linux-android"
  fi
fi

cat <<'EOF'

==> Done. Follow-up checklist (do these manually):
  1. Edit ~/.claude/CLAUDE.md          — replace any remaining <PLACEHOLDER> with this node's identity/user.
                                         (Pass --node/--display/--slot/--user-* to setup.sh to pre-fill these.)
  2. Edit ~/.claude/memories/MEMORY.md — node-specific durable facts (NO raw secrets).
  3. Edit ~/.claude/memories/USER.md   — who you work for + preferences.
  4. Edit $HERMES_ROOT/honcho.json        — set baseUrl / peerName / target (this is node-local; gitignored).
  5. Install wiki-agent at $WIKI_AGENT_BIN (canonical: jinwon-int/wiki-agent).
  6. Auth GitHub:  gh auth login   (or place token per node policy; never commit it).
  7. Start a fresh Claude Code session and confirm the SessionStart snapshot injects.
  8. (Optional) MCP tool servers: ./claude/mcp-setup.sh
     Registers searxng (Tailnet SearXNG) + context7 (docs) + firecrawl (web scrape;
     key read from ~/.hermes/.env). Idempotent; tool perms pre-allowed in settings.json.
  9. (Optional) Telegram bridge: cd bridge && cp .env.example .env && edit, then
     ./start.sh --path $BRIDGE_DEFAULT_PATH -d   (daemon-supervised). See bridge/README.md.
     Linux reboot-persistence: ./start.sh --path $BRIDGE_DEFAULT_PATH --install-systemd   (systemd unit).
  10. (Optional) Keep the memory snapshot warm on idle nodes:
     ./scripts/install-memory-refresh-cron.sh --apply   (cron runs refresh-memory.sh; dry-run by default).
  11. (Optional Codex) Keep CCC_CODEX_CLI_PATH on ~/.claude/hooks/ccc-codex,
      set CCC_CODEX_REAL_CLI_PATH only for a non-PATH binary, and require
      `ccc-memory-check.sh --json` to report `.codex.status == "ready"`.
  12. (Optional Piri) Point CCC_PIRI_CLI_PATH at ~/.claude/hooks/ccc-piri and
      CCC_PIRI_REAL_CLI_PATH at the real Piri CLI so every node-global Piri
      launch materializes the same memory snapshot into the Piri global
      context file first.

Secrets that are intentionally NOT installed by this script:
  - ~/.claude/.credentials.json   (Claude OAuth — created on `claude` login)
  - GitHub token                  (gh auth login)
  - Honcho endpoint value         (you set it in ~/.hermes/honcho.json)
EOF

printf '\nResolved path configuration (override with CCC_* env vars; no secrets printed):\n'
printf '  - CCC_CLAUDE_DIR=%s\n' "$CLAUDE_DIR"
printf '  - CLAUDE.md=%s/CLAUDE.md\n' "$CLAUDE_DIR"
printf '  - CCC_HERMES_DIR=%s\n' "$HERMES_ROOT"
printf '  - honcho.json=%s/honcho.json\n' "$HERMES_ROOT"
printf '  - CCC_WIKI_AGENT_BIN=%s\n' "$WIKI_AGENT_BIN"
printf '  - CCC_BRIDGE_DEFAULT_PATH=%s\n' "$BRIDGE_DEFAULT_PATH"
printf '  - CCC_CODEX_CLI_PATH=%s/hooks/ccc-codex\n' "$CLAUDE_DIR"
printf '  - CCC_CODEX_MEMORY_MATERIALIZER_PATH=%s/hooks/ccc_codex_memory.py\n' "$CLAUDE_DIR"
printf '  - CCC_PIRI_CLI_PATH=%s/hooks/ccc-piri\n' "$CLAUDE_DIR"
printf '  - CODEX_HOME=%s (GitHub plugin disabled; gh CLI-first)\n' "$CODEX_DIR"
printf '  - Codex managed skills=%s/skills (catalog: codex/compatibility.json)\n' "$CODEX_DIR"
printf '  - bridge command=./start.sh --path %s -d\n' "$BRIDGE_DEFAULT_PATH"

SETUP_TXN_ACTIVE=0
