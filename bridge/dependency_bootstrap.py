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


def _is_termux_env(env: Mapping[str, str]) -> bool:
    return bool(env.get("TERMUX_VERSION")) or "/com.termux/" in env.get("PREFIX", "")


# #969: "install ok" is not "usable". On Android/Termux a wheel can install
# cleanly yet fail to load: cryptography's _rust.abi3.so is not linked against
# libpython, and Android's loader does not resolve symbols from the
# interpreter executable the way glibc does. The artifact sits inert for
# months and only detonates when some future code path (pyjwt RS256/ES256 —
# service-account JWTs, signed webhooks, OIDC) touches it. Smoke-import the
# binary extensions after every reconcile so the break surfaces at the
# bootstrap boundary with its real name instead of as a "JWT bug".
SMOKE_IMPORTS_ENV = "CCC_DEPS_SMOKE_IMPORTS"
SMOKE_STRICT_ENV = "CCC_DEPS_SMOKE_STRICT"
DEFAULT_SMOKE_IMPORTS = ("cryptography",)


def _smoke_import_modules(environ: Mapping[str, str]) -> tuple[str, ...]:
    raw = environ.get(SMOKE_IMPORTS_ENV)
    if raw is None:
        return DEFAULT_SMOKE_IMPORTS
    return tuple(module for module in re.split(r"[,\s]+", raw.strip()) if module)


def smoke_import_binary_extensions(
    paths: DependencyPaths,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    """Import-check binary extensions in the venv interpreter (#969).

    Warn-only by default: both Termux bridges run healthy today precisely
    because nothing imports cryptography there, so a hard failure would turn
    a latent break into a self-inflicted outage. Returns 1 on failure only
    when CCC_DEPS_SMOKE_STRICT=1 opts into the closed gate.
    """
    env = dict(os.environ if environ is None else environ)
    modules = _smoke_import_modules(env)
    if not modules:
        return 0
    python = paths.pip.with_name("python")
    probe = (
        "import importlib, sys\n"
        "broken = []\n"
        f"for name in {list(modules)!r}:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        broken.append((name, f'{type(exc).__name__}: {exc}'))\n"
        "for name, detail in broken:\n"
        "    print(f'{name}: {detail}')\n"
        "sys.exit(1 if broken else 0)\n"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
    except OSError as exc:
        print(f"⚠️  Binary-extension smoke probe could not run: {exc}", file=stdout, flush=True)
        return 1 if env.get(SMOKE_STRICT_ENV) == "1" else 0
    if result.returncode == 0:
        return 0
    strict = env.get(SMOKE_STRICT_ENV) == "1"
    detail = result.stdout.strip() or result.stderr.strip() or "unknown import failure"
    print(
        "⚠️  Binary-extension import smoke failed — install ok is not usable (#969)",
        file=stdout,
        flush=True,
    )
    for line in detail.splitlines()[:8]:
        print(f"   {line}", file=stdout, flush=True)
    if _is_termux_env(env):
        print(
            "   On Android/Termux this is usually the unlinked-extension gap: the .so is",
            file=stdout,
            flush=True,
        )
        print(
            "   not linked against libpython and Android's loader cannot resolve symbols",
            file=stdout,
            flush=True,
        )
        print(
            "   from the interpreter executable the way glibc does.",
            file=stdout,
            flush=True,
        )
    print(
        "   The bridge does not import these modules on any current code path, so this is",
        file=stdout,
        flush=True,
    )
    print(
        "   LATENT today — the first pyjwt RS256/ES256 path (service-account JWT, signed",
        file=stdout,
        flush=True,
    )
    print(
        "   webhook, OIDC) fails at runtime on this host instead of at install time.",
        file=stdout,
        flush=True,
    )
    if not strict:
        print(
            f"   Continuing (warn-only); set {SMOKE_STRICT_ENV}=1 to fail closed.",
            file=stdout,
            flush=True,
        )
    return 1 if strict else 0


def _cargo_available(env: Mapping[str, str]) -> bool:
    return shutil.which("cargo", path=env.get("PATH")) is not None


def _saved_fingerprint(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return ""


def _print_install_failure(
    mode: InstallMode, command_index: int, stdout: TextIO, *, rust_missing: bool = False
) -> None:
    if mode is InstallMode.LOCKED and command_index == 0:
        print("❌ Hash-locked dependency installation failed", file=stdout, flush=True)
        if rust_missing:
            # #968: on Android/Termux a missing toolchain, not the lock, is the
            # usual killer — name it instead of a maturin/rustup backtrace.
            print("   Likely cause: this Android/Termux host has no Rust toolchain,", file=stdout)
            print("   so packages without an Android-compatible wheel cannot build", file=stdout)
            print("   (maturin needs cargo). Fix and retry:", file=stdout)
            print("   pkg install rust rust-std-aarch64-linux-android", file=stdout)
            print("   CCC_DEPS_UNLOCKED=1 does NOT bypass a missing toolchain.", file=stdout)
        else:
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
        # Hash match only says the inputs did not change; the artifact on disk
        # may still be unloadable (#969 — Termux's inert cryptography wheel).
        return smoke_import_binary_extensions(paths, environ=environ, stdout=stdout)

    print("📦 Installing Python dependencies...", file=stdout, flush=True)
    child_env = dict(os.environ if environ is None else environ)
    ensure_android_api_level(child_env, stdout=stdout)
    rust_missing = _is_termux_env(child_env) and not _cargo_available(child_env)
    if rust_missing:
        print(
            "⚠️  Android/Termux host without a Rust toolchain — packages without "
            "an Android-compatible wheel (e.g. cryptography via maturin) will fail "
            "to build. Install it with: pkg install rust rust-std-aarch64-linux-android",
            file=stdout,
            flush=True,
        )
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
            _print_install_failure(mode, index, stdout, rust_missing=rust_missing)
            return 1

    try:
        paths.hash_cache.write_text(f"{current_hash}\n", encoding="utf-8")
    except OSError:
        # The legacy shell write was best-effort (start.sh does not use
        # ``set -e``). A failed cache write therefore causes a reinstall next
        # launch but does not turn an otherwise successful install into fail.
        pass
    print("✅ Dependencies are up to date", file=stdout, flush=True)
    return smoke_import_binary_extensions(paths, environ=environ, stdout=stdout)


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
