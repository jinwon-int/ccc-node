#!/usr/bin/env bash
# Tests for ccc doctor — diagnostic-only harness drift classification.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# ccc_hook_tree_files — the canonical deployable-hook walk shared with setup.sh.
# shellcheck source=scripts/lib/harness-paths.sh
. "$ROOT/scripts/lib/harness-paths.sh"
DOCTOR="$ROOT/scripts/ccc-doctor.sh"
pass=0; fail=0
# Hermetic default: provider-specific cases below opt in per invocation. A live
# bridge shell may export these and must not turn every generic fixture into a
# provider or extractor-readiness probe. The standalone doctor fixtures model
# harness drift only; extractor readiness has isolated Python verdict tests.
unset CCC_AGENT_PROVIDER CCC_CODEX_CLI_PATH CCC_CODEX_READINESS_TIMEOUT
export CCC_MEMORY_DISTILL_PROVIDER=off
# Some hardened runners mount /tmp noexec; the doctor must execute fixture CLIs.
TMP_BASE="${TMPDIR:-$(dirname "$ROOT")}"; mkdir -p "$TMP_BASE"
TMP="$(mktemp -d "$TMP_BASE/ccc-doctor-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# Apply setup.sh's canonical-path rewrite to a fixture's installed hook and
# output-style trees, through the same module setup.sh and doctor both use.
# Scoped to the trees doctor compares file-for-file; settings.json has its own
# JSON-semantic checks and is left as the templates produced it.
rewrite_installed() { # <fixture-dir>
  local dir="$1" f
  while IFS= read -r -d '' f; do
    python3 "$ROOT/scripts/lib/canonical_paths.py" "$f" \
      "/opt/ccc-node" "$dir/repo" "/root/.claude" "$dir/home/.claude"
  done < <(find "$dir/home/.claude/hooks" "$dir/home/.claude/output-styles" -type f -print0)
}

make_fixture() { # <name> <mode:standalone|plugin>
  local name="$1" mode="$2" dir
  dir="$TMP/$name"
  mkdir -p "$dir/repo/claude/hooks" "$dir/repo/claude/output-styles" "$dir/repo/bridge" \
           "$dir/home/.claude/hooks" "$dir/home/.claude/output-styles"
  cp "$ROOT/claude/settings.base.json" "$dir/repo/claude/settings.base.json"
  cp "$ROOT/claude/settings.local.template.json" "$dir/repo/claude/settings.local.template.json"
  cp "$ROOT/claude/hooks/enforcement-overlay.json" "$dir/repo/claude/hooks/enforcement-overlay.json"
  cp "$ROOT/claude/hooks/hooks.json" "$dir/repo/claude/hooks/hooks.json"
  cp "$ROOT/bridge/runtime_config_check.py" "$dir/repo/bridge/runtime_config_check.py"
  # Deploy the whole hook tree into both sides the way setup.sh does — via the
  # shared walk, not a hand-kept list. The list this replaced named the same 8
  # hooks doctor used to watch, so as hooks were added the fixture drifted with
  # it and a "healthy node" fixture silently stopped resembling a real install.
  while IFS= read -r _rel; do
    [ -n "$_rel" ] || continue
    mkdir -p "$dir/repo/claude/hooks/$(dirname "$_rel")" "$dir/home/.claude/hooks/$(dirname "$_rel")"
    cp "$ROOT/claude/hooks/$_rel" "$dir/repo/claude/hooks/$_rel"
    cp "$ROOT/claude/hooks/$_rel" "$dir/home/.claude/hooks/$_rel"
  done < <(ccc_hook_tree_files "$ROOT")
  cp "$ROOT/claude/output-styles/ccc-report.md" "$dir/repo/claude/output-styles/ccc-report.md"
  cp "$ROOT/claude/output-styles/ccc-report.md" "$dir/home/.claude/output-styles/ccc-report.md"
  # Mirror setup.sh's canonical-path rewrite on the INSTALLED side. Every fixture
  # is a non-canonical install (repo and harness dir live under $TMP), so without
  # this the fixture models an install no node actually has: templates carrying
  # /opt/ccc-node copied in verbatim. Doctor treats such a file as drifted, which
  # is exactly right — the phantom-drift fleet sweep (2026-07-30) came from doctor
  # NOT knowing about this rewrite, and the fixture must exercise the rewritten
  # comparison path rather than only the byte-exact one.
  rewrite_installed "$dir"
  printf '#!/usr/bin/env bash\n[ "$1" = "--status" ] || [ "$3" = "--status" ] || true\necho "🟢 Bot status: available"\n' > "$dir/repo/bridge/start.sh"
  chmod +x "$dir/repo/bridge/start.sh"
  if [ "$mode" = standalone ]; then
    jq -s '.[0] as $b | .[1] as $o | $b | .hooks = ($b.hooks + $o.hooks)' \
      "$ROOT/claude/settings.base.json" "$ROOT/claude/hooks/enforcement-overlay.json" > "$dir/home/.claude/settings.json"
  else
    cp "$ROOT/claude/settings.base.json" "$dir/home/.claude/settings.json"
  fi
  # A configured node has a seeded node-local approvals file (from the template).
  cp "$ROOT/claude/settings.local.template.json" "$dir/home/.claude/settings.local.json"
  printf '%s\n' "$dir"
}

run_doctor() { # <fixture-dir> [args...]
  local dir="$1"; shift
  (
    unset CLAUDE_PROCESS_TIMEOUT CCC_DELEGATED_TASK_STALL_SECONDS
    CCC_DOCTOR_REPO_DIR="$dir/repo" CCC_DOCTOR_CLAUDE_DIR="$dir/home/.claude" \
      CCC_DOCTOR_BRIDGE_PROJECT_ROOT="$dir/home" bash "$DOCTOR" "$@"
  )
}

make_fake_codex() { # <fixture-dir>
  local dir="$1"
  mkdir -p "$dir/bin"
  cat > "$dir/bin/codex" <<'EOF'
#!/usr/bin/env bash
case "${FAKE_CODEX_MODE:-authenticated}:$*" in
  timeout:*) sleep 2; exit 0 ;;
  authenticated:--version) printf 'codex-cli 1.2.3\n' ;;
  authenticated:app-server\ --help) printf 'Usage: codex app-server [OPTIONS]\n' ;;
  authenticated:login\ status) printf 'Logged in using ChatGPT\n' ;;
  unauthenticated:--version) printf 'codex-cli 1.2.3\n' ;;
  unauthenticated:app-server\ --help) printf 'Usage: codex app-server [OPTIONS]\n' ;;
  unauthenticated:login\ status)
    printf 'Not logged in: SENSITIVE_AUTH_MARKER account@example.invalid {"access_token":"SENSITIVE_TOKEN_MARKER"}\n' >&2
    exit 1
    ;;
  malformed:--version) printf 'codex-cli 1.2.3\n' ;;
  malformed:app-server\ --help) printf 'unexpected output\n' ;;
  *) exit 2 ;;
esac
EOF
  chmod +x "$dir/bin/codex"
}

