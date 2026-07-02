#!/usr/bin/env bash
# ccc doctor — harness consistency diagnostics and conservative repair.
#
# First repair slice for issue #52: `--fix` is dry-run by default; `--fix
# --apply` repairs only settings.json drift that is classified as 교정가능.
# Manual/risky/system-level items remain fail-closed.
set -uo pipefail

REPO="${CCC_DOCTOR_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
CLAUDE_DIR="${CCC_DOCTOR_CLAUDE_DIR:-${HOME:-/root}/.claude}"
SETTINGS="$CLAUDE_DIR/settings.json"
FIX=0
ROLLBACK=0
APPLY=0
SCOPE="settings"
while [ $# -gt 0 ]; do
  arg="$1"
  case "$arg" in
    --fix) FIX=1 ;;
    --rollback) ROLLBACK=1 ;;
    --apply|--write) APPLY=1 ;;
    --scope) [ -n "${2:-}" ] || { echo "--scope requires a value" >&2; exit 2; }; SCOPE="$2"; shift ;;
    --scope=*) SCOPE="${arg#--scope=}" ;;
    -h|--help)
      cat <<'EOF'
Usage: ccc-doctor.sh [--fix [--apply] [--scope=settings|files|hooks,output-styles]] [--rollback [--apply]]

Diagnostics classify checks as: 정상 / 경고 / 교정가능 / 수동필요.

Repair boundary:
- `--fix` is a dry-run plan and makes no filesystem changes.
- `--fix --apply` defaults to `--scope=settings` and writes only deterministic
  settings.json repairs for 교정가능 outputStyle/statusLine/hook wiring drift,
  after a backup tar is created.
- `--fix --apply --scope=files` reinstalls only allowlisted hook scripts and
  output-style files from the repo after a scoped backup. It refuses symlinks,
  path traversal, missing repo sources, and ambiguous/manual install modes.
- `--rollback` is a dry-run plan that selects the latest ccc-doctor settings backup.
- `--rollback --apply` restores only settings.json from that backup, after backing up
  the current settings.json as `ccc-doctor-pre-rollback-*.tar.gz`.
- 수동필요/risky/system-level items fail closed and are never auto-repaired.
EOF
      exit 0
      ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
  shift
done

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

HOOK_FILES=(
  hooks/load-memory.sh hooks/load-tools.sh hooks/checkpoint.sh hooks/statusline.sh
  hooks/guard.sh hooks/audit.sh hooks/redact.sh hooks/notify.sh hooks/evidence-gate.sh
)
OUTPUT_STYLE_FILES=(output-styles/ccc-report.md)

scope_has() {
  local want="$1" raw part
  raw=",$SCOPE,"
  case "$want" in
    settings) [[ "$raw" == *,settings,* ]] || [[ "$raw" == *,all,* ]] ;;
    hooks) [[ "$raw" == *,hooks,* ]] || [[ "$raw" == *,files,* ]] || [[ "$raw" == *,all,* ]] ;;
    output-styles) [[ "$raw" == *,output-styles,* ]] || [[ "$raw" == *,files,* ]] || [[ "$raw" == *,all,* ]] ;;
    *) return 1 ;;
  esac
}

valid_scope() {
  local rest="$SCOPE" part
  while [ -n "$rest" ]; do
    part="${rest%%,*}"
    [ "$part" = "$rest" ] && rest="" || rest="${rest#*,}"
    case "$part" in settings|files|hooks|output-styles|all) ;;
      *) return 1 ;;
    esac
  done
}
valid_scope || { echo "unsupported --scope: $SCOPE" >&2; exit 2; }

json_ok() { jq -e . "$1" >/dev/null 2>&1; }
json_has() { jq -e "$2" "$1" >/dev/null 2>&1; }

harness_version() {
  if [ -x "$REPO/scripts/ccc-version.sh" ]; then
    CCC_VERSION_REPO_DIR="$REPO" "$REPO/scripts/ccc-version.sh" 2>/dev/null || printf 'unknown\n'
  elif git -C "$REPO" describe --tags --dirty --always >/dev/null 2>&1; then
    git -C "$REPO" describe --tags --dirty --always
  else
    printf 'unknown\n'
  fi
}

mode="unknown"
settings_valid=0
if [ ! -f "$SETTINGS" ]; then
  add 수동필요 "settings.json" "missing" "run setup.sh from the repo after backing up ~/.claude; install mode cannot be inferred safely"
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

for rel in "${HOOK_FILES[@]}"; do
  src="$REPO/claude/$rel"
  dst="$CLAUDE_DIR/$rel"
  if [ ! -f "$dst" ]; then
    add 교정가능 "$rel" "missing" "run ccc-doctor --fix --apply --scope=files after backup to reinstall allowlisted harness files"
  elif [ -f "$src" ] && ! cmp -s "$src" "$dst"; then
    add 교정가능 "$rel" "drifted" "run ccc-doctor --fix --apply --scope=files after backup to reinstall allowlisted harness files"
  else
    add 정상 "$rel" "installed" "none"
  fi
