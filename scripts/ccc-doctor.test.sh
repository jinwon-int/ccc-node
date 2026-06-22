#!/usr/bin/env bash
# Tests for ccc doctor — diagnostic-only harness drift classification.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCTOR="$ROOT/scripts/ccc-doctor.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

make_fixture() { # <name> <mode:standalone|plugin>
  local name="$1" mode="$2" dir
  dir="$TMP/$name"
  mkdir -p "$dir/repo/claude/hooks" "$dir/repo/claude/output-styles" "$dir/repo/bridge" \
           "$dir/home/.claude/hooks" "$dir/home/.claude/output-styles"
  cp "$ROOT/claude/settings.base.json" "$dir/repo/claude/settings.base.json"
  cp "$ROOT/claude/settings.local.json" "$dir/repo/claude/settings.local.json"
  cp "$ROOT/claude/hooks/enforcement-overlay.json" "$dir/repo/claude/hooks/enforcement-overlay.json"
  cp "$ROOT/claude/hooks/hooks.json" "$dir/repo/claude/hooks/hooks.json"
  cp "$ROOT/claude/hooks/load-memory.sh" "$dir/repo/claude/hooks/load-memory.sh"
  cp "$ROOT/claude/hooks/load-tools.sh" "$dir/repo/claude/hooks/load-tools.sh"
  cp "$ROOT/claude/hooks/checkpoint.sh" "$dir/repo/claude/hooks/checkpoint.sh"
  cp "$ROOT/claude/hooks/statusline.sh" "$dir/repo/claude/hooks/statusline.sh"
  cp "$ROOT/claude/hooks/guard.sh" "$dir/repo/claude/hooks/guard.sh"
  cp "$ROOT/claude/hooks/audit.sh" "$dir/repo/claude/hooks/audit.sh"
  cp "$ROOT/claude/hooks/redact.sh" "$dir/repo/claude/hooks/redact.sh"
  cp "$ROOT/claude/hooks/notify.sh" "$dir/repo/claude/hooks/notify.sh"
  cp "$ROOT/claude/hooks/evidence-gate.sh" "$dir/repo/claude/hooks/evidence-gate.sh"
  cp "$ROOT/claude/output-styles/ccc-report.md" "$dir/repo/claude/output-styles/ccc-report.md"
  cp "$ROOT/claude/hooks/load-memory.sh" "$dir/home/.claude/hooks/load-memory.sh"
  cp "$ROOT/claude/hooks/load-tools.sh" "$dir/home/.claude/hooks/load-tools.sh"
  cp "$ROOT/claude/hooks/checkpoint.sh" "$dir/home/.claude/hooks/checkpoint.sh"
  cp "$ROOT/claude/hooks/statusline.sh" "$dir/home/.claude/hooks/statusline.sh"
  cp "$ROOT/claude/hooks/guard.sh" "$dir/home/.claude/hooks/guard.sh"
  cp "$ROOT/claude/hooks/audit.sh" "$dir/home/.claude/hooks/audit.sh"
  cp "$ROOT/claude/hooks/redact.sh" "$dir/home/.claude/hooks/redact.sh"
  cp "$ROOT/claude/hooks/notify.sh" "$dir/home/.claude/hooks/notify.sh"
  cp "$ROOT/claude/hooks/evidence-gate.sh" "$dir/home/.claude/hooks/evidence-gate.sh"
  cp "$ROOT/claude/output-styles/ccc-report.md" "$dir/home/.claude/output-styles/ccc-report.md"
  printf '#!/usr/bin/env bash\n[ "$1" = "--status" ] || [ "$3" = "--status" ] || true\necho bridge status ok\n' > "$dir/repo/bridge/start.sh"
  chmod +x "$dir/repo/bridge/start.sh"
  if [ "$mode" = standalone ]; then
    jq -s '.[0] as $b | .[1] as $o | $b | .hooks = ($b.hooks + $o.hooks)' \
      "$ROOT/claude/settings.base.json" "$ROOT/claude/hooks/enforcement-overlay.json" > "$dir/home/.claude/settings.json"
  else
    cp "$ROOT/claude/settings.base.json" "$dir/home/.claude/settings.json"
  fi
  cp "$ROOT/claude/settings.local.json" "$dir/home/.claude/settings.local.json"
  printf '%s\n' "$dir"
}

run_doctor() { # <fixture-dir> [args...]
  local dir="$1"; shift
  CCC_DOCTOR_REPO_DIR="$dir/repo" CCC_DOCTOR_CLAUDE_DIR="$dir/home/.claude" \
    bash "$DOCTOR" "$@"
}

clean="$(make_fixture clean standalone)"
out="$(run_doctor "$clean")"; rc=$?
ok "clean standalone exits 0" '[ "$rc" = 0 ]'
ok "clean output reports 정상" 'grep -q "정상" <<<"$out"'
ok "clean output reports standalone mode" 'grep -q "mode.*standalone" <<<"$out"'

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

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