# Provision the repo-shipped managed Codex skills into a fixture (#647) so the
# doctor's managed-skill diagnostics see a fully set-up Codex node.
# Install the commands/skills trees the way setup.sh does, then apply the same
# canonical-path rewrite. Doctor compares these file-for-file (#1037), so a
# fixture whose repo carries them but whose ~/.claude does not is genuinely
# drifted — same reasoning as the hook-tree walk above: the fixture must
# resemble a real install, not a partial one.
#
# The agents tree needs no counterpart: every repo agent is a2a-*, which is
# role-gated, and a fixture has neither CCC_A2A_ROLE nor an a2a-role marker, so
# doctor correctly expects none.
install_managed_trees() { # <fixture-dir>
  local dir="$1" skill_root skill f
  if [ -f "$dir/repo/claude/headless.sh" ]; then
    cp "$dir/repo/claude/headless.sh" "$dir/home/.claude/headless.sh"
    python3 "$ROOT/scripts/lib/canonical_paths.py" "$dir/home/.claude/headless.sh" \
      "/opt/ccc-node" "$dir/repo" "/root/.claude" "$dir/home/.claude"
  fi
  if [ -d "$dir/repo/claude/commands" ]; then
    mkdir -p "$dir/home/.claude/commands"
    cp "$dir/repo/claude/commands/"*.md "$dir/home/.claude/commands/" 2>/dev/null || true
  fi
  for skill_root in "$dir/repo/claude/skills" "$dir/repo/skills/shared"; do
    [ -d "$skill_root" ] || continue
    mkdir -p "$dir/home/.claude/skills"
    for skill in "$skill_root"/*/; do
      [ -d "$skill" ] || continue
      rm -rf "$dir/home/.claude/skills/$(basename "$skill")"
      cp -r "$skill" "$dir/home/.claude/skills/$(basename "$skill")"
    done
  done
  for f in "$dir/home/.claude/commands" "$dir/home/.claude/skills"; do
    [ -d "$f" ] || continue
    while IFS= read -r -d '' _target; do
      python3 "$ROOT/scripts/lib/canonical_paths.py" "$_target" \
        "/opt/ccc-node" "$dir/repo" "/root/.claude" "$dir/home/.claude"
    done < <(find "$f" -type f -print0)
  done
}

provision_codex_skills() { # <fixture-dir>
  local dir="$1"
  mkdir -p "$dir/repo/scripts"
  # CODEX_HOME must be owner-only 0700 (managed-skill safety contract).
  mkdir -p "$dir/home/.codex"; chmod 700 "$dir/home/.codex"
  # The compatibility catalog classifies the full claude/ + skills/ + codex/ asset roots,
  # so the provisioner needs them present to validate; overlay the real trees.
  cp -r "$ROOT/claude/." "$dir/repo/claude/"
  cp -r "$ROOT/skills" "$dir/repo/skills"
  cp -r "$ROOT/codex" "$dir/repo/codex"
  cp "$ROOT/scripts/ccc_codex_skills.py" "$dir/repo/scripts/ccc_codex_skills.py"
  python3 "$dir/repo/scripts/ccc_codex_skills.py" apply \
    --repo-root "$dir/repo" --codex-home "$dir/home/.codex" >/dev/null
  # The overlay above gave this fixture's repo the full claude/ + skills/ trees,
  # so the installed side must carry them too or doctor reports real drift.
  install_managed_trees "$dir"
}

# Hermetic empty CODEX_HOME default so Codex managed-skill diagnostics never
# read the runner's real ~/.codex. Provisioned fixtures override it.
export CODEX_HOME="$TMP/codex-empty-home"
mkdir -p "$TMP/codex-empty-home"; chmod 700 "$TMP/codex-empty-home"

clean="$(make_fixture clean standalone)"
out="$(run_doctor "$clean")"; rc=$?
ok "clean standalone exits 0" '[ "$rc" = 0 ]'
ok "clean output reports 정상" 'grep -q "정상" <<<"$out"'
ok "clean output reports standalone mode" 'grep -q "mode.*standalone" <<<"$out"'
ok "clean output reports harness version" 'grep -q "harness version" <<<"$out"'

# --- continuation state and opt-in visibility (#1113 follow-up) ---------------
ct="$(make_fixture continuation-state standalone)"
mkdir -p "$ct/home/.telegram_bot/continuation"
chmod 775 "$ct/home/.telegram_bot/continuation"
out="$(CCC_CONTINUATION_ENABLED=true run_doctor "$ct")"; rc=$?
ok "continuation rejects a group-writable state directory under umask 0002" \
  '[ "$rc" != 0 ] && grep -q "수동필요.*continuation state.*configured=enabled.*unsafe-mode-0775" <<<"$out"'

chmod 700 "$ct/home/.telegram_bot/continuation"
out="$(CCC_CONTINUATION_ENABLED=true run_doctor "$ct")"; rc=$?
ok "continuation reports an enabled owner-only state directory" \
  '[ "$rc" = 0 ] && grep -q "정상.*continuation state.*configured=enabled.*private-0700" <<<"$out"'

# 2026-08-03 dungae regression: a legacy one-hour process timeout combined
# with the new two-hour delegated-task default crash-looped the bridge, while
# doctor called the status merely "readable" and offered no repair boundary.
legacy_timeout="$(make_fixture legacy-timeout standalone)"
printf '%s\n' 'CLAUDE_PROCESS_TIMEOUT=3600' > "$legacy_timeout/repo/bridge/.env"
out="$(run_doctor "$legacy_timeout" 2>&1)"; rc=$?
ok "timeout invariant makes doctor fail closed" '[ "$rc" = 1 ]'
ok "timeout invariant is classified manual without values" \
  'grep -q "bridge runtime config.*delegated-task-stall-not-lower-than-process-timeout" <<<"$out" && grep -q "수동필요" <<<"$out"'
out="$(run_doctor "$legacy_timeout" --fix 2>&1)"; rc=$?
ok "doctor refuses to auto-edit operator bridge env" \
  '[ "$rc" = 1 ] && grep -q "manual items present" <<<"$out"'

# Phantom-drift regression (2026-07-30 fleet sweep). Five correctly installed
# nodes — yukson, gwakga, gongmyoung, gongyung, daegyo — reported 교정가능 for
# hooks/distill.sh and hooks/lifecycle-feed.sh purely because their checkout is
# not /opt/ccc-node, so the installed copies legitimately differ from the
# templates. Doctor must compare through setup.sh's rewrite, and must say that it
# did rather than applying it invisibly.
canon="$(make_fixture canonical standalone)"
out="$(run_doctor "$canon")"; rc=$?
ok "non-canonical install reports no drift for templates carrying canonical paths" \
  '[ "$rc" = 0 ] && ! grep -qE "교정가능.*(lifecycle-feed|distill)\.sh" <<<"$out"'
ok "report names the canonical path rewrite it compared through" \
  'grep -q "canonical path rewrite" <<<"$out" && grep -Fq "/opt/ccc-node -> $canon/repo" <<<"$out"'
ok "installed hook keeps this node's real checkout path" \
  'grep -Fq "$canon/repo/bridge/venv/bin/python" "$canon/home/.claude/hooks/lifecycle-feed.sh"'

# ...and the rewrite must not blind doctor to real drift in the same files.
real_drift="$(make_fixture real-drift standalone)"
printf '# unreviewed local edit\n' >> "$real_drift/home/.claude/hooks/lifecycle-feed.sh"
out="$(run_doctor "$real_drift")"; rc=$?
ok "real drift in a rewritten hook is still caught" \
  '[ "$rc" = 1 ] && grep -q "교정가능.*lifecycle-feed.sh" <<<"$out"'

plugin="$(make_fixture plugin plugin)"
out="$(run_doctor "$plugin")"; rc=$?
ok "plugin base-only settings exits 0" '[ "$rc" = 0 ]'
ok "plugin output reports plugin mode" 'grep -q "mode.*plugin" <<<"$out"'
ok "plugin mode does not require portable hooks in settings.json" '! grep -q "PreToolUse.*교정가능" <<<"$out"'

drift="$(make_fixture drift standalone)"
rm -f "$drift/home/.claude/hooks/statusline.sh"
out="$(run_doctor "$drift")"; rc=$?
ok "missing installed hook exits 1" '[ "$rc" = 1 ]'
ok "missing installed hook classified fixable" 'grep -q "교정가능.*statusline.sh" <<<"$out"'

repair="$(make_fixture repair standalone)"
jq '.outputStyle="plain" | .statusLine.command="bad-statusline" | del(.hooks.PostCompact)' \
  "$repair/home/.claude/settings.json" > "$repair/home/.claude/settings.json.tmp"
mv "$repair/home/.claude/settings.json.tmp" "$repair/home/.claude/settings.json"
before="$(find "$repair" -type f -printf '%P %s %T@\n' | sort)"
out="$(run_doctor "$repair" --fix 2>&1)"; rc=$?
after="$(find "$repair" -type f -printf '%P %s %T@\n' | sort)"
ok "--fix defaults to dry-run plan" '[ "$rc" = 1 ] && grep -q "dry-run" <<<"$out" && grep -q "would repair settings.json" <<<"$out"'
ok "--fix dry-run made no filesystem changes" '[ "$before" = "$after" ]'

backup_fail="$(make_fixture backup-fail standalone)"
jq '.outputStyle="plain"' "$backup_fail/home/.claude/settings.json" > "$backup_fail/home/.claude/settings.json.tmp"
mv "$backup_fail/home/.claude/settings.json.tmp" "$backup_fail/home/.claude/settings.json"
mkdir -p "$backup_fail/bin"
cat > "$backup_fail/bin/tar" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  -czf) printf 'not a tar archive\n' > "$2"; exit 0 ;;
  -tzf) exit 1 ;;
esac
exec /usr/bin/tar "$@"
EOF
chmod +x "$backup_fail/bin/tar"
settings_before="$(cat "$backup_fail/home/.claude/settings.json")"
out="$(PATH="$backup_fail/bin:$PATH" CCC_DOCTOR_REPO_DIR="$backup_fail/repo" CCC_DOCTOR_CLAUDE_DIR="$backup_fail/home/.claude" bash "$DOCTOR" --fix --apply 2>&1)"; rc=$?
settings_after="$(cat "$backup_fail/home/.claude/settings.json")"
ok "--fix --apply fails closed when backup tar validation fails" '[ "$rc" = 1 ] && grep -q "failed to create valid settings backup" <<<"$out" && [ "$settings_before" = "$settings_after" ]'

out="$(run_doctor "$repair" --fix --apply 2>&1)"; rc=$?
ok "--fix --apply repairs drift" '[ "$rc" = 0 ]'
ok "--fix --apply restores outputStyle" 'jq -e ".outputStyle == \"ccc-report\"" "$repair/home/.claude/settings.json" >/dev/null'
ok "--fix --apply restores statusLine" 'jq -e ".statusLine.command | contains(\"statusline.sh\")" "$repair/home/.claude/settings.json" >/dev/null'
ok "--fix --apply restores PostCompact hook" 'jq -e ".hooks.PostCompact" "$repair/home/.claude/settings.json" >/dev/null'
ok "--fix --apply creates backup tar" 'find "$repair/home/.claude/backups" -name "ccc-doctor-*.tar.gz" | grep -q .'
backup_count_before="$(find "$repair/home/.claude/backups" -name "ccc-doctor-*.tar.gz" | wc -l)"
out="$(run_doctor "$repair" --fix --apply 2>&1)"; rc=$?
backup_count_after="$(find "$repair/home/.claude/backups" -name "ccc-doctor-*.tar.gz" | wc -l)"
ok "--fix --apply is idempotent" '[ "$rc" = 0 ] && [ "$backup_count_before" = "$backup_count_after" ] && grep -q "no repairs needed" <<<"$out"'

before="$(find "$repair" -type f -printf '%P %s %T@\n' | sort)"
out="$(run_doctor "$repair" --rollback 2>&1)"; rc=$?
after="$(find "$repair" -type f -printf '%P %s %T@\n' | sort)"
ok "--rollback defaults to dry-run" '[ "$rc" = 1 ] && grep -q "dry-run" <<<"$out" && grep -q "would restore settings.json" <<<"$out"'
ok "--rollback dry-run made no filesystem changes" '[ "$before" = "$after" ]'

out="$(run_doctor "$repair" --rollback --apply 2>&1)"; rc=$?
ok "--rollback --apply restores previous settings" '[ "$rc" = 0 ]'
ok "--rollback --apply restores previous outputStyle drift" 'jq -e ".outputStyle == \"plain\"" "$repair/home/.claude/settings.json" >/dev/null'
ok "--rollback --apply restores previous statusLine drift" 'jq -e ".statusLine.command == \"bad-statusline\"" "$repair/home/.claude/settings.json" >/dev/null'
ok "--rollback --apply restores missing PostCompact" 'jq -e "has(\"hooks\") and (.hooks | has(\"PostCompact\") | not)" "$repair/home/.claude/settings.json" >/dev/null'
ok "--rollback --apply creates pre-rollback backup" 'find "$repair/home/.claude/backups" -name "ccc-doctor-pre-rollback-*.tar.gz" | grep -q .'

rollback_backup_fail="$(make_fixture rollback-backup-fail standalone)"
jq '.outputStyle="plain"' "$rollback_backup_fail/home/.claude/settings.json" > "$rollback_backup_fail/home/.claude/settings.json.tmp"
mv "$rollback_backup_fail/home/.claude/settings.json.tmp" "$rollback_backup_fail/home/.claude/settings.json"
out="$(run_doctor "$rollback_backup_fail" --fix --apply 2>&1)"; rc=$?
mkdir -p "$rollback_backup_fail/bin"
cat > "$rollback_backup_fail/bin/tar" <<'EOF'
#!/usr/bin/env bash
case "$1:$2" in
  -czf:*ccc-doctor-pre-rollback-*) exit 1 ;;
esac
exec /usr/bin/tar "$@"
EOF
chmod +x "$rollback_backup_fail/bin/tar"
settings_before="$(cat "$rollback_backup_fail/home/.claude/settings.json")"
out="$(PATH="$rollback_backup_fail/bin:$PATH" CCC_DOCTOR_REPO_DIR="$rollback_backup_fail/repo" CCC_DOCTOR_CLAUDE_DIR="$rollback_backup_fail/home/.claude" bash "$DOCTOR" --rollback --apply 2>&1)"; rc=$?
settings_after="$(cat "$rollback_backup_fail/home/.claude/settings.json")"
ok "--rollback --apply refuses to overwrite settings when its recovery backup fails" \
  '[ "$rc" = 1 ] && grep -q "failed to create valid pre-rollback settings backup" <<<"$out" && [ "$settings_before" = "$settings_after" ]'

nobackup="$(make_fixture nobackup standalone)"
out="$(run_doctor "$nobackup" --rollback --apply 2>&1)"; rc=$?
ok "--rollback --apply fails closed without backup" '[ "$rc" = 1 ] && grep -q "no rollback backup found" <<<"$out"'

files="$(make_fixture files standalone)"
rm -f "$files/home/.claude/hooks/statusline.sh"
printf 'drifted output style\n' > "$files/home/.claude/output-styles/ccc-report.md"
before="$(find "$files" -type f -printf '%P %s %T@\n' | sort)"
out="$(run_doctor "$files" --fix --scope=files 2>&1)"; rc=$?
after="$(find "$files" -type f -printf '%P %s %T@\n' | sort)"
ok "--fix --scope=files is dry-run" '[ "$rc" = 1 ] && grep -q "dry-run: would reinstall scoped files" <<<"$out" && [ "$before" = "$after" ]'

out="$(run_doctor "$files" --fix --apply --scope=files 2>&1)"; rc=$?
ok "--fix --apply --scope=files repairs allowlisted files" '[ "$rc" = 0 ] && grep -q "applied scoped file repair" <<<"$out"'
ok "file repair restores missing hook" 'cmp -s "$ROOT/claude/hooks/statusline.sh" "$files/home/.claude/hooks/statusline.sh"'
ok "file repair restores output style drift" 'cmp -s "$ROOT/claude/output-styles/ccc-report.md" "$files/home/.claude/output-styles/ccc-report.md"'
ok "file repair creates scoped backup tar" 'find "$files/home/.claude/backups" -name "ccc-doctor-files-*.tar.gz" | grep -q .'
backup_count_before="$(find "$files/home/.claude/backups" -name "ccc-doctor-files-*.tar.gz" | wc -l)"
out="$(run_doctor "$files" --fix --apply --scope=files 2>&1)"; rc=$?
backup_count_after="$(find "$files/home/.claude/backups" -name "ccc-doctor-files-*.tar.gz" | wc -l)"
ok "file repair is idempotent" '[ "$rc" = 0 ] && [ "$backup_count_before" = "$backup_count_after" ] && grep -q "no repairs needed" <<<"$out"'

file_backup_fail="$(make_fixture file-backup-fail standalone)"
printf 'drifted output style\n' > "$file_backup_fail/home/.claude/output-styles/ccc-report.md"
mkdir -p "$file_backup_fail/bin"
cat > "$file_backup_fail/bin/tar" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  -czf) printf 'not a tar archive\n' > "$2"; exit 0 ;;
  -tzf) exit 1 ;;
esac
exec /usr/bin/tar "$@"
EOF
chmod +x "$file_backup_fail/bin/tar"
file_before="$(cat "$file_backup_fail/home/.claude/output-styles/ccc-report.md")"
out="$(PATH="$file_backup_fail/bin:$PATH" CCC_DOCTOR_REPO_DIR="$file_backup_fail/repo" CCC_DOCTOR_CLAUDE_DIR="$file_backup_fail/home/.claude" bash "$DOCTOR" --fix --apply --scope=files 2>&1)"; rc=$?
file_after="$(cat "$file_backup_fail/home/.claude/output-styles/ccc-report.md")"
ok "file repair fails closed when its backup tar is invalid" \
  '[ "$rc" = 1 ] && grep -q "failed to create valid scoped file-repair backup" <<<"$out" && [ "$file_before" = "$file_after" ]'

# Repair must reinstall the way setup.sh installs. A plain copyfile restores the
# canonical template, pointing the hook at /opt/ccc-node — a path that does not
# exist on a /root/ccc-node or Termux node — so doctor's own printed action would
# break the node it just diagnosed.
rewrite_repair="$(make_fixture rewrite-repair standalone)"
printf 'clobbered\n' > "$rewrite_repair/home/.claude/hooks/lifecycle-feed.sh"
out="$(run_doctor "$rewrite_repair" --fix --apply --scope=files 2>&1)"; rc=$?
ok "file repair reinstalls with the canonical-path rewrite applied" \
  '[ "$rc" = 0 ] && grep -Fq "$rewrite_repair/repo/bridge/venv/bin/python" "$rewrite_repair/home/.claude/hooks/lifecycle-feed.sh"'
ok "repaired hook carries no unrewritten canonical checkout path" \
  '! grep -Fq "/opt/ccc-node/bridge/venv/bin/python" "$rewrite_repair/home/.claude/hooks/lifecycle-feed.sh"'
out="$(run_doctor "$rewrite_repair")"; rc=$?
ok "repaired rewritten hook is clean on the next run" \
  '[ "$rc" = 0 ] && grep -q "교정가능: 0" <<<"$out"'

symlink="$(make_fixture symlink standalone)"
rm -f "$symlink/home/.claude/hooks/statusline.sh"
ln -s /tmp/ccc-doctor-symlink-target "$symlink/home/.claude/hooks/statusline.sh"
before="$(find "$symlink" -type f,l -printf '%P %s %T@ %l\n' | sort)"
out="$(run_doctor "$symlink" --fix --apply --scope=files 2>&1)"; rc=$?
after="$(find "$symlink" -type f,l -printf '%P %s %T@ %l\n' | sort)"
ok "file repair refuses destination symlink" '[ "$rc" = 1 ] && grep -q "destination symlink refused" <<<"$out" && [ "$before" = "$after" ]'

plugin_repair="$(make_fixture plugin-repair plugin)"
rm -f "$plugin_repair/home/.claude/hooks/statusline.sh"
out="$(run_doctor "$plugin_repair" --fix --apply --scope=files 2>&1)"; rc=$?
ok "file repair refuses plugin mode" '[ "$rc" = 1 ] && grep -q "double-firing" <<<"$out"'

manual="$(make_fixture manual standalone)"
printf '{not-json}\n' > "$manual/home/.claude/settings.json"
before="$(find "$manual" -type f -printf '%P %s %T@\n' | sort)"
out="$(run_doctor "$manual" --fix --apply 2>&1)"; rc=$?
after="$(find "$manual" -type f -printf '%P %s %T@\n' | sort)"
ok "--fix --apply fails closed on manual settings" '[ "$rc" = 1 ] && grep -q "manual items present" <<<"$out" && [ "$before" = "$after" ]'

missing_settings="$(make_fixture missing-settings standalone)"
rm -f "$missing_settings/home/.claude/settings.json"
before="$(find "$missing_settings" -type f -printf '%P %s %T@\n' | sort)"
out="$(run_doctor "$missing_settings" --fix --apply 2>&1)"; rc=$?
after="$(find "$missing_settings" -type f -printf '%P %s %T@\n' | sort)"
ok "missing settings fails closed instead of claiming repairable" '[ "$rc" = 1 ] && grep -q "수동필요.*settings.json.*missing" <<<"$out" && grep -q "install mode cannot be inferred safely" <<<"$out" && [ "$before" = "$after" ]'

# Keep every human-mode Codex failure probe paired with a JSON non-disclosure assertion.
claude_default="$(make_fixture claude-default standalone)"
out_default="$(env -u CCC_AGENT_PROVIDER \
  CCC_DOCTOR_REPO_DIR="$claude_default/repo" \
  CCC_DOCTOR_CLAUDE_DIR="$claude_default/home/.claude" \
  bash "$DOCTOR")"; rc_default=$?
out_claude="$(CCC_AGENT_PROVIDER=claude run_doctor "$claude_default")"; rc_claude=$?
ok "explicit Claude provider preserves default behavior" '[ "$rc_default" = 0 ] && [ "$rc_claude" = 0 ] && [ "$out_default" = "$out_claude" ]'
ok "Claude human output reports provider without a Codex probe" 'grep -q "provider.*claude" <<<"$out_claude" && grep -q "readiness.*not-applicable" <<<"$out_claude"'

codex_absent="$(make_fixture codex-absent standalone)"
out="$(CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH=definitely-not-a-real-codex-command run_doctor "$codex_absent" 2>&1)"; rc=$?
ok "missing Codex binary fails closed" '[ "$rc" = 1 ] && grep -q "Codex executable.*not found" <<<"$out" && ! grep -q "definitely-not-a-real-codex-command" <<<"$out"'
json_fail="$(CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH=definitely-not-a-real-codex-command run_doctor "$codex_absent" --json 2>&1)"; json_rc=$?
ok "missing Codex binary JSON does not disclose configured command" '[ "$json_rc" = 1 ] && ! grep -q "definitely-not-a-real-codex-command" <<<"$json_fail"'

codex_nonexec="$(make_fixture codex-nonexec standalone)"
printf '#!/usr/bin/env bash\nexit 0\n' > "$codex_nonexec/codex-cli"
chmod 600 "$codex_nonexec/codex-cli"
out="$(CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_nonexec/codex-cli" run_doctor "$codex_nonexec" 2>&1)"; rc=$?
ok "non-executable Codex binary fails closed without path disclosure" '[ "$rc" = 1 ] && grep -q "Codex executable.*not executable" <<<"$out" && ! grep -Fq "$codex_nonexec/codex-cli" <<<"$out"'
json_fail="$(CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_nonexec/codex-cli" run_doctor "$codex_nonexec" --json 2>&1)"; json_rc=$?
ok "non-executable Codex binary JSON does not disclose path" '[ "$json_rc" = 1 ] && ! grep -Fq "$codex_nonexec/codex-cli" <<<"$json_fail"'

codex_timeout="$(make_fixture codex-timeout standalone)"
make_fake_codex "$codex_timeout"
out="$(FAKE_CODEX_MODE=timeout CCC_CODEX_READINESS_TIMEOUT=0.1 CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_timeout/bin/codex" run_doctor "$codex_timeout" 2>&1)"; rc=$?
ok "Codex probe timeout is bounded and fail-closed" '[ "$rc" = 1 ] && grep -q "Codex version probe.*timed out" <<<"$out"'

codex_auth="$(make_fixture codex-auth standalone)"
make_fake_codex "$codex_auth"
provision_codex_skills "$codex_auth"
export CODEX_HOME="$codex_auth/home/.codex"
out="$(FAKE_CODEX_MODE=authenticated CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_auth/bin/codex" run_doctor "$codex_auth")"; rc=$?
ok "authenticated Codex readiness succeeds" '[ "$rc" = 0 ] && grep -q "provider.*codex" <<<"$out" && grep -q "readiness.*ready" <<<"$out" && grep -q "Codex login.*authenticated" <<<"$out"'
ok "provisioned managed Codex skills report up to date" 'grep -q "정상.*managed Codex skills.*up to date" <<<"$out"'
out_unprov="$(FAKE_CODEX_MODE=authenticated CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_auth/bin/codex" CODEX_HOME="$TMP/codex-empty-home" run_doctor "$codex_auth")"; rc_unprov=$?
ok "unprovisioned managed Codex skills are correctable, not a blocker" '[ "$rc_unprov" = 1 ] && grep -q "교정가능.*managed Codex skills.*provision" <<<"$out_unprov" && grep -q "readiness.*ready" <<<"$out_unprov"'

out="$(FAKE_CODEX_MODE=unauthenticated CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_auth/bin/codex" run_doctor "$codex_auth" 2>&1)"; rc=$?
ok "unauthenticated Codex readiness fails closed" '[ "$rc" = 1 ] && grep -q "Codex login.*not authenticated" <<<"$out"'
ok "Codex diagnostics redact command output" '! grep -Eq "SENSITIVE_AUTH_MARKER|SENSITIVE_TOKEN_MARKER|account@example.invalid|access_token" <<<"$out"'
json_fail="$(FAKE_CODEX_MODE=unauthenticated CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_auth/bin/codex" run_doctor "$codex_auth" --json 2>&1)"; json_rc=$?
ok "unauthenticated Codex JSON redacts command output" '[ "$json_rc" = 1 ] && ! grep -Eq "SENSITIVE_AUTH_MARKER|SENSITIVE_TOKEN_MARKER|account@example.invalid|access_token" <<<"$json_fail"'

out="$(FAKE_CODEX_MODE=malformed CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_auth/bin/codex" run_doctor "$codex_auth" 2>&1)"; rc=$?
ok "malformed app-server probe fails closed" '[ "$rc" = 1 ] && grep -q "Codex app-server probe.*malformed output" <<<"$out" && ! grep -q "unexpected output" <<<"$out"'
json_fail="$(FAKE_CODEX_MODE=malformed CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_auth/bin/codex" run_doctor "$codex_auth" --json 2>&1)"; json_rc=$?
ok "malformed app-server JSON does not disclose raw output" '[ "$json_rc" = 1 ] && ! grep -q "unexpected output" <<<"$json_fail"'

json_out="$(FAKE_CODEX_MODE=authenticated CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_auth/bin/codex" run_doctor "$codex_auth" --json)"; rc=$?
ok "Codex JSON output is valid and carries additive readiness fields" '[ "$rc" = 0 ] && jq -e '\''(.provider == "codex") and (.readiness == "ready") and (.mode == "standalone") and (.counts["수동필요"] == 0) and ([.rows[].item] | index("Codex login") != null)'\'' <<<"$json_out" >/dev/null'
ok "Codex JSON output does not disclose executable path" '! grep -Fq "$codex_auth/bin/codex" <<<"$json_out"'

# #404: --json stdout must stay strictly machine-parseable. Capture stdout to a
# file (command substitution would strip trailing whitespace and hide the bug)
# and require json.load — not raw_decode recovery — to accept it every time.
strict_json_ok=1
for _ in 1 2 3 4 5; do
  FAKE_CODEX_MODE=authenticated CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_auth/bin/codex" \
    run_doctor "$codex_auth" --json >"$TMP/strict.json" 2>/dev/null
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$TMP/strict.json" || { strict_json_ok=0; break; }
done
ok "Codex --json stdout is strictly json.load-parseable across repeated runs (#404)" '[ "$strict_json_ok" = 1 ]'

# #404: prove the stdout guard captures the intermittent trailing writer. A
# subclassed diagnose leaks to both sys.stdout (stray print) and fd 1 (a
# descriptor-inheriting subprocess/codex grandchild); emit_json_report must keep
# stdout a single JSON document and divert the leaks to stderr.
cat > "$TMP/guard_leak.py" <<'PY'
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["DOCTOR_PY"]).resolve().parent))
import ccc_doctor as mod


class LeakyDoctor(mod.Doctor):
    def diagnose(self):
        print("STRAY_PRINT_LEAK")          # python-level stray stdout write
        sys.stdout.flush()
        os.write(1, b"RAW_FD1_LEAK")        # descriptor-level leak to the real fd 1
        self.add("정상", "synthetic", "ok", "none")


sys.exit(mod.emit_json_report(LeakyDoctor(Path("."), Path("."), "settings")))
PY
DOCTOR_PY="$ROOT/scripts/ccc_doctor.py" python3 "$TMP/guard_leak.py" >"$TMP/guard.out" 2>"$TMP/guard.err"
ok "stdout guard keeps --json stdout pure JSON despite stray fd1/print leaks (#404)" \
  'python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$TMP/guard.out" && ! grep -Eq "STRAY_PRINT_LEAK|RAW_FD1_LEAK" "$TMP/guard.out"'
ok "stdout guard diverts stray diagnostics to stderr (#404)" \
  'grep -q "STRAY_PRINT_LEAK" "$TMP/guard.err" && grep -q "RAW_FD1_LEAK" "$TMP/guard.err"'

# #404: os.write may consume fewer bytes than requested (partial write); the JSON
# document must not be truncated. Cap every os.write to 7 bytes and require the
# full multi-row report to still land on stdout.
cat > "$TMP/shortwrite.py" <<'PY'
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["DOCTOR_PY"]).resolve().parent))
import ccc_doctor as mod


class TinyDoctor(mod.Doctor):
    def harness_version(self):
        return "test-version"

    def diagnose(self):
        for i in range(20):  # encoded JSON far exceeds a single 7-byte write
            self.add("정상", "synthetic-%02d" % i, "ok", "none")


_real_write = os.write
os.write = lambda fd, data: _real_write(fd, bytes(data)[:7])
try:
    rc = mod.emit_json_report(TinyDoctor(Path("."), Path("."), "settings"))
finally:
    os.write = _real_write
sys.exit(rc)
PY
DOCTOR_PY="$ROOT/scripts/ccc_doctor.py" python3 "$TMP/shortwrite.py" >"$TMP/short.out" 2>"$TMP/short.err"
ok "short os.write does not truncate --json stdout (#404)" \
  'python3 -c "import json,sys; obj=json.load(open(sys.argv[1])); sys.exit(0 if len(obj[\"rows\"]) == 20 else 1)" "$TMP/short.out"'

# --- #771: boot ownership is node-type specific ---------------------------
# The check's real property is "whatever restarts the bridge points at the live
# checkout". systemd is only the Linux implementation of that; Termux nodes
# implement it with ~/.termux/boot. Asking a Termux node for a systemd unit
# reported a correctly-booting node as "nothing restarts the bridge on reboot".
cat > "$TMP/nodetype.py" <<'PY_EOF'
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["DOCTOR_PY"]).resolve().parent))
import ccc_doctor as mod

home = Path(os.environ["FAKE_HOME"])
boot = home / ".termux/boot"
boot.mkdir(parents=True, exist_ok=True)
live = f"{home}/ccc-node"


def doctor(running, script_body=None):
    for stale in boot.glob("*.sh"):
        stale.unlink()
    if script_body is not None:
        (boot / "start-telegram-bridge.sh").write_text(script_body)
    d = mod.Doctor(Path(live), home / ".claude", "settings")
    d.check_bridge_boot_path_termux(running)
    return [r for r in d.rows if r.item == "bridge boot path"][0]


agree = doctor(live, f'setsid -f bash "$HOME/ccc-node/bridge/start.sh" --path "$HOME" -d\n')
drift = doctor(live, 'setsid -f bash "/opt/ccc-node/bridge/start.sh" --path "/root" -d\n')
none_ = doctor(live, None)

results = {
    "agree": agree.klass,
    "drift": drift.klass,
    "none": none_.klass,
    "drift_detail": drift.status,
    "none_advice": none_.action,
    "path_arg": mod.Doctor.bridge_home_of(
        "/x/ccc-node/bridge/venv/bin/python -m telegram_bot --path /data/data/com.termux/files/home"
    ),
}
import json
print(json.dumps(results, ensure_ascii=False))
PY_EOF
FAKE_HOME="$TMP/nodehome"; mkdir -p "$FAKE_HOME"
nout="$(DOCTOR_PY="$ROOT/scripts/ccc_doctor.py" FAKE_HOME="$FAKE_HOME" HOME="$FAKE_HOME" python3 "$TMP/nodetype.py" 2>"$TMP/nodetype.err")"
ok "termux boot script agreeing with runtime is 정상" '[ -n "$nout" ] && jq -e ".agree == \"정상\"" <<<"$nout" >/dev/null'
ok "termux boot script pointing elsewhere is 수동필요 (same severity as a stale unit)" 'jq -e ".drift == \"수동필요\"" <<<"$nout" >/dev/null'
ok "termux drift names both checkouts" 'jq -e ".drift_detail | contains(\"/opt/ccc-node\")" <<<"$nout" >/dev/null'
ok "no boot script is 경고 with Termux advice, not systemd advice" 'jq -e ".none == \"경고\" and (.none_advice | contains(\".termux/boot\")) and (.none_advice | contains(\"systemd\") | not)" <<<"$nout" >/dev/null'
ok "bridge --path is read from the live process" 'jq -e ".path_arg == \"/data/data/com.termux/files/home\"" <<<"$nout" >/dev/null'

# harness version: a version script that cannot exec must fall through to git
# describe, not report "unknown". On Termux the `#!/usr/bin/env bash` shebang
# is unresolvable (no /usr/bin/env), which is exactly this path.
cat > "$TMP/version.py" <<'PY_EOF'
import os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["DOCTOR_PY"]).resolve().parent))
import ccc_doctor as mod

repo = Path(os.environ["VER_REPO"])
(repo / "scripts").mkdir(parents=True, exist_ok=True)
script = repo / "scripts/ccc-version.sh"
script.write_text("#!/nonexistent/interpreter\necho never\n")
script.chmod(0o755)
subprocess.run(["git", "init", "-q", str(repo)], check=True)
subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
print(mod.Doctor(repo, repo / ".claude", "settings").harness_version())
PY_EOF
vout="$(DOCTOR_PY="$ROOT/scripts/ccc_doctor.py" VER_REPO="$TMP/verrepo" python3 "$TMP/version.py" 2>"$TMP/version.err")"
ok "unrunnable version script falls through to git describe, not 'unknown'" '[ -n "$vout" ] && [ "$vout" != "unknown" ]'

# --- #775: repo scripts are invoked through bash, never bare-exec'd -------
# Class defect, not a one-off: every probe that exec'd a repo `.sh` directly
# failed on Termux, where `#!/usr/bin/env bash` has no /usr/bin/env to resolve.
# The failure is silent — the except arm degrades the probe to "unavailable" or
# "unknown" — so it reads as a real finding rather than a broken probe. This
# test drives each probe against a script whose shebang cannot be resolved
# ANYWHERE, so it fails the same way on Linux CI as it did on the phone.
cat > "$TMP/shebang.py" <<'PY_EOF'
import json, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["DOCTOR_PY"]).resolve().parent))
import ccc_doctor as mod

repo = Path(os.environ["SHEBANG_REPO"])
claude = repo / ".claude"
(repo / "scripts").mkdir(parents=True, exist_ok=True)
claude.mkdir(parents=True, exist_ok=True)

BAD = "#!/nonexistent/interpreter\n"

mem = repo / "scripts/ccc-memory-check.sh"
mem.write_text(BAD + """printf '%s' '{"wiki":{"status":"ok"},"honcho":{"status":"stale"},"local_index":{"exists":true}}'\n""")
mem.chmod(0o755)

ver = repo / "scripts/ccc-version.sh"
ver.write_text(BAD + "echo v9.9.9-from-script\n")
ver.chmod(0o755)

subprocess.run(["git", "init", "-q", str(repo)], check=True)
subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True)

d = mod.Doctor(repo, claude, "settings")
d.check_memory_cache()
row = [r for r in d.rows if r.item == "memory cache"][0]
print(json.dumps({
    "status": row.status,
    "klass": row.klass,
    "version": mod.Doctor(repo, claude, "settings").harness_version(),
}, ensure_ascii=False))
PY_EOF
SHEBANG_REPO="$TMP/shebangrepo"
sout="$(DOCTOR_PY="$ROOT/scripts/ccc_doctor.py" SHEBANG_REPO="$SHEBANG_REPO" python3 "$TMP/shebang.py" 2>"$TMP/shebang.err")"
ok "memory probe survives an unresolvable shebang (not 'diagnostic unavailable')" \
  '[ -n "$sout" ] && jq -e ".status != \"diagnostic unavailable\"" <<<"$sout" >/dev/null'
ok "memory probe reports the real cache state through bash" \
  'jq -e ".status | contains(\"honcho=stale\") and contains(\"wiki=ok\")" <<<"$sout" >/dev/null'
ok "a stale honcho cache is 경고, not a broken-probe 경고" 'jq -e ".klass == \"경고\"" <<<"$sout" >/dev/null'
ok "version probe survives the same unresolvable shebang" 'jq -e ".version != \"unknown\"" <<<"$sout" >/dev/null'

# #827: doctor must not call an enabled nunchi/Palace stack healthy merely
# because the legacy Wiki/Honcho cache is healthy.
cat > "$TMP/nunchi-memory.py" <<'PY_EOF'
import json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["DOCTOR_PY"]).resolve().parent))
import ccc_doctor as mod

repo = Path(os.environ["NUNCHI_REPO"])
claude = repo / ".claude"
(repo / "scripts").mkdir(parents=True, exist_ok=True)
claude.mkdir(parents=True, exist_ok=True)
script = repo / "scripts/ccc-memory-check.sh"
script.touch()
script.chmod(0o755)

def classify(nunchi, mempalace, audiences=None):
    nunchi_payload = {"status": nunchi}
    if audiences is not None:
        nunchi_payload["audience_scoped"] = audiences
    payload = {
        "wiki": {"status": "ok"}, "honcho": {"status": "ok"},
        "local_index": {"exists": True},
        "nunchi": nunchi_payload, "mempalace": {"status": mempalace},
    }
    script.write_text("#!/usr/bin/env bash\nprintf '%s' '" + json.dumps(payload) + "'\n")
    d = mod.Doctor(repo, claude, "settings")
    d.check_memory_cache()
    row = d.rows[0]
    return {"klass": row.klass, "status": row.status}

print(json.dumps({
    "healthy": classify("ok", "ok"),
    "degraded": classify("degraded", "ok"),
    "scoped": classify("ok", "ok", {"enabled": True, "root_status": "ok", "invalid_entries": 0, "scope_count": 4, "private_count": 3, "shared_count": 1}),
    "unsafe": classify("ok", "ok", {"enabled": True, "root_status": "unsafe", "invalid_entries": 1, "scope_count": 0, "private_count": 0, "shared_count": 0}),
}, ensure_ascii=False))
PY_EOF
nout="$(DOCTOR_PY="$ROOT/scripts/ccc_doctor.py" NUNCHI_REPO="$TMP/nunchi-repo" python3 "$TMP/nunchi-memory.py")"
ok "doctor accepts a healthy new memory stack" 'jq -e '\''.healthy.klass == "정상"'\'' <<<"$nout" >/dev/null'
ok "doctor warns when nunchi is degraded despite healthy Honcho" \
  'jq -e '\''.degraded.klass == "경고" and (.degraded.status | contains("nunchi=degraded"))'\'' <<<"$nout" >/dev/null'
ok "doctor reports body-free scoped audience counts and accepts a safe root" \
  'jq -e '\''.scoped.klass == "정상" and (.scoped.status | contains("audiences=4/3/1 root=ok invalid=0"))'\'' <<<"$nout" >/dev/null'
ok "doctor warns for an unsafe scoped audience root" \
  'jq -e '\''.unsafe.klass == "경고" and (.unsafe.status | contains("root=unsafe invalid=1"))'\'' <<<"$nout" >/dev/null'

# #920: doctor surfaces the nunchi MemPalace collection lane (configured provider
# vs runtime CCC_AGENT_PROVIDER DRIFT, source kind/path, MemPalace binary/version,
# last body-free collection state) — non-fatal (경고). Parsing mirrors
# install-nunchi.sh status. Driven through a Python helper that constructs the
# Doctor and calls check_nunchi_collection() directly (same pattern as the
# memory-cache cases above) against fake crontab/mempalace/status fixtures.
nbin="$TMP/nunchi-coll-fakebin"; mkdir -p "$nbin"
cat > "$nbin/crontab" <<'SH'
#!/usr/bin/env bash
[ "${1:-}" = "-l" ] && { [ -f "${CCC_TEST_CRONTAB_STORE:-}" ] && cat "${CCC_TEST_CRONTAB_STORE:-}"; exit 0; }
exit 0
SH
chmod +x "$nbin/crontab"
cat > "$nbin/mempalace" <<'SH'
#!/usr/bin/env bash
[ "${1:-}" = "--version" ] && { printf 'MemPalace 9.9-test\n'; exit 0; }
exit 0
SH
chmod +x "$nbin/mempalace"
nrepo="$TMP/nunchi-coll-repo"
cat > "$TMP/nunchi-collection.py" <<'PY_EOF'
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["DOCTOR_PY"]).resolve().parent))
import ccc_doctor as mod
repo = Path(os.environ["NUNCHI_COLL_REPO"])
(repo / "scripts").mkdir(parents=True, exist_ok=True)
(repo / ".claude").mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = os.environ["ND_HOME"]
os.environ["PATH"] = os.environ["ND_PATH"]
os.environ["CCC_AGENT_PROVIDER"] = os.environ["ND_PROVIDER"]
os.environ["CCC_CRONTAB_CMD"] = os.environ["ND_CRONTAB"]
os.environ["CCC_TEST_CRONTAB_STORE"] = os.environ["ND_CRON_STORE"]
store = Path(os.environ["ND_CRON_STORE"]); store.parent.mkdir(parents=True, exist_ok=True)
store.write_text(os.environ.get("ND_CRON", ""))
sfile = Path(os.environ["ND_STATUS"]); sfile.parent.mkdir(parents=True, exist_ok=True)
sj = os.environ.get("ND_STATUS_JSON", "")
if sj:
    sfile.write_text(sj)
elif sfile.exists():
    sfile.unlink()
os.environ["CCC_NUNCHI_MEMPALACE_STATUS"] = str(sfile)
os.environ["NUNCHI_HOME"] = str(sfile.parent)
mp = os.environ.get("ND_MP", "")
if mp:
    os.environ["CCC_NUNCHI_MEMPALACE_CLI"] = mp
else:
    os.environ.pop("CCC_NUNCHI_MEMPALACE_CLI", None)
d = mod.Doctor(repo, repo / ".claude", "settings")
d.check_nunchi_collection()
r = d.rows[0] if d.rows else None
print(json.dumps({"klass": r.klass, "status": r.status} if r else {"klass": "none", "status": "none"}, ensure_ascii=False))
PY_EOF
run_nc() {  # <provider> <cron-text> <status-json> <mp-path|empty> [path]
  ND_PROVIDER="$1" ND_CRON="$2" ND_STATUS_JSON="$3" ND_MP="$4" \
  ND_HOME="$TMP/nc-home" ND_PATH="${5:-$nbin:/usr/bin:/bin}" \
  ND_CRONTAB="$nbin/crontab" ND_CRON_STORE="$TMP/nc-cron" ND_STATUS="$TMP/nc-status.json" \
  NUNCHI_COLL_REPO="$nrepo" DOCTOR_PY="$ROOT/scripts/ccc_doctor.py" \
  python3 "$TMP/nunchi-collection.py" 2>/dev/null
}
codex_cron='*/10 * * * * bash /h/.claude/hooks/nunchi/codex-feed.sh >> /log 2>&1 # nunchi:#816
17 * * * * bash /h/.claude/hooks/nunchi/mempalace-refresh.sh codex /h/.codex/sessions >> /log 2>&1 # nunchi:#816'
claude_cron='*/10 * * * * bash /h/.claude/hooks/nunchi/ingest-cron.sh >> /log 2>&1 # nunchi:#816
17 * * * * bash /h/.claude/hooks/nunchi/mempalace-refresh.sh claude /h/.claude/projects >> /log 2>&1 # nunchi:#816'
piri_cron='*/10 * * * * bash /h/.claude/hooks/nunchi/piri-feed.sh >> /log 2>&1 # nunchi:#816
17 * * * * bash /h/.claude/hooks/nunchi/mempalace-refresh.sh piri /h/.piri/agent/sessions >> /log 2>&1 # nunchi:#816'
ok_json='{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":1,"finished_at":2}'
deg_json='{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"degraded","exit_code":0,"started_at":1,"finished_at":2}'

nc="$(run_nc claude "" "" "")"
ok "no managed cron reports nunchi collection not enabled (정상, body-free)" \
  'jq -e ".klass == \"정상\" and (.status | contains(\"not enabled\"))" <<<"$nc" >/dev/null'
nc="$(run_nc codex "$codex_cron" "$ok_json" "$nbin/mempalace")"
ok "codex lane with matching provider and present MemPalace is 정상" \
  'jq -e ".klass == \"정상\" and (.status | contains(\"configured=codex\") and contains(\"runtime=codex\") and contains(\"match=ok\") and contains(\"source=mine\") and contains(\"/h/.codex/sessions\") and contains(\"version=MemPalace 9.9-test\"))" <<<"$nc" >/dev/null'
nc="$(run_nc claude "$claude_cron" "$ok_json" "$nbin/mempalace")"
ok "claude lane uses sweep source and stays 정상" \
  'jq -e ".klass == \"정상\" and (.status | contains(\"match=ok\") and contains(\"source=sweep\") and contains(\"/h/.claude/projects\"))" <<<"$nc" >/dev/null'
nc="$(run_nc piri "$piri_cron" "$ok_json" "$nbin/mempalace")"
ok "piri lane uses the conversation miner (mine) source and stays 정상" \
  'jq -e ".klass == \"정상\" and (.status | contains(\"configured=piri\") and contains(\"runtime=piri\") and contains(\"match=ok\") and contains(\"source=mine\") and contains(\"/h/.piri/agent/sessions\"))" <<<"$nc" >/dev/null'
nc="$(run_nc claude "$codex_cron" "$ok_json" "$nbin/mempalace")"
ok "provider drift (configured codex, runtime claude) is a non-fatal 경고" \
  'jq -e ".klass == \"경고\" and (.status | contains(\"match=DRIFT\"))" <<<"$nc" >/dev/null'
nc="$(run_nc codex "$codex_cron" "$deg_json" "" "/usr/bin:/bin")"
ok "missing MemPalace CLI is a 경고 (peer-facts-only degrade), body-free" \
  'jq -e ".klass == \"경고\" and (.status | contains(\"mempalace=missing\"))" <<<"$nc" >/dev/null'

# #1081: doctor surfaces installer-managed cron entries frozen at older code.
# One row per known marker (absent = opt-in 정상; gen match = 정상; unstamped
# or mismatched gen = non-fatal 경고) plus unmanaged-marker classification.
# Driven through a Python helper (same pattern as nunchi-collection above)
# against a minimal fixture repo carrying the real installers + gen-stamp lib.
cdrepo="$TMP/cron-drift-repo"
mkdir -p "$cdrepo/scripts/lib" "$cdrepo/.claude"
cp "$ROOT/scripts/lib/installer-gen-stamp.sh" "$cdrepo/scripts/lib/"
for s in install-memory-refresh-cron install-pr-status-poll-cron install-skill-autosave-cron install-nunchi; do
  cp "$ROOT/scripts/$s.sh" "$cdrepo/scripts/"
done
# shellcheck source=/dev/null
. "$ROOT/scripts/lib/installer-gen-stamp.sh"
gen_mr="$(ccc_installer_gen_stamp "$cdrepo/scripts/install-memory-refresh-cron.sh")"
gen_pp="$(ccc_installer_gen_stamp "$cdrepo/scripts/install-pr-status-poll-cron.sh")"
gen_sa="$(ccc_installer_gen_stamp "$cdrepo/scripts/install-skill-autosave-cron.sh")"
gen_nu="$(ccc_installer_gen_stamp "$cdrepo/scripts/install-nunchi.sh")"
cat > "$TMP/cron-drift.py" <<'PY_EOF'
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["DOCTOR_PY"]).resolve().parent))
import ccc_doctor as mod
repo = Path(os.environ["CD_REPO"])
os.environ["CCC_CRONTAB_CMD"] = os.environ["CD_CRONTAB"]
os.environ["CCC_TEST_CRONTAB_STORE"] = os.environ["CD_CRON_STORE"]
store = Path(os.environ["CD_CRON_STORE"]); store.parent.mkdir(parents=True, exist_ok=True)
store.write_text(os.environ.get("CD_CRON", ""))
d = mod.Doctor(repo, repo / ".claude", "settings")
d.check_cron_drift()
print(json.dumps({
    "exit": d.report_exit_code(),
    "rows": [{"item": r.item, "klass": r.klass, "status": r.status, "action": r.action} for r in d.rows],
}, ensure_ascii=False))
PY_EOF
run_cd() {  # <cron-text> [repo-dir]
  CD_CRON="$1" CD_REPO="${2:-$cdrepo}" \
  CD_CRONTAB="$nbin/crontab" CD_CRON_STORE="$TMP/cd-cron" \
  DOCTOR_PY="$ROOT/scripts/ccc_doctor.py" \
  python3 "$TMP/cron-drift.py" 2>/dev/null
}

