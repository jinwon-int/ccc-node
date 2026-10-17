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

# --- fixture checkout: A (serving) -> B (same lock) -> C (lock changed) -> D ---
REPO="$HOME/ccc-node"
git init -q -b main "$REPO"
mkdir -p "$REPO/bridge"
printf 'pkg==1\n' > "$REPO/bridge/requirements.lock.txt"
printf '#!/usr/bin/env python3\n' > "$REPO/bridge/termux_prepare.py"
printf 'bridge/.env\n' > "$REPO/.gitignore"
git -C "$REPO" add -A && git -C "$REPO" commit -qm A
A="$(git -C "$REPO" rev-parse HEAD)"
echo b > "$REPO/b.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm B
B="$(git -C "$REPO" rev-parse HEAD)"
printf 'pkg==2\n' > "$REPO/bridge/requirements.lock.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm C
C="$(git -C "$REPO" rev-parse HEAD)"
echo d > "$REPO/d.txt"; git -C "$REPO" add -A && git -C "$REPO" commit -qm D
D="$(git -C "$REPO" rev-parse HEAD)"

# --- fake base python: plays termux_prepare.py (receipt) and reads receipts ---
FAKEBIN="$TMP/bin"; mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/python3" <<'PY'
#!/usr/bin/env bash
# Two personalities: `python3 - <receipt>` (stdin script reading a receipt) and
# `python3 -B .../termux_prepare.py --work-dir X ...` (the build).
if [ "${1:-}" = "-" ]; then
  exec /usr/bin/env python3 - "$2"
fi
printf '%s\n' "$*" >> "${FAKE_CALLS:?}"
printf 'VIRTUAL_ENV=%s PYTHONPATH=%s PATH=%s\n' "${VIRTUAL_ENV-unset}" "${PYTHONPATH-unset}" "$PATH" > "${FAKE_CALLS}.env"
work=""; while [ $# -gt 0 ]; do [ "$1" = "--work-dir" ] && work="$2"; shift; done
mkdir -p "$work"
if [ "${FAKE_PREPARE_FAIL:-0}" = 1 ]; then
  printf '{"status":"failed"}\n' > "$work/receipt.json"; echo "build failed" ; exit 1
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
ok "build reused the serving generation native wheels (lock unchanged)" 'grep -q -- "--reuse-wheels-from $SERV/job" "$FAKE_CALLS"'
ok "build passed --verify-reinstall and the job dir" 'grep -q -- "--work-dir $NEWGEN/job --verify-reinstall" "$FAKE_CALLS"'
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
ok "changed lock builds without wheel reuse" '[ "$rc" = 0 ] && ! grep -q -- "--reuse-wheels-from" <<<"$last"'
ok "pointer now at the C generation" '[ "$(git -C "$(readlink -e "$CN/bridge-current")/source" rev-parse HEAD)" = "$C" ]'

# 5) failing build (target D) -> non-zero, pointer unchanged, attempt retained
git -C "$REPO" checkout -q "$D"
ptr_before="$(readlink "$CN/bridge-current")"
out="$(FAKE_PREPARE_FAIL=1 run)"; rc=$?
ok "failed build exits 3 and keeps the pointer" '[ "$rc" = 3 ] && [ "$(readlink "$CN/bridge-current")" = "$ptr_before" ]'
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
out="$(CCC_TERMUX_PREPARE_PATH="/only/this:$FAKEBIN:/usr/bin:/bin" run)"; rc=$?
ok "CCC_TERMUX_PREPARE_PATH replaces the build PATH verbatim" '[ "$rc" = 0 ] && grep -q "PATH=/only/this:$FAKEBIN:/usr/bin:/bin$" "$FAKE_CALLS.env"'

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

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
