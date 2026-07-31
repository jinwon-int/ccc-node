#!/usr/bin/env bash
# install-nunchi.sh (#816) — opt-in enable/disable for the nunchi memory
# shadow pilot. The hook tree (claude/hooks/nunchi/) is deployed to every
# node by setup.sh but is a no-op until state/nunchi.mode is "on".
#
#   install-nunchi.sh --apply           # Claude node: mode on + ingest cron + SessionStart hook
#   install-nunchi.sh --apply --codex   # Codex node:  mode on + codex-feed cron + SessionStart hook
#   install-nunchi.sh --remove          # mode off + cron lines removed (code/DB kept)
#   install-nunchi.sh                   # status
#
# Also retires a pre-#816 hand-deployed pilot ($HOME/nunchi) if present:
# renames it and strips its old crontab lines so only the canonical copy runs.
set -euo pipefail

CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
HOOKS="$CLAUDE_DIR/hooks/nunchi"
MODE_FILE="$STATE/nunchi.mode"
MARK="# nunchi:#816"
TS="$(date +%Y%m%dT%H%M%S)"

status() {
  echo "mode: $(cat "$MODE_FILE" 2>/dev/null || echo off)"
  echo "hooks: $([ -f "$HOOKS/nunchi.py" ] && echo present || echo MISSING) ($HOOKS)"
  echo "cron: $(crontab -l 2>/dev/null | grep -cF "$MARK" || true) line(s)"
  echo "db: $(ls -la "$HOME/.nunchi/facts.db" 2>/dev/null | awk '{print $5" bytes"}' || echo none)"
}

strip_cron() {  # remove our marker lines + any legacy hand-deploy nunchi lines
  local tmp; tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -vF "$MARK" | grep -v "nunchi/ingest-cron.sh\|nunchi/codex-feed.sh\|/nunchi/ingest-cron\|nunchi:distill-mirror\|nunchi:codex-feed" > "$tmp" || true
  crontab "$tmp"; rm -f "$tmp"
}

retire_legacy() {
  local legacy="$HOME/nunchi"
  if [ -f "$legacy/nunchi.py" ] && [ "$legacy" != "$HOOKS" ]; then
    mv "$legacy" "$legacy.retired-$TS"
    echo "legacy hand-deploy retired: $legacy -> $legacy.retired-$TS (DB untouched)"
  fi
}

add_sessionstart_hook() {
  # Idempotently append our SessionStart hook to settings.local.json.
  # The settings template stays untouched until gate 3 (#816 out of scope).
  python3 - "$CLAUDE_DIR/settings.local.json" "$HOOKS/sessionstart.sh" <<'PY'
import json, os, sys
path, script = sys.argv[1], sys.argv[2]
cmd = f"bash {script}"
data = {}
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
hooks = data.setdefault("hooks", {}).setdefault("SessionStart", [])
present = any(cmd == h.get("command") for grp in hooks for h in grp.get("hooks", []))
if not present:
    hooks.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"SessionStart hook added to {path}")
else:
    print("SessionStart hook already present")
PY
}

case "${1:-}" in
  --apply)
    [ -f "$HOOKS/nunchi.py" ] || { echo "hooks missing at $HOOKS — run setup.sh first" >&2; exit 2; }
    mkdir -p "$STATE"
    retire_legacy
    strip_cron
    printf 'on' > "$MODE_FILE"
    feed="$HOOKS/ingest-cron.sh"
    [ "${2:-}" = "--codex" ] && feed="$HOOKS/codex-feed.sh"
    ( crontab -l 2>/dev/null; echo "*/10 * * * * bash $feed >> $HOME/.nunchi/cron.log 2>&1 $MARK" ) | crontab -
    # #824 Phase 1: hourly incremental MemPalace sweep keeps the verbatim
    # layer fresh (sweep is idempotent). Only when the external CLI and the
    # transcript dir exist; skipped silently otherwise (peer_facts-only).
    mp="$(command -v mempalace || true)"
    [ -z "$mp" ] && [ -x "$HOME/.local/bin/mempalace" ] && mp="$HOME/.local/bin/mempalace"
    sweep_dir="${NUNCHI_SWEEP_DIR:-$HOME/.claude/projects}"
    if [ -n "$mp" ] && [ -d "$sweep_dir" ]; then
      ( crontab -l 2>/dev/null; echo "17 * * * * $mp sweep $sweep_dir >> $HOME/.nunchi/mempalace-sweep.cron.log 2>&1 $MARK" ) | crontab -
      echo "mempalace hourly sweep cron added ($sweep_dir)"
    else
      echo "mempalace CLI or transcript dir missing — verbatim sweep cron skipped"
    fi
    # #827 Phase 2: weekly parity bench (Mon 08:07) — feeds the gate-3
    # retirement criteria (two weeks of zero Honcho-only answers, zero
    # hallucination). bench.sh is itself mode-gated, so this line is safe
    # even if the node opts out later without --remove.
    ( crontab -l 2>/dev/null; echo "7 8 * * 1 bash $HOOKS/bench.sh >> $HOME/.nunchi/bench.cron.log 2>&1 $MARK" ) | crontab -
    echo "weekly bench cron added (Mon 08:07)"
    add_sessionstart_hook
    python3 "$HOOKS/nunchi.py" init
    echo "nunchi enabled (mode=on, feed=$(basename "$feed"))"; status
    ;;
  --remove)
    printf 'off' > "$MODE_FILE"
    strip_cron
    echo "nunchi disabled (code and ~/.nunchi DB kept)"; status
    ;;
  *) status ;;
esac
