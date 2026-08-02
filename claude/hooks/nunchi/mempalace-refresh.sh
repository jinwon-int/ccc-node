#!/usr/bin/env bash
# Provider-aware, bounded MemPalace refresh for the managed nunchi cron.
#
# MemPalace 3.6.x `sweep` parses Claude JSONL only. Codex JSONL is supported by
# the conversation miner, so Codex nodes must use incremental `mine --mode
# convos` instead. A wrapper also gives the readiness probe a body-free durable
# result and prevents overlapping or permanently orphaned refresh jobs.
set -euo pipefail

provider="${1:-}"
target="${2:-}"
case "$provider" in
  claude|codex) ;;
  *) echo "usage: mempalace-refresh.sh <claude|codex> <transcript-dir>" >&2; exit 2 ;;
esac
[ -n "$target" ] && [ -d "$target" ] || { echo "transcript directory missing" >&2; exit 2; }

state_dir="${NUNCHI_HOME:-$HOME/.nunchi}"
mode_file="${CCC_STATE_DIR:-$HOME/.claude/state}/nunchi.mode"
status_file="${CCC_NUNCHI_MEMPALACE_STATUS:-$state_dir/mempalace-refresh.status.json}"
lock_file="$state_dir/mempalace-refresh.lock"
timeout_sec="${CCC_NUNCHI_MEMPALACE_REFRESH_TIMEOUT_SEC:-3300}"
case "$timeout_sec" in ''|*[!0-9]*) timeout_sec=3300 ;; esac

[ "$(cat "$mode_file" 2>/dev/null || true)" = on ] || exit 0
mp="$(command -v mempalace || true)"
[ -z "$mp" ] && [ -x "$HOME/.local/bin/mempalace" ] && mp="$HOME/.local/bin/mempalace"
[ -n "$mp" ] || { echo "mempalace CLI missing" >&2; exit 2; }
mkdir -p "$state_dir"

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

(
  flock -n 9 || { echo "mempalace refresh already running"; exit 0; }
  started="$(date +%s)"
  write_status running -1 "$started" 0
  cd "$HOME"
  set +e
  if [ "$provider" = codex ]; then
    timeout -k 30s "$timeout_sec" "$mp" mine "$target" --mode convos
  else
    timeout -k 30s "$timeout_sec" "$mp" sweep "$target"
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
) 9>"$lock_file"
