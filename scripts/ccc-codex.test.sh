#!/usr/bin/env bash
# Hermetic launch-surface tests for scripts/ccc-codex (#419 Slice B).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHER="$ROOT/scripts/ccc-codex"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

mat="$TMP/materializer"
real="$TMP/real-codex"
cat > "$mat" <<'SH'
#!/usr/bin/env bash
case "${1:-}" in
  materialize)
    printf 'materialize\n' >> "${ORDER_FILE:?}"
    printf 'MATERIALIZER_BODY_SENTINEL\n'
    printf 'MATERIALIZER_ERROR_SENTINEL\n' >&2
    exit "${MAT_RC:-0}"
    ;;
  status)
    printf 'status\n' >> "${ORDER_FILE:?}"
    exit "${STATUS_RC:-1}"
    ;;
  *) exit 64 ;;
esac
SH
cat > "$real" <<'SH'
#!/usr/bin/env bash
[ -f "${ORDER_FILE:?}" ] || exit 92
printf 'real\n' >> "$ORDER_FILE"
pwd > "${CWD_FILE:?}"
python3 - "$ARGV_FILE" "$@" <<'PY'
import json,sys
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[2:], fh)
PY
input="$(cat)"
printf 'REAL:%s' "$input"
exit "${REAL_RC:-0}"
SH
chmod 0700 "$mat" "$real"

work="$TMP/work dir"; mkdir -p "$work"
order="$TMP/order"; argv="$TMP/argv.json"; cwd_file="$TMP/cwd"; err="$TMP/err"
set +e
out="$(cd "$work" && printf 'stdin data' | ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" MAT_RC=0 STATUS_RC=1 REAL_RC=23 CCC_CODEX_MEMORY_MATERIALIZER_PATH="$mat" CCC_CODEX_REAL_CLI_PATH="$real" "$LAUNCHER" --alpha 'two words' -- 2>"$err")"
rc=$?
set -e
ok "launcher preserves real Codex exit code and stdio" '[ "$rc" = 23 ] && [ "$out" = "REAL:stdin data" ]'
ok "launcher suppresses materializer body and error output" '! grep -q "MATERIALIZER_" "$err" && [[ "$out" != *MATERIALIZER_* ]]'
ok "launcher materializes before exec and preserves cwd" '[ "$(cat "$order")" = $'"'"'materialize\nreal'"'"' ] && [ "$(cat "$cwd_file")" = "$work" ]'
ok "launcher preserves argv boundaries including spaces and double dash" 'python3 - "$argv" <<'"'"'PY'"'"'
import json,sys
raise SystemExit(0 if json.load(open(sys.argv[1])) == ["--alpha", "two words", "--"] else 1)
PY'

: > "$order"; rm -f "$argv"
set +e
out="$(ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" MAT_RC=9 STATUS_RC=0 REAL_RC=0 CCC_CODEX_MEMORY_MATERIALIZER_PATH="$mat" CCC_CODEX_REAL_CLI_PATH="$real" "$LAUNCHER" ready 2>"$err")"
rc=$?
set -e
ok "launcher uses last valid snapshot when refresh fails" '[ "$rc" = 0 ] && [ "$out" = "REAL:" ] && [ "$(cat "$order")" = $'"'"'materialize\nstatus\nreal'"'"' ]'

: > "$order"; rm -f "$argv"
set +e
ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" MAT_RC=9 STATUS_RC=7 REAL_RC=0 CCC_CODEX_MEMORY_MATERIALIZER_PATH="$mat" CCC_CODEX_REAL_CLI_PATH="$real" "$LAUNCHER" blocked >"$TMP/out" 2>"$err"
rc=$?
set -e
ok "launcher fails closed when no current or last-valid snapshot exists" '[ "$rc" = 78 ] && [ ! -e "$argv" ] && [ "$(cat "$order")" = $'"'"'materialize\nstatus'"'"' ]'
ok "fail-closed diagnostic is bounded and body-free" '[ "$(wc -c < "$err")" -lt 256 ] && ! grep -q "MATERIALIZER_\|SECRET" "$err"'

: > "$order"
set +e
ORDER_FILE="$order" ARGV_FILE="$argv" CWD_FILE="$cwd_file" MAT_RC=0 STATUS_RC=0 CCC_CODEX_MEMORY_MATERIALIZER_PATH="$mat" CCC_CODEX_REAL_CLI_PATH="$LAUNCHER" "$LAUNCHER" >"$TMP/out" 2>"$err"
rc=$?
set -e
ok "launcher rejects recursive real-cli configuration" '[ "$rc" = 127 ]'
ok "launcher uses final exec rather than a child Codex process" 'grep -Fq '"'"'exec "$real_cli" "$@"'"'"' "$LAUNCHER"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
