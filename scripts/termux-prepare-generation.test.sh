#!/usr/bin/env bash
# shellcheck disable=SC2034  # out/rc/NEWGEN/... are read via eval inside ok()
# Tests for termux-prepare-generation.sh — hermetic: fixture checkout, a fake
# base python that plays termux_prepare.py, a fake serving generation. No
# Termux, no network, nothing restarted.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/termux-prepare-generation.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export HOME="$TMP/home"; mkdir -p "$HOME/.claude/state"
unset CCC_SELF_UPDATE_TARGET_SHA CCC_SELF_UPDATE_REPO_DIR CCC_TERMUX_CCC_NODE_DIR CCC_TERMUX_PYTHON
unset CCC_TERMUX_PREPARE_TIMEOUT CCC_TERMUX_PREPARE_JOBS CCC_TERMUX_PREPARE_LOG PREFIX
unset CCC_TERMUX_PREPARE_ENV_FILE XDG_CONFIG_HOME   # the operator file resolves under the fixture HOME

# --- fixture checkout: A (serving) -> B (same lock) -> C (native pin changed) -> D ---
REPO="$HOME/ccc-node"
git init -q -b main "$REPO"
mkdir -p "$REPO/bridge" "$REPO/.github/requirements"
# Canonical-style hash locks (#2524): the reuse prefilter is the real
# bridge/termux_lock_subset.py at the target commit, so the fixture needs locks
# it can parse. mklocks <pydantic-core hash char> <certifi version> <cffi version>
h64() { printf '%064d' 0 | tr 0 "$1"; }
mklocks() {
  local n c v
  { echo "# fixture lock (pip-compile style)"
    for n in cryptography jiter pydantic-core rpds-py pyromark; do
      c=a; [ "$n" = pydantic-core ] && c="$1"
      printf '%s==1 \\\n    --hash=sha256:%s\n' "$n" "$(h64 "$c")"
    done
    printf 'certifi==%s \\\n    --hash=sha256:%s\n' "$2" "$(h64 c)"
  } > "$REPO/bridge/requirements.lock.txt"
  { for n in setuptools packaging cffi pycparser; do
      v=1; [ "$n" = cffi ] && v="$3"
      printf '%s==%s \\\n    --hash=sha256:%s\n' "$n" "$v" "$(h64 b)"
    done
    printf 'pytest==1 \\\n    --hash=sha256:%s\n' "$(h64 d)"
  } > "$REPO/.github/requirements/bridge-ci.txt"
}
mklocks a 1 1
cp "$HERE/../bridge/termux_lock_subset.py" "$REPO/bridge/termux_lock_subset.py"
printf '#!/usr/bin/env python3\n' > "$REPO/bridge/termux_prepare.py"
printf 'bridge/.env\n' > "$REPO/.gitignore"
git -C "$REPO" add -A && git -C "$REPO" commit -qm A
A="$(git -C "$REPO" rev-parse HEAD)"
echo b > "$REPO/b.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm B
B="$(git -C "$REPO" rev-parse HEAD)"
mklocks b 1 1; git -C "$REPO" add -A && git -C "$REPO" commit -qm C
C="$(git -C "$REPO" rev-parse HEAD)"
echo d > "$REPO/d.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm D
D="$(git -C "$REPO" rev-parse HEAD)"

# --- fake base python: plays termux_prepare.py (receipt) and reads receipts ---
FAKEBIN="$TMP/bin"; mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/python3" <<'PY'
#!/usr/bin/env bash
# Three personalities: `python3 - <receipt> <key>` (stdin script reading a
# receipt), `python3 -I -B - --compare-git ...` (the target's real lock-subset
# helper on stdin, #2524) and `python3 -B .../termux_prepare.py --work-dir X ...`
# (the build).
if [ "${1:-}" = "-" ]; then
  exec /usr/bin/env python3 - "$2" "$3"
fi
case " $* " in *" --compare-git "*) exec /usr/bin/env python3 "$@" ;; esac
printf '%s\n' "$*" >> "${FAKE_CALLS:?}"
printf 'VIRTUAL_ENV=%s PYTHONPATH=%s PATH=%s\n' "${VIRTUAL_ENV-unset}" "${PYTHONPATH-unset}" "$PATH" > "${FAKE_CALLS}.env"
reuse=0; case " $* " in *" --reuse-wheels-from "*) reuse=1 ;; esac
work=""; while [ $# -gt 0 ]; do [ "$1" = "--work-dir" ] && work="$2"; shift; done
mkdir -p "$work"
if [ "$reuse" = 1 ] && [ -n "${FAKE_REUSE_FAIL_REASON:-}" ]; then
  printf '{"status":"error","reason":"%s"}\n' "$FAKE_REUSE_FAIL_REASON" > "$work/receipt.json"; exit 1
fi
if [ "${FAKE_PREPARE_FAIL:-0}" = 1 ] || { [ "$reuse" = 0 ] && [ "${FAKE_FULL_FAIL:-0}" = 1 ]; }; then
  printf '{"status":"failed","reason":"native-wheels_timeout"}\n' > "$work/receipt.json"; echo "build failed" ; exit 1
fi
printf '{"status":"ready","work_dir":"%s"}\n' "$work" > "$work/receipt.json"
mkdir -p "$work/runtime/bin"
cat > "$work/runtime/bin/python" <<'SHIM'
#!/usr/bin/env bash
# runtime python shim: `-c "import aiohttp, nio"` succeeds only when the build included the Matrix extra
case "$*" in *aiohttp*) [ "${FAKE_MATRIX_OK:-0}" = 1 ] ;; *) exit 0 ;; esac
SHIM
chmod +x "$work/runtime/bin/python"
PY
chmod +x "$FAKEBIN/python3"
export CCC_TERMUX_PYTHON="$FAKEBIN/python3" FAKE_CALLS="$TMP/prepare.calls"
: > "$FAKE_CALLS"

