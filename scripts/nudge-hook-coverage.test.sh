#!/usr/bin/env bash
# Tests for scripts/nudge-hook-coverage.sh (#1229 follow-up) — no network: `gh`
# is a stub that answers from fixture files and logs every argv line, so the
# read-only contract (no POST to /hooks, comment only on gaps) is asserted.
# shellcheck disable=SC2034  # variables are read inside the eval'd conditions
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/nudge-hook-coverage.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
mkdir -p "$TMP/bin" "$TMP/fx"
cat > "$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$1 $2" in
  "repo list") cat "$STUB_FX/repos.txt" ;;
  "api repos/o/alpha/hooks") echo "$(cat "$STUB_FX/alpha.hooks")" ;;
  "api repos/o/beta/hooks") echo "" ;;
  "api repos/o/gamma/hooks") echo "" ;;
  "api repos/o/delta/hooks") exit 1 ;;
  "api repos/o/beta/contents/.github/workflows") echo 3 ;;
  # a 404 from gh prints the error body on stdout AND exits non-zero (the
  # 2026-10-02 live run misread this as "has workflows")
  "api repos/o/gamma/contents/.github/workflows") echo '{"message":"Not Found","status":"404"}' | jq 'if type=="array" then length else 0 end'; exit 1 ;;
  "issue comment") echo "commented" ;;
  *) echo "stub gh: unexpected: $*" >&2; exit 1 ;;
esac
EOF
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH" STUB_LOG="$TMP/gh.log" STUB_FX="$TMP/fx"
printf 'alpha 2026-04-01 2026-10-01 PRIVATE\nbeta 2026-09-08 2026-10-02 PUBLIC\ngamma 2026-09-05 2026-09-05 PUBLIC\ndelta 2026-05-01 2026-09-30 PRIVATE\n' > "$TMP/fx/repos.txt"
printf '668600590:active' > "$TMP/fx/alpha.hooks"

# 1. table + summary + exit 10 on a gap; no-CI repo skipped; denied repo counted
: > "$STUB_LOG"
out="$(bash "$SCRIPT" --org o --url-pattern ccc-nudge)"; rc=$?
ok "a CI repo without the hook → exit 10" '[ "$rc" = 10 ]'
ok "covered repo lists its hook id" 'printf "%s\n" "$out" | grep -q "^alpha hooks=668600590:active"'
ok "missing repo is marked MISSING with its workflow count" 'printf "%s\n" "$out" | grep -q "^beta hooks=MISSING .*workflows=3"'
ok "a repo without CI workflows is skipped, not a gap" 'printf "%s\n" "$out" | grep -q "^gamma hooks=- .*no-ci-workflows"'
ok "a repo the token cannot list is reported as denied, not missing" 'printf "%s\n" "$out" | grep -q "^delta hooks=? .*hook-list-denied"'
ok "summary counts match" 'printf "%s\n" "$out" | grep -q "repos=4 covered=1 missing=1 skipped_no_ci=1 list_denied=1"'
ok "read-only: no POST and no issue comment without --comment" '! grep -qE -- "--method POST|issue comment" "$STUB_LOG"'

# 2. --include-no-ci turns the no-CI repo into a gap
out="$(bash "$SCRIPT" --org o --url-pattern ccc-nudge --include-no-ci)"
ok "--include-no-ci counts gamma as missing" 'printf "%s\n" "$out" | grep -q "missing=2"'

# 3. --comment posts only when gaps exist; body names the gap and stays body-free
: > "$STUB_LOG"
bash "$SCRIPT" --org o --url-pattern ccc-nudge --comment o/tracker#1229 >/dev/null; rc=$?
ok "with gaps, one issue comment is posted to the given issue" '[ "$(grep -c "^issue comment 1229 --repo o/tracker" "$STUB_LOG")" = 1 ]'
ok "the comment names the missing repo and not the covered one as a gap" 'grep -q "beta created=2026-09-08" "$STUB_LOG" && ! grep -qE "^- alpha" "$STUB_LOG"'

# 4. full coverage → exit 0, no comment unless --always-comment
printf 'alpha 2026-04-01 2026-10-01 PRIVATE\n' > "$TMP/fx/repos.txt"
: > "$STUB_LOG"
bash "$SCRIPT" --org o --url-pattern ccc-nudge --comment o/tracker#1229 >/dev/null; rc=$?
ok "full coverage → exit 0 and no comment" '[ "$rc" = 0 ] && ! grep -q "issue comment" "$STUB_LOG"'
bash "$SCRIPT" --org o --url-pattern ccc-nudge --comment o/tracker#1229 --always-comment >/dev/null
ok "--always-comment posts a 'Full coverage' comment" 'grep -q "Full coverage" "$STUB_LOG"'

# 5. --json shape
out="$(bash "$SCRIPT" --org o --url-pattern ccc-nudge --json)"
ok "--json emits one object with counts and rows" 'printf "%s" "$out" | jq -e ".org == \"o\" and .total == 1 and .covered == 1 and (.rows|length) == 1" >/dev/null'

# 6. usage / environment errors
bash "$SCRIPT" --org o >/dev/null 2>&1; rc=$?
ok "missing --url-pattern → exit 2" '[ "$rc" = 2 ]'
bash "$SCRIPT" --org o --url-pattern x --comment nope >/dev/null 2>&1; rc=$?
ok "malformed --comment → exit 2" '[ "$rc" = 2 ]'
: > "$TMP/fx/repos.txt"
bash "$SCRIPT" --org o --url-pattern x >/dev/null 2>&1; rc=$?
ok "empty repository list → exit 3" '[ "$rc" = 3 ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
