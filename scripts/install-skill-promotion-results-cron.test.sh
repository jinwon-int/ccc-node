#!/usr/bin/env bash
# Tests for install-skill-promotion-results-cron.sh (#2239) — hermetic: a
# stubbed crontab backed by a temp file (CCC_CRONTAB_CMD), dry-run vs --apply,
# idempotency, the publisher-flag refusal, placement below the autosave CRON_TZ
# block, legacy ad-hoc line detection + --migrate-legacy, --status and
# --remove. No real cron. Modeled on install-skill-autosave-cron.test.sh.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SC="$HERE/install-skill-promotion-results-cron.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# Fixture state dir: fleet identity + (later) the publisher flag. CCC_NODE is
# cleared so no case reads the live node's identity.
export CCC_STATE_DIR="$TMP/state"
export CCC_CLAUDE_DIR="$TMP/claude"
mkdir -p "$CCC_STATE_DIR" "$CCC_CLAUDE_DIR"
chmod 700 "$CCC_STATE_DIR"
printf 'fixture-node\n' > "$CCC_STATE_DIR/node.txt"
unset CCC_NODE
# Deterministic baked paths; neither file needs to exist for the installer.
export CCC_SKILL_PROMOTION_ENV_FILE="$TMP/promotion.env"
export CCC_A2A_EDGE_ENV="$TMP/edge.env"
# shellcheck disable=SC2034  # read via eval inside ok()
FLAG="$CCC_STATE_DIR/skill-promotion.publisher"

# Stubbed crontab backed by a temp file (CRON_STORE, read from env).
export CRON_STORE="$TMP/crontab.store"
STUB="$TMP/crontab"
cat > "$STUB" <<'STUBEOF'
#!/usr/bin/env bash
case "${1:-}" in
  -l) [ -f "$CRON_STORE" ] || exit 1; cat "$CRON_STORE" ;;
  -)  cat > "$CRON_STORE" ;;
  *)  exit 2 ;;
esac
STUBEOF
chmod +x "$STUB"

# shellcheck disable=SC2034  # consumed via eval in ok()
MARKER="# ccc-node:skill-promotion-results"
# shellcheck disable=SC2034
BEGIN="# ccc-node:skill-promotion-results:begin"
# shellcheck disable=SC2034
END="# ccc-node:skill-promotion-results:end"
# shellcheck disable=SC2034
LEGACY="*/15 * * * * cd /root/ccc-node-wt/skill-pipeline-repair-20261004 && python3 skill-pipeline-repair-20261004/run-results.py >> /root/.claude/state/results.log 2>&1"
OUT="$TMP/out"; RC=0
entries() { grep -cF -- "$MARKER gen=" "$CRON_STORE" || true; }
run() { RC=0; CCC_CRONTAB_CMD="$STUB" "$@" >"$OUT" 2>&1 || RC=$?; }
set_flag() { printf '%s\n' "$1" > "$FLAG"; chmod "${2:-600}" "$FLAG"; }

# ---- crontab absent -> guard exits 3 ---------------------------------------
run env CCC_CRONTAB_CMD="$TMP/no-such-crontab" bash "$SC" --dry-run
okc "$RC" 3 "missing crontab exits 3"
ok "missing crontab is reported" 'grep -q "crontab command not found" "$OUT"'

# ---- dry-run: shows the entry, writes nothing (no publisher flag yet) -------
rm -f "$CRON_STORE" "$FLAG"
run bash "$SC"
okc "$RC" 0 "dry-run exits 0"
ok "dry-run shows install intent" 'grep -q "would install skill-promotion-results cron" "$OUT"'
ok "dry-run shows the marker line" 'grep -qF "$MARKER" "$OUT"'
ok "dry-run renders collect --results-only with a single-try lock" \
  'grep -qF "ccc-skill-promotion.py\" collect --results-only --lock-wait 0" "$OUT"'
ok "dry-run bakes the fleet identity and claude dir" \
  'grep -qF "CCC_NODE=\"fixture-node\" CCC_CLAUDE_DIR=\"$CCC_CLAUDE_DIR\"" "$OUT"'
ok "dry-run sources the promotion env file first, guarded owner-only" \
  'grep -qF "P=\"$TMP/promotion.env\"; if [ -f \"\$P\" ] && [ ! -L \"\$P\" ] && [ -O \"\$P\" ]" "$OUT" && grep -qF "perm /077" "$OUT"'
