#!/usr/bin/env bash
# Tests for converge-distill-peer.sh — hermetic (no live Honcho): honcho.json
# fixtures via CCC_HONCHO_CFG, backup/JSON-validity/aiPeer-rewrite on --apply,
# and --verify's no-baseUrl skip (no network). (#457)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SC="$HERE/converge-distill-peer.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

command -v jq >/dev/null 2>&1 || { echo "SKIP: jq absent"; exit 0; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
OUT="$TMP/out"; RC=0; out=""

run() { # <cfg> <arg...> -> $OUT + RC (in-parent, no subshell), and $out
  local cfg="$1"; shift
  RC=0
  CCC_HONCHO_CFG="$cfg" bash "$SC" "$@" >"$OUT" 2>&1 || RC=$?
  # shellcheck disable=SC2034  # $out is consumed via eval in ok()
  out="$(cat "$OUT")"
}

# ---- absent cfg -------------------------------------------------------------
run "$TMP/none.json" --check; okc "$RC" 0 "absent cfg exits 0"
ok "absent cfg reports absent" 'printf "%s" "$out" | grep -q "status=absent"'

# ---- invalid JSON -----------------------------------------------------------
printf 'not json{' > "$TMP/bad.json"
run "$TMP/bad.json" --check; okc "$RC" 1 "invalid JSON exits 1"
ok "invalid JSON reported" 'printf "%s" "$out" | grep -q "not valid JSON"'

# ---- already converged (flat) ----------------------------------------------
printf '{"aiPeer":"family-assistant"}' > "$TMP/conv.json"
run "$TMP/conv.json" --check; okc "$RC" 0 "converged exits 0"
ok "converged is no-op" 'printf "%s" "$out" | grep -q "status=converged"'

# ---- unset aiPeer -> fallback ----------------------------------------------
printf '{"other":"x"}' > "$TMP/unset.json"
run "$TMP/unset.json" --check; okc "$RC" 0 "unset aiPeer exits 0"
ok "unset -> fallback" 'printf "%s" "$out" | grep -q "status=fallback"'

# ---- explicit non-target (flat) + --check -> needs-apply (exit 2) ----------
printf '{"aiPeer":"nosuk"}' > "$TMP/flat.json"
run "$TMP/flat.json" --check; okc "$RC" 2 "needs-apply exits 2"
ok "check reports needs-apply" 'printf "%s" "$out" | grep -q "status=needs-apply"'

# ---- --apply flat: backup + valid JSON + rewrite ---------------------------
printf '{"aiPeer":"nosuk","keep":"me"}' > "$TMP/flat.json"
run "$TMP/flat.json" --apply; okc "$RC" 0 "apply exits 0"
ok "apply rewrites aiPeer" '[ "$(jq -r .aiPeer "$TMP/flat.json")" = "family-assistant" ]'
ok "apply preserves other keys" '[ "$(jq -r .keep "$TMP/flat.json")" = "me" ]'
ok "apply result is valid JSON" 'jq -e . "$TMP/flat.json" >/dev/null'
ok "apply created a backup" 'ls "$TMP"/flat.json.bak-* >/dev/null 2>&1'
ok "backup retains the old value" '[ "$(jq -r .aiPeer "$TMP"/flat.json.bak-*)" = "nosuk" ]'

# idempotency: re-apply is a converged no-op
run "$TMP/flat.json" --apply; okc "$RC" 0 "re-apply exits 0"
ok "re-apply is converged no-op" 'printf "%s" "$out" | grep -q "status=converged"'

# ---- --apply nested (.hosts.hermes.aiPeer) ---------------------------------
printf '{"hosts":{"hermes":{"aiPeer":"dungae"}}}' > "$TMP/nested.json"
run "$TMP/nested.json" --apply; okc "$RC" 0 "nested apply exits 0"
ok "nested apply rewrites nested field" '[ "$(jq -r .hosts.hermes.aiPeer "$TMP/nested.json")" = "family-assistant" ]'

# ---- --verify with no baseUrl: applies then skips live check (no network) ---
printf '{"aiPeer":"nosuk"}' > "$TMP/verify.json"
run "$TMP/verify.json" --verify; okc "$RC" 0 "verify(no baseUrl) exits 0"
ok "verify without baseUrl skips live check" 'printf "%s" "$out" | grep -q "skip live check"'
ok "verify still applied the rewrite" '[ "$(jq -r .aiPeer "$TMP/verify.json")" = "family-assistant" ]'

# ---- usage error ------------------------------------------------------------
run "$TMP/conv.json" --bogus; okc "$RC" 1 "unknown arg exits 1"

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
