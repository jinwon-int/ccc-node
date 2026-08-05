#!/usr/bin/env bash
# install-nunchi.sh (#816) — opt-in enable/disable for the nunchi memory
# shadow pilot. The hook tree (claude/hooks/nunchi/) is deployed to every
# node by setup.sh but is a no-op until state/nunchi.mode is "on".
#
#   install-nunchi.sh --apply              # auto-detect live provider
#   install-nunchi.sh --apply --codex      # explicit Codex override
#   install-nunchi.sh --apply --claude     # explicit Claude override
#   install-nunchi.sh --apply --piri       # explicit Piri override
#   install-nunchi.sh --apply --target-user gongmyoung
#   install-nunchi.sh --remove             # mode off + managed cron/hook removal
#   install-nunchi.sh                      # status
#
# Claude retains the standalone SessionStart hook and reuses the Session
# Distiller output (zero LLM cost). Codex and Piri keep supplementary nunchi
# per-new-session extractors (codex exec / Piri print mode); the main bridge
# distill journal separately owns replay-safe memory sinks.
# Provider changes remove the other path so one runtime never injects the same
# node-global snapshot twice.
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
    --piri) PROVIDER="piri" ;;
    --target-user)
      [ $# -ge 2 ] || { echo "--target-user requires a user" >&2; exit 2; }
      TARGET_USER="$2"; shift ;;
    --help|-h)
      sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$PROVIDER" in
  codex|claude|piri|auto) ;;
  *) echo "invalid provider: $PROVIDER" >&2; exit 2 ;;
esac

# A bridge may run as a non-root service user even when the operator connects
# as root. Re-exec with a minimal environment as that user so HOME, crontab,
# DB ownership and the actual runtime's hook tree stay in one scope.
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "$(id -un)" ]; then
  [ "$(id -u)" = 0 ] || { echo "--target-user requires root when switching users" >&2; exit 2; }
  case "$TARGET_USER" in
    -*|*[!A-Za-z0-9_.-]*) echo "invalid target user" >&2; exit 2 ;;
  esac
  command -v getent >/dev/null 2>&1 || { echo "getent required for --target-user" >&2; exit 2; }
  command -v runuser >/dev/null 2>&1 || { echo "runuser required for --target-user" >&2; exit 2; }
  passwd_row="$(getent passwd "$TARGET_USER" || true)"
  target_uid="$(awk -F: 'NR == 1 {print $3}' <<<"$passwd_row")"
  target_home="${CCC_NUNCHI_TARGET_HOME:-$(awk -F: 'NR == 1 {print $6}' <<<"$passwd_row")}"
  [ -n "$target_home" ] && [ "$target_home" != "/" ] && [ -d "$target_home" ] \
    && [ -n "$target_uid" ] && [ "$(stat -c %u -- "$target_home")" = "$target_uid" ] \
    || { echo "safe target home not found for $TARGET_USER" >&2; exit 2; }
  target_claude_dir="${CCC_CLAUDE_DIR:-$target_home/.claude}"
  target_codex_home="${CODEX_HOME:-$target_home/.codex}"
  target_state_dir="${CCC_STATE_DIR:-$target_claude_dir/state}"
  target_nunchi_home="${NUNCHI_HOME:-$target_home/.nunchi}"
  target_status="${CCC_NUNCHI_MEMPALACE_STATUS:-$target_nunchi_home/mempalace-refresh.status.json}"
  exec runuser -u "$TARGET_USER" -- env -i \
    HOME="$target_home" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    CCC_NUNCHI_TARGET_USER="$TARGET_USER" CCC_NUNCHI_PROVIDER="$PROVIDER" \
    CCC_CLAUDE_DIR="$target_claude_dir" CODEX_HOME="$target_codex_home" CCC_STATE_DIR="$target_state_dir" \
    NUNCHI_HOME="$target_nunchi_home" NUNCHI_DB="${NUNCHI_DB:-$target_nunchi_home/facts.db}" \
    NUNCHI_SNAPSHOT="${NUNCHI_SNAPSHOT:-$target_nunchi_home/snapshot.md}" \
    CCC_NUNCHI_MEMPALACE_STATUS="$target_status" \
    CCC_NUNCHI_MEMPALACE_CLI="${CCC_NUNCHI_MEMPALACE_CLI:-}" \
    CCC_NUNCHI_TIMEOUT_CLI="${CCC_NUNCHI_TIMEOUT_CLI:-}" \
    CCC_NUNCHI_FLOCK_CLI="${CCC_NUNCHI_FLOCK_CLI:-}" \
    CCC_NUNCHI_MEMPALACE_REFRESH_TIMEOUT_SEC="${CCC_NUNCHI_MEMPALACE_REFRESH_TIMEOUT_SEC:-3300}" \
    NUNCHI_SWEEP_DIR="${NUNCHI_SWEEP_DIR:-}" \
    bash "$0" "${ORIGINAL_ARGS[@]}"
