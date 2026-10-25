#!/usr/bin/env bash
# Hermetic coverage for the Termux PRoot MemPalace installer (#867).
# shellcheck disable=SC2034 # assertion variables are consumed through ok/eval
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Inherited installer env costs 4 assertions on a live node (#1023).
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

home="$TMP/home"
prefix="$TMP/prefix"
fake_bin="$prefix/bin"
container_root="$prefix/var/lib/proot-distro/containers/ccc-mempalace/rootfs"
cron_store="$TMP/crontab"
proot_log="$TMP/proot.log"
nunchi_capture="$TMP/nunchi.capture"
mkdir -p "$home/.codex/sessions" "$home/.claude/hooks/nunchi" "$home/.claude/state" "$fake_bin"
printf '{}\n' > "$home/.codex/sessions/rollout one.jsonl"
printf on > "$home/.claude/state/nunchi.mode"

cat > "$fake_bin/proot-distro" <<'SH'
#!/usr/bin/env bash
printf '%s\0' "$@" >> "${CCC_TEST_PROOT_LOG:?}"
printf '\n' >> "${CCC_TEST_PROOT_LOG:?}"
if [ "${1:-}" = install ]; then
  root="${CCC_TEST_CONTAINER_ROOT:?}"
  mkdir -p "$root/opt/ccc-mempalace/venv/bin" "$root/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-3.11.0.dist-info"
  cat > "$root/opt/ccc-mempalace/venv/bin/mempalace" <<'EOF'
#!/bin/sh
exit 0
EOF
  chmod 700 "$root/opt/ccc-mempalace/venv/bin/mempalace"
  printf '%s\n' managed > "$root/opt/ccc-mempalace/.ccc-node-managed"
  chmod 600 "$root/opt/ccc-mempalace/.ccc-node-managed"
  exit 0
fi
if [ "${1:-}" = login ] && [ "${CCC_TEST_PROOT_GUEST_EXEC:-0}" = 1 ]; then
  # Opt-in (#2512): run the real guest setup script against the container root
  # with a fake apt-get and the fake venv pip from mk_fake_pip.
  args=("$@")
  for i in "${!args[@]}"; do
    if [ "${args[$i]}" = -c ]; then
      root="${CCC_TEST_CONTAINER_ROOT:?}"
      script="${args[$((i+1))]}"
      script="${script//\/opt\/ccc-mempalace/$root/opt/ccc-mempalace}"
      PATH="${CCC_TEST_GUEST_BIN:?}:$PATH" sh -eu -c "$script" "${args[@]:$((i+2))}"
      exit $?
    fi
  done
fi
if [ "${1:-}" = login ] && [ -f "${CCC_TEST_CONTAINER_ROOT:?}/opt/ccc-mempalace/requirements.input.lock" ]; then
  # Mimic the guest pip: the venv ends up on exactly the pin the input lock names.
  pin="$(sed -n 's/^mempalace==//p' "${CCC_TEST_CONTAINER_ROOT:?}/opt/ccc-mempalace/requirements.input.lock" | head -1)"
  if [ -n "$pin" ]; then
    site="${CCC_TEST_CONTAINER_ROOT:?}/opt/ccc-mempalace/venv/lib/python3.11/site-packages"
    find "$site" -maxdepth 1 -type d -name 'mempalace-*.dist-info' ! -name "mempalace-$pin.dist-info" -exec rm -rf {} + 2>/dev/null
    mkdir -p "$site/mempalace-$pin.dist-info"
  fi
  cp "${CCC_TEST_CONTAINER_ROOT:?}/opt/ccc-mempalace/requirements.input.lock" \
    "${CCC_TEST_CONTAINER_ROOT:?}/opt/ccc-mempalace/requirements.lock"
  chmod 600 "${CCC_TEST_CONTAINER_ROOT:?}/opt/ccc-mempalace/requirements.lock"
fi
case " $* " in
  *" /opt/ccc-mempalace/venv/bin/mempalace --version "*) echo 'mempalace 3.11.0' ;;
esac
exit 0
SH
chmod 700 "$fake_bin/proot-distro"

cat > "$fake_bin/crontab" <<'SH'
#!/usr/bin/env bash
store="${CCC_TEST_CRON_STORE:?}"
if [ "${1:-}" = -l ]; then [ ! -f "$store" ] || cat "$store"; exit 0; fi
cp "$1" "$store"
SH
chmod 700 "$fake_bin/crontab"

cat > "$TMP/install-nunchi" <<'SH'
#!/usr/bin/env bash
printf 'args=%s\ncli=%s\nsource=%s\n' "$*" "${CCC_NUNCHI_MEMPALACE_CLI:-}" "${NUNCHI_SWEEP_DIR:-}" > "${CCC_TEST_NUNCHI_CAPTURE:?}"
exit 0
SH
chmod 700 "$TMP/install-nunchi"

