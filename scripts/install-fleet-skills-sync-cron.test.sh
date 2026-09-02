#!/usr/bin/env bash
# Hermetic tests for install-fleet-skills-sync-cron.sh.
# Uses a stub crontab (CCC_CRONTAB_CMD) backed by a temp file, so no real
# crontab is touched and the suite is platform-independent (Linux + Termux).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$ROOT/scripts/install-fleet-skills-sync-cron.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccc-cron-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

export FAKE_CRON="$TMP/crontab.txt"
: > "$FAKE_CRON"

# Stub crontab: `-l` prints the file, `-` overwrites it from stdin.
STUB="$TMP/crontab-stub.sh"
BASH_BIN="$(command -v bash)"
cat > "$STUB" <<STUBEOF
#!$BASH_BIN
f="\${FAKE_CRON:?}"
case "\${1:-}" in
  -l) [ -s "\$f" ] && cat "\$f" || exit 1 ;;
  -)  cat > "\$f" ;;
  *)  exit 2 ;;
esac
STUBEOF
chmod +x "$STUB"
export CCC_CRONTAB_CMD="$STUB"
export CCC_CLAUDE_DIR="$TMP/claude"
export CCC_STATE_DIR="$CCC_CLAUDE_DIR/state"
mkdir -p "$CCC_CLAUDE_DIR/hooks" "$CCC_CLAUDE_DIR/state"
: > "$CCC_CLAUDE_DIR/hooks/ccc-fleet-skills-sync.py"

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
marker_count() { grep -E "# ccc-node:fleet-skills-sync gen=h_" "$FAKE_CRON" 2>/dev/null | wc -l | tr -d ' '; }
block_count() { grep -cF "# ccc-node:fleet-skills-sync:begin" "$FAKE_CRON" 2>/dev/null | head -1; }

# dry-run does not mutate the crontab
out="$(bash "$INSTALLER" --dry-run 2>&1)"; rc=$?
ok "dry-run exits 0" '[ "$rc" = 0 ]'
ok "dry-run announces install" 'printf "%s" "$out" | grep -q "would install"'
ok "dry-run does not write crontab" '[ "$(marker_count)" = 0 ]'

# apply installs exactly one entry line inside one managed block (#1077)
out="$(bash "$INSTALLER" --apply 2>&1)"; rc=$?
ok "apply exits 0" '[ "$rc" = 0 ]'
ok "apply installs one marker line" '[ "$(marker_count)" = 1 ]'
ok "apply wraps the entry in a managed block" '[ "$(block_count)" = 1 ] && grep -qF "# ccc-node:fleet-skills-sync:end" "$FAKE_CRON"'
ok "installed line carries default schedule" 'grep -qF "0 5 * * *" "$FAKE_CRON"'
ok "installed line loads login PATH via bash -lc" 'grep -qF "bash -lc" "$FAKE_CRON"'
ok "installed line resolves the exact ref at fire time" 'grep -qF "git ls-remote \"https://github.com/jinwon-int/fleet-skills.git\" \"main\" | cut -f1" "$FAKE_CRON"'
ok "installed line applies the resolved ref" 'grep -qF "ccc-fleet-skills-sync.py\" apply --ref \"\$S\"" "$FAKE_CRON"'
ok "installed line guards an empty ref (offline)" 'grep -qF "[ -n \"\$S\" ] &&" "$FAKE_CRON"'
ok "installed line pins CCC_CLAUDE_DIR for the sync" 'grep -qF "CCC_CLAUDE_DIR=\"$CCC_CLAUDE_DIR\" python3" "$FAKE_CRON"'

# generation stamp (#1081)
# shellcheck source=/dev/null
. "$ROOT/scripts/lib/installer-gen-stamp.sh"
want_gen="$(ccc_installer_gen_stamp_auto "$INSTALLER")"
ok "installed line carries gen stamp" 'grep -qE "# ccc-node:fleet-skills-sync gen=h_[0-9a-f]{12}$" "$FAKE_CRON"'
ok "gen stamp matches installer content" 'grep -qF "gen=$want_gen" "$FAKE_CRON"'
ok "gen inputs include the shared cron lib" 'ccc_installer_gen_inputs "$INSTALLER" | grep -q "lib/installer-cron-common.sh"'
ok "BEGIN/END block markers stay unstamped" '! grep -qE "fleet-skills-sync:(begin|end) gen=" "$FAKE_CRON"'