fi

CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
STATE="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
HOOKS="$CLAUDE_DIR/hooks/nunchi"
NUNCHI_DIR="${NUNCHI_HOME:-$HOME/.nunchi}"
NUNCHI_DB_PATH="${NUNCHI_DB:-$NUNCHI_DIR/facts.db}"
NUNCHI_SNAPSHOT_PATH="${NUNCHI_SNAPSHOT:-$NUNCHI_DIR/snapshot.md}"
MEMPALACE_STATUS="${CCC_NUNCHI_MEMPALACE_STATUS:-$NUNCHI_DIR/mempalace-refresh.status.json}"
MEMPALACE_TIMEOUT="${CCC_NUNCHI_MEMPALACE_REFRESH_TIMEOUT_SEC:-3300}"
MODE_FILE="$STATE/nunchi.mode"
MARK="# nunchi:#816"
TS="$(date +%Y%m%dT%H%M%S)"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"

validate_codex_loader() {
  python3 - "$HOOKS/codex-loader.py" <<'PY'
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
try:
    meta = path.lstat()
except OSError:
    raise SystemExit(1)
safe = (
    stat.S_ISREG(meta.st_mode)
    and meta.st_nlink == 1
    and meta.st_uid in {0, os.geteuid()}
    and not stat.S_IMODE(meta.st_mode) & 0o022
    and 0 < meta.st_size <= 1024 * 1024
)
raise SystemExit(0 if safe else 1)
PY
}

# Body-free status renderers for the enriched nunchi status (#865). These
# report provider wiring, source, binary, and the last collection result
# without ever printing transcript body, excerpts, session ids or credentials.
_status_provider_match() {  # <configured> <runtime>
  local configured="$1" runtime="$2"
  [ "$configured" = none ] && { printf 'n/a'; return; }
  [ "$runtime" = auto ] && { printf 'auto'; return; }
  [ "$configured" = "$runtime" ] && printf 'ok' || printf 'DRIFT'
}

_status_source() {  # <cron-text> -> "kind=<mine|sweep> path=<dir>" or "none"
  python3 - "$1" <<'PY'
import re, sys
cron = sys.argv[1]
m = re.search(r'mempalace-refresh\.sh\s+(\w+)\s+(.+?)\s*>>', cron)
if not m:
    print("none")
else:
    provider, path = m.group(1), m.group(2).strip()
    kind = "mine" if provider in ("codex", "piri") else "sweep"
    if len(path) >= 2 and path[0] in "\"'" and path[-1] == path[0]:
        path = path[1:-1]  # strip a single layer of cron shell-quoting (path only)
    print(f"kind={kind} path={path}")
PY
}

_status_collection() {  # <status-file> -> "state=<s> exit_code=<n> finished_at=<ts>" or "none"
  python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as handle:
        d = json.load(handle)
    print(f"state={d.get('state', '?')} exit_code={d.get('exit_code', '?')} "
          f"finished_at={d.get('finished_at', d.get('started_at', '?'))}")
except Exception:
    print("none")
PY
}