cat > "$home/.claude/hooks/nunchi/mempalace-refresh.sh" <<'SH'
#!/usr/bin/env bash
mkdir -p "$HOME/.nunchi"
# Mirrors the real exit-0-without-running paths (#2511): nunchi mode off, and
# another refresh holding the single-writer lock (CCC_TEST_REFRESH_SKIP=1).
[ "$(cat "$HOME/.claude/state/nunchi.mode" 2>/dev/null || true)" = on ] || exit 0
[ "${CCC_TEST_REFRESH_SKIP:-0}" = 1 ] && exit 0
rc="${CCC_TEST_REFRESH_RC:-0}"
state="${CCC_TEST_REFRESH_STATE:-ok}"
[ "$rc" = 0 ] || state=error
now="$(date +%s)"
printf '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"%s","state":"%s","exit_code":%s,"started_at":%s,"finished_at":%s}\n' "$1" "$state" "$rc" "$now" "$now" > "$HOME/.nunchi/mempalace-refresh.status.json"
exit "$rc"
SH
chmod 700 "$home/.claude/hooks/nunchi/mempalace-refresh.sh"

common_env=(
  HOME="$home" PREFIX="$prefix" CCC_TERMUX_MEMPALACE_PREFIX="$prefix"
  CCC_TERMUX_MEMPALACE_FORCE=1 CCC_TERMUX_MEMPALACE_PROOT_CLI="$fake_bin/proot-distro"
  CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$container_root"
  CCC_TERMUX_MEMPALACE_NUNCHI_INSTALLER="$TMP/install-nunchi"
  CCC_TERMUX_MEMPALACE_CRONTAB_CMD="$fake_bin/crontab"
  CCC_TEST_CONTAINER_ROOT="$container_root" CCC_TEST_PROOT_LOG="$proot_log"
  CCC_TEST_CRON_STORE="$cron_store" CCC_TEST_NUNCHI_CAPTURE="$nunchi_capture"
)
run_install() { env "${common_env[@]}" bash "$ROOT/scripts/install-termux-mempalace.sh" "$@"; }

out="$(run_install --preview --codex 2>&1)"; rc=$?
ok "preview is read-only and discovers the Codex transcript" \
  '[ "$rc" = 0 ] && grep -q "source_files=1" <<<"$out" && [ ! -e "$container_root" ] && [ ! -e "$home/.local/bin/mempalace" ] && [ ! -e "$proot_log" ]'

# Regression: a large mtime stream made `sort | head` trip pipefail after
# printing the correct value, appending a second zero and corrupting JSON.
pipefail_bin="$TMP/pipefail-bin"
mkdir -p "$pipefail_bin"
cat > "$pipefail_bin/find" <<'SH'
#!/usr/bin/env bash
case "$*" in
  *%T@*) awk 'BEGIN { for (i=0; i<50000; i++) print "1785724620.5" }'; exit 0 ;;
esac
exec "${CCC_TEST_REAL_FIND:?}" "$@"
SH
chmod 700 "$pipefail_bin/find"
real_find="$(command -v find)"
out="$(PATH="$pipefail_bin:$PATH" CCC_TEST_REAL_FIND="$real_find" \
  run_install --status --json --codex 2>&1)"; rc=$?
ok "large transcript mtime streams produce one valid JSON scalar under pipefail" \
  '[ "$rc" = 0 ] && jq -e '\'' .source_latest_mtime == 1785724620 and .source_files == 1 '\'' <<<"$out" >/dev/null'

custom_sessions="$TMP/custom-codex-sessions"
mkdir -p "$custom_sessions"
printf '{}\n' > "$custom_sessions/custom.jsonl"
out="$(env "${common_env[@]}" CODEX_SESSIONS_DIR="$custom_sessions" \
  bash "$ROOT/scripts/install-termux-mempalace.sh" --preview --codex 2>&1)"; rc=$?
ok "an explicit Codex sessions directory overrides the default source" \
  '[ "$rc" = 0 ] && grep -q "source=$custom_sessions" <<<"$out" && grep -q "source_files=1" <<<"$out"'
out="$(env "${common_env[@]}" CODEX_SESSIONS_DIR="$custom_sessions" \
  bash "$ROOT/scripts/install-termux-mempalace.sh" --apply --codex 2>&1)"; rc=$?
ok "apply rejects transcript paths outside the isolated HOME bind" \
  '[ "$rc" = 2 ] && grep -q "must be inside Termux HOME" <<<"$out" && [ ! -e "$container_root" ]'

out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "apply creates a dedicated container, managed wrapper and body-free metadata" \
  '[ "$rc" = 0 ] && [ -x "$container_root/opt/ccc-mempalace/venv/bin/mempalace" ] && [ -x "$home/.local/bin/mempalace" ] && [ "$(stat -c %a "$home/.nunchi/termux-mempalace/status.json")" = 600 ]'
ok "apply wires nunchi to the exact wrapper and Codex source" \
  'grep -q "args=--apply --codex" "$nunchi_capture" && grep -q "cli=$home/.local/bin/mempalace" "$nunchi_capture" && grep -q "source=$home/.codex/sessions" "$nunchi_capture"'
ok "the initial refresh records a successful provider-scoped status" \
  'jq -e '\'' .provider == "codex" and .state == "ok" '\'' "$home/.nunchi/mempalace-refresh.status.json" >/dev/null'

