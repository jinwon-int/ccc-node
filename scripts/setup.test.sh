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

out="$(HOME="$TMP/root-guard-home" CCC_CLAUDE_DIR=/tmp/.. CCC_HERMES_DIR="$TMP/root-guard-hermes" bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "setup refuses normalized filesystem-root aliases" '[ "$rc" = 2 ] && grep -q "filesystem-root" <<<"$out"'

mkdir -p "$TMP/live-claude-target"
ln -s "$TMP/live-claude-target" "$TMP/live-claude-link"
out="$(HOME="$TMP/root-guard-home" CCC_CLAUDE_DIR="$TMP/live-claude-link" CCC_HERMES_DIR="$TMP/root-guard-hermes" bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "setup refuses install roots with symlink components" '[ "$rc" = 2 ] && grep -q "symlink" <<<"$out"'

managed_link_claude="$TMP/managed-link-claude"
mkdir -p "$managed_link_claude" "$TMP/external-hooks"
ln -s "$TMP/external-hooks" "$managed_link_claude/hooks"
out="$(HOME="$TMP/root-guard-home" CCC_CLAUDE_DIR="$managed_link_claude" CCC_HERMES_DIR="$TMP/root-guard-hermes" bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "setup refuses managed artifact symlinks before mutation" '[ "$rc" = 2 ] && grep -q "managed artifact symlink" <<<"$out" && [ -z "$(find "$TMP/external-hooks" -mindepth 1 -print -quit)" ]'

hardlink_claude="$TMP/hardlink-claude"
mkdir -p "$hardlink_claude"
printf '%s\n' '{"shared":true}' > "$TMP/shared-settings.json"
ln "$TMP/shared-settings.json" "$hardlink_claude/settings.json"
out="$(HOME="$TMP/root-guard-home" CCC_CLAUDE_DIR="$hardlink_claude" CCC_HERMES_DIR="$TMP/root-guard-hermes" bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "setup refuses managed artifact hardlinks before mutation" \
  '[ "$rc" = 2 ] && grep -q "managed artifact hardlink" <<<"$out" && grep -q "shared" "$TMP/shared-settings.json"'

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
printf '%s\n' '{"oldLocal":true}' > "$txn_claude/settings.local.json"
settings_txn_before="$(sha256sum "$txn_claude/settings.json")"
hook_txn_before="$(sha256sum "$txn_claude/hooks/old-local.sh")"
local_txn_before="$(sha256sum "$txn_claude/settings.local.json")"
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
  '[ "$(sha256sum "$txn_claude/settings.json")" = "$settings_txn_before" ] && [ "$(sha256sum "$txn_claude/hooks/old-local.sh")" = "$hook_txn_before" ] && [ "$(sha256sum "$txn_claude/settings.local.json")" = "$local_txn_before" ]'

# Hook settings merge is collision-safe at the mechanism layer even though the
# canonical base/overlay event sets remain disjoint by policy. Base hooks run
# first, overlay hooks second, and unrelated top-level settings are preserved.
merge_filter="$ROOT/scripts/merge-settings.jq"
merge_base="$TMP/merge-base.json"
merge_overlay="$TMP/merge-overlay.json"
merge_out="$TMP/merge-out.json"
printf '%s\n' '{"model":"base","hooks":{"SessionStart":[{"hooks":[{"command":"base-start"}]}]}}' > "$merge_base"
printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"command":"overlay-start"}]}],"Stop":[{"hooks":[{"command":"overlay-stop"}]}]}}' > "$merge_overlay"
if [ -f "$merge_filter" ]; then
  jq -s -f "$merge_filter" "$merge_base" "$merge_overlay" > "$merge_out" 2>/dev/null
  merge_rc=$?
else
  merge_rc=127
fi
ok "settings merge preserves both sides of a colliding hook event" \
  '[ "$merge_rc" = 0 ] && jq -e '\''(.hooks.SessionStart | length) == 2 and .hooks.SessionStart[0].hooks[0].command == "base-start" and .hooks.SessionStart[1].hooks[0].command == "overlay-start"'\'' "$merge_out" >/dev/null'
ok "settings merge preserves overlay-only events and base top-level settings" \
  'jq -e '\''.model == "base" and .hooks.Stop[0].hooks[0].command == "overlay-stop"'\'' "$merge_out" >/dev/null'

