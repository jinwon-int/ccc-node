#!/usr/bin/env bash
# Tests for termux-bridge-generation.sh (#2175 C) against fake generations:
# stub start.sh / runtime python / sv / termux_prepare.py record their calls,
# so pointer resolution, env scrubbing, promote success/rollback/gate/busy,
# Matrix follow and prepare (reuse refusal → full-build fallback, .env link)
# are checked without a real bridge.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT_REPO="$(cd "$HERE/.." && pwd)"
SUT="$HERE/termux-bridge-generation.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT_REPO/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 not available"; echo "PASS=0 FAIL=0"; exit 0; }
command -v flock >/dev/null 2>&1 || { echo "SKIP: flock not available"; echo "PASS=0 FAIL=0"; exit 0; }

TMP="$(ccc_test_tmpdir)" || exit 1
trap '[ -n "${KEEP:-}" ] || rm -rf "$TMP"' EXIT
ROOT="$TMP/root"; CALLS="$TMP/calls"; BIN="$TMP/bin"
mkdir -p "$ROOT/preparations" "$BIN"

# Stubs are exec'd directly; write_exec_stub resolves the real bash for the
# shebang (Termux has no /usr/bin/env without termux-exec).
# runtime python stub: answers the serving gate from $TMP/gate-<gen>, else defers to python3
write_exec_stub "$TMP/runtime-python" <<'EOF'
for a in "$@"; do
  case "$a" in */prepared_runtime.py)
    gen="$(basename "$(dirname "$(dirname "$(dirname "$a")")")")"
    printf '{"schema":"ccc.prepared-runtime.v1","status":"%s"}\n' "$(cat "$STUB_DIR/gate-$gen" 2>/dev/null || echo ready)"
    exit 0 ;;
  esac
done
exec python3 "$@"
EOF

# start.sh stub: logs args + channel env, exits with $TMP/start-rc (default 0)
cat > "$TMP/start.sh" <<'EOF'
#!/usr/bin/env bash
echo "start $* | CCC_CHANNEL=${CCC_CHANNEL:-} CCC_MATRIX_X=${CCC_MATRIX_X:-}" >> "$STUB_DIR/calls"
exit "$(cat "$STUB_DIR/start-rc" 2>/dev/null || echo 0)"
EOF

mk_gen() { # <name>
  local g="$ROOT/preparations/$1"
  mkdir -p "$g/source/bridge" "$g/job/runtime/bin"
  cp "$TMP/start.sh" "$g/source/bridge/start.sh"
  : > "$g/source/bridge/prepared_runtime.py"
  ln -s "$TMP/runtime-python" "$g/job/runtime/bin/python"
  echo '{"status":"ready"}' > "$g/job/receipt.json"
}
mk_gen genA; mk_gen genB
# genB serves a retry job through the optional `serving` link
mv "$ROOT/preparations/genB/job" "$ROOT/preparations/genB/job2"
ln -s job2 "$ROOT/preparations/genB/serving"
ln -s preparations/genA "$ROOT/bridge-current"

health() { # <file> <source-dir> <active>
  printf '{"workload":{"active_requests":%s,"waiting_for_turn":0,"turn_occupancy":{"state":"%s"}},"runtime_generation":{"source_dir":"%s","source_git":{"head":"abc"}}}\n' \
    "$3" "$([ "$3" = 0 ] && echo idle || echo busy)" "$2" > "$1"
}
TG="$TMP/tg-health.json"; MX="$TMP/mx-health.json"
health "$TG" "$ROOT/preparations/genA/source/bridge" 0
health "$MX" "$ROOT/preparations/genA/source/bridge" 0

# sv stub: records and makes the Matrix health report the pointer's generation
write_exec_stub "$BIN/sv" <<'EOF'
echo "sv $*" >> "$STUB_DIR/calls"
src="$(readlink -e "$STUB_ROOT/bridge-current")/source/bridge"
printf '{"workload":{"active_requests":0},"runtime_generation":{"source_dir":"%s"}}\n' "$src" > "$STUB_MX"
EOF
mkdir -p "$TMP/service/ccc-matrix-bridge"

PY3="$(command -v python3)"
export STUB_DIR="$TMP" STUB_ROOT="$ROOT" STUB_MX="$MX"
export CCC_NODE_STATE_DIR="$ROOT" CCC_BRIDGE_PROJECT_PATH="$TMP/project" \
  CCC_TERMUX_BASE_PYTHON="$PY3" CCC_BRIDGE_HEALTH_FILE="$TG" \
  CCC_MATRIX_HEALTH_FILE="$MX" CCC_SV="$BIN/sv" SVDIR="$TMP/service" \
  CCC_BRIDGE_IDLE_POLL_SECONDS=0 CCC_MATRIX_FOLLOW_POLL_SECONDS=0 CCC_MATRIX_FOLLOW_CHECKS=3

