#!/usr/bin/env python3
"""Enforce the ccc-node GitHub CLI-first policy in Codex config.

Only the canonical GitHub plugin toggle is changed. The rest of config.toml is
kept byte-for-byte so node-local settings, comments, and credentials are never
re-rendered or printed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tomllib


PLUGIN_KEY = "github@openai-curated-remote"
PLUGIN_HEADER = f'[plugins."{PLUGIN_KEY}"]'
CONFIG_NAME = "config.toml"
LOCK_NAME = ".ccc-github-cli-policy.lock"
MAX_CONFIG_BYTES = 1024 * 1024

_PLUGIN_HEADER_RE = re.compile(
    rf'^\s*\[plugins\."{re.escape(PLUGIN_KEY)}"\]\s*(?:#.*)?$', re.MULTILINE
)
_ANY_TABLE_RE = re.compile(r"^\s*\[\[?[^\r\n]+\]\]?\s*(?:#.*)?$", re.MULTILINE)
_ENABLED_RE = re.compile(
    r"^(?P<prefix>\s*enabled\s*=\s*)(?P<value>true|false)(?P<suffix>\s*(?:#.*)?)$",
    re.MULTILINE,
)


class PolicyError(RuntimeError):
    pass


def _safe_json(status: str, *, changed: bool = False, code: str | None = None) -> str:
    payload: dict[str, object] = {"status": status, "changed": changed}
    if code is not None:
        payload["code"] = code
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _validate_home(path: Path, *, create: bool) -> bool:
    if path == Path(path.anchor):
        raise PolicyError("codex_home_unsafe")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            component = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(component.st_mode):
            raise PolicyError("codex_home_unsafe")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise PolicyError("codex_home_unsafe")
    return True


def _read_config(path: Path) -> tuple[str, int, tuple[int, int, int, int]] | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_size > MAX_CONFIG_BYTES
    ):
        raise PolicyError("config_unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) & 0o022
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise PolicyError("config_raced")
            chunks: list[bytes] = []
            remaining = MAX_CONFIG_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except PolicyError:
        raise
    except OSError as exc:
        raise PolicyError("config_read_failed") from exc
    raw = b"".join(chunks)
    if len(raw) > MAX_CONFIG_BYTES:
        raise PolicyError("config_too_large")
    try:
        signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        return raw.decode("utf-8"), stat.S_IMODE(before.st_mode), signature
    except UnicodeDecodeError as exc:
        raise PolicyError("config_not_utf8") from exc


def _parse(text: str) -> dict[str, object]:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError("config_invalid_toml") from exc
    if not isinstance(parsed, dict):
        raise PolicyError("config_invalid_toml")
    return parsed


def _plugin_entry(parsed: dict[str, object]) -> object | None:
    plugins = parsed.get("plugins")
    if plugins is None:
        return None
    if not isinstance(plugins, dict):
        raise PolicyError("plugins_config_unsupported")
    return plugins.get(PLUGIN_KEY)


def render_disabled(text: str) -> str:
    parsed = _parse(text)
    entry = _plugin_entry(parsed)
    matches = list(_PLUGIN_HEADER_RE.finditer(text))
    if len(matches) > 1:
        raise PolicyError("plugin_table_duplicated")

    if not matches:
        if entry is not None:
            # Inline/dotted representations are valid TOML, but rewriting them
            # without a full TOML-preserving editor could damage comments.
            raise PolicyError("plugin_config_noncanonical")
        separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        rendered = f"{text}{separator}{PLUGIN_HEADER}\nenabled = false\n"
    else:
        match = matches[0]
        next_table = _ANY_TABLE_RE.search(text, match.end())
        section_end = next_table.start() if next_table is not None else len(text)
        section = text[match.end() : section_end]
        enabled = list(_ENABLED_RE.finditer(section))
        if len(enabled) > 1:
            raise PolicyError("plugin_enabled_duplicated")
        if enabled:
            flag = enabled[0]
            absolute_start = match.end() + flag.start()
            absolute_end = match.end() + flag.end()
            replacement = f"{flag.group('prefix')}false{flag.group('suffix')}"
            rendered = text[:absolute_start] + replacement + text[absolute_end:]
        else:
            rendered = text[: match.end()] + "\nenabled = false" + text[match.end() :]

    verified = _parse(rendered)
    verified_entry = _plugin_entry(verified)
    if not isinstance(verified_entry, dict) or verified_entry.get("enabled") is not False:
        raise PolicyError("policy_verification_failed")
    return rendered


def _write_atomic(
    home: Path, text: str, expected: tuple[int, int, int, int] | None
) -> None:
    config_path = home / CONFIG_NAME
    temp_name = f".{CONFIG_NAME}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    dir_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        dir_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        dir_flags |= os.O_NOFOLLOW
    dir_fd = os.open(home, dir_flags)
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        payload = text.encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            current = os.stat(CONFIG_NAME, dir_fd=dir_fd, follow_symlinks=False)
            current_signature = (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            )
        except FileNotFoundError:
            current_signature = None
        if current_signature != expected:
            raise PolicyError("config_raced")
        os.replace(temp_name, CONFIG_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    except PolicyError:
        raise
    except OSError as exc:
        raise PolicyError("config_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
        except OSError:
            pass
        os.close(dir_fd)
    if not config_path.is_file():
        raise PolicyError("policy_verification_failed")


def apply_policy(home: Path) -> bool:
    _validate_home(home, create=True)
    lock_path = home / LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise PolicyError("lock_unsafe") from exc
    try:
        lock_meta = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_meta.st_mode)
            or lock_meta.st_uid != os.geteuid()
            or lock_meta.st_nlink != 1
        ):
            raise PolicyError("lock_unsafe")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existing = _read_config(home / CONFIG_NAME)
        text, mode, signature = existing if existing is not None else ("", 0o600, None)
        rendered = render_disabled(text)
        changed = rendered != text or mode != 0o600
        if changed:
            _write_atomic(home, rendered, signature)
        return changed
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def policy_status(home: Path) -> str:
    if not _validate_home(home, create=False):
        return "missing"
    existing = _read_config(home / CONFIG_NAME)
    if existing is None:
        return "missing"
    parsed = _parse(existing[0])
    entry = _plugin_entry(parsed)
    if not isinstance(entry, dict):
        return "missing"
    return "disabled" if entry.get("enabled") is False else "enabled"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enforce Codex GitHub CLI-first policy")
    parser.add_argument("command", choices=("apply", "status"))
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    raw_home = args.codex_home or Path(os.environ.get("CODEX_HOME", "~/.codex"))
    home = Path(os.path.abspath(os.path.expanduser(str(raw_home))))
    try:
        if args.command == "apply":
            changed = apply_policy(home)
            if args.json:
                print(_safe_json("disabled", changed=changed))
            else:
                print("Codex GitHub plugin disabled (gh CLI-first policy).")
            return 0
        status = policy_status(home)
        if args.json:
            print(_safe_json(status))
        else:
            print(status)
        return 0 if status == "disabled" else 3
    except PolicyError as exc:
        if args.json:
            print(_safe_json("error", code=str(exc)))
        else:
            print(f"ccc-codex-github-policy: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
