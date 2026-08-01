#!/usr/bin/env python3
"""Deterministic bridge dependency bootstrap used by ``start.sh``.

The module is intentionally standard-library-only: it runs with the freshly
created virtualenv interpreter before the bridge package or its dependencies
have been installed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TextIO


class InstallMode(str, Enum):
    """Supported dependency installation policies."""

    LOCKED = "locked"
    UNLOCKED = "unlocked"


@dataclass(frozen=True)
class DependencyPaths:
    """Filesystem inputs and executables used by dependency bootstrap."""

    bridge_dir: Path
    venv_dir: Path
    project_env: Path
    bridge_env: Path
    requirements: Path
    lock: Path
    pyproject: Path
    hash_cache: Path
    pip: Path

    @classmethod
    def from_roots(
        cls, bridge_dir: Path, venv_dir: Path, project_env: Path
    ) -> DependencyPaths:
        return cls(
            bridge_dir=bridge_dir,
            venv_dir=venv_dir,
            project_env=project_env,
            bridge_env=bridge_dir / ".env",
            requirements=bridge_dir / "requirements.txt",
            lock=bridge_dir / "requirements.lock.txt",
            pyproject=bridge_dir / "pyproject.toml",
            hash_cache=venv_dir / ".req_hash",
            pip=venv_dir / "bin" / "pip",
        )


def _read_env_value(path: Path, key: str) -> str:
    """Read the last shell-style assignment using start.sh's legacy rules."""
    if not path.is_file():
        return ""
    assignment = re.compile(rf"^[ \t]*(?:export[ \t]+)?{re.escape(key)}[ \t]*=(.*)$")
    value = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        match = assignment.match(line)
        if match is not None:
            value = match.group(1)
    value = value.strip()
    if value.endswith('"'):
        value = value[:-1]
    if value.startswith('"'):
        value = value[1:]
    if value.endswith("'"):
        value = value[:-1]
    if value.startswith("'"):
        value = value[1:]
    value = value.split(" #", 1)[0]
    return value.rstrip()


def resolve_install_mode(
    process_value: str | None,
    project_env: Path,
    bridge_env: Path,
) -> InstallMode:
    """Resolve process > project env > bridge env, unlocking only for ``1``."""
    configured = process_value or _read_env_value(project_env, "CCC_DEPS_UNLOCKED")
    if not configured:
        configured = _read_env_value(bridge_env, "CCC_DEPS_UNLOCKED")
    return InstallMode.UNLOCKED if configured == "1" else InstallMode.LOCKED


def dependency_fingerprint(paths: DependencyPaths, mode: InstallMode) -> str:
    """Return the legacy SHA-256 cache key for install inputs and mode."""
    digest = hashlib.sha256()
    for path in (paths.requirements, paths.lock, paths.pyproject):
        digest.update(path.read_bytes() if path.exists() else b"<absent>")
        digest.update(b"\0")
    digest.update(mode.value.encode())
    return digest.hexdigest()


def install_commands(paths: DependencyPaths, mode: InstallMode) -> tuple[tuple[str, ...], ...]:
    """Construct pip argv without a shell so all path bytes stay literal."""
    pip = str(paths.pip)
    if mode is InstallMode.LOCKED:
        return (
            (pip, "install", "-q", "--require-hashes", "-r", str(paths.lock)),
            (pip, "install", "-q", "--no-deps", "-e", str(paths.bridge_dir)),
        )
    return (
        (pip, "install", "-q", "--upgrade", "pip"),
        (pip, "install", "-q", "-r", str(paths.requirements)),
        (pip, "install", "-q", "-e", str(paths.bridge_dir)),
    )


def _remove_legacy_package_link() -> None:
    """Remove the pre-packaging site-packages symlink when present."""
    package_link = Path(sysconfig.get_paths()["purelib"]) / "telegram_bot"
    if package_link.is_symlink():
        try:
            package_link.unlink()
        except OSError:
            # start.sh historically allowed a failed best-effort rm to fall
            # through to pip, whose editable install supplies the hard gate.
            pass