status() {
  local cron configured="none" runtime_provider mp_path mp_ver
  cron="$("$CRONTAB" -l 2>/dev/null || true)"
  runtime_provider="${CCC_AGENT_PROVIDER:-auto}"
  grep -q 'codex-feed.sh'  <<<"$cron" && configured="codex"
  grep -q 'ingest-cron.sh' <<<"$cron" && configured="claude"
  grep -q 'piri-feed.sh'   <<<"$cron" && configured="piri"
  mp_path="${CCC_NUNCHI_MEMPALACE_CLI:-$(command -v mempalace || true)}"
  [ -z "$mp_path" ] && [ -x "$HOME/.local/bin/mempalace" ] && mp_path="$HOME/.local/bin/mempalace"
  mp_ver="none"
  if [ -n "$mp_path" ] && [ -x "$mp_path" ]; then
    mp_ver="$("$mp_path" --version 2>/dev/null | tail -1 | tr -d '\r' || true)"
    [ -n "$mp_ver" ] || mp_ver="unknown"
  fi
  echo "runtime: user=$(id -un) home=$HOME"
  echo "provider: configured=$configured runtime=$runtime_provider match=$(_status_provider_match "$configured" "$runtime_provider")"
  echo "source: $(_status_source "$cron")"
  echo "mempalace: binary=${mp_path:-missing} version=$mp_ver"
  echo "collection: $(_status_collection "$MEMPALACE_STATUS")"
  echo "mode: $(cat "$MODE_FILE" 2>/dev/null || echo off)"
  echo "hooks: $([ -f "$HOOKS/nunchi.py" ] && echo present || echo MISSING) ($HOOKS)"
  echo "codex_loader: $(if validate_codex_loader; then echo present; else echo MISSING/UNSAFE; fi)"
  echo "cron: $(grep -cF "$MARK" <<<"$cron" || true) line(s)"
  echo "db: $(ls -la "$NUNCHI_DIR/facts.db" 2>/dev/null | awk '{print $5" bytes"}' || echo none)"
}

write_mode() {  # atomic owner-local marker; replacing a stale link never follows it
  local value="$1" tmp
  mkdir -p "$STATE"
  tmp="$(mktemp "$STATE/.nunchi.mode.XXXXXX")"
  chmod 600 "$tmp"
  printf '%s' "$value" > "$tmp"
  mv -f -- "$tmp" "$MODE_FILE"
}

strip_cron() {  # remove managed and legacy hand-deploy nunchi lines
  local tmp
  tmp="$(mktemp)"
  "$CRONTAB" -l 2>/dev/null \
    | grep -vF "$MARK" \
    | grep -v "nunchi/ingest-cron.sh\|nunchi/codex-feed.sh\|/nunchi/ingest-cron\|nunchi:distill-mirror\|nunchi:codex-feed" \
    > "$tmp" || true
  "$CRONTAB" "$tmp"
  rm -f "$tmp"
}

cron_quote() {  # shell quote one argv value, then protect cron's special '%'
  python3 - "$1" <<'PY'
import shlex
import sys

print(shlex.quote(sys.argv[1]).replace("%", r"\%"))
PY
}

append_cron_line() {  # <literal cron line>
  local line="$1" tmp
  tmp="$(mktemp)"
  "$CRONTAB" -l > "$tmp" 2>/dev/null || true
  printf '%s\n' "$line" >> "$tmp"
  "$CRONTAB" "$tmp"
  rm -f "$tmp"
}

retire_legacy() {
  local legacy="$HOME/nunchi"
  if [ -f "$legacy/nunchi.py" ] && [ "$legacy" != "$HOOKS" ]; then
    mv "$legacy" "$legacy.retired-$TS"
    echo "legacy hand-deploy retired: $legacy -> $legacy.retired-$TS (DB untouched)"
  fi
}

