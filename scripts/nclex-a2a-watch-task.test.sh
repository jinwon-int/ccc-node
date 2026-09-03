#!/usr/bin/env bash
# Tests for skills/nclex-a2a-content-pipeline/watch-task.sh — hermetic:
# a local python3 mock broker serves task JSON; no network, no real broker.
#
# Regression target is #1389: an ad-hoc watcher used the wrong terminal
# vocabulary (`completed`/`cancelled` instead of the broker's
# `succeeded`/`failed`/`canceled`), logged state=succeeded 38 times, timed out
# after 40 polls, and never wrote a result file. These tests pin:
#   - every real terminal status ends the watch and writes the out file
#   - an out-of-vocabulary status (e.g. the old wrong `completed`) trip-wires
#     an unknown_state exit instead of looping to max-polls
#   - timeout and every other exit path still writes the out file
#   - the task id is percent-encoded (lane ids contain ":" and ",")
#   - the edge secret reaches the broker via headers, not argv output
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WATCH="$HERE/../skills/nclex-a2a-content-pipeline/watch-task.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

command -v curl >/dev/null 2>&1 || { echo "SKIP: curl not available"; echo "PASS=0 FAIL=0"; exit 0; }

MOCK="$TMP/mock_broker.py"
PORT_FILE="$TMP/mock.port"
STATE_FILE="$TMP/state.json"
HDR_FILE="$TMP/headers.json"
SECRET="edge-secret-test-$$"
SECRET_FILE="$TMP/secret"

cat > "$MOCK" <<PY
import http.server, json, sys, urllib.parse

port_file, state_file, hdr_file = sys.argv[1], sys.argv[2], sys.argv[3]

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        rec = {k.lower(): v for k, v in self.headers.items()}
        # The request path is not a header; record it alongside (underscore
        # prefix cannot collide with a header name) for encoding assertions.
        rec["_request_path"] = self.path
        with open(hdr_file, "w") as fh:
            json.dump(rec, fh)
        with open(state_file) as fh:
            st = json.load(fh)
        if st.get("http_status"):
            self.send_response(st["http_status"])
            self.end_headers()
            return
        task_id = urllib.parse.unquote(self.path[len("/tasks/"):])
        body = json.dumps({"id": task_id, "status": st.get("status", "running")}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(port_file, "w") as fh:
    fh.write(str(srv.server_address[1]))
srv.serve_forever()
PY

printf '%s\n' "$SECRET" > "$SECRET_FILE"
set_state() { printf '{"status": "%s"}\n' "$1" > "$STATE_FILE"; }
set_state_http() { printf '{"http_status": %s}\n' "$1" > "$STATE_FILE"; }

python3 "$MOCK" "$PORT_FILE" "$STATE_FILE" "$HDR_FILE" &
MOCK_PID=$!
trap 'kill "$MOCK_PID" 2>/dev/null; rm -rf "$TMP"' EXIT

# Wait for the mock to publish its bound port.
mock_port=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$PORT_FILE" ] && { mock_port="$(cat "$PORT_FILE")"; break; }
  sleep 0.2
done
[ -n "$mock_port" ] || { echo "FAIL: mock broker did not start"; echo "PASS=$pass FAIL=$((fail+1))"; exit 1; }
BROKER="http://127.0.0.1:${mock_port}"
LANE_ID='nclex-pr459-test-aac0dcf-w1:RNM-20260902-001:terminology_bilingual'
OUT="$TMP/watch-result.json"

run_watch() {  # <status or "http:N"> then watcher args; runs against the mock
  local state="$1"; shift
  case "$state" in
    http:*) set_state_http "${state#http:}" ;;
    *) set_state "$state" ;;
  esac
  rm -f "$OUT" "$HDR_FILE"
  bash "$WATCH" --broker "$BROKER" --task-id "$LANE_ID" \
    --requester-id testnode --requester-role analyst \
    --secret-file "$SECRET_FILE" --interval 0 --out "$OUT" "$@"
}

result_field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1" "$2" 2>/dev/null; }

# --- 1) real terminal vocabulary: succeeded ends the watch, saves the task ---
out="$(run_watch succeeded 2>&1)"; rc=$?
ok "succeeded exits 0" '[ "$rc" = 0 ]'
ok "succeeded verdict recorded" '[ "$(result_field "$OUT" verdict)" = "succeeded" ]'
ok "succeeded out file carries the raw task record" \
  '[ "$(result_field "$OUT" task_status)" = "succeeded" ]'
