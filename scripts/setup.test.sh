#!/usr/bin/env bash
# Tests for setup.sh backup safety.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETUP="$ROOT/setup.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# Route every setup-run systemd interaction away from the live tree (#885).
# A full (non-dry-run) $SETUP run reaches bridge/service-systemd.sh reconcile;
# without this suite-wide seam a root test run rewrites the real
# /etc/systemd/system/ccc-telegram-bridge.service with the case's scratch
# $HOME — observed live on dungae 2026-08-03, where a leaked $TMP/wk-home
# HOME poisoned the node's session storage. Cases that exercise the seam
# explicitly still override these per invocation.
export CCC_SYSTEMD_DIR="$TMP/systemd-seam"
export CCC_SYSTEMD_SCOPE=user
mkdir -p "$CCC_SYSTEMD_DIR"
printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP/systemctl-stub"
chmod +x "$TMP/systemctl-stub"
export CCC_SYSTEMCTL="$TMP/systemctl-stub"

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
ok "setup dry-run does not create Codex plugin policy state" '[ ! -e "$nonroot_home/.codex" ]'
ok "setup dry-run reports Codex managed skills without creating CODEX_HOME" \
  '[ "$rc" = 0 ] && grep -Fq "ccc-doctor" <<<"$out" && grep -Fq "create" <<<"$out" && [ ! -e "$nonroot_home/.codex" ]'

# systemd transient units / timers do not export HOME (only User= services get
# it), and ccc-self-update runs setup.sh headless from exactly such contexts —
# with NO CCC_* overrides, so the `${CCC_*:-$HOME/...}` defaults dereference
# $HOME. Overrides must stay absent here: `${VAR:-word}` never evaluates the
# default when VAR is set, which would mask the bug this test pins.
# Regression: gwakga 2026-08-02 — `set -u` aborted at the first $HOME default
# ("setup.sh: line 87: HOME: unbound variable") and self-update rolled back.
out="$(env -u HOME bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "setup dry-run survives unset HOME (systemd/cron context)" \
  '[ "$rc" = 0 ] && ! grep -q "HOME: unbound variable" <<<"$out"'

# setup/self-update wiring must reach the canonical bridge renderer while
# remaining mutation-free in dry-run. Use only fixture paths and fake systemctl.
setup_sd="$TMP/setup-systemd"
setup_project="$TMP/setup-project"
setup_systemctl="$TMP/setup-systemctl"
setup_systemctl_calls="$TMP/setup-systemctl.calls"
mkdir -p "$setup_project"
printf '#!/usr/bin/env bash\necho "$*" >> "%s"\n' "$setup_systemctl_calls" > "$setup_systemctl"
chmod +x "$setup_systemctl"
HOME="$nonroot_home" CCC_SYSTEMD_DIR="$setup_sd" CCC_SYSTEMD_SCOPE=user \
  CCC_SYSTEMCTL="$setup_systemctl" bash "$ROOT/bridge/service-systemd.sh" \
  install --project-root "$setup_project" >/dev/null 2>&1
setup_unit="$setup_sd/ccc-telegram-bridge.service"
sed -i 's/^Restart=always$/Restart=on-failure/' "$setup_unit"
setup_unit_before="$(sha256sum "$setup_unit")"
: > "$setup_systemctl_calls"
out="$(HOME="$nonroot_home" CCC_CLAUDE_DIR="$nonroot_claude" \
  CCC_HERMES_DIR="$nonroot_hermes" CCC_WIKI_AGENT_BIN="$nonroot_wiki" \
  CCC_SYSTEMD_DIR="$setup_sd" CCC_SYSTEMD_SCOPE=user \
  CCC_SYSTEMCTL="$setup_systemctl" bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "setup dry-run explicitly detects existing systemd unit drift" \
  '[ "$rc" = 0 ] && grep -q "systemd unit drift detected" <<<"$out"'
ok "setup dry-run leaves systemd unit and daemon untouched" \
  '[ "$(sha256sum "$setup_unit")" = "$setup_unit_before" ] && [ ! -s "$setup_systemctl_calls" ]'

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
mkdir -p "$TMP/rewrite-home/.piri/agent"
printf '%s\n' 'credential-note=/root/.claude/private' > "$rewrite_claude/.credentials.json"
credential_before="$(sha256sum "$rewrite_claude/.credentials.json")"
out="$(HOME="$TMP/rewrite-home" CCC_CLAUDE_DIR="$rewrite_claude" CCC_HERMES_DIR="$rewrite_hermes" bash "$SETUP" --no-backup 2>&1)"; rc=$?
ok "custom-path rewrite leaves node-local credentials untouched" \
  '[ "$rc" = 0 ] && [ "$(sha256sum "$rewrite_claude/.credentials.json")" = "$credential_before" ]'
