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

# ---- T9: dual-broker plan (#2024) — broker passthrough, dedup, counts ------

cat > "$FIX/workers-dual.json" <<'EOF'
[
  {"node": "alpha", "provider": "claude", "model": "m", "online": true,  "broker": "primary"},
  {"node": "beta",  "provider": "xai",    "model": "xai/grok", "online": true, "broker": "t2"},
  {"node": "alpha", "provider": "claude", "model": "m-dup", "online": false, "broker": "t2"},
  {"node": "gamma", "provider": "codex",  "model": "openai-codex/gpt", "online": true, "broker": "t2"}
]
EOF
run_plan "$FIX/cases3.json" "$FIX/workers-dual.json" "$FIX/plan-dual.json" ""
ok "T9 dual assigns all 3 cases" "$ASSERT $FIX/plan-dual.json 'd[\"count\"] == 3'"
ok "T9 broker recorded on every assignment" \
  "$ASSERT $FIX/plan-dual.json 'all(\"broker\" in a for a in d[\"assignments\"])'"
ok "T9 broker_counts sum == count" \
  "$ASSERT $FIX/plan-dual.json 'sum(d[\"broker_counts\"].values()) == d[\"count\"]'"
ok "T9 t2-only worker reachable via t2 broker" \
  "$ASSERT $FIX/plan-dual.json 'any(a[\"reviewer\"] == \"gamma\" and a[\"broker\"] == \"t2\" for a in d[\"assignments\"])'"

python3 - "$TOOL" > "$FIX/norm.json" <<'EOF'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("rot", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
workers = mod._normalize_workers([
    {"node": "alpha", "provider": "claude", "model": "m", "online": True, "broker": "primary"},
    {"node": "alpha", "provider": "claude", "model": "m-dup", "online": False, "broker": "t2"},
    {"node": "zeta", "provider": "x", "model": "x", "online": False, "broker": "t9"},
    {"node": "zeta", "provider": "x", "model": "x2", "online": True, "broker": "t2"},
    {"node": "legacy", "provider": "claude", "model": "m", "online": True},
])
print(json.dumps(workers))
EOF
ok "T9 normalize dedups (alpha=primary, zeta=t2 OR-online, legacy=primary-default)" \
  "$ASSERT $FIX/norm.json 'len(d) == 3 and [w for w in d if w[\"node\"] == \"alpha\"][0][\"broker\"] == \"primary\" and [w for w in d if w[\"node\"] == \"alpha\"][0][\"online\"] is True and [w for w in d if w[\"node\"] == \"zeta\"][0][\"broker\"] == \"t2\" and [w for w in d if w[\"node\"] == \"zeta\"][0][\"online\"] is True and [w for w in d if w[\"node\"] == \"legacy\"][0][\"broker\"] == \"primary\"'"

python3 - "$FIX/plan-dual.json" > "$FIX/alpha-brokers.txt" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
alpha_brokers = {a["broker"] for a in d["assignments"] if a["reviewer"] == "alpha"}
print(",".join(sorted(alpha_brokers)) or "none")
EOF
ok "T9 duplicate alpha assignment-side stays primary-only" "grep -qv t2 $FIX/alpha-brokers.txt"

# ---- T10: registry read is fail-closed, secret-free (#2024) ----------------

mkdir -p "$TMP/claude/hooks"
cat > "$TMP/claude/hooks/ccc-skill-promotion.py" <<'PYEOF'
import json, re
def _parse_remote_brokers(raw):
    if not raw or not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    brokers, seen = [], set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        values = [entry.get(k) for k in ("name", "ssh_host", "broker_url", "nexus_dir", "secret_cmd")]
        if not all(isinstance(v, str) and v.strip() for v in values):
            continue
        if not re.match(r"^https?://", entry["broker_url"]):
            continue
        name = entry["name"].strip()
        if name in seen or name == "primary":
            continue
        seen.add(name)
        brokers.append({k: entry[k].strip() for k in ("name", "ssh_host", "broker_url", "nexus_dir", "secret_cmd")})
    return tuple(brokers)
PYEOF

mk_reg_case() { # $1 out.json $2 registry-value (single-quoted into env file)
  : > "$TMP/reg.env"
  printf "export CCC_SKILL_PROMOTION_REMOTE_BROKERS='%s'\n" "$2" >> "$TMP/reg.env"
  CCC_CLAUDE_DIR="$TMP/claude" python3 - "$TOOL" > "$1" <<EOF
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("rot", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
brokers, error = mod._remote_broker_registry("$TMP/reg.env")
print(json.dumps({"count": len(brokers), "names": [b["name"] for b in brokers], "error": error}))
EOF
}

VALID='[{"name":"t2","ssh_host":"gwakga","broker_url":"http://127.0.0.1:8787","nexus_dir":"/root/work/a2a/a2a-nexus","secret_cmd":"cat /x | cut -d= -f2"}]'
mk_reg_case "$FIX/reg-valid.json" "$VALID"
ok "T10 valid registry parsed" "$ASSERT $FIX/reg-valid.json 'd[\"count\"] == 1 and d[\"names\"] == [\"t2\"] and d[\"error\"] is None'"
ok "T10 registry carries no secret material" \
  "! grep -qE 'SECRET=[A-Za-z0-9+/=_-]{8,}' $FIX/reg-valid.json"
mk_reg_case "$FIX/reg-empty.json" ""
ok "T10 absent registry = primary-only, no error" "$ASSERT $FIX/reg-empty.json 'd[\"count\"] == 0 and d[\"error\"] is None'"
mk_reg_case "$FIX/reg-invalid.json" 'not-json-at-all'
ok "T10 invalid registry dropped fail-closed" "$ASSERT $FIX/reg-invalid.json 'd[\"count\"] == 0'"
: > "$TMP/reg-broken.env"
printf "this is 'not shell-safe\n" >> "$TMP/reg-broken.env"
CCC_CLAUDE_DIR="$TMP/claude" python3 - "$TOOL" > "$FIX/reg-broken.json" <<EOF
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("rot", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
brokers, error = mod._remote_broker_registry("$TMP/reg-broken.env")
print(json.dumps({"count": len(brokers), "error": error}))
EOF
ok "T10 unreadable env file degrades to primary-only" "$ASSERT $FIX/reg-broken.json 'd[\"count\"] == 0 and d[\"error\"] == \"env-file-unreadable\"'"

# ---- summary ----------------------------------------------------------------

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
