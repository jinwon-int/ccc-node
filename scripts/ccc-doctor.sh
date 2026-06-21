#!/usr/bin/env bash
# ccc doctor — read-only harness consistency diagnostics.
#
# First slice for issue #52: classify drift only. `--fix` is intentionally
# non-mutating/not implemented here; future slices must add backup + dry-run +
# idempotent repair before writing anything.
set -uo pipefail

REPO="${CCC_DOCTOR_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
CLAUDE_DIR="${CCC_DOCTOR_CLAUDE_DIR:-/root/.claude}"
SETTINGS="$CLAUDE_DIR/settings.json"
FIX=0
for arg in "$@"; do
  case "$arg" in
    --fix) FIX=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: ccc-doctor.sh [--fix]

Read-only ccc-node harness diagnostics. Classifies checks as:
정상 / 경고 / 교정가능 / 수동필요.

This implementation slice is diagnostic-only: --fix is not implemented and
makes no filesystem changes.
EOF
      exit 0
      ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

if [ "$FIX" = 1 ]; then
  echo "ccc doctor --fix is not implemented in this diagnostic-only slice; no filesystem changes were made." >&2
  exit 2
fi

normal=0; warn=0; fixable=0; manual=0
rows=()
add() { # <class> <item> <status> <action>
  local class="$1" item="$2" status="$3" action="$4"
  rows+=("$class|$item|$status|$action")
  case "$class" in
    정상) normal=$((normal+1)) ;;
    경고) warn=$((warn+1)) ;;
    교정가능) fixable=$((fixable+1)) ;;
    수동필요) manual=$((manual+1)) ;;
  esac
}

json_ok() { jq -e . "$1" >/dev/null 2>&1; }
json_has() { jq -e "$2" "$1" >/dev/null 2>&1; }

mode="unknown"
settings_valid=0
if [ ! -f "$SETTINGS" ]; then
  add 교정가능 "settings.json" "missing" "run setup.sh from the repo after backing up ~/.claude"
elif ! json_ok "$SETTINGS"; then
  add 수동필요 "settings.json" "invalid JSON" "repair JSON manually or restore from backup"
else
  settings_valid=1
  has_session=0; has_pretool=0
  json_has "$SETTINGS" '.hooks.SessionStart' && has_session=1
  json_has "$SETTINGS" '.hooks.PreToolUse' && has_pretool=1
  if [ "$has_session" = 1 ] && [ "$has_pretool" = 1 ]; then
    mode="standalone"
  elif [ "$has_session" = 1 ] && [ "$has_pretool" = 0 ]; then
    mode="plugin"
  elif [ "$has_session" = 0 ] && [ "$has_pretool" = 1 ]; then
    mode="ambiguous"
  fi
  add 정상 "settings.json" "valid JSON; mode: $mode" "none"
fi

if [ "$settings_valid" = 1 ]; then
  if json_has "$SETTINGS" '.outputStyle == "ccc-report"'; then
    add 정상 "outputStyle" "ccc-report" "none"
  else
    add 교정가능 "outputStyle" "missing or not ccc-report" "restore settings from claude/settings.base.json"
  fi

  sl_cmd="$(jq -r '.statusLine.command // empty' "$SETTINGS" 2>/dev/null)"
  if [ -z "$sl_cmd" ]; then
    add 교정가능 "statusLine" "missing" "restore settings statusLine wiring"
  elif [[ "$sl_cmd" == *statusline.sh* ]]; then
    add 정상 "statusLine" "$sl_cmd" "none"
  else
    add 교정가능 "statusLine" "unexpected command: $sl_cmd" "point statusLine at hooks/statusline.sh"
  fi

  for event in SessionStart PostCompact; do
    if json_has "$SETTINGS" ".hooks.$event"; then
      add 정상 "hook wiring $event" "present" "none"
    else
      add 교정가능 "hook wiring $event" "missing" "restore node-local hook wiring from settings.base.json"
    fi
  done

  if [ "$mode" = standalone ]; then
    for event in PreToolUse PostToolUse UserPromptSubmit Notification Stop SessionEnd; do
      if json_has "$SETTINGS" ".hooks.$event"; then
        add 정상 "portable hook $event" "settings-owned" "none"
      else
        add 교정가능 "portable hook $event" "missing in standalone settings" "merge enforcement-overlay.json into settings.json"
      fi
    done
  elif [ "$mode" = plugin ]; then
    add 정상 "portable hooks" "plugin-owned mode detected" "do not merge enforcement-overlay into settings.json"
  else
    add 수동필요 "install mode" "could not distinguish standalone vs plugin" "inspect settings.json/plugin ownership to avoid double-firing"
  fi
