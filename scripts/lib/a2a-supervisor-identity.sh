#!/usr/bin/env bash
# Shared process-identity helpers for the Termux-native A2A supervisor.
#
# A numeric PID alone is not an identity: the kernel may reuse it after the
# original process exits.  The helpers below pair each PID with Linux/Android's
# /proc start-time token and verify the exact canonical supervisor argv before
# callers report, stop, or replace a process.

# Print the kernel start-time token for a live process.
a2a_process_start_token() {
    local pid="$1" stat rest
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/stat" ]] || return 1
    stat=$(<"/proc/$pid/stat") || return 1
    rest=${stat##*) }
    # Fields in $rest begin at proc(5) field 3; starttime is field 22, so it
    # is the twentieth whitespace-separated value here.
    set -- $rest
    [[ $# -ge 20 && "${20}" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "${20}"
}

a2a_process_identity_matches() {
    local pid="$1" expected_token="$2" live_token
    live_token=$(a2a_process_start_token "$pid") || return 1
    [[ "$live_token" == "$expected_token" ]]
}

a2a_process_parent_pid() {
    local pid="$1" ppid
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/status" ]] || return 1
    ppid=$(awk '/^PPid:/ {print $2; exit}' "/proc/$pid/status" 2>/dev/null) || return 1
    [[ "$ppid" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$ppid"
}

# Verify the canonical execution grammar exactly:
#
#     bash EXPECTED_SCRIPT supervise [--env-file PATH]
#
# Checking only whether the script path and `supervise` occur somewhere in
# argv lets an unrelated `bash -c ... EXPECTED_SCRIPT supervise` process spoof
# the supervisor identity.  The canonical supervisor also uniquely holds an
# exclusive flock through fd 200 on its configured lock file, so bind the argv
# to both that file's device/inode and the FLOCK WRITE record exposed by
# /proc/PID/fdinfo/200.  Merely opening the same file is not sufficient.  Bind
# the executable to this verifier's Bash too, while allowing the exact deleted
# paths observed when dpkg replaces a running Bash binary on Termux/Linux.
a2a_supervisor_command_matches() {
    local pid="$1" expected_script="$2" expected_lock="$3"
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
    python3 - "$pid" "$expected_script" "$expected_lock" <<'PY'
import os
import sys

pid, expected, expected_lock = sys.argv[1:]
try:
    raw = open(f"/proc/{pid}/cmdline", "rb").read()
    argv = [os.fsdecode(arg) for arg in raw.split(b"\0") if arg]
    lock_stat = os.stat(expected_lock)
    fd_stat = os.stat(f"/proc/{pid}/fd/200")
    fdinfo = open(f"/proc/{pid}/fdinfo/200", encoding="ascii").read().splitlines()
    executable = os.readlink(f"/proc/{pid}/exe")
    verifier_bash = os.readlink(f"/proc/{os.getppid()}/exe")
except OSError:
    raise SystemExit(1)

expected = os.path.realpath(expected)
holds_exclusive_flock = any(
    " ".join(line.split()).split()[2:5] == ["FLOCK", "ADVISORY", "WRITE"]
    for line in fdinfo
    if line.startswith("lock:")
)
allowed_bash_executables = {
    verifier_bash,
    f"{verifier_bash} (deleted)",
    f"{verifier_bash}.dpkg-tmp (deleted)",
}
is_canonical = (
    len(argv) >= 3
    and os.path.basename(argv[0]) == "bash"
    and os.path.realpath(argv[1]) == expected
    and argv[2] == "supervise"
    and (fd_stat.st_dev, fd_stat.st_ino) == (lock_stat.st_dev, lock_stat.st_ino)
    and holds_exclusive_flock
    and executable in allowed_bash_executables
)
raise SystemExit(0 if is_canonical else 1)
PY
}

# Print "PID START_TOKEN" only when PID is still the exact canonical
# supervisor instance.  TOKEN may be empty for a legacy one-field PID file;
# callers then derive and return the live token after command verification.
a2a_validate_supervisor_identity() {
    local pid="$1" token="${2:-}" expected_script="$3" expected_lock="$4" live_token
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    live_token=$(a2a_process_start_token "$pid") || return 1
    [[ -z "$token" || "$token" == "$live_token" ]] || return 1
    a2a_supervisor_command_matches "$pid" "$expected_script" "$expected_lock" || return 1
    printf '%s %s' "$pid" "$live_token"
}

# Read and validate a PID file.  The current format is "PID START_TOKEN";
# legacy PID-only files remain readable only when the live argv is canonical.
a2a_current_supervisor_record() {
    local pidfile="$1" expected_script="$2" expected_lock="$3" pid token extra
    [[ -f "$pidfile" ]] || return 0
    read -r pid token extra < "$pidfile" || true
    [[ -z "${extra:-}" ]] || return 0
    a2a_validate_supervisor_identity "${pid:-}" "${token:-}" \
        "$expected_script" "$expected_lock" 2>/dev/null || true
}

# Atomically publish an owner-only PID identity record after the caller owns
# the singleton flock.  The temporary file lives beside PIDFILE so rename(2)
# cannot cross filesystems.
a2a_write_supervisor_record() {
    local pidfile="$1" pid="$2" token tmp
    token=$(a2a_process_start_token "$pid") || return 1
    tmp=$(umask 077; mktemp "$pidfile.$pid.XXXXXX") || return 1
    if ! printf '%s %s\n' "$pid" "$token" > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    if ! mv -f "$tmp" "$pidfile"; then
        rm -f "$tmp"
        return 1
    fi
}

# Extract the exact --env-file value from a verified supervisor's argv.  This
# lets stop's SIGKILL fallback sweep only the tunnel owned by that supervisor.
a2a_supervisor_env_file() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
    python3 - "$pid" <<'PY'
import os
import sys

try:
    argv = [os.fsdecode(arg) for arg in open(
        f"/proc/{sys.argv[1]}/cmdline", "rb"
    ).read().split(b"\0") if arg]
except OSError:
    raise SystemExit(1)
for index, arg in enumerate(argv[:-1]):
    if arg == "--env-file":
        print(argv[index + 1])
        raise SystemExit(0)
raise SystemExit(1)
PY
}