done

for rel in "${OUTPUT_STYLE_FILES[@]}"; do
  src="$REPO/claude/$rel"
  dst="$CLAUDE_DIR/$rel"
  if [ ! -f "$dst" ]; then
    add 교정가능 "$rel" "missing" "run ccc-doctor --fix --apply --scope=files after backup to reinstall output styles"
  elif [ -f "$src" ] && ! cmp -s "$src" "$dst"; then
    add 교정가능 "$rel" "drifted" "run ccc-doctor --fix --apply --scope=files after backup to reinstall output styles"
  else
    add 정상 "$rel" "installed" "none"
  fi
done

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

if [ -x "$REPO/scripts/ccc-memory-check.sh" ]; then
  mem_json="$(CCC_STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}" CCC_MEMORY_CACHE_DIR="${CCC_MEMORY_CACHE_DIR:-$CLAUDE_DIR/hooks/cache}" "$REPO/scripts/ccc-memory-check.sh" --json 2>/dev/null || true)"
  if [ -n "$mem_json" ] && printf '%s' "$mem_json" | jq -e . >/dev/null 2>&1; then
    wiki_status="$(printf '%s' "$mem_json" | jq -r '.wiki.status // "unknown"')"
    honcho_status="$(printf '%s' "$mem_json" | jq -r '.honcho.status // "unknown"')"
    index_exists="$(printf '%s' "$mem_json" | jq -r '.local_index.exists // false')"
    if [ "$wiki_status" = ok ] && { [ "$honcho_status" = ok ] || [ "$honcho_status" = disabled ]; }; then
      add 정상 "memory cache" "wiki=$wiki_status; honcho=$honcho_status; local_index=$index_exists" "none"
    else
      add 경고 "memory cache" "wiki=$wiki_status; honcho=$honcho_status; local_index=$index_exists" "run scripts/ccc-memory-check.sh --json and inspect stale/missing cache metadata"
    fi
  else
    add 경고 "memory cache" "diagnostic unavailable" "run scripts/ccc-memory-check.sh manually"
  fi
else
  add 경고 "memory cache" "ccc-memory-check.sh missing" "complete checkout or reinstall scripts"
fi

print_report() {
  printf '# ccc doctor\n\n'
  printf -- '- repo: `%s`\n' "$REPO"
  printf -- '- harness version: `%s`\n' "$(harness_version)"
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
  printf -- '- Diagnostics are read-only unless `--fix --apply` or `--rollback --apply` is explicitly used.\n'
  printf -- '- `--fix` and `--rollback` alone are dry-run only.\n'
  printf -- '- No remote nodes, secrets, broker/Gateway restarts, bridge restarts, migrations, or provider sends are touched.\n'
}

settings_desired_tmp=""
make_settings_desired() {
  [ "$settings_valid" = 1 ] || return 1
  [ "$mode" = standalone ] || [ "$mode" = plugin ] || return 1
  [ -f "$REPO/claude/settings.base.json" ] || return 1
  settings_desired_tmp="$(mktemp)"
  if [ "$mode" = standalone ]; then
    [ -f "$REPO/claude/hooks/enforcement-overlay.json" ] || return 1
    jq -s '
      .[0] as $cur | .[1] as $base | .[2] as $overlay |
      $cur
      | .outputStyle = $base.outputStyle
      | .statusLine = $base.statusLine
      | .hooks.SessionStart = $base.hooks.SessionStart
      | .hooks.PostCompact = $base.hooks.PostCompact
      | .hooks.PreToolUse = $overlay.hooks.PreToolUse
      | .hooks.PostToolUse = $overlay.hooks.PostToolUse
      | .hooks.UserPromptSubmit = $overlay.hooks.UserPromptSubmit
      | .hooks.Notification = $overlay.hooks.Notification
      | .hooks.Stop = $overlay.hooks.Stop
      | .hooks.SessionEnd = $overlay.hooks.SessionEnd
    ' "$SETTINGS" "$REPO/claude/settings.base.json" "$REPO/claude/hooks/enforcement-overlay.json" > "$settings_desired_tmp" 2>/dev/null
  else
    jq -s '
      .[0] as $cur | .[1] as $base |
      $cur
      | .outputStyle = $base.outputStyle
      | .statusLine = $base.statusLine
      | .hooks.SessionStart = $base.hooks.SessionStart
      | .hooks.PostCompact = $base.hooks.PostCompact
    ' "$SETTINGS" "$REPO/claude/settings.base.json" > "$settings_desired_tmp" 2>/dev/null
  fi
}

