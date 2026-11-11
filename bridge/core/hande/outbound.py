"""「한데」 outbound attachments (#2764): agent files and images as ``attachment`` frames.

Contract: family-messenger ``docs/FLEET-WORKER.md`` "나가는 첨부 — ``attachment`` 출력
프레임" (#761). The mirror of :mod:`.attachments`: the bridge writes the plaintext
of a deliverable the answer names into the shared spool as
``out-<32 hex>.bin`` and names it in a frame sent **before** ``result``::

    {"type": "attachment", "turn_id": "t-12",
     "attachment": {"v": 1, "path": "<spool>/out-<32 hex>.bin", "kind": "image"|"file",
                    "mime": "<sniffed hint>", "name": "<display name>",
                    "size": 123, "sha256": "<plaintext sha256>"}}

The bot encrypts, uploads and posts it (FILE-V2 §2·§3), answers
``attachment_ack``/``attachment_nack`` on the same connection and deletes the
spool file. Which files go is the Telegram/Matrix auto-send rule
(``core/deliverables.py``, ``PROJECT_ROOT`` scope, direct rooms only); this
module only reads one chosen file again (``read_deliverable``: no symlink swap,
still inside the root, bounded) and writes the spool copy.

Ownership mirrors the inbound rule (ccc-node#2625): a spool directory writable
by *other* is refused. When the bridge runs as the spool directory's owner
(Termux: one uid) the file is 0600; otherwise (Linux: the bot owns the 2770
directory, the bridge is root or in its group) it is 0640 in the directory's
group, so the bot can read and delete it. Nothing here logs a path, a name or
file bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from telegram_bot.core.matrix.media import IMAGE_EXTENSIONS
from telegram_bot.core.matrix.outbound_media import OutboundMediaError, read_deliverable

from .attachments import CONTROL_PATTERN, MAX_NAME_CHARS, MIME_PATTERN, kind_for_mime

FRAME_VERSION = 1
OUT_NAME_PATTERN = re.compile(r"out-[0-9a-f]{32}\.bin")
# The bot's per-turn ceiling (``DEFAULT_ATTACHMENT_OUT_MAX``); more would be nacked ``limit``.
MAX_ATTACHMENTS_PER_TURN = 4
# FILE-V2's hard ceiling and the Telegram/Matrix auto-send cap (``MAX_OUTBOUND_FILE_BYTES``).
# The bot's own (lower, configurable) limit answers ``oversize``.
MAX_OUT_FILE_BYTES = 50 * 1024 * 1024
# Per attachment: how long ``result`` waits for the bot's answer (an older bot never answers).
ACK_TIMEOUT_S = 60.0
# The bot deletes a spool file once it has handled it; the bridge sweeps what is left.
CLEANUP_DELAY_S = 30.0
MAX_ATTACHMENT_JSON_BYTES = 2048
# Left out of ``name`` entirely (FILE-V2 storage-name rule), never "cleaned".
BIDI_PATTERN = re.compile("[؜‎‏‪-‮⁦-⁩]")
NACK_REASON_PATTERN = re.compile(r"[a-z0-9_]{1,32}")

ATTACHMENT_NOT_SENT = "📎 첨부를 보내지 못했습니다: {name}({reason})"
NAMELESS = "파일"
FILES_SKIPPED = (
    "📎 파일 {count}개는 보내지 않았습니다(프로젝트 폴더 밖이거나 한 번에 보낼 수 있는 "
    f"{MAX_ATTACHMENTS_PER_TURN}개 초과)."
)

_CHUNK = 1 << 20


class OutboundAttachmentError(RuntimeError):
    """A body-free reason one deliverable could not be handed to the bot."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PreparedAttachment:
    """One spool file written for the bot; ``attachment`` is the frame object."""

    spool_name: str
    sha256: str
    display: str
    attachment: dict[str, Any]


def new_spool_name() -> str:
    return f"out-{secrets.token_hex(16)}.bin"


def display_name(path: Path | str) -> str | None:
    """The last path component as a FILE-V2 ``name``, or ``None`` when it may not be one."""

    name = os.path.basename(str(path))
    if (
        not 1 <= len(name) <= MAX_NAME_CHARS
        or CONTROL_PATTERN.search(name) is not None
        or BIDI_PATTERN.search(name) is not None
    ):
        return None
    return name


def not_sent_line(name: str | None, reason: str) -> str:
    """The fixed line appended to the answer; the reason is a short code, never a path."""

    if not NACK_REASON_PATTERN.fullmatch(reason):
        reason = "error"
    return ATTACHMENT_NOT_SENT.format(name=name or NAMELESS, reason=reason)


