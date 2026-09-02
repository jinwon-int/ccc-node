#!/usr/bin/env bash
# install-nunchi.sh (#816) — opt-in enable/disable for the nunchi memory
# shadow pilot. The hook tree (claude/hooks/nunchi/) is deployed to every
# node by setup.sh but is a no-op until state/nunchi.mode is "on".
#
#   install-nunchi.sh --apply              # auto-detect live provider
#   install-nunchi.sh --apply --codex      # explicit Codex override
#   install-nunchi.sh --apply --claude     # explicit Claude override
#   install-nunchi.sh --apply --piri       # explicit Piri override
#   install-nunchi.sh --apply --piri --audience-scoped /absolute/audience/root
#   install-nunchi.sh --apply --judge    # + daily review-queue judge batch (#1204)
#   install-nunchi.sh --apply --judge-apply  # judge batch in APPLY mode — MUTATES
#                                            # the fact store; implies --judge and
#                                            # needs fresh per-node approval (#1264)
#   install-nunchi.sh --apply --target-user gongmyoung
#   install-nunchi.sh --remove             # mode off + managed cron/hook removal
#   install-nunchi.sh                      # status
#
# Claude retains the standalone SessionStart hook and reuses the Session
# Distiller output (zero LLM cost). Codex and Piri keep supplementary nunchi
# per-new-session extractors (codex exec / Piri print mode); the main bridge
# distill journal separately owns replay-safe memory sinks.
# Provider changes remove the other path so one runtime never injects the same
# node-global snapshot twice. Managed cron lines carry a `gen=h_<sha256:12>`
# stamp of this script's content (#1081) so ccc-doctor can tell when the
# installed entries were rendered by an older installer.
set -euo pipefail

ACTION="status"
PROVIDER="${CCC_NUNCHI_PROVIDER:-auto}"
TARGET_USER="${CCC_NUNCHI_TARGET_USER:-}"
JUDGE="${CCC_NUNCHI_JUDGE:-0}"
JUDGE_APPLY="${CCC_NUNCHI_JUDGE_APPLY:-0}"
AUDIENCE_SCOPED="${CCC_NUNCHI_AUDIENCE_SCOPED:-0}"
AUDIENCE_ROOT="${CCC_NUNCHI_AUDIENCE_ROOT:-}"
ORIGINAL_ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) ACTION="apply" ;;
    --remove) ACTION="remove" ;;
    --codex) PROVIDER="codex" ;;
    --claude) PROVIDER="claude" ;;
    --piri) PROVIDER="piri" ;;
    --audience-scoped)
      [ $# -ge 2 ] || { echo "--audience-scoped requires an absolute root" >&2; exit 2; }
      AUDIENCE_SCOPED=1; AUDIENCE_ROOT="$2"; shift ;;
    --target-user)
      [ $# -ge 2 ] || { echo "--target-user requires a user" >&2; exit 2; }
      TARGET_USER="$2"; shift ;;
    --judge) JUDGE=1 ;;
    # Implies --judge: an apply-mode cron with no judge cron is not a state
    # the installer can express, so asking for one is asking for both.
    --judge-apply) JUDGE=1; JUDGE_APPLY=1 ;;
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
    CCC_NUNCHI_AUDIENCE_SCOPED="$AUDIENCE_SCOPED" \
    CCC_NUNCHI_AUDIENCE_ROOT="$AUDIENCE_ROOT" \
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

# Generation stamp (#1081): content hash of this script, pinned into every
# managed cron line so ccc-doctor can tell when the installed entries were
# rendered by an older installer (#996 sat frozen for 4 days). Appended after
# "$MARK", which strip_cron matches by substring, so removal keeps working.
NUNCHI_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN_STAMP_LIB="$NUNCHI_SELF_DIR/lib/installer-gen-stamp.sh"
if [ ! -r "$GEN_STAMP_LIB" ]; then
  echo "shared gen-stamp library is missing: $GEN_STAMP_LIB" >&2
  exit 4
fi
# shellcheck source=/dev/null
. "$GEN_STAMP_LIB"
GEN="$(ccc_installer_gen_stamp "$NUNCHI_SELF_DIR/install-nunchi.sh")"

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

