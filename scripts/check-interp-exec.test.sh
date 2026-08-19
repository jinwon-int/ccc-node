#!/usr/bin/env bash
# Tests for scripts/check-interp-exec.py (#1160) — the lint that flags repo .sh
# scripts executed WITHOUT a named interpreter (the #472/#663/#1151/#1157/#1159
# root cause). Each fixture pins one cell of the flag/pass matrix so the
# false-positive guards (sourcing, arguments, assignments, arrays, case
# patterns, globs, heredocs, seams, waivers) cannot silently regress.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LINT="$ROOT/scripts/check-interp-exec.py"
pass=0; fail=0
TMP_BASE="${TMPDIR:-$(dirname "$ROOT")}"; mkdir -p "$TMP_BASE"
TMP="$(mktemp -d "$TMP_BASE/ccc-interp-exec-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
# Suite-env hygiene (#1064): the stub audit fails any stub-sourcing suite that
# does not reset inherited CCC_*/NUNCHI_* state. This lint reads no harness
# env, but reset anyway so a live-node run matches CI exactly.
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# flagged <name> — fixture MUST be reported
flagged() { # <name> <file>
  if python3 "$LINT" --root "$TMP" "$2" >"$TMP/out" 2>/dev/null; then
    fail=$((fail+1)); echo "FAIL: $1 (expected finding, got clean)"
  else
    pass=$((pass+1))
  fi
}
# clean <name> — fixture MUST NOT be reported
clean() { # <name> <file>
  if python3 "$LINT" --root "$TMP" "$2" >"$TMP/out" 2>/dev/null; then
    pass=$((pass+1))
  else
    fail=$((fail+1)); echo "FAIL: $1 (unexpected finding: $(cat "$TMP/out"))"
  fi
}
mk() { # <file> — write stdin as a fixture
  cat > "$TMP/$1"
}

ok "lint script exists" '[ -r "$LINT" ]'
ok "lint compiles" 'python3 -m py_compile "$LINT"'

# --- true positives: the defect family -------------------------------------
mk tp-basic.sh <<'EOF'
#!/usr/bin/env bash
"$HOOKDIR/scan-injection.sh" "$@"
EOF
flagged "plain direct exec" tp-basic.sh

mk tp-nohup.sh <<'EOF'
nohup "$SCRIPT_DIR/start.sh" >>"$LOG" 2>&1 &
EOF
flagged "nohup prefix" tp-nohup.sh

mk tp-exec.sh <<'EOF'
exec "$DIR/service-launchd.sh" install
EOF
flagged "exec prefix" tp-exec.sh

mk tp-setsid.sh <<'EOF'
setsid "$DIR/restart.sh" </dev/null >/dev/null 2>&1 &
EOF
flagged "setsid prefix" tp-setsid.sh

mk tp-env-assign.sh <<'EOF'
if CCC_STORE="$store" \
     "$SRC/agent-cron.sh" list --json | jq -e .; then :; fi
EOF
flagged "env-assignment prefix + continuation + pipe" tp-env-assign.sh

mk tp-subst.sh <<'EOF'
out="$("$DIR/gen.sh" arg)"
EOF
flagged "command substitution" tp-subst.sh

mk tp-backtick.sh <<'EOF'
out=`"$DIR/gen.sh" arg`
EOF
flagged "backtick substitution" tp-backtick.sh

mk tp-cond.sh <<'EOF'
while "$DIR/poll.sh"; do sleep 1; done
EOF
flagged "while-condition exec" tp-cond.sh

mk tp-after-and.sh <<'EOF'
[ -f "$DIR/foo.sh" ] && "$DIR/foo.sh" --now
EOF
flagged "exec after &&" tp-after-and.sh

mk tp-sudo.sh <<'EOF'
sudo -u ops "$DIR/install.sh"
EOF
flagged "sudo prefix" tp-sudo.sh

# --- true negatives: the false-positive guards ------------------------------
mk tn-interp.sh <<'EOF'
bash "$HOOKDIR/scan-injection.sh" "$@"
setsid bash "$DIR/restart.sh" </dev/null >/dev/null 2>&1 &
sh ./legacy.sh
EOF
clean "interpreter named" tn-interp.sh

mk tn-source.sh <<'EOF'
. "$(cd "$(dirname "$0")/.." && pwd)/lib/test-stub.sh"
source "$LIBDIR/hook-common.sh"
EOF
clean "sourcing" tn-source.sh

mk tn-args.sh <<'EOF'
grep -n pat "$DIR/foo.sh"
cat old.sh | diff - new.sh
[ -r "$f.sh" ] && [ -x run.sh ] || true
shellcheck --severity=error "$f"
EOF
clean ".sh as argument / test" tn-args.sh

mk tn-assign.sh <<'EOF'
DEFAULT_BIN="$DIR/scan-injection.sh"
SCAN="${CCC_SCAN_INJECTION_BIN:-$DEFAULT_BIN}"
export PATH_ENTRY="$HOME/bin/foo.sh"
EOF
clean "assignments + seam indirection" tn-assign.sh

mk tn-array.sh <<'EOF'
targets=(
  "$DIR/one.sh"
  "$DIR/two.sh"
)
run chmod +x "${targets[@]}"
SINGLE=("$DIR/three.sh")
EOF
clean "array elements" tn-array.sh

mk tn-case.sh <<'EOF'
case "$f" in
  *.test.sh|lib/stub-helper.sh|*.pyc)
    : ;;
  install-a.sh|install-b.sh)
    bash "$f" --check ;;
  *)
    if [ ! -r "$f" ]; then
      case "$f" in
        nested.sh) bash "$f" ;;
      esac
      ;;
  esac
done_marker=1
EOF
clean "case patterns incl. nested" tn-case.sh

mk tn-glob.sh <<'EOF'
scripts/install-[A-Za-z0-9._-]*\.sh) ;;
pat='*.sh'
EOF
clean "glob/regex tokens" tn-glob.sh

mk tn-heredoc.sh <<'EOF'
cat > "$OUT" <<'EOS'
"$DIR/inner.sh" --generated
EOS
printf 'done\n'
EOF
clean "heredoc body" tn-heredoc.sh

mk tn-waiver.sh <<'EOF'
# interp-exec-ok: deliberate direct exec to prove the shebang fails first
"$TMP/badshebang-tool.sh" >/dev/null 2>&1; rc=$?
"$TMP/other.sh" >/dev/null 2>&1  # interp-exec-ok: same-line waiver form
EOF
clean "waiver comments (above + inline)" tn-waiver.sh

mk tn-comment.sh <<'EOF'
# "$DIR/old.sh" used to run here
printf 'x\n'
EOF
clean "commented-out exec" tn-comment.sh

# --- python -----------------------------------------------------------------
mk tp-sub.py <<'EOF'
import subprocess
subprocess.run(["/opt/ccc-node/scripts/foo.sh", "x"], check=True)
subprocess.Popen(['./bar.sh'])
EOF
flagged "python subprocess literal .sh" tp-sub.py

mk tn-sub.py <<'EOF'
import subprocess
subprocess.run(["bash", "/opt/ccc-node/scripts/foo.sh"], check=True)
subprocess.Popen([tool, query])  # seam variable — not a literal .sh
EOF
clean "python interpreter named / seam var" tn-sub.py

# --- repo gate: the lint must be clean on the repo it ships in --------------
if python3 "$LINT" --root "$ROOT" >"$TMP/repo.out" 2>/dev/null; then
  pass=$((pass+1))
else
  fail=$((fail+1)); echo "FAIL: repo-wide lint clean"; cat "$TMP/repo.out"
fi

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
