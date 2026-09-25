#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/skills/gh-pr-flow/repo-hygiene-via-relay.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); }
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL + 1)); }

mkdir -p "$TMP/bin" "$TMP/state"
cat > "$TMP/bin/ssh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    *) break ;;
  esac
done
shift
exec "$@"
SH
# Fake gh backed by per-repo state files under $FAKE_STATE, keyed by
# OWNER_REPO (slash replaced):
#   <key>.noadmin   repo read reports admin=false
#   <key>.alerts    Dependabot alerts enabled
#   <key>.ruleset   JSON of an existing ruleset named like ours
# Every mutation appends a line to $FAKE_STATE/mutations.
cat > "$TMP/bin/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
S="${FAKE_STATE:?}"
method=GET
if [ "${2:-}" = -X ]; then method="$3"; set -- "$1" "${@:4}"; fi
[ "${1:-}" = api ] || { echo "unexpected fake gh invocation: $*" >&2; exit 90; }
path="$2"
if [ "$path" = user ]; then printf '{"login":"%s"}\n' "${FAKE_ACTOR:-jinon86}"; exit 0; fi
IFS=/ read -r _ owner repo rest <<<"$path"
k="${owner}_${repo}"
case "$method ${rest:-}" in
  "GET vulnerability-alerts")
    [ -e "$S/$k.alerts" ] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; } ;;
  "PUT vulnerability-alerts")
    echo "PUT alerts $owner/$repo" >> "$S/mutations"; : > "$S/$k.alerts" ;;
  "GET rulesets")
    if [ -e "$S/$k.ruleset" ]; then jq -c '[{id:.id,name:.name}]' "$S/$k.ruleset"; else echo '[]'; fi ;;
  "POST rulesets")
    echo "POST ruleset $owner/$repo" >> "$S/mutations"
    jq -c '. + {id: 42}' > "$S/$k.ruleset"; echo '{"id":42}' ;;
  GET\ rulesets/*) cat "$S/$k.ruleset" ;;
  "GET ")
    if [ -e "$S/$k.noadmin" ]; then echo '{"permissions":{"admin":false}}'
    else echo '{"permissions":{"admin":true},"archived":false}'; fi ;;
  *) echo "unexpected fake gh invocation: $method $path" >&2; exit 90 ;;
esac
SH
chmod +x "$TMP/bin/ssh" "$TMP/bin/gh"

export PATH="$TMP/bin:$PATH"
export FAKE_STATE="$TMP/state"
reset_state() { rm -rf "$FAKE_STATE"; mkdir -p "$FAKE_STATE"; }
export CCC_REPO_HYGIENE_ALLOWLIST="$TMP/allowlist"
cat > "$CCC_REPO_HYGIENE_ALLOWLIST" <<'LIST'
# fixture allowlist
example-org/repo-a
example-org/repo-b   # trailing comment

example-org/repo-c
LIST

# 1) No --operator-approved: refused before any remote call.
reset_state
if bash "$SCRIPT" >"$TMP/out" 2>"$TMP/err"; then
  fail "ran without --operator-approved"
elif grep -q -- '--operator-approved is required' "$TMP/err" && [ ! -e "$FAKE_STATE/mutations" ]; then
  pass
else
  fail "missing approval failure was not explicit"
fi

# 2) A repository outside the fixed allowlist is refused.
if bash "$SCRIPT" --operator-approved --repo example-org/other >"$TMP/out" 2>"$TMP/err"; then
  fail "non-allowlisted repo was accepted"
elif grep -q 'not in allowlist: example-org/other' "$TMP/err"; then
  pass
else
  fail "allowlist refusal was not explicit"
fi

# 3) Wrong remote actor is refused.
reset_state
FAKE_ACTOR=other-account bash "$SCRIPT" --operator-approved --repo example-org/repo-a \
  >"$TMP/out" 2>"$TMP/err" && rc=0 || rc=$?
if [ "$rc" -eq 3 ] && grep -q 'remote actor is not jinon86' "$TMP/err"; then
  pass
else
  fail "wrong actor was not refused (rc=$rc)"
fi

# 4) Default dry run over the full allowlist: 3 would-change rows, no mutation.
reset_state
if bash "$SCRIPT" --operator-approved >"$TMP/out" 2>"$TMP/err"; then
  n="$(jq -s '[.[] | select(.dependabot == "would-enable" and .ruleset == "would-create")] | length' "$TMP/out")"
  if [ "$n" -eq 3 ] && [ ! -e "$FAKE_STATE/mutations" ] \
    && jq -se '.[-1] | .summary and .apply == false and .ok' "$TMP/out" >/dev/null; then
    pass
  else
    fail "dry run rows or mutation guard invalid (n=$n)"
  fi
else
  fail "valid dry run failed: $(cat "$TMP/err")"
fi

# 5) Apply enables alerts and creates a verified ruleset.
reset_state
if bash "$SCRIPT" --operator-approved --apply --repo example-org/repo-a --repo example-org/repo-b \
  >"$TMP/out" 2>"$TMP/err"; then
  if jq -se '[.[] | select(.repo)] | length == 2 and all(.status == "ok" and .dependabot == "enabled" and .ruleset == "created(#42)")' \
    "$TMP/out" >/dev/null && [ "$(wc -l < "$FAKE_STATE/mutations")" -eq 4 ]; then
    pass
  else
    fail "apply output or mutations invalid"
  fi
else
  fail "valid apply failed: $(cat "$TMP/err")"
fi

# 6) Apply is idempotent: a second run mutates nothing.
: > "$FAKE_STATE/mutations"
if bash "$SCRIPT" --operator-approved --apply --repo example-org/repo-a >"$TMP/out" 2>"$TMP/err" \
  && jq -se '.[0].dependabot == "already-on" and .[0].ruleset == "already-present(#42)"' "$TMP/out" >/dev/null \
  && [ ! -s "$FAKE_STATE/mutations" ]; then
  pass
else
  fail "second apply was not idempotent"
fi

# 7) An existing same-name ruleset with different rules is reported, never overwritten.
reset_state
echo '{"id":7,"name":"default-branch-no-delete-no-force-push","enforcement":"evaluate","conditions":{"ref_name":{"include":["~DEFAULT_BRANCH"],"exclude":[]}},"rules":[{"type":"deletion"}]}' \
  > "$FAKE_STATE/example-org_repo-c.ruleset"
bash "$SCRIPT" --operator-approved --apply --repo example-org/repo-c >"$TMP/out" 2>"$TMP/err" && rc=0 || rc=$?
if [ "$rc" -eq 4 ] && jq -se '.[0].status == "incomplete" and .[0].ruleset == "conflict-mismatch(#7)"' "$TMP/out" >/dev/null \
  && ! grep -q 'POST ruleset' "$FAKE_STATE/mutations"; then
  pass
else
  fail "mismatched ruleset was not reported as conflict (rc=$rc)"
fi

# 8) No admin: refused per repo, others still processed, overall exit 4.
reset_state
: > "$FAKE_STATE/example-org_repo-b.noadmin"
bash "$SCRIPT" --operator-approved --apply --repo example-org/repo-b --repo example-org/repo-c \
  >"$TMP/out" 2>"$TMP/err" && rc=0 || rc=$?
if [ "$rc" -eq 4 ] \
  && jq -se '.[0].status == "refused" and .[0].reason == "no-admin" and .[1].status == "ok" and (.[-1].ok == false)' "$TMP/out" >/dev/null \
  && ! grep -q 'repo-b' "$FAKE_STATE/mutations"; then
  pass
else
  fail "no-admin repo was not refused cleanly (rc=$rc)"
fi

# 9) Missing or malformed allowlist is refused before any remote call.
reset_state
if CCC_REPO_HYGIENE_ALLOWLIST="$TMP/absent" bash "$SCRIPT" --operator-approved >"$TMP/out" 2>"$TMP/err"; then
  fail "ran without an allowlist file"
elif grep -q 'allowlist file not found' "$TMP/err" && [ ! -e "$FAKE_STATE/mutations" ]; then
  pass
else
  fail "missing allowlist refusal was not explicit"
fi
printf 'example-org/repo-a\nnot a repo; rm -rf /\n' > "$TMP/bad-allowlist"
if bash "$SCRIPT" --operator-approved --allowlist "$TMP/bad-allowlist" >"$TMP/out" 2>"$TMP/err"; then
  fail "malformed allowlist entry was accepted"
elif grep -q 'invalid allowlist entry' "$TMP/err"; then
  pass
else
  fail "malformed allowlist refusal was not explicit"
fi

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