settings_needs_repair() {
  make_settings_desired || return 1
  ! diff <(jq -S . "$SETTINGS") <(jq -S . "$settings_desired_tmp") >/dev/null 2>&1
}

apply_settings_repair() {
  settings_needs_repair || { rm -f "${settings_desired_tmp:-}"; return 1; }
  local ts archive
  ts="$(date +%Y%m%d-%H%M%S)"
  archive="$CLAUDE_DIR/backups/ccc-doctor-$ts.tar.gz"
  mkdir -p "$CLAUDE_DIR/backups"
  if ! tar -czf "$archive" -C "$CLAUDE_DIR" settings.json \
    || ! validate_settings_backup "$archive"; then
    rm -f "${settings_desired_tmp:-}"
    printf 'failed to create valid settings backup: %s\n' "$archive" >&2
    return 1
  fi
  mv "$settings_desired_tmp" "$SETTINGS"
  printf 'applied settings.json repair; backup=%s\n' "$archive"
}

latest_rollback_backup() {
  local backup_dir="$CLAUDE_DIR/backups"
  [ -d "$backup_dir" ] || return 1
  find "$backup_dir" -maxdepth 1 -type f -name 'ccc-doctor-[0-9]*.tar.gz' -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr | head -1 | cut -d' ' -f2-
}

validate_settings_backup() {
  local archive="$1"
  [ -n "$archive" ] && [ -f "$archive" ] || return 1
  tar -tzf "$archive" settings.json >/dev/null 2>&1 || return 1
}

apply_settings_rollback() {
  local archive="$1" ts pre_archive
  validate_settings_backup "$archive" || return 1
  ts="$(date +%Y%m%d-%H%M%S)"
  pre_archive="$CLAUDE_DIR/backups/ccc-doctor-pre-rollback-$ts.tar.gz"
  mkdir -p "$CLAUDE_DIR/backups"
  if [ -f "$SETTINGS" ]; then
    tar -czf "$pre_archive" -C "$CLAUDE_DIR" settings.json
  fi
  tar -xzf "$archive" -C "$CLAUDE_DIR" settings.json
  printf 'applied settings.json rollback; restored=%s; preRollbackBackup=%s\n' "$archive" "$pre_archive"
}

file_repair_list() {
  local rel src dst
  if scope_has hooks; then
    for rel in "${HOOK_FILES[@]}"; do
      src="$REPO/claude/$rel"; dst="$CLAUDE_DIR/$rel"
      if [ ! -f "$dst" ] || { [ -f "$src" ] && ! cmp -s "$src" "$dst"; }; then
        printf '%s\n' "$rel"
      fi
    done
  fi
  if scope_has output-styles; then
    for rel in "${OUTPUT_STYLE_FILES[@]}"; do
      src="$REPO/claude/$rel"; dst="$CLAUDE_DIR/$rel"
      if [ ! -f "$dst" ] || { [ -f "$src" ] && ! cmp -s "$src" "$dst"; }; then
        printf '%s\n' "$rel"
      fi
    done
  fi
}