# #1263 — a node that ships claude/codex binaries was never checked for
# whether either is actually *authenticated*. `--apply` used to succeed
# silently on a node where both are logged out (gongmyoung: codex 401 +
# claude not installed): ingest still worked, but dialectic/bench synthesis
# was dead from minute one with no signal anywhere that it needed attention.
# This is a diagnostic-only probe — it never blocks --apply, since ingest is
# a real, independent value even with zero working synthesis backend.
any_backend_authenticated() {
  if command -v claude >/dev/null 2>&1; then
    if claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
      return 0
    fi
  fi
  if command -v codex >/dev/null 2>&1; then
    # Anchored: "Not logged in" contains the unanchored substring "logged in"
    # too, so a bare `grep -qi 'logged in'` reports a dead codex as healthy.
    # 2>&1, not 2>/dev/null: `codex login status` prints its verdict on STDERR
    # (measured 2026-08-25 on gwakga — stdout is empty, stderr carries
    # "Logged in using ChatGPT"), so discarding stderr made an authenticated
    # codex read as unauthenticated on every node. The anchor is what keeps
    # the merge safe: an error message on stderr cannot start with "logged in".
    if codex login status 2>&1 | grep -qi '^logged in'; then
      return 0
    fi
  fi
  return 1
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

# Audience-scoped collection (#985): in scoped mode the dispatcher writes one
# status file per scope (<root>/<scope>/nunchi/mempalace-refresh.status.json)
# and the node-global file goes stale, so status must aggregate per-scope.
# Scope boundary rules mirror the dispatcher walk in mempalace-refresh.sh
# (opaque canonical names, owner-only dirs, symlink rejection, 64 scope cap).
# Scope labels stay body-free: "shared" in full, private scopes truncated to
# the same "private-a9d7…" shape the issue report uses.
_status_collection_scoped() {  # <audience-root> -> worst-first per-scope rows or "none"
  python3 - "$1" <<'PY'
import json
import os
from pathlib import Path
import re
import stat
import sys

root = Path(sys.argv[1])
try:
    limit = int(os.environ.get("CCC_NUNCHI_MAX_SCOPES_PER_RUN", "64"))
except ValueError:
    limit = 64
if limit < 1 or limit > 64:
    limit = 64

def safe_dir(path):
    try:
        meta = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(meta.st_mode)
        and meta.st_uid == os.geteuid()
        and not stat.S_IMODE(meta.st_mode) & 0o077
    )

entries = []
if root.is_absolute() and safe_dir(root):
    count = 0
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if count >= limit:
            break
        if child.name != "shared" and not re.fullmatch(r"private-[0-9a-f]{32}", child.name):
            continue
        if not safe_dir(child):
            continue
        count += 1
        try:
            with (child / "nunchi" / "mempalace-refresh.status.json").open() as handle:
                data = json.load(handle)
        except Exception:
            continue
        label = child.name if child.name == "shared" else f"{child.name[:12]}\u2026"
        try:
            ts = float(data.get("finished_at") or data.get("started_at") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        body = (f"state={data.get('state', '?')} exit_code={data.get('exit_code', '?')} "
                f"finished_at={data.get('finished_at', data.get('started_at', '?'))}")
        entries.append((0 if data.get("state") != "ok" else 1, ts, label, body))

if not entries:
    print("none")
else:
    entries.sort(key=lambda item: (item[0], item[1]))
    shown = [f"{label}({body})" for _rank, _ts, label, body in entries[:8]]
    if len(entries) > 8:
        shown.append(f"(+{len(entries) - 8} more)")
    print(" ".join(shown))
PY
}

_status_audience() {  # <cron-text> -> body-free "enabled=<0|1> root=<path|none>"
  python3 - "$1" "$AUDIENCE_SCOPED" "$AUDIENCE_ROOT" <<'PY'
import shlex
import sys

cron, explicit, explicit_root = sys.argv[1:]
if explicit == "1" and explicit_root:
    print(f"enabled=1 root={explicit_root}")
    raise SystemExit(0)
for line in cron.splitlines():
    if "# nunchi:#816" not in line:
        continue
    fields = line.split(maxsplit=5)
    if len(fields) != 6:
        continue
    try:
        tokens = shlex.split(fields[5].split("# nunchi:#816", 1)[0])
    except ValueError:
        continue
    env = {}
    for token in tokens:
        if "=" not in token:
            break
        key, value = token.split("=", 1)
        env[key] = value.replace(r"\%", "%")
    if env.get("CCC_NUNCHI_AUDIENCE_SCOPED") == "1":
        print(f"enabled=1 root={env.get('CCC_NUNCHI_AUDIENCE_ROOT') or 'missing'}")
        raise SystemExit(0)
print("enabled=0 root=none")
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
  audience_line="$(_status_audience "$cron")"
  collection_line="$(_status_collection "$MEMPALACE_STATUS")"
  case "$audience_line" in
    enabled=1\ root=/*)
      # Scoped cron records one status per audience; the global file is stale (#985).
      collection_line="$(_status_collection_scoped "${audience_line#enabled=1 root=}")" ;;
  esac
  echo "runtime: user=$(id -un) home=$HOME"
  echo "provider: configured=$configured runtime=$runtime_provider match=$(_status_provider_match "$configured" "$runtime_provider")"
  echo "source: $(_status_source "$cron")"
  echo "mempalace: binary=${mp_path:-missing} version=$mp_ver"
  echo "collection: $collection_line"
  echo "audience_scoped: $audience_line"
  echo "mode: $(cat "$MODE_FILE" 2>/dev/null || echo off)"
  echo "hooks: $([ -f "$HOOKS/nunchi.py" ] && echo present || echo MISSING) ($HOOKS)"
  echo "codex_loader: $(if validate_codex_loader; then echo present; else echo MISSING/UNSAFE; fi)"
  echo "backend_auth: $(if any_backend_authenticated; then echo ok; else echo "NONE (claude/codex both unauthenticated)"; fi)"
  echo "cron: $(grep -cF "$MARK" <<<"$cron" || true) line(s)"
  echo "ghost_cron: $(_ghost_lines "$cron" | grep -c . || true) line(s) pointing at missing paths (#1079)"
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

# Ghost marker detection (#1079): a managed cron line whose script/binary/dir
# path no longer exists fails on every tick forever — the gongmyoung root
# crontab ran 3 such ghosts for weeks after the harness moved to a service
# account, because strip_cron only ever reaches the CALLER's crontab.
# Detection is warning-only; removal stays an operator decision.
_ghost_lines() {  # <cron-text> -> "lineno<TAB>missing-path" per missing path
  python3 - "$1" <<'PY'
import os
import re
import shlex
import sys

cron, mark = sys.argv[1], "# nunchi:#816"
assign = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
for lineno, line in enumerate(cron.splitlines(), 1):
    if mark not in line:
        continue
    fields = line.split(maxsplit=5)
    if len(fields) != 6:
        continue
    cmd = fields[5].split(mark, 1)[0]
    cmd = cmd.split(">>", 1)[0].split("2>&1", 1)[0]
    try:
        tokens = shlex.split(cmd.replace(r"\%", "%"))
    except ValueError:
        continue
    for tok in tokens:
        if assign.match(tok):
            continue  # env prefix values (NUNCHI_DB=...) may not exist yet
        if tok.startswith("/") or tok.endswith(".sh"):
            if not os.path.exists(tok):
                print(f"{lineno}\t{tok}")
PY
}

warn_ghost_markers() {  # <context-label> — best-effort, never fails the action
  local label="$1" cron ghosts
  cron="$("$CRONTAB" -l 2>/dev/null || true)"
  ghosts="$(_ghost_lines "$cron")"
  if [ -n "$ghosts" ]; then
    echo "WARNING ($label): managed nunchi cron line(s) point at missing paths (ghost entries — #1079):" >&2
    echo "$ghosts" >&2
    echo "  own-crontab ghosts are removed by --apply/--remove; otherwise: crontab -l | grep -vF '$MARK' | crontab -" >&2
  fi
  # Cross-account sweep (root only): other users' crontabs may hold ghosts
  # this account cannot see — #1079 was root-vs-service-user on gongmyoung.
  # Skipped under --target-user re-exec so the sweep runs once, at the top.
  if [ "$(id -u)" = 0 ] && [ -z "${CCC_NUNCHI_TARGET_USER:-}" ] \
     && command -v runuser >/dev/null 2>&1 && command -v getent >/dev/null 2>&1; then
    local dir user ucrontab ughosts swept=0
    for dir in /home/*; do
      [ -d "$dir" ] || continue
      user="${dir#/home/*}"
      getent passwd "$user" >/dev/null 2>&1 || continue
      swept=$((swept + 1)); [ "$swept" -gt 16 ] && break
      ucrontab="$(runuser -u "$user" -- crontab -l 2>/dev/null || true)"
      [ -n "$ucrontab" ] || continue
      ughosts="$(_ghost_lines "$ucrontab")"
      if [ -n "$ughosts" ]; then
        echo "WARNING ($label): user '$user' has ghost nunchi cron line(s) (#1079):" >&2
        echo "$ughosts" >&2
        echo "  remove with: runuser -u $user -- sh -c 'crontab -l | grep -vF \"$MARK\" | crontab -'" >&2
      fi
    done
  fi
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
    if [ "$AUDIENCE_SCOPED" = 1 ]; then
      [ "$resolved_provider" = piri ] || { echo "audience-scoped collection currently requires Piri" >&2; exit 2; }
      case "$AUDIENCE_ROOT" in
        /*) ;;
        *) echo "audience-scoped collection requires an absolute root" >&2; exit 2 ;;
      esac
      if [ -L "$AUDIENCE_ROOT" ] || { [ -e "$AUDIENCE_ROOT" ] && [ ! -d "$AUDIENCE_ROOT" ]; }; then
        echo "audience-scoped root is unsafe" >&2; exit 2
      fi
      mkdir -p "$AUDIENCE_ROOT"
      [ "$(stat -c %u -- "$AUDIENCE_ROOT")" = "$(id -u)" ] \
        || { echo "audience-scoped root owner is unsafe" >&2; exit 2; }
      chmod 700 "$AUDIENCE_ROOT"
    fi
    if [ "$resolved_provider" = "codex" ] && ! validate_codex_loader; then
      echo "Codex nunchi loader missing or unsafe at $HOOKS/codex-loader.py — run setup.sh first" >&2
      exit 2
    fi
    feed="$HOOKS/ingest-cron.sh"
    [ "$resolved_provider" = "codex" ] && feed="$HOOKS/codex-feed.sh"
    [ "$resolved_provider" = "piri" ]  && feed="$HOOKS/piri-feed.sh"
    # The Piri feed resolves its extractor CLI at RUNTIME from
    # CCC_PIRI_CLI_PATH/PATH; cron's bare PATH has no piri entry, which made
    # every feed tick a silent no-op on real nodes. Resolve a runnable CLI at
    # install time and pin it into the cron line (env-first so tests stay
    # hermetic; the wrapper is bypassed in favour of its real CLI).
    piri_env=""
    if [ "$resolved_provider" = "piri" ]; then
      piri_cli=""
      for _piri_cand in "${CCC_PIRI_REAL_CLI_PATH:-}" \
        "${CCC_PIRI_DEFAULT_CLI_PATH:-/opt/piri/piri-ccc.sh}" \
        "${CCC_PIRI_CLI_PATH:-}"; do
        [ -n "$_piri_cand" ] || continue
        case "$_piri_cand" in /*) ;; *) continue ;; esac
        if [ -f "$_piri_cand" ] && [ -x "$_piri_cand" ] && [ ! -L "$_piri_cand" ]; then
          piri_cli="$_piri_cand"; break
        fi
      done
      [ -n "$piri_cli" ] || piri_cli="$(command -v piri 2>/dev/null || true)"
      if [ -n "$piri_cli" ]; then
        piri_env="CCC_PIRI_CLI_PATH=$(cron_quote "$piri_cli") "
      else
        echo "WARNING: no runnable Piri CLI found (CCC_PIRI_REAL_CLI_PATH / /opt/piri/piri-ccc.sh / piri on PATH) — the piri-feed cron would skip extraction; install Piri or set CCC_PIRI_REAL_CLI_PATH, then re-apply" >&2
      fi
    fi
    bash_bin="$(command -v bash)"
    python3_bin="$(command -v python3)"
    mp="${CCC_NUNCHI_MEMPALACE_CLI:-}"
    [ -n "$mp" ] || mp="$(command -v mempalace || true)"
    [ -z "$mp" ] && [ -x "$HOME/.local/bin/mempalace" ] && mp="$HOME/.local/bin/mempalace"
    default_sweep="$CLAUDE_DIR/projects"
    [ "$resolved_provider" = "codex" ] && default_sweep="$CODEX_HOME_DIR/sessions"
    [ "$resolved_provider" = "piri" ]  && default_sweep="$HOME/.piri/agent/sessions"
    [ "$resolved_provider" = "piri" ] && [ "$AUDIENCE_SCOPED" = 1 ] \
      && default_sweep="$AUDIENCE_ROOT"
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
    warn_ghost_markers apply  # ghosts in OUR crontab are removed by strip_cron below
    strip_cron
    write_mode on
    scoped_env=""
    if [ "$AUDIENCE_SCOPED" = 1 ]; then
      scoped_env="CCC_NUNCHI_AUDIENCE_SCOPED=1 CCC_NUNCHI_AUDIENCE_ROOT=$(cron_quote "$AUDIENCE_ROOT") "
    fi
    append_cron_line "*/10 * * * * CCC_STATE_DIR=$(cron_quote "$STATE") ${scoped_env}${piri_env}NUNCHI_HOME=$(cron_quote "$NUNCHI_DIR") NUNCHI_DB=$(cron_quote "$NUNCHI_DB_PATH") NUNCHI_SNAPSHOT=$(cron_quote "$NUNCHI_SNAPSHOT_PATH") $(cron_quote "$bash_bin") $(cron_quote "$feed") >> $(cron_quote "$NUNCHI_DIR/cron.log") 2>&1 $MARK gen=$GEN"
    if [ "$refresh_ready" = 1 ]; then
      append_cron_line "17 * * * * CCC_STATE_DIR=$(cron_quote "$STATE") ${scoped_env}NUNCHI_HOME=$(cron_quote "$NUNCHI_DIR") CCC_NUNCHI_MEMPALACE_STATUS=$(cron_quote "$MEMPALACE_STATUS") CCC_NUNCHI_MEMPALACE_REFRESH_TIMEOUT_SEC=$(cron_quote "$MEMPALACE_TIMEOUT") CCC_NUNCHI_MEMPALACE_CLI=$(cron_quote "$mp") CCC_NUNCHI_TIMEOUT_CLI=$(cron_quote "$timeout_bin") CCC_NUNCHI_FLOCK_CLI=$(cron_quote "$flock_bin") $(cron_quote "$bash_bin") $(cron_quote "$refresh") $resolved_provider $(cron_quote "$sweep_dir") >> $(cron_quote "$NUNCHI_DIR/mempalace-sweep.cron.log") 2>&1 $MARK gen=$GEN"
      echo "mempalace hourly refresh cron added ($resolved_provider: $sweep_dir)"
    else
      echo "mempalace CLI, refresh hook or transcript dir missing — verbatim refresh cron skipped"
    fi
    # ${scoped_env} must be present here for the same reason as the feed and
    # refresh lines above: without it bench.sh scores the unscoped
    # $NUNCHI_DIR, which on a scoped node stops receiving facts as soon as
    # ingest becomes scoped. That made the Phase 2 parity gate (#827) measure
    # a store frozen weeks earlier on every audience-scoped node.
    # #832 — Mon 11:07, after the z.ai weekly quota reset (Mon 10:00 KST). The
    # old 08:07 slot ran while the previous week's pool was still exhausted and
    # produced contaminated sheets on nosuk/gongyung (2026-08-24 and 08-31,
    # #1210). The claude→codex→piri fallback chain (#1385) limits the damage,
    # but a post-reset slot keeps the primary backend consistent week to week.
    append_cron_line "7 11 * * 1 CCC_STATE_DIR=$(cron_quote "$STATE") ${scoped_env}NUNCHI_HOME=$(cron_quote "$NUNCHI_DIR") NUNCHI_DB=$(cron_quote "$NUNCHI_DB_PATH") NUNCHI_SNAPSHOT=$(cron_quote "$NUNCHI_SNAPSHOT_PATH") $(cron_quote "$bash_bin") $(cron_quote "$HOOKS/bench.sh") >> $(cron_quote "$NUNCHI_DIR/bench.cron.log") 2>&1 $MARK gen=$GEN"
    echo "weekly bench cron added (Mon 11:07, post-quota-reset)"
    if [ "$JUDGE" = 1 ]; then
      # #1204 daily review-queue triage. Dry-run unless --judge-apply is passed:
      # flipping to apply is a fresh-approval, per-node action, never a default.
      # #1264 — the flag exists so that approval can SURVIVE. Before it, the
      # only way to enable apply was hand-editing the managed cron line, which
      # strip_cron rewrites on the next re-apply: the approved pilot switched
      # itself back off with no error, the same silent-revert class as the
      # missing --judge install-record entry.
      judge_apply_env=""
      if [ "$JUDGE_APPLY" = 1 ]; then judge_apply_env="NUNCHI_JUDGE_APPLY=1 "; fi
      append_cron_line "41 4 * * * CCC_STATE_DIR=$(cron_quote "$STATE") ${judge_apply_env}${scoped_env}NUNCHI_HOME=$(cron_quote "$NUNCHI_DIR") NUNCHI_DB=$(cron_quote "$NUNCHI_DB_PATH") NUNCHI_SNAPSHOT=$(cron_quote "$NUNCHI_SNAPSHOT_PATH") $(cron_quote "$python3_bin") $(cron_quote "$HOOKS/judge-batch.py") >> $(cron_quote "$NUNCHI_DIR/judge.cron.log") 2>&1 $MARK gen=$GEN"
      if [ "$JUDGE_APPLY" = 1 ]; then
        echo "daily judge-batch cron added (04:41, APPLY — mutates the fact store)"
      else
        echo "daily judge-batch cron added (04:41, dry-run)"
      fi
    fi
    if [ "$resolved_provider" = "claude" ]; then
      set_sessionstart_hook add
    else
      set_sessionstart_hook remove
    fi
    python3 "$HOOKS/nunchi.py" init
    # Install record (#1081 phase 2): the replay must reproduce these exact
    # lines, so the RESOLVED provider and audience flags are materialized —
    # re-deriving from the ambient env would silently un-scope an
    # audience-scoped install (the #996 emergency configuration).
    record_argv=(--apply "--$resolved_provider")
    if [ "$AUDIENCE_SCOPED" = 1 ]; then record_argv+=(--audience-scoped "$AUDIENCE_ROOT"); fi
    # --judge must be materialized for the same reason as the audience flags:
    # the judge line is opt-in, so a replay that omits it silently DELETES the
    # cron (strip_cron drops all managed lines, then only the recorded flags
    # re-add them). Measured 2026-08-25: judge-batch cron was live on 1 of 11
    # fleet nodes while every node carried the script and a non-empty review
    # queue (#1264) — this omission is the mechanism that loses it.
    # --judge-apply supersedes --judge in the record (it implies it), so the
    # replay reproduces apply mode instead of silently downgrading to dry-run.
    if [ "$JUDGE_APPLY" = 1 ]; then record_argv+=(--judge-apply)
    elif [ "$JUDGE" = 1 ]; then record_argv+=(--judge); fi
    ccc_installer_record_write "$STATE" "$NUNCHI_SELF_DIR/install-nunchi.sh" "$MARK" "$GEN" -- \
      "${record_argv[@]}" \
      || echo "WARNING: install record write failed — self-update re-apply will not track these entries" >&2
    echo "nunchi enabled (mode=on, provider=$resolved_provider, feed=$(basename "$feed"))"
    if ! any_backend_authenticated; then
      echo "⚠ no authenticated LLM backend found (claude/codex) — ingest will still" >&2
      echo "  collect facts, but dialectic/bench synthesis has nothing to answer with" >&2
      echo "  until one is: run 'claude auth login' or 'codex login --device-auth'." >&2
    fi
    status
    ;;
  remove)
    write_mode off
    strip_cron
    warn_ghost_markers remove  # reports only cross-account residue at this point
    set_sessionstart_hook remove
    ccc_installer_record_remove "$STATE" "$NUNCHI_SELF_DIR/install-nunchi.sh" || true
    echo "nunchi disabled (code and ~/.nunchi DB kept)"
    status
    ;;
  *) status ;;
esac
