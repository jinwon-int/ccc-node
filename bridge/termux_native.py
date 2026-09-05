"""Repair the Android libpython dependency of locally built cryptography wheels.

Runs before importing bridge dependencies, including on a requirements cache
hit. Only the venv's cryptography extension is eligible; downloads and version
pins remain pip's responsibility.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO


def _regular_owned(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError(f"symlink rejected: {item}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError(f"expected an owner-controlled regular file: {path}")


def _probe(python: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), "-c", "from cryptography.exceptions import InvalidTag"],
        env=env, text=True, capture_output=True, check=False, timeout=30,
    )


def _extension_info(python: Path, venv: Path, env: Mapping[str, str]) -> tuple[Path, str]:
    result = subprocess.run(
        [str(python), "-c",
         "import cryptography,json,pathlib,sysconfig; "
         "print(json.dumps([str(pathlib.Path(cryptography.__file__).parent / "
         "'hazmat/bindings/_rust.abi3.so'),sysconfig.get_config_var('LDLIBRARY')]))"],
        env=env, text=True, capture_output=True, check=True, timeout=30,
    )
    filename, library = json.loads(result.stdout)
    extension = Path(filename)
    if not extension.is_absolute() or not extension.is_relative_to(venv.absolute()):
        raise ValueError("cryptography extension is outside the selected venv")
    if not isinstance(library, str) or not library.startswith("libpython") or "/" in library:
        raise ValueError("interpreter did not report a libpython shared-library name")
    _regular_owned(extension)
    return extension, library


def _repair(
    extension: Path, library: str, python: Path, patchelf: str,
    env: Mapping[str, str], stdout: TextIO,
) -> None:
    # Keep original and failed candidates for manual recovery. The private
    # directory is beside the extension so the final replace is atomic.
    original = extension.read_bytes()
    recovery = Path(tempfile.mkdtemp(prefix=".ccc-native-recovery-", dir=extension.parent))
    backup = recovery / "original.so"
    candidate = recovery / "patched.so"
    for path in (backup, candidate):
        with path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(original)
    print(f"   Native extension backup: {backup}", file=stdout, flush=True)
    subprocess.run(
        [patchelf, "--add-needed", library, str(candidate)],
        env=env, capture_output=True, text=True, check=True, timeout=30,
    )
    # dlopen the candidate in the target interpreter before replacing anything.
    subprocess.run(
        [str(python), "-c", "import ctypes,sys; ctypes.CDLL(sys.argv[1])", str(candidate)],
        env=env, capture_output=True, text=True, check=True, timeout=30,
    )
    _regular_owned(extension)
    if extension.read_bytes() != original:
        raise ValueError("cryptography changed during repair; original left untouched")
    patched = candidate.read_bytes()
    os.replace(candidate, extension)
    try:
        result = _probe(python, env)
        if result.returncode:
            raise RuntimeError("cryptography still fails to import after link repair")
    except (OSError, subprocess.SubprocessError, RuntimeError):
        # Preserve the failed candidate and restore the known original. A
        # conflicting writer is never silently overwritten during rollback.
        _regular_owned(extension)
        if extension.read_bytes() != patched:
            raise RuntimeError(f"rollback conflict; restore manually from {backup}")
        os.replace(extension, recovery / "failed.so")
        restored = recovery / "restore.so"
        with restored.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(original)
        os.replace(restored, extension)
        raise
    print("✅ Repaired Termux cryptography libpython linkage", file=stdout, flush=True)


def ensure_termux_cryptography(
    python: Path, venv: Path, env: Mapping[str, str], stdout: TextIO,
) -> int:
    """Import or repair cryptography on Termux; fail before starting a broken bot."""
    if not (env.get("TERMUX_VERSION") or "/com.termux/" in env.get("PREFIX", "")):
        return 0
    try:
        # fcntl is unavailable on Windows; import it only on the Termux path.
        import fcntl

        if _probe(python, env).returncode == 0:
            return 0
        lock_path = venv / ".termux-native.lock"
        for item in (lock_path, *lock_path.parents):
            if item.is_symlink():
                raise ValueError(f"symlink rejected: {item}")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            _regular_owned(lock_path)
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = _probe(python, env)
            if result.returncode == 0:
                return 0
            if 'cannot locate symbol "Py' not in result.stderr:
                raise RuntimeError("cryptography import failed for a reason other than libpython linkage")
            extension, library = _extension_info(python, venv, env)
            patchelf = shutil.which("patchelf", path=env.get("PATH"))
            if patchelf is None:
                raise RuntimeError("missing patchelf; run: pkg install patchelf")
            needed = subprocess.run(
                [patchelf, "--print-needed", str(extension)], env=env,
                capture_output=True, text=True, check=True, timeout=30,
            ).stdout.splitlines()
            if library in needed:
                raise RuntimeError("cryptography already links libpython but cannot import")
            _repair(extension, library, python, patchelf, env, stdout)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # Do not print child command output: the target interpreter may have
        # arbitrary site customizations. Paths and exception types suffice.
        detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        print(f"❌ Termux cryptography readiness failed: {detail}", file=stdout, flush=True)
        return 1