cd_out="$(run_cd "")"
ok "empty crontab: four opt-in rows, all 정상" \
  'jq -e "[.rows[] | select(.klass != \"정상\")] | length == 0" <<<"$cd_out" >/dev/null && [ "$(jq ".rows | length" <<<"$cd_out")" = 4 ]'
ok "empty crontab: rows are the four known markers" \
  'jq -e "[.rows[].item] == [\"cron gen memory-refresh\", \"cron gen pr-status-poll\", \"cron gen skill-autosave\", \"cron gen nunchi\"]" <<<"$cd_out" >/dev/null'

full_cron="*/30 * * * * bash -lc 'x' >> /l 2>&1  # ccc-node:memory-refresh gen=$gen_mr
*/17 * * * * bash -lc 'x' >> /l 2>&1  # ccc-node:pr-status-poll gen=$gen_pp
# ccc-node:autosave-schedule:begin
CRON_TZ=Etc/UTC
45 20 * * * bash -lc 'x' >> /l 2>&1  # ccc-node:skill-autosave gen=$gen_sa
# ccc-node:autosave-schedule:end
*/10 * * * * bash /h/ingest-cron.sh >> /l 2>&1 # nunchi:#816 gen=$gen_nu
17 * * * * bash /h/mempalace-refresh.sh codex /s >> /l 2>&1 # nunchi:#816 gen=$gen_nu
7 8 * * 1 bash /h/bench.sh >> /l 2>&1 # nunchi:#816 gen=$gen_nu"
cd_out="$(run_cd "$full_cron")"
ok "all-stamped current entries: four 정상 rows" \
  'jq -e "[.rows[] | select(.klass != \"정상\")] | length == 0" <<<"$cd_out" >/dev/null'
