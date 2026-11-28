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
# shellcheck disable=SC2034  # merged is read via eval inside ok()
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
# shellcheck disable=SC2034  # out is read via eval inside ok()
out="$(python3 "$MOD" "$REG" --max-bytes 1024)"
ok "bare-path invocation sweeps like pending_promises.py" 'grep -q "완료됨" <<<"$out"'

# ---- #2636: wait budget — ⏳ only for jobs the conversation can resume on ----
# A fake bridge interpreter records the external_wait_cli argv it was handed and
# answers like a successful registration; the registry is selected via env so
# the arming path (skipped for --registry) is exercised.
ARMS="$TMP/arms.log"
write_exec_stub "$TMP/bin/fake-bridge-python" <<'SH'
printf '%s\n' "$*" >> "${ARMS_LOG:?}"
printf '{"ok":true,"wait_id":"w-test"}\n'
SH
export ARMS_LOG="$ARMS" CCC_BRIDGE_PYTHON="$TMP/bin/fake-bridge-python" CCC_DETACHED_JOBS_REGISTRY="$TMP/reg2636.jsonl"
unset CCC_DETACHED_JOBS_NO_WAIT
arm_count() { [ -f "$ARMS" ] && wc -l < "$ARMS" || echo 0; }
reg2636() { : > "$CCC_DETACHED_JOBS_REGISTRY"; : > "$ARMS"; }

py_dj() { (cd "$HERE" && python3 -c "$@"); }
ok "#2636: parse_duration accepts 45m/2h/1h30m/1d/90min and rejects junk" \
  'py_dj "import detached_jobs as d; assert [d.parse_duration(x) for x in [\"45m\",\"2h\",\"1h30m\",\"1d\",\"90min\",\"3600\"]] == [2700,7200,5400,86400,5400,3600]; assert d.parse_duration(\"abc\") is None and d.parse_duration(\"0\") is None"'
ok "#2636: parse_until HH:MM rolls to tomorrow when already past, ISO/space forms parse, past instants are None" \
  'py_dj "import detached_jobs as d, time; now=time.time(); s=d.parse_until(\"00:00\", now=now); assert 0 < s <= 86400; assert d.parse_until(time.strftime(\"%Y-%m-%d %H:%M\", time.localtime(now+7200)), now=now) > 7000; assert d.parse_until(\"2020-01-01T00:00:00\", now=now) is None; assert d.parse_until(\"nope\", now=now) is None"'
ok "#2636: wait_plan — within budget sizes the wait to expectation+margin, at budget caps, over budget does not arm, none keeps the default" \
  'py_dj "import detached_jobs as d; a=d.wait_plan(5400, budget=21600); b=d.wait_plan(21600, budget=21600); c=d.wait_plan(72000, budget=21600); n=d.wait_plan(None, budget=21600); assert a[\"arm\"] and a[\"timeout_seconds\"]==7200; assert b[\"arm\"] and b[\"timeout_seconds\"]==21600; assert c[\"arm\"] is False; assert n[\"arm\"] and n[\"timeout_seconds\"] is None"'

reg2636
out="$(python3 "$MOD" register --unit over --log "$TMP/over.log" --summary "sleeps until morning" --expect 20h)"
ok "#2636: --expect over the budget arms no bridge wait and says why" \
  '[ "$(arm_count)" = 0 ] && grep -q "not armed" <<<"$out" && grep -q "6h" <<<"$out"'
ok "#2636: the over-budget row carries expect_seconds and notify_on_exit" \
  'python3 -c "import json,sys; r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()][-1]; assert r[\"expect_seconds\"]==72000 and r[\"notify_on_exit\"] is True" "$CCC_DETACHED_JOBS_REGISTRY"'

reg2636
out="$(python3 "$MOD" register --unit within --log "$TMP/within.log" --summary "90 minute build" --expect 90m)"
ok "#2636: --expect within the budget arms the wait sized to expectation + margin" \
  '[ "$(arm_count)" = 1 ] && grep -q -- "--source detached-job --unit within " "$ARMS" && grep -q -- "--timeout-seconds 7200" "$ARMS" && grep -q "wait_id=w-test" <<<"$out"'

reg2636
python3 "$MOD" register --unit atcap --log "$TMP/atcap.log" --expect 5h50m >/dev/null
ok "#2636: an expectation near the budget is capped at the budget, not budget+margin" 'grep -q -- "--timeout-seconds 21600" "$ARMS"'

reg2636
out="$(python3 "$MOD" register --unit soon --log "$TMP/soon.log" --until "$(date -d '+40 min' '+%Y-%m-%d %H:%M')")"
ok "#2636: --until a near wall-clock time arms like a short --expect" '[ "$(arm_count)" = 1 ] && grep -qE -- "--timeout-seconds (4[0-9]{3})" "$ARMS"'

reg2636
out="$(python3 "$MOD" register --unit bare --log "$TMP/bare2.log")"
ok "#2636: bare register (no expectation) still arms with the bridge default and prints a hint" \
  '[ "$(arm_count)" = 1 ] && ! grep -q -- "--timeout-seconds" "$ARMS" && grep -q "hint:" <<<"$out" && grep -q -- "--expect" <<<"$out"'

reg2636
out="$(python3 "$MOD" register --unit both --log "$TMP/x.log" --expect 1h --until 23:59 2>&1; echo "rc=$?")"
ok "#2636: --expect and --until together is a usage error; nothing registered or armed" \
  'grep -q "rc=2" <<<"$out" && [ ! -s "$CCC_DETACHED_JOBS_REGISTRY" ] && [ "$(arm_count)" = 0 ]'
out="$(python3 "$MOD" register --unit junk --log "$TMP/x.log" --expect soonish 2>&1; echo "rc=$?")"
ok "#2636: an unparsable --expect is a usage error" 'grep -q "rc=2" <<<"$out" && grep -q "cannot parse" <<<"$out"'

reg2636
python3 "$MOD" register --unit nowait --log "$TMP/nowait.log" --expect 10m --no-wait >/dev/null
ok "#2636: --no-wait still wins over a within-budget expectation" '[ "$(arm_count)" = 0 ]'

# notify: the body-free spool record the bridge PushNotifier delivers.
export CCC_PUSH_SPOOL="$TMP/spool2636"
out="$(CCC_NODE=testnode python3 "$MOD" notify --unit sched1 --exit 0 --summary "verify rollout" --log "$TMP/s.log")"
# shellcheck disable=SC2034  # f is read via eval inside ok()
f="$(ls "$CCC_PUSH_SPOOL"/*DetachedJob-sched1-*.json 2>/dev/null | head -1)"
ok "#2636/#2710: notify queues one spool record with the notify.sh field contract plus tier (default info)" \
  '[ -n "$f" ] && python3 -c "import json,sys; r=json.load(open(sys.argv[1])); assert r[\"event\"]==\"DetachedJob\" and r[\"node\"]==\"testnode\" and r[\"dedup\"]==\"DetachedJob:sched1:0\" and \"EXIT=0\" in r[\"text\"] and \"verify rollout\" in r[\"text\"] and set(r)=={\"ts\",\"event\",\"node\",\"text\",\"dedup\",\"tier\"} and r[\"tier\"]==\"info\"" "$f"'
ok "#2636: the spool record is owner-only" '[ "$(stat -c %a "$f")" = "600" ]'
out="$(python3 "$MOD" notify --unit sched1 --exit 7 --summary "$(python3 -c 'print("y"*3000)')")"
# shellcheck disable=SC2034  # f7 is read via eval inside ok()
f7="$(ls "$CCC_PUSH_SPOOL"/*DetachedJob-sched1-*.json | wc -l)"
ok "#2636: a failing exit gets its own dedup key and the summary is capped" \
  '[ "$f7" = 2 ] && python3 -c "import json,sys,glob; rs=[json.load(open(p)) for p in glob.glob(sys.argv[1]+\"/*.json\")]; r=[x for x in rs if x[\"dedup\"]==\"DetachedJob:sched1:7\"][0]; assert r[\"text\"].startswith(\"❌\") and len(r[\"text\"]) < 400" "$CCC_PUSH_SPOOL"'
out="$(python3 "$MOD" notify --unit x 2>&1; echo "rc=$?")"
ok "#2636: notify without --exit is a usage error" 'grep -q "rc=2" <<<"$out"'

