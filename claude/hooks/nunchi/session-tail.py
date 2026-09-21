#!/usr/bin/env python3
"""Read only a bounded tail of an owned native session, excluding tool messages."""

import json
import os
from pathlib import Path
import stat
import sys

CAP = 4 * 1024 * 1024


def read_tail(provider, path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root):
        raise ValueError("outside_root")
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError("symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_uid != os.geteuid():
            raise ValueError("unsafe_source")
        if provider == "piri" and meta.st_mode & 0o077:
            raise ValueError("unsafe_permissions")
        offset = max(0, meta.st_size - CAP)
        raw = os.pread(fd, min(meta.st_size, CAP), offset)
        if offset:
            raw = raw.partition(b"\n")[2]
    finally:
        os.close(fd)
    return raw


def read(provider, path, root):
    raw = read_tail(provider, path, root)
    messages = []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if provider == "codex":
            if d.get("type") != "event_msg":
                continue
            item = d.get("payload") or {}
            role = {"user_message": "USER", "agent_message": "AGENT"}.get(item.get("type"))
            text = item.get("message", "")
            if "nunchi-codex-feed-816" in str(text):
                return ""
        elif provider == "piri":
            if d.get("type") != "message":
                continue
            item = d.get("message") or {}
            role = {"user": "USER", "assistant": "AGENT"}.get(item.get("role"))
            content = item.get("content")
            text = (
                content
                if isinstance(content, str)
                else " ".join(
                    b.get("text", "")
                    for b in (content or [])
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            )
        else:
            raise ValueError("provider")
        if role and isinstance(text, str):
            messages.append(role + ": " + text[:800])
    return "\n".join(messages[-60:])[:40000]


if __name__ == "__main__":
    try:
        print(read(*sys.argv[1:]))
    except (OSError, ValueError, TypeError, AttributeError):
        print("nunchi-feed: source unreadable", file=sys.stderr)
        raise SystemExit(2)
