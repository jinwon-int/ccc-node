#!/usr/bin/env bash
# a2a-rescreen-rotation.test.sh — deterministic planning tests for the
# provider-aware rescreen rotation planner (a2a-nexus#2028).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL="$HERE/a2a-rescreen-rotation.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0
fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

# assert.py PLAN 'python bool expr over d' — test-only assertion helper
cat > "$TMP/assert.py" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
sys.exit(0 if eval(sys.argv[2], {"d": d}) else 1)
PYEOF
ASSERT="python3 $TMP/assert.py"

NOW="2026-08-31T00:00:00Z"
FIX="$TMP/fix"
mkdir -p "$FIX"

# ---- fixtures -------------------------------------------------------------

cat > "$FIX/workers3.json" <<'EOF'
[
  {"node": "alpha", "provider": "claude", "model": "claude-host-default", "online": true},
  {"node": "beta",  "provider": "claude", "model": "claude-host-default", "online": true},
  {"node": "gamma", "provider": "xai",    "model": "xai/grok-4.6",       "online": true}
]
EOF

cat > "$FIX/cases3.json" <<'EOF'
[
  {"name": "s-one",   "pr": 1, "branch": "intake/x/one",   "author_node": "gongmyoung"},
  {"name": "s-two",   "pr": 2, "branch": "intake/x/two",   "author_node": "gongmyoung"},
  {"name": "s-three", "pr": 3, "branch": "intake/x/three", "author_node": "gongmyoung"}
]
EOF

cat > "$FIX/cases10.json" <<'EOF'
[
  {"name": "bridge-detached-self-restart", "pr": 34, "branch": "b34", "author_node": "sogyo"},
  {"name": "bridge-yield-continue",        "pr": 48, "branch": "b48", "author_node": "yukson"},
  {"name": "ccc-agent-cron",               "pr": 61, "branch": "b61", "author_node": "nosuk"},
  {"name": "ccc-node-status",              "pr": 63, "branch": "b63", "author_node": "nosuk"},
  {"name": "gh-ci-wait",                   "pr": 53, "branch": "b53", "author_node": "yukson"},
  {"name": "gh-pr-flow",                   "pr": 66, "branch": "b66", "author_node": "sogyo"},
  {"name": "github-merge-state-conflict",  "pr": 41, "branch": "b41", "author_node": "dungae"},
  {"name": "scheduled-verification",       "pr": 56, "branch": "b56", "author_node": "yukson"},
  {"name": "self-update",                  "pr": 46, "branch": "b46", "author_node": "soonwook"},
  {"name": "web",                          "pr": 67, "branch": "b67", "author_node": "gongmyoung"}
]
EOF

cat > "$FIX/workers10.json" <<'EOF'
[
  {"node": "bangtong", "provider": "claude", "model": "claude-host-default", "online": true},
  {"node": "nosuk",    "provider": "claude", "model": "claude-host-default", "online": true},
  {"node": "yukson",   "provider": "claude", "model": "claude-host-default", "online": true}
]
EOF

run_plan() { # $1 cases $2 workers $3 out $4 extra-args
  python3 "$TOOL" plan --cases "$1" --workers "$2" --now "$NOW" --out "$3" $4 > /dev/null 2>&1
}

# ---- T1: provider-balanced deterministic pick ------------------------------

run_plan "$FIX/cases3.json" "$FIX/workers3.json" "$FIX/plan3.json" ""
ok "T1 plan exits ok"       "$ASSERT $FIX/plan3.json 'd[\"ok\"] is True'"
ok "T1 assigns all 3 cases" "$ASSERT $FIX/plan3.json 'd[\"count\"] == 3'"
ok "T1 first pick is alpha (name tie-break)" \
  "$ASSERT $FIX/plan3.json 'd[\"assignments\"][0][\"reviewer\"] == \"alpha\"'"
ok "T1 second pick is gamma (provider balance)" \
  "$ASSERT $FIX/plan3.json 'd[\"assignments\"][1][\"reviewer\"] == \"gamma\"'"
ok "T1 providers balanced (claude=2, xai=1)" \
  "$ASSERT $FIX/plan3.json 'sorted(a[\"provider\"] for a in d[\"assignments\"]) == [\"claude\",\"claude\",\"xai\"]'"

# ---- T2: rescreen-10 replay — determinism + full coverage ------------------

run_plan "$FIX/cases10.json" "$FIX/workers10.json" "$FIX/plan10a.json" ""
run_plan "$FIX/cases10.json" "$FIX/workers10.json" "$FIX/plan10b.json" ""
ok "T2 assigns all 10 cases" "$ASSERT $FIX/plan10a.json 'd[\"count\"] == 10'"
ok "T2 deterministic across runs" "diff -q $FIX/plan10a.json $FIX/plan10b.json >/dev/null"
ok "T2 balanced counts 4/3/3" \
  "$ASSERT $FIX/plan10a.json 'sorted(c for c in {r: [a[\"reviewer\"] for a in d[\"assignments\"]].count(r) for r in set(a[\"reviewer\"] for a in d[\"assignments\"])}.values()) in ([3,3,4],)'"
ok "T2 no author reviews own PR" \
  "$ASSERT $FIX/plan10a.json 'all(a[\"reviewer\"] != a[\"author_node\"] for a in d[\"assignments\"])'"

