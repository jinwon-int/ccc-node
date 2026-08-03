#!/usr/bin/env bash
# Install the optional MemPalace verbatim layer on Android/Termux (#867).
#
# Native Android wheels are unavailable, so this creates a dedicated Debian 12
# PRoot container and installs MemPalace 3.6.0 with sqlite_exact + CPU MiniLM.
# Existing palace data is never removed by this script.
#
#   install-termux-mempalace.sh --preview [--codex|--claude]
#   install-termux-mempalace.sh --apply   [--codex|--claude]
#   install-termux-mempalace.sh --status  [--json]
#   install-termux-mempalace.sh --disable # keep container/palace, remove live wiring
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ACTION=status
PROVIDER=auto
JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --status) ACTION=status ;;
    --preview) ACTION=preview ;;
    --apply) ACTION=apply ;;
    --disable|--remove) ACTION=disable ;;
    --codex) PROVIDER=codex ;;
    --claude) PROVIDER=claude ;;
    --json) JSON=1 ;;
    --help|-h) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$PROVIDER" in auto|codex|claude) ;; *) echo "invalid provider" >&2; exit 2 ;; esac

prefix="${CCC_TERMUX_MEMPALACE_PREFIX:-${PREFIX:-}}"
is_termux=0
case "$prefix" in */com.termux/files/usr) is_termux=1 ;; esac
[ -n "${TERMUX_VERSION:-}" ] && is_termux=1
[ "${CCC_TERMUX_MEMPALACE_FORCE:-0}" = 1 ] && is_termux=1
[ "$is_termux" = 1 ] || { echo "Termux runtime required" >&2; exit 2; }
[ -n "$prefix" ] && [ -d "$prefix" ] || { echo "safe Termux PREFIX required" >&2; exit 2; }
case "$HOME" in /*) ;; *) echo "absolute HOME required" >&2; exit 2 ;; esac
case "$HOME" in *:*) echo "HOME containing ':' is unsupported" >&2; exit 2 ;; esac

container="${CCC_TERMUX_MEMPALACE_CONTAINER:-ccc-mempalace}"
case "$container" in ''|-*|*[!A-Za-z0-9_.-]*) echo "invalid container name" >&2; exit 2 ;; esac
image="${CCC_TERMUX_MEMPALACE_IMAGE:-debian:12}"
version="${CCC_TERMUX_MEMPALACE_VERSION:-3.6.0}"
[ "$version" = 3.6.0 ] || { echo "unsupported MemPalace version: $version" >&2; exit 2; }

proot_cli="${CCC_TERMUX_MEMPALACE_PROOT_CLI:-$prefix/bin/proot-distro}"
wrapper="$HOME/.local/bin/mempalace"
wrapper_source="$ROOT/scripts/termux-mempalace-wrapper.sh"
requirements_source="$ROOT/scripts/termux-mempalace-requirements.txt"
nunchi_installer="${CCC_TERMUX_MEMPALACE_NUNCHI_INSTALLER:-$ROOT/scripts/install-nunchi.sh}"
nunchi_home="${NUNCHI_HOME:-$HOME/.nunchi}"
managed_dir="$nunchi_home/termux-mempalace"
metadata="$managed_dir/status.json"
disabled_wrapper="$managed_dir/mempalace.disabled"
lock_file="$managed_dir/install.lock"
crontab_cli="${CCC_TERMUX_MEMPALACE_CRONTAB_CMD:-crontab}"

container_root() {
  if [ -n "${CCC_TERMUX_MEMPALACE_CONTAINER_ROOT:-}" ]; then
    printf '%s' "$CCC_TERMUX_MEMPALACE_CONTAINER_ROOT"
  elif [ -d "$prefix/var/lib/proot-distro/containers/$container/rootfs" ]; then
    printf '%s' "$prefix/var/lib/proot-distro/containers/$container/rootfs"
  elif [ -d "$prefix/var/lib/proot-distro/installed-rootfs/$container" ]; then
    printf '%s' "$prefix/var/lib/proot-distro/installed-rootfs/$container"
  else
    printf '%s' "$prefix/var/lib/proot-distro/containers/$container/rootfs"
  fi
}

detect_provider() {
  if [ "$PROVIDER" != auto ]; then printf '%s' "$PROVIDER"; return; fi
  local cron=""
  cron="$("$crontab_cli" -l 2>/dev/null || true)"
  if grep -q 'codex-feed.sh' <<<"$cron"; then printf codex; return; fi
  if grep -q 'ingest-cron.sh' <<<"$cron"; then printf claude; return; fi
  if [ -d "$HOME/.codex/sessions" ] && [ ! -d "$HOME/.claude/projects" ]; then
    printf codex
  elif [ -d "$HOME/.claude/projects" ] && [ ! -d "$HOME/.codex/sessions" ]; then
    printf claude
  else
    echo "cannot auto-detect provider; pass --codex or --claude" >&2
    return 2
  fi
}

transcript_source() {
  if [ -n "${NUNCHI_SWEEP_DIR:-}" ]; then printf '%s' "$NUNCHI_SWEEP_DIR"; return; fi
  if [ "$1" = codex ] && [ -n "${CODEX_SESSIONS_DIR:-}" ]; then
    printf '%s' "$CODEX_SESSIONS_DIR"
  elif [ "$1" = codex ]; then printf '%s' "$HOME/.codex/sessions"
  else printf '%s' "$HOME/.claude/projects"; fi
}

safe_managed_wrapper() {
  [ -f "$wrapper" ] && [ ! -L "$wrapper" ] && [ "$(stat -c %h -- "$wrapper")" = 1 ] \
    && [ "$(stat -c %u -- "$wrapper")" = "$(id -u)" ] && [ "$(stat -c %a -- "$wrapper")" = 700 ] \
    && grep -qF 'ccc-node:termux-mempalace-wrapper:v1' "$wrapper" 2>/dev/null
}

safe_source_file() {
  local path="$1" mode owner
  [ -f "$path" ] && [ ! -L "$path" ] && [ "$(stat -c %h -- "$path")" = 1 ] || return 1
  owner="$(stat -c %u -- "$path")"
  [ "$owner" = "$(id -u)" ] || [ "$owner" = 0 ] || return 1
  mode="$(stat -c %a -- "$path")"
  [ $(( ((mode / 10) % 10) & 2 )) -eq 0 ] && [ $(( (mode % 10) & 2 )) -eq 0 ]
}

safe_private_file() {
  [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c %h -- "$1")" = 1 ] \
    && [ "$(stat -c %u -- "$1")" = "$(id -u)" ] && [ "$(stat -c %a -- "$1")" = 600 ]
}

safe_managed_container() {
  local base="$1/opt/ccc-mempalace" marker
  marker="$base/.ccc-node-managed"
  [ -d "$base" ] && [ ! -L "$base" ] && [ "$(stat -c %u -- "$base")" = "$(id -u)" ] \
    && [ "$(stat -c %a -- "$base")" = 700 ] && safe_private_file "$marker" \
    && [ "$(cat -- "$marker")" = "ccc-node #867 managed container" ]
}

prepare_managed_dir() {
  if [ -L "$managed_dir" ]; then
    echo "refusing symlinked managed directory: $managed_dir" >&2
    return 2
  fi
  mkdir -p "$managed_dir"
  [ "$(stat -c %u -- "$managed_dir")" = "$(id -u)" ] \
    || { echo "managed directory has the wrong owner: $managed_dir" >&2; return 2; }
  chmod 700 "$managed_dir"
}

acquire_install_lock() {
  prepare_managed_dir
  if [ -e "$lock_file" ] || [ -L "$lock_file" ]; then
    safe_private_file "$lock_file" \
      || { echo "unsafe install lock: $lock_file" >&2; return 2; }
  fi
  exec 9>"$lock_file"
  chmod 600 "$lock_file"
  flock -w 10 9 || { echo "another Termux MemPalace operation is active" >&2; return 2; }
}

file_count() {
  [ -d "$1" ] || { printf 0; return; }
  find "$1" -type f -name '*.jsonl' -print 2>/dev/null | wc -l | tr -d ' '
}

refresh_state() {
  python3 - "$nunchi_home/mempalace-refresh.status.json" <<'PY'
import json, sys
try:
    doc=json.load(open(sys.argv[1], encoding="utf-8"))
    state=doc.get("state", "invalid") if doc.get("schema") == "ccc.nunchi.mempalace-refresh.v1" else "invalid"
    state=state if state in {"running", "ok", "error"} else "invalid"
    finished=doc.get("finished_at", 0)
    finished=finished if isinstance(finished, int) and finished >= 0 else 0
    print(f"{state}\t{finished}")
except FileNotFoundError:
    print("missing\t0")
except Exception:
    print("invalid\t0")
PY
}

latest_source_mtime() {
  [ -d "$1" ] || { printf 0; return; }
  find "$1" -type f -name '*.jsonl' -printf '%T@\n' 2>/dev/null \
    | sort -nr | head -n 1 | cut -d. -f1 | grep -E '^[0-9]+$' || printf 0
}

palace_state() {
  local db="$1/opt/ccc-mempalace/palace/sqlite_exact.sqlite3"
  if ! safe_managed_container "$1"; then printf 'missing\t0\t0'; return; fi
  if [ ! -e "$db" ] && [ ! -L "$db" ]; then printf 'missing\t0\t0'; return; fi
  if [ ! -f "$db" ] || [ -L "$db" ] || [ "$(stat -c %h -- "$db")" != 1 ] \
    || [ "$(stat -c %u -- "$db")" != "$(id -u)" ]; then
    printf 'unsafe\t0\t0'; return
  fi
  python3 - "$db" <<'PY'
import os, sqlite3, sys, urllib.parse
path=os.path.abspath(sys.argv[1])
try:
    uri="file:" + urllib.parse.quote(path, safe="/") + "?mode=ro"
    conn=sqlite3.connect(uri, uri=True, timeout=1)
    try:
        integrity=conn.execute("PRAGMA quick_check(1)").fetchone()
        integrity="ok" if integrity and integrity[0] == "ok" else "error"
        count=conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()
    print(f"{integrity}\t{int(count)}\t{os.path.getsize(path)}")
except Exception:
    print("error\t0\t0")
PY
}

status_values() {
  local provider source root installed=0 wrapper_state=missing refresh refresh_finished_at=0 cron_count=0
  local cli_version=missing palace_integrity=missing drawer_count=0 palace_bytes=0 source_mtime=0 source_age=0 now
  provider="$(detect_provider 2>/dev/null || printf unknown)"
  if [ "$provider" = codex ] || [ "$provider" = claude ]; then source="$(transcript_source "$provider")"; else source=unknown; fi
  root="$(container_root)"
  safe_managed_container "$root" \
    && [ -x "$root/opt/ccc-mempalace/venv/bin/mempalace" ] && installed=1
  if [ "$installed" = 1 ]; then
    dist_info="$(find "$root/opt/ccc-mempalace/venv/lib" -type d -name 'mempalace-*.dist-info' -print -quit 2>/dev/null || true)"
    if [ -n "$dist_info" ]; then
      cli_version="$(basename "$dist_info")"
      cli_version="${cli_version#mempalace-}"
      cli_version="${cli_version%.dist-info}"
    else
      cli_version=error
    fi
  fi
  if safe_managed_wrapper; then wrapper_state=managed
  elif [ -e "$wrapper" ]; then wrapper_state=unmanaged; fi
  IFS=$'\t' read -r refresh refresh_finished_at <<<"$(refresh_state)"
  cron_count="$("$crontab_cli" -l 2>/dev/null | grep -c 'mempalace-refresh.sh.*# nunchi:#816' || true)"
  IFS=$'\t' read -r palace_integrity drawer_count palace_bytes <<<"$(palace_state "$root")"
  source_mtime="$(latest_source_mtime "$source")"
  now="$(date +%s)"
  [ "$source_mtime" -gt 0 ] && [ "$source_mtime" -le "$now" ] && source_age=$((now - source_mtime))
  printf '%s\n' \
    "provider=$provider" "source=$source" "source_files=$(file_count "$source")" \
    "source_latest_mtime=$source_mtime" "source_age_sec=$source_age" \
    "container=$container" "container_installed=$installed" "wrapper=$wrapper_state" \
    "cli_version=$cli_version" "backend=sqlite_exact" "embedding_model=minilm" \
    "embedding_threads=1" "refresh_state=$refresh" "refresh_finished_at=$refresh_finished_at" \
    "refresh_cron_count=$cron_count" "palace_integrity=$palace_integrity" \
    "drawer_count=$drawer_count" "palace_db_bytes=$palace_bytes"
}

emit_status() {
  local values
  values="$(status_values)"
  if [ "$JSON" = 0 ]; then printf '%s\n' "$values"; return; fi
  python3 -c 'import json,sys
d={}
for line in sys.stdin:
 k,v=line.rstrip("\n").split("=",1); d[k]=int(v) if k in {"source_files","source_latest_mtime","source_age_sec","container_installed","embedding_threads","refresh_finished_at","refresh_cron_count","drawer_count","palace_db_bytes"} else v
print(json.dumps({"schema":"ccc.termux-mempalace.status.v1",**d},separators=(",",":"),sort_keys=True))' <<<"$values"
}

write_metadata() {
  local enabled="$1" provider="$2" source="$3" state="$4"
  prepare_managed_dir
  python3 - "$metadata" "$enabled" "$provider" "$source" "$state" "$container" "$version" <<'PY'
import json, os, sys, tempfile, time
path=sys.argv[1]
payload={"schema":"ccc.termux-mempalace.install.v1","enabled":sys.argv[2]=="1","provider":sys.argv[3],"source":sys.argv[4],"state":sys.argv[5],"container":sys.argv[6],"version":sys.argv[7],"updated_at":int(time.time())}
fd,tmp=tempfile.mkstemp(prefix=".status.",dir=os.path.dirname(path))
try:
 with os.fdopen(fd,"w",encoding="utf-8") as f: json.dump(payload,f,separators=(",",":")); f.write("\n")
 os.chmod(tmp,0o600); os.replace(tmp,path)
finally:
 try: os.unlink(tmp)
 except FileNotFoundError: pass
PY
}

install_wrapper() {
  mkdir -p "$HOME/.local/bin"; chmod 700 "$HOME/.local/bin"
  if [ -e "$wrapper" ] && ! safe_managed_wrapper; then
    echo "refusing to replace unmanaged $wrapper" >&2
    return 2
  fi
  local tmp
  tmp="$(mktemp "$HOME/.local/bin/.mempalace.XXXXXX")"
  cp "$wrapper_source" "$tmp"; chmod 700 "$tmp"; mv -f "$tmp" "$wrapper"
}

disable_refresh_cron() {
  local tmp
  tmp="$(mktemp)"
  "$crontab_cli" -l 2>/dev/null | grep -v 'mempalace-refresh.sh.*# nunchi:#816' > "$tmp" || true
  "$crontab_cli" "$tmp"
  rm -f "$tmp"
}

provider=unknown
if ! provider="$(detect_provider)"; then
  [ "$ACTION" = status ] || exit 2
fi
if [ "$provider" = codex ] || [ "$provider" = claude ]; then
  source="$(transcript_source "$provider")"
else
  source=unknown
fi
root="$(container_root)"

case "$ACTION" in
  status)
    emit_status
    ;;
  preview)
    files="$(file_count "$source")"
    available_kb="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
    printf '%s\n' "action=preview" "provider=$provider" "source=$source" "source_files=$files" \
      "container=$container" "container_present=$([ -d "$root" ] && echo 1 || echo 0)" \
      "image=$image" "version=$version" "available_kb=${available_kb:-0}" \
      "would_restart_bridge=0" "would_delete_palace=0"
    ;;
  disable)
    acquire_install_lock
    disable_refresh_cron
    if safe_managed_wrapper; then mv -f "$wrapper" "$disabled_wrapper"; chmod 700 "$disabled_wrapper"; fi
    write_metadata 0 "$provider" "$source" disabled
    echo "Termux MemPalace disabled; nunchi facts, container and palace preserved"
    emit_status
    ;;
  apply)
    [ -f "$proot_cli" ] && [ -x "$proot_cli" ] || { echo "proot-distro unavailable" >&2; exit 2; }
    safe_source_file "$wrapper_source" && [ -x "$wrapper_source" ] \
      || { echo "managed wrapper source missing or unsafe" >&2; exit 2; }
    safe_source_file "$requirements_source" \
      || { echo "managed dependency lock missing or unsafe" >&2; exit 2; }
    safe_source_file "$nunchi_installer" \
      || { echo "nunchi installer missing or unsafe" >&2; exit 2; }
    [ -d "$source" ] || { echo "transcript source missing: $source" >&2; exit 2; }
    home_real="$(cd "$HOME" && pwd -P)"
    source="$(cd "$source" && pwd -P)"
    case "$source/" in
      "$home_real"/*) ;;
      *) echo "transcript source must be inside Termux HOME for isolated PRoot" >&2; exit 2 ;;
    esac
    files="$(file_count "$source")"
    [ "$files" -gt 0 ] || { echo "transcript source contains no JSONL: $source" >&2; exit 2; }
    if [ -e "$wrapper" ] && ! safe_managed_wrapper; then
      echo "refusing to replace unmanaged $wrapper" >&2; exit 2
    fi
    available_kb="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
    min_kb=524288
    [ -d "$root" ] || min_kb=2097152
    [ "${available_kb:-0}" -ge "$min_kb" ] || { echo "insufficient Termux HOME space" >&2; exit 2; }
    acquire_install_lock
    created_container=0
    if [ ! -d "$root" ]; then
      "$proot_cli" install -q -n "$container" "$image"
      root="$(container_root)"
      created_container=1
    fi
    [ -d "$root" ] || { echo "container root missing after install" >&2; exit 2; }
    if [ "$created_container" = 1 ]; then
      mkdir -p "$root/opt/ccc-mempalace"
      chmod 700 "$root/opt/ccc-mempalace"
      printf '%s\n' "ccc-node #867 managed container" > "$root/opt/ccc-mempalace/.ccc-node-managed"
      chmod 600 "$root/opt/ccc-mempalace/.ccc-node-managed"
    fi
    if [ "$created_container" = 0 ] && ! safe_managed_container "$root"; then
      echo "refusing to modify unmanaged container: $container" >&2
      exit 2
    fi
    if [ -e "$root/opt/ccc-mempalace/requirements.lock" ] \
      || [ -L "$root/opt/ccc-mempalace/requirements.lock" ]; then
      safe_private_file "$root/opt/ccc-mempalace/requirements.lock" \
        || { echo "unsafe installed dependency lock" >&2; exit 2; }
      cmp -s "$requirements_source" "$root/opt/ccc-mempalace/requirements.lock" \
        || { echo "installed dependency lock drift; refusing in-place mutation" >&2; exit 2; }
    fi
    if find "$root/opt/ccc-mempalace/venv/lib" -type d -name 'mempalace-*.dist-info' \
      ! -name "mempalace-$version.dist-info" -print -quit 2>/dev/null | grep -q .; then
      echo "installed MemPalace version drift; refusing in-place mutation" >&2
      exit 2
    fi
    input_tmp="$root/opt/ccc-mempalace/.requirements.input.lock.$$"
    cp "$requirements_source" "$input_tmp"
    chmod 600 "$input_tmp"
    mv -f "$input_tmp" "$root/opt/ccc-mempalace/requirements.input.lock"
    need_guest_setup=1
    if safe_managed_container "$root" \
      && [ -x "$root/opt/ccc-mempalace/venv/bin/mempalace" ] \
      && find "$root/opt/ccc-mempalace/venv/lib" -type d -name "mempalace-$version.dist-info" -print -quit 2>/dev/null | grep -q . \
      && safe_private_file "$root/opt/ccc-mempalace/requirements.lock" \
      && cmp -s "$requirements_source" "$root/opt/ccc-mempalace/requirements.lock"; then
      need_guest_setup=0
    fi
    if [ "$need_guest_setup" = 1 ]; then
      "$proot_cli" login --isolated "$container" -- /bin/sh -eu -c '
      version="$1"
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y --no-install-recommends ca-certificates python3 python3-venv
      install -d -m 700 /opt/ccc-mempalace /opt/ccc-mempalace/home /opt/ccc-mempalace/cache /opt/ccc-mempalace/palace
      if [ ! -x /opt/ccc-mempalace/venv/bin/python ]; then python3 -m venv /opt/ccc-mempalace/venv; fi
      /opt/ccc-mempalace/venv/bin/python -m pip install --disable-pip-version-check --no-input "pip==24.0"
      /opt/ccc-mempalace/venv/bin/python -m pip install --disable-pip-version-check --no-input --only-binary=:all: -r /opt/ccc-mempalace/requirements.input.lock
      /opt/ccc-mempalace/venv/bin/python -m pip check
      /opt/ccc-mempalace/venv/bin/python -m pip freeze > /opt/ccc-mempalace/requirements.lock.tmp
      cmp /opt/ccc-mempalace/requirements.input.lock /opt/ccc-mempalace/requirements.lock.tmp
      chmod 600 /opt/ccc-mempalace/requirements.lock.tmp
      mv -f /opt/ccc-mempalace/requirements.lock.tmp /opt/ccc-mempalace/requirements.lock
      printf "%s\n" "ccc-node #867 managed container" > /opt/ccc-mempalace/.ccc-node-managed
      chmod 600 /opt/ccc-mempalace/.ccc-node-managed
    ' sh "$version"
    fi
    [ -x "$root/opt/ccc-mempalace/venv/bin/mempalace" ] \
      || { echo "MemPalace executable missing after install" >&2; exit 2; }
    cmp -s "$requirements_source" "$root/opt/ccc-mempalace/requirements.lock" \
      || { echo "MemPalace dependency lock mismatch after install" >&2; exit 2; }
    install_wrapper
    if ! "$wrapper" --version >/dev/null; then
      disable_refresh_cron
      mv -f "$wrapper" "$disabled_wrapper"
      chmod 700 "$disabled_wrapper"
      write_metadata 0 "$provider" "$source" wrapper-failed
      echo "MemPalace wrapper validation failed; peer_facts-only wiring preserved" >&2
      exit 1
    fi
    if ! CCC_NUNCHI_MEMPALACE_CLI="$wrapper" NUNCHI_SWEEP_DIR="$source" \
      bash "$nunchi_installer" --apply "--$provider"; then
      disable_refresh_cron
      mv -f "$wrapper" "$disabled_wrapper"
      chmod 700 "$disabled_wrapper"
      write_metadata 0 "$provider" "$source" nunchi-wiring-failed
      echo "nunchi wiring failed; MemPalace wrapper disabled" >&2
      exit 1
    fi
    refresh="$HOME/.claude/hooks/nunchi/mempalace-refresh.sh"
    if [ ! -x "$refresh" ] || ! CCC_NUNCHI_MEMPALACE_CLI="$wrapper" NUNCHI_SWEEP_DIR="$source" \
      bash "$refresh" "$provider" "$source"; then
      disable_refresh_cron
      mv -f "$wrapper" "$disabled_wrapper" 2>/dev/null || true
      chmod 700 "$disabled_wrapper" 2>/dev/null || true
      write_metadata 0 "$provider" "$source" refresh-failed
      echo "initial MemPalace refresh failed; peer_facts-only wiring restored" >&2
      exit 1
    fi
    write_metadata 1 "$provider" "$source" ready
    echo "Termux MemPalace installed and initial refresh completed"
    emit_status
    ;;
esac
