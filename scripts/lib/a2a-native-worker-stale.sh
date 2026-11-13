# shellcheck shell=bash
# Termux-native A2A worker serving≠installed detector (#2791, #2589 proposal 3).
#
# The native worker is not a systemd unit, so self-update's #2138 worker gate
# and restart never reach it: a changed supervise script or a redeployed
# ~/a2a-broker-worker/dist/worker.js keeps serving the old code until something
# else restarts the process. This helper only makes that visible — it never
# restarts anything.
#
# a2a_native_worker_stale_report [worker_root]
#   Scans this user's processes for the canonical supervise loop
#   (`a2a-termux-native-worker.sh supervise`) and for `.../dist/worker.js`
#   workers. Each process is compared with the file it actually runs: when that
#   file's mtime is newer than the process start (plus a small slack) or the file
#   is gone, one line per (kind, file) is printed:
#     kind=<supervise|worker> pids=<p1,p2> started=<UTC> installed=<UTC|missing> file=<path>
#   With worker_root, only `<worker_root>/dist/worker.js` counts as a worker.
#   Prints nothing when nothing is stale or when the check cannot run
#   (fail-open: always returns 0).
#
# Process age comes from CLOCK_BOOTTIME, not /proc/uptime: Android denies
# /proc/uptime and /proc/stat to Termux, while the clock matches `ps etimes`.
a2a_native_worker_stale_report() {
    python3 - "${1:-}" "${A2A_NATIVE_WORKER_STALE_SLACK_SECONDS:-2}" <<'PY' 2>/dev/null || true
import glob
import os
import sys
import time

root, slack = sys.argv[1], float(sys.argv[2])
want_worker = os.path.realpath(os.path.join(root, "dist", "worker.js")) if root else ""
SUPERVISE = "a2a-termux-native-worker.sh"


def boot_seconds():
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return float(open("/proc/uptime", encoding="ascii").read().split()[0])


def resolve(pid, arg):
    if os.path.isabs(arg):
        return os.path.realpath(arg)
    try:
        return os.path.realpath(os.path.join(os.readlink(f"/proc/{pid}/cwd"), arg))
    except OSError:
        return ""


def classify(pid, argv):
    args = argv[1:]
    for i, arg in enumerate(args):
        if os.path.basename(arg) == SUPERVISE and "supervise" in args[i + 1:]:
            return "supervise", resolve(pid, arg)
    for arg in args:
        if not arg.endswith("/dist/worker.js") and arg != "dist/worker.js":
            continue
        path = resolve(pid, arg)
        if path and (not want_worker or path == want_worker):
            return "worker", path
    return None, ""


def utc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


uid, me = os.getuid(), os.getpid()
tick = os.sysconf("SC_CLK_TCK")
boot_now, wall_now = boot_seconds(), time.time()
groups = {}
for proc in glob.glob("/proc/[0-9]*"):
    pid = int(proc.rsplit("/", 1)[1])
    if pid == me:
        continue
    try:
        if os.stat(proc).st_uid != uid:
            continue
        raw = open(f"{proc}/cmdline", "rb").read()
        stat = open(f"{proc}/stat", encoding="ascii", errors="replace").read()
    except OSError:
        continue
    argv = [os.fsdecode(a) for a in raw.split(b"\0") if a]
    kind, path = classify(pid, argv)
    if not kind:
        continue
    try:
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])  # field 22
    except (IndexError, ValueError):
        continue
    started = wall_now - (boot_now - start_ticks / tick)
    try:
        installed = os.stat(path).st_mtime
    except OSError:
        installed = None
    if installed is not None and installed <= started + slack:
        continue
    entry = groups.setdefault((kind, path), {"pids": [], "started": started, "installed": installed})
    entry["pids"].append(pid)
    entry["started"] = min(entry["started"], started)

for (kind, path), entry in sorted(groups.items()):
    installed = "missing" if entry["installed"] is None else utc(entry["installed"])
    pids = ",".join(str(p) for p in sorted(entry["pids"]))
    print(f"kind={kind} pids={pids} started={utc(entry['started'])} installed={installed} file={path}")
PY
}
