#!/usr/bin/env bash
# Tests for lib/trajectory_render.py (#2388) — the shared session grammar.
# Golden fixtures live under tests/fixtures/trajectory_render/ (outside the
# deployed hook tree): the SAME trajectory recorded by Claude, Codex and Piri.
# Regenerate goldens after an intended grammar change with
#   CCC_UPDATE_GOLDEN=1 bash claude/hooks/lib/trajectory_render.test.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MOD="$HERE/trajectory_render.py"
FIX="$(cd "$HERE/../../../tests/fixtures/trajectory_render" && pwd)"
pass=0; fail=0
BASE_TMP="${TMPDIR:-/tmp}"; mkdir -p "$BASE_TMP"
TMP="$(mktemp -d "$BASE_TMP/ccc-trajectory-render-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

render() { python3 "$MOD" render "$@"; }

# ---- golden: three providers, one grammar ------------------------------------
for fmt in claude codex piri; do
  render --format "$fmt" --obs-trunc omit --args-trunc 400 "$FIX/$fmt.jsonl" > "$TMP/$fmt.out"
  if [ "${CCC_UPDATE_GOLDEN:-0}" = 1 ]; then cp "$TMP/$fmt.out" "$FIX/$fmt.omit.golden"; fi
  ok "$fmt fixture renders byte-identical to its golden" 'cmp -s "$TMP/$fmt.out" "$FIX/$fmt.omit.golden"'
done

tags() { sed -E 's/^(\[[A-Z ]+\]).*/\1/' "$1"; }
ok "all three providers emit the same tag sequence" \
  '[ "$(tags "$TMP/claude.out")" = "$(tags "$TMP/codex.out")" ] && [ "$(tags "$TMP/claude.out")" = "$(tags "$TMP/piri.out")" ]'
markers() { grep -o '…(+[0-9]* chars — render truncation)' "$1"; }
ok "all three providers emit the same truncation markers" \
  '[ "$(markers "$TMP/claude.out")" = "$(markers "$TMP/codex.out")" ] && [ "$(markers "$TMP/claude.out")" = "$(markers "$TMP/piri.out")" ]'
ok "once tool names are set aside the three renderings are identical" \
  '[ "$(sed -E "s/^\[ACTION\] [a-z_A-Z]+\(/[ACTION] (/" "$TMP/claude.out")" = "$(sed -E "s/^\[ACTION\] [a-z_A-Z]+\(/[ACTION] (/" "$TMP/codex.out")" ] && [ "$(sed -E "s/^\[ACTION\] [a-z_A-Z]+\(/[ACTION] (/" "$TMP/claude.out")" = "$(sed -E "s/^\[ACTION\] [a-z_A-Z]+\(/[ACTION] (/" "$TMP/piri.out")" ]'
ok "the tag set is the shared grammar's" \
  '[ "$(tags "$TMP/claude.out" | LC_ALL=C sort -u | tr "\n" " ")" = "[ACTION] [ASSISTANT] [OBSERVATION ERROR] [OBSERVATION] [USER] " ]'
ok "injected developer noise, bookkeeping and the duplicate event_msg are absent" \
  '! grep -q "skills_instructions\|bookkeeping\|token_count" "$TMP/codex.out" "$TMP/claude.out" && [ "$(grep -c "^\[USER\]" "$TMP/codex.out")" = 1 ]'
ok "a failed tool result keeps its error tag even with the body omitted" \
  'grep -qx "\[OBSERVATION ERROR\] …(+37 chars — render truncation)" "$TMP/codex.out"'
ok "thinking is not rendered unless the caller opts in" '! grep -q "THINKING" "$TMP/claude.out"'
for fmt in claude codex piri; do
  render --format "$fmt" --obs-trunc omit --args-trunc 400 --thinking "$FIX/$fmt.jsonl" > "$TMP/$fmt.think"
done
ok "--thinking renders [THINKING] for every provider" \
  '[ "$(grep -c "^\[THINKING\] 먼저 상태를 본다$" "$TMP/claude.think" "$TMP/codex.think" "$TMP/piri.think" | cut -d: -f2 | tr -d "\n")" = 111 ]'