palace_db="$container_root/opt/ccc-mempalace/palace/sqlite_exact.sqlite3"
mkdir -p "$(dirname "$palace_db")"
python3 - "$palace_db" <<'PY'
import sqlite3, sys
conn=sqlite3.connect(sys.argv[1])
conn.execute("CREATE TABLE documents (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
conn.execute("INSERT INTO documents VALUES (?, ?)", ("drawer-1", "TEST_BODY_MUST_NOT_LEAK"))
conn.commit()
conn.close()
PY

: > "$proot_log"
CCC_TEST_PROOT_LOG="$proot_log" CCC_TEST_CONTAINER_ROOT="$container_root" \
  CCC_TERMUX_MEMPALACE_PROOT_CLI="$fake_bin/proot-distro" PREFIX="$prefix" HOME="$home" \
  "$home/.local/bin/mempalace" mine "$home/path with spaces" 'literal;$(no-exec)' >/dev/null
python3 - "$proot_log" <<'PY' > "$TMP/wrapper-argv"
import pathlib,sys
tokens=pathlib.Path(sys.argv[1]).read_bytes().split(b"\0")
print("\n".join(x.decode() for x in tokens if x and x != b"\n"))
PY
ok "wrapper preserves spaces and shell metacharacters as literal argv" \
  'grep -qx "$home/path with spaces" "$TMP/wrapper-argv" && grep -qx '\''literal;$(no-exec)'\'' "$TMP/wrapper-argv"'
ok "wrapper uses isolated PRoot and the sqlite_exact resource bounds" \
  'grep -qx -- "--isolated" "$TMP/wrapper-argv" && grep -qx "MEMPALACE_BACKEND=sqlite_exact" "$TMP/wrapper-argv" && grep -qx "MEMPALACE_EMBEDDING_THREADS=1" "$TMP/wrapper-argv"'

installs_before="$(tr '\0' '\n' < "$proot_log" | grep -c '^install$' || true)"
run_install --apply --codex >/dev/null 2>&1; rc=$?
installs_after="$(tr '\0' '\n' < "$proot_log" | grep -c '^install$' || true)"
ok "repeat apply is idempotent and does not create another container" \
  '[ "$rc" = 0 ] && [ "$installs_before" = "$installs_after" ]'

out="$(run_install --status --json --codex 2>&1)"; rc=$?
ok "JSON status is body-free and reports the managed topology" \
  '[ "$rc" = 0 ] && jq -e '\'' .schema == "ccc.termux-mempalace.status.v1" and .container == "ccc-mempalace" and .backend == "sqlite_exact" and .source_files == 1 and .drawer_count == 1 and .palace_integrity == "ok" and .refresh_finished_at > 2 and .source_latest_mtime > 0 '\'' <<<"$out" >/dev/null && ! grep -q "literal;\|rollout one\|TEST_BODY_MUST_NOT_LEAK" <<<"$out"'

# #2511: exit 0 from the refresh is not "completed". The installer reads the
# refresh's own status record and reports what actually ran; metadata state
# stays "ready" (ccc_memory_probe requires it) with an initial_refresh field.
meta_json="$home/.nunchi/termux-mempalace/status.json"
refresh_status="$home/.nunchi/mempalace-refresh.status.json"
ok "a refresh that ran for this install is reported as completed" \
  'jq -e '\'' .state == "ready" and .initial_refresh == "ok" '\'' "$meta_json" >/dev/null'
busy_started=$(( $(date +%s) - 600 ))
printf '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"running","exit_code":-1,"started_at":%s,"finished_at":0}\n' "$busy_started" > "$refresh_status"
out="$(CCC_TEST_REFRESH_SKIP=1 run_install --apply --codex 2>&1)"; rc=$?
ok "a refresh skipped behind another refresh's lock is not reported as completed" \
  '[ "$rc" = 0 ] && grep -q "initial refresh skipped: another MemPalace refresh started before this install still holds the lock" <<<"$out" && ! grep -q "initial refresh completed" <<<"$out" && jq -e '\'' .enabled == true and .state == "ready" and .initial_refresh == "skipped-busy" '\'' "$meta_json" >/dev/null && jq -e --argjson s "$busy_started" '\'' .state == "running" and .started_at == $s '\'' "$refresh_status" >/dev/null'
printf '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":%s,"finished_at":%s}\n' "$busy_started" "$busy_started" > "$refresh_status"
out="$(CCC_TEST_REFRESH_SKIP=1 run_install --apply --codex 2>&1)"; rc=$?
ok "an older ok status does not stand in for a refresh this install never ran" \
  '[ "$rc" = 0 ] && grep -q "initial refresh not confirmed" <<<"$out" && ! grep -q "initial refresh completed" <<<"$out" && jq -e '\'' .initial_refresh == "unverified" '\'' "$meta_json" >/dev/null'
out="$(CCC_TEST_REFRESH_STATE=degraded run_install --apply --codex 2>&1)"; rc=$?
ok "a degraded initial refresh is reported as degraded" \
  '[ "$rc" = 0 ] && grep -q "initial refresh reported degraded" <<<"$out" && ! grep -q "initial refresh completed" <<<"$out" && jq -e '\'' .initial_refresh == "degraded" '\'' "$meta_json" >/dev/null'
printf off > "$home/.claude/state/nunchi.mode"
out="$(run_install --apply --codex 2>&1)"; rc=$?
printf on > "$home/.claude/state/nunchi.mode"
ok "a refresh skipped because nunchi mode is off is reported as skipped" \
  '[ "$rc" = 0 ] && grep -q "initial refresh skipped: nunchi mode is not on" <<<"$out" && ! grep -q "initial refresh completed" <<<"$out" && jq -e '\'' .initial_refresh == "skipped-mode-off" '\'' "$meta_json" >/dev/null'
out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "a later apply whose refresh runs reports completed again" \
  '[ "$rc" = 0 ] && grep -q "initial refresh completed" <<<"$out" && jq -e '\'' .initial_refresh == "ok" '\'' "$meta_json" >/dev/null'

printf '%s\n' \
  '*/10 * * * * feed # nunchi:#816' \
  '17 * * * * bash mempalace-refresh.sh codex source # nunchi:#816' \
  '7 8 * * 1 bench # nunchi:#816' \
  '1 2 * * * unrelated' > "$cron_store"
out="$(run_install --disable --codex 2>&1)"; rc=$?
ok "disable removes only live MemPalace wiring and preserves palace/container" \
  '[ "$rc" = 0 ] && ! grep -q "mempalace-refresh" "$cron_store" && grep -q "feed" "$cron_store" && grep -q "bench" "$cron_store" && grep -q "unrelated" "$cron_store" && [ -d "$container_root" ] && [ -x "$home/.nunchi/termux-mempalace/mempalace.disabled" ]'

out="$(CCC_TEST_REFRESH_RC=9 run_install --apply --codex 2>&1)"; rc=$?
ok "a failed initial refresh restores peer-facts-only wrapper state" \
  '[ "$rc" = 1 ] && grep -q "peer_facts-only wiring restored" <<<"$out" && [ ! -e "$home/.local/bin/mempalace" ] && [ -x "$home/.nunchi/termux-mempalace/mempalace.disabled" ] && jq -e '\'' .enabled == false and .state == "refresh-failed" '\'' "$home/.nunchi/termux-mempalace/status.json" >/dev/null'

unmanaged_container_home="$TMP/unmanaged-container-home"
unmanaged_root="$TMP/unmanaged-container-root"
mkdir -p "$unmanaged_container_home/.codex/sessions" "$unmanaged_root"
printf '{}\n' > "$unmanaged_container_home/.codex/sessions/a.jsonl"
out="$(env "${common_env[@]}" HOME="$unmanaged_container_home" \
  CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$unmanaged_root" \
  bash "$ROOT/scripts/install-termux-mempalace.sh" --apply --codex 2>&1)"; rc=$?