ok "nunchi row reports all three lines current" \
  'jq -e ".rows[] | select(.item == \"cron gen nunchi\") | .status | contains(\"lines=3\")" <<<"$cd_out" >/dev/null'
ok "BEGIN/END block markers are not misread as unmanaged" \
  '! jq -e ".rows[] | select(.item == \"cron unmanaged markers\")" <<<"$cd_out" >/dev/null'

cd_out="$(run_cd "*/30 * * * * bash -lc 'x' >> /l 2>&1  # ccc-node:memory-refresh gen=h_000000000000")"
ok "stale gen stamp is a non-fatal 경고 naming both stamps" \
  'jq -e ".rows[] | select(.item == \"cron gen memory-refresh\" and .klass == \"경고\") | .status | contains(\"gen=h_000000000000 != current gen=$gen_mr\")" <<<"$cd_out" >/dev/null'
ok "stale gen action points at the rendering installer" \
  'jq -e ".rows[] | select(.item == \"cron gen memory-refresh\") | .action | contains(\"install-memory-refresh-cron.sh --apply\")" <<<"$cd_out" >/dev/null'
ok "drift 경고 never flips the doctor exit code" \
  'jq -e ".exit == 0" <<<"$cd_out" >/dev/null'

cd_out="$(run_cd "*/10 * * * * bash /h/piri-feed.sh >> /l 2>&1 # nunchi:#816
17 * * * * bash /h/mempalace-refresh.sh piri /s >> /l 2>&1 # nunchi:#816 gen=$gen_nu")"
ok "mixed stamped+unstamped nunchi lines are 경고 unstamped" \
  'jq -e ".rows[] | select(.item == \"cron gen nunchi\" and .klass == \"경고\") | .status | contains(\"unstamped pre-#1081\")" <<<"$cd_out" >/dev/null'

