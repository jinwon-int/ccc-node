#!/usr/bin/env bash
# Unit tests for lib/detached_jobs.py (#1258) — the stateless completion-evidence
# reader for bridge-safe detached jobs. Pins the contracts the loader and the
# skill depend on: silence when nothing is outstanding (so load-memory.sh output
# stays byte-identical), fail-open on malformed input, and — the whole point of
# the issue — that a finished job is classified `done` rather than lumped in
# with a job whose watcher was merely lost.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; export HERE
MOD="$HERE/detached_jobs.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

REG="$TMP/reg.jsonl"
# Pin systemctl to "not active" so these verdicts are identical on a systemd
# node and in a container without one. Without this the running/lost split would
# depend on the host, which is exactly the ambiguity the module exists to remove.
# A stub that fails to write must abort the run: it would leave the host's real
# systemctl on PATH and the running/lost assertions would silently start
# measuring the host instead of the module.
mkdir -p "$TMP/bin" || exit 1
write_exec_stub "$TMP/bin/systemctl" <<'SH'
exit 3
SH
export PATH="$TMP/bin:$PATH"
command -v systemctl >/dev/null && [ "$(command -v systemctl)" = "$TMP/bin/systemctl" ] || {
  echo "FAIL: systemctl stub not in effect"; exit 1
}

reg() { : > "$REG"; }

# ---- silence contract -------------------------------------------------------
# These are the reason the block can default to ON in load-memory.sh.
reg
out="$(python3 "$MOD" sweep "$REG")"
ok "empty registry prints nothing" '[ -z "$out" ]'

out="$(python3 "$MOD" sweep "$TMP/does-not-exist.jsonl")"
ok "missing registry prints nothing" '[ -z "$out" ]'

printf 'not json at all\n{"unit":\n' > "$REG"
out="$(python3 "$MOD" sweep "$REG"; echo "rc=$?")"
ok "malformed registry fails open and stays silent" '[ "$out" = "rc=0" ]'

printf '["not","a","dict"]\n' > "$REG"
out="$(python3 "$MOD" sweep "$REG")"
ok "non-dict records are skipped" '[ -z "$out" ]'

printf '{"log":"/x","started_at":1}\n' > "$REG"
out="$(python3 "$MOD" sweep "$REG")"
ok "record without a unit is skipped" '[ -z "$out" ]'

# ---- the issue's actual failure: done must not read as lost -----------------
# A job that already wrote EXIT=0 was being reported as `status=stopped` with no
# completion record. It must come back as done, with the real code.
reg
LOG="$TMP/job1.log"; printf 'working\nEXIT=0\n' > "$LOG"
python3 "$MOD" register --unit job1 --log "$LOG" --summary "recheck addr" --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "completed job is reported done" 'grep -q "완료됨" <<<"$out"'
ok "completed job shows its exit code" 'grep -q "EXIT=0" <<<"$out"'
ok "completed job is not called lost" '! grep -q "소식 없음" <<<"$out"'
ok "completed job names its log path" 'grep -q "job1.log" <<<"$out"'

# A nonzero code is still `done` — the job ran to completion and failed, which
# is a different action from "unaccounted for".
reg
LOG2="$TMP/job2.log"; printf 'boom\nEXIT=7\n' > "$LOG2"
python3 "$MOD" register --unit job2 --log "$LOG2" --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "failed-but-finished job is done, not lost" 'grep -q "EXIT=7" <<<"$out" && ! grep -q "소식 없음" <<<"$out"'

# The last marker wins: a log that was appended to across a retry must report
# the final outcome, not the first one.
reg
LOG3="$TMP/job3.log"; printf 'EXIT=1\nretried\nEXIT=0\n' > "$LOG3"
python3 "$MOD" register --unit job3 --log "$LOG3" --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "last EXIT marker wins" 'grep -q "EXIT=0" <<<"$out" && ! grep -q "EXIT=1" <<<"$out"'

# A marker with an unreadable payload still proves the job stopped.
reg
LOG4="$TMP/job4.log"; printf 'EXIT=notanumber\n' > "$LOG4"
python3 "$MOD" register --unit job4 --log "$LOG4" --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "malformed EXIT payload still counts as finished" 'grep -q "완료됨" <<<"$out"'

# ---- running vs lost --------------------------------------------------------
# No marker, log touched just now: still working. Re-running would duplicate
# possibly non-idempotent work, so this must never read as lost.
reg
LOG5="$TMP/job5.log"; printf 'still going\n' > "$LOG5"
python3 "$MOD" register --unit job5 --log "$LOG5" --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "fresh log with no marker is running" 'grep -q "진행 중" <<<"$out"'
ok "running job is not reported done" '! grep -q "완료됨" <<<"$out"'

# No marker, log quiet well past the stale threshold, unit gone: this is the
# genuinely ambiguous case the issue says must be separated out.
reg
LOG6="$TMP/job6.log"; printf 'started\n' > "$LOG6"
touch -d '2 hours ago' "$LOG6" 2>/dev/null || touch -t 200001010000 "$LOG6"
python3 "$MOD" register --unit job6 --log "$LOG6" --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "stale log with no marker is lost" 'grep -q "소식 없음" <<<"$out"'

