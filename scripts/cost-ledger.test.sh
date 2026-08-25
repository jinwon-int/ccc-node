#!/usr/bin/env bash
# Hermetic tests for cost-ledger.py and install-cost-ledger-cron.sh (#1205).
#
# Fixtures are synthetic transcripts, so every assertion is about the ledger's
# arithmetic and its refusals — not about whatever this host happens to have in
# ~/.claude/projects.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LEDGER="$ROOT/scripts/cost-ledger.py"
INSTALLER="$ROOT/scripts/install-cost-ledger-cron.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccc-cost-ledger-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

# --- transcript fixture helpers -------------------------------------------
# One assistant record. The CLI emits one JSONL line per content block with the
# same usage object, which is why the ledger dedupes on (message.id, requestId);
# emit() takes both so a test can replay a line deliberately.
emit() { # <file> <ts> <msgid> <reqid> <model> <usage-json>
  printf '{"type":"assistant","timestamp":"%s","requestId":"%s","sessionId":"s1","message":{"id":"%s","model":"%s","usage":%s}}\n' \
    "$2" "$4" "$3" "$5" "$6" >> "$1"
}

proj="$TMP/projects/-root"; mkdir -p "$proj"
T="$proj/a.jsonl"

# 2026-08-20 KST == 2026-08-19T15:00Z .. 2026-08-20T15:00Z
IN_DAY="2026-08-20T01:00:00.000Z"
BEFORE="2026-08-19T14:59:00.000Z"

# Opus 5: input 1M, output 1M, cache_read 1M, 5m-write 1M, 1h-write 1M.
# At the published rates that is 5 + 25 + 0.5 + 6.25 + 10 = 46.75 USD, and the
# 5m/1h split is the whole point: pricing both writes at the 5m rate would give
# 42.50 and pricing both at 1h would give 50.50.
emit "$T" "$IN_DAY" m1 r1 claude-opus-5 \
  '{"input_tokens":1000000,"output_tokens":1000000,"cache_read_input_tokens":1000000,"cache_creation_input_tokens":2000000,"cache_creation":{"ephemeral_5m_input_tokens":1000000,"ephemeral_1h_input_tokens":1000000}}'
# same API response repeated as a second content block — must not double-count
emit "$T" "$IN_DAY" m1 r1 claude-opus-5 \
  '{"input_tokens":1000000,"output_tokens":1000000,"cache_read_input_tokens":1000000,"cache_creation_input_tokens":2000000,"cache_creation":{"ephemeral_5m_input_tokens":1000000,"ephemeral_1h_input_tokens":1000000}}'
# outside the KST day — must be excluded
emit "$T" "$BEFORE" m0 r0 claude-opus-5 \
  '{"input_tokens":9000000,"output_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}'

# Stage 2 (#1205): the codex/piri collectors default to real home dirs, so the
# harness pins them to hermetic empties — assertions stay about fixtures.
codex_root="$TMP/codex-sessions"; piri_root="$TMP/piri-sessions"
mkdir -p "$codex_root" "$piri_root"
run() { CCC_NODE=testnode python3 "$LEDGER" --projects "$TMP/projects" --codex-sessions "$codex_root" --piri-sessions "$piri_root" --date 2026-08-20 "$@"; }

out="$(run --dry-run 2>&1)"; rc=$?
ok "dry-run exits 0" '[ "$rc" = 0 ]'
json="$(printf '%s' "$out" | sed -n '/^{/,$p')"

ok "dedupes repeated content-block lines (one turn, not two)" \
  '[ "$(jq -r ".models[\"claude-opus-5\"].turns" <<<"$json")" = 1 ]'
ok "excludes records outside the KST day" \
  '[ "$(jq -r ".models[\"claude-opus-5\"].input_tokens" <<<"$json")" = 1000000 ]'
ok "splits cache writes by TTL" \
  '[ "$(jq -r ".models[\"claude-opus-5\"].cache_write_5m_tokens" <<<"$json")" = 1000000 ] \
   && [ "$(jq -r ".models[\"claude-opus-5\"].cache_write_1h_tokens" <<<"$json")" = 1000000 ]'
# The distinguishing number: 46.75 only comes out if 1h writes are billed at 2x
# base input while 5m writes are billed at 1.25x.
ok "prices each cache-write TTL at its own published rate" \
  '[ "$(jq -r ".models[\"claude-opus-5\"].est_cost_usd" <<<"$json")" = "46.75" ]'
ok "reports the node identity from CCC_NODE" \
  '[ "$(jq -r ".node" <<<"$json")" = "testnode" ]'

