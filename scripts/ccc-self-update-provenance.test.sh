#!/usr/bin/env bash
# shellcheck disable=SC2034  # out/rc/before/setups are read via eval inside ok()
# Commit-provenance gate for ccc-self-update.sh: WHO authored the incoming
# commits. Hermetic: fixture git repos, a PATH-stubbed fake `gh` that serves a
# canned compare response, and a `git` wrapper that reports a GitHub-shaped
# origin URL (the fixture really fetches from a local bare repo). No network.
#
# CCC_TEST_SELFUP points the suite at another copy of the script (e.g. the
# origin/main version, to prove these cases fail without the gate).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SELFUP="${CCC_TEST_SELFUP:-$HERE/ccc-self-update.sh}"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset CODEX_HOME CLAUDE_PROCESS_TIMEOUT CCC_DELEGATED_TASK_STALL_SECONDS
# Never read the node's real health files, cgroups or handler copies.
export CCC_SELF_UPDATE_HEALTH_FILE="$TMP/no-such-health.json"
export CCC_SELF_UPDATE_MATRIX_HEALTH_FILE="$TMP/no-such-matrix-health.json"
export CCC_SELF_UPDATE_HANDE_HEALTH_FILE="$TMP/no-such-hande-health.json"
export CCC_SELF_UPDATE_CGROUP_ROOT="$TMP/no-such-cgroup"
export CCC_SELF_UPDATE_SERVING_HEALTH_FILE="$TMP/no-such-serving-health.json"
export CCC_A2A_HANDLER_SYSTEM_DEST="$TMP/no-such-sbin"
# The signature gate has its own suite; keep gpg out of these runs.
export CCC_SELF_UPDATE_SIGNATURE_MODE=off

BASH_BIN="$(command -v bash)"
REAL_GIT="$(command -v git)"
ORIG_PATH="$PATH"

# --- fixture: origin bare repo, a seed clone that "merges PRs", the node clone -
ORIGIN="$TMP/origin.git"
SEED="$TMP/seed"
REPO="$TMP/node/ccc-node"
git init -q --bare "$ORIGIN"
git init -q -b main "$SEED"
cat > "$SEED/setup.sh" <<'SH'
#!/usr/bin/env bash
echo "setup ran at $(git rev-parse --short HEAD)" >> "${SETUP_MARKER:?}"
SH
printf '%s\n' 'bridge/.env' > "$SEED/.gitignore"
git -C "$SEED" add -A && git -C "$SEED" commit -qm one
git -C "$SEED" remote add origin "$ORIGIN" && git -C "$SEED" push -q origin main
git -C "$ORIGIN" symbolic-ref HEAD refs/heads/main
mkdir -p "$(dirname "$REPO")"
git clone -q "$ORIGIN" "$REPO"

FAKEBIN="$TMP/bin"; GHBIN="$TMP/ghbin"; mkdir -p "$FAKEBIN" "$GHBIN"
cat > "$FAKEBIN/fakesystemctl" <<'SH'
#!/usr/bin/env bash
exit 0
SH
# The origin URL decides which GitHub repo is asked; report a GitHub-shaped one
# while every other git command (including fetch) runs for real.
cat > "$FAKEBIN/git" <<SH
#!/usr/bin/env bash
if [ -n "\${FAKE_ORIGIN_URL:-}" ]; then
  case " \$* " in *" remote get-url origin "*) printf '%s\n' "\$FAKE_ORIGIN_URL"; exit 0 ;; esac
fi
exec $REAL_GIT "\$@"
SH
cat > "$GHBIN/gh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${GH_CALLS:?}"
if [ "${FAKE_GH_RC:-0}" != 0 ]; then echo "HTTP 502: Bad Gateway" >&2; exit "$FAKE_GH_RC"; fi
cat "${FAKE_GH_JSON:?}"
SH
chmod +x "$FAKEBIN/fakesystemctl" "$FAKEBIN/git" "$GHBIN/gh"

