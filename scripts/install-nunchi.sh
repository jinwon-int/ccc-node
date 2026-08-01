#!/usr/bin/env bash
# install-nunchi.sh (#816) — opt-in enable/disable for the nunchi memory
# shadow pilot. The hook tree (claude/hooks/nunchi/) is deployed to every
# node by setup.sh but is a no-op until state/nunchi.mode is "on".
#
#   install-nunchi.sh --apply              # auto-detect live provider
#   install-nunchi.sh --apply --codex      # explicit Codex override
#   install-nunchi.sh --apply --claude     # explicit Claude override
#   install-nunchi.sh --apply --target-user gongmyoung
#   install-nunchi.sh --remove          # mode off + cron lines removed (code/DB kept)
#   install-nunchi.sh                   # status
#
# Also retires a pre-#816 hand-deployed pilot ($HOME/nunchi) if present:
# renames it and strips its old crontab lines so only the canonical copy runs.
set -euo pipefail

ACTION="status"
PROVIDER="${CCC_NUNCHI_PROVIDER:-auto}"
TARGET_USER="${CCC_NUNCHI_TARGET_USER:-}"
ORIGINAL_ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) ACTION="apply" ;;
    --remove) ACTION="remove" ;;
    --codex) PROVIDER="codex" ;;
    --claude) PROVIDER="claude" ;;
    --target-user)
      [ $# -ge 2 ] || { echo "--target-user requires a user" >&2; exit 2; }
      TARGET_USER="$2"; shift ;;
    --help|-h)
      sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$PROVIDER" in codex|claude|auto) ;; *) echo "invalid provider: $PROVIDER" >&2; exit 2;; esac

# A bridge may run as a non-root service user even when the operator connects
# as root. Re-exec as that user so HOME, crontab ownership, DB ownership and the
# actual runtime's hook tree stay in one scope (#827 gongmyoung finding).
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "$(id -un)" ]; then
  [ "$(id -u)" = 0 ] || { echo "--target-user requires root when switching users" >&2; exit 2; }
  command -v getent >/dev/null 2>&1 || { echo "getent required for --target-user" >&2; exit 2; }
  command -v runuser >/dev/null 2>&1 || { echo "runuser required for --target-user" >&2; exit 2; }
  target_home="${CCC_NUNCHI_TARGET_HOME:-$(getent passwd "$TARGET_USER" | awk -F: '{print $6}')}"
  [ -n "$target_home" ] && [ "$target_home" != "/" ] && [ -d "$target_home" ] \
    || { echo "safe target home not found for $TARGET_USER" >&2; exit 2; }
  exec runuser -u "$TARGET_USER" -- env \
    -u CCC_CLAUDE_DIR -u CCC_STATE_DIR -u NUNCHI_HOME -u NUNCHI_DB -u NUNCHI_SNAPSHOT \
    HOME="$target_home" CCC_NUNCHI_TARGET_USER="$TARGET_USER" \
    CCC_NUNCHI_TARGET_HOME="$target_home" CCC_NUNCHI_PROVIDER="$PROVIDER" \
    bash "$0" "${ORIGINAL_ARGS[@]}"
fi

CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
HOOKS="$CLAUDE_DIR/hooks/nunchi"
MODE_FILE="$STATE/nunchi.mode"
MARK="# nunchi:#816"
TS="$(date +%Y%m%dT%H%M%S)"

status() {
  local cron provider
  cron="$(crontab -l 2>/dev/null || true)"
  provider="missing"
  grep -q 'codex-feed.sh' <<<"$cron" && provider="codex"
  grep -q 'ingest-cron.sh' <<<"$cron" && provider="claude"
  echo "runtime: user=$(id -un) home=$HOME provider=$provider"
  echo "mode: $(cat "$MODE_FILE" 2>/dev/null || echo off)"
  echo "hooks: $([ -f "$HOOKS/nunchi.py" ] && echo present || echo MISSING) ($HOOKS)"
  echo "cron: $(grep -cF "$MARK" <<<"$cron" || true) line(s)"
  echo "db: $(ls -la "$HOME/.nunchi/facts.db" 2>/dev/null | awk '{print $5" bytes"}' || echo none)"
}

strip_cron() {  # remove our marker lines + any legacy hand-deploy nunchi lines
  local tmp; tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -vF "$MARK" | grep -v "nunchi/ingest-cron.sh\|nunchi/codex-feed.sh\|/nunchi/ingest-cron\|nunchi:distill-mirror\|nunchi:codex-feed" > "$tmp" || true
  crontab "$tmp"; rm -f "$tmp"
}

append_cron_line() { # <literal cron line>
  local line="$1" tmp
  tmp="$(mktemp)"
  crontab -l > "$tmp" 2>/dev/null || true
  printf '%s\n' "$line" >> "$tmp"
  crontab "$tmp"
  rm -f "$tmp"
}

