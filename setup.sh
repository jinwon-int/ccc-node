#!/usr/bin/env bash
# Bootstrap a new "Claude Code node" (클코 노드) from this template.
# Installs the SessionStart memory + tool-cheatsheet hooks, the PreCompact/PostCompact
# working-state checkpoint hook, and sanitized settings into ~/.claude,
# and lays down per-node templates you then fill in. NEVER writes secrets.
#
# Usage:
#   ./setup.sh                 # standalone: install full settings (portable hooks included)
#   ./setup.sh --with-plugin   # plugin mode: lean settings; the ccc-node PLUGIN owns the
#                              #   portable hooks (guard/audit/redact/notify) — avoids the
#                              #   double-firing you'd get if both settings.json and the
#                              #   plugin registered them. Node-local hooks stay in settings.
#   ./setup.sh --dry-run       # show what would happen, change nothing (combine with above)
#   ./setup.sh --no-backup     # do NOT snapshot the existing ~/.claude before overwriting
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

run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else eval "$*"; fi; }
note() { printf '  - %s\n' "$*"; }

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
  if jq -s '.[0] as $b | .[1] as $o | $b | .hooks = ($b.hooks + $o.hooks)' "$base" "$overlay" > "$tmp" 2>/dev/null \
     && jq -e . "$tmp" >/dev/null 2>&1; then
    mv "$tmp" "$dest"
  else
    rm -f "$tmp"
    echo "ERROR: failed to merge settings.json from '$base' + '$overlay' (existing file left untouched)" >&2
    return 1
  fi
}