ok "an existing unmarked container is never modified" \
  '[ "$rc" = 2 ] && grep -q "refusing to modify unmanaged container" <<<"$out" && [ ! -e "$unmanaged_root/opt/ccc-mempalace" ]'

drift_home="$TMP/drift-home"
drift_root="$TMP/drift-root"
mkdir -p "$drift_home/.codex/sessions" "$drift_root/opt/ccc-mempalace"
chmod 700 "$drift_root/opt/ccc-mempalace"
printf '{}\n' > "$drift_home/.codex/sessions/a.jsonl"
printf '%s\n' 'ccc-node #867 managed container' > "$drift_root/opt/ccc-mempalace/.ccc-node-managed"
printf '%s\n' 'unexpected dependency set' > "$drift_root/opt/ccc-mempalace/requirements.lock"
chmod 600 "$drift_root/opt/ccc-mempalace/.ccc-node-managed" "$drift_root/opt/ccc-mempalace/requirements.lock"
out="$(env "${common_env[@]}" HOME="$drift_home" \
  CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$drift_root" \
  bash "$ROOT/scripts/install-termux-mempalace.sh" --apply --codex 2>&1)"; rc=$?
ok "dependency drift in a managed container is not mutated in place" \
  '[ "$rc" = 2 ] && grep -q "dependency lock drift" <<<"$out" && grep -qx "unexpected dependency set" "$drift_root/opt/ccc-mempalace/requirements.lock"'

version_out="$(env "${common_env[@]}" CCC_TERMUX_MEMPALACE_VERSION=3.6.0 \
  bash "$ROOT/scripts/install-termux-mempalace.sh" --status --codex 2>&1)"; rc=$?
ok "an unsupported MemPalace version pin fails closed" \
  '[ "$rc" = 2 ] && grep -q "unsupported MemPalace version: 3.6.0" <<<"$version_out"'

catchup_home="$TMP/catchup-home"
catchup_root="$TMP/catchup-root"
mkdir -p "$catchup_home/.codex/sessions" \
  "$catchup_home/.claude/hooks/nunchi" \
  "$catchup_root/opt/ccc-mempalace/venv/bin" \
  "$catchup_root/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-3.11.0.dist-info"
chmod 700 "$catchup_root/opt/ccc-mempalace"
printf '{}\n' > "$catchup_home/.codex/sessions/a.jsonl"
printf '%s\n' 'ccc-node #867 managed container' > "$catchup_root/opt/ccc-mempalace/.ccc-node-managed"
cp "$home/.claude/hooks/nunchi/mempalace-refresh.sh" \
  "$catchup_home/.claude/hooks/nunchi/mempalace-refresh.sh"
cat > "$catchup_root/opt/ccc-mempalace/venv/bin/mempalace" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod 700 "$catchup_root/opt/ccc-mempalace/venv/bin/mempalace"
sed 's/^mempalace==3.11.0$/mempalace==3.6.0/' \
  "$ROOT/scripts/termux-mempalace-requirements.txt" \
  > "$catchup_root/opt/ccc-mempalace/requirements.lock"
