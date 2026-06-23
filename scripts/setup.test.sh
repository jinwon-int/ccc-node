#!/usr/bin/env bash
# Tests for setup.sh backup safety.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETUP="$ROOT/setup.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

home="$TMP/home"
mkdir -p "$home/.claude" "$TMP/bin"
printf '{"existing":true}\n' > "$home/.claude/settings.json"

cat > "$TMP/bin/tar" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  -czf) printf 'not a tar archive\n' > "$2"; exit 0 ;;
  -tzf) exit 1 ;;
esac
exec /usr/bin/tar "$@"
EOF
chmod +x "$TMP/bin/tar"

settings_before="$(cat "$home/.claude/settings.json")"
out="$(HOME="$home" PATH="$TMP/bin:$PATH" bash "$SETUP" 2>&1)"; rc=$?
settings_after="$(cat "$home/.claude/settings.json")"

ok "setup fails closed when backup tar validation fails" '[ "$rc" = 1 ] && grep -q "Backup validation failed" <<<"$out"'
ok "setup leaves existing settings untouched after failed backup validation" '[ "$settings_before" = "$settings_after" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
