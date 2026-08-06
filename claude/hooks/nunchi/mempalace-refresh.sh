#!/usr/bin/env bash
# Provider-aware, bounded MemPalace refresh for the managed nunchi cron.
#
# MemPalace 3.6.x `sweep` parses Claude JSONL only. Codex and Piri JSONL are
# supported by the conversation miner, so those nodes use incremental
# `mine --mode convos --wing <codex|piri>` so mined facts are attributed to
# the provider (mine's `--wing` otherwise defaults to the directory name,
# e.g. "sessions").
# The wrapper records only body-free state and holds a single-flight lock.
# umask 077 keeps the lock, status and any MemPalace-created artefacts
# owner-only (#865).
set -euo pipefail
umask 077

provider="${1:-}"
target="${2:-}"
case "$provider" in
  claude|codex|piri) ;;
  *) echo "usage: mempalace-refresh.sh <claude|codex|piri> <transcript-dir>" >&2; exit 2 ;;
esac

state_dir="${CCC_STATE_DIR:-$HOME/.claude/state}"
nunchi_home="${NUNCHI_HOME:-$HOME/.nunchi}"
status_file="${CCC_NUNCHI_MEMPALACE_STATUS:-$nunchi_home/mempalace-refresh.status.json}"
lock_file="$nunchi_home/mempalace-refresh.lock"
mode_file="$state_dir/nunchi.mode"
timeout_sec="${CCC_NUNCHI_MEMPALACE_REFRESH_TIMEOUT_SEC:-3300}"
# GNU timeout treats zero as disabled. Keep the documented 55-minute ceiling.
case "$timeout_sec" in
  ''|*[!0-9]*|?????*) timeout_sec=3300 ;;
  *) [ "$timeout_sec" -ge 1 ] && [ "$timeout_sec" -le 3300 ] || timeout_sec=3300 ;;
esac

[ "$(cat "$mode_file" 2>/dev/null || true)" = on ] || exit 0

# Audience-scoped cron dispatches only canonical opaque direct children. Each
# child runs with an isolated HOME, which is where MemPalace keeps its index;
# status and locks remain beside that scope's Nunchi DB. Global behavior is
# unchanged when the flag is absent.
if [ "${CCC_NUNCHI_AUDIENCE_SCOPED:-0}" = 1 ] \
    && [ "${CCC_NUNCHI_SCOPED_CHILD:-0}" != 1 ]; then
  [ "$provider" = piri ] || { echo "audience-scoped refresh requires piri" >&2; exit 2; }
  audience_root="${CCC_NUNCHI_AUDIENCE_ROOT:-}"
  max_scopes="${CCC_NUNCHI_MAX_SCOPES_PER_RUN:-64}"
  case "$max_scopes" in
    ''|*[!0-9]*) max_scopes=64 ;;
    *) [ "$max_scopes" -ge 1 ] && [ "$max_scopes" -le 64 ] || max_scopes=64 ;;
  esac
  dispatcher_mp="${CCC_NUNCHI_MEMPALACE_CLI:-$(command -v mempalace || true)}"
  [ -n "$dispatcher_mp" ] || { [ ! -x "$HOME/.local/bin/mempalace" ] || dispatcher_mp="$HOME/.local/bin/mempalace"; }
  rc=0
  while IFS= read -r scope_root; do
    [ -d "$scope_root/piri/sessions" ] || continue
    mkdir -p "$scope_root/nunchi" "$scope_root/mempalace-home"
    chmod 700 "$scope_root/nunchi" "$scope_root/mempalace-home"
    CCC_NUNCHI_SCOPED_CHILD=1 \
      CCC_NUNCHI_AUDIENCE_SCOPE="${scope_root##*/}" \
      HOME="$scope_root/mempalace-home" \
      NUNCHI_HOME="$scope_root/nunchi" \
      CCC_NUNCHI_MEMPALACE_STATUS="$scope_root/nunchi/mempalace-refresh.status.json" \
      CCC_NUNCHI_MEMPALACE_CLI="$dispatcher_mp" \
      bash "$0" piri "$scope_root/piri/sessions" || rc=1
  done < <(python3 - "$audience_root" "$max_scopes" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys

root = Path(sys.argv[1])
limit = int(sys.argv[2])
try:
    meta = root.lstat()
except OSError:
    raise SystemExit(0)
if not (
    root.is_absolute()
    and stat.S_ISDIR(meta.st_mode)
    and meta.st_uid == os.geteuid()
    and not stat.S_IMODE(meta.st_mode) & 0o077
):
    raise SystemExit(0)
count = 0
for child in sorted(root.iterdir(), key=lambda item: item.name):
    if count >= limit:
        break
    if child.name != "shared" and not re.fullmatch(r"private-[0-9a-f]{32}", child.name):
        continue
    try:
        item = child.lstat()
    except OSError:
        continue
    if not (
        stat.S_ISDIR(item.st_mode)
        and item.st_uid == os.geteuid()
        and not stat.S_IMODE(item.st_mode) & 0o077
    ):
        continue
    print(child)
    count += 1
PY
  )
  exit "$rc"