ok "setup installs the Codex common managed skill set with provenance" \
  '[ -f "$TMP/rewrite-home/.codex/skills/ccc-doctor/SKILL.md" ] && [ -f "$TMP/rewrite-home/.codex/skills/ccc-node-status/SKILL.md" ] && [ -f "$TMP/rewrite-home/.codex/skills/ccc-security-audit/SKILL.md" ] && [ -f "$TMP/rewrite-home/.codex/skills/ccc-agent-cron/SKILL.md" ] && [ -f "$TMP/rewrite-home/.codex/skills/ccc-self-update/SKILL.md" ] && [ -f "$TMP/rewrite-home/.codex/skills/ccc-wiki-record/SKILL.md" ] && jq -e ".manager == \"ccc-node\"" "$TMP/rewrite-home/.codex/skills/ccc-doctor/.ccc-node-managed.json" >/dev/null'
ok "setup installs the opt-in central skill promoter executable" \
  '[ -x "$rewrite_claude/hooks/ccc-skill-promotion.py" ] && cmp -s "$ROOT/scripts/ccc-skill-promotion.py" "$rewrite_claude/hooks/ccc-skill-promotion.py"'
ok "setup installs the exact-commit private skill sync executable" \
  '[ -x "$rewrite_claude/hooks/ccc-fleet-skills-sync.py" ] && cmp -s "$ROOT/scripts/ccc-fleet-skills-sync.py" "$rewrite_claude/hooks/ccc-fleet-skills-sync.py"'
rewrite_agent_cron="$rewrite_claude/state/agent-cron/tasks.json"
ok "setup registers the self-update command task against the real agent-cron contract" \
  'jq -e --arg hook "$rewrite_claude/hooks/ccc-self-update.sh" '\''[.tasks[] | select(.id == "self-update" and .enabled == true and .notify == "telegram-owner-on-failure" and .successExitCodes == [0,8,11] and .payload.kind == "command" and .payload.argv == [$hook,"run"] and (.prompt | length > 0))] | length == 1'\'' "$rewrite_agent_cron" >/dev/null'
# --- #1042: reinstall must REPLACE managed files via rename, never truncate the
# installed inode in place. bash reads a running script incrementally by inode,
# so an in-place cp made the cron-run installed self-update hook (which invokes
# setup.sh) crash mid-run with a spurious syntax error — after the repo update
# but before service restart / audit record (silent half-apply, 9/12 fleet
# nodes on 2026-08-07). The staged temp is created while the old inode is still
# linked, so a rename-based install ALWAYS changes the destination inode.
selfupdate_ino_before="$(stat -c '%i' "$rewrite_claude/hooks/ccc-self-update.sh")"
hooktree_ino_before="$(stat -c '%i' "$rewrite_claude/hooks/checkpoint.sh")"
HOME="$TMP/rewrite-home" CCC_CLAUDE_DIR="$rewrite_claude" CCC_HERMES_DIR="$rewrite_hermes" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "setup self-update task registration is idempotent" \
  '[ "$(jq '\''[.tasks[] | select(.id == "self-update")] | length'\'' "$rewrite_agent_cron")" = 1 ]'
ok "reinstall replaces the self-update hook inode (rename, never in-place truncate)" \
  '[ "$(stat -c "%i" "$rewrite_claude/hooks/ccc-self-update.sh")" != "$selfupdate_ino_before" ] && [ -x "$rewrite_claude/hooks/ccc-self-update.sh" ] && grep -Fq "lib/harness-paths.sh" "$rewrite_claude/hooks/ccc-self-update.sh"'
ok "reinstall replaces hook-tree inodes and keeps them executable" \
  '[ "$(stat -c "%i" "$rewrite_claude/hooks/checkpoint.sh")" != "$hooktree_ino_before" ] && [ -x "$rewrite_claude/hooks/checkpoint.sh" ]'
ok "atomic staging leaves no hidden temp files behind" \
  '[ -z "$(find "$rewrite_claude" -name ".*.??????" 2>/dev/null)" ]'
ok "setup installs the Piri web skill when a Piri agent dir exists" \
  '[ -f "$TMP/rewrite-home/.piri/agent/skills/web/SKILL.md" ] && [ -x "$TMP/rewrite-home/.piri/agent/skills/web/web_search.py" ] && [ -x "$TMP/rewrite-home/.piri/agent/skills/web/web_fetch.py" ] && cmp -s "$ROOT/piri/skills/web/web_search.py" "$TMP/rewrite-home/.piri/agent/skills/web/web_search.py"'
# Repo skills install as refreshed copies from the claude + shared trees, with
# a manifest-driven prune for skills the repo no longer ships. Real dirs only
# — the managed-artifact guard refuses symlinks by design (harness_paths.py).
legacy_home="$TMP/legacy-home"
legacy_claude="$legacy_home/.claude"
mkdir -p "$legacy_claude/skills/wiki-record" "$legacy_claude/skills/node-local-only" \
  "$legacy_claude/skills/ghost-skill" "$legacy_claude/skills/edited-skill" "$legacy_claude/state"
