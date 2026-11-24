#!/usr/bin/env python3
"""Node-side read-only probe for the hub span view (#2858).

Run on a node from the hub over stdin, like the other fleet probes:

    ssh <node> python3 - YYYY-MM-DD < scripts/spans_view_probe.py

It reads exactly one file — ``<state>/spans-rollup/YYYY-MM-DD.json``, the
body-free daily summary ``spans-rollup.py`` saves — and prints it on one line.
Nothing is written, no raw span file is opened, and no cron entry is touched.

Output (one ``KEY=value`` per line, the last line proves the probe finished):

    SPANS_VIEW_STATUS=ok|missing|bad-date|oversize|unreadable|not-json
    SPANS_VIEW_FOLDER=1|0          a spans-rollup folder exists on this node
    SPANS_VIEW_JSON=<one line>     only with status ok (ASCII-escaped JSON)
    PROBE_COMPLETE=1

Where the state directory is: the node's rollup cron line names it
(``CCC_STATE_DIR=…`` next to ``spans-rollup.py``), and on a node whose bridge
runs under a service account that line lives in the service account's
crontab, not the login user's. So the probe reads the caller's crontab first,
when it runs as root also the other users' crontab files, then falls back to
``$CCC_STATE_DIR`` and ``~/.claude/state``. A directory named by a crontab is
used only when its ``spans-rollup`` folder belongs to that crontab's owner, so
one account's cron line cannot answer for another account's rollup. A value
that needs shell expansion (``$HOME/…``) is skipped. ``CCC_SPANS_VIEW_STATE_DIR``
names one directory outright (tests, or a node laid out differently);
``CCC_SPANS_VIEW_CRON_SPOOLS`` replaces the crontab folders (tests).

The hub validates everything again; this probe only bounds what it sends.
Keep it free of third-party imports and of syntax newer than Python 3.8 —
it runs on whatever python3 the node has.
"""
import json
import os
import pwd
import re
import stat
import subprocess
import sys

MAX_BYTES = 256 * 1024
MAX_CRON_BYTES = 1024 * 1024
FOLDER = "spans-rollup"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STATE = re.compile(r"CCC_STATE_DIR=(?:\"([^\"]+)\"|'([^']+)'|(\S+))")
_CRON_SPOOLS = ("/var/spool/cron/crontabs", "/var/spool/cron")


def _spools():
    override = os.environ.get("CCC_SPANS_VIEW_CRON_SPOOLS")
    if override is not None:
        return [item for item in override.split(":") if item]
    return list(_CRON_SPOOLS)


def _cron_sources():
    """``(owner uid, lines)`` per crontab: the caller's first, then the others'."""
    sources = []
    me = os.geteuid()
    try:
        done = subprocess.run(["crontab", "-l"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=10, check=False)
        if done.returncode == 0:
            sources.append((me, done.stdout[:MAX_CRON_BYTES].decode("utf-8", "replace").splitlines()))
    except (OSError, subprocess.SubprocessError):
        pass
    if me != 0:
        return sources
    for spool in _spools():
        try:
            names = sorted(os.listdir(spool))
        except OSError:
            continue
        for name in names:
            path = os.path.join(spool, name)
            try:
                owner = pwd.getpwnam(name).pw_uid
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CRON_BYTES:
                    continue
                with open(path, "rb") as handle:
                    lines = handle.read(MAX_CRON_BYTES).decode("utf-8", "replace").splitlines()
            except (KeyError, OSError):
                continue
            sources.append((owner, lines))
    return sources


def state_dirs():
    """Candidate ``(directory, owner uid or None)`` pairs, most specific first.

    ``None`` means the caller named the directory itself, so no owner is
    required of it.
    """
    explicit = os.environ.get("CCC_SPANS_VIEW_STATE_DIR")
    if explicit:
        return [(explicit, None)]
    found = []
    for owner, lines in _cron_sources():
        for line in lines:
            if "spans-rollup.py" not in line or line.lstrip().startswith("#"):
                continue
            match = _STATE.search(line)
            if not match:
                continue
            value = next(group for group in match.groups() if group)
            # Taken literally a `$HOME/…` value is some other path; skip it
            # and let the defaults below find the folder.
            if "$" in value or "`" in value or not os.path.isabs(value):
                continue
            found.append((value, owner))
    me = os.geteuid()
    if os.environ.get("CCC_STATE_DIR"):
        found.append((os.environ["CCC_STATE_DIR"], me))
    found.append((os.path.join(os.path.expanduser("~"), ".claude", "state"), me))
    unique = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique


def _read_file(path):
    """``(status, text)`` for one candidate file; ``missing`` when it is not there."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unreadable", None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "unreadable", None
        if info.st_size > MAX_BYTES:
            return "oversize", None
        raw = os.read(fd, MAX_BYTES + 1)
    except OSError:
        return "unreadable", None
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        return "oversize", None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        return "not-json", None
    if not isinstance(data, dict):
        return "not-json", None
    # ASCII-escaped and compact: always exactly one line, whatever the labels
    # inside contain.
    return "ok", json.dumps(data, ensure_ascii=True, separators=(",", ":"))


def read_day(day):
    """Return ``(status, folder_present, text)`` for one KST day.

    The first candidate folder that holds a readable file for the day wins. A
    candidate whose file is unusable does not stop the search; if nothing
    better turns up, the first such problem is what gets reported.
    """
    folder_present = False
    problem = None
    for state, owner in state_dirs():
        folder = os.path.join(state, FOLDER)
        try:
            info = os.lstat(folder)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):  # a symlinked folder is not followed
            continue
        if owner is not None and info.st_uid != owner:
            continue
        folder_present = True
        status, text = _read_file(os.path.join(folder, day + ".json"))
        if status == "ok":
            return "ok", folder_present, text
        if status != "missing" and problem is None:
            problem = status
    return problem or "missing", folder_present, None


def main(argv):
    day = argv[1] if len(argv) > 1 else ""
    if not _DAY.match(day):
        print("SPANS_VIEW_STATUS=bad-date")
        print("PROBE_COMPLETE=1")
        return 0
    status, folder_present, text = read_day(day)
    print("SPANS_VIEW_STATUS=" + status)
    print("SPANS_VIEW_FOLDER=" + ("1" if folder_present else "0"))
    if text is not None:
        print("SPANS_VIEW_JSON=" + text)
    print("PROBE_COMPLETE=1")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