# A registered job whose log never appeared at all is also unaccounted for.
reg
python3 "$MOD" register --unit job7 --log "$TMP/never-created.log" --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "missing log with dead unit is lost" 'grep -q "소식 없음" <<<"$out"'

# ---- ack stops the repeat ---------------------------------------------------
# Without this the same finished job would be re-announced at every SessionStart
# forever, which trains the reader to ignore the block.
reg
LOG8="$TMP/job8.log"; printf 'EXIT=0\n' > "$LOG8"
python3 "$MOD" register --unit job8 --log "$LOG8" --registry "$REG" >/dev/null
python3 "$MOD" ack --unit job8 --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "acked job disappears from the block" '[ -z "$out" ]'

# An ack is a partial record — {"unit":..,"acked":true} with no log. Merging it
# over the original must keep the log path, or a later un-ack/re-read would have
# lost the only pointer to the completion evidence.
merged="$(python3 - "$REG" <<'PY'
import json, sys, os
sys.path.insert(0, os.environ["HERE"])
import detached_jobs as dj
recs = {r["unit"]: r for r in dj.load_records(sys.argv[1])}
print(json.dumps(recs.get("job8", {}), sort_keys=True))
PY
)"
ok "ack merges over the record without erasing its log" 'grep -q "job8.log" <<<"$merged"'
ok "ack sets the acked flag on the merged record" 'grep -q "\"acked\": true" <<<"$merged"'

reg
printf 'EXIT=0\n' > "$TMP/j9.log"; printf 'EXIT=0\n' > "$TMP/j10.log"
python3 "$MOD" register --unit j9 --log "$TMP/j9.log" --registry "$REG" >/dev/null
python3 "$MOD" register --unit j10 --log "$TMP/j10.log" --registry "$REG" >/dev/null
python3 "$MOD" ack --all --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "ack --all clears every outstanding job" '[ -z "$out" ]'

# ---- re-registration and expiry --------------------------------------------
# Re-using a unit name (the skill's Step 4 reset-failed path) must point at the
# new log, not keep reporting the old one.
reg
printf 'EXIT=0\n' > "$TMP/old.log"; printf 'fresh\n' > "$TMP/new.log"
python3 "$MOD" register --unit reused --log "$TMP/old.log" --registry "$REG" >/dev/null
python3 "$MOD" register --unit reused --log "$TMP/new.log" --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "re-registered unit uses the newest log" 'grep -q "new.log" <<<"$out" && ! grep -q "old.log" <<<"$out"'

# Old records are archived noise, not live context.
reg
printf '{"unit":"ancient","log":"%s","started_at":1}\n' "$TMP/j9.log" > "$REG"
out="$(python3 "$MOD" sweep "$REG")"
ok "records past the age cap are dropped" '[ -z "$out" ]'

# ---- injection safety -------------------------------------------------------
# This text lands in a future session's context window, so the renderer must
# reach for known fields only and must cap what it prints.
reg
python3 "$MOD" register --unit safe --log "$TMP/j9.log" \
  --summary "ok" --registry "$REG" >/dev/null
python3 - "$REG" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"unit": "safe", "secret": "SECRET-TOKEN", "notes": "SECRET-NOTES"}) + "\n")
PY
out="$(python3 "$MOD" sweep "$REG")"
ok "unknown free-text fields are not rendered" '! grep -q "SECRET" <<<"$out"'

reg
long="$(python3 -c 'print("x"*4000)')"
python3 "$MOD" register --unit longsum --log "$TMP/j9.log" --summary "$long" --registry "$REG" >/dev/null
out="$(python3 "$MOD" sweep "$REG")"
ok "summary is capped at registration" '[ "${#out}" -lt 600 ]'

reg
for i in $(seq 1 12); do
  printf 'EXIT=0\n' > "$TMP/many$i.log"
  python3 "$MOD" register --unit "many$i" --log "$TMP/many$i.log" --registry "$REG" >/dev/null
done
out="$(python3 "$MOD" sweep "$REG" --max-bytes 400)"
ok "sweep honours --max-bytes" '[ "$(printf %s "$out" | wc -c)" -le 420 ]'
ok "overflow rows are summarised, not dropped silently" 'grep -q "외 " <<<"$out" || grep -q "truncated" <<<"$out"'

# ---- CLI contract -----------------------------------------------------------
out="$(python3 "$MOD" register --unit x 2>&1; echo "rc=$?")"
ok "register without --log is a usage error" 'grep -q "rc=2" <<<"$out"'

out="$(python3 "$MOD" ack 2>&1; echo "rc=$?")"
ok "ack without --unit is a usage error" 'grep -q "rc=2" <<<"$out"'

# The bare-path form is what load-memory.sh calls, mirroring pending_promises.py.
reg
printf 'EXIT=0\n' > "$TMP/bare.log"
python3 "$MOD" register --unit bare --log "$TMP/bare.log" --registry "$REG" >/dev/null
out="$(python3 "$MOD" "$REG" --max-bytes 1024)"
ok "bare-path invocation sweeps like pending_promises.py" 'grep -q "완료됨" <<<"$out"'

# The summary line format is load-bearing: validate-harness.sh's
# suite_summary() greps for exactly ^PASS=<n> FAIL=<n>$, so a lowercase variant
# reads as a suite that asserted nothing.
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