def ensure_android_api_level(
    env: MutableMapping[str, str], *, stdout: TextIO = sys.stdout
) -> None:
    """Populate ANDROID_API_LEVEL from getprop for Termux installs."""
    if env.get("ANDROID_API_LEVEL"):
        return
    is_termux = bool(env.get("TERMUX_VERSION")) or "/com.termux/" in env.get("PREFIX", "")
    getprop = shutil.which("getprop", path=env.get("PATH"))
    if not is_termux or getprop is None:
        return
    result = subprocess.run(
        [getprop, "ro.build.version.sdk"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    sdk = "".join(character for character in result.stdout if "0" <= character <= "9")
    if sdk:
        env["ANDROID_API_LEVEL"] = sdk
        print(f"\033[90m✓ Android API level auto-detected: {sdk}\033[0m", file=stdout, flush=True)


def _saved_fingerprint(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return ""


def _print_install_failure(mode: InstallMode, command_index: int, stdout: TextIO) -> None:
    if mode is InstallMode.LOCKED and command_index == 0:
        print("❌ Hash-locked dependency installation failed", file=stdout, flush=True)
        print("   If this host cannot install a locked artifact, retry with", file=stdout)
        print("   CCC_DEPS_UNLOCKED=1 and report the platform gap.", file=stdout)
    elif mode is InstallMode.UNLOCKED and command_index == 0:
        print("❌ Failed to upgrade pip", file=stdout, flush=True)
    elif mode is InstallMode.UNLOCKED and command_index == 1:
        print("❌ Dependency installation failed", file=stdout, flush=True)
    else:
        print("❌ Editable bridge package installation failed", file=stdout, flush=True)


def sync_dependencies(
    paths: DependencyPaths,
    mode: InstallMode,
    *,
    force_install: bool = False,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    """Install dependencies when the cache key changes; return a shell status."""
    _remove_legacy_package_link()
    current_hash = dependency_fingerprint(paths, mode)
    saved_hash = _saved_fingerprint(paths.hash_cache)
    if not force_install and saved_hash and saved_hash == current_hash:
        print(
            "\033[90m✓ Dependencies unchanged (requirements hash match)\033[0m",
            file=stdout,
            flush=True,
        )
        return 0

    print("📦 Installing Python dependencies...", file=stdout, flush=True)
    child_env = dict(os.environ if environ is None else environ)
    ensure_android_api_level(child_env, stdout=stdout)
    if mode is InstallMode.LOCKED and not paths.lock.is_file():
        print(f"❌ Hash lock not found: {paths.lock}", file=stdout)
        print("   Regenerate it with scripts/ccc-deps-lock.sh, or set", file=stdout)
        print("   CCC_DEPS_UNLOCKED=1 to use the legacy unlocked install.", file=stdout)
        return 1

    if mode is InstallMode.UNLOCKED:
        print(
            "⚠️  CCC_DEPS_UNLOCKED=1 — legacy unlocked install (no hash verification)",
            file=stdout,
            flush=True,
        )

    for index, command in enumerate(install_commands(paths, mode)):
        try:
            result = subprocess.run(command, check=False, env=child_env)
        except OSError:
            _print_install_failure(mode, index, stdout)
            return 1
        if result.returncode != 0:
            _print_install_failure(mode, index, stdout)
            return 1

    try:
        paths.hash_cache.write_text(f"{current_hash}\n", encoding="utf-8")
    except OSError:
        # The legacy shell write was best-effort (start.sh does not use
        # ``set -e``). A failed cache write therefore causes a reinstall next
        # launch but does not turn an otherwise successful install into fail.
        pass
    print("✅ Dependencies are up to date", file=stdout, flush=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install bridge Python dependencies")
    parser.add_argument("--bridge-dir", required=True, type=Path)
    parser.add_argument("--venv-dir", required=True, type=Path)
    parser.add_argument("--project-env", required=True, type=Path)
    parser.add_argument(
        "--process-unlocked",
        help="CCC_DEPS_UNLOCKED captured before start.sh merges dotenv files",
    )
    parser.add_argument("--force-install", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = DependencyPaths.from_roots(args.bridge_dir, args.venv_dir, args.project_env)
    process_value = (
        args.process_unlocked
        if args.process_unlocked is not None
        else os.environ.get("CCC_DEPS_UNLOCKED")
    )
    mode = resolve_install_mode(process_value, paths.project_env, paths.bridge_env)
    return sync_dependencies(
        paths,
        mode,
        force_install=args.force_install,
        environ=os.environ,
    )


if __name__ == "__main__":
    raise SystemExit(main())