# --- fake serving generation at A -------------------------------------------
CN="$HOME/.ccc-node"; PREP="$CN/preparations"; SERV="$PREP/main-${A:0:7}-20260101"
mkdir -p "$SERV/job" "$PREP"
git -C "$REPO" worktree add -q --detach "$SERV/source" "$A"
printf 'TELEGRAM_BOT_TOKEN=secret-token-marker\n' > "$SERV/source/bridge/.env"; chmod 600 "$SERV/source/bridge/.env"
printf '{"status":"ready"}\n' > "$SERV/job/receipt.json"
ln -sfn "preparations/$(basename "$SERV")" "$CN/bridge-current"

run() { bash "$SCRIPT" "$@" 2>&1; }

# 1) target == serving -> no-op, no build
git -C "$REPO" checkout -q "$A"
out="$(run)"; rc=$?
ok "already-serving target exits 0 without building" '[ "$rc" = 0 ] && grep -q "already serving" <<<"$out" && [ ! -s "$FAKE_CALLS" ]'

# 2) target B (lock unchanged) -> build with wheel reuse, inherit .env, repoint
git -C "$REPO" checkout -q "$B"
out="$(run)"; rc=$?
NEWGEN="$(ls -d "$PREP"/main-"${B:0:7}"-* 2>/dev/null | head -1)"
ok "new generation built and pointer moved" '[ "$rc" = 0 ] && [ -n "$NEWGEN" ] && [ "$(readlink -e "$CN/bridge-current")" = "$(readlink -e "$NEWGEN")" ]'
ok "source worktree is at the target" '[ "$(git -C "$NEWGEN/source" rev-parse HEAD)" = "$B" ]'
ok "build reused the serving generation native wheels (lock unchanged)" 'grep -q -- "--reuse-wheels-from $SERV/job" "$FAKE_CALLS" && grep -q "lock_subsets=equal" <<<"$out"'
ok "build passed --verify-reinstall and the job dir" 'grep -q -- "--work-dir $NEWGEN/job --verify-reinstall" "$FAKE_CALLS"'
ok "no operator file: the tool gets the 3300s default" 'grep -q -- "--timeout-seconds 3300 " "$FAKE_CALLS" && grep -q "timeout=3300s source=default" <<<"$out"'
ok ".env inherited from the serving generation, owner-only" '[ "$(stat -c %a "$NEWGEN/source/bridge/.env")" = 600 ] && grep -q secret-token-marker "$NEWGEN/source/bridge/.env"'
ok "previous pointer recorded" 'compgen -G "$CN/bridge-current.prev-*" >/dev/null && grep -q "$(basename "$SERV")" "$CN"/bridge-current.prev-*'
ok "generation dir is owner-private" '[ "$(stat -c %a "$NEWGEN")" = 700 ]'
ok "secret never reaches stdout or the log" '! grep -q secret-token-marker <<<"$out" && ! grep -q secret-token-marker "$HOME/.claude/state/termux-prepare.log"'

# 3) rerun at B -> already serving, no second build
calls_before="$(wc -l < "$FAKE_CALLS")"
out="$(run)"; rc=$?
ok "rerun is a no-op" '[ "$rc" = 0 ] && grep -q "already serving" <<<"$out" && [ "$(wc -l < "$FAKE_CALLS")" = "$calls_before" ]'

# 4) target C (lock changed) -> full build, no reuse
git -C "$REPO" checkout -q "$C"
out="$(run)"; rc=$?
last="$(tail -1 "$FAKE_CALLS")"
ok "changed native pin builds without wheel reuse" '[ "$rc" = 0 ] && ! grep -q -- "--reuse-wheels-from" <<<"$last" && grep -q "reuse=none; lock_subsets=different" <<<"$out"'
ok "pointer now at the C generation" '[ "$(git -C "$(readlink -e "$CN/bridge-current")/source" rev-parse HEAD)" = "$C" ]'