fi
mkdir -p "$nunchi_home"

write_status() {
  python3 - "$status_file" "$provider" "$1" "$2" "$3" "$4" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
payload = {
    "schema": "ccc.nunchi.mempalace-refresh.v1",
    "provider": sys.argv[2],
    "state": sys.argv[3],
    "exit_code": int(sys.argv[4]),
    "started_at": int(sys.argv[5]),
    "finished_at": int(sys.argv[6]),
}
path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.write("\n")
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, path)
finally:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass
PY
}

# Never write provider status until this process owns the single-writer lock.
flock_cli="${CCC_NUNCHI_FLOCK_CLI:-$(command -v flock || true)}"
if [ -z "$flock_cli" ] || [ ! -f "$flock_cli" ] || [ ! -x "$flock_cli" ]; then
  echo "flock command missing" >&2
  exit 2
fi
if ! exec 9>"$lock_file"; then
  echo "mempalace lock unavailable" >&2
  exit 2
fi

set +e
"$flock_cli" -n -E 75 9
lock_rc=$?
set -e
if [ "$lock_rc" = 75 ]; then
  # The lock owner is responsible for the current running/final status.
  exit 0
fi
if [ "$lock_rc" != 0 ]; then
  echo "mempalace lock error" >&2
  exit 2
fi

started="$(date +%s)"
preflight_error() {
  local rc="${1:-2}"
  write_status error "$rc" "$started" "$(date +%s)"
  return "$rc"
}

# Every preflight failure owned by this lock replaces stale success.
if [ -z "$target" ] || [ ! -d "$target" ]; then
  echo "transcript directory missing" >&2
  preflight_error 2
  exit $?
fi
if [ "${CCC_NUNCHI_SCOPED_CHILD:-0}" = 1 ]; then
  # Do not let an audience transcript tree redirect the external miner across
  # the physical boundary through a symlink or permissive/foreign entry.
  python3 - "$target" <<'PY' || { preflight_error 2; exit $?; }
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
seen = 0
for directory, names, files in os.walk(root, followlinks=False):
    for raw in (directory, *(str(Path(directory) / name) for name in names + files)):
        seen += 1
        if seen > 100000:
            raise SystemExit(1)
        try:
            item = Path(raw).lstat()
        except OSError:
            raise SystemExit(1)
        if (
            stat.S_ISLNK(item.st_mode)
            or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) & 0o077
            or not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode))
        ):
            raise SystemExit(1)
PY
fi

mp="${CCC_NUNCHI_MEMPALACE_CLI:-}"
if [ -z "$mp" ]; then
  mp="$(command -v mempalace || true)"
  [ -n "$mp" ] || { [ ! -x "$HOME/.local/bin/mempalace" ] || mp="$HOME/.local/bin/mempalace"; }
fi
if [ -z "$mp" ] || [ ! -f "$mp" ] || [ ! -x "$mp" ]; then
  # MemPalace is absent on this node or unsupported on this platform
  # (e.g. Termux without a native chromadb build). Degrade to the
  # peer-facts-only path silently instead of failing every cron tick —
  # the feed cron keeps peer facts current without MemPalace (#865).
  write_status degraded 0 "$started" "$(date +%s)"
  exit 0
fi
timeout_cli="${CCC_NUNCHI_TIMEOUT_CLI:-$(command -v timeout || true)}"
if [ -z "$timeout_cli" ] || [ ! -f "$timeout_cli" ] || [ ! -x "$timeout_cli" ]; then
  echo "timeout command missing" >&2
  preflight_error 2
  exit $?
fi

write_status running -1 "$started" 0
cd "$HOME"
set +e
if [ "$provider" = codex ]; then
  "$timeout_cli" -k 30s "$timeout_sec" "$mp" mine "$target" --mode convos --wing codex
elif [ "$provider" = piri ]; then
  "$timeout_cli" -k 30s "$timeout_sec" "$mp" mine "$target" --mode convos --wing piri
else
  "$timeout_cli" -k 30s "$timeout_sec" "$mp" sweep "$target"
fi
rc=$?
set -e
finished="$(date +%s)"
if [ "$rc" = 0 ]; then
  write_status ok 0 "$started" "$finished"
else
  write_status error "$rc" "$started" "$finished"
fi
exit "$rc"