printf 'stale copy\n' > "$legacy_claude/skills/wiki-record/SKILL.md"
printf 'node-local\n' > "$legacy_claude/skills/node-local-only/SKILL.md"
printf 'ghost\n' > "$legacy_claude/skills/ghost-skill/SKILL.md"
printf 'edited\n' > "$legacy_claude/skills/edited-skill/SKILL.md"
ghost_hash="$(cd "$legacy_claude/skills/ghost-skill" && find . -type f -exec sha256sum {} + | sort -k2 | sha256sum | awk '{print $1}')"
printf 'ghost-skill %s\nedited-skill %s\n' "$ghost_hash" "deadbeef" > "$legacy_claude/state/repo-skills.manifest"
HOME="$legacy_home" CCC_CLAUDE_DIR="$legacy_claude" CCC_HERMES_DIR="$legacy_home/.hermes" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "setup refreshes repo skill copies from the claude + shared trees" \
  'cmp -s "$legacy_claude/skills/wiki-record/SKILL.md" "$ROOT/skills/shared/wiki-record/SKILL.md" && [ ! -L "$legacy_claude/skills/wiki-record" ] && [ -f "$legacy_claude/skills/gh-pr-flow/SKILL.md" ] && ! grep -q "stale copy" "$legacy_claude/skills/wiki-record/SKILL.md"'
ok "setup prunes repo-removed skills when the copy is unmodified" \
  '[ ! -e "$legacy_claude/skills/ghost-skill" ]'
ok "setup keeps repo-removed skills the node edited locally" \
  '[ -f "$legacy_claude/skills/edited-skill/SKILL.md" ]'
ok "setup leaves node-local skills untouched" \
  'grep -q "node-local" "$legacy_claude/skills/node-local-only/SKILL.md"'
ok "setup records the installed skill set in the manifest" \
  'grep -q "^wiki-record " "$legacy_claude/state/repo-skills.manifest" && grep -q "^gh-pr-flow " "$legacy_claude/state/repo-skills.manifest"'
# Recording the name is not enough: the canonical-path rewrite edits installed
# skill files after they were hashed, so without a re-record every rewritten
# skill (gh-pr-flow, mcp-add, self-update all embed /opt/ccc-node) carries a
# stale hash and the prune above can never fire for it. This case runs on a
# non-canonical CLAUDE_DIR *and* checkout, so the rewrite really does fire.
manifest_hashes_match() { # manifest_hashes_match <claude-dir>
  local root="$1" sname shash actual
  [ -f "$root/state/repo-skills.manifest" ] || return 1
  while read -r sname shash; do
    [ -n "$sname" ] || continue
    [ -d "$root/skills/$sname" ] || continue
    actual="$(cd "$root/skills/$sname" && find . -type f -exec sha256sum {} + | sort -k2 | sha256sum | awk '{print $1}')"
    [ "$actual" = "$shash" ] || { echo "  manifest hash drift: $sname" >&2; return 1; }
  done < "$root/state/repo-skills.manifest"
  return 0
}
ok "setup records manifest hashes matching the installed bytes after the rewrite" \
  'manifest_hashes_match "$legacy_claude"'
# Slash commands invoke repo scripts verbatim; installed copies must point at
# THIS checkout, not the canonical /opt/ccc-node (broken on e.g. /root/ccc-node
# nodes). Repo templates stay canonical — only installed copies are rewritten.
ok "setup rewrites the canonical repo path into installed slash commands" \
  'grep -Fq "$ROOT/scripts/ccc-doctor.sh" "$rewrite_claude/commands/doctor.md" && grep -Fq "git -C $ROOT status" "$rewrite_claude/commands/node-status.md"'
if [ "$ROOT" != "/opt/ccc-node" ]; then
  ok "setup leaves no stale /opt/ccc-node reference in installed commands" \
    '! grep -rq "/opt/ccc-node" "$rewrite_claude/commands"'
  # Skills are rewritten too, and this fixture has a Piri agent dir — which is
  # what makes the case load bearing (#1072). install_repo_skills_into runs a
  # second time for Piri, so the rewrite must read a snapshot of the CLAUDE
  # install, not whatever the function last left behind. Without the snapshot
  # the skill rewrite silently targets the Piri set and every canonical path in
  # an installed Claude skill survives, unrewritten, on a non-canonical node.
  ok "setup rewrites the canonical repo path inside installed skills" \
    'grep -Fq "$ROOT" "$rewrite_claude/skills/self-update/check.sh" && ! grep -Fq "/opt/ccc-node" "$rewrite_claude/skills/self-update/check.sh"'
  ok "setup leaves no stale /opt/ccc-node reference in installed skills" \
    '! grep -rq "/opt/ccc-node" "$rewrite_claude/skills"'
fi
# ccc_doctor diagnoses skills from SKILL_SOURCE_ROOTS and applies the SAME
# canonical rewrite before comparing. A root that setup installs from but the
# rewrite never covers therefore reads as permanent phantom drift on every
# non-canonical node — and --fix refuses skill paths, so doctor cannot clear its
# own report. Pin the two lists together the way canonical-paths.test.sh already
# pins the canonical constants across files (#1072).
skill_roots_agree() {
  local declared doctor
  declared="$(grep -oE '"\$SRC/(claude/skills|skills/shared)"' "$SETUP" | tr -d '"' | sed "s|\$SRC/||" | sort -u | tr '\n' ' ')"
  doctor="$(python3 - "$ROOT/scripts/ccc_doctor.py" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        getattr(t, "id", "") == "SKILL_SOURCE_ROOTS" for t in node.targets
    ):
        print(" ".join(sorted(ast.literal_eval(node.value))) + " ")
        break