# 5) failing build (target D) -> non-zero, pointer unchanged, attempt retained
git -C "$REPO" checkout -q "$D"
ptr_before="$(readlink "$CN/bridge-current")"
out="$(FAKE_PREPARE_FAIL=1 run)"; rc=$?
ok "failed build exits 3 and keeps the pointer" '[ "$rc" = 3 ] && [ "$(readlink "$CN/bridge-current")" = "$ptr_before" ] && grep -q "reason=native-wheels_timeout" <<<"$out"'
ok "failed attempt is retained with its log" 'compgen -G "$PREP/main-${D:0:7}-*/prepare.log" >/dev/null'

# 6) a ready generation that exists but is not pointed at -> repoint only
GOOD="$(ls -d "$PREP"/main-"${D:0:7}"-* | head -1)"
printf '{"status":"ready"}\n' > "$GOOD/job/receipt.json"
calls_before="$(wc -l < "$FAKE_CALLS")"
out="$(run)"; rc=$?
ok "existing ready generation is repointed without rebuilding" '[ "$rc" = 0 ] && grep -q "pointer:" <<<"$out" && [ "$(wc -l < "$FAKE_CALLS")" = "$calls_before" ] && [ "$(readlink -e "$CN/bridge-current")" = "$(readlink -e "$GOOD")" ]'

# 7) --dry-run never builds or repoints; --status is read-only
echo e > "$REPO/e.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm E
calls_before="$(wc -l < "$FAKE_CALLS")"; ptr_before="$(readlink "$CN/bridge-current")"
out="$(run --dry-run)"; rc=$?
ok "dry-run reports the plan only" '[ "$rc" = 0 ] && grep -q "dry-run: would build" <<<"$out" && [ "$(wc -l < "$FAKE_CALLS")" = "$calls_before" ] && [ "$(readlink "$CN/bridge-current")" = "$ptr_before" ]'
out="$(run --status)"; rc=$?
ok "status prints serving and target heads" '[ "$rc" = 0 ] && grep -q "serving_head=${D:0:7}" <<<"$out"'

# 7b) operator-owned per-node prepare timeout file (#2487): env > file > default.
#     A bad file is ignored with a warning (never fatal, never sourced).
OPF="$HOME/.config/ccc-node/termux-prepare.env"; mkdir -p "$(dirname "$OPF")"
opfile() { rm -f "$OPF"; printf '%s\n' "$@" > "$OPF"; chmod 600 "$OPF"; }
dry() { run --dry-run; }
out="$(dry)"
ok "no operator file: default 3300s, source=default, no warning" 'grep -q "timeout=3300s source=default" <<<"$out" && ! grep -q warning <<<"$out"'
opfile '# slow node: full native build ~50 min' '' '  CCC_TERMUX_PREPARE_TIMEOUT=5400  '
out="$(dry)"
ok "valid 0600 file: its value is used, source=file" 'grep -q "timeout=5400s source=file" <<<"$out" && ! grep -q warning <<<"$out"'
out="$(run --status)"
ok "--status reports the effective timeout and source" 'grep -q "timeout=5400s source=file" <<<"$out"'
out="$(CCC_TERMUX_PREPARE_TIMEOUT=4000 dry)"
ok "explicit env overrides the file" 'grep -q "timeout=4000s source=env" <<<"$out"'
opfile 'CCC_TERMUX_PREPARE_TIMEOUT=6900'
ok "the 6900s cap itself is accepted" 'out="$(dry)"; grep -q "timeout=6900s source=file" <<<"$out"'
opfile 'CCC_TERMUX_PREPARE_TIMEOUT=6901'
out="$(dry)"; rc=$?
ok "too large (6901): default + warning, not fatal" '[ "$rc" = 0 ] && grep -q "timeout=3300s source=default" <<<"$out" && grep -q "warning: .*not an integer in 1..6900; file ignored" <<<"$out"'
for bad in 0 -5 1h 3600s "" 99999999; do
  opfile "CCC_TERMUX_PREPARE_TIMEOUT=$bad"
  ok "invalid value '$bad': default + warning" 'out="$(dry)"; grep -q "timeout=3300s source=default" <<<"$out" && grep -q "warning: .*CCC_TERMUX_PREPARE_TIMEOUT" <<<"$out"'
done
opfile 'CCC_TERMUX_PREPARE_TIMEOUT=5400' 'CCC_TERMUX_PREPARE_TIMEOUT=abc'
ok "any invalid assignment voids the file (no partial trust)" 'out="$(dry)"; grep -q "timeout=3300s source=default" <<<"$out"'
opfile 'CCC_TERMUX_PREPARE_TIMEOUT=$(touch '"$TMP"'/pwned)' "touch $TMP/pwned2" 'CCC_TERMUX_PREPARE_JOBS=2'
out="$(dry)"
ok "the file is parsed, never sourced or executed" '[ ! -e "$TMP/pwned" ] && [ ! -e "$TMP/pwned2" ] && grep -q "timeout=3300s source=default" <<<"$out" && grep -q "is not KEY=VALUE" <<<"$out"'
opfile 'CCC_TERMUX_PREPARE_TIMEOUT=5400' 'OTHER_KEY=1'
out="$(dry)"
ok "an unsupported key is ignored with a warning; the timeout still applies" 'grep -q "timeout=5400s source=file" <<<"$out" && grep -q "unsupported key OTHER_KEY" <<<"$out"'
opfile 'CCC_TERMUX_PREPARE_TIMEOUT=5400'
for m in 644 640 604 660 606; do
  chmod "$m" "$OPF"
  ok "mode $m (group/world bits): default + warning" 'out="$(dry)"; grep -q "timeout=3300s source=default" <<<"$out" && grep -q "has mode $m (group/world access)" <<<"$out"'
