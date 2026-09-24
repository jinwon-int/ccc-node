#!/usr/bin/env bash
# No-network tests for canon-node-name-scan.sh: the canon skill sets must stay
# free of fleet node names (#1446) while account-derived tokens stay allowed.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
BASE_TMP="${TMPDIR:-/tmp}"; mkdir -p "$BASE_TMP"
TMP="$(mktemp -d "$BASE_TMP/canon-scan-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

SCAN="$ROOT/scripts/canon-node-name-scan.sh"

make_canon() { # $1 = fixture root
  mkdir -p "$1/skills/demo" "$1/codex/skills/demo"
}

# 1. Clean canon: role wording only — passes.
make_canon "$TMP/clean"
printf 'Approve via the relay node helper; broker host runs the tunnel.\n' \
  > "$TMP/clean/skills/demo/SKILL.md"
printf 'node-a and node-b are placeholders.\n' \
  > "$TMP/clean/codex/skills/demo/extra.md"
ok "clean canon passes" \
  "bash '$SCAN' --root '$TMP/clean' >'$TMP/out' 2>&1"

# 2. Account-derived tokens are allowed: profile name, config token, env var.
make_canon "$TMP/accounts"
cat > "$TMP/accounts/skills/demo/SKILL.md" <<'MD'
The seoseo-ai profile lives in the gh-seoseo-ai config.
CCC_SEOSEO_AI_GH_CONFIG_DIR may override it; jinon86 merges.
MD
ok "account-derived tokens pass" \
  "bash '$SCAN' --root '$TMP/accounts' >'$TMP/out' 2>&1"

# 3. Bare node names fail, case-insensitively, with the file:line listed.
make_canon "$TMP/dirty"
printf 'run the watcher on gwakga\n' > "$TMP/dirty/skills/demo/SKILL.md"
printf 'merge via SEOSEO\n' > "$TMP/dirty/codex/skills/demo/extra.md"
if bash "$SCAN" --root "$TMP/dirty" >"$TMP/out" 2>"$TMP/err"; then
  ok "dirty canon rejected" "false"
else
  ok "dirty canon rejected" "true"
fi
ok "report lists the offending file" "grep -q 'skills/demo/SKILL.md' '$TMP/err'"
ok "report lists the codex twin" "grep -q 'codex/skills/demo/extra.md' '$TMP/err'"
ok "report is case-insensitive" "grep -qi 'SEOSEO' '$TMP/err'"

# 4. A line mixing an allowed token with a bare name is still flagged.
make_canon "$TMP/mixed"
printf 'seoseo-ai approves on seoseo\n' > "$TMP/mixed/skills/demo/SKILL.md"
if bash "$SCAN" --root "$TMP/mixed" >"$TMP/out" 2>"$TMP/err"; then
  ok "mixed line still flagged" "false"
else
  ok "mixed line still flagged" "true"
fi

# 5. Numbered and hosted slugs are caught too.
make_canon "$TMP/slugs"
printf 'historical host racknerd-167be94 and vps7 are node slugs\n' \
  > "$TMP/slugs/skills/demo/SKILL.md"
if bash "$SCAN" --root "$TMP/slugs" >"$TMP/out" 2>/dev/null; then
  ok "hosted/numbered slugs rejected" "false"
else
  ok "hosted/numbered slugs rejected" "true"
fi

# 6. Missing canon dirs are a usage error, not a silent pass.
if bash "$SCAN" --root "$TMP/nowhere" >"$TMP/out" 2>"$TMP/err"; then
  ok "missing dirs are a usage error" "false"
else
  ok "missing dirs are a usage error" "[ \"\$(tail -1 '$TMP/err' | grep -c 'expected')\" -gt 0 ]"
fi

# 7. Regression (#1446 follow-up): a node-named PATH prefix must not trigger
#    findings — matching is content-only. The fixture root lives under a
#    directory whose name contains a node name.
FIXROOT="$TMP/gongmyoung-home-sim/canon"
mkdir -p "$FIXROOT/skills/demo" "$FIXROOT/codex/skills/demo"
printf 'relay-held helper on the relay node.\n' > "$FIXROOT/skills/demo/SKILL.md"
printf 'the seoseo-ai profile is an allowed identity.\n' \
  > "$FIXROOT/codex/skills/demo/extra.md"
ok "node-named path prefix does not trigger" \
  "bash '$SCAN' --root '$FIXROOT' >'$TMP/out' 2>&1"

