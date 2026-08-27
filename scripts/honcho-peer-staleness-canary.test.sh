#!/usr/bin/env bash
# Tests for honcho-peer-staleness-canary.sh (#1263 follow-up).
# The canary must flag frozen member-peer corpora (2026-08-24 incident:
# dialectic confidently cited 6-week-old facts) while staying silent for
# exempt feeds and hard-failing (exit 2) when the database is unreachable —
# a canary that cannot see must not report "all fresh".
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
G="$HERE/honcho-peer-staleness-canary.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (want rc=$2 got rc=$1)"; fi; }

OLD="$(date -d "30 days ago" +%F)"
RECENT="$(date -d "3 days ago" +%F)"
EDGE="$(date -d "14 days ago" +%F)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ROWS_FILE="$TMP/rows.txt"
FAKE_PSQL="$TMP/fake-psql.sh"
cat > "$FAKE_PSQL" <<'EOF'
#!/usr/bin/env bash
# Fake read-only psql: emits the canned rows file given as $1, ignoring SQL.
set -u
cat "$1"
EOF
chmod +x "$FAKE_PSQL"
run_with_rows() { # $1 = canned psql output
  printf '%s\n' "$1" > "$ROWS_FILE"
  CCC_HONCHO_STALENESS_PSQL_CMD="bash $FAKE_PSQL $ROWS_FILE" \
    bash "$G"
}

# ---- all fresh ---------------------------------------------------------------
out="$(run_with_rows "jingun|$RECENT
daegyo|$RECENT")"; rc=$?
okc "$rc" 0 "fresh peers exit clean"
ok "summary counts checked peers" 'grep -q "checked=2 stale=0" <<<"$out"'

# ---- stale peer flagged, fresh peer still passes ------------------------------
out="$(run_with_rows "jingun|$OLD
daegyo|$RECENT")"; rc=$?
okc "$rc" 1 "stale peer exits 1"
ok "stale line names peer/date/age/limit" "grep -Eq '^STALE peer=jingun last_document=$OLD age_days=[0-9]+ \(limit=14\)$' <<<\"\$out\""
ok "fresh peer not flagged" '! grep -q "STALE peer=daegyo" <<<"$out"'

# ---- boundary: exactly max_age_days is fresh ---------------------------------
out="$(run_with_rows "jingun|$EDGE")"; rc=$?
okc "$rc" 0 "age == limit stays fresh"

# ---- exempt feed ignored even when ancient ------------------------------------
out="$(run_with_rows "family-assistant|$OLD
jingun|$OLD")"; rc=$?
okc "$rc" 1 "exempt feed skipped while real stale still caught"
ok "family-assistant never flagged" "! grep -q 'STALE peer=family-assistant' <<<'$out'"

# ---- unreachable database fails closed ----------------------------------------
CCC_HONCHO_STALENESS_PSQL_CMD="false" bash "$G" >/dev/null 2>&1
okc $? 2 "query failure exits 2"

# ---- malformed row skipped without aborting -----------------------------------
run_with_rows "jingun|not-a-date
daegyo|$RECENT" >/dev/null 2>&1
okc $? 0 "malformed date row skipped, rest checked"

# ---- read-only contract: aggregate dates only, never content ------------------
content_hits="$(grep -cE 'select .*content|\* from documents' "$G" || true)"
okc "$content_hits" 0 "script never selects document content"

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
