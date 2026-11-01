#!/usr/bin/env bash
# Regression tests for scripts/quick-check.sh and the validate-harness quick
# phase (#2611).
# Usage: bash scripts/quick-check.test.sh   (exit 0 = all pass)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
QC="$ROOT/scripts/quick-check.sh"
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

T="$(mktemp -d)"
trap 'rm -rf "$T" 2>/dev/null || true' EXIT
gitq() { git -c user.name=quick-check-test -c user.email=qc@example.invalid -c commit.gpgsign=false "$@"; }

# --- pre-push hook install / uninstall (fixture repo, stubbed tools) ---------
mkrepo() { # <dir> -> a git repo carrying quick-check.sh + stub validators
  mkdir -p "$1/scripts"
  cp "$QC" "$1/scripts/quick-check.sh"
  printf '#!/usr/bin/env bash\necho "  ok hidden-ok-line"\necho "== section =="\n[ "${STUB_VH_FAIL:-0}" = 1 ] && { echo "FAIL: stub validator"; echo "  fix: stub hint"; echo "HARNESS VALIDATION: FAIL"; exit 1; }\necho "phase=$CCC_HARNESS_PHASE base=$CCC_HARNESS_QUICK_BASE"\necho "GIT_DIR=${GIT_DIR-unset}"\nscratch="$(mktemp -d)"; git init -q "$scratch" && git -C "$scratch" -c user.name=s -c user.email=s@example.invalid -c commit.gpgsign=false commit -q --allow-empty -m scratch; rm -rf "$scratch"\necho "HARNESS VALIDATION: PASS"\n' > "$1/scripts/validate-harness.sh"
  printf 'import sys\nprint("changelog fragments ok (stub)")\n' > "$1/scripts/changelog_fragments.py"
  gitq -C "$1" init -q && gitq -C "$1" add -A && gitq -C "$1" commit -qm fixture
}
R="$T/repo"; mkrepo "$R"
HOOK="$R/.git/hooks/pre-push"

out="$(bash "$R/scripts/quick-check.sh" --install-pre-push 2>&1)"; rc=$?
ok "install writes an executable marked pre-push hook" \
  '[ "$rc" = 0 ] && [ -x "$HOOK" ] && grep -qxF "# marker: ccc-node:quick-check-pre-push v1" "$HOOK"'
ok "the hook execs quick-check through a named interpreter" \
  'grep -qF "exec bash \"\$root/scripts/quick-check.sh\" --from-pre-push" "$HOOK" && bash -n "$HOOK"'
out="$(bash "$R/scripts/quick-check.sh" --install-pre-push 2>&1)"; rc=$?
ok "re-install over our own hook is idempotent" '[ "$rc" = 0 ] && [ -x "$HOOK" ]'
out="$(bash "$R/scripts/quick-check.sh" --uninstall-pre-push 2>&1)"; rc=$?
ok "uninstall removes our hook" '[ "$rc" = 0 ] && [ ! -e "$HOOK" ]'
out="$(bash "$R/scripts/quick-check.sh" --uninstall-pre-push 2>&1)"; rc=$?
ok "uninstall with nothing installed is a no-op" '[ "$rc" = 0 ]'

printf '#!/bin/sh\necho mine\n' > "$HOOK"
out="$(bash "$R/scripts/quick-check.sh" --install-pre-push 2>&1)"; rc=$?
ok "install refuses to overwrite a foreign pre-push hook" \
  '[ "$rc" = 1 ] && grep -qx "echo mine" "$HOOK" && grep -q "not ours" <<<"$out"'
out="$(bash "$R/scripts/quick-check.sh" --uninstall-pre-push 2>&1)"; rc=$?
ok "uninstall leaves a foreign pre-push hook untouched" '[ "$rc" = 1 ] && grep -qx "echo mine" "$HOOK"'
rm -f "$HOOK"

printf '#!/usr/bin/env bash\n# marker: ccc-node:managed-checkout-guard v1\n' > "$R/.git/hooks/pre-commit"
out="$(bash "$R/scripts/quick-check.sh" --install-pre-push 2>&1)"; rc=$?
ok "install refuses in a node's managed checkout (guard marker present)" \
  '[ "$rc" = 1 ] && [ ! -e "$HOOK" ] && grep -q "MANAGED checkout" <<<"$out"'
