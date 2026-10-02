#!/usr/bin/env bash
# Tests for a2a-broker-worker-watch.py (#2086) — hermetic, no network.
# A stub `curl` on PATH answers /workers and /livez from fixtures, records
# its argv and stdin config, and lets each case pick an HTTP code or a curl
# exit code. Covers: secret transport (stdin config only, never argv/output),
# raw payload never printed, broker failure classification, config errors
# (exit 2), the DOWN line shape agent-cron's fleet title counts, and the
# owner-only state file. Then runs the python unit suite.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH="$ROOT/scripts/a2a-broker-worker-watch.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

BIN="$TMP/bin"; mkdir -p "$BIN"
cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
# Test stub: record argv + stdin config; answer by url and STUB_* knobs.
printf '%s\n' "$*" >> "$STUB_DIR/curl-argv"
cfg="$(cat)"
printf '%s\n---\n' "$cfg" >> "$STUB_DIR/curl-stdin"
case "$cfg" in
  *'/livez"'*)
    printf '{"ok":true,"uptimeSec":%s,"draining":false}\n200' "${STUB_UPTIME:-86400}"
    exit 0 ;;
esac
[ -n "${STUB_RC:-}" ] && exit "$STUB_RC"
if [ "${STUB_CODE:-200}" != 200 ]; then printf '{"error":"PAYLOAD-SENTINEL"}\n%s' "$STUB_CODE"; exit 0; fi
cat "$STUB_DIR/workers.json"; printf '\n200'
STUB
chmod +x "$BIN/curl"

export STUB_DIR="$TMP"
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$TMP/workers.json" <<JSON
{"items":[
 {"nodeId":"alpha","status":"stale","lastSeenAt":"$NOW_ISO","capabilities":{"note":"PAYLOAD-SENTINEL"}},
 {"nodeId":"beta","status":"online","lastSeenAt":"$NOW_ISO"}
]}
JSON
SECRET='abc"def\ghi jkl'
printf "A2A_EDGE_SECRET='%s'\n" "$SECRET" > "$TMP/edge.env"
printf "UNRELATED=1\n" > "$TMP/nosecret.env"

run() { # <state-name> [extra args...] -> $TMP/out, $TMP/err, $rc
  local st="$1"; shift
  PATH="$BIN:$PATH" python3 "$WATCH" --broker team9=http://127.0.0.1:8787 \
    --edge-env-file "$TMP/edge.env" --state-file "$TMP/$st/state.json" "$@" \
    >"$TMP/out" 2>"$TMP/err"
  rc=$?
}

# ---- 1: secret rides curl stdin config only --------------------------------
: > "$TMP/curl-argv"; : > "$TMP/curl-stdin"
run s1
ok "first run with a stale node is quiet (below threshold)" '[ "$rc" = 0 ] && grep -q "^PENDING alpha source=broker:team9 reason=worker-stale" "$TMP/out"'
ok "curl argv is exactly -sS --config - (secret never in argv)" \
  '[ -s "$TMP/curl-argv" ] && ! grep -qv "^-sS --config -$" "$TMP/curl-argv"'
ok "stdin config carries the escaped hostile header byte-exact" \
  'grep -qF "header = \"x-a2a-edge-secret: abc\\\"def\\\\ghi jkl\"" "$TMP/curl-stdin"'
ok "only /workers gets the secret header (/livez config has none)" \
  '[ "$(grep -c "x-a2a-edge-secret" "$TMP/curl-stdin")" = 1 ] && grep -q "/livez\"" "$TMP/curl-stdin"'
ok "secret and raw payload never reach stdout/stderr" \
  '! grep -qF "ghi jkl" "$TMP/out" "$TMP/err" && ! grep -q "PAYLOAD-SENTINEL\|capabilities\|lastSeenAt" "$TMP/out" "$TMP/err"'
ok "state file is owner-only" '[ "$(stat -c %a "$TMP/s1/state.json")" = 600 ] && ! grep -qF "ghi jkl" "$TMP/s1/state.json"'

