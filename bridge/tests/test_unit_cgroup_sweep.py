"""Exit-time cgroup sweep for bridge units (#2646).

systemd 255's KillMode=mixed final sweep aborts on the first ``pidfd_open``
EINVAL (a reaped session leader whose session members still live) and then
does not wait for the cgroup to empty, so the next start reuses a populated
cgroup. ``sweep_own_unit_cgroup`` empties the unit's cgroup from the main
process first. These tests pin the guards — above all that a test run or tool
shell living INSIDE a bridge cgroup never sweeps the live bridge — and the
kill/retry/deadline loop.
"""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

import telegram_bot.__main__ as main_module
from telegram_bot.utils import unit_cgroup_sweep as ucs

PID, PPID = 4242, 1
REL = "/system.slice/ccc-hande-bridge.service"
ENV = {"INVOCATION_ID": "abc123"}


class Tree:
    def __init__(self, root: Path, *, rel: str = REL, parent_comm: str = "systemd") -> None:
        self.proc = root / "proc"
        self.cg_root = root / "cgroup"
        (self.proc / str(PID)).mkdir(parents=True)
        (self.proc / str(PPID)).mkdir(parents=True)
        (self.proc / str(PPID) / "comm").write_text(parent_comm + "\n")
        (self.proc / str(PID) / "cgroup").write_text(f"0::{rel}\n")
        self.cg = self.cg_root / rel.lstrip("/")
        self.cg.mkdir(parents=True)
        self.set_procs([PID])

    def set_procs(self, pids: list[int]) -> None:
        (self.cg / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))

    def kwargs(self, **extra):
        base = dict(proc_root=self.proc, cgroup_root=self.cg_root, environ=ENV, pid=PID, ppid=PPID,
                    reap=lambda: None, sleep=lambda s: None)
        base.update(extra)
        return base


@pytest.fixture
def tree(tmp_path):
    return Tree(tmp_path)


def test_kills_every_other_pid_until_only_self_is_left(tree):
    tree.set_procs([PID, 10, 11, 12])
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        alive = [p for p in (10, 11, 12) if p not in {s[0] for s in sent}]
        tree.set_procs([PID, *alive])

    result = ucs.sweep_own_unit_cgroup(**tree.kwargs(kill=kill))
    assert result == ucs.SweepResult(True, "ok", killed=3, remaining=0)
    assert sent == [(10, signal.SIGKILL), (11, signal.SIGKILL), (12, signal.SIGKILL)]
    assert PID not in [p for p, _ in sent]


def test_late_forked_child_is_caught_on_the_next_pass(tree):
    tree.set_procs([PID, 10])
    passes = []

    def kill(pid, sig):
        passes.append(pid)
        tree.set_procs([PID, 11] if pid == 10 else [PID])

    result = ucs.sweep_own_unit_cgroup(**tree.kwargs(kill=kill))
    assert passes == [10, 11]
    assert result.killed == 2 and result.remaining == 0


def test_gone_process_is_not_an_error(tree):
    tree.set_procs([PID, 10])

    def kill(pid, sig):
        tree.set_procs([PID])
        raise ProcessLookupError

    result = ucs.sweep_own_unit_cgroup(**tree.kwargs(kill=kill))
    assert result == ucs.SweepResult(True, "ok", killed=0, remaining=0)


def test_deadline_bounds_an_unkillable_leftover(tree):
    tree.set_procs([PID, 10])
    clock = iter([0.0, 0.5, 1.0, 9.0, 9.0, 9.0])
    result = ucs.sweep_own_unit_cgroup(
        **tree.kwargs(kill=lambda p, s: None, monotonic=lambda: next(clock), deadline_seconds=3.0)
    )
    assert result.swept and result.reason == "deadline" and result.remaining == 1


@pytest.mark.parametrize(
    "env, parent, rel, reason",
    [
        ({}, "systemd", REL, "not-a-systemd-service"),
        ({**ENV, ucs.ENV_SWITCH: "0"}, "systemd", REL, "disabled"),
        ({**ENV, ucs.ENV_SWITCH: "off"}, "systemd", REL, "disabled"),
        # A pytest / tool shell inside the bridge cgroup with the inherited env.
        (ENV, "bash", REL, "not-main-process"),
        (ENV, "python3", REL, "not-main-process"),
        # An operator shell or start.sh run: a session scope, not a bridge unit.
        (ENV, "systemd", "/user.slice/user-0.slice/session-4.scope", "not-a-bridge-unit"),
        (ENV, "systemd", "/system.slice/a2a-hermes-worker.service", "not-a-bridge-unit"),
    ],
)
def test_guards_make_it_a_no_op(tmp_path, env, parent, rel, reason):
    t = Tree(tmp_path, rel=rel, parent_comm=parent)
    t.set_procs([PID, 10])
    sent = []
    result = ucs.sweep_own_unit_cgroup(**t.kwargs(environ=env, kill=lambda p, s: sent.append(p)))
    assert result == ucs.SweepResult(False, reason)
    assert sent == []


def test_user_unit_path_is_accepted(tmp_path):
    rel = "/user.slice/user-1001.slice/user@1001.service/app.slice/ccc-hande-bridge.service"
    t = Tree(tmp_path, rel=rel)
    cg, reason = ucs.own_unit_cgroup(proc_root=t.proc, cgroup_root=t.cg_root, environ=ENV, pid=PID, ppid=PPID)
    assert (cg, reason) == (t.cg, "ok")


def test_cgroup_v1_hybrid_is_skipped(tree):
    (tree.proc / str(PID) / "cgroup").write_text(f"12:memory:{REL}\n0::{REL}\n")
    cg, reason = ucs.own_unit_cgroup(proc_root=tree.proc, cgroup_root=tree.cg_root, environ=ENV, pid=PID, ppid=PPID)
    assert (cg, reason) == (None, "not-cgroup-v2")


def test_errors_never_escape(tree):
    tree.set_procs([PID, 10])

    def boom(pid, sig):
        raise RuntimeError("unexpected")

    result = ucs.sweep_own_unit_cgroup(**tree.kwargs(kill=boom))
    assert result.swept is False and result.reason == "error: RuntimeError"


class _StubSettings:
    def hook_policy_environment(self):
        return {}


class _StubBot:
    def __init__(self, raises):
        self.raises = raises

    def validate_runtime_paths(self):
        pass

    def run(self):
        if self.raises is not None:
            raise self.raises


@pytest.mark.parametrize("raises", [None, SystemExit(0), SystemExit(2), RuntimeError("x")])
def test_main_sweeps_on_every_exit_path(monkeypatch, tmp_path, raises):
    calls = []
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["telegram_bot"])
    monkeypatch.setattr(main_module, "load_runtime_settings", lambda: _StubSettings())
    monkeypatch.setattr(main_module, "create_bot", lambda settings: _StubBot(raises))
    monkeypatch.setattr(main_module, "setup_logging", lambda settings: None)
    monkeypatch.setattr(main_module, "report_provider_environment", lambda settings: None)
    monkeypatch.setattr(ucs, "sweep_own_unit_cgroup", lambda: calls.append("swept"))
    if raises is None:
        main_module.main()
    else:
        with pytest.raises(SystemExit):
            main_module.main()
    assert calls == ["swept"]