cd_out="$(run_cd "45 4,5 * * * bash -lc 'x' >> /l 2>&1  # ccc-node:self-update
30 4 * * * /x/ccc-live-backups-rotate.sh >/dev/null 2>&1  # ccc-node:live-backups-rotate")"
ok "documented hand-installed markers are 정상 with labels" \
  'jq -e ".rows[] | select(.item == \"cron unmanaged markers\" and .klass == \"정상\") | .status | contains(\"ccc-node:self-update\") and contains(\"live-backups-rotate\")" <<<"$cd_out" >/dev/null'
cd_out="$(run_cd "*/5 * * * * bash /x/ghost.sh  # ccc-node:ghost-lane")"
ok "unknown unmanaged marker is a 경고 with the label" \
  'jq -e ".rows[] | select(.item == \"cron unmanaged markers\" and .klass == \"경고\") | .status | contains(\"ccc-node:ghost-lane\")" <<<"$cd_out" >/dev/null'

nolbrepo="$TMP/cron-drift-nolib"
mkdir -p "$nolbrepo/scripts" "$nolbrepo/.claude"
cp "$ROOT/scripts/install-memory-refresh-cron.sh" "$nolbrepo/scripts/"
cd_out="$(run_cd "*/30 * * * * bash -lc 'x' >> /l 2>&1  # ccc-node:memory-refresh gen=$gen_mr" "$nolbrepo")"
ok "incomplete checkout (lib missing) is a 경고, not a silent pass" \
  'jq -e ".rows[] | select(.item == \"cron gen memory-refresh\" and .klass == \"경고\") | .status | contains(\"cannot recompute current stamp\")" <<<"$cd_out" >/dev/null'