# ---- T3: author exclusion recorded in rationale -----------------------------

cat > "$FIX/cases-author.json" <<'EOF'
[
  {"name": "s-x", "pr": 9, "branch": "b9", "author_node": "alpha"}
]
EOF
run_plan "$FIX/cases-author.json" "$FIX/workers3.json" "$FIX/plan-author.json" ""
ok "T3 author never assigned own case" \
  "$ASSERT $FIX/plan-author.json 'd[\"assignments\"][0][\"reviewer\"] != \"alpha\"'"
ok "T3 rationale records author-exclusion" \
  "$ASSERT $FIX/plan-author.json 'd[\"assignments\"][0][\"rationale\"][\"excluded\"][\"alpha\"] == \"author-exclusion\"'"

# ---- T4: offline exclusion with reason --------------------------------------

cat > "$FIX/workers-off.json" <<'EOF'
[
  {"node": "alpha", "provider": "claude", "model": "m", "online": true},
  {"node": "beta",  "provider": "claude", "model": "m", "online": false}
]
EOF
run_plan "$FIX/cases3.json" "$FIX/workers-off.json" "$FIX/plan-off.json" ""
ok "T4 offline worker excluded with reason" \
  "$ASSERT $FIX/plan-off.json 'any(x[\"node\"] == \"beta\" and x[\"reason\"] == \"offline\" for x in d[\"excluded_workers\"])'"
ok "T4 offline worker never assigned" \
  "$ASSERT $FIX/plan-off.json 'all(a[\"reviewer\"] != \"beta\" for a in d[\"assignments\"])'"

# ---- T5: failure window excludes in-window only -----------------------------

cat > "$FIX/failures.json" <<'EOF'
[
  {"node": "alpha", "ts": "2026-08-30T23:00:00Z", "reason": "claude-run-failed", "task_id": "t1"},
  {"node": "beta",  "ts": "2026-08-29T00:00:00Z", "reason": "old-failure",       "task_id": "t2"}
]
EOF
run_plan "$FIX/cases3.json" "$FIX/workers3.json" "$FIX/plan-fail.json" "--failures $FIX/failures.json"
ok "T5 in-window failure excluded with reason" \
  "$ASSERT $FIX/plan-fail.json 'any(x[\"node\"] == \"alpha\" and x[\"reason\"].startswith(\"recent-failure:claude-run-failed\") for x in d[\"excluded_workers\"])'"
ok "T5 out-of-window failure not excluded" \
  "$ASSERT $FIX/plan-fail.json 'all(x[\"node\"] != \"beta\" for x in d[\"excluded_workers\"])'"
ok "T5 assignments avoid failed node" \
  "$ASSERT $FIX/plan-fail.json 'all(a[\"reviewer\"] != \"alpha\" for a in d[\"assignments\"])'"

# ---- T6: rotation exhausted → escalation, no silent skip --------------------

cat > "$FIX/workers-one.json" <<'EOF'
[
  {"node": "alpha", "provider": "claude", "model": "m", "online": true}
]
EOF
run_plan "$FIX/cases-author.json" "$FIX/workers-one.json" "$FIX/plan-exh.json" ""
ok "T6 exhausted case lands in unassigned" \
  "$ASSERT $FIX/plan-exh.json 'd[\"unassigned\"][0][\"reason\"] == \"rotation-exhausted\"'"
ok "T6 count reflects zero assignments" "$ASSERT $FIX/plan-exh.json 'd[\"count\"] == 0'"

# ---- T7: start_offset rotates deterministically -----------------------------

run_plan "$FIX/cases3.json" "$FIX/workers3.json" "$FIX/plan-off1.json" "--start-offset 1"
ok "T7 offset rotates first pick to beta" \
  "$ASSERT $FIX/plan-off1.json 'd[\"assignments\"][0][\"reviewer\"] == \"beta\"'"
run_plan "$FIX/cases3.json" "$FIX/workers3.json" "$FIX/plan-off1b.json" "--start-offset 1"
ok "T7 offset stays deterministic" "diff -q $FIX/plan-off1.json $FIX/plan-off1b.json >/dev/null"

# ---- T8: provider parsed from REVIEW_AGENT env strings ----------------------

python3 - "$TOOL" > "$FIX/prov.json" <<'EOF'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("rot", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(json.dumps([
    mod.provider_from_env("", "-p --disallowed-tools *"),
    mod.provider_from_env("/opt/piri/pi-test.sh", "-p --no-tools --model xai/grok-4.6"),
    mod.provider_from_env("pi", "-p --model openai-codex/gpt-5.6-sol"),
]))
EOF
ok "T8 claude default detected" "$ASSERT $FIX/prov.json 'd[0] == [\"claude\", \"claude-host-default\"]'"
ok "T8 xai provider parsed"     "$ASSERT $FIX/prov.json 'd[1] == [\"xai\", \"xai/grok-4.6\"]'"
ok "T8 codex provider parsed"   "$ASSERT $FIX/prov.json 'd[2] == [\"openai-codex\", \"openai-codex/gpt-5.6-sol\"]'"

# ---- summary ----------------------------------------------------------------

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
