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
# Behaves like OpenSSH: the remote command arguments are joined with spaces
# into ONE string that the relay's login shell re-parses (`sh -c "$*"`). An
# earlier fake ran `exec "$@"`, which skipped that re-parse and hid a
# separator that the real remote shell executed as a pipe (#2292 review).
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
exec sh -c "$*"
SH
# Fake gh backed by per-repo state under $FAKE_STATE, keyed by OWNER_REPO
# (slash replaced):
#   <key>.noadmin        repo read reports admin=false
#   <key>.alerts         Dependabot alerts enabled
#   <key>.default        default branch name (else "master")
#   <key>.rs/<id>.json   existing rulesets (POST allocates 42, 43, ... per repo)
# Every mutation appends a line to $FAKE_STATE/mutations; every POSTed ruleset
# body is also kept as $FAKE_STATE/posted-<key>-<id>.json.
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
mkdir -p "$S/$k.rs"
case "$method ${rest:-}" in
  "GET vulnerability-alerts")
    [ -e "$S/$k.alerts" ] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; } ;;
  "PUT vulnerability-alerts")
    echo "PUT alerts $owner/$repo" >> "$S/mutations"; : > "$S/$k.alerts" ;;
  "GET rulesets")
    if compgen -G "$S/$k.rs/*.json" >/dev/null; then jq -cs '[.[] | {id, name}]' "$S/$k.rs"/*.json; else echo '[]'; fi ;;
  "POST rulesets")
    n=$(( $(find "$S/$k.rs" -name '*.json' | wc -l) + 42 ))
    body="$(cat)"
    echo "POST ruleset $owner/$repo $(jq -r .name <<<"$body")" >> "$S/mutations"
    printf '%s\n' "$body" > "$S/posted-$k-$n.json"
    jq -c --argjson id "$n" '. + {id: $id}' <<<"$body" > "$S/$k.rs/$n.json"; printf '{"id":%s}\n' "$n" ;;
  GET\ rulesets/*) cat "$S/$k.rs/${rest#rulesets/}.json" ;;
  "GET ")
    if [ -e "$S/$k.noadmin" ]; then echo '{"permissions":{"admin":false}}'
    else
      d=master; [ -e "$S/$k.default" ] && d="$(cat "$S/$k.default")"
      jq -cn --arg d "$d" '{permissions:{admin:true},archived:false,default_branch:$d}'
    fi ;;
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
mkdir -p "$FAKE_STATE/example-org_repo-c.rs"
echo '{"id":7,"name":"default-branch-no-delete-no-force-push","enforcement":"evaluate","conditions":{"ref_name":{"include":["~DEFAULT_BRANCH"],"exclude":[]}},"rules":[{"type":"deletion"}]}' \
  > "$FAKE_STATE/example-org_repo-c.rs/7.json"
bash "$SCRIPT" --operator-approved --apply --repo example-org/repo-c >"$TMP/out" 2>"$TMP/err" && rc=0 || rc=$?
if [ "$rc" -eq 4 ] && jq -se '.[0].status == "incomplete" and .[0].ruleset == "conflict-mismatch(#7)"' "$TMP/out" >/dev/null \
  && ! grep -q 'POST ruleset' "$FAKE_STATE/mutations" 2>/dev/null; then
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

# --- protect-branch= (#2292): PR-only ruleset on a promotion branch ---------
cat > "$TMP/pb-allowlist" <<'LIST'
example-org/repo-p protect-branch=stable   # promotion branch
example-org/repo-q
example-org/repo-q protect-branch=release protect-branch=stable protect-branch=release
LIST

# 10) Dry run reports the extra ruleset and mutates nothing.
reset_state
if bash "$SCRIPT" --operator-approved --allowlist "$TMP/pb-allowlist" --repo example-org/repo-p >"$TMP/out" 2>"$TMP/err" \
  && jq -se '.[0].ruleset == "would-create" and .[0].protected_branches == ["stable:would-create"]' "$TMP/out" >/dev/null \
  && [ ! -e "$FAKE_STATE/mutations" ]; then
  pass
else
  fail "protect-branch dry run invalid: $(cat "$TMP/out" "$TMP/err")"
fi

# 11) Apply creates both rulesets; the branch ruleset targets exactly
#     refs/heads/stable with an independent-approval PR rule and no bypass.
reset_state
if bash "$SCRIPT" --operator-approved --apply --allowlist "$TMP/pb-allowlist" --repo example-org/repo-p >"$TMP/out" 2>"$TMP/err" \
  && jq -se '.[0].status == "ok" and .[0].ruleset == "created(#42)" and .[0].protected_branches == ["stable:created(#43)"]' "$TMP/out" >/dev/null \
  && jq -e '.name == "protected-branch-stable"
      and .conditions.ref_name.include == ["refs/heads/stable"]
      and .bypass_actors == []
      and ([.rules[].type] | sort == ["deletion","non_fast_forward","pull_request"])
      and (.rules[] | select(.type == "pull_request") | .parameters
           | .required_approving_review_count == 1 and .require_last_push_approval
             and .dismiss_stale_reviews_on_push and .allowed_merge_methods == ["merge"])' \
     "$FAKE_STATE/posted-example-org_repo-p-43.json" >/dev/null \
  && [ "$(grep -c 'POST ruleset' "$FAKE_STATE/mutations")" -eq 2 ]; then
  pass
else
  fail "protect-branch apply invalid: $(cat "$TMP/out" "$TMP/err")"
fi

# 12) Second apply is idempotent for the branch ruleset too.
: > "$FAKE_STATE/mutations"
if bash "$SCRIPT" --operator-approved --apply --allowlist "$TMP/pb-allowlist" --repo example-org/repo-p >"$TMP/out" 2>"$TMP/err" \
  && jq -se '.[0].protected_branches == ["stable:already-present(#43)"]' "$TMP/out" >/dev/null \
  && [ ! -s "$FAKE_STATE/mutations" ]; then
  pass
else
  fail "protect-branch second apply not idempotent: $(cat "$TMP/out")"
fi

# 13) A same-name branch ruleset that is weaker in ONE respect is a conflict,
#     never overwritten. One defect per case, so each acceptance condition is
#     pinned on its own (#2292 review: exclude/target/merge/bypass slipped by).
good_rs='{"id":9,"name":"protected-branch-stable","target":"branch","enforcement":"active","bypass_actors":[],"current_user_can_bypass":"never","conditions":{"ref_name":{"include":["refs/heads/stable"],"exclude":[]}},"rules":[{"type":"deletion"},{"type":"non_fast_forward"},{"type":"pull_request","parameters":{"required_approving_review_count":1,"require_last_push_approval":true,"dismiss_stale_reviews_on_push":true,"allowed_merge_methods":["merge"]}}]}'
pr='(.rules[] | select(.type == "pull_request") | .parameters'
for weak in '.bypass_actors = [{"actor_id":1,"actor_type":"RepositoryRole","bypass_mode":"always"}]' \
  'del(.bypass_actors)' \
  '.current_user_can_bypass = "always"' \
  '.enforcement = "evaluate"' \
  '.target = "tag"' \
  '.conditions.ref_name.exclude = ["refs/heads/stable"]' \
  '.conditions.ref_name.include = ["refs/heads/stable","refs/heads/other"]' \
  '.rules += [{"type":"creation"}]' \
  "$pr.require_last_push_approval) = false" \
  "$pr.dismiss_stale_reviews_on_push) = false" \
  "$pr.required_approving_review_count) = 0" \
  "$pr.required_approving_review_count) = null" \
  "$pr.allowed_merge_methods) = [\"merge\",\"squash\"]"; do
  reset_state
  mkdir -p "$FAKE_STATE/example-org_repo-p.rs"
  jq -c "$weak" <<<"$good_rs" > "$FAKE_STATE/example-org_repo-p.rs/9.json"
  bash "$SCRIPT" --operator-approved --apply --allowlist "$TMP/pb-allowlist" --repo example-org/repo-p >"$TMP/out" 2>"$TMP/err" && rc=0 || rc=$?
  if [ "$rc" -eq 4 ] && jq -se '.[0].status == "incomplete" and .[0].protected_branches == ["stable:conflict-mismatch(#9)"]' "$TMP/out" >/dev/null \
    && ! grep -q 'protected-branch-stable' "$FAKE_STATE/mutations" 2>/dev/null; then
    pass
  else
    fail "weaker branch ruleset ($weak) not reported as conflict (rc=$rc): $(cat "$TMP/out")"
  fi
done
# ...and the same ruleset without the defect is accepted as already present.
reset_state
mkdir -p "$FAKE_STATE/example-org_repo-p.rs"
printf '%s\n' "$good_rs" > "$FAKE_STATE/example-org_repo-p.rs/9.json"
if bash "$SCRIPT" --operator-approved --allowlist "$TMP/pb-allowlist" --repo example-org/repo-p >"$TMP/out" 2>"$TMP/err" \
  && jq -se '.[0].protected_branches == ["stable:already-present(#9)"]' "$TMP/out" >/dev/null; then
  pass
else
  fail "a compliant existing branch ruleset was not accepted: $(cat "$TMP/out")"
fi

# 14) The repository's default branch is refused even under another name, and
#     duplicate repo lines merge their branch lists (release, stable once each).
reset_state
echo release > "$FAKE_STATE/example-org_repo-q.default"
bash "$SCRIPT" --operator-approved --apply --allowlist "$TMP/pb-allowlist" --repo example-org/repo-q >"$TMP/out" 2>"$TMP/err" && rc=0 || rc=$?
if [ "$rc" -eq 4 ] \
  && jq -se '.[0].protected_branches == ["release:refused-default-branch","stable:created(#43)"]' "$TMP/out" >/dev/null \
  && ! grep -q 'protected-branch-release' "$FAKE_STATE/mutations"; then
  pass
else
  fail "default-branch refusal or branch merge invalid (rc=$rc): $(cat "$TMP/out")"
fi

# 14b) An unknown default branch (field missing) fails closed for every
#      protect-branch: never risk PR-gating what might be the default branch.
reset_state
cat > "$TMP/bin/gh-nodefault" <<'SH'
#!/usr/bin/env bash
if [ "${1:-}" = api ] && [ "${2:-}" = repos/example-org/repo-p ]; then
  echo '{"permissions":{"admin":true},"archived":false}'; exit 0
fi
exec "$FAKE_GH_REAL" "$@"
SH
chmod +x "$TMP/bin/gh-nodefault"
mkdir -p "$TMP/bin-nodefault"; cp "$TMP/bin/ssh" "$TMP/bin-nodefault/ssh"
ln -sf "$TMP/bin/gh-nodefault" "$TMP/bin-nodefault/gh"
FAKE_GH_REAL="$TMP/bin/gh" PATH="$TMP/bin-nodefault:$PATH" \
  bash "$SCRIPT" --operator-approved --apply --allowlist "$TMP/pb-allowlist" --repo example-org/repo-p >"$TMP/out" 2>"$TMP/err" && rc=0 || rc=$?
if [ "$rc" -eq 4 ] && jq -se '.[0].protected_branches == ["stable:refused-default-branch"]' "$TMP/out" >/dev/null \
  && ! grep -q 'protected-branch-stable' "$FAKE_STATE/mutations" 2>/dev/null; then
  pass
else
  fail "unknown default branch did not fail closed (rc=$rc): $(cat "$TMP/out" "$TMP/err")"
fi

# 14c) Names that look like shell commands are only ever names: through the
#      realistic ssh re-parse nothing executes, plain lines still work, and
#      each repo keeps its own branch list (#2292 review reproduction).
reset_state
cat > "$TMP/inj-allowlist" <<LIST
example-org/repo-a protect-branch=touch
example-org/repo-b protect-branch=cat
example-org/repo-c
LIST
( cd "$TMP" && bash "$SCRIPT" --operator-approved --apply --allowlist "$TMP/inj-allowlist" >"$TMP/out" 2>"$TMP/err" ) && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && [ ! -e "$TMP/example-org" ] \
  && jq -se '(.[-1].summary and .[-1].ok) and (map(select(.repo)) as $r
      | ($r | length) == 3
      and $r[0].protected_branches == ["touch:created(#43)"]
      and $r[1].protected_branches == ["cat:created(#43)"]
      and $r[2].protected_branches == [])' "$TMP/out" >/dev/null; then
  pass
else
  fail "shell-looking names were not handled as plain names (rc=$rc): $(cat "$TMP/out" "$TMP/err")"
fi

# 14d) A '#' starts a comment only at a field boundary; inside a branch token it
#      is refused, never silently truncated to another branch name.
reset_state
printf 'example-org/repo-p protect-branch=fix#123\n' > "$TMP/hash-allowlist"
if bash "$SCRIPT" --operator-approved --allowlist "$TMP/hash-allowlist" >"$TMP/out" 2>"$TMP/err"; then
  fail "a '#' inside a branch token was accepted"
elif grep -q 'invalid protect-branch name' "$TMP/err" && [ ! -e "$FAKE_STATE/mutations" ]; then
  pass
else
  fail "'#' inside a branch token not refused explicitly: $(cat "$TMP/err")"
fi

# 15) Unsafe protect-branch values and unknown tokens are refused locally,
#     before any remote call.
for bad in 'protect-branch=master' 'protect-branch=Main' 'protect-branch=refs/heads/stable' \
  'protect-branch=../stable' 'protect-branch=-x' 'protect-branch=' 'protect=stable' 'protect-branch=a;b' \
  'protect-branch=stäble' 'protect-branch=a|b' 'protect-branch=a:b'; do
  reset_state
  printf 'example-org/repo-p %s\n' "$bad" > "$TMP/bad-pb"
  if bash "$SCRIPT" --operator-approved --allowlist "$TMP/bad-pb" >"$TMP/out" 2>"$TMP/err"; then
    fail "unsafe allowlist token accepted: $bad"
  elif [ ! -e "$FAKE_STATE/mutations" ] && grep -q 'refusing:' "$TMP/err"; then
    pass
  else
    fail "unsafe token refusal not explicit: $bad"
  fi
done

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