PY
)"
  [ -n "$declared" ] && [ "$declared" = "$doctor" ]
}
ok "setup installs skills from exactly the roots ccc_doctor diagnoses" \
  'skill_roots_agree'
# Non-cascading regression (PR #563 review): a checkout under a path containing
# /root/.claude must keep its freshly inserted $SRC intact — the harness-dir
# pair must not rescan and corrupt the repo-path pair's output.
cascade_src="$TMP/root/.claude/src"
mkdir -p "$cascade_src"
tar -C "$ROOT" --exclude=.git --exclude=bridge/venv --exclude=bridge/logs \
  --exclude=.harness-tmp -cf - . 2>/dev/null | tar -xf - -C "$cascade_src"
cascade_claude="$TMP/cascade-claude"
out="$(HOME="$TMP/cascade-home" CCC_CLAUDE_DIR="$cascade_claude" \
  CCC_HERMES_DIR="$TMP/cascade-hermes" bash "$cascade_src/setup.sh" --no-backup 2>&1)"; rc=$?
ok "setup from a /root/.claude-containing checkout installs commands pointing at that checkout" \
  '[ "$rc" = 0 ] && grep -Fq "$cascade_src/scripts/ccc-doctor.sh" "$cascade_claude/commands/doctor.md"'
ok "cascade regression: installed commands never point into the harness dir" \
  '! grep -Fq "$cascade_claude/scripts" "$cascade_claude/commands/doctor.md"'
# Unsafe checkout paths are rejected up-front (PR #563 review): $SRC is embedded
# verbatim into slash-command shell text, so whitespace/metacharacter paths must
# refuse to install rather than produce broken unquoted commands.
space_src="$TMP/space dir/src"
mkdir -p "$space_src/scripts/lib"
cp "$ROOT/setup.sh" "$space_src/setup.sh"
cp "$ROOT/scripts/lib/harness-paths.sh" "$ROOT/scripts/lib/harness_paths.py" "$space_src/scripts/lib/"
out="$(HOME="$TMP/space-home" CCC_CLAUDE_DIR="$TMP/space-claude" \
  CCC_HERMES_DIR="$TMP/space-hermes" bash "$space_src/setup.sh" --dry-run 2>&1)"; rc=$?
ok "setup refuses a checkout path unsafe for slash-command embedding" \
  '[ "$rc" = 2 ] && grep -q "unsafe for installed slash commands" <<<"$out" && [ ! -e "$TMP/space-claude" ]'
ok "setup deploys the shared path library beside installed self-update" \
  '[ -x "$rewrite_claude/hooks/lib/harness-paths.sh" ] && [ -x "$rewrite_claude/hooks/lib/harness_paths.py" ] && cmp -s "$ROOT/scripts/lib/harness-paths.sh" "$rewrite_claude/hooks/lib/harness-paths.sh" && cmp -s "$ROOT/scripts/lib/harness_paths.py" "$rewrite_claude/hooks/lib/harness_paths.py" && grep -Fq "lib/harness-paths.sh" "$rewrite_claude/hooks/ccc-self-update.sh"'
# checkpoint.sh/distill.sh source lib/mtime-prune.sh behind an if-readable
# guard; without deploying it, standalone-node pruning is a silent no-op.
ok "setup deploys the mtime-prune library the pruning hooks source" \
  '[ -x "$rewrite_claude/hooks/lib/mtime-prune.sh" ] && cmp -s "$ROOT/claude/hooks/lib/mtime-prune.sh" "$rewrite_claude/hooks/lib/mtime-prune.sh"'
ok "setup installs the Codex launcher and materializer as executable managed hooks" \
  '[ -x "$rewrite_claude/hooks/ccc-codex" ] && [ -x "$rewrite_claude/hooks/ccc_codex_memory.py" ] && cmp -s "$ROOT/scripts/ccc-codex" "$rewrite_claude/hooks/ccc-codex" && cmp -s "$ROOT/scripts/ccc_codex_memory.py" "$rewrite_claude/hooks/ccc_codex_memory.py"'
ok "setup installs the Piri launcher as an executable managed hook" \
  '[ -x "$rewrite_claude/hooks/ccc-piri" ] && cmp -s "$ROOT/scripts/ccc-piri" "$rewrite_claude/hooks/ccc-piri"'
ok "setup installs the managed nunchi Codex loader" \
  '[ -x "$rewrite_claude/hooks/nunchi/codex-loader.py" ] && cmp -s "$ROOT/claude/hooks/nunchi/codex-loader.py" "$rewrite_claude/hooks/nunchi/codex-loader.py"'
ok "setup installs the body-free memory readiness probe beside memory-check" \
  '[ -f "$rewrite_claude/hooks/ccc_memory_probe.py" ] && [ ! -x "$rewrite_claude/hooks/ccc_memory_probe.py" ] && cmp -s "$ROOT/scripts/ccc_memory_probe.py" "$rewrite_claude/hooks/ccc_memory_probe.py"'
