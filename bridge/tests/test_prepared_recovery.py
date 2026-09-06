"""Offline recovery rehearsal with real launchers, processes and two venvs.

Only the bot entrypoint and one dependency are fixtures. Native/SDK probes,
source seals, token locking, stop/restart and serving verification are real.
The existing test environment supplies dependencies read-only; no pip install,
Telegram connection, provider invocation or production state is involved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import site
import subprocess
import sys
import time
import venv

import pytest

from telegram_bot.dependency_bootstrap import DependencyPaths, InstallMode, dependency_fingerprint
from telegram_bot.prepared_runtime import source_seal
from telegram_bot.prepared_serving import process_alive


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process ownership rehearsal")
BRIDGE = Path(__file__).resolve().parents[1]

BOT = '''import fcntl, json, os, signal, socket, sys, time
from datetime import datetime, timezone
from pathlib import Path
from telegram_bot.runtime_generation import capture_runtime_generation
from telegram_bot.token_lock import clear_token_claim
from rehearsal_dependency import VERSION

def network_forbidden(*args, **kwargs):
    raise AssertionError("offline fixture attempted network access")
socket.socket = network_forbidden
project = Path(sys.argv[sys.argv.index("--path") + 1])
data = project / ".telegram_bot"
source = Path(__file__).resolve().parent
label = source.parent.name
def event(kind):
    payload = json.dumps(dict(kind=kind, label=label, dependency=VERSION,
                             pid=os.getpid(), prefix=sys.prefix)) + "\\n"
    fd = os.open(project / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try: os.write(fd, payload.encode())
    finally: os.close(fd)
poller = open(project / "poller.lock", "a")
try: fcntl.flock(poller, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    event("overlap")
    raise SystemExit(91)
# The production health reporter publishes the app PID in daemon mode, where
# the shell records only its supervisor PID. Mirror that entrypoint duty after
# acquiring the fixture poller lock; foreground launch already records our PID.
fd = os.open(data / "bot.pid", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as stream: stream.write(str(os.getpid()) + "\\n")
started = datetime.now(timezone.utc).isoformat()
generation = capture_runtime_generation()
event("start")
mode_file = project / (label + ".mode")
mode = mode_file.read_text().strip() if mode_file.exists() else "ready"
if mode == "crash":
    event("crash")
    raise SystemExit(23)
stopping = False
def stop(*args):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
try:
    while not stopping:
        health = dict(schema_version=1, updated_at=datetime.now(timezone.utc).isoformat(),
            process=dict(pid=os.getpid(), started_at=started, mode=os.environ.get("BOT_PROCESS_MODE", "foreground")),
            runtime_generation=generation, service=dict(state="available" if mode == "ready" else "unavailable"),
            telegram=dict(state="healthy"), agent=dict(state="healthy", provider="codex"))
        temp = data / "health.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream: json.dump(health, stream)
        temp.replace(data / "health.json")
        time.sleep(0.05)
finally:
    event("stop")
    clear_token_claim(Path(os.environ["BOT_TOKEN_LOCK_FILE"]), (os.getpid(),))
'''


class Rehearsal:
    def __init__(self, root: Path):
        self.root = root
        self.project = root / "project with spaces"
        self.data = self.project / ".telegram_bot"
        self.data.mkdir(parents=True, mode=0o700)
        self.home = root / "home"
        self.home.mkdir(mode=0o700)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.forbidden_install = root / "unexpected-install"
        # Explicit service/CLI stubs; ambient executables and state never select
        # a production manager, provider or platform wake-lock implementation.
        for name, body in {"systemctl": "exit 1", "termux-wake-lock": "exit 0"}.items():
            path = self.bin / name
            path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(0o700)
        python = self.bin / "python3"
        python.write_text("#!/bin/sh\ncase \" $* \" in\n"
            "  *\" -m venv \"*|*\" -m pip install \"*) touch "
            + shlex.quote(str(self.forbidden_install)) + "; exit 99 ;;\nesac\nexec "
            + shlex.quote(sys.executable) + ' "$@"\n')
        python.chmod(0o700)
        self.env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
                    "LANG": "C.UTF-8", "CCC_SYSTEMD_DIR": str(root / "no-units"),
                    "CCC_SYSTEMCTL": str(self.bin / "systemctl"),
                    "CCC_BRIDGE_RESTART_READY_TIMEOUT": "12", "CCC_BRIDGE_STOP_GRACE_SECONDS": "2"}
        (self.data / ".env").write_text("TELEGRAM_BOT_TOKEN=123456:OFFLINE-REHEARSAL\n"
            "CCC_AGENT_PROVIDER=codex\nCCC_CODEX_CLI_PATH=/bin/true\n")
        (self.data / ".env").chmod(0o600)
        self.generations = {}
        self.groups = []

    def prepare(self, label: str):
        source = self.root / label / "bridge"
        shutil.copytree(BRIDGE, source, ignore=shutil.ignore_patterns(
            ".*", "venv", "__pycache__", "tests", "*.egg-info"))
        (source / "__main__.py").write_text(BOT)
        # Different locked inputs AND an actually imported per-venv dependency
        # make a source-only recovery distinguishable from a full pair recovery.
        with (source / "requirements.lock.txt").open("a") as stream:
            stream.write(f"\n# rehearsal generation {label}\n")
        work = self.root / (label + " job")
        work.mkdir(mode=0o700)
        runtime = work / "runtime"
        venv.EnvBuilder(with_pip=False).create(runtime)
        pip = runtime / "bin/pip"
        pip.write_text("#!/bin/sh\ntouch " + shlex.quote(str(self.forbidden_install)) + "; exit 99\n")
        pip.chmod(0o700)
        packages = runtime / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        shim = self.root / (label + " import")
        shim.mkdir()
        (shim / "telegram_bot").symlink_to(source, target_is_directory=True)
        (packages / "rehearsal.pth").write_text(
            f"import sys; sys.path.insert(0, {str(shim)!r}); sys.path.extend({site.getsitepackages()!r})\n")
        (packages / "rehearsal_dependency.py").write_text(f"VERSION = {label!r}\n")
        paths = DependencyPaths.from_roots(source, runtime, work / "absent.env")
        paths.hash_cache.write_text(dependency_fingerprint(paths, InstallMode.LOCKED))
        receipt = dict(schema="ccc.termux-preparation.v1", status="ready", work_dir=str(work),
                       scenarios={"fresh_install": "pass"}, source_seal=source_seal(source))
        (work / "receipt.json").write_text(json.dumps(receipt))
        (work / "receipt.json").chmod(0o600)
        self.generations[label] = (source, work)

    def command(self, label: str, action="--restart", extra_args=(), timeout=45):
        source, work = self.generations[label]
        command = ["bash", str(source / "start.sh"), "--path", str(self.project),
                   "--prepared-runtime", str(work), action, *extra_args]
        # Keep stdout in a file: a healthy detached bot must not hold a parent's
        # capture pipe open. Retain logs for pytest's assertion diagnostics.
        log = self.root / f"command-{len(self.groups)}.log"
        with log.open("w") as output:
            child = subprocess.Popen(command, env=self.env, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            self.groups.append(child.pid)
            try:
                rc = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                raise
        return rc, log.read_text()

    def health(self):
        return json.loads((self.data / "health.json").read_text())

    def events(self):
        return [json.loads(line) for line in (self.project / "events.jsonl").read_text().splitlines()]

    def owned_groups(self):
        # Validation probes start their own sessions. Recognize those leaders
        # by the exact private interpreter, never just a matching path argument.
        interpreters = {os.fsencode(work / "runtime/bin/python")
                        for _, work in self.generations.values()}
        groups = set()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes()
                if bytes(self.root) not in command:
                    continue
                pid = int(entry.name)
                group = os.getpgid(pid)
                if group in self.groups or (group == pid and command.split(b"\0", 1)[0] in interpreters):
                    groups.add(group)
            except (ProcessLookupError, FileNotFoundError, PermissionError):
                pass
        return groups

    def close(self):
        for group in self.owned_groups():
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 2
        while self.owned_groups() and time.monotonic() < deadline:
            time.sleep(0.05)
        for group in self.owned_groups():
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture
def rehearsal(tmp_path):
    fixture = Rehearsal(tmp_path)
    try:
        fixture.prepare("previous")
        fixture.prepare("candidate")
        yield fixture
    finally:
        fixture.close()


@pytest.mark.parametrize("failure, expected_exit", [("unready", 4), ("crash", 2)])
def test_explicit_recovery_restores_previous_source_and_environment(rehearsal, failure, expected_exit):
    r = rehearsal
    rc, output = r.command("previous")
    assert rc == 0, output
    original = r.health()
    old_pid = original["process"]["pid"]
    originals = {name: (source_seal(source), (work / "receipt.json").read_bytes(),
                        (work / "runtime/.req_hash").read_bytes())
                 for name, (source, work) in r.generations.items()}
    assert originals["previous"][0] != originals["candidate"][0]
    assert originals["previous"][2] != originals["candidate"][2]
    (r.project / "candidate.mode").write_text(failure)
    rc, output = r.command("candidate")
    assert rc == expected_exit, output
    assert not process_alive(old_pid)
    assert [event["label"] for event in r.events() if event["kind"] == "start"] == ["previous", "candidate"]
    if failure == "unready":
        candidate_pid = r.health()["process"]["pid"]
        assert process_alive(candidate_pid)
        # Invalid retained recovery environment must fail before stopping the
        # candidate that was deliberately left alive after readiness failure.
        fingerprint = r.generations["previous"][1] / "runtime/.req_hash"
        saved = fingerprint.read_bytes()
        fingerprint.write_text("invalid")
        rc, output = r.command("previous")
        assert rc == 6, output
        assert process_alive(candidate_pid)
        assert r.health()["process"]["pid"] == candidate_pid
        fingerprint.write_bytes(saved)
        # A valid recovery environment can still fail at runtime. That failure
        # must stay nonzero; a subsequent explicit retry uses the retained pair.
        (r.project / "previous.mode").write_text("unready")
        rc, output = r.command("previous")
        assert rc == 4, output
        assert not process_alive(candidate_pid)
        assert process_alive(r.health()["process"]["pid"])
        (r.project / "previous.mode").write_text("ready")
    rc, output = r.command("previous")
    assert rc == 0, output
    recovered = r.health()
    assert recovered["process"]["pid"] != old_pid
    for field in ("source_dir", "source_seal", "python_executable", "python_prefix", "dependency_fingerprint"):
        assert recovered["runtime_generation"][field] == original["runtime_generation"][field]
    events = r.events()
    assert all(event["kind"] != "overlap" for event in events)
    assert all(event["dependency"] == event["label"] for event in events)
    assert all(event["prefix"] == str(r.generations[event["label"]][1] / "runtime") for event in events)
    assert events[-1]["label"] == events[-1]["dependency"] == "previous"
    for name, (source, work) in r.generations.items():
        assert (source_seal(source), (work / "receipt.json").read_bytes(),
                (work / "runtime/.req_hash").read_bytes()) == originals[name]
    history = list((r.data / "runtime-history").glob("*.json"))
    assert len(history) == len([event for event in events if event["kind"] == "start"])
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in history)
    assert not r.forbidden_install.exists()
    assert all(not (source / "venv").exists() for source, _ in r.generations.values())


def test_cleanup_reaps_private_probe_session_and_preserves_unrelated_sentinel(rehearsal):
    r = rehearsal
    python = r.generations["previous"][1] / "runtime/bin/python"
    processes = []
    try:
        # Same session shape as prepared_runtime.probe(): this group is not in
        # the fixture launcher's recorded groups, and survives its parent exit.
        probe = subprocess.Popen([str(python), "-I", "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        processes.append(probe)
        sentinel = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)", str(r.root)],
                                    start_new_session=True)
        processes.append(sentinel)
        r.close()
        assert probe.wait(timeout=3) == -signal.SIGTERM
        assert sentinel.poll() is None
    finally:
        # Parent-owned handles also make the regression safe on broken cleanup.
        for child in processes:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
