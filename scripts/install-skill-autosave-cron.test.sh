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
# Keep default-schedule assertions deterministic regardless of the test host.
export CCC_SKILL_AUTOSAVE_LOCAL_UTC_OFFSET=+0000
export CCC_SKILL_AUTOSAVE_LOCAL_TIMEZONE=Etc/UTC
# The installer resolves a fleet identity from $CCC_NODE / $STATE_DIR/node.txt
# (#1067). Point STATE_DIR at a fixture and clear CCC_NODE so no case reads the
# live node's identity: otherwise this suite passes on a provisioned node and
# behaves differently in CI, where neither exists.
export CCC_STATE_DIR="$TMP/state"
mkdir -p "$CCC_STATE_DIR"
printf 'fixture-node\n' > "$CCC_STATE_DIR/node.txt"
unset CCC_NODE

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

# ---- default UTC target is rendered in the host's local timezone -----------
run env CCC_SKILL_AUTOSAVE_LOCAL_TIMEZONE=Asia/Seoul CCC_SKILL_AUTOSAVE_LOCAL_UTC_OFFSET=+0900 bash "$SC"
okc "$RC" 0 "KST default conversion exits 0"
ok "KST host gets 05:45 local" 'grep -qF "45 5 * * *" "$OUT"'
ok "KST timezone is pinned" 'grep -qF "CRON_TZ=Asia/Seoul" "$OUT"'
run env CCC_SKILL_AUTOSAVE_LOCAL_TIMEZONE=America/New_York CCC_SKILL_AUTOSAVE_LOCAL_UTC_OFFSET=-0500 bash "$SC"
okc "$RC" 0 "negative-offset conversion exits 0"
ok "UTC-05 host gets 15:45 local" 'grep -qF "45 15 * * *" "$OUT"'
run env CCC_SKILL_AUTOSAVE_LOCAL_TIMEZONE=Asia/Kolkata CCC_SKILL_AUTOSAVE_LOCAL_UTC_OFFSET=+0530 bash "$SC"
okc "$RC" 0 "half-hour conversion exits 0"
ok "UTC+05:30 host gets 02:15 local" 'grep -qF "15 2 * * *" "$OUT"'
run env CCC_SKILL_AUTOSAVE_LOCAL_UTC_OFFSET=invalid bash "$SC"
okc "$RC" 2 "invalid UTC offset exits 2"
ok "invalid UTC offset is reported" 'grep -q "invalid local UTC offset" "$OUT"'

# ---- --apply install: marker line lands in the crontab ---------------------
run bash "$SC" --apply
okc "$RC" 0 "apply exits 0"
ok "crontab now has exactly one marker line" '[ "$(grep -cF "$MARKER" "$CRON_STORE")" = 1 ]'
ok "installed line carries the schedule" 'grep -qF "45 20 * * *" "$CRON_STORE"'
ok "installed line runs the autosave cmd" 'grep -q "ccc-skill-autosave.sh" "$CRON_STORE"'
ok "managed timezone block installed" \
  '[ "$(grep -cF "# ccc-node:autosave-schedule:begin" "$CRON_STORE")" = 1 ] && grep -qF "CRON_TZ=Etc/UTC" "$CRON_STORE"'

# ---- generation stamp (#1081): content hash of installer + shared libs ------
# Inputs are owned by ccc_installer_gen_inputs (#1077): installer + cron-common.
# shellcheck source=/dev/null
. "$HERE/lib/installer-gen-stamp.sh"
want_gen="$(ccc_installer_gen_stamp_auto "$SC")"
ok "installed line carries gen stamp" 'grep -qE "# ccc-node:skill-autosave gen=h_[0-9a-f]{12}$" "$CRON_STORE"'
ok "gen stamp matches installer content" 'grep -qF "gen=$want_gen" "$CRON_STORE"'
ok "BEGIN/END block markers stay unstamped (exact-match parsed)" '! grep -qE "autosave-schedule:(begin|end) gen=" "$CRON_STORE"'

# ---- install record (#1081 phase 2): replay material for self-update --------
REC="$CCC_STATE_DIR/install-skill-autosave-cron.json"
ok "apply writes an install record" '[ -f "$REC" ]'
ok "record carries schema/marker/gen" 'jq -e ".schema==\"ccc.install-record.v1\" and .marker==\"# ccc-node:skill-autosave\" and .gen==\"$want_gen\"" "$REC" >/dev/null'
ok "record argv materializes resolved schedule and fleet identity" \
  'jq -e ".argv == [\"--apply\",\"--schedule\",\"45 20 * * *\",\"--node\",\"fixture-node\"]" "$REC" >/dev/null'

# ---- idempotency: re-apply keeps a single marker line ----------------------
run bash "$SC" --apply
okc "$RC" 0 "re-apply exits 0"
ok "re-apply still exactly one marker line" '[ "$(grep -cF "$MARKER" "$CRON_STORE")" = 1 ]'
ok "re-apply still exactly one timezone block" \
  '[ "$(grep -cF "# ccc-node:autosave-schedule:begin" "$CRON_STORE")" = 1 ]'
