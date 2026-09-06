#!/usr/bin/env python3
"""Collect read-only, body-free readiness evidence using this Python environment."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time


SCHEMA = "ccc.runtime-readiness.v1"
PROBES = (
    ("native_import", ("-c", "import cryptography.exceptions; import cryptography.hazmat.bindings._rust")),
    ("sdk_import", ("-c", "import claude_agent_sdk")),
    ("aes_gcm", ("-c", "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; "
                 "import os; k=AESGCM.generate_key(bit_length=128); a=AESGCM(k); "
                 "n=os.urandom(12); p=os.urandom(32); c=a.encrypt(n,p,b'readiness'); "
                 "assert a.decrypt(n,c,b'readiness') == p")),
    ("pip_check", ("-m", "pip", "check")),
)


def file_hash(path: Path) -> str:
    # Hash only known source inputs. Reject symlinks and bound reads; never
    # open .env, credentials, runtime health/task files or backup contents.
    if path.is_symlink() or not path.is_file():
        raise ValueError("source_input_unavailable")
    with path.open("rb") as stream:
        content = stream.read(4 * 1024 * 1024 + 1)
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("source_input_too_large")
    return hashlib.sha256(content).hexdigest()


def git_identity(bridge_dir: Path, timeout: float = 4.0) -> dict[str, object]:
    # Ambient GIT_DIR/INDEX_FILE/etc. must not describe a different checkout.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    deadline = time.monotonic() + timeout
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"head": None, "tracked_changes": None}
        result = subprocess.run(
            ["git", "-C", str(bridge_dir), "rev-parse", "HEAD"], env=env,
            capture_output=True, text=True, timeout=min(2, remaining), check=False,
        )
        head = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", head):
            return {"head": None, "tracked_changes": None}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"head": head, "tracked_changes": None}
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(bridge_dir), "status",
             "--porcelain=v1", "--untracked-files=no", "--ignore-submodules=none"],
            env=env, capture_output=True, text=True,
            timeout=min(2, remaining), check=False,
        )
        # status refreshes the stat cache in memory to distinguish touch-only
        # changes; GIT_OPTIONAL_LOCKS=0 prevents writing that refresh to index.
        # Names/bodies from porcelain output are never included in receipts.
        return {"head": head, "tracked_changes": bool(result.stdout) if result.returncode == 0 else None}
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "tracked_changes": None}


def runtime_identity() -> dict[str, object]:
    versions = {}
    for name in ("cryptography", "claude-agent-sdk", "mcp"):
        try:
            value = metadata.version(name)
            versions[name] = value if re.fullmatch(r"[A-Za-z0-9.!+_-]{1,128}", value) else None
        except metadata.PackageNotFoundError:
            versions[name] = None
    android = getattr(sys, "getandroidapilevel", None)
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "system": platform.system(), "machine": platform.machine(),
            "android_api_level": android() if android else None,
            "termux_environment_hint": bool(os.environ.get("TERMUX_VERSION") or
                                             "/com.termux/" in os.environ.get("PREFIX", "")),
            "executable": sys.executable, "prefix": sys.prefix, "base_prefix": sys.base_prefix,
            "packages": versions}


def probe(name: str, args: tuple[str, ...], budget: float) -> dict[str, object]:
    started = time.monotonic()
    if budget <= 0:
        return {"id": name, "status": "not_run", "reason": "budget_exhausted", "duration_ms": 0}
    if os.name != "posix":
        return {"id": name, "status": "not_run", "reason": "unsupported_process_cleanup", "duration_ms": 0}
    try:
        # Neither import diagnostics nor pip output enters the receipt. -I
        # ignores ambient PYTHONPATH and user-site imports; -B avoids bytecode
        # writes into the observed environment. No install/repair is invoked.
        with subprocess.Popen(
            [sys.executable, "-I", "-B", *args], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1"),
        ) as child:
            try:
                code = child.wait(timeout=budget)
                result = {"id": name, "status": "pass" if code == 0 else "fail", "exit_code": code}
            except subprocess.TimeoutExpired:
                # Kill the isolated probe group, including a stuck import's
                # descendants, and reap the direct child before returning.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                result = {"id": name, "status": "timeout", "reason": "budget_exhausted"}
    except OSError:
        result = {"id": name, "status": "error", "reason": "probe_spawn_failed"}
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result


def collect(bridge_dir: Path, timeout: float) -> dict[str, object]:
    started = time.monotonic()
    report: dict[str, object] = {
        "schema": SCHEMA, "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "native_sdk_crypto_and_package_consistency", "status": "error",
        "lifecycle_scenarios": {name: "not_run" for name in
                                ("fresh_install", "reinstall", "rollback", "service_restart")},
    }
    try:
        files = {name: file_hash(bridge_dir / name) for name in
                 ("requirements.lock.txt", "requirements.txt", "pyproject.toml")}
        report["source"] = {"inputs_sha256": files, "git": git_identity(bridge_dir, timeout - (time.monotonic() - started)),
                            "observer_sha256": file_hash(Path(__file__))}
        report["runtime"] = runtime_identity()
        # One shared deadline includes identity collection and every probe.
        # Slow/failing probes cannot multiply the configured time budget.
        results = [probe(name, args, timeout - (time.monotonic() - started))
                   for name, args in PROBES]
        report["checks"] = results
        report["status"] = "ready" if all(item["status"] == "pass" for item in results) else "unready"
    except (OSError, ValueError):
        # Do not serialize exception strings; source or site customizations
        # may include private data. Keep failures categorical.
        report["reason"] = "identity_unavailable"
    report["duration_ms"] = round((time.monotonic() - started) * 1000)
    return report


def positive_timeout(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 300:
        raise argparse.ArgumentTypeError("timeout must be finite and in (0, 300] seconds")
    return number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--timeout-seconds", type=positive_timeout, default=60.0,
                        help="shared probe budget including identity collection (default: 60)")
    args = parser.parse_args(argv)
    report = collect(args.bridge_dir, args.timeout_seconds)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return {"ready": 0, "unready": 1, "error": 2}[str(report["status"])]


if __name__ == "__main__":
    raise SystemExit(main())
