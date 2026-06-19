#!/usr/bin/env bash
# Bootstrap a new "Claude Code node" (클코 노드) from this template.
# Installs the SessionStart memory + tool-cheatsheet hooks, the PreCompact/PostCompact
# working-state checkpoint hook, and sanitized settings into ~/.claude,
# and lays down per-node templates you then fill in. NEVER writes secrets.
#
# Usage:
#   ./setup.sh            # install + print follow-up checklist
#   ./setup.sh --dry-run  # show what would happen, change nothing
set -euo pipefail

DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
SRC="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
HERMES_DIR="$HOME/.hermes/memories"

run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else eval "$*"; fi; }
note() { printf '  - %s\n' "$*"; }

echo "==> Installing Claude Code node setup from: $SRC"

# 1) Claude harness config + hooks (safe, secret-free)
run "mkdir -p '$CLAUDE_DIR/hooks'"
run "cp '$SRC/claude/settings.json'           '$CLAUDE_DIR/settings.json'"
run "cp '$SRC/claude/settings.local.json'     '$CLAUDE_DIR/settings.local.json'"
run "cp '$SRC/claude/hooks/load-memory.sh'    '$CLAUDE_DIR/hooks/load-memory.sh'"
run "cp '$SRC/claude/hooks/refresh-memory.sh' '$CLAUDE_DIR/hooks/refresh-memory.sh'"
run "cp '$SRC/claude/hooks/load-tools.sh'     '$CLAUDE_DIR/hooks/load-tools.sh'"
run "cp '$SRC/claude/hooks/checkpoint.sh'     '$CLAUDE_DIR/hooks/checkpoint.sh'"
run "chmod +x '$CLAUDE_DIR/hooks/'*.sh"
# Working-state checkpoint dir (PreCompact snapshot / PostCompact re-inject)
run "mkdir -p '$CLAUDE_DIR/state/checkpoints'"

# 2) Per-node files — only seed templates if a real one is NOT already present.
seed() { # seed <template> <dest>
  if [ -e "$2" ]; then note "kept existing $2 (not overwritten)";
  else run "cp '$1' '$2'"; note "seeded template -> $2 (EDIT ME)"; fi
}
run "mkdir -p '$HERMES_DIR'"
seed "$SRC/claude/CLAUDE.md.template"             "$CLAUDE_DIR/CLAUDE.md"
seed "$SRC/claude/hooks/tools-cheatsheet.md"      "$CLAUDE_DIR/hooks/tools-cheatsheet.md"
seed "$SRC/hermes/memories/MEMORY.template.md"    "$HERMES_DIR/MEMORY.md"
seed "$SRC/hermes/memories/USER.template.md"      "$HERMES_DIR/USER.md"
seed "$SRC/hermes/honcho.template.json"           "$HOME/.hermes/honcho.json"

cat <<'EOF'

==> Done. Follow-up checklist (do these manually):
  1. Edit ~/.claude/CLAUDE.md          — replace every <PLACEHOLDER> with this node's identity/user.
  2. Edit ~/.hermes/memories/MEMORY.md — node-specific durable facts (NO raw secrets).
  3. Edit ~/.hermes/memories/USER.md   — who you work for + preferences.
  4. Edit ~/.hermes/honcho.json        — set baseUrl / peerName / target (this is node-local; gitignored).
  5. Install wiki-agent at /root/.wiki-agent/bin/wiki-agent (canonical: jinwon-int/wiki-agent).
  6. Auth GitHub:  gh auth login   (or place token per node policy; never commit it).
  7. Start a fresh Claude Code session and confirm the SessionStart snapshot injects.
  8. (Optional) Telegram bridge: cd bridge && cp .env.example .env && edit, then
     ./start.sh --path /root -d   (daemon-supervised). See bridge/README.md.

Secrets that are intentionally NOT installed by this script:
  - ~/.claude/.credentials.json   (Claude OAuth — created on `claude` login)
  - GitHub token                  (gh auth login)
  - Honcho endpoint value         (you set it in ~/.hermes/honcho.json)
EOF