ok "setup installs the canonical secure-fs helper beside the Codex materializer" \
  '[ -f "$rewrite_claude/hooks/ccc_secure_fs.py" ] && [ ! -x "$rewrite_claude/hooks/ccc_secure_fs.py" ] && cmp -s "$ROOT/bridge/utils/secure_fs.py" "$rewrite_claude/hooks/ccc_secure_fs.py"'
ok "setup installs the canonical journal core for the pending-v1 adapter" \
  '[ -f "$rewrite_claude/hooks/ccc_journal_core.py" ] && [ ! -x "$rewrite_claude/hooks/ccc_journal_core.py" ] && cmp -s "$ROOT/bridge/memory/journal_core.py" "$rewrite_claude/hooks/ccc_journal_core.py" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= python3 -S "$rewrite_claude/hooks/distill/pending_journal.py" --help >/dev/null 2>&1'
ok "setup installs one canonical local-memory transaction module for both providers" \
  '[ -f "$rewrite_claude/hooks/ccc_local_memory_transaction.py" ] && [ ! -x "$rewrite_claude/hooks/ccc_local_memory_transaction.py" ] && cmp -s "$ROOT/bridge/memory/local_memory_transaction.py" "$rewrite_claude/hooks/ccc_local_memory_transaction.py"'
ok "installed Codex materializer imports its colocated secure-fs helper" \
  'PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= "$rewrite_claude/hooks/ccc_codex_memory.py" --help >/dev/null 2>&1'
ok "installed local-memory transaction imports its colocated secure-fs helper" \
  'PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= python3 "$rewrite_claude/hooks/ccc_local_memory_transaction.py" --help >/dev/null 2>&1'
ok "source-checkout local-memory transaction imports canonical secure-fs directly" \
  'PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= python3 -S "$ROOT/bridge/memory/local_memory_transaction.py" --help >/dev/null 2>&1'
codex_dry_out="$(HOME="$nonroot_home" CCC_CLAUDE_DIR="$nonroot_claude" CCC_HERMES_DIR="$nonroot_hermes" CCC_WIKI_AGENT_BIN="$nonroot_wiki" CCC_BRIDGE_DEFAULT_PATH="$nonroot_bridge" bash "$SETUP" --dry-run 2>&1)"; codex_dry_rc=$?
ok "setup non-root dry-run includes all Codex managed launch artifacts" \
  '[ "$codex_dry_rc" = 0 ] && grep -Fq "$nonroot_claude/hooks/ccc-codex" <<<"$codex_dry_out" && grep -Fq "$nonroot_claude/hooks/ccc-piri" <<<"$codex_dry_out" && grep -Fq "$nonroot_claude/hooks/ccc_codex_memory.py" <<<"$codex_dry_out" && grep -Fq "$nonroot_claude/hooks/ccc_secure_fs.py" <<<"$codex_dry_out" && grep -Fq "$nonroot_claude/hooks/ccc_local_memory_transaction.py" <<<"$codex_dry_out"'

# --- #569: hook-tree walk — deploys recursively, excludes tests/bytecode/wiring,
# and dry-run only RENDERS the walk (no copies). rewrite_claude is a real install.
ok "hook-tree walk deploys nested lib/ and distill/ files preserving structure" \
  '[ -x "$rewrite_claude/hooks/lib/hook-common.sh" ] && [ -x "$rewrite_claude/hooks/distill/resume-write.sh" ] && [ -x "$rewrite_claude/hooks/skill-review/autoinstall.sh" ] && [ -x "$rewrite_claude/hooks/skill-review/ownership.py" ] && cmp -s "$ROOT/claude/hooks/distill/resume-write.sh" "$rewrite_claude/hooks/distill/resume-write.sh" && cmp -s "$ROOT/claude/hooks/skill-review/ownership.py" "$rewrite_claude/hooks/skill-review/ownership.py"'
ok "hook-tree walk installs top-level hooks executable including .py collectors" \
  '[ -x "$rewrite_claude/hooks/checkpoint.sh" ] && [ -x "$rewrite_claude/hooks/scan-injection.sh" ] && [ -x "$rewrite_claude/hooks/statusline-usage.py" ]'
ok "hook-tree walk installs lifecycle feed and opaque-ref helper" \
  '[ -x "$rewrite_claude/hooks/lifecycle-feed.sh" ] && [ -x "$rewrite_claude/hooks/lib/lifecycle-common.sh" ]'
ok "installed lifecycle feed points at this checkout bridge venv" \
  'grep -Fq "$ROOT/bridge/venv/bin/python" "$rewrite_claude/hooks/lifecycle-feed.sh"'
ok "hook-tree walk excludes tests, fixtures, bytecode, and settings-compose wiring" \
  '[ ! -e "$rewrite_claude/hooks/redact.test.sh" ] && [ ! -e "$rewrite_claude/hooks/distill/extract.test.sh" ] && [ ! -e "$rewrite_claude/hooks/lib/test-stub.sh" ] && [ ! -e "$rewrite_claude/hooks/__pycache__" ] && [ ! -e "$rewrite_claude/hooks/hooks.json" ] && [ ! -e "$rewrite_claude/hooks/enforcement-overlay.json" ]'