# --- a model with no published price is null, never guessed ----------------
p2="$TMP/projects2/-root"; mkdir -p "$p2"
emit "$p2/a.jsonl" "$IN_DAY" m2 r2 kimi-coding/k3 \
  '{"input_tokens":1000,"output_tokens":1000,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}'
json2="$(CCC_NODE=testnode python3 "$LEDGER" --projects "$TMP/projects2" --date 2026-08-20 --dry-run 2>&1 | sed -n '/^{/,$p')"
ok "an unpriced model records tokens but a null cost" \
  '[ "$(jq -r ".models[\"kimi-coding/k3\"].input_tokens" <<<"$json2")" = 1000 ] \
   && [ "$(jq -r ".models[\"kimi-coding/k3\"].est_cost_usd" <<<"$json2")" = "null" ]'
ok "the null cost says why" \
  '[ "$(jq -r ".models[\"kimi-coding/k3\"].est_cost_null_reason" <<<"$json2")" = "no-published-price" ]'
ok "totals refuse a partial sum when any model is unpriced" \
  '[ "$(jq -r ".totals.est_cost_usd" <<<"$json2")" = "null" ]'

# --- an unpriced usage modifier is refused, not silently standard-priced ---
p3="$TMP/projects3/-root"; mkdir -p "$p3"
emit "$p3/a.jsonl" "$IN_DAY" m3 r3 claude-opus-5 \
  '{"input_tokens":1000000,"output_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"inference_geo":"us"}'
json3="$(CCC_NODE=testnode python3 "$LEDGER" --projects "$TMP/projects3" --date 2026-08-20 --dry-run 2>&1 | sed -n '/^{/,$p')"
ok "US data residency (1.1x) is refused rather than priced as global" \
  '[ "$(jq -r ".models[\"claude-opus-5\"].est_cost_usd" <<<"$json3")" = "null" ]'
ok "the refusal names the modifier" \
  'jq -e ".models[\"claude-opus-5\"].usage_modifiers | index(\"inference_geo=us\")" <<<"$json3" >/dev/null'

# --- a node with no transcripts is a normal state, not a failure ----------
out4="$(CCC_NODE=testnode python3 "$LEDGER" --projects "$TMP/does-not-exist" --codex-sessions "$TMP/also-missing" --piri-sessions "$TMP/also-missing" --date 2026-08-20 2>&1)"; rc4=$?
ok "a node without transcripts exits 0 (daegyo/gongmyoung are legitimate)" '[ "$rc4" = 0 ]'
ok "and says it skipped rather than writing a zero row" \
  'jq -e ".ok == true and .skipped == \"no-transcripts\"" <<<"$out4" >/dev/null'

# --- ledger upsert is idempotent per (date, node) --------------------------
LED="$TMP/ledger.jsonl"
run --out "$LED" >/dev/null 2>&1
run --out "$LED" >/dev/null 2>&1
ok "re-running the same day replaces its row instead of appending" \
  '[ "$(wc -l < "$LED" | tr -d " ")" = 1 ]'
CCC_NODE=othernode python3 "$LEDGER" --projects "$TMP/projects" --date 2026-08-20 --out "$LED" >/dev/null 2>&1
ok "a different node appends its own row for the same day" \
  '[ "$(wc -l < "$LED" | tr -d " ")" = 2 ]'

# --- node identity falls back to state/node.txt when CCC_NODE is unset -----
mkdir -p "$TMP/state"; printf 'from-node-txt\n' > "$TMP/state/node.txt"
json5="$(env -u CCC_NODE CCC_STATE_DIR="$TMP/state" python3 "$LEDGER" --projects "$TMP/projects" --date 2026-08-20 --dry-run 2>&1 | sed -n '/^{/,$p')"
ok "node.txt is used when CCC_NODE is unset" \
  '[ "$(jq -r ".node" <<<"$json5")" = "from-node-txt" ]'

# --- installer -------------------------------------------------------------
export FAKE_CRON="$TMP/crontab.txt"; : > "$FAKE_CRON"
STUB="$TMP/crontab-stub.sh"; BASH_BIN="$(command -v bash)"
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
export CCC_CLAUDE_DIR="$TMP/claude"; export CCC_STATE_DIR="$CCC_CLAUDE_DIR/state"
mkdir -p "$CCC_STATE_DIR"

marker_count() { grep -cE "# ccc-node:cost-ledger gen=h_" "$FAKE_CRON" 2>/dev/null | head -1; }