printf '%s\n' '{"model":"base-without-hooks"}' > "$merge_base"
printf '%s\n' '{"hooks":{"Stop":[{"hooks":[{"command":"overlay-stop"}]}]}}' > "$merge_overlay"
missing_base_out="$TMP/merge-missing-base.json"
jq -s -f "$merge_filter" "$merge_base" "$merge_overlay" > "$missing_base_out" 2>/dev/null
missing_base_rc=$?
ok "settings merge accepts a base without hooks" \
  '[ "$missing_base_rc" = 0 ] && jq -e '\''.model == "base-without-hooks" and (.hooks.Stop | length) == 1'\'' "$missing_base_out" >/dev/null'

printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"command":"base-start"}]}]}}' > "$merge_base"
printf '%s\n' '{"permissions":{"allow":[]}}' > "$merge_overlay"
missing_overlay_out="$TMP/merge-missing-overlay.json"
jq -s -f "$merge_filter" "$merge_base" "$merge_overlay" > "$missing_overlay_out" 2>/dev/null
missing_overlay_rc=$?
ok "settings merge accepts an overlay without hooks" \
  '[ "$missing_overlay_rc" = 0 ] && jq -e '\''(.hooks.SessionStart | length) == 1'\'' "$missing_overlay_out" >/dev/null'

printf '%s\n' '{"hooks":{"SessionStart":{}}}' > "$merge_base"
printf '%s\n' '{"hooks":{"SessionStart":[]}}' > "$merge_overlay"
jq -s -f "$merge_filter" "$merge_base" "$merge_overlay" > /dev/null 2>&1
invalid_hook_rc=$?
ok "settings merge rejects non-array hook event values" '[ "$invalid_hook_rc" != 0 ]'
ok "setup uses the tracked collision-safe settings merge filter" \
  'grep -Fq '\''jq -s -f "$SRC/scripts/merge-settings.jq"'\'' "$SETUP"'

# HOME-path rewriting is source-driven. Existing node-local files outside the
# installed harness must not be scanned or rewritten.
rewrite_claude="$TMP/rewrite-claude"
rewrite_hermes="$TMP/rewrite-hermes"
mkdir -p "$rewrite_claude"
printf '%s\n' 'credential-note=/root/.claude/private' > "$rewrite_claude/.credentials.json"
credential_before="$(sha256sum "$rewrite_claude/.credentials.json")"
out="$(HOME="$TMP/rewrite-home" CCC_CLAUDE_DIR="$rewrite_claude" CCC_HERMES_DIR="$rewrite_hermes" bash "$SETUP" --no-backup 2>&1)"; rc=$?
ok "custom-path rewrite leaves node-local credentials untouched" \
  '[ "$rc" = 0 ] && [ "$(sha256sum "$rewrite_claude/.credentials.json")" = "$credential_before" ]'
ok "setup deploys the shared path library beside installed self-update" \
  '[ -x "$rewrite_claude/hooks/lib/harness-paths.sh" ] && [ -x "$rewrite_claude/hooks/lib/harness_paths.py" ] && cmp -s "$ROOT/scripts/lib/harness-paths.sh" "$rewrite_claude/hooks/lib/harness-paths.sh" && cmp -s "$ROOT/scripts/lib/harness_paths.py" "$rewrite_claude/hooks/lib/harness_paths.py" && grep -Fq "lib/harness-paths.sh" "$rewrite_claude/hooks/ccc-self-update.sh"'
ok "setup installs the Codex launcher and materializer as executable managed hooks" \
  '[ -x "$rewrite_claude/hooks/ccc-codex" ] && [ -x "$rewrite_claude/hooks/ccc_codex_memory.py" ] && cmp -s "$ROOT/scripts/ccc-codex" "$rewrite_claude/hooks/ccc-codex" && cmp -s "$ROOT/scripts/ccc_codex_memory.py" "$rewrite_claude/hooks/ccc_codex_memory.py"'