set_sessionstart_hook() {  # add|remove the standalone Claude-only hook
  python3 - "$CLAUDE_DIR/settings.local.json" "$HOOKS/sessionstart.sh" "$1" <<'PY'
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile

path = Path(sys.argv[1])
script = sys.argv[2]
action = sys.argv[3]
if action == "remove" and not path.exists():
    raise SystemExit(0)
data = {}
mode = 0o600
if path.exists():
    mode = stat.S_IMODE(path.stat().st_mode)
    with path.open() as handle:
        data = json.load(handle)
session = data.setdefault("hooks", {}).setdefault("SessionStart", [])
kept = []
for group in session:
    hooks = []
    for hook in group.get("hooks", []):
        if "nunchi/sessionstart.sh" not in str(hook.get("command") or ""):
            hooks.append(hook)
    if hooks:
        copy = dict(group)
        copy["hooks"] = hooks
        kept.append(copy)
if action == "add":
    kept.append({"hooks": [{"type": "command", "command": f"bash {shlex.quote(script)}", "timeout": 5}]})
data["hooks"]["SessionStart"] = kept
path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(tmp_name, mode)
    os.replace(tmp_name, path)
finally:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass
PY
}

detect_provider() {
  if [ "$PROVIDER" != "auto" ]; then printf '%s' "$PROVIDER"; return; fi
  local root bridge_status=""
  root="$(cd "$(dirname "$0")/.." && pwd)"
  if [ -f "$root/bridge/start.sh" ]; then
    bridge_status="$(HOME="$HOME" bash "$root/bridge/start.sh" --path "$HOME" --status 2>/dev/null || true)"
  fi
  if grep -q 'Piri: healthy' <<<"$bridge_status"; then printf 'piri'; return; fi
  if grep -q 'Codex: healthy' <<<"$bridge_status"; then printf 'codex'; return; fi
  if grep -q 'Claude: healthy' <<<"$bridge_status"; then printf 'claude'; return; fi
  if [ -d "$CODEX_HOME_DIR/sessions" ] && [ ! -d "$CLAUDE_DIR/projects" ]; then
    printf 'codex'
  else
    printf 'claude'
  fi
}