ok "hook-tree walk dry-run renders nested hook copies without writing anything" \
  '[ "$codex_dry_rc" = 0 ] && grep -Fq "$nonroot_claude/hooks/distill/resume-write.sh" <<<"$codex_dry_out" && grep -Fq "$nonroot_claude/hooks/lib/mtime-prune.sh" <<<"$codex_dry_out" && grep -Fq "$nonroot_claude/hooks/lifecycle-feed.sh" <<<"$codex_dry_out" && [ ! -e "$nonroot_claude" ]'

# --- #454: settings.local.json is node-local — seeded if absent, never clobbered ---
seed_home="$TMP/seed-home"; seed_claude="$TMP/seed-claude"; seed_hermes="$TMP/seed-hermes"
HOME="$seed_home" CCC_CLAUDE_DIR="$seed_claude" CCC_HERMES_DIR="$seed_hermes" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "setup seeds settings.local.json when absent" '[ -f "$seed_claude/settings.local.json" ]'
# Claude Code refuses bypassPermissions under root, so the installed default is
# root-aware: kept when Claude runs non-root, dropped (guard remains) when root.
if [ "$(id -u)" -eq 0 ]; then
  ok "setup drops bypassPermissions default when the setup user is root" \
    'jq -e ".permissions.defaultMode != \"bypassPermissions\"" "$seed_claude/settings.json" >/dev/null'
else
  ok "setup installs Claude bypassPermissions as the native default mode (non-root)" \
    'jq -e ".permissions.defaultMode == \"bypassPermissions\"" "$seed_claude/settings.json" >/dev/null'
fi
ok "seeded settings.local.json carries no broad fleet-wide grants" \
  'jq -e ".permissions.allow == []" "$seed_claude/settings.local.json" >/dev/null'
ok "setup disables the OpenAI-curated GitHub plugin for gh CLI-first operation" \
  'grep -Fq '\''[plugins."github@openai-curated-remote"]'\'' "$seed_home/.codex/config.toml" && grep -Fq '\''enabled = false'\'' "$seed_home/.codex/config.toml"'

policy_home="$TMP/policy-home"
policy_claude="$TMP/policy-claude"
policy_hermes="$TMP/policy-hermes"
policy_codex="$TMP/policy-codex"
mkdir -p "$policy_codex"
chmod 700 "$policy_codex"
printf '%s\n' \
  '# preserve-this-comment' \
  'sentinel = "KEEP"' \
  '' \
  '[plugins."github@openai-curated-remote"]' \
  'enabled = true # connector-first old state' > "$policy_codex/config.toml"
chmod 600 "$policy_codex/config.toml"  # contract-compliant config under any umask (#772)
HOME="$policy_home" CODEX_HOME="$policy_codex" CCC_CLAUDE_DIR="$policy_claude" \
  CCC_HERMES_DIR="$policy_hermes" bash "$SETUP" --no-backup >/dev/null 2>&1
policy_rc=$?
ok "setup honors CODEX_HOME while preserving unrelated Codex config" \
  '[ "$policy_rc" = 0 ] && grep -Fq '\''sentinel = "KEEP"'\'' "$policy_codex/config.toml" && grep -Fq '\''# preserve-this-comment'\'' "$policy_codex/config.toml" && grep -Fq '\''enabled = false # connector-first old state'\'' "$policy_codex/config.toml"'
ok "setup honors CODEX_HOME for managed skills" \
  '[ -f "$policy_codex/skills/ccc-doctor/SKILL.md" ] && [ "$(stat -c %a "$policy_codex/skills/ccc-doctor/SKILL.md")" = 600 ]'

# --- rollback covers Codex GitHub policy state (#1131) ----------------------
# Fail AFTER the transport policy step has applied (the managed-skills step is
# the next python3 invocation) and assert the install transaction puts
# $CODEX_DIR back exactly: a pre-existing config.toml returns byte-for-byte,
# and a $CODEX_DIR the failed run created disappears again.
mkdir -p "$TMP/fail-skills-bin"
cat > "$TMP/fail-skills-bin/python3" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  case "\$a" in *ccc_codex_skills.py) exit 93 ;; esac
done
exec "$(command -v python3)" "\$@"
EOF
chmod +x "$TMP/fail-skills-bin/python3"

codex_txn_codex="$TMP/codex-txn-codex"
mkdir -p "$codex_txn_codex"
chmod 700 "$codex_txn_codex"
printf '%s\n' 'sentinel = "RESTORE-ME"' '' '[plugins."github@openai-curated-remote"]' 'enabled = true' > "$codex_txn_codex/config.toml"
chmod 600 "$codex_txn_codex/config.toml"
codex_txn_cfg_before="$(sha256sum "$codex_txn_codex/config.toml")"
out="$(HOME="$TMP/codex-txn-home" PATH="$TMP/fail-skills-bin:$PATH" \
  CCC_CLAUDE_DIR="$TMP/codex-txn-claude" CCC_HERMES_DIR="$TMP/codex-txn-hermes" CODEX_HOME="$codex_txn_codex" \
  bash "$SETUP" --no-backup 2>&1)"; rc=$?
ok "setup failure after the Codex policy step exits non-zero" '[ "$rc" != 0 ]'
ok "rollback restores a pre-existing Codex config.toml byte-for-byte (#1131)" \
  '[ "$(sha256sum "$codex_txn_codex/config.toml")" = "$codex_txn_cfg_before" ]'