out="$(bash "$INSTALLER" --dry-run 2>&1)"; rc=$?
ok "installer dry-run exits 0 and writes nothing" '[ "$rc" = 0 ] && [ "$(marker_count)" = 0 ]'
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "apply installs exactly one managed entry" '[ "$(marker_count)" = 1 ]'
ok "apply wraps it in a BEGIN/END block" \
  'grep -qF "# ccc-node:cost-ledger:begin" "$FAKE_CRON" && grep -qF "# ccc-node:cost-ledger:end" "$FAKE_CRON"'
ok "entry loads the login PATH (python3 is not on a bare cron PATH under Termux)" \
  'grep -qF "bash -lc" "$FAKE_CRON"'
# The node name resolves inside the cron shell, which exports neither CCC_NODE
# nor CCC_STATE_DIR — carrying it in the line is what keeps a node.txt-only
# node from being recorded under its hostname.
ok "entry carries CCC_STATE_DIR so node identity resolves under cron" \
  'grep -qF "CCC_STATE_DIR=" "$FAKE_CRON"'
# shellcheck source=/dev/null
. "$ROOT/scripts/lib/installer-gen-stamp.sh"
want_gen="$(ccc_installer_gen_stamp_auto "$INSTALLER")"
ok "entry carries the gen stamp of the installer content" 'grep -qF "gen=$want_gen" "$FAKE_CRON"'
ok "BEGIN/END markers stay unstamped (exact-match parsed)" \
  '! grep -qE "cost-ledger:(begin|end) gen=" "$FAKE_CRON"'
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "re-apply is idempotent" '[ "$(marker_count)" = 1 ]'
bash "$INSTALLER" --apply --remove >/dev/null 2>&1
ok "remove takes the entry back out" '[ "$(marker_count)" = 0 ]'

# --- weekly rollup entry (D-4): opt-in --weekly adds a second managed line --
bash "$INSTALLER" --apply --weekly >/dev/null 2>&1
ok "--weekly adds the Monday rollup line inside the same managed block" \
  '[ "$(marker_count)" = 2 ] && grep -qF "cost-ledger-weekly.py" "$FAKE_CRON" && grep -qF "17 5 * * 1" "$FAKE_CRON"'
ok "weekly line carries CCC_STATE_DIR and the gen stamp" \
  'grep "cost-ledger-weekly.py" "$FAKE_CRON" | grep -qF "CCC_STATE_DIR=" && grep "cost-ledger-weekly.py" "$FAKE_CRON" | grep -qE "gen=h_[0-9a-f]{12}$"'
# Regression (#1264 defect class): the record argv must materialize --weekly,
# or the first gen-drift self-update replay rebuilds the block with only the
# daily line and the Monday rollup silently vanishes.
REC="$CCC_STATE_DIR/install-cost-ledger-cron.json"
ok "--weekly is materialized in the install record" \
  'jq -e ".argv | index(\"--weekly\")" "$REC" >/dev/null 2>&1'
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "re-apply without --weekly strips the rollup line (opt-in is not sticky)" \
  '[ "$(marker_count)" = 1 ] && ! grep -qF "cost-ledger-weekly.py" "$FAKE_CRON"'
ok "record argv drops --weekly when reinstalled without it" \
  '! jq -e ".argv | index(\"--weekly\")" "$REC" >/dev/null 2>&1'
bash "$INSTALLER" --apply --remove >/dev/null 2>&1