def _image_magic(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx"):
            return "image/heic"
        if brand in (b"mif1", b"msf1", b"heif"):
            return "image/heif"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return None


def sniff_mime(data: bytes, path: Path | str) -> str:
    """Magic bytes for the image allowlist and PDF, else the extension guess.

    A hint only — the bot sniffs the plaintext again. An image type is claimed
    only from magic bytes (``kind`` follows it), so a ``.png`` that is not a
    PNG goes as ``application/octet-stream`` rather than as an image.
    """

    head = data[:32]
    magic = _image_magic(head)
    if magic is not None:
        return magic
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    guessed, _encoding = mimetypes.guess_type(os.path.basename(str(path)))
    if (
        not guessed
        or guessed.lower() in IMAGE_EXTENSIONS
        or len(guessed) > 100
        or MIME_PATTERN.fullmatch(guessed) is None
    ):
        return "application/octet-stream"
    return guessed


def _open_spool(spool_dir: str) -> tuple[int, os.stat_result]:
    try:
        directory_fd = os.open(spool_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise OutboundAttachmentError("spool") from None
    try:
        meta = os.fstat(directory_fd)
    except OSError:
        os.close(directory_fd)
        raise OutboundAttachmentError("spool") from None
    if meta.st_mode & stat.S_IWOTH:
        # Anyone could replace or read what lands there (ccc-node#2625).
        os.close(directory_fd)
        raise OutboundAttachmentError("spool")
    return directory_fd, meta


def _file_mode(spool_meta: os.stat_result) -> int:
    """0600 when the bridge owns the spool (one uid); 0640 for the bot's group otherwise."""

    if not hasattr(os, "getuid") or spool_meta.st_uid == os.getuid():
        return 0o600
    return 0o640


def write_spool_file(spool_dir: str, spool_name: str, data: bytes) -> None:
    """Create ``spool_name`` in ``spool_dir`` (``O_EXCL|O_NOFOLLOW``), write, fsync."""

    if OUT_NAME_PATTERN.fullmatch(spool_name) is None:
        raise OutboundAttachmentError("spool")
    directory_fd, spool_meta = _open_spool(spool_dir)
    try:
        mode = _file_mode(spool_meta)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(spool_name, flags, mode, dir_fd=directory_fd)
        except OSError:
            raise OutboundAttachmentError("spool") from None
        try:
            try:
                if mode == 0o640:
                    info = os.fstat(descriptor)
                    if info.st_gid != spool_meta.st_gid:
                        # A setgid (2770) spool already does this; otherwise try.
                        try:
                            os.fchown(descriptor, -1, spool_meta.st_gid)
                        except OSError:
                            pass
                os.fchmod(descriptor, mode)  # the umask must not narrow it
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view[:_CHUNK])
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            _unlink(directory_fd, spool_name)
            raise OutboundAttachmentError("spool") from None
    finally:
        os.close(directory_fd)


def _unlink(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except OSError:
        pass


def remove_spool_files(spool_dir: str, names: list[str] | set[str]) -> int:
    """Delete the bridge's own ``out-*.bin`` files; a missing one is fine. Returns removed."""

    wanted = [name for name in names if OUT_NAME_PATTERN.fullmatch(name)]
    if not wanted:
        return 0
    try:
        directory_fd = os.open(spool_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return 0
    removed = 0
    try:
        for name in wanted:
            try:
                os.unlink(name, dir_fd=directory_fd)
                removed += 1
            except OSError:
                pass  # ENOENT: the bot already took it
    finally:
        os.close(directory_fd)
    return removed


def _local_reason(error: OutboundMediaError) -> str:
    return "oversize" if error.reason == "too-large" else "file"


def prepare(
    path: Path,
    *,
    spool_dir: str,
    spool_name: str,
    root: Path | None,
    max_bytes: int = MAX_OUT_FILE_BYTES,
) -> PreparedAttachment:
    """Read one chosen deliverable again and hand its plaintext to the spool.

    Raises :class:`OutboundAttachmentError` (``file``/``oversize``/``spool``) and
    leaves no spool file behind when it does.
    """

    try:
        data = read_deliverable(path, max_bytes=max_bytes, root=root)
    except OutboundMediaError as error:
        raise OutboundAttachmentError(_local_reason(error)) from None
    if not data:
        raise OutboundAttachmentError("file")  # FILE-V2 size is 1..
    digest = hashlib.sha256(data).hexdigest()
    mime = sniff_mime(data, path)
    name = display_name(path)
    attachment: dict[str, Any] = {
        "v": FRAME_VERSION,
        "path": os.path.join(spool_dir, spool_name),
        "kind": kind_for_mime(mime),
        "mime": mime,
        "size": len(data),
        "sha256": digest,
    }
    if name is not None:
        attachment["name"] = name
        if len(_json_bytes(attachment)) > MAX_ATTACHMENT_JSON_BYTES:
            attachment.pop("name")
    write_spool_file(spool_dir, spool_name, data)
    return PreparedAttachment(
        spool_name=spool_name,
        sha256=digest,
        display=name or NAMELESS,
        attachment=attachment,
    )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


__all__ = [
    "ACK_TIMEOUT_S",
    "CLEANUP_DELAY_S",
    "FILES_SKIPPED",
    "MAX_ATTACHMENTS_PER_TURN",
    "MAX_OUT_FILE_BYTES",
    "OUT_NAME_PATTERN",
    "OutboundAttachmentError",
    "PreparedAttachment",
    "display_name",
    "new_spool_name",
    "not_sent_line",
    "prepare",
    "remove_spool_files",
    "sniff_mime",
    "write_spool_file",
]
