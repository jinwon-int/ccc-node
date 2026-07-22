"""Secure private-file logging setup for the bridge.

Extracted from utils/config.py (#584 P0-5): configuration parsing and the
secure logging bootstrap are separate concerns. This module owns the
O_NOFOLLOW / owner-only log-file discipline and the root-logger wiring.
config.py does not import this module, so it stays a standalone leaf.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from pathlib import Path
from typing import Any

# Captured at import time so a test that swaps the config module in
# sys.modules cannot change which settings object the default path uses
# (the pre-extraction code had the same import-time binding semantics).
from telegram_bot.utils.config import config as _default_config


def _open_private_log_directory(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise RuntimeError("secure logging requires O_NOFOLLOW and O_DIRECTORY support")
    flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(f"log parent is not a directory: {path}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError(f"log parent is not owned by the current user: {path}")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise PermissionError(f"log parent is writable by group or others: {path}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _private_log_handler_at(dir_fd: int, name: str, display_path: Path) -> logging.FileHandler:
    """Open a final log component relative to an already validated directory fd."""
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError(f"invalid log file name: {name!r}")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("secure logging requires O_NOFOLLOW support")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK, errno.ENXIO}:
            raise PermissionError(f"refusing unsafe log file: {display_path}") from exc
        raise

    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PermissionError(f"log path is not a regular file: {display_path}")
        if file_stat.st_nlink != 1:
            raise PermissionError(f"log file has multiple hard links: {display_path}")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise PermissionError(f"log file is not owned by the current user: {display_path}")
        os.fchmod(fd, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise PermissionError(f"could not enforce owner-only log mode: {display_path}")

        stream = os.fdopen(fd, "a", encoding="utf-8")
        fd = -1
        handler = logging.FileHandler(display_path, encoding="utf-8", delay=True)
        handler.stream = stream
        return handler
    finally:
        if fd >= 0:
            os.close(fd)


def _private_log_handler(path: Path) -> logging.FileHandler:
    """Compatibility wrapper that anchors the final open to the parent directory."""
    dir_fd = _open_private_log_directory(path.parent)
    try:
        return _private_log_handler_at(dir_fd, path.name, path)
    finally:
        os.close(dir_fd)


def _private_log_handlers(logs_dir: Path, names: tuple[str, ...]) -> list[logging.FileHandler]:
    """Open all handlers against one directory identity or publish none of them."""
    dir_fd = _open_private_log_directory(logs_dir)
    handlers: list[logging.FileHandler] = []
    created: list[str] = []
    try:
        for name in names:
            try:
                os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                existed = True
            except FileNotFoundError:
                existed = False
            handler = _private_log_handler_at(dir_fd, name, logs_dir / name)
            handlers.append(handler)
            if not existed:
                created.append(name)
        return handlers
    except Exception:
        for handler in handlers:
            handler.close()
        for name in created:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            except OSError:
                logging.getLogger(__name__).warning(
                    "Failed to roll back newly-created log artifact %s", logs_dir / name
                )
        raise
    finally:
        os.close(dir_fd)


def setup_logging(settings: Any = None) -> None:
    """Setup logging configuration with console and private file output."""
    runtime_config: Any = _default_config if settings is None else settings
    logs_dir = runtime_config.logs_dir
    from datetime import datetime

    from telegram_bot.utils.secure_fs import ensure_private_directory

    ensure_private_directory(logs_dir)
    error_name = f"error_{datetime.now().strftime('%Y-%m-%d')}.log"
    fh, efh = _private_log_handlers(logs_dir, ("bot.log", error_name))

    log_level = getattr(logging, runtime_config.log_level.upper())
    formatter = logging.Formatter(runtime_config.log_format)
    is_debug = os.environ.get("BOT_DEBUG")
    console_level = log_level if is_debug else logging.WARNING

    try:
        logging.basicConfig(level=log_level, format=runtime_config.log_format)
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        for handler in root_logger.handlers:
            handler.setLevel(console_level)

        fh.setLevel(log_level)
        fh.setFormatter(formatter)
        root_logger.addHandler(fh)

        sep = "=" * 60
        efh.setLevel(logging.ERROR)
        efh.setFormatter(
            logging.Formatter(
                f"\n{sep}\n[%(asctime)s] %(name)s - %(levelname)s\n%(message)s\n{sep}"
            )
        )
        root_logger.addHandler(efh)

        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("telegram").setLevel(logging.WARNING)
        logging.getLogger("telegram.ext").setLevel(logging.WARNING)
        logging.getLogger("telegram.ext.ExtBot").setLevel(logging.WARNING)
    except Exception:
        root_logger = logging.getLogger()
        for handler in (fh, efh):
            root_logger.removeHandler(handler)
            handler.close()
        raise