fi

for rel in \
  hooks/load-memory.sh hooks/load-tools.sh hooks/checkpoint.sh hooks/statusline.sh \
  hooks/guard.sh hooks/audit.sh hooks/redact.sh hooks/notify.sh hooks/evidence-gate.sh; do
  if [ -f "$CLAUDE_DIR/$rel" ]; then
    add 정상 "$rel" "installed" "none"
  else
    add 교정가능 "$rel" "missing" "run setup.sh after backup to reinstall harness files"
  fi
done

if [ -f "$CLAUDE_DIR/output-styles/ccc-report.md" ]; then
  add 정상 "output-styles/ccc-report.md" "installed" "none"
else
  add 교정가능 "output-styles/ccc-report.md" "missing" "run setup.sh after backup to reinstall output styles"
fi

if [ -f "$REPO/claude/hooks/enforcement-overlay.json" ] && [ -f "$REPO/claude/hooks/hooks.json" ]; then
  norm() { jq -S '.hooks | to_entries | map({event:.key, items:(.value|map({m:(.matcher//""), c:(.hooks|map(.command|capture("/(?<b>[A-Za-z0-9_.-]+\\.sh)").b // .)|sort)})|sort)})' "$1" 2>/dev/null; }
  if diff <(norm "$REPO/claude/hooks/enforcement-overlay.json") <(norm "$REPO/claude/hooks/hooks.json") >/dev/null 2>&1; then
    add 정상 "overlay/plugin parity" "equivalent" "none"
  else
    add 교정가능 "overlay/plugin parity" "diverged" "sync enforcement-overlay.json and hooks/hooks.json before release"
  fi
else
  add 경고 "overlay/plugin parity" "repo hook manifests unavailable" "run from a complete ccc-node checkout"
fi

if [ -x "$REPO/bridge/start.sh" ]; then
  if out="$({ "$REPO/bridge/start.sh" --path /root --status || true; } 2>&1 | tail -5)" && [ -n "$out" ]; then
    add 정상 "bridge status" "readable" "none"
  else
    add 경고 "bridge status" "no status output" "check bridge/start.sh manually if this node owns Telegram bridge"
  fi
else
  add 경고 "bridge status" "bridge/start.sh missing or not executable" "not all nodes run the Telegram bridge; install/check only if needed"
fi

printf '# ccc doctor\n\n'
printf -- '- repo: `%s`\n' "$REPO"
printf -- '- claude dir: `%s`\n' "$CLAUDE_DIR"
printf -- '- mode: `%s`\n\n' "$mode"
printf '## 진단 요약\n\n'
printf -- '- 정상: %s\n- 경고: %s\n- 교정가능: %s\n- 수동필요: %s\n\n' "$normal" "$warn" "$fixable" "$manual"
printf '| 분류 | 항목 | 상태 | 조치 |\n|---|---|---|---|\n'
for row in "${rows[@]}"; do
  IFS='|' read -r class item status action <<<"$row"
  printf '| %s | `%s` | %s | %s |\n' "$class" "$item" "$status" "$action"
done
printf '\n## 경계\n\n'
printf -- '- This command is read-only in the current slice.\n'
printf -- '- No remote nodes, secrets, broker/Gateway restarts, bridge restarts, migrations, or provider sends are touched.\n'
printf -- '- `--fix` is reserved for a later backup + dry-run + idempotent repair slice.\n'

if [ "$manual" -gt 0 ] || [ "$fixable" -gt 0 ]; then
  exit 1
fi
exit 0