# schedule: paved path for "start later, announce on exit".
out="$(python3 "$MOD" schedule --unit sched1 --at "2026-11-03 05:00:00 Asia/Seoul" --log "$TMP/s.log" --summary "verify" --print -- bash -c 'echo hi "there"')"
ok "#2636: schedule --print emits the systemd-run timer command without launching" \
  'grep -q "^systemd-run --unit sched1 --collect --on-calendar .2026-11-03 05:00:00 Asia/Seoul. --timer-property=AccuracySec=1min" <<<"$out" && grep -q "Environment=HOME=" <<<"$out"'
ok "#2636: the wrapper appends EXIT= and calls notify with the same unit" \
  'grep -q "EXIT=\$rc" <<<"$out" && grep -q "notify --unit sched1 --log" <<<"$out" && grep -q -- "--exit" <<<"$out"'
ok "#2636: --print registers nothing and arms nothing" '[ ! -s "$CCC_DETACHED_JOBS_REGISTRY" ] || ! grep -q sched1 "$CCC_DETACHED_JOBS_REGISTRY"; [ "$(arm_count)" = 0 ]'
# The printed wrapper must be a real shell program: run it with the launcher
# stripped and see the log + marker + notice appear.
rm -f "$CCC_PUSH_SPOOL"/*.json
wrapper="$(printf '%s' "$out" | sed -E 's/^.* \/bin\/bash -c //')"
eval "/bin/bash -c $wrapper"
# shellcheck disable=SC2034  # wrc is read via eval inside ok()
wrc=$?
ok "#2636: the wrapper runs the command, writes EXIT=0 and queues the notice" \
  '[ "$wrc" = 0 ] && grep -q "hi there" "$TMP/s.log" && [ "$(tail -1 "$TMP/s.log")" = "EXIT=0" ] && ls "$CCC_PUSH_SPOOL"/*DetachedJob-sched1-*.json >/dev/null 2>&1'

SDR="$TMP/sdr.log"
write_exec_stub "$TMP/bin/systemd-run" <<'SH'
printf '%s\n' "$*" >> "${SDR_LOG:?}"
echo "Running timer as unit: ${2}.timer"
SH
reg2636
out="$(SDR_LOG="$SDR" python3 "$MOD" schedule --unit sched2 --at "05:00" --log "$TMP/s2.log" --summary "later" -- true)"
ok "#2636: schedule launches via systemd-run and registers a scheduled, notify_on_exit row without a bridge wait" \
  'grep -q "^--unit sched2 --collect --on-calendar 05:00" "$SDR" && grep -q "scheduled: unit=sched2" <<<"$out" && [ "$(arm_count)" = 0 ] && python3 -c "import json,sys; r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()][-1]; assert r[\"scheduled_at\"]==\"05:00\" and r[\"notify_on_exit\"] is True and r[\"unit\"]==\"sched2\"" "$CCC_DETACHED_JOBS_REGISTRY"'
# Before the timer fires there is no log: the job is running (timer armed), not lost.
write_exec_stub "$TMP/bin/systemctl" <<'SH'
case "$2" in sched2.timer) echo active; exit 0;; *) exit 3;; esac
SH
out="$(python3 "$MOD" sweep "$CCC_DETACHED_JOBS_REGISTRY")"
ok "#2636: a scheduled job whose timer is armed sweeps as running with its ⏰ time, not lost" \
  'grep -q "진행 중" <<<"$out" && grep -q "⏰ 05:00" <<<"$out" && ! grep -q "소식 없음" <<<"$out"'
write_exec_stub "$TMP/bin/systemctl" <<'SH'
exit 3
SH
out="$(python3 "$MOD" sweep "$CCC_DETACHED_JOBS_REGISTRY")"
ok "#2636: once neither timer nor service is active and no log exists, it is lost as before" 'grep -q "소식 없음" <<<"$out"'
out="$(python3 "$MOD" schedule --unit sched3 --at 05:00 --log "$TMP/s3.log" 2>&1; echo "rc=$?")"
ok "#2636: schedule without -- <command> is a usage error" 'grep -q "rc=2" <<<"$out"'
out="$(python3 "$MOD" schedule --unit 'bad unit;rm' --at 05:00 --log "$TMP/s3.log" --print -- true 2>&1; echo "rc=$?")"
ok "#2636: a unit name that is not a plain systemd name is rejected" 'grep -q "rc=2" <<<"$out"'

# ---- #2710: tier, --user scope, --result-file -------------------------------
spool_rec() { python3 -c "import json,sys,glob; rs=[json.load(open(p)) for p in sorted(glob.glob(sys.argv[1]+'/*DetachedJob-'+sys.argv[2]+'-*.json'))]; r=rs[-1]; print(r['tier']+'|'+r['text'])" "$CCC_PUSH_SPOOL" "$1"; }
rm -f "$CCC_PUSH_SPOOL"/*.json
python3 "$MOD" notify --unit t1 --exit 0 --tier notice >/dev/null
ok "#2710: notify --tier notice writes tier=notice" '[ "$(spool_rec t1 | cut -d"|" -f1)" = notice ]'
python3 "$MOD" notify --unit t2 --exit 4 --tier notice --fail-tier action >/dev/null
ok "#2710: a failing exit uses --fail-tier" '[ "$(spool_rec t2 | cut -d"|" -f1)" = action ]'
python3 "$MOD" notify --unit t3 --exit 0 --tier notice --fail-tier action >/dev/null
ok "#2710: a successful exit keeps --tier when --fail-tier is set" '[ "$(spool_rec t3 | cut -d"|" -f1)" = notice ]'
out="$(python3 "$MOD" notify --unit t4 --exit 0 --tier loud 2>&1; echo "rc=$?")"
ok "#2710: an unknown tier is a usage error and queues nothing" 'grep -q "rc=2" <<<"$out" && ! ls "$CCC_PUSH_SPOOL"/*DetachedJob-t4-*.json >/dev/null 2>&1'
out="$(python3 "$MOD" notify 2>&1)"
ok "#2710: notify usage says the default info tier waits for the 09:00 digest" 'grep -q "09:00" <<<"$out" && grep -q -- "--tier notice" <<<"$out"'
printf 'progress 1\n\n  7일 중   6일   통과  \n\n' > "$TMP/r.result"
python3 "$MOD" notify --unit t5 --exit 0 --result-file "$TMP/r.result" >/dev/null
ok "#2710: --result-file injects the last non-empty line, whitespace-collapsed" 'spool_rec t5 | grep -q "· 결과: 7일 중 6일 통과$"'
python3 -c 'print("z"*5000)' > "$TMP/big.result"
python3 "$MOD" notify --unit t6 --exit 0 --result-file "$TMP/big.result" >/dev/null
ok "#2710: the result is capped at 280 chars" '[ "$(spool_rec t6 | grep -o "z*" | awk "{ if (length > m) m = length } END { print m }")" = 280 ]'
python3 "$MOD" notify --unit t7 --exit 0 --result-file "$TMP/missing.result" >/dev/null
ok "#2710: a missing result file still queues the notice, without a result" 'spool_rec t7 | grep -q "EXIT=0" && ! spool_rec t7 | grep -q "결과:"'

out="$(python3 "$MOD" schedule --unit s10 --at 05:00 --log "$TMP/s10.log" --print -- true)"
ok "#2710: schedule defaults the notice to tier notice" 'grep -q -- "--tier notice" <<<"$out" && ! grep -q -- "--user" <<<"$out"'
out="$(python3 "$MOD" schedule --unit s11 --at 05:00 --log "$TMP/s11.log" --user --tier action --fail-tier critical --result-file "$TMP/s11.result" --print -- true)"
ok "#2710: schedule --user builds systemd-run --user and passes tier/fail-tier/result-file to notify" \
  'grep -q "^systemd-run --user --unit s11 --collect" <<<"$out" && grep -q -- "--tier action --fail-tier critical --result-file $TMP/s11.result" <<<"$out"'
out="$(python3 "$MOD" schedule --unit s12 --at 05:00 --log "$TMP/s12.log" --tier nope --print -- true 2>&1; echo "rc=$?")"
ok "#2710: schedule rejects an unknown tier" 'grep -q "rc=2" <<<"$out"'

# Launch path with stubs: systemctl answers only for --user, loginctl says Linger=no.
SDR2="$TMP/sdr2.log"
write_exec_stub "$TMP/bin/systemd-run" <<'SH'
printf '%s\n' "$*" >> "${SDR_LOG:?}"
SH
write_exec_stub "$TMP/bin/loginctl" <<'SH'
echo "Linger=no"
SH
write_exec_stub "$TMP/bin/systemctl" <<'SH'
[ "$1" = "--user" ] && [ "$3" = "s13.timer" ] && { echo active; exit 0; }
exit 3
SH
reg2636
out="$(SDR_LOG="$SDR2" python3 "$MOD" schedule --unit s13 --at 05:00 --log "$TMP/s13.log" --user -- true 2>&1)"
ok "#2710: schedule --user launches with --user, warns on Linger=no, and records scope=user tier=notice" \
  'grep -q "^--user --unit s13 --collect" "$SDR2" && grep -q "Linger=no" <<<"$out" && grep -q "scope=user tier=notice" <<<"$out" && python3 -c "import json,sys; r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()][-1]; assert r[\"scope\"]==\"user\" and r[\"tier\"]==\"notice\"" "$CCC_DETACHED_JOBS_REGISTRY"'
out="$(python3 "$MOD" sweep "$CCC_DETACHED_JOBS_REGISTRY")"
ok "#2710: an armed user timer sweeps as running (asked with systemctl --user), not lost" \
  'grep -q "진행 중" <<<"$out" && grep -q "⏰ 05:00" <<<"$out" && ! grep -q "소식 없음" <<<"$out"'
printf 'ran\nEXIT=0\n' > "$TMP/s13.log"
out="$(python3 "$MOD" sweep "$CCC_DETACHED_JOBS_REGISTRY")"
ok "#2710: after it fires, the EXIT marker makes it done" 'grep -q "완료됨" <<<"$out" && grep -q "EXIT=0" <<<"$out"'

reg2636
python3 "$MOD" register --unit hand --log "$TMP/hand.log" --user --no-wait >/dev/null
write_exec_stub "$TMP/bin/systemctl" <<'SH'
[ "$1" = "--user" ] && [ "$3" = "hand.service" ] && { echo active; exit 0; }
exit 3
SH
out="$(python3 "$MOD" sweep "$CCC_DETACHED_JOBS_REGISTRY")"
ok "#2710: register --user records scope=user and a live user unit is running, not lost" \
  'grep -q "진행 중" <<<"$out" && ! grep -q "소식 없음" <<<"$out"'

# ---- #2728: the bridge wait must learn the unit's scope ----------------------
reg2636
python3 "$MOD" register --unit uarm --log "$TMP/uarm.log" --user --expect 30m >/dev/null
ok "#2728: register --user hands scope=user to the bridge wait" \
  '[ "$(arm_count)" = 1 ] && grep -q -- "--unit uarm .*--scope user" "$ARMS"'
reg2636
python3 "$MOD" register --unit sarm --log "$TMP/sarm.log" --expect 30m >/dev/null
ok "#2728: a system-scope registration passes no --scope (same argv as before)" \
  '[ "$(arm_count)" = 1 ] && grep -q -- "--unit sarm " "$ARMS" && ! grep -q -- "--scope" "$ARMS"'

# Non-root + system manager auth error → hint to use --user.
write_exec_stub "$TMP/bin/systemd-run" <<'SH'
echo "Failed to start transient timer unit: Interactive authentication required." >&2
exit 1
SH
out="$(cd "$HERE" && python3 -c "
import sys, detached_jobs as d
d._geteuid = lambda: 1000
sys.exit(d.main(['schedule','--unit','s14','--at','05:00','--log','$TMP/s14.log','--','true']))" 2>&1; echo "rc=$?")"
ok "#2710: a non-root system systemd-run auth failure suggests schedule --user" 'grep -q "rc=3" <<<"$out" && grep -q "schedule --user" <<<"$out"'
out="$(cd "$HERE" && python3 -c "
import sys, detached_jobs as d
d._geteuid = lambda: 0
sys.exit(d.main(['schedule','--unit','s15','--at','05:00','--log','$TMP/s15.log','--','true']))" 2>&1; echo "rc=$?")"
ok "#2710: root gets no --user hint for the same failure" 'grep -q "rc=3" <<<"$out" && ! grep -q "schedule --user" <<<"$out"'
write_exec_stub "$TMP/bin/systemctl" <<'SH'
exit 3
SH
rm -f "$TMP/bin/loginctl"
unset ARMS_LOG CCC_BRIDGE_PYTHON CCC_DETACHED_JOBS_REGISTRY CCC_PUSH_SPOOL

# ---- #2740: a re-run of the same unit must stand on its own -----------------
# The recipe appends to the log and Step 4 re-uses the unit name, so the
# previous run's EXIT= line is still in the tail and its ack is still in the
# registry. Both used to carry over to the new run.
R2740="$TMP/reg2740.jsonl"; : > "$R2740"
write_exec_stub "$TMP/bin/systemctl" <<'SH'
exit 3
SH
printf 'run 1\nEXIT=1\n' > "$TMP/rerun.log"
python3 "$MOD" register --unit rerun --log "$TMP/rerun.log" --registry "$R2740" >/dev/null
out="$(python3 "$MOD" sweep "$R2740")"
ok "#2740 setup: the first run sweeps as done with its own exit code" 'grep -q "rerun" <<<"$out" && grep -q "EXIT=1" <<<"$out"'
ok "#2740 a finished job registered while its unit is gone keeps no offset (its marker is its own)" \
  '! grep -q "log_offset" "$R2740"'
python3 "$MOD" ack --unit rerun --registry "$R2740" >/dev/null
out="$(python3 "$MOD" sweep "$R2740")"
ok "#2740 setup: the acknowledged first run is silent" '[ -z "$out" ]'
# Run 2: same unit, same log, unit active again, nothing new written yet.
write_exec_stub "$TMP/bin/systemctl" <<'SH'
case "$*" in *rerun.service*) echo active; exit 0;; *) exit 3;; esac
SH
python3 "$MOD" register --unit rerun --log "$TMP/rerun.log" --registry "$R2740" >/dev/null
out="$(python3 "$MOD" sweep "$R2740")"
ok "#2740 the earlier ack does not hide the re-registered run" 'grep -q "rerun" <<<"$out"'
ok "#2740 the re-run is running, not done with the previous run's exit code" \
  'grep -q "진행 중" <<<"$out" && ! grep -q "완료" <<<"$out"'
ok "#2740 the registration recorded where this run's output starts" \
  '[ "$(tail -n 1 "$R2740" | python3 -c "import json,sys; print(json.load(sys.stdin).get(\"log_offset\"))")" = "$(stat -c %s "$TMP/rerun.log" 2>/dev/null || stat -f %z "$TMP/rerun.log")" ]'
printf 'run 2\nEXIT=0\n' >> "$TMP/rerun.log"
write_exec_stub "$TMP/bin/systemctl" <<'SH'
exit 3
SH
out="$(python3 "$MOD" sweep "$R2740")"
ok "#2740 the re-run's own marker ends it with its own exit code" 'grep -q "rerun" <<<"$out" && grep -q "EXIT=0" <<<"$out" && ! grep -q "EXIT=1" <<<"$out"'
# scope / scheduled_at / notify_on_exit must not leak into a plain re-registration.
: > "$R2740"
printf '{"unit":"leak","log":"%s","started_at":%s,"scope":"user","scheduled_at":"05:00","notify_on_exit":true,"acked":true}\n' "$TMP/leak.log" "$(date +%s)" > "$R2740"
printf 'fresh\n' > "$TMP/leak.log"
python3 "$MOD" register --unit leak --log "$TMP/leak.log" --registry "$R2740" >/dev/null
# shellcheck disable=SC2034  # read via eval inside ok()
merged="$(python3 - "$MOD" "$R2740" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("dj", sys.argv[1]); dj = importlib.util.module_from_spec(spec); spec.loader.exec_module(dj)
print(json.dumps(dj.load_records(sys.argv[2])[0], sort_keys=True))
PY
)"
ok "#2740 a new registration drops the previous run's acked/scope/scheduled_at/notify_on_exit" \
  '! grep -q "acked\|scope\|scheduled_at\|notify_on_exit" <<<"$merged" && grep -q "leak.log" <<<"$merged"'
# An ack is still a partial record merged over the registration.
python3 "$MOD" ack --unit leak --registry "$R2740" >/dev/null
out="$(python3 "$MOD" sweep "$R2740")"
ok "#2740 an ack after the new registration still closes it" '[ -z "$out" ]'
# A truncated or replaced log is read whole again.
printf 'EXIT=7\n' > "$TMP/short.log"
# shellcheck disable=SC2034  # read via eval inside ok()
code="$(python3 - "$MOD" "$TMP/short.log" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("dj", sys.argv[1]); dj = importlib.util.module_from_spec(spec); spec.loader.exec_module(dj)
print(dj.read_exit_marker(sys.argv[2], 5000), dj.read_exit_marker(sys.argv[2], 7), dj.read_exit_marker(sys.argv[2], "x"))
PY
)"
ok "#2740 read_exit_marker: shorter-than-offset log read whole, marker before the offset ignored, bad offset fails closed" '[ "$code" = "7 None None" ]'

# ---- #2778: a re-scheduled job is alive while its timer is armed -------------
# `schedule` under the same unit and log leaves the previous run's output in
# the log. That log is long quiet, so the sweep took the "log exists" branch,
# which asked only about the .service — and called an armed timer lost.
R2778="$TMP/reg2778.jsonl"
write_exec_stub "$TMP/bin/systemd-run" <<'SH'
exit 0
SH
write_exec_stub "$TMP/bin/loginctl" <<'SH'
echo "Linger=yes"
SH
old_log() { printf 'yesterday\nEXIT=0\n' > "$1"; touch -d '2 hours ago' "$1" 2>/dev/null || touch -t 200001010000 "$1"; }

: > "$R2778"; old_log "$TMP/resched.log"
write_exec_stub "$TMP/bin/systemctl" <<'SH'
[ "$1" = "is-active" ] && [ "$2" = "resched.timer" ] && { echo active; exit 0; }
exit 3
SH
python3 "$MOD" schedule --unit resched --at 05:00 --log "$TMP/resched.log" --registry "$R2778" -- true >/dev/null 2>&1
ok "#2778 setup: the re-schedule recorded scheduled_at and skips the previous run's marker" \
  'python3 -c "import json,sys; r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()][-1]; assert r[\"scheduled_at\"]==\"05:00\" and r[\"log_offset\"]>0" "$R2778"'
out="$(python3 "$MOD" sweep "$R2778")"
ok "#2778: a re-scheduled job with a long-quiet log is running while its timer is armed" \
  'grep -q "진행 중" <<<"$out" && grep -q "⏰ 05:00" <<<"$out" && ! grep -q "소식 없음" <<<"$out"'

: > "$R2778"; old_log "$TMP/uresched.log"
write_exec_stub "$TMP/bin/systemctl" <<'SH'
[ "$1" = "--user" ] && [ "$3" = "uresched.timer" ] && { echo active; exit 0; }
exit 3
SH
python3 "$MOD" schedule --unit uresched --at 05:00 --log "$TMP/uresched.log" --user --registry "$R2778" -- true >/dev/null 2>&1
out="$(python3 "$MOD" sweep "$R2778")"
ok "#2778: the same for a --user timer, asked of systemctl --user" \
  'grep -q "진행 중" <<<"$out" && ! grep -q "소식 없음" <<<"$out"'

# The timer fired and is gone, the job wrote its own marker after the offset.
printf 'today\nEXIT=3\n' >> "$TMP/uresched.log"
write_exec_stub "$TMP/bin/systemctl" <<'SH'
exit 3
SH
out="$(python3 "$MOD" sweep "$R2778")"
ok "#2778: once it fires, the marker written after the offset ends it with its own exit code" \
  'grep -q "uresched" <<<"$out" && grep -q "EXIT=3" <<<"$out" && ! grep -q "EXIT=0" <<<"$out"'

# Timer gone and nothing written since: still lost, as before.
: > "$R2778"; old_log "$TMP/gone.log"
write_exec_stub "$TMP/bin/systemctl" <<'SH'
[ "$1" = "is-active" ] && [ "$2" = "gone.timer" ] && { echo active; exit 0; }
exit 3
SH
python3 "$MOD" schedule --unit gone --at 05:00 --log "$TMP/gone.log" --registry "$R2778" -- true >/dev/null 2>&1
write_exec_stub "$TMP/bin/systemctl" <<'SH'
exit 3
SH
out="$(python3 "$MOD" sweep "$R2778")"
ok "#2778: a scheduled job whose timer is gone with no new marker is still lost" 'grep -q "소식 없음" <<<"$out"'

# A record without scheduled_at never asks about a timer, even one that is up.
: > "$R2778"; printf 'started\n' > "$TMP/plain2778.log"
touch -d '2 hours ago' "$TMP/plain2778.log" 2>/dev/null || touch -t 200001010000 "$TMP/plain2778.log"
ASKED="$TMP/asked2778.log"; : > "$ASKED"
write_exec_stub "$TMP/bin/systemctl" <<'SH'
printf '%s\n' "$*" >> "${ASKED_LOG:?}"
case "$*" in *plain2778.timer*) echo active; exit 0;; esac
exit 3
SH
ASKED_LOG="$ASKED" python3 "$MOD" register --unit plain2778 --log "$TMP/plain2778.log" --registry "$R2778" >/dev/null
: > "$ASKED"
out="$(ASKED_LOG="$ASKED" python3 "$MOD" sweep "$R2778")"
ok "#2778: a record without scheduled_at is judged as before (lost) and no timer is asked about" \
  'grep -q "소식 없음" <<<"$out" && grep -q "plain2778.service" "$ASKED" && ! grep -q "\.timer" "$ASKED"'

# ---- #2827: a scheduled job's week runs from its due time ---------------------
# The age cap measured every record from started_at, so a reminder armed 19 days
# ahead left the sweep after 7 — and when a reboot then took its transient timer
# nothing said `lost`. The cap now starts at the due time.
ok "#2827: calendar_instant reads one-shot instants (zone, UTC, local) and refuses recurring specs" \
  'py_dj "import detached_jobs as d; from datetime import datetime; from zoneinfo import ZoneInfo
assert d.calendar_instant(\"2026-12-02 09:00:00 Asia/Seoul\") == datetime(2026,12,2,9,0,0,tzinfo=ZoneInfo(\"Asia/Seoul\")).timestamp()
assert d.calendar_instant(\"2026-12-02  09:00 UTC\") == 1796202000
assert d.calendar_instant(\"2026-12-02 09:00\") == datetime(2026,12,2,9,0).astimezone().timestamp()
for bad in [\"05:00\", \"Mon *-*-* 05:00\", \"2026-12-02 09:00 Not/AZone\", \"2026-13-40 09:00\", \"\"]:
    assert d.calendar_instant(bad) is None, bad"'

R2827="$TMP/reg2827.jsonl"; NOW="$(date +%s)"; DAY=86400
write_exec_stub "$TMP/bin/systemctl" <<'SH'
exit 3
SH
# Registered 19 days ago, due 12 days from now, timer gone (reboot): must stay visible as lost.
printf '{"unit":"far2827","log":"%s","started_at":%s,"scheduled_at":"2099-01-01 00:00:00 UTC","scheduled_epoch":%s,"notify_on_exit":true}\n' \
  "$TMP/far2827.log" "$((NOW - 19*DAY))" "$((NOW + 12*DAY))" > "$R2827"
out="$(python3 "$MOD" sweep "$R2827")"
ok "#2827: a job registered 19 days ago and due in 12 is still swept — as lost once its timer is gone" \
  'grep -q "far2827" <<<"$out" && grep -q "소식 없음" <<<"$out"'

# A row written before scheduled_epoch existed: the due time comes from its spec.
due_spec="$(TZ=UTC date -d "@$((NOW + 12*DAY))" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || python3 -c "import time,sys;print(time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(int(sys.argv[1]))))" "$((NOW + 12*DAY))")"
printf '{"unit":"legacy2827","log":"%s","started_at":%s,"scheduled_at":"%s UTC","notify_on_exit":true}\n' \
  "$TMP/legacy2827.log" "$((NOW - 19*DAY))" "$due_spec" > "$R2827"
out="$(python3 "$MOD" sweep "$R2827")"
ok "#2827: a legacy row with no scheduled_epoch takes its due time from scheduled_at" 'grep -q "legacy2827" <<<"$out"'

# Due 8 days ago and never acked: archived noise, as any other week-old record.
printf '{"unit":"past2827","log":"%s","started_at":%s,"scheduled_at":"x","scheduled_epoch":%s}\n' \
  "$TMP/past2827.log" "$((NOW - 9*DAY))" "$((NOW - 8*DAY))" > "$R2827"
out="$(python3 "$MOD" sweep "$R2827")"
ok "#2827: a scheduled job due more than a week ago is dropped" '[ -z "$out" ]'

# A recurring spec has no single due time: the started_at rule still applies.
printf '{"unit":"recur2827","log":"%s","started_at":%s,"scheduled_at":"Mon *-*-* 05:00"}\n' \
  "$TMP/recur2827.log" "$((NOW - 8*DAY))" > "$R2827"
out="$(python3 "$MOD" sweep "$R2827")"
ok "#2827: a recurring spec keeps the started_at age rule" '[ -z "$out" ]'

# schedule writes the due epoch for a one-shot instant, and none for a recurring one.
write_exec_stub "$TMP/bin/systemd-run" <<'SH'
exit 0
SH
: > "$R2827"
python3 "$MOD" schedule --unit due2827 --at "2030-01-02 03:04:05 Asia/Seoul" --log "$TMP/due2827.log" --registry "$R2827" -- true >/dev/null 2>&1
python3 "$MOD" schedule --unit rec2827 --at "05:00" --log "$TMP/rec2827.log" --registry "$R2827" -- true >/dev/null 2>&1
ok "#2827: schedule records scheduled_epoch for a one-shot --at and omits it for a recurring one" \
  'python3 -c "import json,sys; from datetime import datetime; from zoneinfo import ZoneInfo
rows={r[\"unit\"]:r for r in (json.loads(l) for l in open(sys.argv[1]) if l.strip())}
assert rows[\"due2827\"][\"scheduled_epoch\"]==int(datetime(2030,1,2,3,4,5,tzinfo=ZoneInfo(\"Asia/Seoul\")).timestamp())
assert \"scheduled_epoch\" not in rows[\"rec2827\"]" "$R2827"'

# ---- #2827 (B): export / restore of scheduled timers around a reboot ----------
# A transient timer lives under /run and a reboot erases it. `schedule` now
# saves the argv it launched in the registry row; `restore-timers` replays it
# for every job whose timer is gone, and `export-timers` is the pre-reboot
# snapshot the re-created units are compared with. The fake systemd below keeps
# one file per armed timer, so "reboot" is `rm` and nothing touches the host.
FAKE="$TMP/fake-systemd"; mkdir -p "$FAKE/system" "$FAKE/user" || exit 1
RB="$TMP/reg-restore.jsonl"; RBLOG="$TMP/restore-sdr.log"; : > "$RB"; : > "$RBLOG"
export FAKE_SYSTEMD="$FAKE" SDR_LOG="$RBLOG"
write_exec_stub "$TMP/bin/systemd-run" <<'SH'
[ -n "${SDR_FAIL:-}" ] && { echo "Failed to start transient timer unit: boom" >&2; exit 1; }
{ printf '%q ' "$@"; echo; } >> "${SDR_LOG:?}"
scope=system; [ "$1" = "--user" ] && { scope=user; shift; }
unit=""; cal=""; wd=""; envs=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --unit) unit="$2"; shift 2 ;;
    --on-calendar) cal="$2"; shift 2 ;;
    --property=Environment=*) envs+=("${1#--property=Environment=}"); shift ;;
    --property=WorkingDirectory=*) wd="${1#--property=WorkingDirectory=}"; shift ;;
    --collect|--timer-property=*) shift ;;
    *) break ;;
  esac
done
{
  echo "[Timer]"; echo "OnCalendar=$cal"; echo "[Service]"
  for e in "${envs[@]}"; do echo "Environment=\"$e\""; done
  [ -n "$wd" ] && echo "WorkingDirectory=$wd"
  echo "ExecStart="; echo "ExecStart=$*"
} > "${FAKE_SYSTEMD:?}/$scope/$unit.cat"
SH
write_exec_stub "$TMP/bin/systemctl" <<'SH'
scope=system; [ "$1" = "--user" ] && { scope=user; shift; }
case "$1" in
  is-active)
    case "$2" in
      *.timer) [ -f "${FAKE_SYSTEMD:?}/$scope/${2%.timer}.cat" ] && { echo active; exit 0; } ;;
      *.service) [ -f "${FAKE_SYSTEMD:?}/$scope/${2%.service}.running" ] && { echo active; exit 0; } ;;
    esac
    exit 3 ;;
  cat)
    shift; [ "$1" = "--no-pager" ] && shift
    [ -f "${FAKE_SYSTEMD:?}/$scope/${1%.timer}.cat" ] || exit 1
    cat "${FAKE_SYSTEMD:?}/$scope/${1%.timer}.cat"; exit 0 ;;
esac
exit 3
SH
write_exec_stub "$TMP/bin/loginctl" <<'SH'
echo "Linger=yes"
SH
reboot_fake() { rm -f "$FAKE"/system/*.cat "$FAKE"/user/*.cat; }
armed_count() { find "$FAKE/system" "$FAKE/user" -name '*.cat' | wc -l | tr -d ' '; }
launches() { grep -c -- "--unit $1 " "$RBLOG" || true; }
# jpy FILE EXPR — EXPR is python over the snapshot `d`: T / S map unit -> timers / skipped row.
jpy() {
  python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
T = {t["unit"]: t for t in d["timers"]}; S = {t["unit"]: t for t in d["skipped"]}
assert eval(sys.argv[2]), sys.argv[2]' "$1" "$2" 2>/dev/null
}
FUTURE="2099-01-02 03:04:05 Asia/Seoul"

# The environment at schedule time is deliberately not the one restore runs in.
HOME=/home/sched PATH="$TMP/bin:/sched/bin:$PATH" python3 "$MOD" schedule --unit rb-a --at "$FUTURE" \
  --log "$TMP/rb-a.log" --summary "check the rollout" --workdir "$TMP" --tier action --fail-tier critical \
  --result-file "$TMP/rb-a.result" --registry "$RB" -- bash -c 'echo "hi there" "$HOME"' >/dev/null 2>&1
HOME=/home/sched python3 "$MOD" schedule --unit rb-u --at "$FUTURE" --log "$TMP/rb-u.log" --user --registry "$RB" -- true >/dev/null 2>&1
python3 "$MOD" schedule --unit rb-r --at "05:00" --log "$TMP/rb-r.log" --registry "$RB" -- true >/dev/null 2>&1
ok "#2827B: schedule saves the exact systemd-run argv it launched in the registry row" \
  'python3 -c "import json,sys,shlex
rows={r[\"unit\"]:r for r in (json.loads(l) for l in open(sys.argv[1]) if l.strip())}
logged=[shlex.split(l) for l in open(sys.argv[2]) if l.strip()]
a=rows[\"rb-a\"][\"launch_argv\"]; u=rows[\"rb-u\"][\"launch_argv\"]
assert a[:6]==[\"systemd-run\",\"--unit\",\"rb-a\",\"--collect\",\"--on-calendar\",sys.argv[3]], a[:6]
assert \"--property=Environment=HOME=/home/sched\" in a and any(x.startswith(\"--property=Environment=PATH=\") and \"/sched/bin\" in x for x in a)
assert u[:2]==[\"systemd-run\",\"--user\"] and a[1:]==logged[0] and u[1:]==logged[1]" "$RB" "$RBLOG" "$FUTURE"'

# Scheduled jobs that are no longer waiting: finished, acknowledged, already started.
for u in rb-done rb-acked rb-running; do
  python3 "$MOD" schedule --unit "$u" --at "$FUTURE" --log "$TMP/$u.log" --registry "$RB" -- true >/dev/null 2>&1
done
printf 'ran\nEXIT=0\n' > "$TMP/rb-done.log"
python3 "$MOD" ack --unit rb-acked --registry "$RB" >/dev/null
: > "$FAKE/system/rb-running.running"
python3 "$MOD" register --unit rb-plain --log "$TMP/rb-plain.log" --registry "$RB" >/dev/null
n_before="$(wc -l < "$RBLOG")"
python3 "$MOD" export-timers --registry "$RB" > "$TMP/snap0.json" 2>"$TMP/snap0.err"; rc=$?
ok "#2827B: export-timers lists only the scheduled jobs still waiting (not done, acked, started, or plain register)" \
  '[ "$rc" = 0 ] && [ ! -s "$TMP/snap0.err" ] && jpy "$TMP/snap0.json" "sorted(T)==[\"rb-a\",\"rb-r\",\"rb-u\"] and not S and d[\"version\"]==1"'
ok "#2827B: an exported row carries time, scope, the launch argv and the live unit's values" \
  'jpy "$TMP/snap0.json" "T[\"rb-a\"][\"state\"]==\"armed\" and T[\"rb-a\"][\"scheduled_at\"]==\"$FUTURE\" and T[\"rb-a\"][\"scope\"]==\"system\" and T[\"rb-u\"][\"scope\"]==\"user\" and T[\"rb-a\"][\"launch_argv\"][0]==\"systemd-run\" and T[\"rb-a\"][\"live\"][\"OnCalendar\"]==[\"$FUTURE\"] and \"HOME=/home/sched\" in T[\"rb-a\"][\"live\"][\"Environment\"] and T[\"rb-a\"][\"live\"][\"WorkingDirectory\"]==[\"$TMP\"] and len(T[\"rb-a\"][\"live\"][\"ExecStart\"])==1"'
ok "#2827B: export-timers launches nothing" '[ "$(wc -l < "$RBLOG")" = "$n_before" ]'

# A row written before launch_argv existed, its timer still armed: the export
# must say before the reboot that this one cannot be brought back.
printf '{"unit":"rb-legacy","log":"%s","started_at":%s,"scheduled_at":"%s","notify_on_exit":true,"tier":"notice"}\n' \
  "$TMP/rb-legacy.log" "$NOW" "$FUTURE" >> "$RB"
printf '[Timer]\nOnCalendar=%s\n[Service]\nEnvironment="HOME=/root"\nExecStart=\nExecStart="/bin/bash" "-c" "true"\n' "$FUTURE" > "$FAKE/system/rb-legacy.cat"
out="$(python3 "$MOD" export-timers --registry "$RB" --out "$TMP/snap.json" 2>"$TMP/snap.err")"; rc=$?
ok "#2827B: a row without a launch definition is exported under skipped, with its live values, and exit 1" \
  '[ "$rc" = 1 ] && grep -q "not restorable: rb-legacy (armed)" "$TMP/snap.err" && jpy "$TMP/snap.json" "sorted(T)==[\"rb-a\",\"rb-r\",\"rb-u\"] and sorted(S)==[\"rb-legacy\"] and \"no launch definition\" in S[\"rb-legacy\"][\"reason\"] and S[\"rb-legacy\"][\"live\"][\"ExecStart\"]"'
ok "#2827B: --out writes the snapshot owner-only and prints the counts" \
  '[ "$(stat -c %a "$TMP/snap.json" 2>/dev/null || stat -f %Lp "$TMP/snap.json")" = 600 ] && grep -q "exported: timers=3 skipped=1 -> $TMP/snap.json" <<<"$out"'

# Nothing is missing yet: restore must not launch a second copy of an armed timer.
out="$(python3 "$MOD" restore-timers --registry "$RB" 2>&1)"; rc=$?
ok "#2827B: restore-timers leaves armed timers alone (kept, nothing launched, exit 0)" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$RBLOG")" = "$n_before" ] && grep -q "restore-timers: restored=0 kept=4$" <<<"$out" && grep -q "^kept	rb-legacy	.*a reboot would lose it" <<<"$out"'

# ---- reboot: every transient timer is gone -----------------------------------
PAST_AT="$(python3 -c "import time; print(time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 7200)))") UTC"
python3 "$MOD" schedule --unit rb-past --at "$PAST_AT" --log "$TMP/rb-past.log" --registry "$RB" -- true >/dev/null 2>&1
reboot_fake
cp "$RB" "$TMP/reg-restore.before"; n_before="$(wc -l < "$RBLOG")"
python3 "$MOD" export-timers --registry "$RB" > "$TMP/snap-after.json" 2>/dev/null
ok "#2827B: after the reboot the export shows the timers missing, the past one missed, the legacy one unrestorable" \
  'jpy "$TMP/snap-after.json" "sorted(T)==[\"rb-a\",\"rb-r\",\"rb-u\"] and all(t[\"state\"]==\"missing\" and \"live\" not in t for t in T.values()) and S[\"rb-past\"][\"state\"]==\"missed\" and S[\"rb-legacy\"][\"state\"]==\"unrestorable\""'

for flag in --dry-run --print; do
  out="$(HOME=/home/restorer python3 "$MOD" restore-timers --registry "$RB" --from "$TMP/snap.json" "$flag" 2>&1)"; rc=$?
  ok "#2827B: restore-timers $flag prints what it would launch and changes nothing" \
    '[ "$rc" = 1 ] && [ "$(grep -c "^would-restore	" <<<"$out")" = 3 ] && grep -q "^would-restore	rb-a	$FUTURE	system	systemd-run --unit rb-a --collect --on-calendar" <<<"$out" && grep -q "would-restore=3 .*(dry run: nothing launched)" <<<"$out" && [ "$(wc -l < "$RBLOG")" = "$n_before" ] && [ "$(armed_count)" = 0 ] && cmp -s "$RB" "$TMP/reg-restore.before"'
done

# The real thing, from a shell whose HOME/PATH are not the ones the job was scheduled with.
out="$(HOME=/home/restorer python3 "$MOD" restore-timers --registry "$RB" --from "$TMP/snap.json" 2>&1)"; rc=$?
ok "#2827B: restore-timers re-creates each missing timer by replaying the saved argv — same time, command, environment, scope" \
  '[ "$(launches rb-a)" = 2 ] && [ "$(launches rb-u)" = 2 ] && [ "$(launches rb-r)" = 2 ] && [ "$(grep -- "--unit rb-a " "$RBLOG" | sort -u | wc -l)" = 1 ] && [ "$(grep -- "--unit rb-u " "$RBLOG" | sort -u | wc -l)" = 1 ] && grep -q "^--user --unit rb-u " "$RBLOG" && ! grep -q "/home/restorer" "$RBLOG" && [ -f "$FAKE/system/rb-a.cat" ] && [ -f "$FAKE/user/rb-u.cat" ] && [ -f "$FAKE/system/rb-r.cat" ]'
ok "#2827B: the re-created unit is compared with the snapshot's values and reported as matching" \
  'grep -q "^restored	rb-a	$FUTURE	system	time, command and environment match the snapshot$" <<<"$out" && grep -q "^restored	rb-u	$FUTURE	user	" <<<"$out"'
ok "#2827B: a job whose time has passed is reported missed and not re-armed; exit 1" \
  '[ "$rc" = 1 ] && grep -q "^missed	rb-past	.*passed 2h.*not re-armed" <<<"$out" && [ "$(launches rb-past)" = 1 ] && [ ! -f "$FAKE/system/rb-past.cat" ]'
ok "#2827B: a row without the new fields is reported unrestorable, without a crash or a launch" \
  'grep -q "^unrestorable	rb-legacy	.*no launch definition" <<<"$out" && [ "$(launches rb-legacy)" = 0 ] && grep -q "restore-timers: restored=3 kept=0 missed=1 unrestorable=1$" <<<"$out"'
ok "#2827B: restore-timers does not write the registry (started_at and log_offset stay the original run's)" 'cmp -s "$RB" "$TMP/reg-restore.before"'

n_before="$(wc -l < "$RBLOG")"
out="$(python3 "$MOD" restore-timers --registry "$RB" --from "$TMP/snap.json" 2>&1)"
ok "#2827B: a second restore-timers is a no-op — the timers that exist are kept, not duplicated" \
  '[ "$(wc -l < "$RBLOG")" = "$n_before" ] && grep -q "restored=0 kept=3 " <<<"$out" && grep -q "^kept	rb-a	.*match the snapshot" <<<"$out"'

# By value, not by text: the same environment on one line is still the same unit...
{
  grep -v '^Environment=' "$FAKE/system/rb-a.cat"
  printf 'Environment=%s\n' "$(sed -n 's/^Environment=//p' "$FAKE/system/rb-a.cat" | tr '\n' ' ')"
} > "$TMP/rb-a.cat" && cp "$TMP/rb-a.cat" "$FAKE/system/rb-a.cat"
out="$(python3 "$MOD" restore-timers --registry "$RB" --from "$TMP/snap.json" 2>&1)"
ok "#2827B: two Environment= assignments on one line compare equal to one per line" \
  '[ "$(grep -c "^Environment=" "$FAKE/system/rb-a.cat")" = 1 ] && grep -q "^kept	rb-a	.*match the snapshot" <<<"$out"'
# ...and a timer re-created by hand without its environment is not.
grep -v '^Environment=' "$TMP/rb-a.cat" > "$FAKE/system/rb-a.cat"
out="$(python3 "$MOD" restore-timers --registry "$RB" --from "$TMP/snap.json" 2>&1)"; rc=$?
ok "#2827B: an armed timer whose environment is not the snapshot's is reported as differs" \
  '[ "$rc" = 1 ] && grep -q "^differs	rb-a	.*differs from the snapshot in Environment$" <<<"$out" && grep -q " differs=1$" <<<"$out"'

# A snapshot is a reference, not an override: a job scheduled again since the
# export is restored from the registry's newer row.
NEWER="2099-06-07 08:09:10 Asia/Seoul"
python3 "$MOD" schedule --unit rb-a --at "$NEWER" --log "$TMP/rb-a.log" --registry "$RB" -- true >/dev/null 2>&1
reboot_fake
out="$(python3 "$MOD" restore-timers --registry "$RB" --from "$TMP/snap.json" 2>&1)"
ok "#2827B: a job re-scheduled after the export is restored at its new time, without a false differs" \
  'grep -q "^restored	rb-a	$NEWER	system$" <<<"$out" && grep -q "^OnCalendar=$NEWER$" "$FAKE/system/rb-a.cat"'
# With the registry gone, the snapshot rows are the jobs.
reboot_fake
out="$(python3 "$MOD" restore-timers --registry "$TMP/no-such-registry.jsonl" --from "$TMP/snap.json" 2>&1)"
ok "#2827B: when the registry no longer knows a unit, the snapshot row restores it" \
  'grep -q "^restored	rb-a	$FUTURE	system	" <<<"$out" && grep -q "^restored	rb-u	$FUTURE	user	" <<<"$out" && grep -q "^OnCalendar=$FUTURE$" "$FAKE/system/rb-a.cat"'

# A launch definition that does not belong to its row is never run.
reboot_fake; RBAD="$TMP/reg-bad.jsonl"
# shellcheck disable=SC2034  # read via eval inside ok()
n_before="$(wc -l < "$RBLOG")"
{
  printf '{"unit":"rb-x1","log":"%s","started_at":%s,"scheduled_at":"05:00","launch_argv":["/bin/sh","-c","touch %s"]}\n' "$TMP/x1.log" "$NOW" "$TMP/pwned"
  printf '{"unit":"rb-x2","log":"%s","started_at":%s,"scheduled_at":"05:00","launch_argv":["systemd-run","--unit","other","--collect","--on-calendar","05:00","true"]}\n' "$TMP/x2.log" "$NOW"
  printf '{"unit":"rb-x3","log":"%s","started_at":%s,"scheduled_at":"05:00","launch_argv":["systemd-run","--user","--unit","rb-x3","--collect","--on-calendar","05:00","true"]}\n' "$TMP/x3.log" "$NOW"
  printf '{"unit":"rb-x4","log":"%s","started_at":%s,"scheduled_at":"05:00","launch_argv":["systemd-run","--unit","rb-x4","--collect","--on-calendar","06:00","true"]}\n' "$TMP/x4.log" "$NOW"
  printf '{"unit":"rb-x5","log":"%s","started_at":%s,"scheduled_at":"05:00","launch_argv":"systemd-run --unit rb-x5"}\n' "$TMP/x5.log" "$NOW"
} > "$RBAD"
out="$(python3 "$MOD" restore-timers --registry "$RBAD" 2>&1)"; rc=$?
ok "#2827B: an argv that is not schedule's, or names another unit, scope or time, is unrestorable and never run" \
  '[ "$rc" = 1 ] && [ "$(grep -c "^unrestorable	rb-x[1-5]	" <<<"$out")" = 5 ] && [ "$(wc -l < "$RBLOG")" = "$n_before" ] && [ ! -e "$TMP/pwned" ]'

# Failures are said out loud.
out="$(SDR_FAIL=1 python3 "$MOD" restore-timers --registry "$RB" 2>&1)"
# shellcheck disable=SC2034  # read via eval inside ok()
rc=$?
ok "#2827B: a systemd-run that refuses is reported failed with exit 1" \
  '[ "$rc" = 1 ] && grep -q "^failed	rb-a	" <<<"$out" && grep -q "systemd-run rc=1" <<<"$out"'
printf 'not json\n' > "$TMP/bad-snap.json"; printf '{"version":99,"timers":[]}\n' > "$TMP/old-snap.json"
chmod 600 "$TMP/bad-snap.json" "$TMP/old-snap.json"
out="$(for snap in bad-snap old-snap absent; do python3 "$MOD" restore-timers --registry "$RB" --from "$TMP/$snap.json" 2>&1; echo "rc=$?"; done)"
ok "#2827B: an unreadable, foreign or missing --from snapshot is a usage error and launches nothing" \
  '[ "$(grep -c "^rc=2$" <<<"$out")" = 3 ] && grep -q "not an export-timers snapshot" <<<"$out" && [ "$(armed_count)" = 0 ]'
# A snapshot row's argv can be run, so the file must be this user's alone.
cp "$TMP/snap.json" "$TMP/loose-snap.json"; chmod 664 "$TMP/loose-snap.json"
out="$(python3 "$MOD" restore-timers --registry "$TMP/no-such-registry.jsonl" --from "$TMP/loose-snap.json" 2>&1)"
# shellcheck disable=SC2034  # read via eval inside ok()
rc=$?
ok "#2827B: a group-writable --from snapshot is refused and launches nothing" \
  '[ "$rc" = 2 ] && grep -q "not group/world-writable" <<<"$out" && [ "$(armed_count)" = 0 ]'
# The sweep fails open on an unexpected error; an operator-run restore must not.
crash() {
  python3 - "$MOD" "$@" <<'PY'
import json, runpy, sys
def boom(*_a, **_k):
    raise RuntimeError("boom")
json.loads = boom
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
PY
}
out="$(crash sweep "$RB" 2>&1; echo "rc=$?"; crash restore-timers --registry "$RB" 2>&1; echo "rc=$?"; crash export-timers --registry "$RB" 2>&1; echo "rc=$?")"
ok "#2827B: an unexpected error stays silent for sweep (rc 0) and is loud for export/restore (rc 1)" \
  '[ "$(sed -n 1p <<<"$out")" = "rc=0" ] && grep -q "restore-timers failed: RuntimeError" <<<"$out" && grep -q "export-timers failed: RuntimeError" <<<"$out" && [ "$(grep -c "^rc=1$" <<<"$out")" = 2 ]'
unset FAKE_SYSTEMD SDR_LOG


# ---- #2827 proposal A: schedule --durable (persistent unit files) -----------
# Persistent .service + .timer written to a unit dir (CCC_DETACHED_UNIT_DIR in
# tests), enabled, and removed by the wrapper after the run. The fake systemctl
# logs its argv and calls a timer "active" while its unit file exists.
UD="$TMP/units"; mkdir -p "$UD" || exit 1
DLOG="$TMP/durable-systemctl.log"; : > "$DLOG"
DREG="$TMP/reg-durable.jsonl"; : > "$DREG"
export CCC_DETACHED_UNIT_DIR="$UD" DURABLE_CTL_LOG="$DLOG"
write_exec_stub "$TMP/bin/systemctl" <<'SH'
{ printf '%s ' "$@"; echo; } >> "${DURABLE_CTL_LOG:?}"
[ "$1" = "--user" ] && shift
case "$1" in
  daemon-reload|disable) exit 0 ;;
  enable) [ -n "${DURABLE_ENABLE_FAIL:-}" ] && { echo "Failed to enable unit: boom" >&2; exit 1; }; exit 0 ;;
  is-active)
    case "$2" in *.timer) [ -f "${CCC_DETACHED_UNIT_DIR:?}/$2" ] && { echo active; exit 0; } ;; esac
    exit 3 ;;
esac
exit 3
SH
DFUT="2099-01-02 03:04:05 Asia/Seoul"

out="$(HOME=/home/sched python3 "$MOD" schedule --unit dur-p --at "$DFUT" --log "$TMP/dur-p.log" --summary "night check" --durable --print -- bash -c 'echo "x $HOME" 100%')"; rc=$?
ok "#2827A: --durable --print shows both unit files and the systemctl steps, writes nothing" \
  '[ "$rc" = 0 ] && grep -q "^### $UD/dur-p.service" <<<"$out" && grep -q "^### $UD/dur-p.timer" <<<"$out" \
   && grep -q "^OnCalendar=$DFUT$" <<<"$out" && grep -q "^Persistent=true$" <<<"$out" && grep -q "^Type=oneshot$" <<<"$out" \
   && grep -q "^WantedBy=timers.target$" <<<"$out" && grep -q "enable --now dur-p.timer" <<<"$out" \
   && [ -z "$(ls -A "$UD")" ] && [ ! -s "$DLOG" ]'
ok "#2827A: --durable escapes systemd \$ and % in ExecStart (rc and 100% reach bash intact)" \
  'grep -q "rc=\$\$?" <<<"$out" && grep -q "100%%" <<<"$out" && grep -q "Environment=\"HOME=/home/sched\"" <<<"$out"'

out="$(python3 "$MOD" schedule --unit dur-r --at "05:00" --log "$TMP/dur-r.log" --durable -- true 2>&1; echo "rc=$?")"
ok "#2827A: --durable refuses a recurring/relative time" 'grep -q "rc=2" <<<"$out" && grep -q "absolute time" <<<"$out"'
out="$(python3 "$MOD" schedule --unit dur-o --at "2001-01-01 00:00 UTC" --log "$TMP/dur-o.log" --durable -- true 2>&1; echo "rc=$?")"
ok "#2827A: --durable refuses a time already in the past" 'grep -q "rc=2" <<<"$out" && grep -q "in the past" <<<"$out"'

out="$(HOME=/home/sched python3 "$MOD" schedule --unit dur-a --at "$DFUT" --log "$TMP/dur-a.log" --summary "night check" --tier action --durable --registry "$DREG" -- bash -c 'echo ran "$HOME"; exit 4' 2>&1)"; rc=$?
ok "#2827A: --durable installs the pair, reloads, enables the timer, and records a durable row without launch_argv" \
  '[ "$rc" = 0 ] && [ -f "$UD/dur-a.service" ] && [ -f "$UD/dur-a.timer" ] \
   && grep -qx "daemon-reload " "$DLOG" && grep -qx "enable --now dur-a.timer " "$DLOG" \
   && python3 -c "import json,sys
r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()][-1]
assert r[\"unit\"]==\"dur-a\" and r[\"durable\"] is True and \"launch_argv\" not in r and r[\"tier\"]==\"action\"
assert r[\"scheduled_epoch\"]>0 and r[\"unit_files\"]==sorted([sys.argv[2]+\"/dur-a.service\",sys.argv[2]+\"/dur-a.timer\"])" "$DREG" "$UD"'

before="$(cat "$UD/dur-a.timer")"
out="$(python3 "$MOD" schedule --unit dur-a --at "$DFUT" --log "$TMP/dur-a.log" --durable --registry "$DREG" -- true 2>&1; echo "rc=$?")"
ok "#2827A: --durable never overwrites an existing unit file" 'grep -q "rc=3" <<<"$out" && grep -q "already exists" <<<"$out" && [ "$(cat "$UD/dur-a.timer")" = "$before" ]'

out="$(DURABLE_ENABLE_FAIL=1 python3 "$MOD" schedule --unit dur-f --at "$DFUT" --log "$TMP/dur-f.log" --durable --registry "$DREG" -- true 2>&1; echo "rc=$?")"
ok "#2827A: a failed enable removes the files it wrote and registers nothing" \
  'grep -q "rc=3" <<<"$out" && [ ! -e "$UD/dur-f.service" ] && [ ! -e "$UD/dur-f.timer" ] && ! grep -q "\"dur-f\"" "$DREG"'

# Reboot-safety bookkeeping: an armed durable timer is reported armed and
# never replayed; one whose files vanished is reported, not re-armed.
python3 "$MOD" export-timers --registry "$DREG" > "$TMP/dsnap.json" 2>/dev/null
ok "#2827A: export-timers lists an armed durable timer as armed and marks it durable (nothing to replay)" \
  'python3 -c "import json,sys
d=json.load(open(sys.argv[1])); t={x[\"unit\"]:x for x in d[\"timers\"]}[\"dur-a\"]
assert t[\"state\"]==\"armed\" and t[\"durable\"] is True and \"launch_argv\" not in t and len(t[\"unit_files\"])==2" "$TMP/dsnap.json"'

# Run the generated ExecStart the way systemd would: unquote, $$->$ and %%->%.
python3 - "$UD/dur-a.service" > "$TMP/dur-a.exec" <<'PY'
import re, sys
line = [l for l in open(sys.argv[1]) if l.startswith("ExecStart=")][0][len("ExecStart="):].strip()
m = re.fullmatch(r'/bin/bash -c "((?:[^"\\]|\\.)*)"', line)
body = re.sub(r'\\(.)', r'\1', m.group(1)).replace("$$", "$").replace("%%", "%")
print(body)
PY
export CCC_PUSH_SPOOL="$TMP/spool-durable"; mkdir -p "$CCC_PUSH_SPOOL"
HOME=/home/sched bash -c "$(cat "$TMP/dur-a.exec")"; rc=$?
ok "#2827A: the unescaped wrapper runs the job, keeps its exit code, writes EXIT= and a notice" \
  '[ "$rc" = 4 ] && grep -q "^ran /home/sched$" "$TMP/dur-a.log" && tail -1 "$TMP/dur-a.log" | grep -qx "EXIT=4" \
   && ls "$CCC_PUSH_SPOOL"/*DetachedJob-dur-a-*.json >/dev/null 2>&1'
ok "#2827A: after the run the wrapper disables the timer and deletes both unit files" \
  '[ ! -e "$UD/dur-a.service" ] && [ ! -e "$UD/dur-a.timer" ] && grep -qx "disable dur-a.timer " "$DLOG"'

python3 "$MOD" schedule --unit dur-g --at "$DFUT" --log "$TMP/dur-g.log" --durable --registry "$DREG" -- true >/dev/null 2>&1
rm -f "$UD/dur-g.timer" "$UD/dur-g.service"
n_enable="$(grep -c "^enable" "$DLOG")"
out="$(python3 "$MOD" restore-timers --registry "$DREG" 2>&1)"
ok "#2827A: restore-timers reports a durable job whose timer vanished and does not re-arm it" \
  'grep -q "dur-g" <<<"$out" && grep -q "not re-armed automatically" <<<"$out" && [ "$(grep -c "^enable" "$DLOG")" = "$n_enable" ] && [ ! -e "$UD/dur-g.timer" ]'

out="$(python3 "$MOD" schedule --unit dur-u --at "$DFUT" --log "$TMP/dur-u.log" --durable --user --print -- true)"
ok "#2827A: --durable --user targets the user manager in the steps and the cleanup" \
  'grep -q "systemctl --user daemon-reload && systemctl --user enable --now dur-u.timer" <<<"$out" && grep -q "systemctl --user disable" <<<"$out"'
unset CCC_DETACHED_UNIT_DIR DURABLE_CTL_LOG

# The summary line format is load-bearing: validate-harness.sh's
# suite_summary() greps for exactly ^PASS=<n> FAIL=<n>$, so a lowercase variant
# reads as a suite that asserted nothing.
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