codex_dry_out="$(HOME="$nonroot_home" CCC_CLAUDE_DIR="$nonroot_claude" CCC_HERMES_DIR="$nonroot_hermes" CCC_WIKI_AGENT_BIN="$nonroot_wiki" CCC_BRIDGE_DEFAULT_PATH="$nonroot_bridge" bash "$SETUP" --dry-run 2>&1)"; codex_dry_rc=$?
ok "setup non-root dry-run includes both Codex managed launch artifacts" \
  '[ "$codex_dry_rc" = 0 ] && grep -Fq "$nonroot_claude/hooks/ccc-codex" <<<"$codex_dry_out" && grep -Fq "$nonroot_claude/hooks/ccc_codex_memory.py" <<<"$codex_dry_out"'

# --- #454: settings.local.json is node-local — seeded if absent, never clobbered ---
seed_home="$TMP/seed-home"; seed_claude="$TMP/seed-claude"; seed_hermes="$TMP/seed-hermes"
HOME="$seed_home" CCC_CLAUDE_DIR="$seed_claude" CCC_HERMES_DIR="$seed_hermes" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "setup seeds settings.local.json when absent" '[ -f "$seed_claude/settings.local.json" ]'
ok "seeded settings.local.json carries no broad fleet-wide grants" \
  'jq -e ".permissions.allow == []" "$seed_claude/settings.local.json" >/dev/null'

# A node's accumulated/hand-added approvals must survive a re-run (the self-update path).
printf '%s\n' '{"permissions":{"allow":["Bash(node-local-only:*)"]}}' > "$seed_claude/settings.local.json"
local_before="$(sha256sum "$seed_claude/settings.local.json")"
HOME="$seed_home" CCC_CLAUDE_DIR="$seed_claude" CCC_HERMES_DIR="$seed_hermes" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "re-run does NOT clobber existing settings.local.json (node-local preserved)" \
  '[ "$(sha256sum "$seed_claude/settings.local.json")" = "$local_before" ] && grep -q "node-local-only" "$seed_claude/settings.local.json"'

# --- A2A worker sub-agent roster is worker-role-gated (nexus-drift fix) ---
# Default / broker: the a2a-* roster is NOT installed, so the only A2A entry
# point stays the nexus/broker flow. Worker nodes opt in via CCC_A2A_ROLE=worker.
a2a_home="$TMP/a2a-home"; a2a_claude="$TMP/a2a-claude"; a2a_hermes="$TMP/a2a-hermes"
HOME="$a2a_home" CCC_CLAUDE_DIR="$a2a_claude" CCC_HERMES_DIR="$a2a_hermes" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "default (broker) install ships no a2a-* worker roster" \
  '[ -z "$(ls "$a2a_claude/agents/"a2a-*.md 2>/dev/null)" ]'

# Broker cleanup: a pre-existing roster is removed on a non-worker install.
mkdir -p "$a2a_claude/agents"; printf 'x\n' > "$a2a_claude/agents/a2a-explorer.md"
HOME="$a2a_home" CCC_CLAUDE_DIR="$a2a_claude" CCC_HERMES_DIR="$a2a_hermes" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "non-worker install removes a stale a2a-* roster" '[ ! -e "$a2a_claude/agents/a2a-explorer.md" ]'

# Worker role: opt in, roster installed, and the choice is persisted to a marker.
wk_home="$TMP/wk-home"; wk_claude="$TMP/wk-claude"; wk_hermes="$TMP/wk-hermes"
HOME="$wk_home" CCC_CLAUDE_DIR="$wk_claude" CCC_HERMES_DIR="$wk_hermes" \
  CCC_A2A_ROLE=worker bash "$SETUP" --no-backup >/dev/null 2>&1
ok "CCC_A2A_ROLE=worker installs the a2a-* roster" \
  '[ -f "$wk_claude/agents/a2a-explorer.md" ] && [ -f "$wk_claude/agents/a2a-verifier.md" ]'
ok "worker role choice is persisted to a node-local marker" \
  '[ "$(cat "$wk_claude/a2a-role" 2>/dev/null)" = worker ]'

# Marker persistence: an unattended self-update (no env) honors the marker.
HOME="$wk_home" CCC_CLAUDE_DIR="$wk_claude" CCC_HERMES_DIR="$wk_hermes" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "env-less re-run keeps the roster via the persisted marker" \
  '[ -f "$wk_claude/agents/a2a-implementer.md" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
