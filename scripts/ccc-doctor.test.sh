#!/usr/bin/env bash
# Tests for ccc doctor — diagnostic-only harness drift classification.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCTOR="$ROOT/scripts/ccc-doctor.sh"
pass=0; fail=0
# Some hardened runners mount /tmp noexec; the doctor must execute fixture CLIs.
TMP_BASE="${TMPDIR:-$(dirname "$ROOT")}"; mkdir -p "$TMP_BASE"
TMP="$(mktemp -d "$TMP_BASE/ccc-doctor-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

make_fixture() { # <name> <mode:standalone|plugin>
  local name="$1" mode="$2" dir
  dir="$TMP/$name"
  mkdir -p "$dir/repo/claude/hooks" "$dir/repo/claude/output-styles" "$dir/repo/bridge" \
           "$dir/home/.claude/hooks" "$dir/home/.claude/output-styles"
  cp "$ROOT/claude/settings.base.json" "$dir/repo/claude/settings.base.json"
  cp "$ROOT/claude/settings.local.template.json" "$dir/repo/claude/settings.local.template.json"
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
  # A configured node has a seeded node-local approvals file (from the template).
  cp "$ROOT/claude/settings.local.template.json" "$dir/home/.claude/settings.local.json"
  printf '%s\n' "$dir"
}

run_doctor() { # <fixture-dir> [args...]
  local dir="$1"; shift
  CCC_DOCTOR_REPO_DIR="$dir/repo" CCC_DOCTOR_CLAUDE_DIR="$dir/home/.claude" \
    bash "$DOCTOR" "$@"
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

clean="$(make_fixture clean standalone)"
out="$(run_doctor "$clean")"; rc=$?
ok "clean standalone exits 0" '[ "$rc" = 0 ]'
ok "clean output reports 정상" 'grep -q "정상" <<<"$out"'
ok "clean output reports standalone mode" 'grep -q "mode.*standalone" <<<"$out"'
ok "clean output reports harness version" 'grep -q "harness version" <<<"$out"'

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

# Keep every human-mode Codex failure probe paired with a JSON non-disclosure assertion.
claude_default="$(make_fixture claude-default standalone)"
out_default="$(run_doctor "$claude_default")"; rc_default=$?
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
out="$(FAKE_CODEX_MODE=authenticated CCC_AGENT_PROVIDER=codex CCC_CODEX_CLI_PATH="$codex_auth/bin/codex" run_doctor "$codex_auth")"; rc=$?
ok "authenticated Codex readiness succeeds" '[ "$rc" = 0 ] && grep -q "provider.*codex" <<<"$out" && grep -q "readiness.*ready" <<<"$out" && grep -q "Codex login.*authenticated" <<<"$out"'

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

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
