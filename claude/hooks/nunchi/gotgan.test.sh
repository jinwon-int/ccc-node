#!/usr/bin/env bash
# Tests for gotgan.py — nunchi's own verbatim store (#2331).
# Fully isolated: GOTGAN_DB/NUNCHI_HOME point into a temp dir, embeddings are
# forced off (no model download, no network), MemPalace CLI is a PATH stub.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GG="$HERE/gotgan.py"
NP="$HERE/nunchi.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

export NUNCHI_HOME="$TMP/nunchi-home"
export GOTGAN_DB="$NUNCHI_HOME/gotgan.db"
export GOTGAN_EMBED=off
mkdir -p "$TMP/claude/p1" "$TMP/codex" "$TMP/piri" "$TMP/bin"

python3 - "$TMP" <<'PY'
import json, sys
T = sys.argv[1]
def w(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
w(f"{T}/claude/p1/s1.jsonl", [
    {"type": "user", "sessionId": "s1", "timestamp": "2026-10-14T01:00:00Z",
     "message": {"role": "user", "content": "포괄위임 배정제도 개편안 PR #2331 확인해줘"}},
    {"type": "assistant", "sessionId": "s1", "timestamp": "2026-10-14T01:00:05Z",
     "message": {"content": [
         {"type": "text", "text": "chromadb 핀 1.5.9 보류<system-reminder>REMINDER-LEAK</system-reminder>"},
         {"type": "tool_use", "name": "Bash", "input": {"command": "TOOLUSE-LEAK"}}]}},
    {"type": "user", "sessionId": "s1",
     "message": {"content": [{"type": "tool_result", "content": "TOOLRESULT-LEAK"}]}},
    {"type": "user", "isMeta": True, "sessionId": "s1", "message": {"content": "META-LEAK"}},
    {"type": "user", "sessionId": "s1", "message": {"content": "<command-name>/clear</command-name>"}},
])
w(f"{T}/codex/r1.jsonl", [
    {"type": "response_item", "timestamp": "2026-10-13T00:00:00Z",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "강제경매 우선신청 스케줄 정리 완료"}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
     "content": [{"type": "input_text", "text": "summarize [nunchi-codex-feed-816]"}]}},
    {"type": "response_item", "payload": {"type": "reasoning", "content": "REASONING-LEAK"}},
])
w(f"{T}/piri/p1.jsonl", [
    {"type": "message", "timestamp": "2026-10-12T00:00:00Z",
     "message": {"role": "user", "content": [{"type": "text", "text": "공매 진행중 세대 확인"}]}},
])
PY

s() { python3 "$GG" search "$1" -n "${2:-3}"; }

# ---- 1. index three providers ------------------------------------------------
out="$(python3 "$GG" index claude "$TMP/claude")"
ok "claude index adds 2 chunks" 'grep -q "\"chunks_added\": 2" <<<"$out"'
out="$(python3 "$GG" index codex "$TMP/codex")"
ok "codex index adds 1 chunk (feed prompt + reasoning dropped)" 'grep -q "\"chunks_added\": 1" <<<"$out"'
out="$(python3 "$GG" index piri "$TMP/piri")"
ok "piri index adds 1 chunk" 'grep -q "\"chunks_added\": 1" <<<"$out"'
ok "db is owner-only" '[ "$(stat -c %a "$GOTGAN_DB")" = 600 ]'

# ---- 2. idempotent re-index ----------------------------------------------------
out="$(python3 "$GG" index claude "$TMP/claude")"
ok "re-index is a no-op" 'grep -q "\"chunks_added\": 0" <<<"$out"'

# ---- 3. lexical recall ---------------------------------------------------------
ok "2-syllable Korean term hits via bigram phrase" 'grep -q "배정제도" <<<"$(s 배정)"'
ok "longer Korean term with particle hits" 'grep -q "우선신청" <<<"$(s "우선신청은 어떻게")"'
ok "identifier hits via trigram" 'grep -q "#2331" <<<"$(s "#2331")"'
ok "mixed ascii+korean hits" 'grep -q "chromadb" <<<"$(s "chromadb 핀")"'
ok "provider label on excerpt line" 'grep -q "^\[codex 2026-10-13\] AGENT:" <<<"$(s 강제경매)"'
ok "non-adjacent bigrams do not match" '[ -z "$(s "배개")" ]'
ok "unknown term returns nothing" '[ -z "$(s 존재하지않는단어)" ]'

