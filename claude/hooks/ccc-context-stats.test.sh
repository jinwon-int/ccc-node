#!/usr/bin/env bash
# shellcheck disable=SC2034  # rc/total/ALL/E/J2 are read inside eval'd assertions
# Tests for ccc-context-stats.py (#2262) — aggregates only, basename only,
# compaction / >20k share / re-Read counts, malformed lines skipped.
# Usage: bash ccc-context-stats.test.sh   (exit 0 = all pass)
#
# Hermetic: a fixture transcript tree under a temp dir passed via --root; the
# node's real ~/.claude/projects is never read.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STATS="$HERE/ccc-context-stats.py"
pass=0; fail=0

TDIR="$(mktemp -d 2>/dev/null || mktemp -d -t ccc-context-stats-test)"
trap 'rm -rf "$TDIR" 2>/dev/null || true' EXIT
export HOME="$TDIR/home"
mkdir -p "$HOME"

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

ROOT="$TDIR/projects"
# Sentinels: a tool_result body, a private directory component, the project
# directory name and a session id — none may ever reach the output.
BODY_SENTINEL="BODYSENTINEL7f3a"
DIR_SENTINEL="privdirsentinel9c"
PROJ="-proj-sentinel-4b2e"
SID="sessid-sentinel-0d1c"

python3 - "$ROOT" "$BODY_SENTINEL" "$DIR_SENTINEL" "$PROJ" "$SID" <<'PY'
import json, os, sys, time
root, body, priv, proj, sid = sys.argv[1:6]
now = time.time()
ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 3600))
old_ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 40 * 86400))