chmod 600 "$catchup_root/opt/ccc-mempalace/.ccc-node-managed" \
  "$catchup_root/opt/ccc-mempalace/requirements.lock"
out="$(env "${common_env[@]}" HOME="$catchup_home" \
  CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$catchup_root" \
  bash "$ROOT/scripts/install-termux-mempalace.sh" --apply --codex 2>&1)"; rc=$?
ok "a managed venv already on the pin may catch up a mempalace-only lock delta" \
  '[ "$rc" = 0 ] && cmp -s "$ROOT/scripts/termux-mempalace-requirements.txt" "$catchup_root/opt/ccc-mempalace/requirements.lock"'

stale_home="$TMP/stale-lock-home"
stale_root="$TMP/stale-lock-root"
mkdir -p "$stale_home/.codex/sessions" \
  "$stale_root/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-3.11.0.dist-info"
chmod 700 "$stale_root/opt/ccc-mempalace"
printf '{}\n' > "$stale_home/.codex/sessions/a.jsonl"
printf '%s\n' 'ccc-node #867 managed container' > "$stale_root/opt/ccc-mempalace/.ccc-node-managed"
printf '%s\n' 'unexpected dependency set' > "$stale_root/opt/ccc-mempalace/requirements.lock"
chmod 600 "$stale_root/opt/ccc-mempalace/.ccc-node-managed" \
  "$stale_root/opt/ccc-mempalace/requirements.lock"
out="$(env "${common_env[@]}" HOME="$stale_home" \
  CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$stale_root" \
  bash "$ROOT/scripts/install-termux-mempalace.sh" --apply --codex 2>&1)"; rc=$?
ok "matching dist-info does not overwrite an unrelated drifted lock" \
  '[ "$rc" = 2 ] && grep -q "dependency lock drift" <<<"$out" && grep -qx "unexpected dependency set" "$stale_root/opt/ccc-mempalace/requirements.lock"'

# In-place upgrade from a listed previous pin: venv on 3.10.0, installed lock =
# managed freeze with mempalace==3.10.0 (the only delta a pin bump produces).
mk_prev_root() { # <home> <root> <dist-version> <lock-mode: delta|drift>
  mkdir -p "$1/.codex/sessions" "$1/.claude/hooks/nunchi" \
    "$2/opt/ccc-mempalace/venv/bin" \
    "$2/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-$3.dist-info"
  chmod 700 "$2/opt/ccc-mempalace"
  printf '{}\n' > "$1/.codex/sessions/a.jsonl"
  printf '%s\n' 'ccc-node #867 managed container' > "$2/opt/ccc-mempalace/.ccc-node-managed"
  cp "$home/.claude/hooks/nunchi/mempalace-refresh.sh" "$1/.claude/hooks/nunchi/mempalace-refresh.sh"
  printf '#!/bin/sh\nexit 0\n' > "$2/opt/ccc-mempalace/venv/bin/mempalace"
  chmod 700 "$2/opt/ccc-mempalace/venv/bin/mempalace"
  if [ "$4" = delta ]; then
    sed "s/^mempalace==3.11.0$/mempalace==$3/" "$ROOT/scripts/termux-mempalace-requirements.txt" \
      > "$2/opt/ccc-mempalace/requirements.lock"
  else
    { sed "s/^mempalace==3.11.0$/mempalace==$3/" "$ROOT/scripts/termux-mempalace-requirements.txt"; echo 'extra-dep==1.0'; } \
      > "$2/opt/ccc-mempalace/requirements.lock"
  fi
  chmod 600 "$2/opt/ccc-mempalace/.ccc-node-managed" "$2/opt/ccc-mempalace/requirements.lock"
}

up_home="$TMP/upgrade-home"; up_root="$TMP/upgrade-root"
mk_prev_root "$up_home" "$up_root" 3.10.0 delta
out="$(env "${common_env[@]}" HOME="$up_home" \
  CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$up_root" CCC_TEST_CONTAINER_ROOT="$up_root" \
  bash "$ROOT/scripts/install-termux-mempalace.sh" --apply --codex 2>&1)"; rc=$?
ok "a managed venv on the previous pin upgrades in place when the lock delta is mempalace-only" \
  '[ "$rc" = 0 ] && grep -q "upgrading MemPalace 3.10.0 -> 3.11.0" <<<"$out" && cmp -s "$ROOT/scripts/termux-mempalace-requirements.txt" "$up_root/opt/ccc-mempalace/requirements.lock"'
ok "the upgraded venv carries exactly the new dist-info" \
  '[ -d "$up_root/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-3.11.0.dist-info" ] && [ ! -d "$up_root/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-3.10.0.dist-info" ]'
ok "the upgrade ran the guest pip install from the new input lock" \
  'grep -q "mempalace==3.11.0" "$up_root/opt/ccc-mempalace/requirements.input.lock" && grep -aq "pip install" "$proot_log"'

old_home="$TMP/oldpin-home"; old_root="$TMP/oldpin-root"
mk_prev_root "$old_home" "$old_root" 3.9.0 delta
out="$(env "${common_env[@]}" HOME="$old_home" \
  CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$old_root" CCC_TEST_CONTAINER_ROOT="$old_root" \
  bash "$ROOT/scripts/install-termux-mempalace.sh" --apply --codex 2>&1)"; rc=$?
