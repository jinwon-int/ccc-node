"""「한데」 inbound attachments (#2606).

Contract: family-messenger ``docs/FLEET-WORKER.md`` "첨부 turn — ``attachment``".
The native bot decrypts a file-v2/v1 attachment itself (the per-file key never
leaves it), writes the plaintext to a spool file and names that file in the
``turn`` frame::

    "attachment": {"v": 1, "path": "<spool>/<32 hex>.bin", "kind": "image"|"file",
                   "mime": "<sniffed type/subtype>", "name": "<hint>", "size": 123,
                   "sha256": "<plaintext sha256>", "caption": ""}

:func:`parse_frame_attachment` checks the object's shape (the transport answers
``invalid-attachment`` when it is wrong). :func:`adopt` takes the file over by
renaming it into the bridge's private staging directory, and only then verifies
it. Whatever happens after the rename, the bridge owns the file and deletes it,
so the bot never races the bridge on one path. A file the bridge never renamed
(oversize, knob off, frame refused) stays the bot's to delete.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from telegram_bot.core.matrix.media import IMAGE_EXTENSIONS, AttachmentError, staged_name

FRAME_VERSION = 1
SPOOL_NAME_PATTERN = re.compile(r"[0-9a-f]{32}\.bin")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
# FILE-V2 descriptor MIME shape (``archive/native-mls/web/hande/file-v2.js``).
MIME_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}"
)
CONTROL_PATTERN = re.compile(r"[\u0000-\u001f\u007f-\u009f]")
MAX_NAME_CHARS = 255
# A caption is a person's text like ``prompt``; the bot bounds the whole frame
# at 64 KiB, this only stops one field from taking all of it.
MAX_CAPTION_BYTES = 8 * 1024
_CHUNK = 65_536


class AttachmentFrameError(ValueError):
    """The ``attachment`` object breaks the frame contract (answered ``invalid-attachment``)."""


def _bad(reason: str) -> AttachmentFrameError:
    return AttachmentFrameError(reason)


def kind_for_mime(mime: str) -> str:
    """``image`` exactly when the bridge would stage the MIME as an image."""

    return "image" if mime.lower() in IMAGE_EXTENSIONS else "file"


def parse_frame_attachment(raw: Any, spool_dir: str) -> dict[str, Any] | None:
    """Validate a ``turn.attachment`` object against ``spool_dir``.

    Returns the normalised attachment, ``None`` for a version this bridge does
    not know (the turn then runs on its ``prompt``, the bot's fallback text),
    or raises :class:`AttachmentFrameError`. Unknown keys are ignored.
    """

    if not isinstance(raw, Mapping):
        raise _bad("not-an-object")
    version = raw.get("v")
    if type(version) is not int:
        raise _bad("v")
    if version != FRAME_VERSION:
        return None
    path = raw.get("path")
    if (
        not isinstance(path, str)
        or not os.path.isabs(path)
        or os.path.dirname(path) != spool_dir
        or SPOOL_NAME_PATTERN.fullmatch(os.path.basename(path)) is None
    ):
        raise _bad("path")
    mime = raw.get("mime")
    if not isinstance(mime, str) or len(mime) > 100 or MIME_PATTERN.fullmatch(mime) is None:
        raise _bad("mime")
    kind = raw.get("kind")
    if kind not in ("image", "file") or kind != kind_for_mime(mime):
        raise _bad("kind")
    name = raw.get("name")
    if name is not None and (
        not isinstance(name, str)
        or not 1 <= len(name) <= MAX_NAME_CHARS
        or CONTROL_PATTERN.search(name) is not None
    ):
        raise _bad("name")
    size = raw.get("size")
    if type(size) is not int or size < 1:
        raise _bad("size")
    digest = raw.get("sha256")
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        raise _bad("sha256")
    caption = raw.get("caption")
    if not isinstance(caption, str) or len(caption.encode("utf-8", "replace")) > MAX_CAPTION_BYTES:
        raise _bad("caption")
    return {
        "v": FRAME_VERSION,
        "path": path,
        "kind": kind,
        "mime": mime,
        "name": name,
        "size": size,
        "sha256": digest,
        "caption": caption,
    }


def matrix_shape(attachment: Mapping[str, Any]) -> dict[str, Any]:
    """The keys the shared Matrix staging/prompt helpers read (``mimetype``, not ``mime``)."""

    return {
        "kind": attachment["kind"],
        "mimetype": attachment["mime"],
        "name": attachment.get("name"),
        "size": attachment["size"],
    }


def _open_directory(path: str) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _verify(directory_fd: int, name: str, attachment: Mapping[str, Any]) -> None:
    """The staged file is the bot's: a private regular file of the promised bytes."""

    try:
        # O_NONBLOCK: a FIFO planted in the spool must not hang the turn.
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
    except OSError:
        raise AttachmentError("invalid") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AttachmentError("invalid")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise AttachmentError("invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AttachmentError("invalid")
        if metadata.st_size != attachment["size"]:
            raise AttachmentError("integrity")
        digest = hashlib.sha256()
        remaining = attachment["size"] + 1  # one byte more would show a growing file
        while remaining > 0:
            chunk = os.read(descriptor, min(_CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining != 1 or not hmac.compare_digest(digest.hexdigest(), attachment["sha256"]):
            raise AttachmentError("integrity")
    finally:
        os.close(descriptor)


def adopt(
    attachment: Mapping[str, Any], *, spool_dir: str, staging_dir: Path, limit: int
) -> Path:
    """Move the bot's spool file into ``staging_dir`` and verify it; return the staged path.

    Raises :class:`AttachmentError` (``oversize``/``missing``/``storage``/``invalid``/
    ``integrity``). After the rename the file is the bridge's: a failed check
    deletes it here, a passed one leaves it for the caller to delete when the
    turn ends. ``oversize`` is decided before the rename, so that file stays the
    bot's to remove.
    """

    from telegram_bot.core import media as core_media

    if attachment["size"] > limit:
        raise AttachmentError("oversize")
    source = os.path.basename(str(attachment["path"]))
    target = staged_name(matrix_shape(attachment))
    try:
        spool_fd = _open_directory(spool_dir)
    except OSError:
        raise AttachmentError("missing") from None
    try:
        try:
            staging_fd = core_media.open_private_document_directory(staging_dir)
        except Exception:  # noqa: BLE001 - unsafe/unwritable staging dir is a storage refusal
            raise AttachmentError("storage") from None
        try:
            try:
                os.rename(source, target, src_dir_fd=spool_fd, dst_dir_fd=staging_fd)
            except FileNotFoundError:
                raise AttachmentError("missing") from None
            except OSError as error:
                # EXDEV: another filesystem; copying would leave two owners.
                raise AttachmentError(
                    "storage" if error.errno in (errno.EXDEV, errno.EACCES, errno.EPERM) else "invalid"
                ) from None
            try:
                _verify(staging_fd, target, attachment)
            except BaseException:
                try:
                    os.unlink(target, dir_fd=staging_fd)
                except OSError:
                    pass
                raise
        finally:
            os.close(staging_fd)
    finally:
        os.close(spool_fd)
    return staging_dir / target