# Static backstop: a new probe added later must not reintroduce bare-exec.
ok "no repo script is subprocess-exec'd without an explicit interpreter" \
  '! grep -nE "subprocess\.(run|check_output|Popen)\(\[str\(" "$ROOT/scripts/ccc_doctor.py"'

# --- managed skills/agents/commands drift (#1037) ----------------------------
# setup.sh installs four trees; doctor watched two, so a stale skill, agent or
# slash command was invisible to /doctor AND to self-update's check.sh, which
# delegates to doctor. These fixtures pin the extended watch and, just as
# importantly, the cases that must NOT fire.
mt="$(make_fixture managed-trees standalone)"
mkdir -p "$mt/repo/claude/commands" "$mt/repo/claude/skills/demo" "$mt/repo/skills/shared/shared-demo" \
         "$mt/repo/claude/agents"
printf '# demo command\n' > "$mt/repo/claude/commands/demo.md"
printf '# demo skill\n'   > "$mt/repo/claude/skills/demo/SKILL.md"
printf '# shared skill\n' > "$mt/repo/skills/shared/shared-demo/SKILL.md"
install_managed_trees "$mt"

out="$(run_doctor "$mt")"; rc=$?
ok "fully installed managed trees are 정상" \
  '[ "$rc" = 0 ] && grep -q "정상.*commands/demo.md" <<<"$out" && grep -q "정상.*skills/demo/SKILL.md" <<<"$out"'