run() { RC=0; OUT="$(bash "$SUT" "$@" 2>"$TMP/err")" || RC=$?; }
pointer() { readlink "$ROOT/bridge-current"; }

# ── resolution ───────────────────────────────────────────────────────────────
run print-source; okc "$RC" 0 "print-source rc"
ok "print-source is the real source" '[ "$OUT" = "$ROOT/preparations/genA/source" ]'
run --print-job; ok "legacy --print-job alias resolves job" '[ "$OUT" = "$ROOT/preparations/genA/job" ]'
mv "$ROOT/bridge-current" "$ROOT/bc.off"; run print-job; okc "$RC" 3 "missing pointer → 3"
mv "$ROOT/bc.off" "$ROOT/bridge-current"
run nope; okc "$RC" 2 "unknown subcommand → 2"

# ── launch scrubs channel env and selects the serving job ────────────────────
: > "$CALLS"
RC=0; CCC_CHANNEL=matrix CCC_MATRIX_X=1 bash "$SUT" launch --path /p --status 2>/dev/null || RC=$?
okc "$RC" 0 "launch rc"
ok "launch passes prepared runtime + args" 'grep -q "^start --prepared-runtime $ROOT/preparations/genA/job --path /p --status" "$CALLS"'
ok "launch drops CCC_CHANNEL and CCC_MATRIX_*" 'grep -q "CCC_CHANNEL= CCC_MATRIX_X=$" "$CALLS"'
: > "$CALLS"
run --path /p --restart -d; okc "$RC" 0 "bare start.sh options rc"
ok "bare start.sh options mean launch (old wrapper contract)" 'grep -q "^start --prepared-runtime $ROOT/preparations/genA/job --path /p --restart -d" "$CALLS"'

# ── promote: dry run changes nothing ─────────────────────────────────────────
: > "$CALLS"
run promote genB --dry-run; okc "$RC" 0 "dry-run rc"
ok "dry-run plan names both generations" '[[ "$OUT" == *genA*genB*recovery* ]]'
ok "dry-run leaves pointer" '[ "$(pointer)" = preparations/genA ]'
ok "dry-run never starts" '[ ! -s "$CALLS" ]'

# ── promote: gate refusal ────────────────────────────────────────────────────
echo error > "$TMP/gate-genB"
run promote genB --no-matrix; okc "$RC" 6 "gate refused → 6"
ok "gate refusal leaves pointer" '[ "$(pointer)" = preparations/genA ]'
ok "gate report saved" 'grep -q "\"status\":\"error\"" "$ROOT/preparations/genB/prepared-gate.json"'
rm -f "$TMP/gate-genB"

# ── promote: busy Telegram ───────────────────────────────────────────────────
health "$TG" "$ROOT/preparations/genA/source/bridge" 2
: > "$CALLS"
run promote genB --no-matrix --wait-idle 0; okc "$RC" 75 "busy → 75"
ok "busy leaves pointer and does not start" '[ "$(pointer)" = preparations/genA ] && [ ! -s "$CALLS" ]'
health "$TG" "$ROOT/preparations/genA/source/bridge" 0

# ── promote: candidate fails → pointer rolled back, rc propagated ────────────
echo 7 > "$TMP/start-rc"; : > "$CALLS"
run promote genB --no-matrix; okc "$RC" 7 "failed restart rc propagated"
ok "failed restart rolls the pointer back" '[ "$(pointer)" = preparations/genA ]'
ok "restart carried recovery to previous generation" \
  'grep -q -- "--restart -d --recovery-source $ROOT/preparations/genA/source/bridge --recovery-runtime $ROOT/preparations/genA/job" "$CALLS"'
ok "candidate job came from serving link" 'grep -q -- "--prepared-runtime $ROOT/preparations/genB/job2" "$CALLS"'
rm -f "$TMP/start-rc"

# ── promote: success + detached Matrix follow ────────────────────────────────
: > "$CALLS"
run promote genB --matrix --matrix-wait 5; okc "$RC" 0 "promote rc"
ok "pointer swapped (relative)" '[ "$(pointer)" = preparations/genB ]'
for _ in $(seq 1 50); do grep -q "^sv restart ccc-matrix-bridge" "$CALLS" && break; sleep 0.1; done
ok "matrix follower restarted the runit service" 'grep -q "^sv restart ccc-matrix-bridge" "$CALLS"'
for _ in $(seq 1 50); do ls "$ROOT"/logs/matrix-follow-*.log >/dev/null 2>&1 && grep -q "now serves" "$ROOT"/logs/matrix-follow-*.log && break; sleep 0.1; done
ok "matrix follower verified the new generation" 'grep -q "matrix now serves $ROOT/preparations/genB/source/bridge" "$ROOT"/logs/matrix-follow-*.log'

