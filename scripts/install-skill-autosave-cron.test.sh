#!/usr/bin/env bash
# Tests for install-skill-autosave-cron.sh — hermetic: a stubbed crontab backed
# by a temp file (CCC_CRONTAB_CMD), dry-run vs --apply, idempotency, unrelated-
# line preservation, --remove, and the crontab-absent guard. No real cron. (#457)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SC="$HERE/install-skill-autosave-cron.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

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

# shellcheck disable=SC2034  # $MARKER is consumed via eval in ok()
MARKER="# ccc-node:skill-autosave"
OUT="$TMP/out"; RC=0
run() { RC=0; CCC_CRONTAB_CMD="$STUB" "$@" >"$OUT" 2>&1 || RC=$?; }

# ---- crontab absent -> guard exits 3 ---------------------------------------
run env CCC_CRONTAB_CMD="$TMP/no-such-crontab" bash "$SC" --dry-run
okc "$RC" 3 "missing crontab exits 3"
ok "missing crontab is reported" 'grep -q "crontab command not found" "$OUT"'

# ---- dry-run: shows marker line, writes nothing ----------------------------
rm -f "$CRON_STORE"
run bash "$SC"
okc "$RC" 0 "dry-run exits 0"
ok "dry-run shows install intent" 'grep -q "would install skill-autosave cron" "$OUT"'
ok "dry-run shows the marker line" 'grep -qF "$MARKER" "$OUT"'
ok "dry-run references the autosave cmd" 'grep -q "ccc-skill-autosave.sh" "$OUT"'
ok "dry-run writes NO crontab" '[ ! -f "$CRON_STORE" ]'

# ---- --apply install: marker line lands in the crontab ---------------------
run bash "$SC" --apply
okc "$RC" 0 "apply exits 0"
ok "crontab now has exactly one marker line" '[ "$(grep -cF "$MARKER" "$CRON_STORE")" = 1 ]'
ok "installed line carries the schedule" 'grep -qF "45 20 * * *" "$CRON_STORE"'
ok "installed line runs the autosave cmd" 'grep -q "ccc-skill-autosave.sh" "$CRON_STORE"'

# ---- idempotency: re-apply keeps a single marker line ----------------------
run bash "$SC" --apply
okc "$RC" 0 "re-apply exits 0"
ok "re-apply still exactly one marker line" '[ "$(grep -cF "$MARKER" "$CRON_STORE")" = 1 ]'

# ---- unrelated pre-existing lines are preserved ----------------------------
printf '%s\n' "0 3 * * * /usr/bin/other-job" > "$CRON_STORE"
run bash "$SC" --apply
ok "unrelated line preserved on install" 'grep -qF "other-job" "$CRON_STORE"'
ok "marker line added alongside" '[ "$(grep -cF "$MARKER" "$CRON_STORE")" = 1 ]'

# ---- --remove --apply: marker line gone, unrelated kept --------------------
run bash "$SC" --remove --apply
okc "$RC" 0 "remove exits 0"
ok "marker line removed" '[ "$(grep -cF "$MARKER" "$CRON_STORE")" = 0 ]'
ok "unrelated line survives removal" 'grep -qF "other-job" "$CRON_STORE"'

# ---- custom --schedule propagates ------------------------------------------
rm -f "$CRON_STORE"
run bash "$SC" --apply --schedule "30 6 * * 1"
ok "custom schedule honored" 'grep -qF "30 6 * * 1" "$CRON_STORE"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