ok "the second skill source root (skills/shared) is watched too" \
  'grep -q "skills/shared-demo/SKILL.md" <<<"$out"'

# A node-local skill setup.sh never installs must never be reported.
mkdir -p "$mt/home/.claude/skills/node-local-only"
printf '# local\n' > "$mt/home/.claude/skills/node-local-only/SKILL.md"
out="$(run_doctor "$mt")"
ok "node-local skills are not reported" '! grep -q "node-local-only" <<<"$out"'

printf '# demo skill drifted\n' > "$mt/home/.claude/skills/demo/SKILL.md"
out="$(run_doctor "$mt")"; rc=$?
ok "a drifted managed skill is caught" \
  '[ "$rc" != 0 ] && grep -q "교정가능.*skills/demo/SKILL.md.*drifted" <<<"$out"'
# Repair must stay with setup.sh: doctor's --fix path refuses these paths, so
# pointing at it would hand the operator a command that cannot work.
ok "drifted managed files are told to run setup.sh, not doctor --fix" \
  'grep -q "skills/demo/SKILL.md" <<<"$out" && grep -qE "skills/demo/SKILL.md.*setup\.sh" <<<"$out"'
ok "doctor --fix does not claim it can repair managed trees" \
  '! run_doctor "$mt" --fix --scope=files | grep -q "skills/demo"'

rm -f "$mt/home/.claude/commands/demo.md"
out="$(run_doctor "$mt")"
ok "a missing managed command is caught" \
  'grep -q "교정가능.*commands/demo.md.*missing" <<<"$out"'

# The a2a-* roster is a worker-role capability; a broker/unconfigured node
# deliberately has none, and reporting them missing there is a false alarm.
printf '# worker agent\n' > "$mt/repo/claude/agents/a2a-demo.md"
printf '# plain agent\n'  > "$mt/repo/claude/agents/plain-demo.md"
out="$(run_doctor "$mt")"
ok "role-gated a2a agents are not expected on a non-worker node" \
  '! grep -q "a2a-demo.md" <<<"$out"'
ok "node-agnostic agents are still watched" \
  'grep -q "agents/plain-demo.md.*missing" <<<"$out"'

mkdir -p "$mt/home/.claude/agents"
printf '%s\n' worker > "$mt/home/.claude/a2a-role"
out="$(run_doctor "$mt")"
ok "the persisted worker marker opts the roster back in" \
  'grep -q "agents/a2a-demo.md.*missing" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
