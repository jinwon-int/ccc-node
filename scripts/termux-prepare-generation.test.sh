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

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