# ---- obs_trunc guard ---------------------------------------------------------
render --format claude --obs-trunc 9999 --args-trunc 400 "$FIX/claude.jsonl" >/dev/null 2>"$TMP/err"; rc=$?
ok "obs_trunc below 10000 is a configuration error (exit 2)" '[ "$rc" = 2 ] && grep -q "below 10000" "$TMP/err"'
render --format claude --obs-trunc 0 --args-trunc 400 "$FIX/claude.jsonl" >/dev/null 2>&1; rc=$?
ok "obs_trunc 0 is rejected too" '[ "$rc" = 2 ]'
render --format claude --obs-trunc lots --args-trunc 400 "$FIX/claude.jsonl" >/dev/null 2>&1; rc=$?
ok "a non-numeric obs_trunc is rejected" '[ "$rc" = 2 ]'
render --format claude --args-trunc 400 "$FIX/claude.jsonl" >/dev/null 2>&1; rc=$?
ok "obs_trunc has no default: omitting it is a usage error" '[ "$rc" = 2 ]'
render --format claude --obs-trunc omit "$FIX/claude.jsonl" >/dev/null 2>&1; rc=$?
ok "args_trunc has no default: omitting it is a usage error" '[ "$rc" = 2 ]'
render --format claude --obs-trunc 10000 --args-trunc 400 "$FIX/claude.jsonl" > "$TMP/obs10k"; rc=$?
ok "obs_trunc 10000 is accepted" '[ "$rc" = 0 ]'
# shellcheck disable=SC2034  # obs_line is read via eval inside ok()
obs_line="$(python3 -c 'import sys; t=open(sys.argv[1]).read(); s=t.index("[OBSERVATION] "); e=t.index("\n[ACTION]", s); print(len(t[s+14:e]))' "$TMP/obs10k")"
# shellcheck disable=SC2034  # m500 is read via eval inside ok()
m500="…(+500 chars — render truncation)"
ok "a cut observation keeps exactly obs_trunc chars plus the marker" \
  'grep -qF "$m500" "$TMP/obs10k" && [ "$obs_line" = "$(python3 -c "import sys; print(10000 + len(sys.argv[1]))" "$m500")" ]'
# shellcheck disable=SC2034  # read via eval inside ok()
python3 - "$MOD" <<'PY' 2>"$TMP/api" && api_rc=0 || api_rc=$?
import importlib.util, sys
spec = importlib.util.spec_from_file_location("trajectory_render", sys.argv[1])
tr = importlib.util.module_from_spec(spec); sys.modules[spec.name] = tr; spec.loader.exec_module(tr)
ev = [tr.Event("observation", ("x" * 20000,))]
try:
    tr.render(ev)
except TypeError:
    pass
else:
    sys.exit("render() accepted missing budgets")
for bad in (9999, 1, True, "10000"):
    try:
        tr.render(ev, obs_trunc=bad, args_trunc=400, text_trunc=None, thinking=False)
    except ValueError:
        continue
    sys.exit(f"obs_trunc={bad!r} accepted")
out = tr.render(ev, obs_trunc=None, args_trunc=None, text_trunc=None, thinking=False)
assert out == ["[OBSERVATION] …(+20000 chars — render truncation)"], out
out = tr.render([tr.Event("action", name="Bash", command="ls -la")],
                obs_trunc=None, args_trunc=None, text_trunc=None, thinking=False)
assert out == ["[ACTION] Bash(…(+6 chars — render truncation))"], out
out = tr.render([tr.Event("user", ("가" * 10,))], obs_trunc=None, args_trunc=1,
                text_trunc=4, thinking=False)
assert out == ["[USER] 가가가가…(+6 chars — render truncation)"], out
PY
ok "python API: budgets are required keywords, the guard rejects <10000/bool/str, omit modes are marked" '[ "$api_rc" = 0 ]'

# ---- window clipping -----------------------------------------------------------
python3 - "$MOD" > "$TMP/lines" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("trajectory_render", sys.argv[1])
tr = importlib.util.module_from_spec(spec); sys.modules[spec.name] = tr; spec.loader.exec_module(tr)
print(tr.tail_lines("l1\nl2\nl3\nl4\nl5", 2))
print("--")
print(tr.tail_lines("l1\nl2", 5))
PY
ok "tail_lines keeps the last N lines behind a marker counting the dropped chars" \
  '[ "$(cat "$TMP/lines")" = "$(printf "…(+9 chars — render truncation)\nl4\nl5\n--\nl1\nl2")" ]'

