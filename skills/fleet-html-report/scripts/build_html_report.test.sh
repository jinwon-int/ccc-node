#!/usr/bin/env bash
# Hermetic tests for skills/fleet-html-report/scripts/build_html_report.py (#2109 C).
# Single-file output, escaping, status chips, the artifacts/ gate, limits, bad specs.
# shellcheck disable=SC2034  # test variables are read inside the eval'd ok() conditions
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
B="$DIR/build_html_report.py"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
export HOME="$TMP/home"
mkdir -p "$HOME"

cat > "$TMP/spec.json" <<'EOF'
{"title":"플릿 <매트릭스>","subtitle":"2026-10-02 · node-b","sections":[
 {"kind":"table","title":"노드별","columns":["node","harness"],"rows":[["node-a","ok"],["node-c","fail"],["x","<script>alert(1)</script>"]]},
 {"kind":"kv","title":"요약","items":[["브로커","a930ce8b"],["상태","warn"]]},
 {"kind":"list","items":["관측 창 18:00 KST","pending"]},
 {"kind":"text","title":"메모","text":"줄1\n줄2 & more"}],
 "footer":"snapshot"}
EOF

# 1. builds one file under artifacts/, JSON result, exit 0
out="$(python3 "$B" --spec "$TMP/spec.json" --out "$HOME/.claude/state/artifacts" --name "my report/1")"; rc=$?
f="$HOME/.claude/state/artifacts/my-report-1.html"
ok "report builds with exit 0 and a JSON result naming the file" '[ "$rc" = 0 ] && printf "%s" "$out" | jq -e ".ok == true and .bytes > 500" >/dev/null && [ -s "$f" ]'
ok "output is a single html document with charset and CSP, no script tags, no external urls" \
  'grep -q "<meta charset=\"utf-8\">" "$f" && grep -q "Content-Security-Policy" "$f" && ! grep -q "<script" "$f" && ! grep -qE "https?://" "$f"'
ok "title and cell values are escaped" 'grep -q "플릿 &lt;매트릭스&gt;" "$f" && grep -q "&lt;script&gt;alert(1)&lt;/script&gt;" "$f"'
ok "status words become colour chips (ok/fail/warn/pending)" '[ "$(grep -o "class=\"chip\"" "$f" | wc -l)" = 4 ]'
ok "text section keeps line breaks (pre-wrap) and escapes ampersand" 'grep -q "줄1" "$f" && grep -q "줄2 &amp; more" "$f"'
ok "footer rendered" 'grep -q "<footer>snapshot</footer>" "$f"'

# 2. --out as an explicit .html path under artifacts/
out="$(python3 "$B" --spec "$TMP/spec.json" --out "$HOME/work/artifacts/nested/r.html")"; rc=$?
ok "explicit .html path under a nested artifacts dir is accepted" '[ "$rc" = 0 ] && [ -s "$HOME/work/artifacts/nested/r.html" ]'

# 3. artifacts/ gate: refuse other locations (the bridge would not send them)
out="$(python3 "$B" --spec "$TMP/spec.json" --out "$HOME/public" --name index)"; rc=$?
ok "a path without an artifacts directory is refused with exit 3 and nothing written" '[ "$rc" = 3 ] && [ ! -e "$HOME/public/index.html" ] && printf "%s" "$out" | jq -e ".ok == false" >/dev/null'

# 4. bad specs → exit 2, body-free error
echo '{"title":"t"}' > "$TMP/nosec.json"
python3 "$B" --spec "$TMP/nosec.json" --out "$HOME/.claude/state/artifacts" >/dev/null; rc=$?
ok "missing sections is rejected" '[ "$rc" = 2 ]'
echo '{"title":"t","sections":[{"kind":"table","columns":["a","b"],"rows":[["x"]]}]}' > "$TMP/shape.json"
out="$(python3 "$B" --spec "$TMP/shape.json" --out "$HOME/.claude/state/artifacts")"; rc=$?
ok "a row/column shape mismatch is rejected" '[ "$rc" = 2 ] && printf "%s" "$out" | jq -e ".error|test(\"one cell per column\")" >/dev/null'
echo '{"title":"t","sections":[{"kind":"chart"}]}' > "$TMP/kind.json"
python3 "$B" --spec "$TMP/kind.json" --out "$HOME/.claude/state/artifacts" >/dev/null; rc=$?
ok "an unknown section kind is rejected" '[ "$rc" = 2 ]'
echo 'nope' > "$TMP/bad.json"
out="$(python3 "$B" --spec "$TMP/bad.json" --out "$HOME/.claude/state/artifacts")"; rc=$?
ok "non-JSON input is rejected without a traceback" '[ "$rc" = 2 ] && printf "%s" "$out" | jq -e ".ok == false" >/dev/null'

# 5. limits: long cells are truncated, oversized tables refused
python3 - "$TMP/long.json" "$TMP/big.json" <<'PY'
import json, sys
json.dump({"title": "t", "sections": [{"kind": "kv", "items": [["k", "v" * 900]]}]}, open(sys.argv[1], "w"))
json.dump({"title": "t", "sections": [{"kind": "table", "columns": ["a"], "rows": [["x"]] * 600}]}, open(sys.argv[2], "w"))
PY
python3 "$B" --spec "$TMP/long.json" --out "$HOME/.claude/state/artifacts" --name long >/dev/null
ok "a 900-char cell is truncated with an ellipsis" 'grep -q "vvv…" "$HOME/.claude/state/artifacts/long.html" && ! grep -q "$(printf "v%.0s" $(seq 1 500))" "$HOME/.claude/state/artifacts/long.html"'
python3 "$B" --spec "$TMP/big.json" --out "$HOME/.claude/state/artifacts" --name big >/dev/null; rc=$?
ok "a 600-row table is refused (exit 2)" '[ "$rc" = 2 ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
