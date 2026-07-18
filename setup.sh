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
  if [ "$failed" = 0 ]; then
    echo "ERROR: setup failed; restored previous installed artifacts" >&2
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
if [ "$WITH_PLUGIN" = 1 ]; then
  note "plugin mode: lean settings (portable hooks come from the ccc-node plugin)"
  run cp "$SRC/claude/settings.base.json" "$CLAUDE_DIR/settings.json"
else
  merge_settings_json "$SRC/claude/settings.base.json" "$SRC/claude/hooks/enforcement-overlay.json" "$CLAUDE_DIR/settings.json"
fi
neutralize_bypass_if_root "$CLAUDE_DIR/settings.json"
# settings.local.json is the NODE-LOCAL approvals file — seed it from the
# template ONLY when absent so a node's accumulated/hand-added approvals are
# never clobbered by setup or self-update (#454). It is not a managed artifact.
if [ ! -e "$CLAUDE_DIR/settings.local.json" ]; then
  run cp "$SRC/claude/settings.local.template.json" "$CLAUDE_DIR/settings.local.json"
else
  note "settings.local.json already present — left untouched (node-local approvals)"
fi
run cp "$SRC/claude/hooks/lib/spawn-detached.sh" "$CLAUDE_DIR/hooks/lib/spawn-detached.sh"
# Shared hook helpers (#584 P0-3): memory/distill/skill hooks hard-source this
# and no-op (exit 0) when it is missing, so it must ship with the hooks.
run cp "$SRC/claude/hooks/lib/hook-common.sh" "$CLAUDE_DIR/hooks/lib/hook-common.sh"
# Portable mtime pruning (#449): checkpoint.sh and distill.sh source this lib
# behind an if-readable guard, so leaving it uninstalled silently disables
# state/checkpoint pruning on every standalone node (unbounded growth).
run cp "$SRC/claude/hooks/lib/mtime-prune.sh" "$CLAUDE_DIR/hooks/lib/mtime-prune.sh"
run cp "$SRC/scripts/lib/harness-paths.sh" "$CLAUDE_DIR/hooks/lib/harness-paths.sh"
run cp "$SRC/scripts/lib/harness_paths.py" "$CLAUDE_DIR/hooks/lib/harness_paths.py"
run cp "$SRC/claude/hooks/load-memory.sh" "$CLAUDE_DIR/hooks/load-memory.sh"
# Codex launch boundary: the launcher and materializer are installed beside
# load-memory.sh so every direct/app-server run reuses the same snapshot policy.
run cp "$SRC/scripts/ccc-codex" "$CLAUDE_DIR/hooks/ccc-codex"
run cp "$SRC/scripts/ccc_codex_memory.py" "$CLAUDE_DIR/hooks/ccc_codex_memory.py"
run cp "$SRC/claude/hooks/refresh-memory.sh" "$CLAUDE_DIR/hooks/refresh-memory.sh"
run cp "$SRC/claude/hooks/scan-injection.sh" "$CLAUDE_DIR/hooks/scan-injection.sh"
run cp "$SRC/claude/hooks/load-tools.sh" "$CLAUDE_DIR/hooks/load-tools.sh"
run cp "$SRC/claude/hooks/checkpoint.sh" "$CLAUDE_DIR/hooks/checkpoint.sh"
run cp "$SRC/claude/hooks/audit.sh" "$CLAUDE_DIR/hooks/audit.sh"
run cp "$SRC/claude/hooks/redact.sh" "$CLAUDE_DIR/hooks/redact.sh"
run cp "$SRC/claude/hooks/notify.sh" "$CLAUDE_DIR/hooks/notify.sh"
run cp "$SRC/claude/hooks/evidence-gate.sh" "$CLAUDE_DIR/hooks/evidence-gate.sh"
run cp "$SRC/claude/hooks/statusline.sh" "$CLAUDE_DIR/hooks/statusline.sh"
run cp "$SRC/claude/hooks/statusline-usage.py" "$CLAUDE_DIR/hooks/statusline-usage.py"
# Memory helper tools used by load-memory.sh / refresh-memory.sh in standalone installs.
run cp "$SRC/scripts/ccc-memory-index.sh" "$CLAUDE_DIR/hooks/ccc-memory-index.sh"
run cp "$SRC/scripts/ccc_memory_index.py" "$CLAUDE_DIR/hooks/ccc_memory_index.py"
run cp "$SRC/scripts/ccc-memory-search.sh" "$CLAUDE_DIR/hooks/ccc-memory-search.sh"
run cp "$SRC/scripts/ccc_memory_search.py" "$CLAUDE_DIR/hooks/ccc_memory_search.py"
run cp "$SRC/scripts/ccc-memory-consolidate.sh" "$CLAUDE_DIR/hooks/ccc-memory-consolidate.sh"
run cp "$SRC/scripts/ccc-memory-query.sh" "$CLAUDE_DIR/hooks/ccc-memory-query.sh"
run cp "$SRC/scripts/ccc-memory-check.sh" "$CLAUDE_DIR/hooks/ccc-memory-check.sh"
run cp "$SRC/scripts/ccc-memory-explain.sh" "$CLAUDE_DIR/hooks/ccc-memory-explain.sh"
run cp "$SRC/scripts/ccc-wiki-triage.sh" "$CLAUDE_DIR/hooks/ccc-wiki-triage.sh"
run cp "$SRC/scripts/ccc-memory-eval.sh" "$CLAUDE_DIR/hooks/ccc-memory-eval.sh"
run cp "$SRC/scripts/ccc-memory-benchmark-export.sh" "$CLAUDE_DIR/hooks/ccc-memory-benchmark-export.sh"
# Session Distiller — PreCompact/SessionEnd trans → Haiku (OAuth) → Honcho push + wiki-candidates queue.
# See pages/team/dungae/DECISIONS.md [TM-1058] for design rationale.
run cp "$SRC/claude/hooks/distill.sh" "$CLAUDE_DIR/hooks/distill.sh"
run mkdir -p "$CLAUDE_DIR/hooks/distill"
run cp "$SRC/claude/hooks/distill/extract.sh" "$CLAUDE_DIR/hooks/distill/extract.sh"
run cp "$SRC/claude/hooks/distill/honcho-push.sh" "$CLAUDE_DIR/hooks/distill/honcho-push.sh"
run cp "$SRC/claude/hooks/distill/wiki-queue.sh" "$CLAUDE_DIR/hooks/distill/wiki-queue.sh"
run cp "$SRC/claude/hooks/distill/queue-drain.sh" "$CLAUDE_DIR/hooks/distill/queue-drain.sh"
run cp "$SRC/claude/hooks/distill/pending-drain.sh" "$CLAUDE_DIR/hooks/distill/pending-drain.sh"
run cp "$SRC/claude/hooks/distill/local-facts.sh" "$CLAUDE_DIR/hooks/distill/local-facts.sh"
run cp "$SRC/claude/hooks/distill/resume-write.sh" "$CLAUDE_DIR/hooks/distill/resume-write.sh"
# Skill Review — Hermes-style background skill draft staging (human-approved by
# default; autoinstall.sh adds the opt-in unattended auto mode, #355).
run cp "$SRC/claude/hooks/skill-review.sh" "$CLAUDE_DIR/hooks/skill-review.sh"
run mkdir -p "$CLAUDE_DIR/hooks/skill-review"
run cp "$SRC/claude/hooks/skill-review/extract.sh" "$CLAUDE_DIR/hooks/skill-review/extract.sh"
run cp "$SRC/claude/hooks/skill-review/autoinstall.sh" "$CLAUDE_DIR/hooks/skill-review/autoinstall.sh"
# Skill autosave sweep — covers bridge/SDK sessions that never fire SessionEnd
# hooks; scheduled separately via scripts/install-skill-autosave-cron.sh.
run cp "$SRC/scripts/ccc-skill-autosave.sh" "$CLAUDE_DIR/hooks/ccc-skill-autosave.sh"
# Self-update — the pre-approved node maintenance procedure (pull + setup +
# restart of operator-allowlisted services only; see docs/self-update.md).
run cp "$SRC/scripts/ccc-self-update.sh" "$CLAUDE_DIR/hooks/ccc-self-update.sh"
installed_hook_scripts=(
  "$CLAUDE_DIR/hooks/lib/spawn-detached.sh"
  "$CLAUDE_DIR/hooks/lib/hook-common.sh"
  "$CLAUDE_DIR/hooks/lib/mtime-prune.sh"
  "$CLAUDE_DIR/hooks/lib/harness-paths.sh"
  "$CLAUDE_DIR/hooks/lib/harness_paths.py"
  "$CLAUDE_DIR/hooks/load-memory.sh"
  "$CLAUDE_DIR/hooks/ccc-codex"
  "$CLAUDE_DIR/hooks/ccc_codex_memory.py"
  "$CLAUDE_DIR/hooks/refresh-memory.sh"
  "$CLAUDE_DIR/hooks/scan-injection.sh"
  "$CLAUDE_DIR/hooks/load-tools.sh"
  "$CLAUDE_DIR/hooks/checkpoint.sh"
  "$CLAUDE_DIR/hooks/audit.sh"
  "$CLAUDE_DIR/hooks/redact.sh"
  "$CLAUDE_DIR/hooks/notify.sh"
  "$CLAUDE_DIR/hooks/evidence-gate.sh"
  "$CLAUDE_DIR/hooks/statusline.sh"
  "$CLAUDE_DIR/hooks/statusline-usage.py"
  "$CLAUDE_DIR/hooks/ccc-memory-index.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-search.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-consolidate.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-query.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-check.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-explain.sh"
  "$CLAUDE_DIR/hooks/ccc-wiki-triage.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-eval.sh"
  "$CLAUDE_DIR/hooks/ccc-memory-benchmark-export.sh"
  "$CLAUDE_DIR/hooks/distill.sh"
  "$CLAUDE_DIR/hooks/distill/extract.sh"
  "$CLAUDE_DIR/hooks/distill/honcho-push.sh"
  "$CLAUDE_DIR/hooks/distill/wiki-queue.sh"
  "$CLAUDE_DIR/hooks/distill/queue-drain.sh"
  "$CLAUDE_DIR/hooks/distill/pending-drain.sh"
  "$CLAUDE_DIR/hooks/distill/local-facts.sh"
  "$CLAUDE_DIR/hooks/distill/resume-write.sh"
  "$CLAUDE_DIR/hooks/skill-review.sh"
  "$CLAUDE_DIR/hooks/skill-review/extract.sh"
  "$CLAUDE_DIR/hooks/skill-review/autoinstall.sh"
  "$CLAUDE_DIR/hooks/ccc-skill-autosave.sh"
  "$CLAUDE_DIR/hooks/ccc-self-update.sh"
)
run chmod +x "${installed_hook_scripts[@]}"
# Tier 3: status line (node·model·git·context·cost·A2A) wired via settings.json statusLine.
# Output style (한국어 구조화 보고) — node-agnostic; settings.json activates it as outputStyle.
run mkdir -p "$CLAUDE_DIR/output-styles"
run cp "$SRC/claude/output-styles/"*.md "$CLAUDE_DIR/output-styles/"
# Headless runner for cron/A2A/CI (`claude -p` wrapper).
run cp "$SRC/claude/headless.sh" "$CLAUDE_DIR/headless.sh"
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
    *) run cp "$_agent_src" "$CLAUDE_DIR/agents/$(basename "$_agent_src")" ;;
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
    run cp "$_agent_src" "$CLAUDE_DIR/agents/$(basename "$_agent_src")"
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
# Custom skills (reusable procedures: wiki-record, mcp-add, skill-suggest, ...) — node-agnostic
run mkdir -p "$CLAUDE_DIR/skills"
run cp -r "$SRC/claude/skills/." "$CLAUDE_DIR/skills/"
skill_sources=("$SRC"/claude/skills/*/*.sh)
skill_targets=()
for skill_source in "${skill_sources[@]}"; do
  [ -e "$skill_source" ] || continue
  skill_targets+=("$CLAUDE_DIR/skills/${skill_source#"$SRC/claude/skills/"}")
done
if [ "${#skill_targets[@]}" -gt 0 ]; then run chmod +x "${skill_targets[@]}"; fi

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
      "${installed_hook_scripts[@]}" "${SEEDED[@]}"
    )
    for source_tree in output-styles agents commands skills; do
      while IFS= read -r -d '' source_file; do
        rewrite_targets+=("$CLAUDE_DIR/$source_tree/${source_file#"$SRC/claude/$source_tree/"}")
      done < <(find "$SRC/claude/$source_tree" -type f -print0)
    done
    for rewrite_file in "${rewrite_targets[@]}"; do
      [ -f "$rewrite_file" ] || continue
      python3 - "$rewrite_file" "/opt/ccc-node" "$SRC" "/root/.claude" "$CLAUDE_DIR" <<'PY'
import re
import sys

path = sys.argv[1]
args = sys.argv[2:]
pairs = {old: new for old, new in zip(args[0::2], args[1::2]) if old != new}
if pairs:
    # Single non-cascading pass: every occurrence in the ORIGINAL text is
    # replaced exactly once and replacement values are never rescanned, so one
    # pair's output cannot be corrupted by the other pair. Longest token first
    # for deterministic behavior on overlapping prefixes.
    pattern = re.compile(
        "|".join(re.escape(tok) for tok in sorted(pairs, key=len, reverse=True))
    )
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    content = pattern.sub(lambda m: pairs[m.group(0)], content)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
PY
    done
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
printf '  - bridge command=./start.sh --path %s -d\n' "$BRIDGE_DEFAULT_PATH"

SETUP_TXN_ACTIVE=0