out="$(printf 'short' | python3 "$MOD" clip --max-bytes 100)"
ok "clip passes an under-budget window through unchanged" '[ "$out" = "short" ]'
printf '한%.0s' $(seq 1 100) | python3 "$MOD" clip --max-bytes 31 > "$TMP/clip"
ok "clip keeps whole UTF-8 characters within the byte budget" \
  'python3 -c "import sys; open(sys.argv[1], \"rb\").read().decode(\"utf-8\")" "$TMP/clip" && [ "$(tail -n 1 "$TMP/clip" | tr -d "\n" | wc -c)" -le 31 ] && head -n 1 "$TMP/clip" | grep -qx "…(+90 chars — render truncation)"'
# shellcheck disable=SC2034  # read via eval inside ok()
out="$(printf '%s\nabcdefghij' '…(+7 chars — render truncation)' | python3 "$MOD" clip --max-bytes 4)"
ok "clip merges an earlier window marker instead of stacking a second one" \
  '[ "$out" = "$(printf "…(+13 chars — render truncation)\nghij")" ] && [ "$(grep -c "render truncation" <<<"$out")" = 1 ]'
render --format claude --obs-trunc omit --args-trunc 400 --max-lines 2 "$FIX/claude.jsonl" > "$TMP/tail2"
ok "render --max-lines marks the dropped head of the session" \
  'head -n 1 "$TMP/tail2" | grep -q "^…(+[0-9]* chars — render truncation)$" && [ "$(wc -l < "$TMP/tail2" | tr -d " ")" = 3 ]'

# ---- adapters: provider specifics ------------------------------------------------
cat > "$TMP/headless.jsonl" <<'EOF'
{"type":"session_meta","payload":{"session_id":"h","cwd":"/x","originator":"codex_exec"}}
{"type":"event_msg","payload":{"type":"user_message","message":"run the nightly job"}}
{"type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"backup --verify\"}"}}
{"type":"event_msg","payload":{"type":"agent_message","message":"backup verified"}}
{"type":"event_msg","payload":{"type":"error","message":"stream disconnected before completion"}}
not json
EOF
render --format codex --obs-trunc omit --args-trunc 400 "$TMP/headless.jsonl" > "$TMP/headless.out"
ok "headless codex rollouts fall back to event_msg in session order, errors become [ERROR]" \
  '[ "$(cat "$TMP/headless.out")" = "$(printf "[USER] run the nightly job\n[ACTION] exec_command(backup --verify)\n[ASSISTANT] backup verified\n[ERROR] stream disconnected before completion")" ]'
cat > "$TMP/custom.jsonl" <<'EOF'
{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"patch it"}]}}
{"type":"response_item","payload":{"type":"custom_tool_call","name":"apply_patch","input":"*** Begin Patch"}}
{"type":"response_item","payload":{"type":"custom_tool_call_output","output":"{\"output\":\"Done!\",\"metadata\":{\"exit_code\":0}}"}}
EOF
render --format codex --obs-trunc 10000 --args-trunc 400 "$TMP/custom.jsonl" > "$TMP/custom.out"
ok "codex custom tool calls and their outputs render as ACTION/OBSERVATION" \
  'grep -qx "\[ACTION\] apply_patch(\*\*\* Begin Patch)" "$TMP/custom.out" && grep -qx "\[OBSERVATION\] Done!" "$TMP/custom.out"'
render --format claude --obs-trunc omit --args-trunc 400 "$TMP/does-not-exist.jsonl" >/dev/null 2>&1
# shellcheck disable=SC2034  # read via eval inside ok()
rc=$?
ok "an unreadable source exits 1" '[ "$rc" = 1 ]'

# ---- prompt notes ---------------------------------------------------------------
# shellcheck disable=SC2034  # read via eval inside ok()
note="$(python3 "$MOD" prompt-note)"
ok "prompt-note names every grammar tag and the marker contract" \
  'for t in "[USER]" "[ASSISTANT]" "[ACTION]" "[OBSERVATION]" "[OBSERVATION ERROR]" "[ERROR]"; do grep -qF -- "$t" <<<"$note" || exit 1; done; grep -qF "…(+N chars — render truncation)" <<<"$note" && grep -q "not agent, tool" <<<"$note"'
ok "marker-note is the marker sentence alone" \
  '[ "$(python3 "$MOD" marker-note)" = "$(sed -n "s/.*\(Markers of the form.*\)/\1/p" <<<"$note")" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
