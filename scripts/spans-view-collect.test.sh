#!/usr/bin/env bash
# Hermetic tests for scripts/spans-view-collect.sh (#2858 item 4): the ssh
# probe call shape, how an empty answer is classified (255 UNREACHABLE, 124
# UNVERIFIED), the retry, the size cap, the self shortcut, connection reuse,
# and that nothing is left behind on the hub. A fake ssh stands in for nodes.
# shellcheck disable=SC2034  # test variables are read inside the eval'd ok() conditions
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
C="$DIR/spans-view-collect.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

DAY=2026-11-23
NODE_STATE="$TMP/node-state"; HUB="$TMP/hub"; OUT="$TMP/uploads/spans"; CALLS="$TMP/calls.log"
mkdir -p "$NODE_STATE/spans-rollup" "$TMP/uploads" "$TMP/bin"
cat > "$NODE_STATE/spans-rollup/$DAY.json" <<'EOF'
{"schema":"ccc.spans-rollup.v1","date":"2026-11-23","tz":"Asia/Seoul","node":"alpha","empty":false,
 "totals":{"spans":4,"turns":2,"tool_calls":2,"tool_errors":0,"tool_error_rate_pct":0.0},
 "tools":{"Bash":{"calls":2,"errors":0,"error_rate_pct":0.0,"p50_ms":10.0,"p95_ms":20.0}},
 "models":{"model-a":{"turns":2,"input_tokens":10,"output_tokens":20,"cost_usd":0.5}},
 "cost_ledger_check":{"available":false,"reason":"x"},"read":{"files_read":1}}
EOF

# Fake ssh: logs its arguments, then behaves per node name. The probe arrives
# on stdin exactly as it would on a node.
cat > "$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALLS"
node=''; day="${*: -1}"
prev=''
for a in "$@"; do
  [ "$a" = python3 ] && break
  if [ "$prev" != -o ] && [ "$a" != -o ]; then node="$a"; fi
  prev="$a"
done
count_file="$TMP/attempts.$node"; n=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$count_file"
case "$node" in
  down) exit 255 ;;
  slow) exit 124 ;;
  dies) echo "SPANS_VIEW_STATUS=ok"; exit 1 ;;
  drop255) echo "SPANS_VIEW_STATUS=ok"; exit 255 ;;
  hang) echo "SPANS_VIEW_STATUS=ok"; sleep 30 ;;
  flaky) [ "$n" -ge 2 ] || exit 255 ;;
  huge) head -c 3000000 /dev/zero | tr '\0' 'x'; exit 0 ;;
  silent) exit 0 ;;
esac
CCC_SPANS_VIEW_STATE_DIR="$NODE_STATE" python3 - "$day"
EOF
chmod +x "$TMP/bin/ssh"
export CALLS TMP NODE_STATE
run() { # <nodes> [extra args...]
  local nodes="$1"; shift
  : > "$CALLS"; rm -f "$TMP"/attempts.*
  CCC_FLEET_SSH="$TMP/bin/ssh" CCC_FLEET_NODES="$nodes" CCC_FLEET_SELF="${SELF_NAME:-_none_}" \
    CCC_STATE_DIR="$HUB" CCC_SPANS_VIEW_RETRY_DELAY=0 CCC_SPANS_VIEW_STATE_DIR="${SELF_STATE:-}" \
    CCC_SPANS_VIEW_NOW="2026-11-24T04:30:00+09:00" CCC_SPANS_VIEW_PROBE_TIMEOUT="${PROBE_LIMIT:-20}" \
    bash "$C" --out-dir "$OUT" --date "$DAY" "$@" 2>"$TMP/stderr"
}

# 1. classification of every kind of answer
out="$(run "alpha down slow dies drop255 flaky huge silent")"; rc=$?
ok "run succeeds even when most nodes did not answer" '[ "$rc" = 0 ]'
ok "a good answer is collected" 'grep -qx "SPANS_VIEW OK alpha" <<<"$out"'
ok "ssh 255 with no output is UNREACHABLE" 'grep -qx "SPANS_VIEW UNREACHABLE down ssh-255" <<<"$out"'
ok "timeout 124 with no output is UNVERIFIED" 'grep -qx "SPANS_VIEW UNVERIFIED slow timeout-124" <<<"$out"'
ok "partial output from a probe that exited non-zero is not an answer, and is retried" \
  'grep -qx "SPANS_VIEW UNVERIFIED dies probe-error" <<<"$out" && [ "$(cat "$TMP/attempts.dies")" = 2 ]'