rm -f "$R/.git/hooks/pre-commit"

gitq -C "$R" config core.hooksPath .githooks
out="$(bash "$R/scripts/quick-check.sh" --install-pre-push 2>&1)"; rc=$?
ok "install refuses to guess around a custom core.hooksPath" \
  '[ "$rc" = 1 ] && [ ! -e "$HOOK" ] && [ ! -e "$R/.githooks/pre-push" ] && grep -q "core.hooksPath" <<<"$out"'
gitq -C "$R" config --unset core.hooksPath

# --- run: verdict, quick phase wiring, output filter (stubbed validator) -----
base="$(git -C "$R" rev-parse HEAD)"
out="$(cd "$R" && bash scripts/quick-check.sh --base "$base" 2>&1)"; rc=$?
ok "a passing run exits 0 and says PASS" '[ "$rc" = 0 ] && grep -q "^QUICK CHECK: PASS" <<<"$out"'
ok "validate-harness runs in the quick phase with the resolved base" \
  'grep -qxF "phase=quick base=$base" <<<"$out"'
ok "per-file ok lines and section headers are hidden by default" \
  '! grep -q "hidden-ok-line" <<<"$out" && ! grep -qx "== section ==" <<<"$out"'
out="$(cd "$R" && bash scripts/quick-check.sh --base "$base" --verbose 2>&1)"
ok "--verbose shows the full validator output" 'grep -q "hidden-ok-line" <<<"$out"'
out="$(cd "$R" && STUB_VH_FAIL=1 bash scripts/quick-check.sh --base "$base" --from-pre-push </dev/null 2>&1)"; rc=$?
ok "a validator FAIL fails the run and keeps the FAIL + fix lines" \
  '[ "$rc" = 1 ] && grep -q "^QUICK CHECK: FAIL" <<<"$out" && grep -qx "FAIL: stub validator" <<<"$out" && grep -qx "  fix: stub hint" <<<"$out"'
ok "from the hook, a failure names the --no-verify bypass" 'grep -q "git push --no-verify" <<<"$out"'
out="$(bash "$R/scripts/quick-check.sh" --bogus 2>&1)"; rc=$?
ok "an unknown argument is a usage error" '[ "$rc" = 2 ]'
out="$(cd "$R" && bash scripts/quick-check.sh --base no-such-ref 2>&1)"; rc=$?
ok "an explicit base that does not resolve is an error, not a silent full/empty scope" \
  '[ "$rc" = 2 ] && grep -q "does not resolve" <<<"$out"'
out="$(cd "$R" && printf "refs/heads/other %s refs/heads/other %s\n" 1111111111111111111111111111111111111111 0000000000000000000000000000000000000000 \
  | bash scripts/quick-check.sh --base "$base" --from-pre-push 2>&1)"
ok "hook mode warns when the pushed ref is not the checked-out HEAD" 'grep -q "warning: pushing refs/heads/other" <<<"$out"'
echo dirty >> "$R/scripts/changelog_fragments.py"
out="$(cd "$R" && bash scripts/quick-check.sh --base "$base" --from-pre-push </dev/null 2>&1)"
ok "hook mode warns when the working tree differs from HEAD" 'grep -q "working tree differs from HEAD" <<<"$out"'
gitq -C "$R" checkout -q -- scripts/changelog_fragments.py

# --- the hook in a LINKED worktree: a real push (review finding, #2611) ------
# git exports GIT_DIR=<common>/.git/worktrees/<wt> to hooks there; leaked into
# the checks, a scratch `git init && git commit` (the statusline probe; the stub
# validator does the same) flipped core.bare on the user's repo and committed
# onto the pushed branch. The push must pass and leave the repo untouched.
BARE="$T/remote.git"; WT="$T/wt"
git init -q --bare "$BARE"
gitq -C "$R" remote add origin "$BARE" && gitq -C "$R" push -q origin HEAD:refs/heads/main 2>/dev/null
gitq -C "$R" worktree add -q -b feat "$WT" 2>/dev/null
bash "$WT/scripts/quick-check.sh" --install-pre-push >/dev/null 2>&1
echo change > "$WT/note.txt"; gitq -C "$WT" add note.txt; gitq -C "$WT" commit -qm change
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
before="$(gitq -C "$WT" rev-parse HEAD)"
out="$(cd "$WT" && gitq push origin feat 2>&1)"; rc=$?
ok "linked worktree: the push passes through the hook" '[ "$rc" = 0 ] && grep -q "QUICK CHECK: PASS" <<<"$out"'
ok "linked worktree: the checks run without the hook GIT_DIR" 'grep -q "GIT_DIR=unset" <<<"$out"'
ok "linked worktree: the repo is not flipped to bare and stays usable" \
  '[ "$(git -C "$R" config --get core.bare)" = false ] && git -C "$R" status --short >/dev/null 2>&1'