is_path_under() { # <path> <root>
  local path root
  path="$(realpath -m "$1")"
  root="$(realpath -m "$2")"
  [[ "$path" == "$root" || "$path" == "$root"/* ]]
}

validate_file_repair_target() { # <rel>
  local rel="$1" src="$REPO/claude/$1" dst="$CLAUDE_DIR/$1" parent
  case "$rel" in hooks/*|output-styles/*) ;;
    *) printf 'unsupported repair target: %s\n' "$rel" >&2; return 1 ;;
  esac
  [ -f "$src" ] || { printf 'source file missing: %s\n' "$src" >&2; return 1; }
  [ ! -L "$src" ] || { printf 'source symlink refused: %s\n' "$src" >&2; return 1; }
  parent="$(dirname "$dst")"
  if [ -L "$parent" ]; then
    printf 'destination parent symlink refused: %s\n' "$parent" >&2; return 1
  fi
  if [ -L "$dst" ]; then
    printf 'destination symlink refused: %s\n' "$dst" >&2; return 1
  fi
  case "$rel" in
    hooks/*) is_path_under "$dst" "$CLAUDE_DIR/hooks" || { printf 'destination escapes hooks dir: %s\n' "$dst" >&2; return 1; } ;;
    output-styles/*) is_path_under "$dst" "$CLAUDE_DIR/output-styles" || { printf 'destination escapes output-styles dir: %s\n' "$dst" >&2; return 1; } ;;
  esac
}

backup_file_repairs() { # <list-file>
  local list_file="$1" ts archive existing
  ts="$(date +%Y%m%d-%H%M%S)"
  archive="$CLAUDE_DIR/backups/ccc-doctor-files-$ts.tar.gz"
  mkdir -p "$CLAUDE_DIR/backups"
  existing="$(mktemp)"
  while IFS= read -r rel; do
    [ -e "$CLAUDE_DIR/$rel" ] && printf '%s\n' "$rel" >> "$existing"
  done < "$list_file"
  if [ -s "$existing" ]; then
    tar -czf "$archive" -C "$CLAUDE_DIR" -T "$existing"
  else
    tar -czf "$archive" -C "$CLAUDE_DIR" --files-from /dev/null --warning=no-file-changed 2>/dev/null || :
    # GNU tar refuses empty archives on some systems; a manifest still records the guarded apply.
    printf 'no pre-existing files for scoped repair\n' > "$CLAUDE_DIR/backups/ccc-doctor-files-$ts.manifest.txt"
  fi
  rm -f "$existing"
  printf '%s\n' "$archive"
}

apply_file_repairs() {
  local list_file rel archive dst src
  list_file="$(mktemp)"
  file_repair_list > "$list_file"
  if [ ! -s "$list_file" ]; then
    rm -f "$list_file"
    return 1
  fi
  if [ "$mode" != standalone ]; then
    rm -f "$list_file"
    printf 'install mode is %s; refusing scoped file repair to avoid plugin/standalone double-firing.\n' "$mode" >&2
    return 2
  fi
  while IFS= read -r rel; do
    validate_file_repair_target "$rel" || { rm -f "$list_file"; return 2; }
  done < "$list_file"
  archive="$(backup_file_repairs "$list_file")"
  while IFS= read -r rel; do
    src="$REPO/claude/$rel"; dst="$CLAUDE_DIR/$rel"
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    chmod --reference="$src" "$dst" 2>/dev/null || true
  done < "$list_file"
  printf 'applied scoped file repair; backup=%s; repaired=%s\n' "$archive" "$(paste -sd, "$list_file")"
  rm -f "$list_file"
}

if [ "$ROLLBACK" = 1 ]; then
  printf '# ccc doctor --rollback\n\n'
  archive="$(latest_rollback_backup || true)"
  if ! validate_settings_backup "$archive"; then
    printf 'no rollback backup found; refusing automatic rollback.\n' >&2
    exit 1
  fi
  if [ "$APPLY" = 1 ]; then
    apply_settings_rollback "$archive" || { printf 'rollback backup is invalid; refusing automatic rollback.\n' >&2; exit 1; }
    exit 0
  fi
  printf 'dry-run: would restore settings.json from %s. Re-run with `--rollback --apply` to write after pre-rollback backup.\n' "$archive"
  exit 1
fi

if [ "$FIX" = 1 ]; then
  printf '# ccc doctor --fix\n\n'
  if [ "$manual" -gt 0 ]; then
    printf 'manual items present; refusing automatic repair.\n' >&2
    print_report
    exit 1
  fi

  settings_needed=0
  files_needed=0
  file_repairs_tmp="$(mktemp)"
  if scope_has settings && settings_needs_repair; then
    settings_needed=1
  fi
  if { scope_has hooks || scope_has output-styles; } && file_repair_list > "$file_repairs_tmp" && [ -s "$file_repairs_tmp" ]; then
    files_needed=1
  fi

  if [ "$settings_needed" = 1 ] || [ "$files_needed" = 1 ]; then
    if [ "$APPLY" = 1 ]; then
      if [ "$settings_needed" = 1 ]; then
        apply_settings_repair || exit 1
      fi
      if [ "$files_needed" = 1 ]; then
        apply_file_repairs || exit 1
      fi
      rm -f "$file_repairs_tmp" "${settings_desired_tmp:-}"
      exit 0
    fi
    [ "$settings_needed" = 1 ] && printf 'dry-run: would repair settings.json from canonical repo templates. Re-run with `--fix --apply` to write after backup.\n'
    if [ "$files_needed" = 1 ]; then
      printf 'dry-run: would reinstall scoped files from canonical repo templates: %s. Re-run with `--fix --apply --scope=%s` to write after backup.\n' "$(paste -sd, "$file_repairs_tmp")" "$SCOPE"
    fi
    rm -f "$file_repairs_tmp" "${settings_desired_tmp:-}"
    exit 1
  fi

  if { scope_has hooks || scope_has output-styles; } && [ "$APPLY" != 1 ]; then
    printf 'no scoped file repairs needed.\n'
  else
    printf 'no repairs needed.\n'
  fi
  rm -f "$file_repairs_tmp" "${settings_desired_tmp:-}"
  exit 0
fi

print_report
if [ "$manual" -gt 0 ] || [ "$fixable" -gt 0 ]; then
  exit 1
fi
exit 0