ok "a venv on a pin that is not listed as upgradable still fails closed" \
  '[ "$rc" = 2 ] && grep -q "version drift" <<<"$out" && [ -d "$old_root/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-3.9.0.dist-info" ] && grep -q "^mempalace==3.9.0$" "$old_root/opt/ccc-mempalace/requirements.lock"'

dr_home="$TMP/upgrade-drift-home"; dr_root="$TMP/upgrade-drift-root"
mk_prev_root "$dr_home" "$dr_root" 3.10.0 drift
out="$(env "${common_env[@]}" HOME="$dr_home" \
  CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$dr_root" CCC_TEST_CONTAINER_ROOT="$dr_root" \
  bash "$ROOT/scripts/install-termux-mempalace.sh" --apply --codex 2>&1)"; rc=$?
ok "a previous-pin venv with an unrelated lock drift is not upgraded" \
  '[ "$rc" = 2 ] && grep -q "version drift" <<<"$out" && [ -d "$dr_root/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-3.10.0.dist-info" ] && grep -q "^extra-dep==1.0$" "$dr_root/opt/ccc-mempalace/requirements.lock"'

# #2509: Dependabot bumped the freeze after the node installed it. A lock that is
# byte-identical to a freeze the checkout shipped earlier is managed state. The
# check reads git history, so build a hermetic checkout (CI may be shallow).
hist_repo="$TMP/hist-repo"
mkdir -p "$hist_repo/scripts"
cp "$ROOT/scripts/install-termux-mempalace.sh" "$ROOT/scripts/termux-mempalace-wrapper.sh" "$hist_repo/scripts/"
old_freeze="$TMP/old-freeze.txt"
sed -e 's/^mempalace==3.11.0$/mempalace==3.10.0/' -e '1a\
zz-retired-security-pin==0.1' "$ROOT/scripts/termux-mempalace-requirements.txt" > "$old_freeze"
same_pin_freeze="$TMP/same-pin-freeze.txt"
sed '1a\
zz-same-pin-older-dep==0.1' "$ROOT/scripts/termux-mempalace-requirements.txt" > "$same_pin_freeze"
hgit() { git -C "$hist_repo" -c user.name=test -c user.email=test@example.invalid -c commit.gpgsign=false "$@"; }
hgit init -q
for f in "$old_freeze" "$same_pin_freeze" "$ROOT/scripts/termux-mempalace-requirements.txt"; do
  cp "$f" "$hist_repo/scripts/termux-mempalace-requirements.txt"
  hgit add -A && hgit commit -q -m freeze
done
chmod 700 "$hist_repo/scripts/install-termux-mempalace.sh" "$hist_repo/scripts/termux-mempalace-wrapper.sh"
run_hist() { # <home> <root>
  env "${common_env[@]}" HOME="$1" CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$2" CCC_TEST_CONTAINER_ROOT="$2" \
    bash "$hist_repo/scripts/install-termux-mempalace.sh" --apply --codex 2>&1
}

hu_home="$TMP/hist-up-home"; hu_root="$TMP/hist-up-root"
mk_prev_root "$hu_home" "$hu_root" 3.10.0 delta
cp "$old_freeze" "$hu_root/opt/ccc-mempalace/requirements.lock"
out="$(run_hist "$hu_home" "$hu_root")"; rc=$?
ok "a previous-pin venv on an older shipped freeze upgrades in place to the current freeze" \
  '[ "$rc" = 0 ] && grep -q "upgrading MemPalace 3.10.0 -> 3.11.0" <<<"$out" && cmp -s "$ROOT/scripts/termux-mempalace-requirements.txt" "$hu_root/opt/ccc-mempalace/requirements.lock" && [ -d "$hu_root/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-3.11.0.dist-info" ] && [ ! -d "$hu_root/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-3.10.0.dist-info" ]'

hs_home="$TMP/hist-same-home"; hs_root="$TMP/hist-same-root"
mk_prev_root "$hs_home" "$hs_root" 3.11.0 delta
cp "$same_pin_freeze" "$hs_root/opt/ccc-mempalace/requirements.lock"
: > "$proot_log"
out="$(run_hist "$hs_home" "$hs_root")"; rc=$?
ok "a venv already on the pin refreshes its dependencies from an older shipped freeze" \
  '[ "$rc" = 0 ] && grep -q "refreshing MemPalace 3.11.0 dependencies" <<<"$out" && grep -aq "pip install" "$proot_log" && cmp -s "$ROOT/scripts/termux-mempalace-requirements.txt" "$hs_root/opt/ccc-mempalace/requirements.lock"'

hm_home="$TMP/hist-mismatch-home"; hm_root="$TMP/hist-mismatch-root"
mk_prev_root "$hm_home" "$hm_root" 3.10.0 delta
cp "$same_pin_freeze" "$hm_root/opt/ccc-mempalace/requirements.lock"
out="$(run_hist "$hm_home" "$hm_root")"; rc=$?
ok "a shipped freeze whose mempalace pin disagrees with the installed dist-info still fails closed" \
  '[ "$rc" = 2 ] && grep -q "version drift" <<<"$out" && cmp -s "$same_pin_freeze" "$hm_root/opt/ccc-mempalace/requirements.lock"'

