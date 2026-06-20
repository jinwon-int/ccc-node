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
set -euo pipefail

DRY=0; WITH_PLUGIN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --with-plugin) WITH_PLUGIN=1 ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done
SRC="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
MEM_DIR="$CLAUDE_DIR/memories"          # node-owned memory (Hermes-independent)
HERMES_DIR="$HOME/.hermes/memories"     # legacy memory location (fallback only)

run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else eval "$*"; fi; }
note() { printf '  - %s\n' "$*"; }

echo "==> Installing Claude Code node setup from: $SRC"

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
  run "jq -s '.[0] as \$b | .[1] as \$o | \$b | .hooks = (\$b.hooks + \$o.hooks)' '$SRC/claude/settings.base.json' '$SRC/claude/hooks/enforcement-overlay.json' > '$CLAUDE_DIR/settings.json'"
fi
run "cp '$SRC/claude/settings.local.json'     '$CLAUDE_DIR/settings.local.json'"
run "cp '$SRC/claude/hooks/load-memory.sh'    '$CLAUDE_DIR/hooks/load-memory.sh'"
run "cp '$SRC/claude/hooks/refresh-memory.sh' '$CLAUDE_DIR/hooks/refresh-memory.sh'"
run "cp '$SRC/claude/hooks/load-tools.sh'     '$CLAUDE_DIR/hooks/load-tools.sh'"
run "cp '$SRC/claude/hooks/checkpoint.sh'     '$CLAUDE_DIR/hooks/checkpoint.sh'"
run "cp '$SRC/claude/hooks/guard.sh'          '$CLAUDE_DIR/hooks/guard.sh'"
run "cp '$SRC/claude/hooks/audit.sh'          '$CLAUDE_DIR/hooks/audit.sh'"
run "cp '$SRC/claude/hooks/redact.sh'         '$CLAUDE_DIR/hooks/redact.sh'"
run "cp '$SRC/claude/hooks/notify.sh'         '$CLAUDE_DIR/hooks/notify.sh'"
run "cp '$SRC/claude/hooks/statusline.sh'     '$CLAUDE_DIR/hooks/statusline.sh'"
run "chmod +x '$CLAUDE_DIR/hooks/'*.sh"
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
seed() { # seed <template> <dest>
  if [ -e "$2" ]; then note "kept existing $2 (not overwritten)";
  else run "cp '$1' '$2'"; note "seeded template -> $2 (EDIT ME)"; fi
}
run "mkdir -p '$MEM_DIR'"
run "mkdir -p '$HOME/.hermes'"
seed "$SRC/claude/CLAUDE.md.template"             "$CLAUDE_DIR/CLAUDE.md"
seed "$SRC/claude/hooks/tools-cheatsheet.md"      "$CLAUDE_DIR/hooks/tools-cheatsheet.md"
# Node-owned memory (Hermes-independent): seed into ~/.claude/memories.
# load-memory.sh reads here first, falling back to ~/.hermes/memories only if absent.
seed "$SRC/hermes/memories/MEMORY.template.md"    "$MEM_DIR/MEMORY.md"
seed "$SRC/hermes/memories/USER.template.md"      "$MEM_DIR/USER.md"
# honcho.json stays node-local under ~/.hermes (documentation/Hermes-side; not a hard CC dep).
seed "$SRC/hermes/honcho.template.json"           "$HOME/.hermes/honcho.json"

cat <<'EOF'

==> Done. Follow-up checklist (do these manually):
  1. Edit ~/.claude/CLAUDE.md          — replace every <PLACEHOLDER> with this node's identity/user.
  2. Edit ~/.claude/memories/MEMORY.md — node-specific durable facts (NO raw secrets).
  3. Edit ~/.claude/memories/USER.md   — who you work for + preferences.
  4. Edit ~/.hermes/honcho.json        — set baseUrl / peerName / target (this is node-local; gitignored).
  5. Install wiki-agent at /root/.wiki-agent/bin/wiki-agent (canonical: jinwon-int/wiki-agent).
  6. Auth GitHub:  gh auth login   (or place token per node policy; never commit it).
  7. Start a fresh Claude Code session and confirm the SessionStart snapshot injects.
  8. (Optional) MCP tool servers: ./claude/mcp-setup.sh
     Registers searxng (Tailnet SearXNG) + context7 (docs) + firecrawl (web scrape;
     key read from ~/.hermes/.env). Idempotent; tool perms pre-allowed in settings.json.
  9. (Optional) Telegram bridge: cd bridge && cp .env.example .env && edit, then
     ./start.sh --path /root -d   (daemon-supervised). See bridge/README.md.

Secrets that are intentionally NOT installed by this script:
  - ~/.claude/.credentials.json   (Claude OAuth — created on `claude` login)
  - GitHub token                  (gh auth login)
  - Honcho endpoint value         (you set it in ~/.hermes/honcho.json)
EOF
