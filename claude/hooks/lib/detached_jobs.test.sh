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

# The summary line format is load-bearing: validate-harness.sh's
# suite_summary() greps for exactly ^PASS=<n> FAIL=<n>$, so a lowercase variant
# reads as a suite that asserted nothing.
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