# ---- 2: DOWN line shape and agent-cron fleet title -------------------------
run s2 --threshold 0s
ok "stale node past threshold pages with exit 1" '[ "$rc" = 1 ] && grep -qx "DOWN alpha source=broker:team9 reason=worker-stale age=0m" "$TMP/out"'
ok "paging lines come before info and summary" '[ "$(head -1 "$TMP/out" | cut -d" " -f1)" = DOWN ] && tail -1 "$TMP/out" | grep -q "^SUMMARY "'
# shellcheck disable=SC2034  # title is read via eval inside ok()
title="$(cd "$ROOT/scripts" && python3 - "$TMP/out" <<'PY'
import importlib.util, sys
sys.path.insert(0, ".")
spec = importlib.util.spec_from_file_location("agent_cron_title_probe", "agent_cron.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
out = open(sys.argv[1]).read()
# Owner-spool redaction masks long token runs (>=24 chars): every reason
# code and field must survive it unchanged or the page loses its meaning.
probe = out + "UNVERIFIED broker:team9 reason=env-unreadable\nUNREACHABLE broker:team9 reason=refused age=5m runs=2\n"
print("intact" if mod.redact_for_owner(probe, 4000) == probe.strip() or mod.redact_for_owner(probe, 4000) == probe else "masked")
print(mod.fleet_diagnostic_title("a2a-worker-watch", "failed", out, ""))
PY
)"
ok "agent-cron owner redaction leaves the output intact" '[ "$(printf "%s\n" "$title" | head -1)" = intact ]'
ok "agent-cron fleet title counts exactly the DOWN line" '[ "$(printf "%s\n" "$title" | tail -1)" = "agent-cron fleet alert for task a2a-worker-watch: DOWN=1" ]'
run s2 --threshold 0s
ok "same finding next run is held (exit 0, ONGOING)" '[ "$rc" = 0 ] && grep -q "^ONGOING alpha " "$TMP/out"'

# ---- 3: broker failures are broker findings, not stale workers -------------
STUB_CODE=401 run s3
ok "401 first run is pending" '[ "$rc" = 0 ] && grep -qx "PENDING broker:team9 reason=auth-rejected age=0m runs=1" "$TMP/out"'
STUB_CODE=401 run s3
ok "401 second run pages DEGRADED auth-rejected" '[ "$rc" = 1 ] && grep -q "^DEGRADED broker:team9 reason=auth-rejected .*runs=2$" "$TMP/out"'
ok "error body is never printed" '! grep -q "PAYLOAD-SENTINEL" "$TMP/out" "$TMP/err"'
STUB_RC=6 run s4 --broker-fail-runs 1
ok "curl exit 6 pages UNREACHABLE reason=dns" '[ "$rc" = 1 ] && grep -q "^UNREACHABLE broker:team9 reason=dns " "$TMP/out"'
STUB_CODE=503 run s5 --broker-fail-runs 1
ok "HTTP 503 is UNREACHABLE" 'grep -q "^UNREACHABLE broker:team9 reason=http-503 " "$TMP/out"'
ok "no worker line is emitted while the broker is failing" '! grep -q "alpha\|beta" "$TMP/out"'

# ---- 4: configuration errors exit 2 ----------------------------------------
PATH="$BIN:$PATH" python3 "$WATCH" --broker team9=http://127.0.0.1:8787 \
  --edge-env-file "$TMP/missing.env" --state-file "$TMP/s6/state.json" >"$TMP/out" 2>&1; rc=$?
ok "unreadable env file exits 2 with UNVERIFIED" '[ "$rc" = 2 ] && grep -qx "UNVERIFIED broker:team9 reason=env-unreadable" "$TMP/out"'
PATH="$BIN:$PATH" python3 "$WATCH" --broker team9=http://127.0.0.1:8787 \
  --edge-env-file "$TMP/nosecret.env" --state-file "$TMP/s6/state.json" >"$TMP/out" 2>&1; rc=$?
ok "env file without A2A_EDGE_SECRET exits 2" '[ "$rc" = 2 ] && grep -qx "UNVERIFIED broker:team9 reason=secret-missing" "$TMP/out"'
python3 "$WATCH" >"$TMP/out" 2>&1; rc=$?
ok "no broker configured is a usage error (exit 2)" '[ "$rc" = 2 ] && grep -q "usage error" "$TMP/out"'
python3 "$WATCH" --broker team9=http://x --threshold soon >"$TMP/out" 2>&1; rc=$?
ok "bad duration is a usage error (exit 2)" '[ "$rc" = 2 ]'
python3 "$WATCH" --bogus >"$TMP/out" 2>&1
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "unknown flag exits 2" '[ "$rc" = 2 ]'

# ---- 5: python unit suite ---------------------------------------------------
if python3 "$ROOT/scripts/a2a_broker_worker_watch_test.py" >"$TMP/unit.out" 2>&1; then
  pass=$((pass+1))
else
  fail=$((fail+1)); echo "FAIL: python unit suite"; tail -30 "$TMP/unit.out"
fi

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