# --- stage 2: codex collector (#1205 D-1) -----------------------------------
# turn_context carries the model; token_count events carry total (cumulative)
# AND last (per-turn delta) — the ledger must use `last`.
cx="$codex_root/2026/08/20"; mkdir -p "$cx"
cat > "$cx/rollout-2026-08-20T01-00-00-test.jsonl" <<'EOF'
{"timestamp":"2026-08-20T00:55:00.000Z","type":"turn_context","payload":{"turn_id":"t1","model":"gpt-5.6-sol"}}
{"timestamp":"2026-08-20T01:00:00.000Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":17598,"cached_input_tokens":4864,"cache_write_input_tokens":100,"output_tokens":162,"reasoning_output_tokens":71,"total_tokens":17760},"last_token_usage":{"input_tokens":17598,"cached_input_tokens":4864,"cache_write_input_tokens":100,"output_tokens":162,"reasoning_output_tokens":71,"total_tokens":17760},"model_context_window":258400}}}
{"timestamp":"2026-08-20T02:00:00.000Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":30000,"cached_input_tokens":4864,"cache_write_input_tokens":250,"output_tokens":462,"reasoning_output_tokens":71,"total_tokens":30462},"last_token_usage":{"input_tokens":12402,"cached_input_tokens":0,"cache_write_input_tokens":150,"output_tokens":300,"reasoning_output_tokens":0,"total_tokens":12702},"model_context_window":258400}}}
{"timestamp":"2026-08-19T14:00:00.000Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{},"last_token_usage":{"input_tokens":999999,"output_tokens":0},"model_context_window":258400}}}
EOF
out="$(run --dry-run 2>&1)"; rc=$?
json="$(printf '%s' "$out" | sed -n '/^{/,$p')"
ok "codex: uses last_token_usage deltas, not the cumulative total" \
  '[ "$rc" = 0 ] && [ "$(jq -r ".models[\"codex:gpt-5.6-sol\"].input_tokens" <<<"$json")" = 30000 ]'
ok "codex: cache_write lands in the untyped bucket (no TTL split exists)" \
  '[ "$(jq -r ".models[\"codex:gpt-5.6-sol\"].cache_write_untyped_tokens" <<<"$json")" = 250 ]'
ok "codex: reasoning counts as thinking tokens" \
  '[ "$(jq -r ".models[\"codex:gpt-5.6-sol\"].thinking_tokens" <<<"$json")" = 71 ]'
ok "codex: model prices at the official OpenAI short-context rate (read 2026-08-21)" \
  '[ "$(jq -r ".models[\"codex:gpt-5.6-sol\"].est_cost_usd" <<<"$json")" = 0.167854 ]'
ok "codex: row is provider-tagged" \
  '[ "$(jq -r ".models[\"codex:gpt-5.6-sol\"].provider" <<<"$json")" = codex ]'

# --- stage 2: piri collector (#1205 D-2) ------------------------------------
cat > "$piri_root/2026-08-20T01-00-00-000Z_sess1.jsonl" <<'EOF'
{"type":"message","id":"m1","parentId":"m0","timestamp":"2026-08-20T01:05:00.000Z","message":{"role":"assistant","model":"k3","usage":{"input":703,"output":153,"cacheRead":274176,"cacheWrite":0,"totalTokens":275032}}}
{"type":"message","id":"m2","parentId":"m1","timestamp":"2026-08-20T01:06:00.000Z","message":{"role":"assistant","model":"k3","usage":{"input":800,"output":50,"cacheRead":1000,"cacheWrite":40,"totalTokens":1890}}}
{"type":"message","id":"m2","parentId":"m1","timestamp":"2026-08-20T01:06:00.000Z","message":{"role":"assistant","model":"k3","usage":{"input":800,"output":50,"cacheRead":1000,"cacheWrite":40,"totalTokens":1890}}}
{"type":"message","id":"u1","timestamp":"2026-08-20T01:04:00.000Z","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}
EOF
out="$(run --dry-run 2>&1)"; rc=$?
json="$(printf '%s' "$out" | sed -n '/^{/,$p')"
ok "piri: sums per-message deltas with dedup by (file,id)" \
  '[ "$(jq -r ".models[\"piri:k3\"].input_tokens" <<<"$json")" = 1503 ] && [ "$(jq -r ".models[\"piri:k3\"].turns" <<<"$json")" = 2 ]'
ok "piri: cacheRead folds into cache_read" \
  '[ "$(jq -r ".models[\"piri:k3\"].cache_read_input_tokens" <<<"$json")" = 275176 ]'
ok "piri: cacheWrite lands in untyped bucket, priced at the official Kimi K3 rate" \
  '[ "$(jq -r ".models[\"piri:k3\"].cache_write_untyped_tokens" <<<"$json")" = 40 ] && [ "$(jq -r ".models[\"piri:k3\"].est_cost_usd" <<<"$json")" = 0.090107 ]'
ok "piri: row is provider-tagged" \
  '[ "$(jq -r ".models[\"piri:k3\"].provider" <<<"$json")" = piri ]'
ok "record source flips to multi-provider-transcripts with several collectors" \
  '[ "$(jq -r ".source" <<<"$json")" = "multi-provider-transcripts" ]'

# --- stage 2: --providers restricts collectors -------------------------------
out="$(run --providers claude --dry-run 2>&1)"; rc=$?
json="$(printf '%s' "$out" | sed -n '/^{/,$p')"
ok "--providers claude excludes codex/piri collectors" \
  '[ "$(jq -r ".models | keys | length" <<<"$json")" = 1 ] && [ "$(jq -r ".source" <<<"$json")" = "claude-projects-transcripts" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
