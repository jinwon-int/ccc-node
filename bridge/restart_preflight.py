#!/usr/bin/env python3
"""Read-only readiness gate for a legacy Termux restart, before stopping it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

if __package__:
    from . import dependency_bootstrap as deps
    from .prepared_runtime import bounded_read
    from .runtime_readiness import PROBES, positive_timeout, probe
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import dependency_bootstrap as deps
    from prepared_runtime import bounded_read
    from runtime_readiness import PROBES, positive_timeout, probe


def check(source: Path, runtime: Path, process_unlocked: str | None, timeout: float, *, project_env: Path) -> dict:
    started = time.monotonic()
    report = {"schema": "ccc.restart-preflight.v1", "status": "preparation_required", "checks": []}
    try:
        if Path(sys.prefix).resolve() != runtime.resolve() or sys.prefix == sys.base_prefix:
            report["reason"] = "selected_venv_mismatch"
            return report
        api = deps.android_build_api()
        report["android_api"] = api
        if os.environ.get("ANDROID_API_LEVEL") not in (None, "", str(api)):
            report["reason"] = "android_api_override_mismatch"
            return report
        # Match bootstrap's install-mode precedence and fingerprint, but read
        # bounded regular inputs without following final symlinks. No lock,
        # receipt, package repair, install or cache write occurs in this gate.
        paths = deps.DependencyPaths.from_roots(source, runtime, project_env)
        mode = deps.resolve_install_mode(process_unlocked, paths.project_env, paths.bridge_env)
        digest = hashlib.sha256()
        for path in (paths.requirements, paths.lock, paths.pyproject):
            digest.update(bounded_read(path))
            digest.update(b"\0")
        digest.update(mode.value.encode())
        if bounded_read(paths.hash_cache, 128).decode().rstrip("\n") != digest.hexdigest():
            report["reason"] = "dependencies_changed"
            return report
        # Include every locked Android native package, not just cryptography.
        checks = (("android_native_import", ("-c", "import cryptography.exceptions, "
                   "cryptography.hazmat.bindings._rust, jiter, pydantic_core, rpds, pyromark")), *PROBES[1:])
        report["checks"] = [probe(name, args, timeout - (time.monotonic() - started))
                            for name, args in checks]
        if all(item["status"] == "pass" for item in report["checks"]):
            report["status"] = "ready"
        else:
            report["reason"] = "runtime_checks_failed"
    except (OSError, ValueError):
        report["reason"] = "runtime_inputs_unavailable"
    finally:
        report["duration_ms"] = round((time.monotonic() - started) * 1000)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", required=True, type=Path)
    parser.add_argument("--venv-dir", required=True, type=Path)
    parser.add_argument("--project-env", required=True, type=Path)
    parser.add_argument("--process-unlocked", default=os.environ.get("CCC_DEPS_UNLOCKED"))
    parser.add_argument("--timeout-seconds", type=positive_timeout, default=30.0)
    args = parser.parse_args(argv)
    report = check(args.bridge_dir, args.venv_dir, args.process_unlocked, args.timeout_seconds,
                   project_env=args.project_env)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "ready" else 6


if __name__ == "__main__":
    raise SystemExit(main())