ok "ssh 255 after partial output is still UNREACHABLE, and is retried" \
  'grep -qx "SPANS_VIEW UNREACHABLE drop255 ssh-255" <<<"$out" && [ "$(cat "$TMP/attempts.drop255")" = 2 ]'
ok "an empty answer on the first attempt is retried once and can succeed" \
  'grep -qx "SPANS_VIEW OK flaky" <<<"$out" && [ "$(cat "$TMP/attempts.flaky")" = 2 ]'
ok "a node that keeps failing is tried exactly twice" '[ "$(cat "$TMP/attempts.down")" = 2 ]'
ok "an answer over the size cap is cut and reported OVERSIZE" 'grep -qx "SPANS_VIEW OVERSIZE huge size" <<<"$out"'
ok "an empty answer with exit 0 is UNVERIFIED, not collected" 'grep -qx "SPANS_VIEW UNVERIFIED silent probe-error" <<<"$out"'
ok "pages are written with the not-collected reasons" \
  '[ -s "$OUT/$DAY.html" ] && [ -s "$OUT/index.html" ] && grep -q "UNREACHABLE — ssh 연결 실패(255)" "$OUT/$DAY.html"'
ok "final JSON line reports the counts" 'tail -1 <<<"$out" | jq -e ".ok and .collected == 2 and (.not_collected|length) == 6" >/dev/null'

# 1b. a node that really hangs is cut at the limit and reported as a timeout
start=$(date +%s)
out="$(PROBE_LIMIT=1 run "hang")"; rc=$?
took=$(( $(date +%s) - start ))
ok "a hung probe is stopped at the limit (two 1s attempts, not 2 x 30s) and is UNVERIFIED timeout" \
  '[ "$rc" = 0 ] && grep -qx "SPANS_VIEW UNVERIFIED hang timeout-124" <<<"$out" && [ "$took" -lt 20 ] && [ "$(cat "$TMP/attempts.hang")" = 2 ]'
out="$(PROBE_LIMIT=99999999999999999999 run "alpha")"; rc=$?
ok "an absurd timeout value falls back to the default instead of breaking the run" \
  '[ "$rc" = 0 ] && grep -qx "SPANS_VIEW OK alpha" <<<"$out" && ! grep -qi "integer expression" "$TMP/stderr"'

# 2. the remote command: batch mode, the probe on stdin, the day as the only argument
ok "ssh is called in batch mode with python3 - <day>" \
  'grep -q -- "-o BatchMode=yes -o ConnectTimeout=8 alpha python3 - $DAY\$" "$CALLS"'
ok "a standalone run adds no connection-sharing option" '! grep -q "ControlMaster\|ControlPath" "$CALLS"'
ok "the staging folder is removed after the run" '[ -z "$(ls -A "$HUB/spans-view/incoming" 2>/dev/null)" ]'
ok "collected records are private (0700 folder, 0600 file)" \
  '[ "$(stat -c %a "$HUB/spans-view/data/$DAY")" = 700 ] && [ "$(stat -c %a "$HUB/spans-view/data/$DAY/alpha.json")" = 600 ]'
ok "pages are world-readable (0755 folder, 0644 file) despite the private umask" \
  '[ "$(stat -c %a "$OUT")" = 755 ] && [ "$(stat -c %a "$OUT/index.html")" = 644 ]'
ok "nothing was written on the node side" \
  '[ "$(find "$NODE_STATE" -type f | wc -l)" = 1 ] && [ -z "$(find "$NODE_STATE" -newer "$C" -name "*.tmp")" ]'

