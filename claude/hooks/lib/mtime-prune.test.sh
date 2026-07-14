#!/usr/bin/env bash
# Tests for lib/mtime-prune.sh — portable, busybox-safe mtime prune/select (#449).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=claude/hooks/lib/mtime-prune.sh
. "$HERE/mtime-prune.sh"

pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# Deterministic mtimes: older index => older timestamp.
mk() { # mk <path> <YYYYMMDDhhmm>
  : > "$1"
  touch -t "$2" "$1"
}

# ---- prune_keep_newest: keeps N newest, removes older ----------------------
D="$TMP/ckpt"; mkdir -p "$D"
mk "$D/working-state-1.md" 202601010001
mk "$D/working-state-2.md" 202601010002
mk "$D/working-state-3.md" 202601010003
mk "$D/working-state-4.md" 202601010004
mk "$D/working-state-5.md" 202601010005
prune_keep_newest "$D" 'working-state-*.md' 2
remaining="$(ls "$D" | sort | tr '\n' ' ')"
ok "prune keeps exactly 2 newest" '[ "$(ls "$D" | wc -l | tr -d " ")" = 2 ]'
ok "prune kept the 2 newest by mtime (4,5)" '[ "$remaining" = "working-state-4.md working-state-5.md " ]'

# keep=0 removes all matching
prune_keep_newest "$D" 'working-state-*.md' 0
ok "prune keep=0 removes all matches" '[ "$(ls "$D" | wc -l | tr -d " ")" = 0 ]'

# ---- busybox scenario: works with a `find` that lacks -printf ---------------
# Shadow `find` with a stub that errors on -printf, proving the helper never
# depends on GNU find. (The real bug: busybox find silently no-op'd the prune.)
BB="$TMP/bin"; mkdir -p "$BB"
cat > "$BB/find" <<'STUB'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "-printf" ]; then echo "find: unrecognized: -printf" >&2; exit 1; fi
done
exit 1
STUB
chmod +x "$BB/find"
D2="$TMP/bb"; mkdir -p "$D2"
mk "$D2/a.json" 202601010001
mk "$D2/b.json" 202601010002
mk "$D2/c.json" 202601010003
PATH="$BB:$PATH" prune_keep_newest "$D2" '*.json' 1
ok "busybox find(no -printf): prune still trims to 1" '[ "$(ls "$D2" | wc -l | tr -d " ")" = 1 ]'
ok "busybox find(no -printf): kept the newest (c)" '[ "$(ls "$D2")" = "c.json" ]'

# ---- whitespace-safe filenames ---------------------------------------------
D3="$TMP/ws"; mkdir -p "$D3"
mk "$D3/old file.json" 202601010001
mk "$D3/new file.json" 202601010002
prune_keep_newest "$D3" '*.json' 1
ok "whitespace filenames: trims to 1" '[ "$(ls "$D3" | wc -l | tr -d " ")" = 1 ]'
ok "whitespace filenames: kept the newest" '[ "$(ls "$D3")" = "new file.json" ]'

# ---- newest_file selection --------------------------------------------------
D4="$TMP/sel"; mkdir -p "$D4"
mk "$D4/working-state-x.md" 202601010001
mk "$D4/working-state-y.md" 202601010009
mk "$D4/working-state-z.md" 202601010005
got="$(newest_file "$D4" 'working-state-*.md')"
ok "newest_file returns newest by mtime" '[ "$got" = "$D4/working-state-y.md" ]'
ok "newest_file: empty when no match" '[ -z "$(newest_file "$D4" "nope-*.md")" ]'
ok "newest_file: empty when dir missing" '[ -z "$(newest_file "$TMP/does-not-exist" "*.md")" ]'

# ---- guards -----------------------------------------------------------------
ok "prune no-op when dir missing (rc 0)" 'prune_keep_newest "$TMP/missing" "*.json" 2'
ok "prune no-op on non-numeric keep (rc 0)" 'prune_keep_newest "$D4" "*.md" abc'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