ok "rollback report names the Codex policy config in the restored scope (#1131)" \
  'grep -Fq "restored previous installed artifacts (Claude harness, honcho.json, Codex GitHub policy config)" <<<"$out"'

codex_new_codex="$TMP/codex-new-codex"
out="$(HOME="$TMP/codex-new-home" PATH="$TMP/fail-skills-bin:$PATH" \
  CCC_CLAUDE_DIR="$TMP/codex-new-claude" CCC_HERMES_DIR="$TMP/codex-new-hermes" CODEX_HOME="$codex_new_codex" \
  bash "$SETUP" --no-backup 2>&1)"; rc=$?
ok "setup failure after the policy step on a codex-less node exits non-zero" '[ "$rc" != 0 ]'
ok "rollback removes a Codex dir the failed run created (#1131)" '[ ! -e "$codex_new_codex" ]'

# Root-run Claude would reject --dangerously-skip-permissions, so setup must
# neutralize the bypassPermissions default when the run user is root. Simulate
# root deterministically with the setup test seam, which is accepted only when
# the install target resolves beneath the caller's writable /tmp root.
root_claude="$TMP/root-bypass-claude"
HOME="$TMP/root-bypass-home" CCC_CLAUDE_DIR="$root_claude" CCC_HERMES_DIR="$TMP/root-bypass-hermes" \
  CCC_SETUP_TEST_EUID=0 \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "setup neutralizes bypassPermissions default when Claude runs as root" \
  'jq -e ".permissions.defaultMode != \"bypassPermissions\"" "$root_claude/settings.json" >/dev/null'
ok "root-neutralized settings.json still parses with its permissions block intact" \
  'jq -e ".permissions.allow | type == \"array\"" "$root_claude/settings.json" >/dev/null'