done
chmod 400 "$OPF"
ok "owner read-only 0400 is accepted" 'out="$(dry)"; grep -q "timeout=5400s source=file" <<<"$out"'
chmod 600 "$OPF"
STATBIN="$TMP/statbin"; mkdir -p "$STATBIN"
printf '#!/usr/bin/env bash\necho "%s 600"\n' "$(( $(id -u) + 4242 ))" > "$STATBIN/stat"; chmod +x "$STATBIN/stat"
ok "a file owned by another uid: default + warning" 'out="$(PATH="$STATBIN:$PATH" dry)"; grep -q "timeout=3300s source=default" <<<"$out" && grep -q "is owned by uid" <<<"$out"'
mv "$OPF" "$TMP/real-termux-prepare.env"; ln -s "$TMP/real-termux-prepare.env" "$OPF"
ok "a symlinked file is refused: default + warning" 'out="$(dry)"; grep -q "timeout=3300s source=default" <<<"$out" && grep -q "not a regular file" <<<"$out"'
rm -f "$OPF"
mkdir -p "$TMP/xdg/ccc-node"; printf 'CCC_TERMUX_PREPARE_TIMEOUT=4500\n' > "$TMP/xdg/ccc-node/termux-prepare.env"; chmod 600 "$TMP/xdg/ccc-node/termux-prepare.env"
ok "XDG_CONFIG_HOME is respected" 'out="$(XDG_CONFIG_HOME="$TMP/xdg" dry)"; grep -q "timeout=4500s source=file" <<<"$out"'
ok "a relative XDG_CONFIG_HOME is ignored (falls back to ~/.config)" 'out="$(XDG_CONFIG_HOME=rel dry)"; grep -q "timeout=3300s source=default" <<<"$out"'
ok "CCC_TERMUX_PREPARE_ENV_FILE overrides the path" 'out="$(CCC_TERMUX_PREPARE_ENV_FILE="$TMP/xdg/ccc-node/termux-prepare.env" dry)"; grep -q "timeout=4500s source=file" <<<"$out"'
ok "the operator file never moved the pointer or built anything" '[ "$(wc -l < "$FAKE_CALLS")" = "$calls_before" ] && [ "$(readlink "$CN/bridge-current")" = "$ptr_before" ]'

# 8) explicit target env (as self-update passes it) and precondition failures
out="$(CCC_SELF_UPDATE_TARGET_SHA=deadbeef run)"; rc=$?
ok "short/invalid target is refused" '[ "$rc" = 2 ]'
rm "$GOOD/source/bridge/.env"
# make GOOD the serving gen without an .env, then ask for a new target
out="$(run)"; rc=$?
ok "missing serving .env fails closed before building" '[ "$rc" = 2 ] && grep -q "no bridge/.env" <<<"$out"'


# 9) Matrix frontend configured: a generation without the Matrix extra is never promoted
mkdir -p "$HOME/.ccc-matrix"
git -C "$REPO" checkout -q "$D"
# the serving generation is GOOD (D) without .env now; restore an .env there so builds can inherit
printf 'TELEGRAM_BOT_TOKEN=secret-token-marker\n' > "$GOOD/source/bridge/.env"; chmod 600 "$GOOD/source/bridge/.env"
echo f > "$REPO/f.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm F
F="$(git -C "$REPO" rev-parse HEAD)"
ptr_before="$(readlink "$CN/bridge-current")"
ok "dry-run says the extra cannot be included by this tool" 'run --dry-run | grep -q "extra=none"'
out="$(run)"; rc=$?
ok "tool without --extra: build runs but the generation is refused (exit 4) and the pointer stays" \
  '[ "$rc" = 4 ] && grep -q "lacks the Matrix extra" <<<"$out" && [ "$(readlink "$CN/bridge-current")" = "$ptr_before" ]'
ok "no --extra flag was passed to a tool that does not support it" '! grep -q -- "--extra matrix" "$FAKE_CALLS"'
# the refused generation stays on disk (diagnosis) and a rerun does not promote it either
out="$(run)"; rc=$?
ok "an existing ready generation without the extra is not repointed on rerun" '[ "$rc" = 4 ] && [ "$(readlink "$CN/bridge-current")" = "$ptr_before" ]'