# A PATH with every tool except gh: symlink each PATH entry but `gh`.
NOGH="$TMP/nogh-bin"; mkdir -p "$NOGH"
IFS=: read -ra _path_dirs <<<"$ORIG_PATH"
for _d in "${_path_dirs[@]}"; do
  [ -d "$_d" ] || continue
  for _f in "$_d"/*; do
    _n="${_f##*/}"
    [ "$_n" = gh ] && continue
    [ -e "$NOGH/$_n" ] || [ -L "$NOGH/$_n" ] || ln -s "$_f" "$NOGH/$_n"
  done
done

CLAUDE="$TMP/claude"; STATE="$CLAUDE/state"; HOMEDIR="$TMP/home"
mkdir -p "$STATE" "$TMP/hermes" "$TMP/codex" "$HOMEDIR"
printf '%s\n' 'hermes-broker' > "$CLAUDE/self-update.services"
export SETUP_MARKER="$TMP/setup.marker"
export GH_CALLS="$TMP/gh.calls" FAKE_GH_JSON="$TMP/compare.json"
APPROVED="$STATE/self-update-approved-external-commits"
TRUSTED_FILE="$HOMEDIR/.ccc-node/self-update-trusted-authors"
FAILS="$STATE/self-update.provenance-failures"
LOGF="$STATE/self-update.log"

run_selfup() { # extra env via the caller; GH_PATH overrides the gh lookup PATH
  HOME="$HOMEDIR" CCC_CLAUDE_DIR="$CLAUDE" CCC_STATE_DIR="$STATE" CCC_PUSH_SPOOL="$TMP/spool" \
  CCC_HERMES_DIR="$TMP/hermes" CODEX_HOME="$TMP/codex" \
  CCC_SELF_UPDATE_BRIDGE_PROJECT_ROOT="$TMP/project" \
  CCC_SELF_UPDATE_REPO="$REPO" CCC_SELF_UPDATE_SYSTEMCTL="$FAKEBIN/fakesystemctl" \
  CCC_SELF_UPDATE_REAPPLY=0 \
  FAKE_ORIGIN_URL="${FAKE_ORIGIN_URL-https://github.com/jinwon-int/ccc-node.git}" \
  PATH="$FAKEBIN:${GH_PATH:-$GHBIN:$ORIG_PATH}" \
  CCC_SELF_UPDATE_RESTART_WAIT_SECONDS=3 \
  CCC_NODE=testnode "$BASH_BIN" "$SELFUP" run
}

