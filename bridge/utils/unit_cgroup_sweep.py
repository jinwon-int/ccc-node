"""Empty the bridge unit's own cgroup right before the bridge process exits (#2646).

Why this exists
---------------
Bridge units run with ``KillMode=mixed``: on stop systemd sends SIGTERM to the
main process only and, once it has exited, SIGKILLs whatever is left in the
unit's cgroup. systemd 255 does that last step one PID at a time: it reads
``cgroup.procs``, opens each PID with ``pidfd_open`` and kills it. Its reader
skips only ``ESRCH``; any other error aborts the whole loop
(``cg_read_pidref``/``cg_kill_items``), and ``unit_kill_context`` then logs
``Failed to kill control group …, ignoring: Invalid argument`` and does NOT
wait for the cgroup to empty.

The kernel (6.8 ``pidfd_prepare``) returns ``EINVAL`` instead of ``ESRCH`` for
a PID that has been reaped while other processes still reference it as their
session or process-group id — exactly what an in-flight Claude Code tool shell
(a session leader) looks like during teardown. So a stop during a running turn
leaves part of the tool tree unkilled, the unit goes inactive at once, and the
next start reuses the populated cgroup (``Found left-over process …``): the
old generation's processes run alongside the new bridge and ``memory.peak`` /
``memory.events`` keep counting across restarts (#2633). Observed on 8 of 10
nodes in one week, 263 surviving processes.

The fix
-------
``sweep_own_unit_cgroup()`` runs at the very end of ``main()``. When — and only
when — this process is the main process of a ``ccc-*bridge*.service`` unit, it
SIGKILLs every other PID in its own cgroup with plain ``kill(2)`` (no pidfd, so
the ``EINVAL`` path does not exist), repeating until only itself is left or a
short deadline passes. systemd's own kill loop then finds nothing to kill.

Guards (all required; anything else is a no-op):

- cgroup v2, ``/proc/self/cgroup`` names a ``ccc-*bridge*.service`` leaf;
- ``INVOCATION_ID`` is set (systemd service environment);
- the parent process is the service manager (``comm`` == ``systemd``), so a
  test run or tool shell that merely lives inside a bridge cgroup and inherited
  its environment never sweeps the live bridge;
- ``CCC_BRIDGE_EXIT_CGROUP_SWEEP`` is not ``0``/``false``/``off``.

Fail-open: every error is logged at debug level and swallowed — exiting must
never be blocked by the sweep.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_SWITCH = "CCC_BRIDGE_EXIT_CGROUP_SWEEP"
_UNIT_RE = re.compile(r"^ccc-[a-z0-9-]*bridge[a-z0-9-]*\.service$")
_OFF = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class SweepResult:
    swept: bool
    reason: str
    killed: int = 0
    remaining: int = 0


def own_unit_cgroup(
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    environ: Mapping[str, str] | None = None,
    pid: int | None = None,
    ppid: int | None = None,
) -> tuple[Path | None, str]:
    """Return (cgroup dir, "ok") when this process is a bridge unit's main process."""
    env = os.environ if environ is None else environ
    if env.get(ENV_SWITCH, "").strip().lower() in _OFF:
        return None, "disabled"
    if not env.get("INVOCATION_ID"):
        return None, "not-a-systemd-service"
    pid = os.getpid() if pid is None else pid
    ppid = os.getppid() if ppid is None else ppid
    try:
        parent = (proc_root / str(ppid) / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None, "parent-unknown"
    if parent != "systemd":
        return None, "not-main-process"
    try:
        lines = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, "cgroup-unknown"
    unified = [line[3:] for line in lines if line.startswith("0::")]
    if len(lines) != 1 or len(unified) != 1:
        return None, "not-cgroup-v2"
    rel = unified[0].strip()
    if not _UNIT_RE.match(Path(rel).name):
        return None, "not-a-bridge-unit"
    cg_dir = cgroup_root / rel.lstrip("/")
    if not (cg_dir / "cgroup.procs").is_file():
        return None, "cgroup-procs-missing"
    return cg_dir, "ok"


def _read_procs(cg_dir: Path) -> list[int]:
    text = (cg_dir / "cgroup.procs").read_text(encoding="utf-8")
    return [int(tok) for tok in text.split() if tok.isdigit()]


def _reap_own_children() -> None:
    while True:
        try:
            child, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if child == 0:
            return


def sweep_own_unit_cgroup(
    *,
    deadline_seconds: float = 3.0,
    poll_seconds: float = 0.05,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    environ: Mapping[str, str] | None = None,
    pid: int | None = None,
    ppid: int | None = None,
    kill: Callable[[int, int], None] = os.kill,
    reap: Callable[[], None] = _reap_own_children,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SweepResult:
    """SIGKILL every other process in this bridge unit's cgroup; never raises."""
    try:
        pid = os.getpid() if pid is None else pid
        cg_dir, reason = own_unit_cgroup(
            proc_root=proc_root, cgroup_root=cgroup_root, environ=environ, pid=pid, ppid=ppid
        )
        if cg_dir is None:
            logger.debug("exit cgroup sweep skipped: %s", reason)
            return SweepResult(False, reason)
        killed: set[int] = set()
        others: list[int] = []
        end = monotonic() + max(0.0, deadline_seconds)
        while True:
            others = [p for p in _read_procs(cg_dir) if p != pid]
            if not others:
                break
            for other in others:
                try:
                    kill(other, signal.SIGKILL)
                    killed.add(other)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    logger.debug("exit cgroup sweep: kill %s failed: %s", other, exc)
            reap()
            if monotonic() >= end:
                break
            sleep(poll_seconds)
        if killed or others:
            logger.info(
                "exit cgroup sweep: SIGKILLed %d leftover process(es), %d still present (#2646)",
                len(killed), len(others),
            )
        return SweepResult(True, "ok" if not others else "deadline", len(killed), len(others))
    except Exception as exc:  # fail-open: exiting must not depend on this
        logger.debug("exit cgroup sweep failed: %s", exc)
        return SweepResult(False, f"error: {type(exc).__name__}")