# 10) tool with --extra support + a build that includes the extra -> promoted with --extra matrix
printf '#!/usr/bin/env python3\n# parser.add_argument("--extra", action="append")\n' > "$REPO/bridge/termux_prepare.py"
git -C "$REPO" add -A && git -C "$REPO" commit -qm G
G="$(git -C "$REPO" rev-parse HEAD)"
out="$(FAKE_MATRIX_OK=1 run)"; rc=$?
ok "tool with --extra: built with --extra matrix and promoted" \
  '[ "$rc" = 0 ] && grep -q -- "--extra matrix" "$FAKE_CALLS" && [ "$(git -C "$(readlink -e "$CN/bridge-current")/source" rev-parse HEAD)" = "$G" ]'
ok "dry-run reports extra=--extra matrix when supported" 'echo h > "$REPO/h.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm H; run --dry-run | grep -q "extra=--extra matrix"'

# 11) Matrix frontend not configured -> no gate, no --extra
rm -rf "$HOME/.ccc-matrix"
: > "$FAKE_CALLS"
out="$(run)"; rc=$?
ok "without a Matrix frontend the build is promoted without the extra gate" '[ "$rc" = 0 ] && ! grep -q -- "--extra" "$FAKE_CALLS"'
ok "CCC_TERMUX_MATRIX_FRONTEND=1 forces the gate" 'echo i > "$REPO/i.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm I; CCC_TERMUX_MATRIX_FRONTEND=1 run >/dev/null 2>&1; [ "$?" = 4 ]'

# 10) the build never inherits a venv / generation runtime environment
echo j > "$REPO/j.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm J
rm -f "$FAKE_CALLS.env"
out="$(VIRTUAL_ENV="$TMP/venv" PYTHONPATH=/x/site PATH="$TMP/venv/bin:$PREP/main-deadbee-20260101/job/runtime/bin:$PATH" run)"; rc=$?
ok "termux_prepare.py runs with VIRTUAL_ENV/PYTHONPATH unset" '[ "$rc" = 0 ] && grep -q "^VIRTUAL_ENV=unset PYTHONPATH=unset " "$FAKE_CALLS.env"'
ok "venv and generation runtime bin entries are dropped from PATH, the rest is kept" '! grep -q "$TMP/venv/bin" "$FAKE_CALLS.env" && ! grep -q "/preparations/" "$FAKE_CALLS.env" && grep -q "PATH=.*/bin" "$FAKE_CALLS.env"'
echo k > "$REPO/k.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm K
opfile 'CCC_TERMUX_PREPARE_TIMEOUT=5400'
out="$(CCC_TERMUX_PREPARE_PATH="/only/this:$FAKEBIN:/usr/bin:/bin" run)"; rc=$?
rm -f "$OPF"
ok "CCC_TERMUX_PREPARE_PATH replaces the build PATH verbatim" '[ "$rc" = 0 ] && grep -q "PATH=/only/this:$FAKEBIN:/usr/bin:/bin$" "$FAKE_CALLS.env"'
ok "a real build passes the operator file value as --timeout-seconds" 'tail -1 "$FAKE_CALLS" | grep -q -- "--timeout-seconds 5400 " && grep -q "timeout=5400s source=file" <<<"$out"'