# Land one commit on origin (what a merged PR looks like to the node).
land() { # <name>
  echo "$1" > "$SEED/$1.txt"
  git -C "$SEED" add -A && git -C "$SEED" commit -qm "$1" && git -C "$SEED" push -q origin main
}
# Canned compare response for HEAD..origin tip: one login per incoming commit,
# newest first; `null` = commit email not linked to an account. <total> `-`
# means "as many as listed".
write_compare() { # <total|-> <login>...
  local total="$1" old new i=0 items='[]' login
  shift
  old="$(git -C "$REPO" rev-parse HEAD)"; new="$(git -C "$SEED" rev-parse HEAD)"
  local -a shas
  mapfile -t shas < <(git -C "$SEED" rev-list "$old..$new")
  for login in "$@"; do
    items="$(jq -c --arg sha "${shas[$i]}" --arg login "$login" \
      '. + [{sha:$sha, author:(if $login == "null" then null else {login:$login} end)}]' <<<"$items")"
    i=$((i + 1))
  done
  [ "$total" = - ] && total="$#"
  jq -nc --argjson total "$total" --argjson c "$items" \
    '{status:"ahead", total_commits:$total, commits:$c}' > "$FAKE_GH_JSON"
}
reset_tick() { rm -rf "$TMP/spool"; : > "$GH_CALLS"; : > "$LOGF"; }
head_sha() { git -C "$REPO" rev-parse HEAD; }
seed_sha() { git -C "$SEED" rev-parse HEAD; }
setup_runs() { [ -f "$SETUP_MARKER" ] && wc -l < "$SETUP_MARKER" | tr -d ' ' || echo 0; }
spool_text() { cat "$TMP/spool"/*SelfUpdate*.json 2>/dev/null | jq -r '.text + " " + .dedup'; }

# --- up-to-date tick does not call gh -----------------------------------------
reset_tick
out="$(run_selfup 2>&1)"; rc=$?
ok "up-to-date tick exits 0" '[ "$rc" = 0 ]'
ok "up-to-date tick never calls gh" '[ ! -s "$GH_CALLS" ]'

# --- all incoming authors trusted -> proceeds -----------------------------------
land trusted-1; land trusted-2
reset_tick
old="$(head_sha)"
write_compare - jinon86 'dependabot[bot]'
out="$(run_selfup 2>&1)"; rc=$?
ok "trusted authors: update exits 0" '[ "$rc" = 0 ]'
ok "trusted authors: checkout fast-forwarded" '[ "$(head_sha)" = "$(seed_sha)" ]'
ok "trusted authors: exactly one compare call for old...new" \
  '[ "$(wc -l < "$GH_CALLS")" = 1 ] && grep -qxF "api repos/jinwon-int/ccc-node/compare/$old...$(seed_sha)" "$GH_CALLS"'
ok "trusted authors: success line logged" 'grep -q "provenance ok rev=$(seed_sha) commits=2 approved=0 mode=enforce" "$LOGF"'

# --- external login -> enforce refuses, checkout untouched, owner notified ----
land external-1
reset_tick
before="$(head_sha)"; setups="$(setup_runs)"
write_compare - evil-contrib
out="$(run_selfup 2>&1)"; rc=$?
ext_sha="$(seed_sha)"
ok "external author: enforce exits 16" '[ "$rc" = 16 ]'
ok "external author: HEAD not moved" '[ "$(head_sha)" = "$before" ]'
ok "external author: working tree clean, setup.sh not run" '[ -z "$(git -C "$REPO" status --porcelain)" ] && [ "$(setup_runs)" = "$setups" ]'
ok "external author: owner notified with login + sha12 + approved file" \
  'spool_text | grep -q "외부 작성자.*evil-contrib ${ext_sha:0:12}.*$APPROVED.*stalled-external-author"'
ok "external author: abort logged" 'grep -q "abort reason=external-author .*rev=$ext_sha untrusted=1" "$LOGF"'
ok "external author: clear stderr line" 'grep -q "refusing origin/main ${ext_sha:0:12}: 1 incoming commit(s) by untrusted authors" <<<"$out"'

# --- null author.login -> untrusted ----------------------------------------
reset_tick
write_compare - null
out="$(run_selfup 2>&1)"; rc=$?
ok "unlinked author: exits 16" '[ "$rc" = 16 ] && [ "$(head_sha)" = "$before" ]'
ok "unlinked author: notification says unlinked" 'spool_text | grep -q "unlinked ${ext_sha:0:12}"'

# --- unknown mode value is treated as enforce ------------------------------
reset_tick
write_compare - evil-contrib
out="$(CCC_SELF_UPDATE_PROVENANCE_MODE=bogus run_selfup 2>&1)"; rc=$?
ok "unknown mode: treated as enforce" '[ "$rc" = 16 ] && [ "$(head_sha)" = "$before" ]'

# --- owner-approved SHA lets it through ------------------------------------
reset_tick
printf '# owner approved 2026-10-24\n%s\n' "$ext_sha" > "$APPROVED"
write_compare - evil-contrib
out="$(run_selfup 2>&1)"; rc=$?
ok "approved SHA: update exits 0" '[ "$rc" = 0 ] && [ "$(head_sha)" = "$ext_sha" ]'
ok "approved SHA: counted in success line" 'grep -q "provenance ok rev=$ext_sha commits=1 approved=1" "$LOGF"'
rm -f "$APPROVED"

# --- mixed: only the untrusted commit is reported ---------------------------
land mixed-trusted; land mixed-external
reset_tick
before="$(head_sha)"
write_compare - outsider jinon86
out="$(run_selfup 2>&1)"; rc=$?
ok "mixed: one untrusted of two blocks the tick" '[ "$rc" = 16 ] && [ "$(head_sha)" = "$before" ]'
ok "mixed: only the untrusted commit is listed" \
  'grep -q "untrusted=1 commits=2 authors=outsider:$(seed_sha | cut -c1-12)$" "$LOGF" && ! spool_text | grep -q jinon86'

# --- warn mode reports and proceeds ------------------------------------------
reset_tick
out="$(CCC_SELF_UPDATE_PROVENANCE_MODE=warn run_selfup 2>&1)"; rc=$?
ok "warn: update proceeds" '[ "$rc" = 0 ] && [ "$(head_sha)" = "$(seed_sha)" ]'
ok "warn: untrusted logged and warned" \
  'grep -q "provenance untrusted .*untrusted=1 .*mode=warn proceeding" "$LOGF" && grep -q "WARNING 1 incoming commit(s) by untrusted authors" <<<"$out"'
ok "warn: no stall notification" '! spool_text | grep -q stalled-external-author'

# --- gh missing -> hold (exit 16); alert only on the 2nd consecutive tick ----
land nogh
reset_tick
before="$(head_sha)"
write_compare - jinon86
rm -f "$FAILS"
out="$(GH_PATH="$NOGH" run_selfup 2>&1)"; rc=$?
ok "gh missing: holds with exit 16" '[ "$rc" = 16 ] && [ "$(head_sha)" = "$before" ]'
ok "gh missing: unverifiable abort logged" 'grep -q "abort reason=provenance-unverifiable .*detail=no-gh consecutive=1" "$LOGF"'
ok "gh missing: first failure does not notify" '! spool_text | grep -q stalled-provenance-unverifiable'
ok "gh missing: failure counter = 1" '[ "$(cat "$FAILS")" = 1 ]'
reset_tick
out="$(GH_PATH="$NOGH" run_selfup 2>&1)"; rc=$?
ok "gh missing: second consecutive failure still holds" '[ "$rc" = 16 ] && [ "$(head_sha)" = "$before" ]'
ok "gh missing: second consecutive failure notifies" 'spool_text | grep -q "2회 연속.*no-gh.*stalled-provenance-unverifiable"'

# --- warn mode never blocks on a lookup error (ON_ERROR defaults to proceed) --
reset_tick
out="$(CCC_SELF_UPDATE_PROVENANCE_MODE=warn GH_PATH="$NOGH" run_selfup 2>&1)"; rc=$?
ok "warn + gh missing: proceeds" '[ "$rc" = 0 ] && [ "$(head_sha)" = "$(seed_sha)" ]'
ok "warn + gh missing: hold counter untouched" '[ "$(cat "$FAILS")" = 2 ]'

# --- a successful lookup clears the failure counter --------------------------
land recovered
reset_tick
write_compare - seoseo-ai
out="$(run_selfup 2>&1)"; rc=$?
ok "recovered lookup: proceeds" '[ "$rc" = 0 ] && [ "$(head_sha)" = "$(seed_sha)" ]'
ok "recovered lookup: failure counter removed" '[ ! -e "$FAILS" ]'

# --- ON_ERROR=proceed continues on a lookup error ----------------------------
land proceed
reset_tick
out="$(CCC_SELF_UPDATE_PROVENANCE_ON_ERROR=proceed GH_PATH="$NOGH" run_selfup 2>&1)"; rc=$?
ok "ON_ERROR=proceed: update proceeds" '[ "$rc" = 0 ] && [ "$(head_sha)" = "$(seed_sha)" ]'
ok "ON_ERROR=proceed: warning logged" 'grep -q "provenance unverifiable .*detail=no-gh .*on_error=proceed proceeding" "$LOGF"'

# --- incomplete compare (total_commits > returned) -> lookup error ------------
land incomplete
reset_tick
before="$(head_sha)"
write_compare 300 jinon86
out="$(run_selfup 2>&1)"; rc=$?
ok "incomplete compare: holds" '[ "$rc" = 16 ] && [ "$(head_sha)" = "$before" ]'
ok "incomplete compare: logged as incomplete" 'grep -q "detail=incomplete total=300 returned=1" "$LOGF"'

# --- malformed JSON / API failure / commit missing from compare -> error ------
reset_tick
printf 'not json' > "$FAKE_GH_JSON"
out="$(run_selfup 2>&1)"; rc=$?
ok "malformed JSON: holds" '[ "$rc" = 16 ] && grep -q "detail=malformed-json" "$LOGF"'
reset_tick
printf '{"message":"Not Found"}' > "$FAKE_GH_JSON"
out="$(run_selfup 2>&1)"; rc=$?
ok "error-shaped JSON: holds" '[ "$rc" = 16 ] && grep -q "detail=malformed-json" "$LOGF"'
reset_tick
write_compare - jinon86
out="$(FAKE_GH_RC=1 run_selfup 2>&1)"; rc=$?
ok "API failure: holds" '[ "$rc" = 16 ] && grep -q "detail=api-failed rc=1" "$LOGF" && [ "$(head_sha)" = "$before" ]'
land second-uncovered
reset_tick
write_compare 1 jinon86   # two incoming commits, compare accounts for one
out="$(run_selfup 2>&1)"; rc=$?
ok "commit missing from compare: holds" '[ "$rc" = 16 ] && grep -q "detail=commit-missing-from-compare" "$LOGF" && [ "$(head_sha)" = "$before" ]'

# --- non-GitHub remote -> lookup error; SSH remote form accepted -------------
reset_tick
write_compare - jinon86 jinon86
out="$(FAKE_ORIGIN_URL='https://gitlab.example/jinwon-int/ccc-node.git' run_selfup 2>&1)"; rc=$?
ok "non-GitHub remote: holds without calling gh" '[ "$rc" = 16 ] && grep -q "detail=non-github-remote" "$LOGF" && [ ! -s "$GH_CALLS" ]'
reset_tick
out="$(FAKE_ORIGIN_URL='git@github.com:jinwon-int/ccc-node.git' run_selfup 2>&1)"; rc=$?
ok "SSH remote: accepted and resolved to owner/repo" \
  '[ "$rc" = 0 ] && grep -q "^api repos/jinwon-int/ccc-node/compare/" "$GH_CALLS" && [ "$(head_sha)" = "$(seed_sha)" ]'

# --- trusted-authors file REPLACES the built-in list -------------------------
land trusted-file
mkdir -p "$(dirname "$TRUSTED_FILE")"
printf '# node-local list\nalice   # the only trusted author\n' > "$TRUSTED_FILE"
reset_tick
before="$(head_sha)"
write_compare - jinon86
out="$(run_selfup 2>&1)"; rc=$?
ok "trusted file: built-in login no longer trusted" '[ "$rc" = 16 ] && [ "$(head_sha)" = "$before" ] && spool_text | grep -q "jinon86 "'
reset_tick
write_compare - Alice
out="$(run_selfup 2>&1)"; rc=$?
ok "trusted file: listed login trusted (case-insensitive)" '[ "$rc" = 0 ] && [ "$(head_sha)" = "$(seed_sha)" ]'
rm -f "$TRUSTED_FILE"

# --- off mode skips the lookup entirely ------------------------------------
land off
reset_tick
write_compare - evil-contrib
out="$(CCC_SELF_UPDATE_PROVENANCE_MODE=off run_selfup 2>&1)"; rc=$?
ok "off: proceeds without calling gh" '[ "$rc" = 0 ] && [ ! -s "$GH_CALLS" ] && [ "$(head_sha)" = "$(seed_sha)" ]'

# --- wiring: the gate sits before the merge; trust is never read from the checkout
ok "gate runs before the ff-merge" \
  'awk "/verify_commit_provenance \"\\\$OLD_SHA\"/ {g=NR} /merge --ff-only \"origin\\/\\\$BRANCH\"/ && !m {m=NR} END {exit !(g && m && g < m)}" "$SELFUP"'
ok "trust list defaults never resolve inside the repo" \
  '! grep -E "PROVENANCE_(TRUSTED|APPROVED)_FILE=.*\\\$REPO" "$SELFUP"'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