def use(uid, name, **inp):
    return {"type": "assistant", "timestamp": ts, "sessionId": sid,
            "message": {"content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}]}}

def res(uid, content, stamp=ts):
    return {"type": "user", "timestamp": stamp, "sessionId": sid,
            "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "content": content}]},
            "toolUseResult": {"stdout": body * 10}}

big_png = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "A" * 30000}}]
lines = [
    {"type": "system", "subtype": "compact_boundary", "timestamp": ts, "content": body,
     "compactMetadata": {"trigger": "auto", "preTokens": 1}},
    use("u1", "Read", file_path=f"/tmp/{priv}/render-v1/page_06.png"),
    res("u1", big_png),
    use("u2", "Read", file_path=f"/tmp/{priv}/render-v2/page_06.png"),
    res("u2", big_png),
    use("u3", "Read", file_path=f"/tmp/{priv}/render-v3/page_06.png"),
    res("u3", big_png),
    use("u4", "Bash", command=f"echo {body}"),
    res("u4", body + "x" * 9000),
    use("u5", "Read", file_path=f"/srv/{priv}/notes.md", offset=1, limit=50),
    res("u5", [{"type": "text", "text": body + "y" * 1000}]),
    use("u6", "Read", file_path=f"/srv/{priv}/notes.md", offset=51, limit=50),
    res("u6", [{"type": "text", "text": "z" * 1000}]),
    # Outside the window by timestamp: must not count.
    {"type": "system", "subtype": "compact_boundary", "timestamp": old_ts,
     "compactMetadata": {"trigger": "manual"}},
    res("u4", "q" * 50000, stamp=old_ts),
]
d = os.path.join(root, proj)
os.makedirs(os.path.join(d, sid, "subagents"), exist_ok=True)
with open(os.path.join(d, sid + ".jsonl"), "w") as f:
    for obj in lines:
        f.write(json.dumps(obj) + "\n")
    f.write("{not json " + body + "\n")       # malformed
    f.write("[1, 2, 3]\n")                    # valid JSON, not an object
    f.write("\n")                             # blank: ignored, not malformed
    f.write(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": ["bad"], "content": None}]}}) + "\n")
# Subagent transcript: its tool results count, it is not a session.
with open(os.path.join(d, sid, "subagents", "agent-x.jsonl"), "w") as f:
    f.write(json.dumps(use("s1", "Grep", pattern=body)) + "\n")
    f.write(json.dumps(res("s1", "g" * 100)) + "\n")
# A file older than --days: skipped entirely (mtime).
stale = os.path.join(d, "stale.jsonl")
with open(stale, "w") as f:
    f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
os.utime(stale, (now - 30 * 86400, now - 30 * 86400))
PY

J="$(python3 "$STATS" --root "$ROOT" --days 14 --json)"; rc=$?
ok "json exit 0" '[ "$rc" = 0 ]'
jf() { printf '%s' "$J" | python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }
ok "sessions counts top-level files only" '[ "$(jf "d[\"sessions\"]")" = 1 ]'
ok "subagent file counted separately" '[ "$(jf "d[\"subagent_files\"]")" = 1 ]'
ok "compaction counted once (old entry + stale file excluded)" '[ "$(jf "d[\"compactions\"]")" = 1 ]'
ok "compaction trigger aggregated" '[ "$(jf "d[\"compaction_triggers\"]")" = "{'"'"'auto'"'"': 1}" ]'
ok "malformed lines counted (bad json + non-object)" '[ "$(jf "d[\"malformed_lines\"]")" = 2 ]'
# results: 3 png + bash + 2 notes + bad-id + grep = 8
ok "tool results counted" '[ "$(jf "d[\"tool_results\"]")" = 8 ]'
total=$((30000 * 3 + 9000 + ${#BODY_SENTINEL} + 1000 + ${#BODY_SENTINEL} + 1000 + 100))
ok "total chars (image base64 + text)" '[ "$(jf "d[\"tool_result_chars\"]")" = "$total" ]'
ok "three results over 20k" '[ "$(jf "d[\"over_threshold_results\"]")" = 3 ]'
ok "over-threshold chars" '[ "$(jf "d[\"over_threshold_chars\"]")" = 90000 ]'
ok "over-threshold share" '[ "$(jf "d[\"over_threshold_char_share\"]")" = "$(python3 -c "print(round(90000/$total,4))")" ]'
ok "image results counted" '[ "$(jf "d[\"image_results\"]")" = 3 ]'
ok "top tool is Read" '[ "$(jf "d[\"top_tools\"][0][\"tool\"]")" = Read ]'
ok "Read over-threshold count" '[ "$(jf "d[\"top_tools\"][0][\"over_threshold\"]")" = 3 ]'
ok "unknown tool_use id bucketed as ?" '[ "$(jf "[t[\"tool\"] for t in d[\"top_tools\"]].count(\"?\")")" = 1 ]'
ok "top file is basename page_06.png" '[ "$(jf "d[\"top_files\"][0][\"file\"]")" = page_06.png ]'
ok "page_06.png read 3 times" '[ "$(jf "d[\"top_files\"][0][\"reads\"]")" = 3 ]'
ok "re-reads: same basename across versions = 2, paging not counted" '[ "$(jf "d[\"rereads\"]")" = 2 ]'
ok "top re-read basename" '[ "$(jf "d[\"top_rereads\"][0]")" = "{'"'"'file'"'"': '"'"'page_06.png'"'"', '"'"'rereads'"'"': 2}" ]'

L="$(python3 "$STATS" --root "$ROOT" --one-line)"; rc=$?
ok "one-line exit 0" '[ "$rc" = 0 ]'
ok "one-line is a single line" '[ "$(printf "%s\n" "$L" | wc -l | tr -d " ")" = 1 ]'
ok "one-line carries the headline numbers" 'case "$L" in *"compactions=1"*">20k=3"*"rereads=2 (top page_06.png x2)"*"skipped=2 lines"*) true ;; *) false ;; esac'
T="$(python3 "$STATS" --root "$ROOT")"; rc=$?
ok "text report exit 0" '[ "$rc" = 0 ]'

# No content leakage in any output mode.
ALL="$J
$L
$T"
for s in "$BODY_SENTINEL" "$DIR_SENTINEL" "$PROJ" "$SID" "/tmp/" "/srv/" "$TDIR"; do
  ok "no leak of sentinel '$s'" '! printf "%s" "$ALL" | grep -qF -- "$s"'
done

# Missing root and a too-narrow window are clean zeros, not errors.
E="$(python3 "$STATS" --root "$TDIR/nope" --one-line)"; rc=$?
ok "missing root exits 0" '[ "$rc" = 0 ]'
ok "missing root reports zeros" 'case "$E" in *"sessions=0"*"tool_results=0"*) true ;; *) false ;; esac'
ok "--days 0 rejected" '! python3 "$STATS" --root "$ROOT" --days 0 >/dev/null 2>&1'
ok "--json and --one-line are exclusive" '! python3 "$STATS" --root "$ROOT" --json --one-line >/dev/null 2>&1'

# Long and control-character basenames are sanitized and truncated.
mkdir -p "$ROOT/p2"
python3 - "$ROOT/p2/s.jsonl" <<'PY'
import json, sys
name = "/x/" + "n" * 80 + "\x1b[31m.png"
with open(sys.argv[1], "w") as f:
    f.write(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": name}}]}}) + "\n")
    f.write(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "a", "content": "k" * 99999}]}}) + "\n")
PY
J2="$(python3 "$STATS" --root "$ROOT/p2" --json)"
ok "basename truncated to 48 chars" '[ "$(printf "%s" "$J2" | python3 -c "import json,sys; print(len(json.load(sys.stdin)[\"top_files\"][0][\"file\"]))")" = 48 ]'
ok "no control characters in output" '! printf "%s" "$J2" | grep -q "$(printf "\033")"'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
