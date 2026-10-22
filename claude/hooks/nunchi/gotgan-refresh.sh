#!/usr/bin/env bash
# Bounded gotgan (#2331) refresh for the managed nunchi cron — the verbatim
# store's counterpart of mempalace-refresh.sh, under the same discipline:
# mode gate, single-flight lock, hard timeout, owner-only body-free status.
#
# install-nunchi.sh schedules this hourly when the node opted into the gotgan
# verbatim backend (--verbatim-backend gotgan|both). Hand-installed shadow
# lanes (`# nunchi:gotgan-shadow`) are replaced by that managed line.
#
# Each run indexes at most GOTGAN_RUN_BYTES of new transcript bytes (gotgan.py
# default 64 MiB); a backlog drains over successive hourly runs rather than
# in one RSS spike (Termux ceiling 2.0 GB). The status file records the
# per-run counters so ccc-doctor / status can see a lane that never drains.
set -euo pipefail
umask 077

provider="${1:-}"
target="${2:-}"
case "$provider" in
  claude|codex|piri) ;;
  *) echo "usage: gotgan-refresh.sh <claude|codex|piri> <transcript-dir>" >&2; exit 2 ;;
esac

state_dir="${CCC_STATE_DIR:-$HOME/.claude/state}"
nunchi_home="${NUNCHI_HOME:-$HOME/.nunchi}"
status_file="${CCC_NUNCHI_GOTGAN_STATUS:-$nunchi_home/gotgan-refresh.status.json}"
lock_file="$nunchi_home/gotgan-refresh.lock"
mode_file="$state_dir/nunchi.mode"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
gotgan_py="${CCC_NUNCHI_GOTGAN_PY:-$here/gotgan.py}"
timeout_bin="${CCC_NUNCHI_TIMEOUT_CLI:-$(command -v timeout || true)}"
flock_bin="${CCC_NUNCHI_FLOCK_CLI:-$(command -v flock || true)}"
timeout_sec="${CCC_NUNCHI_GOTGAN_REFRESH_TIMEOUT_SEC:-1500}"
# GNU timeout treats zero as disabled. Keep the documented 55-minute ceiling.
case "$timeout_sec" in
  ''|*[!0-9]*|?????*) timeout_sec=1500 ;;
  *) [ "$timeout_sec" -ge 1 ] && [ "$timeout_sec" -le 3300 ] || timeout_sec=1500 ;;
esac
# A dedicated interpreter (venv with numpy + model2vec for the optional
# vector layer) may be pinned; otherwise the harness python3 serves the
# lexical-only store.
python_bin="${CCC_NUNCHI_GOTGAN_PYTHON:-}"
if [ -z "$python_bin" ]; then
  if [ -x "$HOME/.venv-gotgan/bin/python" ]; then python_bin="$HOME/.venv-gotgan/bin/python"
  else python_bin="$(command -v python3)"; fi
fi
export NUNCHI_HOME="$nunchi_home"
export GOTGAN_DB="${GOTGAN_DB:-$nunchi_home/gotgan.db}"
export GOTGAN_EMBED="${GOTGAN_EMBED:-auto}"

[ "$(cat "$mode_file" 2>/dev/null || true)" = on ] || exit 0

write_status() {  # <state> <exit_code> <started> [index-json]
  local tmp
  mkdir -p "$nunchi_home"
  tmp="$(mktemp "$nunchi_home/.gotgan-refresh.status.XXXXXX")"
  python3 - "$provider" "$1" "$2" "$3" "$(date +%s)" "${4:-}" > "$tmp" <<'PY'
import json, sys
provider, state, rc, started, finished, index_json = sys.argv[1:7]
rec = {"schema": "ccc.nunchi.gotgan-refresh.v1", "provider": provider, "state": state,
       "exit_code": int(rc), "started_at": int(started), "finished_at": int(finished)}
try:
    d = json.loads(index_json) if index_json else {}
except ValueError:
    d = {}
for k in ("files_changed", "chunks_added", "bytes_read", "run_bytes", "files_pending"):
    if isinstance(d.get(k), int):
        rec[k] = d[k]
print(json.dumps(rec, ensure_ascii=False))
PY
  chmod 600 "$tmp"
  mv -f -- "$tmp" "$status_file"
}

started="$(date +%s)"
if [ ! -f "$gotgan_py" ]; then
  write_status skipped 0 "$started"; echo "gotgan.py missing at $gotgan_py — skipped" >&2; exit 0
fi
if [ ! -d "$target" ]; then
  write_status skipped 0 "$started"; exit 0
fi
[ -n "$timeout_bin" ] && [ -x "$timeout_bin" ] || { write_status error 2 "$started"; echo "timeout missing" >&2; exit 2; }
[ -n "$flock_bin" ] && [ -x "$flock_bin" ] || { write_status error 2 "$started"; echo "flock missing" >&2; exit 2; }

mkdir -p "$nunchi_home"
exec 9>"$lock_file"
"$flock_bin" -n 9 || exit 0   # a previous run is still indexing; next tick retries

rc=0
out="$("$timeout_bin" -k 30s "$timeout_sec" "$python_bin" "$gotgan_py" index "$provider" "$target" 2>/dev/null)" || rc=$?
if [ "$rc" = 0 ]; then
  write_status ok 0 "$started" "$out"
else
  write_status error "$rc" "$started" "$out"
fi
exit "$rc"