# TM-1306 native posture: installed settings must carry NO native deny
# backstop and NO PreToolUse guard wiring (operator decision — the semantic
# guard was removed from the enforcement path).
ok "root-neutralized settings carry no native deny backstop (TM-1306)" \
  'jq -e '\''(.permissions.deny // []) | length == 0'\'' "$root_claude/settings.json" >/dev/null'
ok "root install wires no PreToolUse guard (TM-1306)" \
  'jq -e '\''(.hooks.PreToolUse // []) | length == 0'\'' "$root_claude/settings.json" >/dev/null'

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

# #958: setup records the install-source repo path for ccc-self-update.
ok "setup records install-source repo for self-update" \
  '[ "$(cat "$wk_claude/self-update.repo" 2>/dev/null)" = "$ROOT" ]'

# An operator-owned override is never silently rewritten by a later setup run
# from a different checkout. (Copy the WORKING TREE, not a git clone — a clone
# would miss uncommitted changes under test.)
other_checkout="$TMP/other-checkout"
mkdir -p "$other_checkout"
(cd "$ROOT" && tar cf - --exclude=.git .) | (cd "$other_checkout" && tar xf -)
printf '%s\n' '/operator/custom/repo' > "$wk_claude/self-update.repo"
out="$(cd "$other_checkout" && HOME="$wk_home" CCC_CLAUDE_DIR="$wk_claude" CCC_HERMES_DIR="$wk_hermes" bash ./setup.sh --no-backup 2>&1)"; rc=$?
ok "setup preserves a differing operator override with a warning" \
  '[ "$(cat "$wk_claude/self-update.repo")" = "/operator/custom/repo" ] && grep -q "preserving the operator override" <<<"$out"'

# Dry-run records nothing.
dry_claude="$TMP/dry-claude-958"
out="$(HOME="$TMP/dry-home-958" CCC_CLAUDE_DIR="$dry_claude" CCC_HERMES_DIR="$TMP/dry-hermes-958" bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "setup dry-run does not write self-update.repo" '[ ! -e "$dry_claude/self-update.repo" ]'

# #973: setup installs the versioned live-backups rotate script.
lb_home="$TMP/lb-home-973"; lb_claude="$TMP/lb-claude-973"; lb_hermes="$TMP/lb-hermes-973"
HOME="$lb_home" CCC_CLAUDE_DIR="$lb_claude" CCC_HERMES_DIR="$lb_hermes" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "setup installs the versioned live-backups rotate script" \
  '[ -x "$lb_home/.ccc-node/scripts/ccc-live-backups-rotate.sh" ] && grep -q "CCC_LIVE_BACKUPS_ROOTS" "$lb_home/.ccc-node/scripts/ccc-live-backups-rotate.sh"'
# #968: Termux Rust toolchain handling.
tm_home="$TMP/tm-home"; tm_claude="$TMP/tm-claude"; tm_hermes="$TMP/tm-hermes"; tm_bin="$TMP/tm-bin"
mkdir -p "$tm_bin"
printf '#!/usr/bin/env bash\necho "$@" >> "%s"\nexit 0\n' "$TMP/tm-pkg.calls" > "$tm_bin/pkg"
chmod +x "$tm_bin/pkg"
out="$(HOME="$tm_home" CCC_CLAUDE_DIR="$tm_claude" CCC_HERMES_DIR="$tm_hermes" \
  TERMUX_VERSION=0.118 PATH="$tm_bin:/usr/local/bin:/usr/bin:/bin" bash "$SETUP" --no-backup 2>&1)"; rc=$?
ok "Termux without cargo installs the Rust toolchain via pkg" \
  '[ "$rc" = 0 ] && grep -q "rust rust-std-aarch64-linux-android" "$TMP/tm-pkg.calls"'

printf '#!/usr/bin/env bash\necho "cargo 1.97.1"\nexit 0\n' > "$tm_bin/cargo"
chmod +x "$tm_bin/cargo"
: > "$TMP/tm-pkg.calls"
out="$(HOME="$tm_home" CCC_CLAUDE_DIR="$tm_claude" CCC_HERMES_DIR="$tm_hermes" \
  TERMUX_VERSION=0.118 PATH="$tm_bin:/usr/local/bin:/usr/bin:/bin" bash "$SETUP" --no-backup 2>&1)"; rc=$?
ok "Termux with cargo skips pkg install" \
  '[ "$rc" = 0 ] && [ ! -s "$TMP/tm-pkg.calls" ] && grep -q "Rust toolchain present" <<<"$out"'

rm -f "$tm_bin/cargo"
: > "$TMP/tm-pkg.calls"
out="$(HOME="$tm_home" CCC_CLAUDE_DIR="$tm_claude" CCC_HERMES_DIR="$tm_hermes" \
  TERMUX_VERSION=0.118 PATH="$tm_bin:/usr/local/bin:/usr/bin:/bin" bash "$SETUP" --dry-run 2>&1)"; rc=$?
ok "Termux dry-run prints but does not run pkg install" \
  'grep -q "dry-run. pkg install -y rust rust-std-aarch64-linux-android" <<<"$out" && [ ! -s "$TMP/tm-pkg.calls" ]'

# #1235: settings.json is recomposed from repo templates on every run, so the
# node-local `model` pin has to be carried across explicitly or self-update
# erases it on the next changed tick.
mp_home="$TMP/mp-home-1235"; mp_claude="$TMP/mp-claude-1235"; mp_hermes="$TMP/mp-hermes-1235"
mkdir -p "$mp_claude"
printf '{"model":"claude-fable-5"}\n' > "$mp_claude/settings.json"
out="$(HOME="$mp_home" CCC_CLAUDE_DIR="$mp_claude" CCC_HERMES_DIR="$mp_hermes" \
  bash "$SETUP" --no-backup 2>&1)"; rc=$?
ok "setup preserves the node-local model pin across a rebuild" \
  '[ "$rc" = 0 ] && [ "$(jq -r .model "$mp_claude/settings.json")" = "claude-fable-5" ]'
ok "the rebuilt settings still carry the repo-owned keys" \
  '[ "$(jq -r "has(\"hooks\") and has(\"permissions\")" "$mp_claude/settings.json")" = true ]'

# A second run is the self-update case: the pin must survive repeatedly, not
# just the first rebuild after it was set.
HOME="$mp_home" CCC_CLAUDE_DIR="$mp_claude" CCC_HERMES_DIR="$mp_hermes" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "the model pin survives a repeat (self-update) run" \
  '[ "$(jq -r .model "$mp_claude/settings.json")" = "claude-fable-5" ]'

# No pin present: setup must not invent one.
np_claude="$TMP/np-claude-1235"
HOME="$TMP/np-home-1235" CCC_CLAUDE_DIR="$np_claude" CCC_HERMES_DIR="$TMP/np-hermes-1235" \
  bash "$SETUP" --no-backup >/dev/null 2>&1
ok "setup does not add a model key when the node had none" \
  '[ "$(jq -r "has(\"model\")" "$np_claude/settings.json")" = false ]'

# --with-plugin takes the other install path; it drops the pin too without the fix.
pg_claude="$TMP/pg-claude-1235"
mkdir -p "$pg_claude"
printf '{"model":"claude-opus-5"}\n' > "$pg_claude/settings.json"
HOME="$TMP/pg-home-1235" CCC_CLAUDE_DIR="$pg_claude" CCC_HERMES_DIR="$TMP/pg-hermes-1235" \
  bash "$SETUP" --no-backup --with-plugin >/dev/null 2>&1
ok "plugin-mode install preserves the model pin too" \
  '[ "$(jq -r .model "$pg_claude/settings.json")" = "claude-opus-5" ]'

# Dry-run must not touch the file.
dm_claude="$TMP/dm-claude-1235"
mkdir -p "$dm_claude"
printf '{"model":"claude-opus-5"}\n' > "$dm_claude/settings.json"
dm_before="$(cat "$dm_claude/settings.json")"
out="$(HOME="$TMP/dm-home-1235" CCC_CLAUDE_DIR="$dm_claude" CCC_HERMES_DIR="$TMP/dm-hermes-1235" \
  bash "$SETUP" --dry-run 2>&1)"
ok "dry-run reports the preserved pin without writing" \
  '[ "$dm_before" = "$(cat "$dm_claude/settings.json")" ] && grep -q "preserve node-local model pin" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