# 12) prune (#2362): serving, newest N rollback targets, live, <24h and
#     hand-made generations survive; only old script-built ones go.
gen_for() { ls -d "$PREP"/main-"${1:0:7}"-* 2>/dev/null | head -1; }
sha_of() { git -C "$REPO" log --all --format=%H --grep="^$1\$" -1; }
J="$(sha_of J)"; K="$(git -C "$REPO" rev-parse HEAD)"
GA="$SERV"; GB="$(gen_for "$B")"; GC="$(gen_for "$C")"; GD="$(gen_for "$D")"
GG="$(gen_for "$G")"; GJ="$(gen_for "$J")"; GK="$(gen_for "$K")"
HAND="$PREP/self-update-c1058258-20260917"; mkdir -p "$HAND/job"
for g in "$PREP"/*; do find "$g" -maxdepth 2 -exec touch -h -d '3 days ago' {} + 2>/dev/null; done
rm -f "$CN"/bridge-current.prev-*
printf 'preparations/%s\n' "$(basename "$GC")" > "$CN/bridge-current.prev-20990101T000000"
printf 'preparations/%s\n' "$(basename "$GG")" > "$CN/bridge-current.prev-20990101T000001"
printf 'preparations/%s\n' "$(basename "$GJ")" > "$CN/bridge-current.prev-20990101T000002"
( cd "$GB/source" && exec sleep 300 ) & LIVE_PID=$!
sleep 0.3
touch "$GD/prepare.log"
EXPECT_PRUNE="$(for g in "$PREP"/main-*; do case "$g" in "$GK"|"$GJ"|"$GG"|"$GB"|"$GD") ;; *) basename "$g" ;; esac; done | sort)"
EXPECT_N="$(grep -c . <<<"$EXPECT_PRUNE")"
ok "prune fixture: serving is K and every generation exists" \
  '[ "$(readlink -e "$CN/bridge-current")" = "$(readlink -e "$GK")" ] && ( for g in "$GA" "$GB" "$GC" "$GD" "$GG" "$GJ" "$GK" "$HAND"; do [ -d "$g" ] || exit 1; done )'

out="$(CCC_TERMUX_PREPARE_PRUNE=0 run)"; rc=$?
ok "CCC_TERMUX_PREPARE_PRUNE=0 removes nothing" '[ "$rc" = 0 ] && grep -q "prune: disabled" <<<"$out" && [ -d "$GA" ] && [ -d "$GC" ]'

out="$(run --dry-run)"; rc=$?
ok "dry-run lists exactly the old generations and removes nothing" \
  '[ "$rc" = 0 ] && [ "$(grep -o "would prune [^ ]*" <<<"$out" | awk "{print \$3}" | sort)" = "$EXPECT_PRUNE" ] && grep -qx "$(basename "$GA")" <<<"$EXPECT_PRUNE" && grep -qx "$(basename "$GC")" <<<"$EXPECT_PRUNE" && [ -d "$GA" ] && [ -d "$GC" ]'

echo l > "$REPO/l.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm L
out="$(FAKE_PREPARE_FAIL=1 run)"; rc=$?
ok "a failed build never prunes" '[ "$rc" = 3 ] && [ -d "$GA" ] && [ -d "$GC" ] && ! grep -q "prune:" <<<"$out"'
git -C "$REPO" checkout -q "$K"

# Dangling pointer + a target with no generation yet: nothing can be built
# (no serving .env to inherit) and nothing may be pruned.
echo m > "$REPO/m.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm M
mv "$CN/bridge-current" "$CN/bridge-current.saved"; ln -sfn preparations/missing "$CN/bridge-current"
out="$(run)"; rc=$?
ok "an unresolvable serving generation never prunes" '[ "$rc" = 2 ] && [ -d "$GA" ] && [ -d "$GC" ] && ! grep -q "prune:" <<<"$out"'
rm -f "$CN/bridge-current"; mv "$CN/bridge-current.saved" "$CN/bridge-current"
git -C "$REPO" checkout -q "$K"

out="$(run)"; rc=$?
ok "already-serving run prunes exactly the old generations" \
  '[ "$rc" = 0 ] && [ "$EXPECT_N" -ge 2 ] && grep -q "prune: removed $EXPECT_N generation" <<<"$out" && ( for n in $EXPECT_PRUNE; do [ -e "$PREP/$n" ] && exit 1; done; exit 0 )'
ok "serving and the newest two rollback targets survive" '[ -d "$GK" ] && [ -d "$GJ" ] && [ -d "$GG" ]'
ok "a generation used by a live process survives" '[ -d "$GB" ] && grep -q "keep $(basename "$GB") (referenced by a live process)" <<<"$out"'
ok "a generation changed in the last 24h survives" '[ -d "$GD" ]'
ok "hand-made (non main-*) generations are never candidates" '[ -d "$HAND" ]'
ok "pruned worktrees are gone from git" '! git -C "$REPO" worktree list | grep -qF -e "$GA/source" -e "$GC/source"'
kill "$LIVE_PID" 2>/dev/null; wait "$LIVE_PID" 2>/dev/null

out="$(CCC_TERMUX_GENERATIONS_KEEP=0 run --dry-run)"; rc=$?
ok "KEEP=0 is clamped to 1: the newest rollback target is still kept" \
  '[ "$rc" = 0 ] && ! grep -q "would prune $(basename "$GJ")" <<<"$out"'

# 13) #2524: the reuse prefilter keys off the native + build-tool lock subsets
#     (the target's termux_lock_subset.py), and a refused reuse retries once
#     as a full build. Isolated ccc-node dir so the sections above are untouched.
CN2="$TMP/cn2524"; PREP2="$CN2/preparations"
git -C "$REPO" checkout -q "$K"
commit_at() { # <base> <msg> <cmd...> — commit on a detached base, print the sha
  local base="$1" msg="$2"; shift 2
  git -C "$REPO" checkout -q "$base" && "$@" && git -C "$REPO" add -A && git -C "$REPO" commit -qm "$msg" && git -C "$REPO" rev-parse HEAD
}
S0="$(commit_at "$K" S0 mklocks a 1 1)"
P_PURE="$(commit_at "$S0" P_PURE mklocks a 2 1)"          # certifi bump only
P_PURE2="$(commit_at "$S0" P_PURE2 mklocks a 3 1)"        # another pure bump
P_PURE3="$(commit_at "$S0" P_PURE3 mklocks a 4 1)"
P_PURE4="$(commit_at "$S0" P_PURE4 mklocks a 5 1)"
P_PURE5="$(commit_at "$S0" P_PURE5 mklocks a 6 1)"
P_NATIVE="$(commit_at "$S0" P_NATIVE mklocks e 1 1)"      # pydantic-core hash change
P_TOOLS="$(commit_at "$S0" P_TOOLS mklocks a 1 2)"        # cffi (build tool) pin change
P_NOHELPER="$(commit_at "$S0" P_NOHELPER git -C "$REPO" rm -q bridge/termux_lock_subset.py)"
P_BROKEN="$(commit_at "$S0" P_BROKEN sh -c "printf 'pkg>=1\n' > '$REPO/bridge/requirements.lock.txt'")"
S2="$PREP2/main-${S0:0:7}-20260101"
mkdir -p "$S2/job"
git -C "$REPO" worktree add -q --detach "$S2/source" "$S0"
printf 'TELEGRAM_BOT_TOKEN=secret-token-marker\n' > "$S2/source/bridge/.env"; chmod 600 "$S2/source/bridge/.env"
printf '{"status":"ready"}\n' > "$S2/job/receipt.json"
ln -sfn "preparations/$(basename "$S2")" "$CN2/bridge-current"
run2() { CCC_TERMUX_CCC_NODE_DIR="$CN2" CCC_TERMUX_PREPARE_PRUNE=0 bash "$SCRIPT" "$@" 2>&1; }
dry2() { run2 --dry-run --target "$1"; }

out="$(dry2 "$P_PURE")"
ok "pure-package-only lock change: prefilter passes --reuse-wheels-from" 'grep -q "reuse=--reuse-wheels-from $S2/job lock_subsets=equal" <<<"$out"'
out="$(dry2 "$P_NATIVE")"
ok "native hash change: reuse=none (lock_subsets=different)" 'grep -q "reuse=none lock_subsets=different" <<<"$out"'
out="$(dry2 "$P_TOOLS")"
ok "build-tool pin change in bridge-ci.txt: reuse=none" 'grep -q "reuse=none lock_subsets=different" <<<"$out"'
out="$(dry2 "$P_NOHELPER")"
ok "target without the lock-subset helper: undecided, reuse=none" 'grep -q "reuse=none lock_subsets=undecided:no-helper-at-target" <<<"$out"'
out="$(dry2 "$P_BROKEN")"
ok "unparseable target lock: undecided, reuse=none" 'grep -q "reuse=none lock_subsets=undecided:unsupported_lock_syntax" <<<"$out"'
printf '{"status":"error"}\n' > "$S2/job/receipt.json"
out="$(dry2 "$P_PURE")"
ok "serving receipt not ready: reuse=none" 'grep -q "reuse=none lock_subsets=serving-receipt-not-ready" <<<"$out"'
printf '{"status":"ready"}\n' > "$S2/job/receipt.json"
ok "the prefilter never ran the build tool" '! grep -q "$CN2" "$FAKE_CALLS"'

# real build: pure change reuses and is promoted
out="$(run2 --target "$P_PURE")"; rc=$?
ok "pure change: one build, with reuse, promoted" '[ "$rc" = 0 ] && [ "$(grep -c "$PREP2/main-${P_PURE:0:7}" "$FAKE_CALLS")" = 1 ] && grep "$PREP2/main-${P_PURE:0:7}" "$FAKE_CALLS" | grep -q -- "--reuse-wheels-from $S2/job" && [ "$(git -C "$(readlink -e "$CN2/bridge-current")/source" rev-parse HEAD)" = "$P_PURE" ]'
SERV2="$(readlink -e "$CN2/bridge-current")"

# mismatch fallback: the tool refuses the reuse -> kept for diagnosis, one full build, promoted
for reason in reuse_lock_subset_sha256_mismatch reuse_lock_subset_unavailable; do
  case "$reason" in *mismatch) T="$P_PURE2" ;; *) T="$P_PURE3" ;; esac
  out="$(FAKE_REUSE_FAIL_REASON="$reason" run2 --target "$T")"; rc=$?
  gens="$(ls -d "$PREP2"/main-"${T:0:7}"-* 2>/dev/null)"
  first="$(grep "$PREP2/main-${T:0:7}" "$FAKE_CALLS" | head -1)"; second="$(grep "$PREP2/main-${T:0:7}" "$FAKE_CALLS" | sed -n 2p)"
  ok "$reason: retried once as a full build and promoted" '[ "$rc" = 0 ] && grep -q -- "--reuse-wheels-from" <<<"$first" && [ -n "$second" ] && ! grep -q -- "--reuse-wheels-from" <<<"$second" && [ "$(grep -c "$PREP2/main-${T:0:7}" "$FAKE_CALLS")" = 2 ] && grep -q "reuse refused (reason=$reason).*retrying once as a full build" <<<"$out"'
  served="$(readlink -e "$CN2/bridge-current")"
  refused="$(grep -vxF "$served" <<<"$gens")"
  ok "$reason: refused attempt kept for diagnosis, pointer at the ready retry" '[ "$(grep -c . <<<"$gens")" = 2 ] && [ "$(grep -c . <<<"$refused")" = 1 ] && grep -q "\"reason\":\"$reason\"" "$refused/job/receipt.json" && grep -q "\"status\":\"ready\"" "$served/job/receipt.json" && [ "$(git -C "$served/source" rev-parse HEAD)" = "$T" ]'
  ln -sfn "preparations/$(basename "$SERV2")" "$CN2/bridge-current"   # back to the reuse source for the next case
done

ptr_before="$(readlink "$CN2/bridge-current")"
out="$(FAKE_REUSE_FAIL_REASON=reuse_toolchain_mismatch FAKE_FULL_FAIL=1 run2 --target "$P_PURE4")"; rc=$?
ok "retry that also fails: exit 3, pointer unchanged, both attempts kept, no third build" \
  '[ "$rc" = 3 ] && grep -q "full-build retry failed.*reason=native-wheels_timeout" <<<"$out" && [ "$(readlink "$CN2/bridge-current")" = "$ptr_before" ] && [ "$(ls -d "$PREP2"/main-"${P_PURE4:0:7}"-* | grep -c .)" = 2 ] && [ "$(grep -c "$PREP2/main-${P_PURE4:0:7}" "$FAKE_CALLS")" = 2 ]'
out="$(FAKE_REUSE_FAIL_REASON=reused_wheels_not_used run2 --target "$P_PURE5")"; rc=$?
ok "a non-mismatch reuse failure is not retried" '[ "$rc" = 3 ] && grep -q "reason=reused_wheels_not_used" <<<"$out" && [ "$(grep -c "$PREP2/main-${P_PURE5:0:7}" "$FAKE_CALLS")" = 1 ]'
P_PURE6="$(commit_at "$S0" P_PURE6 mklocks a 7 1)"
out="$(CCC_TERMUX_PREPARE_TIMEOUT=59 FAKE_REUSE_FAIL_REASON=reuse_lock_subset_sha256_mismatch run2 --target "$P_PURE6")"; rc=$?
ok "no retry when less than 60 s of the budget remain" '[ "$rc" = 3 ] && [ "$(grep -c "$PREP2/main-${P_PURE6:0:7}" "$FAKE_CALLS")" = 1 ]'
out="$(FAKE_REUSE_FAIL_REASON=reuse_lock_subset_sha256_mismatch run2 --target "$P_NATIVE")"; rc=$?
ok "a build without reuse is never retried (mismatch reason irrelevant)" '[ "$rc" = 0 ] && [ "$(grep -c "$PREP2/main-${P_NATIVE:0:7}" "$FAKE_CALLS")" = 1 ]'
git -C "$REPO" checkout -q "$K"

# 14) #2621: a ready generation built without the Matrix extra must not be reused on a Matrix node
export CCC_TERMUX_MATRIX_FRONTEND=1
printf '#!/usr/bin/env python3\n# parser.add_argument("--extra", action="append")\n' > "$REPO/bridge/termux_prepare.py"
date +%s%N > "$REPO/n2621.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm J2621
J="$(git -C "$REPO" rev-parse HEAD)"
ptr_before="$(readlink "$CN/bridge-current")"
out="$(FAKE_MATRIX_OK=0 run)"; rc=$?   # the extra did not land inside the build: receipt ready, gate refuses
STALE="$(ls -d "$PREP"/main-"${J:0:7}"-* | head -1)"
ok "extra-less build for J is refused (exit 4) and kept on disk" '[ "$rc" = 4 ] && [ -d "$STALE" ] && [ "$(readlink "$CN/bridge-current")" = "$ptr_before" ]'
# pin the stale generation's runtime to "no extra" whatever FAKE_MATRIX_OK says later
printf '#!/usr/bin/env bash\ncase "$*" in *aiohttp*) exit 1 ;; *) exit 0 ;; esac\n' > "$STALE/job/runtime/bin/python"; chmod +x "$STALE/job/runtime/bin/python"
calls_before="$(grep -c "$PREP/main-${J:0:7}" "$FAKE_CALLS")"
out="$(FAKE_MATRIX_OK=1 run)"; rc=$?
ok "rerun skips the stale extra-less generation, builds a fresh one with the extra and promotes it" \
  '[ "$rc" = 0 ] && grep -q "not reusing it" <<<"$out" && [ "$(grep -c "$PREP/main-${J:0:7}" "$FAKE_CALLS")" = "$((calls_before + 1))" ] && [ "$(basename "$(readlink -e "$CN/bridge-current")")" != "$(basename "$STALE")" ] && [ "$(git -C "$(readlink -e "$CN/bridge-current")/source" rev-parse HEAD)" = "$J" ]'
ok "the stale extra-less generation stays for diagnosis" '[ -d "$STALE" ]'

# 15) #2621: when the --extra probe cannot read the tool (git show fails) nothing is built
git -C "$REPO" rm -q bridge/termux_prepare.py && git -C "$REPO" commit -qm K2
ptr_before="$(readlink "$CN/bridge-current")"; calls_before="$(wc -l < "$FAKE_CALLS")"
out="$(run)"; rc=$?
ok "unreadable termux_prepare.py at the target: exit 3, no build attempted, pointer unchanged" \
  '[ "$rc" = 3 ] && grep -q "git show failed twice" <<<"$out" && [ "$(wc -l < "$FAKE_CALLS")" = "$calls_before" ] && [ "$(readlink "$CN/bridge-current")" = "$ptr_before" ]'
unset CCC_TERMUX_MATRIX_FRONTEND

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