# already serving: no restart, follow is a no-op
health "$TG" "$ROOT/preparations/genB/source/bridge" 0
: > "$CALLS"
run promote genB --no-matrix; okc "$RC" 0 "re-promote rc"
ok "re-promote of serving generation does not restart" '[ ! -s "$CALLS" ]'
run promote genB --dry-run; ok "dry-run says already serving" '[[ "$OUT" == *"already serving (no restart)"* ]]'
run matrix-follow --wait 0; okc "$RC" 0 "matrix-follow no-op rc"
ok "matrix-follow no-op does not call sv" '! grep -q "^sv" "$CALLS"'
health "$MX" "$ROOT/preparations/genA/source/bridge" 3
run matrix-follow --wait 0; okc "$RC" 75 "busy matrix → 75"
ok "busy matrix not restarted" '! grep -q "^sv" "$CALLS"'

run status; okc "$RC" 0 "status rc"
ok "status shows pointer and frontends" '[[ "$OUT" == *"pointer: preparations/genB"* && "$OUT" == *"telegram serving: $ROOT/preparations/genB/source/bridge"* && "$OUT" == *"idle=no"* ]]'

# ── prepare: worktree, .env link, reuse refusal → full build, gate ───────────
CO="$TMP/checkout"
mkdir -p "$CO/bridge"
cp "$TMP/start.sh" "$CO/bridge/start.sh"; : > "$CO/bridge/prepared_runtime.py"
cat > "$CO/bridge/termux_prepare.py" <<'EOF'
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["STUB_DIR"] + "/calls", "a") as f:
    f.write("prepare " + " ".join(args) + f" | VIRTUAL_ENV={os.environ.get('VIRTUAL_ENV', '')}\n")
work = Path(args[args.index("--work-dir") + 1])
reuse = "--reuse-wheels-from" in args
if reuse and os.environ.get("STUB_REUSE") == "refuse":
    print(json.dumps({"status": "error", "reason": "reuse_toolchain_mismatch"}))
    raise SystemExit(1)
(work / "runtime/bin").mkdir(parents=True)
os.symlink(os.environ["STUB_DIR"] + "/runtime-python", work / "runtime/bin/python")
(work / "receipt.json").write_text(json.dumps({"status": "ready"}))
print(json.dumps({"status": "ready"}))
EOF
printf '.env\n' > "$CO/.gitignore"
git -C "$CO" init -q
git -C "$CO" add -A
git -C "$CO" -c user.email=t@t -c user.name=t commit -q -m init
echo "TELEGRAM_BOT_TOKEN=x" > "$CO/bridge/.env"
# shellcheck disable=SC2034  # SHA, OUT and G are read via eval inside ok()
SHA="$(git -C "$CO" rev-parse HEAD)"
export CCC_NODE_CHECKOUT="$CO"

: > "$CALLS"
RC=0
# shellcheck disable=SC2034
OUT="$(STUB_REUSE=refuse VIRTUAL_ENV=/old/venv bash "$SUT" prepare HEAD --name gen-new --extra matrix 2>"$TMP/err")" || RC=$?
okc "$RC" 0 "prepare rc"
# shellcheck disable=SC2034
G="$ROOT/preparations/gen-new"
ok "prepare prints the generation" '[ "$OUT" = "$G" ]'
ok "source is a worktree at the revision" '[ "$(git -C "$G/source" rev-parse HEAD)" = "$SHA" ]'
ok "provider .env linked from the checkout" '[ "$(readlink "$G/source/bridge/.env")" = "$CO/bridge/.env" ]'
ok "first attempt reused the serving job" 'grep -q "prepare --work-dir $G/job --reuse-wheels-from $ROOT/preparations/genB/job2 --timeout-seconds 7200 --extra matrix --verify-reinstall" "$CALLS"'
ok "fallback full build into job2 without reuse" 'grep "work-dir $G/job2" "$CALLS" | grep -vq reuse-wheels-from'
ok "inherited VIRTUAL_ENV removed for termux_prepare" '! grep -q "VIRTUAL_ENV=/old" "$CALLS"'
ok "fallback job selected through serving link" '[ "$(readlink "$G/serving")" = job2 ]'
ok "gate report written" 'grep -q ready "$G/prepared-gate.json"'
ok "fallback reason reported" 'grep -q "reuse_toolchain_mismatch" "$TMP/err"'

run prepare HEAD --name gen-new; okc "$RC" 2 "existing generation refused"
run prepare no-such-rev; okc "$RC" 2 "unknown revision refused"
run prepare HEAD --name ../escape; okc "$RC" 2 "path-like name refused"

: > "$CALLS"
run prepare HEAD --name gen-reuse --reuse none --no-reinstall; okc "$RC" 0 "prepare --reuse none rc"
ok "--reuse none builds once without reuse or reinstall" \
  '[ "$(grep -c "^prepare" "$CALLS")" = 1 ] && ! grep -q -e reuse-wheels-from -e verify-reinstall "$CALLS"'
ok "plain job has no serving link" '[ ! -e "$ROOT/preparations/gen-reuse/serving" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