hd_home="$TMP/hist-drift-home"; hd_root="$TMP/hist-drift-root"
mk_prev_root "$hd_home" "$hd_root" 3.10.0 delta
{ cat "$old_freeze"; echo 'extra-dep==1.0'; } > "$hd_root/opt/ccc-mempalace/requirements.lock"
out="$(run_hist "$hd_home" "$hd_root")"; rc=$?
ok "a lock that matches no shipped freeze still fails closed with history available" \
  '[ "$rc" = 2 ] && grep -q "version drift" <<<"$out" && grep -q "^extra-dep==1.0$" "$hd_root/opt/ccc-mempalace/requirements.lock"'

nh_home="$TMP/no-history-home"; nh_root="$TMP/no-history-root"
mk_prev_root "$nh_home" "$nh_root" 3.10.0 delta
cp "$old_freeze" "$nh_root/opt/ccc-mempalace/requirements.lock"
nh_repo="$TMP/no-history-repo"
mkdir -p "$nh_repo/scripts"
cp "$hist_repo/scripts/"* "$nh_repo/scripts/"
out="$(env "${common_env[@]}" HOME="$nh_home" CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$nh_root" CCC_TEST_CONTAINER_ROOT="$nh_root" \
  GIT_CEILING_DIRECTORIES="$TMP" bash "$nh_repo/scripts/install-termux-mempalace.sh" --apply --codex 2>&1)"; rc=$?
ok "without git history an older shipped freeze is not trusted" \
  '[ "$rc" = 2 ] && grep -q "version drift" <<<"$out" && cmp -s "$old_freeze" "$nh_root/opt/ccc-mempalace/requirements.lock"'

# #2512: the real guest script runs against a fake pip with real `pip install -r`
# semantics — requested pins replace same-name packages, anything else stays.
# old_freeze carries zz-retired-security-pin, which the current freeze dropped.
guest_bin="$TMP/guest-bin"
mkdir -p "$guest_bin"
printf '#!/bin/sh\nexit 0\n' > "$guest_bin/apt-get"; chmod 700 "$guest_bin/apt-get"
mk_fake_pip() { # <root> <installed-freeze>
  cat > "$1/opt/ccc-mempalace/venv/bin/python" <<'SH'
#!/usr/bin/env bash
venv="$(cd "$(dirname "$0")/.." && pwd)"
state="$venv/.fake-site"; site="$venv/lib/python3.11/site-packages"
[ "$1 $2" = "-m pip" ] || exit 2
shift 2
printf '%s\n' "$*" >> "${CCC_TEST_PIP_LOG:?}"
case "$1" in
  install)
    req=""; prev=""
    for a in "$@"; do [ "$prev" = -r ] && req="$a"; prev="$a"; done
    [ -n "$req" ] || exit 0
    pin="$(sed -n 's/^mempalace==//p' "$req" | head -n 1)"
    find "$site" -maxdepth 1 -type d -name 'mempalace-*.dist-info' -exec rm -rf {} +
    mkdir -p "$site/mempalace-$pin.dist-info"
    [ "${CCC_TEST_PIP_FAIL_AFTER_PIN:-0}" = 1 ] && { echo "simulated pip failure after the pin moved" >&2; exit 1; }
    awk -F'==' 'NR==FNR { want[$1]=1; print; next } !($1 in want)' "$req" "$state" > "$state.new"
    mv -f "$state.new" "$state" ;;
  uninstall)
    shift
    names=""; for a in "$@"; do case "$a" in -*) ;; *) names="$names $a" ;; esac; done
    awk -F'==' -v drop="$names" 'BEGIN { n=split(drop, d, " "); for (i=1; i<=n; i++) gone[d[i]]=1 } !($1 in gone)' "$state" > "$state.new"
    mv -f "$state.new" "$state" ;;
  check) ;;
  freeze) cat "$state" ;;
  *) exit 2 ;;
esac
SH
  chmod 700 "$1/opt/ccc-mempalace/venv/bin/python"
  cp "$2" "$1/opt/ccc-mempalace/venv/.fake-site"
}
run_guest() { # <home> <root> [VAR=value...]
  local h="$1" r="$2"; shift 2
  env "${common_env[@]}" HOME="$h" CCC_TERMUX_MEMPALACE_CONTAINER_ROOT="$r" CCC_TEST_CONTAINER_ROOT="$r" \
    CCC_TEST_PROOT_GUEST_EXEC=1 CCC_TEST_GUEST_BIN="$guest_bin" CCC_TEST_PIP_LOG="$r/pip.log" "$@" \
    bash "$hist_repo/scripts/install-termux-mempalace.sh" --apply --codex 2>&1
}
cur_freeze="$ROOT/scripts/termux-mempalace-requirements.txt"
site_pkgs() { echo "$1/opt/ccc-mempalace/venv/lib/python3.11/site-packages"; }