ok "linked worktree: no stray commit lands on the pushed branch" \
  '[ "$(gitq -C "$WT" rev-parse HEAD)" = "$before" ] && [ "$(git --git-dir="$BARE" rev-parse feat)" = "$before" ]'
bash "$WT/scripts/quick-check.sh" --uninstall-pre-push >/dev/null 2>&1

# --- real tree: the quick phase catches the 2026-10 failure classes ----------
# Hermetic copy of the tracked tree (working-tree contents; never touches this
# checkout's .git). Then reproduce the two deterministic failure classes that
# were 7 of the 11 failed harness-ci runs of 2026-10-26..31 (#2611): a
# repo-wide node-name ratchet drift and a stale skill registry.
W="$T/tree"; mkdir -p "$W"
(cd "$ROOT" && git ls-files -z | tar --null -T - -cf -) | tar -xf - -C "$W"
gitq -C "$W" init -q && gitq -C "$W" add -A && gitq -C "$W" commit -qm tree
out="$(cd "$W" && bash scripts/quick-check.sh --base HEAD 2>&1)"; rc=$?
ok "real tree, no change: quick check passes" '[ "$rc" = 0 ] && grep -q "^QUICK CHECK: PASS" <<<"$out"'
[ "$rc" = 0 ] || grep -E "^FAIL|^QUICK" <<<"$out" | head -10
ok "real tree, no change: shellcheck scope is the (empty) changed set" \
  'grep -q "quick: linting only files changed since" <<<"$out"'

ratchet_file="$(awk '!/^#/ && NF==2 {print $2; exit}' "$W/scripts/canon-node-name-baseline.txt")"
skill_src="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["skills"][0]["source"])' "$W/skills/registry.json")"
[ -d "$W/$skill_src" ] && skill_src="$skill_src/SKILL.md"
ok "fixture targets resolved (baseline entry + registry skill)" \
  '[ -n "$ratchet_file" ] && [ -f "$W/$ratchet_file" ] && [ -f "$W/$skill_src" ]'
cat "$W/$ratchet_file" "$W/$ratchet_file" > "$W/ratchet.tmp" && mv "$W/ratchet.tmp" "$W/$ratchet_file"
printf '\n<!-- edited without regenerating the registry -->\n' >> "$W/$skill_src"
# A changed script that execs a repo .sh without naming an interpreter (#1160),
# and a cron installer whose marker doctor does not know: a tree guard that
# lives in a *_test.py file — quick must still run it.
printf '#!/usr/bin/env bash\nD=/opt/x\n"$D/foo.sh"\n' > "$W/scripts/zz-qc-interp.sh"
printf '#!/usr/bin/env bash\nMARKER="# zz-qc-unregistered-marker"\necho "$MARKER"\n' > "$W/scripts/install-zz-qc-cron.sh"
gitq -C "$W" add scripts/zz-qc-interp.sh scripts/install-zz-qc-cron.sh
out="$(cd "$W" && bash scripts/quick-check.sh --base HEAD 2>&1)"; rc=$?
ok "real tree: a node-name ratchet drift fails the quick check" \
  '[ "$rc" = 1 ] && grep -q "^FAIL: repo-wide node-name ratchet failed" <<<"$out"'
ok "real tree: a stale skill registry fails the quick check, with the fix hint" \
  'grep -q "^FAIL: skill registry validation failed" <<<"$out" && grep -q "ccc-skill-registry.py update" <<<"$out"'
ok "real tree: the interpreter scan covers changed files in quick" \
  'grep -q "^FAIL: interpreter-less repo script invocation" <<<"$out" && grep -q "zz-qc-interp.sh" <<<"$out"'
ok "real tree: a tree guard named *_test.py still runs in quick (cron marker registry)" \
  'grep -q "^FAIL: doctor cron marker registry guard failed" <<<"$out"'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
