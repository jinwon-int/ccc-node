#!/usr/bin/env bash
# Hermetic tests for skills/fleet-diagram/scripts/render_diagram.py (#2109 B).
# Covers the three spec kinds, SVG content, PNG when Pillow is importable
# (skipped otherwise), fail-closed exits on bad specs, name sanitisation and
# the --format contract. No network, private output dir.
# shellcheck disable=SC2034  # test variables are read inside the eval'd ok() conditions
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
R="$DIR/render_diagram.py"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
export HOME="$TMP/home"
mkdir -p "$HOME"
has_pil=0
python3 -c "import PIL" 2>/dev/null && has_pil=1

cat > "$TMP/matrix.json" <<'EOF'
{"kind":"matrix","title":"매트릭스 <t>","rows":["node-a","node-b"],"cols":["harness","bridge"],
 "cells":[["ok","warn"],["fail","n/a"]],"legend":{"ok":"green","warn":"amber","fail":"red","n/a":"grey"}}
EOF
cat > "$TMP/timeline.json" <<'EOF'
{"kind":"timeline","title":"타임라인","events":[{"t":"13:40","label":"second","lane":"b","status":"fail"},{"t":"13:25","label":"first & last","lane":"a","status":"ok"}]}
EOF
cat > "$TMP/dag.json" <<'EOF'
{"kind":"dag","title":"의존","nodes":[{"id":"a","label":"#1","status":"ok"},{"id":"b","label":"#2"},{"id":"c","label":"#3","status":"pending"}],"edges":[["a","b"],["a","c"],["b","c"]]}
EOF

# 1. matrix → SVG always; JSON result line
out="$(python3 "$R" --spec "$TMP/matrix.json" --out "$TMP/out" --name "my matrix/1" --format svg)"; rc=$?
ok "matrix svg renders with exit 0" '[ "$rc" = 0 ] && printf "%s" "$out" | jq -e ".ok == true and .kind == \"matrix\" and .png == false" >/dev/null'
svg="$TMP/out/my-matrix-1.svg"
ok "output name is sanitised (slash/space → dash)" '[ -f "$svg" ]'
ok "svg escapes labels and carries the legend colours" 'grep -q "매트릭스 &lt;t&gt;" "$svg" && grep -q "#2e9e5b" "$svg" && grep -q "#d9a400" "$svg" && grep -q "#c93b3b" "$svg"'
ok "svg has one cell rect per matrix cell (4) plus legend swatches (4)" '[ "$(grep -c "<rect " "$svg")" = 9 ]'

# 2. timeline sorts by t and escapes
out="$(python3 "$R" --spec "$TMP/timeline.json" --out "$TMP/out" --name tl --format svg)"
ok "timeline renders" 'printf "%s" "$out" | jq -e ".ok == true and .kind == \"timeline\"" >/dev/null'
ok "timeline events are sorted by t and ampersand is escaped" 'grep -n "13:25" "$TMP/out/tl.svg" | head -1 | cut -d: -f1 | xargs -I{} test {} -lt "$(grep -n "13:40" "$TMP/out/tl.svg" | head -1 | cut -d: -f1)" && grep -q "first &amp; last" "$TMP/out/tl.svg"'

# 3. dag layers and arrows
out="$(python3 "$R" --spec "$TMP/dag.json" --out "$TMP/out" --name dag --format svg)"
ok "dag renders" 'printf "%s" "$out" | jq -e ".ok == true and .kind == \"dag\"" >/dev/null'
ok "dag draws one arrowed line per edge (3)" '[ "$(grep -c "marker-end" "$TMP/out/dag.svg")" = 3 ]'

# 4. PNG when Pillow exists; honest otherwise
out="$(python3 "$R" --spec "$TMP/dag.json" --out "$TMP/out/dag.png")"; rc=$?
if [ "$has_pil" = 1 ]; then
  ok "png + svg produced with --out <file.png> (Pillow present)" '[ "$rc" = 0 ] && [ -s "$TMP/out/dag.png" ] && printf "%s" "$out" | jq -e ".png == true and (.files[0]|endswith(\".png\"))" >/dev/null'
  ok "png is a real PNG" 'head -c 8 "$TMP/out/dag.png" | od -An -c | tr -d " " | grep -q "211PNG"'
else
  ok "without Pillow the default format still succeeds with svg only and a reason" '[ "$rc" = 0 ] && printf "%s" "$out" | jq -e ".png == false and (.reason|length>0)" >/dev/null'
  python3 "$R" --spec "$TMP/dag.json" --out "$TMP/out" --format png >/dev/null; rc=$?
  ok "--format png fails closed without Pillow (exit 4)" '[ "$rc" = 4 ]'
fi

# 5. bad specs fail closed with exit 2 and a body-free error
echo '{"kind":"dag","nodes":[{"id":"a"},{"id":"b"}],"edges":[["a","b"],["b","a"]]}' > "$TMP/cyc.json"
out="$(python3 "$R" --spec "$TMP/cyc.json" --out "$TMP/out")"; rc=$?
ok "a cyclic dag is rejected" '[ "$rc" = 2 ] && printf "%s" "$out" | jq -e ".ok == false and (.error|test(\"cycle\"))" >/dev/null'
echo '{"kind":"matrix","rows":["a"],"cols":["x","y"],"cells":[["ok"]]}' > "$TMP/shape.json"
out="$(python3 "$R" --spec "$TMP/shape.json" --out "$TMP/out")"; rc=$?
ok "a rows x cols shape mismatch is rejected" '[ "$rc" = 2 ] && printf "%s" "$out" | jq -e ".error|test(\"rows x cols\")" >/dev/null'
echo '{"kind":"pie"}' > "$TMP/kind.json"
out="$(python3 "$R" --spec "$TMP/kind.json" --out "$TMP/out")"; rc=$?
ok "an unknown kind is rejected" '[ "$rc" = 2 ]'
echo 'not json' > "$TMP/bad.json"
out="$(python3 "$R" --spec "$TMP/bad.json" --out "$TMP/out")"; rc=$?
ok "non-JSON input is rejected without a traceback" '[ "$rc" = 2 ] && printf "%s" "$out" | jq -e ".ok == false" >/dev/null'
python3 - "$TMP/big.json" <<'PY'
import json, sys
json.dump({"kind": "dag", "nodes": [{"id": str(i)} for i in range(300)], "edges": []}, open(sys.argv[1], "w"))
PY
out="$(python3 "$R" --spec "$TMP/big.json" --out "$TMP/out")"; rc=$?
ok "oversized inputs are rejected (max-nodes)" '[ "$rc" = 2 ] && printf "%s" "$out" | jq -e ".error|test(\"too many\")" >/dev/null'

# 6. labels are truncated, never dropped
python3 - "$TMP/long.json" <<'PY'
import json, sys
json.dump({"kind": "matrix", "rows": ["r" * 200], "cols": ["c"], "cells": [["ok"]]}, open(sys.argv[1], "w"))
PY
python3 "$R" --spec "$TMP/long.json" --out "$TMP/out" --name long --format svg >/dev/null
ok "a 200-char label is truncated with an ellipsis" 'grep -q "rrrr…" "$TMP/out/long.svg" && ! grep -q "$(printf "r%.0s" $(seq 1 100))" "$TMP/out/long.svg"'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