ok "dry-run sources the edge env after the promotion env" \
  'grep -oE "P=\"[^\"]+\".*E=\"[^\"]+\"" "$OUT" | grep -qF "promotion.env" && grep -qF "E=\"$TMP/edge.env\"; if [ -f \"\$E\" ]; then set -a; . \"\$E\"" "$OUT"'
ok "dry-run logs to the results cron log" 'grep -qF ">> \"$CCC_STATE_DIR/skill-promotion.results.cron.log\" 2>&1" "$OUT"'
ok "dry-run reports the publisher flag and legacy state" \
  'grep -q "publisher: false; legacy_results_cron: absent" "$OUT"'
ok "dry-run on a non-publisher says --apply would be refused" 'grep -q "would be REFUSED: publisher flag is not true" "$OUT"'
ok "dry-run writes NO crontab" '[ ! -f "$CRON_STORE" ]'
ok "dry-run never prints a secret-shaped value" '! grep -qiE "A2A_EDGE_SECRET=" "$OUT"'

# ---- --apply refused without the publisher flag (exit 5, nothing written) --
run bash "$SC" --apply
okc "$RC" 5 "apply without publisher flag exits 5"
ok "publisher refusal is reported" 'grep -q "REFUSED: publisher flag is not true" "$OUT"'
ok "publisher refusal writes NO crontab" '[ ! -f "$CRON_STORE" ]'
set_flag true 644
run bash "$SC" --apply
okc "$RC" 5 "a 0644 publisher flag is not trusted (exit 5)"
set_flag false 600
run bash "$SC" --apply
okc "$RC" 5 "publisher flag false exits 5"
ok "no record is written on refusal" '[ ! -f "$CCC_STATE_DIR/install-skill-promotion-results-cron.json" ]'

# ---- --apply with the flag: block with its own CRON_TZ pin (no autosave block)
set_flag true
run bash "$SC" --apply
okc "$RC" 0 "apply exits 0 on a publisher"
ok "crontab has exactly one marker line" '[ "$(entries)" = 1 ]'
ok "block begin/end present once" \
  '[ "$(grep -cxF "$BEGIN" "$CRON_STORE")" = 1 ] && [ "$(grep -cxF "$END" "$CRON_STORE")" = 1 ]'
ok "without an autosave block the block opens with CRON_TZ=Asia/Seoul" \
  '[ "$(sed -n "/^# ccc-node:skill-promotion-results:begin$/{n;p;}" "$CRON_STORE")" = "CRON_TZ=Asia/Seoul" ]'
ok "entry follows the CRON_TZ line" \
  '[ "$(grep -n "CRON_TZ=Asia/Seoul" "$CRON_STORE" | cut -d: -f1)" -lt "$(grep -nF -- "$MARKER gen=" "$CRON_STORE" | cut -d: -f1)" ]'
ok "installed entry carries the schedule" 'grep -qF "*/15 * * * * bash -lc" "$CRON_STORE"'
ok "apply reports placement" 'grep -q "placement: appended with its own CRON_TZ=Asia/Seoul pin" "$OUT"'

# ---- generation stamp (#1081): installer + shared cron lib -----------------
# shellcheck source=/dev/null
. "$HERE/lib/installer-gen-stamp.sh"
# shellcheck disable=SC2034  # want_gen is read via eval inside ok()
want_gen="$(ccc_installer_gen_stamp_auto "$SC")"
ok "installed line carries gen stamp" 'grep -qE "# ccc-node:skill-promotion-results gen=h_[0-9a-f]{12}$" "$CRON_STORE"'
ok "gen stamp matches installer content" 'grep -qF "gen=$want_gen" "$CRON_STORE"'
ok "gen inputs include the shared cron lib" 'ccc_installer_gen_inputs "$SC" | grep -q "lib/installer-cron-common.sh"'
ok "BEGIN/END block markers stay unstamped" '! grep -qE "skill-promotion-results:(begin|end) gen=" "$CRON_STORE"'

# ---- install record (#1081 phase 2) ----------------------------------------
# shellcheck disable=SC2034  # REC is read via eval inside ok()
REC="$CCC_STATE_DIR/install-skill-promotion-results-cron.json"
ok "apply writes an install record" '[ -f "$REC" ]'
ok "record carries schema/marker/gen" \
  'jq -e ".schema==\"ccc.install-record.v1\" and .marker==\"# ccc-node:skill-promotion-results\" and .gen==\"$want_gen\"" "$REC" >/dev/null'