gs_home="$TMP/guest-sync-home"; gs_root="$TMP/guest-sync-root"
mk_prev_root "$gs_home" "$gs_root" 3.10.0 delta
cp "$old_freeze" "$gs_root/opt/ccc-mempalace/requirements.lock"
mk_fake_pip "$gs_root" "$old_freeze"
out="$(run_guest "$gs_home" "$gs_root")"; rc=$?
ok "an upgrade removes a package the current freeze dropped and lands on the exact freeze" \
  '[ "$rc" = 0 ] && grep -q "removing packages the managed freeze no longer lists: zz-retired-security-pin" <<<"$out" && grep -qx "uninstall --disable-pip-version-check --no-input -y zz-retired-security-pin" "$gs_root/pip.log" && ! grep -q "^zz-retired-security-pin==" "$gs_root/opt/ccc-mempalace/venv/.fake-site" && cmp -s "$cur_freeze" "$gs_root/opt/ccc-mempalace/requirements.lock"'
ok "the synced upgrade keeps exactly the new dist-info" \
  '[ -d "$(site_pkgs "$gs_root")/mempalace-3.11.0.dist-info" ] && [ ! -d "$(site_pkgs "$gs_root")/mempalace-3.10.0.dist-info" ]'

gn_home="$TMP/guest-noop-home"; gn_root="$TMP/guest-noop-root"
mk_prev_root "$gn_home" "$gn_root" 3.10.0 delta
mk_fake_pip "$gn_root" "$gn_root/opt/ccc-mempalace/requirements.lock"
out="$(run_guest "$gn_home" "$gn_root")"; rc=$?
ok "a freeze that drops nothing uninstalls nothing" \
  '[ "$rc" = 0 ] && ! grep -q "^uninstall" "$gn_root/pip.log" && ! grep -q "removing packages" <<<"$out" && cmp -s "$cur_freeze" "$gn_root/opt/ccc-mempalace/requirements.lock"'

gf_home="$TMP/guest-fail-home"; gf_root="$TMP/guest-fail-root"
mk_prev_root "$gf_home" "$gf_root" 3.10.0 delta
cp "$old_freeze" "$gf_root/opt/ccc-mempalace/requirements.lock"
mk_fake_pip "$gf_root" "$old_freeze"
out="$(run_guest "$gf_home" "$gf_root" CCC_TEST_PIP_FAIL_AFTER_PIN=1)"; rc=$?
ok "a guest failure after pip moved the pin is reported, not a silent set -e exit" \
  '[ "$rc" = 2 ] && grep -q "guest dependency setup failed" <<<"$out" && jq -e '\'' .state == "guest-setup-failed" and .enabled == false '\'' "$gf_home/.nunchi/termux-mempalace/status.json" >/dev/null && cmp -s "$old_freeze" "$gf_root/opt/ccc-mempalace/requirements.lock" && [ -d "$(site_pkgs "$gf_root")/mempalace-3.11.0.dist-info" ]'
out="$(run_guest "$gf_home" "$gf_root")"; rc=$?
ok "a rerun on the same freeze resumes the interrupted upgrade" \
  '[ "$rc" = 0 ] && grep -q "resuming interrupted MemPalace 3.10.0 -> 3.11.0 upgrade" <<<"$out" && cmp -s "$cur_freeze" "$gf_root/opt/ccc-mempalace/requirements.lock" && ! grep -q "^zz-retired-security-pin==" "$gf_root/opt/ccc-mempalace/venv/.fake-site" && [ ! -d "$(site_pkgs "$gf_root")/mempalace-3.10.0.dist-info" ]'

gr_home="$TMP/guest-resume-guard-home"; gr_root="$TMP/guest-resume-guard-root"
mk_prev_root "$gr_home" "$gr_root" 3.11.0 delta
cp "$old_freeze" "$gr_root/opt/ccc-mempalace/requirements.lock"
cp "$old_freeze" "$gr_root/opt/ccc-mempalace/requirements.input.lock"
chmod 600 "$gr_root/opt/ccc-mempalace/requirements.input.lock"
mk_fake_pip "$gr_root" "$old_freeze"
out="$(run_guest "$gr_home" "$gr_root")"; rc=$?
ok "a venv on the pin with an old lock but no attempt on this freeze still fails closed" \
  '[ "$rc" = 2 ] && grep -q "dependency lock drift" <<<"$out" && ! grep -q resuming <<<"$out" && cmp -s "$old_freeze" "$gr_root/opt/ccc-mempalace/requirements.lock" && [ ! -e "$gr_root/pip.log" ]'

unmanaged="$TMP/unmanaged-home"
mkdir -p "$unmanaged/.codex/sessions" "$unmanaged/.local/bin"
printf '{}\n' > "$unmanaged/.codex/sessions/a.jsonl"
printf '#!/bin/sh\n' > "$unmanaged/.local/bin/mempalace"; chmod 700 "$unmanaged/.local/bin/mempalace"
out="$(env "${common_env[@]}" HOME="$unmanaged" bash "$ROOT/scripts/install-termux-mempalace.sh" --apply --codex 2>&1)"; rc=$?
ok "an unmanaged wrapper fails closed before replacement" \
  '[ "$rc" = 2 ] && grep -q "refusing to replace unmanaged" <<<"$out" && [ "$(cat "$unmanaged/.local/bin/mempalace")" = "#!/bin/sh" ]'

out="$(env -u TERMUX_VERSION -u CCC_TERMUX_MEMPALACE_FORCE \
  HOME="$home" PREFIX="$prefix" bash "$ROOT/scripts/install-termux-mempalace.sh" --status --codex 2>&1)"; rc=$?
ok "non-Termux execution is rejected" '[ "$rc" = 2 ] && grep -q "Termux runtime required" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