retire_legacy() {
  local legacy="$HOME/nunchi"
  if [ -f "$legacy/nunchi.py" ] && [ "$legacy" != "$HOOKS" ]; then
    mv "$legacy" "$legacy.retired-$TS"
    echo "legacy hand-deploy retired: $legacy -> $legacy.retired-$TS (DB untouched)"
  fi
}

remove_standalone_sessionstart_hooks() {
  # load-memory.sh now injects nunchi for both Claude and Codex. Remove both
  # the canonical pilot hook and retired /root/nunchi hook so Claude does not
  # inject the same snapshot twice and broken legacy paths stop firing.
  python3 - "$CLAUDE_DIR/settings.local.json" <<'PY'
import json, os, sys
path = sys.argv[1]
if not os.path.exists(path):
    raise SystemExit(0)
with open(path) as f:
    data = json.load(f)
groups = data.get("hooks", {}).get("SessionStart", [])
removed = 0
kept = []
for group in groups:
    hooks = []
    for hook in group.get("hooks", []):
        command = str(hook.get("command") or "")
        if "nunchi/sessionstart.sh" in command:
            removed += 1
        else:
            hooks.append(hook)
    if hooks:
        copy = dict(group)
        copy["hooks"] = hooks
        kept.append(copy)
if removed:
    data["hooks"]["SessionStart"] = kept
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
print(f"standalone nunchi SessionStart hooks removed: {removed}")
PY
}

detect_provider() {
  if [ "$PROVIDER" != "auto" ]; then printf '%s' "$PROVIDER"; return; fi
  local root bridge_status=""
  root="$(cd "$(dirname "$0")/.." && pwd)"
  if [ -f "$root/bridge/start.sh" ]; then
    bridge_status="$(HOME="$HOME" bash "$root/bridge/start.sh" --path "$HOME" --status 2>/dev/null || true)"
  fi
  if grep -q 'Codex: healthy' <<<"$bridge_status"; then printf 'codex'; return; fi
  if grep -q 'Claude: healthy' <<<"$bridge_status"; then printf 'claude'; return; fi
  if [ -d "$HOME/.codex/sessions" ] && [ ! -d "$HOME/.claude/projects" ]; then
    printf 'codex'
  else
    printf 'claude'
  fi
}

case "$ACTION" in
  apply)
    [ -f "$HOOKS/nunchi.py" ] || { echo "hooks missing at $HOOKS — run setup.sh first" >&2; exit 2; }
    mkdir -p "$STATE"
    retire_legacy
    strip_cron
    printf 'on' > "$MODE_FILE"
    resolved_provider="$(detect_provider)"
    feed="$HOOKS/ingest-cron.sh"
    [ "$resolved_provider" = "codex" ] && feed="$HOOKS/codex-feed.sh"
    append_cron_line "*/10 * * * * bash $feed >> $HOME/.nunchi/cron.log 2>&1 $MARK"
    # #824 Phase 1: hourly incremental MemPalace sweep keeps the verbatim
    # layer fresh (sweep is idempotent). Only when the external CLI and the
    # transcript dir exist; skipped silently otherwise (peer_facts-only).
    mp="$(command -v mempalace || true)"
    [ -z "$mp" ] && [ -x "$HOME/.local/bin/mempalace" ] && mp="$HOME/.local/bin/mempalace"
    default_sweep="$HOME/.claude/projects"
    [ "$resolved_provider" = "codex" ] && default_sweep="$HOME/.codex/sessions"
    sweep_dir="${NUNCHI_SWEEP_DIR:-$default_sweep}"
    if [ -n "$mp" ] && [ -d "$sweep_dir" ]; then
      append_cron_line "17 * * * * $mp sweep $sweep_dir >> $HOME/.nunchi/mempalace-sweep.cron.log 2>&1 $MARK"
      echo "mempalace hourly sweep cron added ($sweep_dir)"
    else
      echo "mempalace CLI or transcript dir missing — verbatim sweep cron skipped"
    fi
    # #827 Phase 2: weekly parity bench (Mon 08:07) — feeds the gate-3
    # retirement criteria (two weeks of zero Honcho-only answers, zero
    # hallucination). bench.sh is itself mode-gated, so this line is safe
    # even if the node opts out later without --remove.
    append_cron_line "7 8 * * 1 bash $HOOKS/bench.sh >> $HOME/.nunchi/bench.cron.log 2>&1 $MARK"
    echo "weekly bench cron added (Mon 08:07)"
    remove_standalone_sessionstart_hooks
    python3 "$HOOKS/nunchi.py" init
    echo "nunchi enabled (mode=on, provider=$resolved_provider, feed=$(basename "$feed"))"; status
    ;;
  remove)
    printf 'off' > "$MODE_FILE"
    strip_cron
    remove_standalone_sessionstart_hooks
    echo "nunchi disabled (code and ~/.nunchi DB kept)"; status
    ;;
  *) status ;;
esac