case "$ACTION" in
  apply)
    [ -f "$HOOKS/nunchi.py" ] || { echo "hooks missing at $HOOKS — run setup.sh first" >&2; exit 2; }
    mkdir -p "$NUNCHI_DIR"; chmod 700 "$NUNCHI_DIR"  # owner-only: logs/state stay private (#865)
    resolved_provider="$(detect_provider)"
    if [ "$resolved_provider" = "codex" ] && ! validate_codex_loader; then
      echo "Codex nunchi loader missing or unsafe at $HOOKS/codex-loader.py — run setup.sh first" >&2
      exit 2
    fi
    feed="$HOOKS/ingest-cron.sh"
    [ "$resolved_provider" = "codex" ] && feed="$HOOKS/codex-feed.sh"
    [ "$resolved_provider" = "piri" ]  && feed="$HOOKS/piri-feed.sh"
    bash_bin="$(command -v bash)"
    mp="${CCC_NUNCHI_MEMPALACE_CLI:-}"
    [ -n "$mp" ] || mp="$(command -v mempalace || true)"
    [ -z "$mp" ] && [ -x "$HOME/.local/bin/mempalace" ] && mp="$HOME/.local/bin/mempalace"
    default_sweep="$CLAUDE_DIR/projects"
    [ "$resolved_provider" = "codex" ] && default_sweep="$CODEX_HOME_DIR/sessions"
    [ "$resolved_provider" = "piri" ]  && default_sweep="$HOME/.piri/agent/sessions"
    sweep_dir="${NUNCHI_SWEEP_DIR:-$default_sweep}"
    refresh="$HOOKS/mempalace-refresh.sh"
    refresh_ready=0
    if [ -n "$mp" ] && [ -f "$mp" ] && [ -x "$mp" ] && [ -d "$sweep_dir" ] && [ -x "$refresh" ]; then
      timeout_bin="${CCC_NUNCHI_TIMEOUT_CLI:-$(command -v timeout || true)}"
      flock_bin="${CCC_NUNCHI_FLOCK_CLI:-$(command -v flock || true)}"
      [ -n "$timeout_bin" ] && [ -f "$timeout_bin" ] && [ -x "$timeout_bin" ] \
        || { echo "timeout command missing or unsafe — nunchi not enabled" >&2; exit 2; }
      [ -n "$flock_bin" ] && [ -f "$flock_bin" ] && [ -x "$flock_bin" ] \
        || { echo "flock command missing or unsafe — nunchi not enabled" >&2; exit 2; }
      refresh_ready=1
    fi
    retire_legacy
    strip_cron
    write_mode on
    append_cron_line "*/10 * * * * CCC_STATE_DIR=$(cron_quote "$STATE") NUNCHI_HOME=$(cron_quote "$NUNCHI_DIR") NUNCHI_DB=$(cron_quote "$NUNCHI_DB_PATH") NUNCHI_SNAPSHOT=$(cron_quote "$NUNCHI_SNAPSHOT_PATH") $(cron_quote "$bash_bin") $(cron_quote "$feed") >> $(cron_quote "$NUNCHI_DIR/cron.log") 2>&1 $MARK"
    if [ "$refresh_ready" = 1 ]; then
      append_cron_line "17 * * * * CCC_STATE_DIR=$(cron_quote "$STATE") NUNCHI_HOME=$(cron_quote "$NUNCHI_DIR") CCC_NUNCHI_MEMPALACE_STATUS=$(cron_quote "$MEMPALACE_STATUS") CCC_NUNCHI_MEMPALACE_REFRESH_TIMEOUT_SEC=$(cron_quote "$MEMPALACE_TIMEOUT") CCC_NUNCHI_MEMPALACE_CLI=$(cron_quote "$mp") CCC_NUNCHI_TIMEOUT_CLI=$(cron_quote "$timeout_bin") CCC_NUNCHI_FLOCK_CLI=$(cron_quote "$flock_bin") $(cron_quote "$bash_bin") $(cron_quote "$refresh") $resolved_provider $(cron_quote "$sweep_dir") >> $(cron_quote "$NUNCHI_DIR/mempalace-sweep.cron.log") 2>&1 $MARK"
      echo "mempalace hourly refresh cron added ($resolved_provider: $sweep_dir)"
    else
      echo "mempalace CLI, refresh hook or transcript dir missing — verbatim refresh cron skipped"
    fi
    append_cron_line "7 8 * * 1 CCC_STATE_DIR=$(cron_quote "$STATE") NUNCHI_HOME=$(cron_quote "$NUNCHI_DIR") NUNCHI_DB=$(cron_quote "$NUNCHI_DB_PATH") NUNCHI_SNAPSHOT=$(cron_quote "$NUNCHI_SNAPSHOT_PATH") $(cron_quote "$bash_bin") $(cron_quote "$HOOKS/bench.sh") >> $(cron_quote "$NUNCHI_DIR/bench.cron.log") 2>&1 $MARK"
    echo "weekly bench cron added (Mon 08:07)"
    if [ "$resolved_provider" = "claude" ]; then
      set_sessionstart_hook add
    else
      set_sessionstart_hook remove
    fi
    python3 "$HOOKS/nunchi.py" init
    echo "nunchi enabled (mode=on, provider=$resolved_provider, feed=$(basename "$feed"))"
    status
    ;;
  remove)
    write_mode off
    strip_cron
    set_sessionstart_hook remove
    echo "nunchi disabled (code and ~/.nunchi DB kept)"
    status
    ;;
  *) status ;;
esac
