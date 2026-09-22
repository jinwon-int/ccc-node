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


def text_blocks(content, allowed):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        b.get("text", "")
        for b in (content or [])
        if isinstance(b, dict) and isinstance(b.get("type"), str)
        and b["type"] in allowed and isinstance(b.get("text"), str)
    )


def codex_message(d):
    item = d.get("payload") or {}
    if not isinstance(item, dict):
        return None, "", False
    if d.get("type") == "response_item" and item.get("type") == "message":
        role = {"user": "USER", "assistant": "AGENT"}.get(str(item.get("role")))
        return role, text_blocks(item.get("content"), {"input_text", "output_text"}), True
    if d.get("type") == "event_msg":
        role = {"user_message": "USER", "agent_message": "AGENT"}.get(str(item.get("type")))
        return role, item.get("message", ""), False
    return None, "", False


def read(provider, path, root):
    raw = read_tail(provider, path, root)
    messages, legacy = [], []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        modern = True
        if provider == "codex":
            role, text, modern = codex_message(d)
            if (
                role == "USER"
                and isinstance(text, str)
                and text.rstrip().endswith("[nunchi-codex-feed-816]")
            ):
                return ""
        elif provider == "piri":
            if d.get("type") != "message":
                continue
            item = d.get("message") or {}
            if not isinstance(item, dict):
                continue
            role = {"user": "USER", "assistant": "AGENT"}.get(str(item.get("role")))
            text = text_blocks(item.get("content"), {"text"})
        else:
            raise ValueError("provider")
        if role and isinstance(text, str) and text.strip():
            (messages if modern else legacy).append(role + ": " + text[:800])
    # Older Codex duplicates messages as event_msg; prefer the native message
    # representation when present, never tool results or hidden reasoning.
    return "\n".join((messages or legacy)[-60:])[:40000]


if __name__ == "__main__":
    try:
        print(read(*sys.argv[1:]))
    except (OSError, ValueError, TypeError, AttributeError):
        print("nunchi-feed: source unreadable", file=sys.stderr)
        raise SystemExit(2)