# 8. --repo-wide (#1451 P4): per-file counts ratcheted against a baseline.
#    Fixture is a real git checkout because the scan walks `git ls-files`.
make_repo() { # $1 = fixture root
  mkdir -p "$1/scripts" "$1/src"
  git -C "$1" init -q
  # Detection is per LINE (two names on one line count once), so two lines.
  printf 'watcher runs on gwakga\nGwakga again, case-insensitively\n' > "$1/src/fleet.sh"
  printf 'role wording only: relay node, broker host\n' > "$1/src/clean.sh"
  printf 'seoseo-ai approves; gh-seoseo-ai config\n' > "$1/src/accounts.md"
  printf '## history\n- moved off yukson\n' > "$1/CHANGELOG.md"
  git -C "$1" add -A
}
make_repo "$TMP/repo"
if bash "$SCAN" --repo-wide --root "$TMP/repo" >"$TMP/out" 2>"$TMP/err"; then
  ok "repo-wide without a baseline is a usage error" "false"
else
  ok "repo-wide without a baseline is a usage error" "grep -q 'baseline not found' '$TMP/err'"
fi
ok "repo-wide --update-baseline writes the file" \
  "bash '$SCAN' --repo-wide --root '$TMP/repo' --update-baseline >'$TMP/out' 2>&1"
ok "baseline counts hits per file (2 on the dirty file only)" \
  "grep -qx '2 src/fleet.sh' '$TMP/repo/scripts/canon-node-name-baseline.txt'"
ok "baseline skips clean files, account tokens and CHANGELOG.md" \
  "! grep -qE 'clean.sh|accounts.md|CHANGELOG' '$TMP/repo/scripts/canon-node-name-baseline.txt'"
ok "repo-wide matches its own baseline" \
  "bash '$SCAN' --repo-wide --root '$TMP/repo' >'$TMP/out' 2>&1 && grep -q 'ok — matches baseline (1 files, 2 hits)' '$TMP/out'"

# 9. Growth in a baseline file and a new file both fail, naming each.
printf 'and once more on gwakga\n' >> "$TMP/repo/src/fleet.sh"
printf 'ssh daegyo\n' > "$TMP/repo/src/new.sh"
git -C "$TMP/repo" add -A
if bash "$SCAN" --repo-wide --root "$TMP/repo" >"$TMP/out" 2>"$TMP/err"; then
  ok "growth + new file rejected" "false"
else
  ok "growth + new file rejected" "true"
fi
ok "report marks the grown file" "grep -qE '^GREW +2 -> +3 +src/fleet.sh' '$TMP/err'"
ok "report marks the new file" "grep -qE '^NEW +1 +src/new.sh' '$TMP/err'"
ok "report says not to extend the baseline" "grep -q 'do not extend the baseline' '$TMP/err'"

# 10. Shrinking fails too (until locked in), then --update-baseline clears it.
printf 'role wording now\n' > "$TMP/repo/src/fleet.sh"
rm "$TMP/repo/src/new.sh"; git -C "$TMP/repo" add -A
if bash "$SCAN" --repo-wide --root "$TMP/repo" >"$TMP/out" 2>"$TMP/err"; then
  ok "shrink is flagged until locked in" "false"
else
  ok "shrink is flagged until locked in" "grep -qE '^SHRANK +2 -> +0 +src/fleet.sh' '$TMP/err' && grep -q 'update-baseline' '$TMP/err'"
fi
ok "regenerated baseline is empty of entries" \
  "bash '$SCAN' --repo-wide --root '$TMP/repo' --update-baseline >'$TMP/out' 2>&1 && ! grep -qvE '^#' '$TMP/repo/scripts/canon-node-name-baseline.txt'"
ok "clean tree matches empty baseline" \
  "bash '$SCAN' --repo-wide --root '$TMP/repo' >'$TMP/out' 2>&1"

# 11. A node-named PATH prefix is content-only in repo-wide mode as well.
NROOT="$TMP/daegyo-home-sim/repo"; mkdir -p "$NROOT/scripts"; git -C "$NROOT" init -q
printf 'relay node only\n' > "$NROOT/x.sh"; git -C "$NROOT" add -A
ok "node-named path prefix does not trigger repo-wide" \
  "bash '$SCAN' --repo-wide --root '$NROOT' --update-baseline >'$TMP/out' 2>&1 && ! grep -qvE '^#' '$NROOT/scripts/canon-node-name-baseline.txt'"

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