ok "record argv materializes schedule and fleet identity" \
  'jq -e ".argv == [\"--apply\",\"--schedule\",\"*/15 * * * *\",\"--node\",\"fixture-node\"]" "$REC" >/dev/null'
ok "record is owner-only" '[ "$(stat -c %a "$REC")" = 600 ]'

# ---- idempotency -----------------------------------------------------------
run bash "$SC" --apply
okc "$RC" 0 "re-apply exits 0"
ok "re-apply still exactly one marker line" '[ "$(entries)" = 1 ]'
ok "re-apply still exactly one block" '[ "$(grep -cxF "$BEGIN" "$CRON_STORE")" = 1 ]'
ok "re-apply still exactly one CRON_TZ pin of ours" '[ "$(grep -cF "CRON_TZ=Asia/Seoul" "$CRON_STORE")" = 1 ]'

# ---- --status --------------------------------------------------------------
run bash "$SC" --status
okc "$RC" 0 "status exits 0"
ok "status reports the publisher flag" 'grep -q "publisher flag: true" "$OUT"'
ok "status reports env file presence (absent here), never content" \
  'grep -q "promotion env file: absent" "$OUT" && grep -q "a2a edge env: absent" "$OUT"'
ok "status reports the installed block as current" 'grep -q "managed block: installed gen=$want_gen (current)" "$OUT"'
ok "status reports no legacy line" 'grep -q "legacy_results_cron: absent" "$OUT"'
ok "status reports the install record" 'grep -q "install record: present" "$OUT"'
printf 'CCC_SKILL_PROMOTION_COLLECT_WINDOW=7\n' > "$TMP/promotion.env"; chmod 600 "$TMP/promotion.env"
run bash "$SC" --status
ok "status reports an owner-only env file as present" 'grep -q "promotion env file: present" "$OUT"'
chmod 644 "$TMP/promotion.env"
run bash "$SC" --status
ok "status reports a group/other-readable env file as unsafe" 'grep -q "promotion env file: unsafe" "$OUT"'
rm -f "$TMP/promotion.env"

# ---- placement: directly below the autosave block, no own CRON_TZ -----------
printf '%s\n' \
  "CRON_TZ=Etc/UTC" \
  "0 3 * * * /usr/bin/other-job" \
  "# ccc-node:autosave-schedule:begin" \
  "CRON_TZ=Asia/Seoul" \
  "45 5 * * * bash -lc 'x'  # ccc-node:skill-autosave gen=h_000000000000" \
  "# ccc-node:autosave-schedule:end" \
  "30 4 * * * /usr/bin/trailing-job" > "$CRON_STORE"
run bash "$SC" --apply
okc "$RC" 0 "apply next to an autosave block exits 0"
ok "block sits directly below the autosave block" \
  '[ "$(sed -n "/^# ccc-node:autosave-schedule:end$/{n;p;}" "$CRON_STORE")" = "$BEGIN" ]'
ok "block inherits the autosave CRON_TZ (no own pin)" \
  '[ "$(sed -n "/^# ccc-node:skill-promotion-results:begin$/{n;p;}" "$CRON_STORE" | grep -cF "$MARKER")" = 1 ] && [ "$(grep -cF "CRON_TZ=Asia/Seoul" "$CRON_STORE")" = 1 ]'
ok "trailing unrelated line is preserved after the block" \
  '[ "$(grep -nF "trailing-job" "$CRON_STORE" | cut -d: -f1)" -gt "$(grep -nxF "$END" "$CRON_STORE" | cut -d: -f1)" ]'
ok "leading unrelated lines are preserved" 'grep -qF "other-job" "$CRON_STORE" && grep -qF "CRON_TZ=Etc/UTC" "$CRON_STORE"'
ok "autosave block is untouched" '[ "$(grep -cF "# ccc-node:skill-autosave gen=h_000000000000" "$CRON_STORE")" = 1 ]'
ok "apply reports the placement" 'grep -q "placement: below autosave block" "$OUT"'
run bash "$SC" --apply
ok "re-apply next to an autosave block keeps one block in place" \
  '[ "$(entries)" = 1 ] && [ "$(sed -n "/^# ccc-node:autosave-schedule:end$/{n;p;}" "$CRON_STORE")" = "$BEGIN" ]'

