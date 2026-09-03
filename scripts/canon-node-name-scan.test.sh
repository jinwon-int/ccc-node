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

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