ok "succeeded saves watcher schema marker" \
  '[ "$(result_field "$OUT" watcher_schema)" = "nclex-a2a-watch-result.v1" ]'
ok "WATCHER_RESULT line printed" 'grep -q "^WATCHER_RESULT=succeeded polls=1" <<<"$out"'

# --- 2) failed: terminal, exit 1, out file written ---------------------------
run_watch failed >/dev/null 2>&1; rc=$?
ok "failed exits 1" '[ "$rc" = 1 ]'
ok "failed verdict recorded" '[ "$(result_field "$OUT" verdict)" = "failed" ]'

# --- 3) canceled (the old British-spelling trap): terminal, exit 1 -----------
run_watch canceled >/dev/null 2>&1; rc=$?
ok "canceled exits 1" '[ "$rc" = 1 ]'
ok "canceled verdict recorded" '[ "$(result_field "$OUT" verdict)" = "canceled" ]'

# --- 4) THE #1389 REGRESSION: old wrong vocab `completed` must trip-wire -----
# shellcheck disable=SC2034  # $out is consumed via eval in ok()
out="$(run_watch completed --unknown-limit 2 --max-polls 10 2>&1)"; rc=$?
ok "out-of-vocabulary status exits 2 (fail-safe), not timeout" '[ "$rc" = 2 ]'
ok "unknown_state verdict recorded" '[ "$(result_field "$OUT" verdict)" = "unknown_state" ]'
ok "unknown_state polls stop at the limit (2), not max-polls" \
  '[ "$(result_field "$OUT" polls)" = "2" ]'
ok "first poll prints the observed raw status (vocabulary verification)" \
  'grep -q "^poll=1 status=completed$" <<<"$out"'
ok "unknown_state out file preserved with raw status" \
  '[ "$(result_field "$OUT" task_status)" = "completed" ]'

# --- 5) timeout still writes the result file (second #1389 defect) -----------
run_watch running --max-polls 2 >/dev/null 2>&1; rc=$?
ok "timeout exits 3" '[ "$rc" = 3 ]'
ok "timeout verdict recorded" '[ "$(result_field "$OUT" verdict)" = "timeout" ]'
ok "timeout out file exists for the next session" 'test -s "$OUT"'

# --- 6) 404 fails fast (typo'd task id must not burn the poll budget) --------
run_watch http:404 >/dev/null 2>&1; rc=$?
ok "404 exits 6 (not_found)" '[ "$rc" = 6 ]'
ok "not_found verdict recorded" '[ "$(result_field "$OUT" verdict)" = "not_found" ]'

# --- 7) usage error ----------------------------------------------------------
bash "$WATCH" --broker "$BROKER" --task-id x >/dev/null 2>&1; rc=$?
ok "missing --out is a usage error (64)" '[ "$rc" = 64 ]'

# --- 8) headers: secret reaches broker; task id percent-encoded --------------
run_watch running --max-polls 1 >/dev/null 2>&1
ok "edge secret delivered as header (from file, not argv)" \
  "python3 -c 'import json,sys; h=json.load(open(sys.argv[1])); sys.exit(0 if h.get(\"x-a2a-edge-secret\")==\"$SECRET\" else 1)' \"$HDR_FILE\""
ok "requester headers delivered" \
  "python3 -c 'import json,sys; h=json.load(open(sys.argv[1])); sys.exit(0 if h.get(\"x-a2a-requester-id\")==\"testnode\" and h.get(\"x-a2a-requester-role\")==\"analyst\" else 1)' \"$HDR_FILE\""
ok "raw path was percent-encoded (no bare \":\" from the lane id)" \
  "python3 -c 'import json,sys; h=json.load(open(sys.argv[1])); p=h.get(\"_request_path\",\"\"); sys.exit(0 if \":\" not in p and \"%3A\" in p else 1)' \"$HDR_FILE\""
ok "task id round-trips through percent-encoding" \
  '[ "$(result_field "$OUT" broker_url)" = "$BROKER/tasks/nclex-pr459-test-aac0dcf-w1%3ARNM-20260902-001%3Aterminology_bilingual" ]'

# --- 9) non-terminal statuses never end the watch ----------------------------
run_watch queued --max-polls 3 >/dev/null 2>&1
# shellcheck disable=SC2034  # $rc is consumed via eval in ok()
rc=$?
ok "queued runs to max-polls then timeout (3 polls)" "test \"\$rc\" = 3"
ok "timeout after queued records polls=3" '[ "$(result_field "$OUT" polls)" = "3" ]'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