# ---- legacy ad-hoc line: detected, refused without --migrate-legacy --------
printf '%s\n' "0 3 * * * /usr/bin/other-job" "$LEGACY" > "$CRON_STORE"
run bash "$SC"
okc "$RC" 0 "dry-run with a legacy line exits 0"
ok "dry-run reports legacy_results_cron: present" 'grep -q "legacy_results_cron: present" "$OUT"'
ok "dry-run says --apply would be refused for the legacy line" 'grep -q "would be REFUSED: legacy ad-hoc collector line present" "$OUT"'
cp "$CRON_STORE" "$TMP/crontab.before-legacy"
run bash "$SC" --apply
okc "$RC" 6 "apply with a legacy line and no --migrate-legacy exits 6"
ok "legacy refusal leaves the crontab untouched" 'cmp -s "$CRON_STORE" "$TMP/crontab.before-legacy"'
run bash "$SC" --status
ok "status reports legacy_results_cron: present" 'grep -q "legacy_results_cron: present" "$OUT"'
run bash "$SC" --migrate-legacy
ok "dry-run --migrate-legacy previews the legacy line gone" \
  '! grep -qF "run-results.py" "$OUT" || ! sed -n "/resulting crontab:/,\$p" "$OUT" | grep -qF "run-results.py"'
ok "dry-run --migrate-legacy writes nothing" 'cmp -s "$CRON_STORE" "$TMP/crontab.before-legacy"'
run bash "$SC" --apply --migrate-legacy
okc "$RC" 0 "--apply --migrate-legacy exits 0"
ok "legacy line is removed" '! grep -qF "run-results.py" "$CRON_STORE"'
ok "managed block is installed in its place" '[ "$(entries)" = 1 ]'
ok "unrelated line survives the migration" 'grep -qF "other-job" "$CRON_STORE"'
ok "migration is reported" 'grep -q "legacy_results_cron: present removed" "$OUT"'
run bash "$SC" --status
ok "status reports legacy gone after migration" 'grep -q "legacy_results_cron: absent" "$OUT"'

# ---- --remove --apply: block gone, unrelated kept, record dropped ----------
# Removal is allowed regardless of the publisher flag.
set_flag false
run bash "$SC" --remove --apply
okc "$RC" 0 "remove exits 0 (no publisher gate on removal)"
ok "marker line removed" '[ "$(entries)" = 0 ]'
ok "block markers removed" '! grep -qxF "$BEGIN" "$CRON_STORE" && ! grep -qxF "$END" "$CRON_STORE"'
ok "unrelated line survives removal" 'grep -qF "other-job" "$CRON_STORE"'
ok "remove drops the install record" '[ ! -f "$REC" ]'
set_flag true

# ---- malformed managed block fails closed ---------------------------------
printf '%s\n' "$BEGIN" 'CRON_TZ=Asia/Seoul' > "$CRON_STORE"
run bash "$SC" --apply
okc "$RC" 4 "corrupt managed block exits 4"
ok "corrupt managed block is reported" 'grep -q "corrupt managed schedule block" "$OUT"'

# ---- custom --schedule and --node propagate --------------------------------
rm -f "$CRON_STORE"
run bash "$SC" --apply --schedule "*/30 * * * *" --node "  Yuk_Son!! "
okc "$RC" 0 "custom schedule/node apply exits 0"
ok "custom schedule honored" 'grep -qF "*/30 * * * * bash -lc" "$CRON_STORE"'
ok "identity is sanitized like _safe_node" 'grep -qF "CCC_NODE=\"yuk-son\"" "$CRON_STORE"'
run bash "$SC" --schedule 'x"; id'
okc "$RC" 2 "a metachar schedule is rejected"
run env CCC_SKILL_PROMOTION_ENV_FILE='/tmp/it'"'"'s.env' bash "$SC"
okc "$RC" 2 "a single-quote env file path is refused (cannot be baked)"
ok "unsafe path refusal is reported" 'grep -q "refusing to bake unsafe path" "$OUT"'

# Unresolvable identity installs without CCC_NODE and never guesses the hostname.
rm -f "$CRON_STORE"
run env CCC_STATE_DIR="$TMP/no-identity" bash "$SC"
ok "unresolved identity omits CCC_NODE" '! grep -qF "CCC_NODE=" "$OUT"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