ok "re-apply keeps the same gen stamp" 'grep -qF "gen=$want_gen" "$CRON_STORE"'

# ---- unrelated pre-existing lines are preserved ----------------------------
printf '%s\n' "CRON_TZ=Asia/Seoul" "0 3 * * * /usr/bin/other-job" > "$CRON_STORE"
run bash "$SC" --apply
ok "unrelated line preserved on install" 'grep -qF "other-job" "$CRON_STORE"'
ok "unrelated timezone preserved on install" '[ "$(grep -cF "CRON_TZ=Asia/Seoul" "$CRON_STORE")" = 1 ]'
ok "marker line added alongside" '[ "$(grep -cF "$MARKER" "$CRON_STORE")" = 1 ]'

# ---- --remove --apply: marker line gone, unrelated kept --------------------
run bash "$SC" --remove --apply
okc "$RC" 0 "remove exits 0"
ok "marker line removed" '[ "$(grep -cF "$MARKER" "$CRON_STORE")" = 0 ]'
ok "unrelated line survives removal" 'grep -qF "other-job" "$CRON_STORE"'
ok "managed timezone block removed" '! grep -qF "# ccc-node:autosave-schedule:begin" "$CRON_STORE" && ! grep -qF "CRON_TZ=Etc/UTC" "$CRON_STORE"'
ok "unrelated timezone survives removal" 'grep -qF "CRON_TZ=Asia/Seoul" "$CRON_STORE"'
ok "remove drops the install record (no resurrection via re-apply)" '[ ! -f "$REC" ]'

# ---- malformed managed block fails closed ---------------------------------
printf '%s\n' '# ccc-node:autosave-schedule:begin' 'CRON_TZ=Etc/UTC' > "$CRON_STORE"
run bash "$SC" --apply
okc "$RC" 4 "corrupt managed block exits 4"
ok "corrupt managed block is reported" 'grep -q "corrupt managed schedule block" "$OUT"'

# ---- custom --schedule propagates ------------------------------------------
rm -f "$CRON_STORE"
run bash "$SC" --apply --schedule "30 6 * * 1"
ok "custom schedule honored" 'grep -qF "30 6 * * 1" "$CRON_STORE"'

# ---- fleet identity is baked into the entry (#1067) ------------------------
# Without CCC_NODE in the cron line, `bash -lc` gives ccc-skill-promotion.py no
# identity and scheduled staging refuses with node_identity_unresolved — the
# publisher-side symptom that hid this on 11 of 12 nodes.
rm -f "$CRON_STORE"
run bash "$SC" --apply
ok "installed line carries the fleet identity from node.txt" \
  'grep -qF "CCC_NODE=\"fixture-node\"" "$CRON_STORE"'
ok "identity precedes the autosave command" \
  'grep -qE "CCC_NODE=\"fixture-node\" CCC_CLAUDE_DIR=" "$CRON_STORE"'

rm -f "$CRON_STORE"
run env CCC_NODE=env-node bash "$SC" --apply
ok "CCC_NODE overrides node.txt" 'grep -qF "CCC_NODE=\"env-node\"" "$CRON_STORE"'

rm -f "$CRON_STORE"
run env CCC_NODE=env-node bash "$SC" --apply --node flag-node
ok "--node wins over CCC_NODE" \
  'grep -qF "CCC_NODE=\"flag-node\"" "$CRON_STORE" && ! grep -qF "env-node" "$CRON_STORE"'

# Sanitization mirrors _safe_node() so the installer cannot bake a value the
# Python side would rewrite or reject.
rm -f "$CRON_STORE"
run bash "$SC" --apply --node "  Yuk_Son!! "
ok "identity is sanitized like _safe_node" 'grep -qF "CCC_NODE=\"yuk-son\"" "$CRON_STORE"'

# Unresolvable identity must not guess: hostname is a machine name, not the
# fleet alias the publisher matches (yukson vs vps5). Install, but say so.
rm -f "$CRON_STORE"
run env CCC_STATE_DIR="$TMP/no-identity" bash "$SC" --apply
okc "$RC" 0 "unresolved identity still installs cron"
ok "unresolved identity omits CCC_NODE" '! grep -qF "CCC_NODE=" "$CRON_STORE"'
ok "unresolved identity is reported" 'grep -q "no fleet identity resolved" "$OUT"'
# Pin the no-guess assertion to the CCC_NODE assignment itself. Grepping the
# whole store for the hostname string false-positives on nodes whose username
# equals the hostname (#1339): the crontab lines legitimately embed
# /home/<user> paths that contain it.
hn="$(hostname -s 2>/dev/null || echo __no_hostname__)"
guess="CCC_NODE=\"${hn}\""
ok "unresolved identity never guesses the hostname" \
  '! grep -qF "$guess" "$CRON_STORE"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