# Snapshot the existing ~/.claude config BEFORE we overwrite anything. setup.sh unconditionally
# overwrites settings.json, settings.local.json and the hook/output-style/agent/command/skill
# dirs — on a node that already has a configured identity that is destructive, so we tar a
# restore point first. Credentials (~/.claude/.credentials.json) are intentionally NOT included.
backup_claude_dir() {
  if [ "$BACKUP" != 1 ]; then note "backup skipped (--no-backup)"; return 0; fi
  [ -d "$CLAUDE_DIR" ] || { note "no existing $CLAUDE_DIR — nothing to back up"; return 0; }
  local items=() p
  for p in settings.json settings.local.json hooks output-styles agents commands skills; do
    [ -e "$CLAUDE_DIR/$p" ] && items+=("$p")
  done
  if [ "${#items[@]}" -eq 0 ]; then note "fresh install — no overwritable config to back up"; return 0; fi
  local ts archive
  ts="$(date +%Y%m%d-%H%M%S)"
  archive="$CLAUDE_DIR/backups/ccc-node-setup-$ts.tar.gz"
  run "mkdir -p '$CLAUDE_DIR/backups'"
  if ! run "tar -czf '$archive' -C '$CLAUDE_DIR' ${items[*]}"; then
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

# 1) Claude harness config + hooks (safe, secret-free)
run "mkdir -p '$CLAUDE_DIR/hooks'"
# settings.json is composed from two sources so the portable enforcement/observability
# hooks have a SINGLE owner (no double-firing):
#   - claude/settings.base.json          : node-local hooks + statusLine + outputStyle (always)
#   - claude/hooks/enforcement-overlay.json : portable hooks guard/audit/redact/notify
# Standalone (default): base + overlay merged → settings.json owns everything.
# --with-plugin: base only → the ccc-node plugin's hooks/hooks.json owns the portable hooks.
if [ "$WITH_PLUGIN" = 1 ]; then
  note "plugin mode: lean settings (portable hooks come from the ccc-node plugin)"
  run "cp '$SRC/claude/settings.base.json'    '$CLAUDE_DIR/settings.json'"
else
  merge_settings_json "$SRC/claude/settings.base.json" "$SRC/claude/hooks/enforcement-overlay.json" "$CLAUDE_DIR/settings.json"
fi
run "cp '$SRC/claude/settings.local.json'     '$CLAUDE_DIR/settings.local.json'"
run "cp '$SRC/claude/hooks/load-memory.sh'    '$CLAUDE_DIR/hooks/load-memory.sh'"
run "cp '$SRC/claude/hooks/refresh-memory.sh' '$CLAUDE_DIR/hooks/refresh-memory.sh'"
run "cp '$SRC/claude/hooks/scan-injection.sh' '$CLAUDE_DIR/hooks/scan-injection.sh'"
run "cp '$SRC/claude/hooks/load-tools.sh'     '$CLAUDE_DIR/hooks/load-tools.sh'"
run "cp '$SRC/claude/hooks/checkpoint.sh'     '$CLAUDE_DIR/hooks/checkpoint.sh'"
run "cp '$SRC/claude/hooks/guard.sh'          '$CLAUDE_DIR/hooks/guard.sh'"
run "cp '$SRC/claude/hooks/audit.sh'          '$CLAUDE_DIR/hooks/audit.sh'"
run "cp '$SRC/claude/hooks/redact.sh'         '$CLAUDE_DIR/hooks/redact.sh'"
run "cp '$SRC/claude/hooks/notify.sh'         '$CLAUDE_DIR/hooks/notify.sh'"
run "cp '$SRC/claude/hooks/evidence-gate.sh'  '$CLAUDE_DIR/hooks/evidence-gate.sh'"
run "cp '$SRC/claude/hooks/statusline.sh'     '$CLAUDE_DIR/hooks/statusline.sh'"
# Memory helper tools used by load-memory.sh / refresh-memory.sh in standalone installs.
run "cp '$SRC/scripts/ccc-memory-index.sh'    '$CLAUDE_DIR/hooks/ccc-memory-index.sh'"
run "cp '$SRC/scripts/ccc_memory_index.py'    '$CLAUDE_DIR/hooks/ccc_memory_index.py'"
run "cp '$SRC/scripts/ccc-memory-search.sh'   '$CLAUDE_DIR/hooks/ccc-memory-search.sh'"
run "cp '$SRC/scripts/ccc_memory_search.py'   '$CLAUDE_DIR/hooks/ccc_memory_search.py'"
run "cp '$SRC/scripts/ccc-memory-consolidate.sh' '$CLAUDE_DIR/hooks/ccc-memory-consolidate.sh'"
run "cp '$SRC/scripts/ccc-memory-query.sh'    '$CLAUDE_DIR/hooks/ccc-memory-query.sh'"
run "cp '$SRC/scripts/ccc-memory-check.sh'    '$CLAUDE_DIR/hooks/ccc-memory-check.sh'"
run "cp '$SRC/scripts/ccc-memory-explain.sh'  '$CLAUDE_DIR/hooks/ccc-memory-explain.sh'"
run "cp '$SRC/scripts/ccc-wiki-triage.sh'     '$CLAUDE_DIR/hooks/ccc-wiki-triage.sh'"
run "cp '$SRC/scripts/ccc-memory-eval.sh'     '$CLAUDE_DIR/hooks/ccc-memory-eval.sh'"
run "cp '$SRC/scripts/ccc-memory-benchmark-export.sh' '$CLAUDE_DIR/hooks/ccc-memory-benchmark-export.sh'"
# Session Distiller — PreCompact/SessionEnd trans → Haiku (OAuth) → Honcho push + wiki-candidates queue.
# See pages/team/dungae/DECISIONS.md [TM-1058] for design rationale.
run "cp '$SRC/claude/hooks/distill.sh'        '$CLAUDE_DIR/hooks/distill.sh'"
run "mkdir -p '$CLAUDE_DIR/hooks/distill'"
run "cp '$SRC/claude/hooks/distill/extract.sh'     '$CLAUDE_DIR/hooks/distill/extract.sh'"
run "cp '$SRC/claude/hooks/distill/honcho-push.sh' '$CLAUDE_DIR/hooks/distill/honcho-push.sh'"
run "cp '$SRC/claude/hooks/distill/wiki-queue.sh'  '$CLAUDE_DIR/hooks/distill/wiki-queue.sh'"
run "cp '$SRC/claude/hooks/distill/queue-drain.sh' '$CLAUDE_DIR/hooks/distill/queue-drain.sh'"
run "cp '$SRC/claude/hooks/distill/local-facts.sh' '$CLAUDE_DIR/hooks/distill/local-facts.sh'"
run "cp '$SRC/claude/hooks/distill/resume-write.sh' '$CLAUDE_DIR/hooks/distill/resume-write.sh'"
# Skill Review — Hermes-style background skill draft staging (human-approved).
run "cp '$SRC/claude/hooks/skill-review.sh' '$CLAUDE_DIR/hooks/skill-review.sh'"
run "mkdir -p '$CLAUDE_DIR/hooks/skill-review'"
run "cp '$SRC/claude/hooks/skill-review/extract.sh' '$CLAUDE_DIR/hooks/skill-review/extract.sh'"
# Skill autosave sweep — covers bridge/SDK sessions that never fire SessionEnd
# hooks; scheduled separately via scripts/install-skill-autosave-cron.sh.
run "cp '$SRC/scripts/ccc-skill-autosave.sh' '$CLAUDE_DIR/hooks/ccc-skill-autosave.sh'"
# Self-update — the pre-approved node maintenance procedure (pull + setup +
# restart of operator-allowlisted services only; see docs/self-update.md).
run "cp '$SRC/scripts/ccc-self-update.sh' '$CLAUDE_DIR/hooks/ccc-self-update.sh'"
run "chmod +x '$CLAUDE_DIR/hooks/'*.sh '$CLAUDE_DIR/hooks/distill/'*.sh '$CLAUDE_DIR/hooks/skill-review/'*.sh"
# Tier 3: status line (node·model·git·context·cost·A2A) wired via settings.json statusLine.
# Output style (한국어 구조화 보고) — node-agnostic; settings.json activates it as outputStyle.
run "mkdir -p '$CLAUDE_DIR/output-styles'"
run "cp '$SRC/claude/output-styles/'*.md '$CLAUDE_DIR/output-styles/'"
# Headless runner for cron/A2A/CI (`claude -p` wrapper, guard still applies).
run "cp '$SRC/claude/headless.sh'             '$CLAUDE_DIR/headless.sh'"
run "chmod +x '$CLAUDE_DIR/headless.sh'"
# Working-state checkpoint dir (PreCompact snapshot / PostCompact re-inject)
run "mkdir -p '$CLAUDE_DIR/state/checkpoints'"
# A2A worker sub-agent roster (explorer/implementer/verifier) — node-agnostic role defs
run "mkdir -p '$CLAUDE_DIR/agents'"
run "cp '$SRC/claude/agents/'*.md '$CLAUDE_DIR/agents/'"
# Slash commands (quick prompt templates: /node-status, /a2a-claim, /wiki-log) — node-agnostic
run "mkdir -p '$CLAUDE_DIR/commands'"
run "cp '$SRC/claude/commands/'*.md '$CLAUDE_DIR/commands/'"
# Custom skills (reusable procedures: wiki-record, mcp-add, skill-suggest, ...) — node-agnostic
run "mkdir -p '$CLAUDE_DIR/skills'"
run "cp -r '$SRC/claude/skills/.' '$CLAUDE_DIR/skills/'"
run "chmod +x '$CLAUDE_DIR/skills/'*/*.sh 2>/dev/null || true"

# 2) Per-node files — only seed templates if a real one is NOT already present.
SEEDED=()  # files freshly created from a template this run (safe to placeholder-substitute)
seed() { # seed <template> <dest>
  if [ -e "$2" ]; then note "kept existing $2 (not overwritten)";
  else run "cp '$1' '$2'"; SEEDED+=("$2"); note "seeded template -> $2 (EDIT ME)"; fi
}
run "mkdir -p '$MEM_DIR'"
run "mkdir -p '$HERMES_ROOT'"
seed "$SRC/claude/CLAUDE.md.template"             "$CLAUDE_DIR/CLAUDE.md"
seed "$SRC/claude/hooks/tools-cheatsheet.md"      "$CLAUDE_DIR/hooks/tools-cheatsheet.md"
# Node-owned memory (Hermes-independent): seed into ~/.claude/memories.
# load-memory.sh reads here first, falling back to ~/.hermes/memories only if absent.
seed "$SRC/hermes/memories/MEMORY.template.md"    "$MEM_DIR/MEMORY.md"
seed "$SRC/hermes/memories/USER.template.md"      "$MEM_DIR/USER.md"
# honcho.json stays node-local under ~/.hermes (documentation/Hermes-side; not a hard CC dep).
seed "$SRC/hermes/honcho.template.json"           "$HERMES_ROOT/honcho.json"

# 2b) HOME-path rewrite — the settings/hook/skill templates use /root/.claude as the
# canonical harness path. On nodes whose harness dir is not /root/.claude (e.g. Termux,
# HOME=/data/data/com.termux/files/home), rewrite the installed files so settings.json
# hook *command* paths resolve AND hook internal defaults (${CCC_*:-/root/.claude/...})
# point at this node's real dir. Without this, claude fails its SessionEnd/Start hooks
# (hook script not found) and memory/cache/state default to a nonexistent /root path.
# No-op on standard root-HOME nodes where CLAUDE_DIR == /root/.claude.
if [ "$CLAUDE_DIR" != "/root/.claude" ]; then
  note "rewrite /root/.claude -> $CLAUDE_DIR in installed harness files"
  run "grep -rlZ '/root/.claude' '$CLAUDE_DIR' 2>/dev/null | xargs -0 -r sed -i 's#/root/.claude#$CLAUDE_DIR#g' || true"
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
printf '  - bridge command=./start.sh --path %s -d\n' "$BRIDGE_DEFAULT_PATH"