# 3. inside a fleet sweep the first attempt rides the shared connection, the retry does not
MUX="$(umask 077; mktemp -d "$TMP/mux.XXXXXX")"
out="$(CCC_FLEET_SSH_MUX_DIR="$MUX" run "alpha down")"
ok "first attempt uses the sweep's shared connection" \
  'grep -q -- "ControlMaster=auto -o ControlPath=$MUX/alpha -o ControlPersist=120 alpha python3" "$CALLS"'
ok "the retry makes its own connection" 'grep -q -- "ControlMaster=no -o ControlPath=none down python3" "$CALLS"'

# 4. the hub node reads its own file without ssh
out="$(SELF_NAME=alpha SELF_STATE="$NODE_STATE" run "alpha down")"
ok "the hub's own node is probed locally" 'grep -qx "SPANS_VIEW OK alpha" <<<"$out" && ! grep -q " alpha python3" "$CALLS"'

# 5. input gates
out="$(run "alpha" --date "2026-11-23;touch $TMP/pwned")"; rc=$?
ok "a date that is not YYYY-MM-DD is refused before any ssh call" '[ "$rc" = 2 ] && [ ! -s "$CALLS" ] && [ ! -e "$TMP/pwned" ]'
out="$(run "alpha ../evil a;b")"; rc=$?
ok "an unsafe node name is skipped, the rest still runs" \
  '[ "$rc" = 0 ] && grep -qx "SPANS_VIEW OK alpha" <<<"$out" && [ "$(wc -l < "$CALLS")" = 1 ] && grep -q "skipping unsafe node name" "$TMP/stderr"'
out="$(cd "$DIR" && run "alpha *")"; rc=$?
ok "a wildcard in the node list is refused as a name, never expanded to file names" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$CALLS")" = 1 ] && grep -q "skipping unsafe node name" "$TMP/stderr"'
out="$(run "alpha" --date 2027-11-23)"; rc=$?
ok "a day later than yesterday is refused by the builder (exit 2) and the pages stay" \
  '[ "$rc" = 2 ] && tail -1 <<<"$out" | jq -e ".ok == false" >/dev/null && [ -s "$OUT/$DAY.html" ]'
out="$(CCC_FLEET_SSH="$TMP/bin/ssh" CCC_FLEET_NODES=alpha CCC_STATE_DIR="$HUB" bash "$C" --date "$DAY" 2>/dev/null)"; rc=$?
ok "--out-dir is required" '[ "$rc" = 2 ]'
out="$(CCC_FLEET_SSH="$TMP/bin/ssh" CCC_FLEET_NODES='' CCC_STATE_DIR="$HUB" bash "$C" --out-dir "$OUT" --date "$DAY" 2>"$TMP/stderr")"; rc=$?
ok "there is no built-in node list: CCC_FLEET_NODES is required" '[ "$rc" = 2 ] && grep -q "CCC_FLEET_NODES is required" "$TMP/stderr"'

# 6. --collect-only stages the answers and builds nothing
rm -rf "$OUT"
stage="$(run "alpha down" --collect-only)"; rc=$?
ok "--collect-only prints the staging folder and writes no page" \
  '[ "$rc" = 0 ] && [ -d "$stage" ] && [ ! -e "$OUT" ] && grep -qx "transport=ok rc=0" "$stage/alpha.meta" && grep -qx "transport=unreachable rc=255" "$stage/down.meta"'
ok "the staging folder is private" '[ "$(stat -c %a "$stage")" = 700 ] && [ "$(stat -c %a "$stage/alpha.out")" = 600 ]'

# 7. default day is yesterday in Korea time
out="$(CCC_FLEET_SSH="$TMP/bin/ssh" CCC_FLEET_NODES=down CCC_FLEET_SELF=_none_ CCC_STATE_DIR="$HUB" CCC_SPANS_VIEW_RETRY_DELAY=0 bash "$C" --out-dir "$OUT" 2>/dev/null)"
want="$(TZ=Asia/Seoul date -d yesterday +%F)"
ok "without --date the run is for yesterday (KST)" 'tail -1 <<<"$out" | jq -e --arg d "$want" ".date == \$d" >/dev/null && [ -s "$OUT/$want.html" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
