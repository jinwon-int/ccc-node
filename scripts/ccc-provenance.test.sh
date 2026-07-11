#!/usr/bin/env bash
# Regression tests for canonical ccc-node bridge provenance/update routing.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
START="$ROOT/bridge/start.sh"
pass=0
fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() {
  local name="$1"; shift
  if eval "$*"; then
    printf 'ok - %s\n' "$name"
    pass=$((pass + 1))
  else
    printf 'not ok - %s\n' "$name"
    fail=$((fail + 1))
  fi
}

FIXTURE="$TMP/repo"
mkdir -p "$FIXTURE/bridge" "$FIXTURE/scripts" "$TMP/project" "$TMP/home" "$TMP/bin"
cp "$START" "$FIXTURE/bridge/start.sh"
printf 'fixture requirements\n' > "$FIXTURE/bridge/requirements.txt"
printf '[project]\nname="fixture"\nversion="0"\n' > "$FIXTURE/bridge/pyproject.toml"
cat > "$FIXTURE/scripts/ccc-version.sh" <<'EOF'
#!/usr/bin/env bash
if [ "${FAKE_VERSION_RC:-0}" -ne 0 ]; then
  exit "$FAKE_VERSION_RC"
fi
printf 'v9.9.9-3-gabc1234\n'
EOF
cat > "$FIXTURE/scripts/ccc-self-update.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s|%s|%s\n' "${CCC_SELF_UPDATE_REPO:-}" "${CCC_SELF_UPDATE_BRANCH:-}" "$*" >> "$FAKE_UPDATE_LOG"
exit "${FAKE_UPDATE_RC:-0}"
EOF
cat > "$TMP/bin/curl" <<'EOF'
#!/usr/bin/env bash
printf 'curl-called\n' >> "$FAKE_CURL_LOG"
exit 97
EOF
chmod +x "$FIXTURE/bridge/start.sh" "$FIXTURE/scripts/ccc-version.sh" \
  "$FIXTURE/scripts/ccc-self-update.sh" "$TMP/bin/curl"
git -C "$FIXTURE" init -q
git -C "$FIXTURE" remote add origin https://github.com/jinwon-int/ccc-node.git

COMMON_ENV=(
  "HOME=$TMP/home"
  "PATH=$TMP/bin:$PATH"
  "FAKE_UPDATE_LOG=$TMP/update.log"
  "FAKE_CURL_LOG=$TMP/curl.log"
)

out="$(env "${COMMON_ENV[@]}" bash "$FIXTURE/bridge/start.sh" --path "$TMP/project" --version 2>&1)"; rc=$?
ok "--version exits zero" '[ "$rc" = 0 ]'
ok "--version reports checkout identity" 'grep -q "v9.9.9-3-gabc1234" <<<"$out"'
ok "--version does not invoke network release lookup" '[ ! -e "$TMP/curl.log" ]'

: > "$TMP/update.log"
out="$(env "${COMMON_ENV[@]}" bash "$FIXTURE/bridge/start.sh" --path "$TMP/project" --upgrade 2>&1)"; rc=$?
ok "--upgrade exits zero through canonical updater" '[ "$rc" = 0 ]'
ok "--upgrade delegates exact repo main run contract" 'grep -Fxq "$FIXTURE|main|run" "$TMP/update.log"'
ok "--upgrade completion derives installed checkout identity" 'grep -q "v9.9.9-3-gabc1234" <<<"$out"'
ok "--upgrade never invokes upstream release API" '[ ! -e "$TMP/curl.log" ]'

: > "$TMP/update.log"
out="$(env "${COMMON_ENV[@]}" CCC_SELF_UPDATE_BRANCH=feature/drift bash "$FIXTURE/bridge/start.sh" --path "$TMP/project" --upgrade 2>&1)"; rc=$?
ok "bridge compatibility updater pins canonical main despite caller env" '[ "$rc" = 0 ] && grep -Fxq "$FIXTURE|main|run" "$TMP/update.log"'

: > "$TMP/update.log"
git -C "$FIXTURE" remote set-url origin https://github.com/terranc/claude-telegram-bot-bridge.git
out="$(env "${COMMON_ENV[@]}" bash "$FIXTURE/bridge/start.sh" --path "$TMP/project" --upgrade 2>&1)"; rc=$?
ok "--upgrade rejects non-canonical origin" '[ "$rc" -ne 0 ]'
ok "rejected origin never reaches updater" '[ ! -s "$TMP/update.log" ]'
ok "rejected origin does not claim completion" '! grep -q "Upgrade complete" <<<"$out"'

git -C "$FIXTURE" remote set-url origin https://example-token@github.com/jinwon-int/ccc-node.git
out="$(env "${COMMON_ENV[@]}" bash "$FIXTURE/bridge/start.sh" --path "$TMP/project" --upgrade 2>&1)"; rc=$?
ok "credential-bearing canonical-looking origin is rejected" '[ "$rc" -ne 0 ]'
ok "credential-bearing origin value is never printed" '! grep -q "example-token" <<<"$out"'
git -C "$FIXTURE" remote set-url origin https://github.com/jinwon-int/ccc-node.git

: > "$TMP/update.log"
out="$(env "${COMMON_ENV[@]}" FAKE_VERSION_RC=9 bash "$FIXTURE/bridge/start.sh" --path "$TMP/project" --upgrade 2>&1)"; rc=$?
ok "--upgrade fails when installed checkout identity cannot be derived" '[ "$rc" -ne 0 ]'
ok "missing post-update identity does not claim completion" '! grep -q "Upgrade complete" <<<"$out"'

: > "$TMP/update.log"
out="$(env "${COMMON_ENV[@]}" FAKE_UPDATE_RC=8 bash "$FIXTURE/bridge/start.sh" --path "$TMP/project" --upgrade 2>&1)"; rc=$?
ok "--upgrade preserves canonical updater deferred exit" '[ "$rc" = 8 ]'
ok "--upgrade failure does not claim completion" '! grep -q "Upgrade complete" <<<"$out"'

ok "runtime start script has no terranc updater URL" '! grep -q "api.github.com/repos/terranc/claude-telegram-bot-bridge" "$START"'
ok "English quickstart clones canonical repository" 'grep -q "git clone https://github.com/jinwon-int/ccc-node" "$ROOT/bridge/README.md"'
ok "Chinese quickstart clones canonical repository" 'grep -q "git clone https://github.com/jinwon-int/ccc-node" "$ROOT/bridge/README-zh.md"'
ok "version/provenance contract is documented" '[ -f "$ROOT/docs/version-and-provenance.md" ] && grep -q "scripts/ccc-version.sh" "$ROOT/docs/version-and-provenance.md" && grep -q "scripts/ccc-self-update.sh" "$ROOT/docs/version-and-provenance.md"'

printf 'PASS=%d FAIL=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
