#!/usr/bin/env bash
# Tests for setup.sh backup safety.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETUP="$ROOT/setup.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

home="$TMP/home"
mkdir -p "$home/.claude" "$TMP/bin"
printf '{"existing":true}\n' > "$home/.claude/settings.json"

cat > "$TMP/bin/tar" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  -czf) printf 'not a tar archive\n' > "$2"; exit 0 ;;
  -tzf) exit 1 ;;
esac
exec /usr/bin/tar "$@"
EOF
chmod +x "$TMP/bin/tar"

settings_before="$(cat "$home/.claude/settings.json")"
out="$(HOME="$home" PATH="$TMP/bin:$PATH" bash "$SETUP" 2>&1)"; rc=$?
settings_after="$(cat "$home/.claude/settings.json")"

ok "setup fails closed when backup tar validation fails" '[ "$rc" = 1 ] && grep -q "Backup validation failed" <<<"$out"'
ok "setup leaves existing settings untouched after failed backup validation" '[ "$settings_before" = "$settings_after" ]'

out="$(HOME="$home" PATH="$TMP/bin:$PATH" bash "$SETUP" --no-backup 2>&1)"; rc=$?
settings_after="$(cat "$home/.claude/settings.json")"
ok "setup validates the private rollback snapshot before installing" \
  '[ "$rc" != 0 ] && [ "$settings_before" = "$settings_after" ]'

nonroot_home="$TMP/nonroot-home"
nonroot_claude="$TMP/custom-claude"
nonroot_hermes="$TMP/custom-hermes"
nonroot_wiki="$TMP/custom-wiki-agent/bin/wiki-agent"
nonroot_bridge="$TMP/nonroot-workspace"
out="$(HOME="$nonroot_home" CCC_CLAUDE_DIR="$nonroot_claude" CCC_HERMES_DIR="$nonroot_hermes" CCC_WIKI_AGENT_BIN="$nonroot_wiki" CCC_BRIDGE_DEFAULT_PATH="$nonroot_bridge" bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "setup dry-run accepts explicit non-root path overrides" '[ "$rc" = 0 ] && grep -q "$nonroot_claude/CLAUDE.md" <<<"$out" && grep -q "$nonroot_hermes/honcho.json" <<<"$out" && grep -q "$nonroot_wiki" <<<"$out" && grep -q -- "--path $nonroot_bridge" <<<"$out"'
escaped_hooks="$(printf '%q' "$nonroot_claude/hooks")"
ok "setup dry-run renders the shared argv plan with shell escaping" \
  'grep -Fq -- "[dry-run] mkdir -p $escaped_hooks" <<<"$out"'
ok "setup executor does not evaluate command strings" \
  '! grep -Eq "(^|[[:space:]])eval([[:space:]]|$)" "$SETUP"'
ok "setup non-root dry-run avoids hardcoded root paths in checklist" '! grep -q "/root/.wiki-agent/bin/wiki-agent" <<<"$out" && ! grep -q -- "--path /root" <<<"$out"'
ok "setup non-root dry-run writes nothing to override dirs" '[ ! -e "$nonroot_claude" ] && [ ! -e "$nonroot_hermes" ]'

out="$(HOME="$TMP/root-guard-home" CCC_CLAUDE_DIR=/ CCC_HERMES_DIR="$TMP/root-guard-hermes" bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "setup refuses filesystem-root Claude install target" '[ "$rc" = 2 ] && grep -q "filesystem-root" <<<"$out"'
out="$(HOME="$TMP/root-guard-home" CCC_CLAUDE_DIR="$TMP/root-guard-claude" CCC_HERMES_DIR=/ bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "setup refuses filesystem-root Hermes install target" '[ "$rc" = 2 ] && grep -q "filesystem-root" <<<"$out"'

# Paths are data, never shell source. The historical run() helper passed these
# values through eval, so a quote plus command separator could execute a second
# command during an otherwise harmless install.
inject_marker="$TMP/setup-command-injection"
inject_claude="$TMP/claude'"$'\n'"; touch '$inject_marker'; #"
out="$(HOME="$TMP/inject-home" CCC_CLAUDE_DIR="$inject_claude" \
  CCC_HERMES_DIR="$TMP/inject-hermes" bash "$SETUP" --no-backup 2>&1)"; rc=$?
ok "setup treats quote and metacharacter paths as literal argv" \
  '[ "$rc" = 0 ] && [ ! -e "$inject_marker" ] && [ -f "$inject_claude/settings.json" ]'

# A failed staging copy must not leave a mixed old/new install. Inject a cp
# failure after setup has begun and compare representative managed artifacts.
txn_claude="$TMP/txn-claude"
txn_hermes="$TMP/txn-hermes"
mkdir -p "$txn_claude/hooks" "$txn_hermes" "$TMP/fail-bin"
printf '%s\n' '{"old":true}' > "$txn_claude/settings.json"
printf '%s\n' 'old-hook' > "$txn_claude/hooks/old-local.sh"
ln -s "$TMP/missing-node-local-settings" "$txn_claude/settings.local.json"
settings_txn_before="$(sha256sum "$txn_claude/settings.json")"
hook_txn_before="$(sha256sum "$txn_claude/hooks/old-local.sh")"
cat > "$TMP/fail-bin/cp" <<'EOF'
#!/usr/bin/env bash
count_file="${CCC_TEST_CP_COUNT:?}"
count="$(cat "$count_file" 2>/dev/null || echo 0)"
count=$((count + 1)); printf '%s' "$count" > "$count_file"
[ "$count" -eq "${CCC_TEST_CP_FAIL_AT:-3}" ] && exit 91
exec /bin/cp "$@"
EOF
chmod +x "$TMP/fail-bin/cp"
out="$(HOME="$TMP/txn-home" PATH="$TMP/fail-bin:$PATH" CCC_TEST_CP_COUNT="$TMP/cp.count" \
  CCC_CLAUDE_DIR="$txn_claude" CCC_HERMES_DIR="$txn_hermes" \
  bash "$SETUP" --no-backup 2>&1)"; rc=$?
ok "setup injected staging failure exits non-zero" '[ "$rc" != 0 ]'
ok "setup staging failure preserves installed artifacts byte-for-byte" \
  '[ "$(sha256sum "$txn_claude/settings.json")" = "$settings_txn_before" ] && [ "$(sha256sum "$txn_claude/hooks/old-local.sh")" = "$hook_txn_before" ] && [ -L "$txn_claude/settings.local.json" ] && [ "$(readlink "$txn_claude/settings.local.json")" = "$TMP/missing-node-local-settings" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