# ---- 4. excluded content never indexed -----------------------------------------
for leak in REMINDER-LEAK TOOLUSE-LEAK TOOLRESULT-LEAK META-LEAK REASONING-LEAK nunchi-codex-feed-816 clear; do
  ok "excluded: $leak" '[ -z "$(s "$leak")" ]'
done

# ---- 5. incremental append + rewrite -------------------------------------------
printf '%s\n' '{"type":"user","sessionId":"s1","timestamp":"2026-10-15T00:00:00Z","message":{"content":"신규 줄 감지 확인"}}' >> "$TMP/claude/p1/s1.jsonl"
printf '%s' '{"type":"user","message":{"content":"반쯤 쓰인 줄"' >> "$TMP/claude/p1/s1.jsonl"
out="$(python3 "$GG" index claude "$TMP/claude")"
ok "append indexes only the new complete line" 'grep -q "\"chunks_added\": 1" <<<"$out"'
ok "half-written tail is not indexed yet" '[ -z "$(s "반쯤")" ]'
printf '%s\n' '{"type":"user","sessionId":"s9","message":{"content":"교체된 파일 내용"}}' > "$TMP/claude/p1/s1.jsonl"
python3 "$GG" index claude "$TMP/claude" >/dev/null
ok "rewritten file drops stale chunks" '[ -z "$(s 배정)" ]'
ok "rewritten file indexes new content" 'grep -q "교체된" <<<"$(s 교체된)"'

# ---- 6. symlink / foreign file guard -------------------------------------------
mkdir -p "$TMP/claude/p2" "$TMP/outside"
printf '%s\n' '{"type":"user","message":{"content":"심볼릭 링크 유출"}}' > "$TMP/outside/x.jsonl"
ln -s "$TMP/outside/x.jsonl" "$TMP/claude/p2/link.jsonl"
python3 "$GG" index claude "$TMP/claude" >/dev/null
ok "symlinked transcript is skipped" '[ -z "$(s 심볼릭)" ]'

# ---- 7. status ------------------------------------------------------------------
st="$(python3 "$GG" status)"
ok "status reports ok + integrity" 'grep -q "\"state\": \"ok\"" <<<"$st" && grep -q "\"integrity\": \"ok\"" <<<"$st"'
ok "status lexical-only (no vectors)" 'grep -q "\"vectors\": 0" <<<"$st"'
ok "absent db reports absent" 'GOTGAN_DB="$TMP/none.db" python3 "$GG" status | grep -q "\"state\": \"absent\""'
ok "search on absent db is empty, rc 0" '[ -z "$(GOTGAN_DB="$TMP/none.db" python3 "$GG" search 배정)" ]'

# ---- 8. nunchi backend switch ---------------------------------------------------
cat > "$TMP/bin/mempalace" <<'SH'
#!/usr/bin/env bash
echo "MP-STUB-LINE"
SH
chmod +x "$TMP/bin/mempalace"
v() { PATH="$TMP/bin:$PATH" CCC_NUNCHI_VERBATIM_BACKEND="$1" python3 -c \
  "import sys; sys.path.insert(0, '$HERE'); import nunchi; print(nunchi.verbatim_excerpts('교체된 내용', 3))"; }
ok "default backend stays mempalace" 'out="$(PATH="$TMP/bin:$PATH" python3 -c "import sys; sys.path.insert(0, \"$HERE\"); import nunchi; print(nunchi.verbatim_excerpts(\"교체된\", 3))")"; grep -q MP-STUB-LINE <<<"$out" && ! grep -q 교체된 <<<"$out"'
ok "gotgan backend serves gotgan only" 'out="$(v gotgan)"; grep -q 교체된 <<<"$out" && ! grep -q MP-STUB-LINE <<<"$out"'
ok "both backend merges" 'out="$(v both)"; grep -q 교체된 <<<"$out" && grep -q MP-STUB-LINE <<<"$out"'
ok "unknown backend falls back to mempalace" 'out="$(v bogus)"; grep -q MP-STUB-LINE <<<"$out"'
ok "gotgan backend with absent db is empty" 'out="$(GOTGAN_DB="$TMP/none.db" v gotgan)"; [ -z "$out" ]'

echo "gotgan tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
