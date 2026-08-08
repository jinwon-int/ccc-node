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
  mkdir -p "$root/opt/ccc-mempalace/venv/bin" "$root/opt/ccc-mempalace/venv/lib/python3.11/site-packages/mempalace-3.6.0.dist-info"
  cat > "$root/opt/ccc-mempalace/venv/bin/mempalace" <<'EOF'
#!/bin/sh
exit 0
EOF
  chmod 700 "$root/opt/ccc-mempalace/venv/bin/mempalace"
  printf '%s\n' managed > "$root/opt/ccc-mempalace/.ccc-node-managed"
  chmod 600 "$root/opt/ccc-mempalace/.ccc-node-managed"
  exit 0
fi
if [ "${1:-}" = login ] && [ -f "${CCC_TEST_CONTAINER_ROOT:?}/opt/ccc-mempalace/requirements.input.lock" ]; then
  cp "${CCC_TEST_CONTAINER_ROOT:?}/opt/ccc-mempalace/requirements.input.lock" \
    "${CCC_TEST_CONTAINER_ROOT:?}/opt/ccc-mempalace/requirements.lock"
  chmod 600 "${CCC_TEST_CONTAINER_ROOT:?}/opt/ccc-mempalace/requirements.lock"
fi
case " $* " in
  *" /opt/ccc-mempalace/venv/bin/mempalace --version "*) echo 'mempalace 3.6.0' ;;
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
rc="${CCC_TEST_REFRESH_RC:-0}"
state=ok
[ "$rc" = 0 ] || state=error
printf '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"%s","state":"%s","exit_code":%s,"started_at":1,"finished_at":2}\n' "$1" "$state" "$rc" > "$HOME/.nunchi/mempalace-refresh.status.json"
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
  '[ "$rc" = 0 ] && jq -e '\'' .schema == "ccc.termux-mempalace.status.v1" and .container == "ccc-mempalace" and .backend == "sqlite_exact" and .source_files == 1 and .drawer_count == 1 and .palace_integrity == "ok" and .refresh_finished_at == 2 and .source_latest_mtime > 0 '\'' <<<"$out" >/dev/null && ! grep -q "literal;\|rollout one\|TEST_BODY_MUST_NOT_LEAK" <<<"$out"'

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