# idempotent: re-apply keeps a single line
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "re-apply stays idempotent (one line)" '[ "$(marker_count)" = 1 ]'
ok "re-apply keeps the same gen stamp" 'grep -qF "gen=$want_gen" "$FAKE_CRON"'

# custom schedule + repo override replace, still single line
CCC_FLEET_SKILLS_REPO="https://example.invalid/x.git" bash "$INSTALLER" --apply --schedule "3 * * * *" >/dev/null 2>&1
ok "custom schedule still single line" '[ "$(marker_count)" = 1 ]'
ok "custom schedule applied" 'grep -qF "3 * * * *" "$FAKE_CRON"'
ok "repo override applied" 'grep -qF "example.invalid/x.git" "$FAKE_CRON"'

# install record (#1081 phase 2)
REC="$CCC_CLAUDE_DIR/state/install-fleet-skills-sync-cron.json"
ok "apply writes an install record" '[ -f "$REC" ]'
ok "record carries schema/marker/gen" 'jq -e ".schema==\"ccc.install-record.v1\" and .marker==\"# ccc-node:fleet-skills-sync\" and .gen==\"$want_gen\"" "$REC" >/dev/null'
ok "record argv materializes the resolved schedule" 'jq -e ".argv == [\"--apply\",\"--schedule\",\"3 * * * *\"]" "$REC" >/dev/null'
ok "record is owner-only" '[ "$(stat -c %a "$REC")" = 600 ]'

# a pre-existing unrelated cron line is preserved
printf '0 4 * * * echo keepme\n' >> "$FAKE_CRON"
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "unrelated cron line preserved" 'grep -qF "echo keepme" "$FAKE_CRON"'
ok "still one marker line after preserve" '[ "$(marker_count)" = 1 ]'

# remove takes the marker line out, keeps the unrelated one
bash "$INSTALLER" --apply --remove >/dev/null 2>&1
ok "remove deletes marker line" '[ "$(marker_count)" = 0 ]'
ok "remove deletes the block markers too" '! grep -qF "# ccc-node:fleet-skills-sync:begin" "$FAKE_CRON"'
ok "remove keeps unrelated line" 'grep -qF "echo keepme" "$FAKE_CRON"'
ok "remove drops the install record" '[ ! -f "$REC" ]'

# legacy migration (#1077): the fleet's hand-installed bare line (no gen, no
# block — exactly what the 12 nodes carried on 2026-09-02) is folded into a block
printf '%s\n' "0 5 * * * bash -lc 'S=\$(git ls-remote https://github.com/jinwon-int/fleet-skills.git main | cut -f1); for P in \$HOME/ccc-node /opt/ccc-node /root/ccc-node; do [ -f \"\$P/scripts/ccc-fleet-skills-sync.py\" ] && python3 \"\$P/scripts/ccc-fleet-skills-sync.py\" apply --ref \"\$S\" && break; done' >> \$HOME/.claude/state/fleet-skills-sync.cron.log 2>&1 # ccc-node:fleet-skills-sync" '0 4 * * * echo keepme2' > "$FAKE_CRON"
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "legacy hand-installed line is migrated into a block" '[ "$(marker_count)" = 1 ] && [ "$(block_count)" = 1 ]'
ok "legacy checkout-loop body is gone" '! grep -qF "for P in" "$FAKE_CRON"'
ok "migration preserves unrelated lines" 'grep -qF "echo keepme2" "$FAKE_CRON"'
ok "no bare marker line survives migration" '[ "$(grep -cE "# ccc-node:fleet-skills-sync$" "$FAKE_CRON")" = 0 ]'

# corrupt managed block fails closed (#1077)
printf '%s\n' '# ccc-node:fleet-skills-sync:begin' '0 5 * * * dangling  # ccc-node:fleet-skills-sync gen=h_000000000000' > "$FAKE_CRON"
out="$(bash "$INSTALLER" --apply 2>&1)"; rc=$?
ok "corrupt managed block exits 4" '[ "$rc" = 4 ]'
ok "corrupt managed block is reported" 'printf "%s" "$out" | grep -q "corrupt managed schedule block"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
