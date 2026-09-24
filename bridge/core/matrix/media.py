"""Matrix media input (#1795): download, decrypt, bound and stage one attachment.

The Telegram bridge hands images/documents to the agent by writing them to a
private local file and putting the path into the prompt (``core/media.py``).
Matrix media is end-to-end encrypted: the event carries an ``EncryptedFile``
(``mxc://`` URL + AES-CTR key + IV + SHA-256 of the ciphertext). This module
fetches the ciphertext through the authenticated media API, verifies and
decrypts it, enforces the same size limits as Telegram, and stores it with the
Telegram document helpers (0700 directory, ``O_EXCL``/``O_NOFOLLOW`` 0600 file).

Every failure is an :class:`AttachmentError` with a short reason code; none of
them is a ``SafetyStop`` — a bad attachment must never stop the service.
Nothing here logs file names, URLs or keys.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from pathlib import Path
import secrets
from typing import Any, Mapping
from urllib.parse import quote

from telegram_bot.core import media as core_media
from telegram_bot.core.matrix.attachments import encrypted_file, parse_mxc

DOWNLOAD_TIMEOUT_S = 120
STALE_MEDIA_SECONDS = 3600
MEDIA_DIRNAME = "matrix-media"
_CHUNK = 65_536

# Images need a real image suffix: agent runtimes pick their image reader by
# extension, and the document MIME table has no image entries.
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


class AttachmentError(RuntimeError):
    """A body-free reason an attachment could not be staged."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def size_limit(attachment: Mapping[str, Any], settings: Any) -> int:
    """Byte limit for this attachment, reusing the Telegram settings.

    Images use ``CCC_TELEGRAM_MAX_IMAGE_BYTES`` when the image context guard is
    on; everything else (and unguarded images) uses ``CCC_MAX_DOCUMENT_SIZE_MB``
    (decimal megabytes, as on Telegram).
    """
    document = int(getattr(settings, "max_document_size_mb", 10)) * 1_000_000
    if attachment.get("kind") == "image" and getattr(settings, "image_context_guard", False):
        return int(getattr(settings, "telegram_max_image_bytes", 5 * 1024 * 1024))
    return document


def check_declared(attachment: Mapping[str, Any], settings: Any) -> int:
    """Reject by the sender-declared size/pixels before downloading anything."""
    limit = size_limit(attachment, settings)
    size = attachment.get("size")
    if type(size) is int and size > limit:
        raise AttachmentError("oversize")
    if attachment.get("kind") == "image" and getattr(settings, "image_context_guard", False):
        width, height = attachment.get("w"), attachment.get("h")
        max_pixels = int(getattr(settings, "telegram_max_image_pixels", 4_000_000))
        if type(width) is int and type(height) is int and width * height > max_pixels:
            raise AttachmentError("oversize")
    return limit


async def download_ciphertext(http: Any, homeserver: str, mxc: str, *, max_bytes: int) -> bytes:
    """GET the authenticated media endpoint (Tuwunel refuses the legacy one: 403)."""
    parsed = parse_mxc(mxc)
    if parsed is None:
        raise AttachmentError("invalid")
    server, media_id = parsed
    url = (
        homeserver.rstrip("/")
        + "/_matrix/client/v1/media/download/"
        + quote(server, safe="")
        + "/"
        + quote(media_id, safe="")
    )
    kwargs: dict[str, Any] = {"allow_redirects": False}  # never replay the bearer token elsewhere
    try:
        import aiohttp

        kwargs["timeout"] = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_S)
    except (ImportError, AttributeError):
        pass
    try:
        async with http.request("GET", url, **kwargs) as response:
            if response.status != 200:
                raise AttachmentError("download")
            declared = getattr(response, "content_length", None)
            if type(declared) is int and declared > max_bytes:
                raise AttachmentError("oversize")
            body = bytearray()
            async for chunk in response.content.iter_chunked(_CHUNK):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise AttachmentError("oversize")
            return bytes(body)
    except AttachmentError:
        raise
    except Exception:  # noqa: BLE001 - transport errors are a download failure, not a stop
        raise AttachmentError("download") from None  # CancelledError is not an Exception


def _unpadded_b64(value: str, *, urlsafe: bool) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    decode = base64.urlsafe_b64decode if urlsafe else base64.b64decode
    return decode(padded.encode("ascii"))


def decrypt(ciphertext: bytes, file: Mapping[str, Any]) -> bytes:
    """Verify the ciphertext SHA-256, then AES-256-CTR decrypt (spec ``EncryptedFile`` v2).

    Uses ``cryptography`` (in the runtime lock and CI) rather than nio, so the
    path is testable without the Matrix extras; equivalent to nio's
    ``decrypt_attachment`` (cross-checked in the tests when nio is present).
    """
    valid = encrypted_file(file)
    if valid is None:
        raise AttachmentError("invalid")
    try:
        key = _unpadded_b64(valid["key"]["k"], urlsafe=True)
        iv = _unpadded_b64(valid["iv"], urlsafe=False)
        expected = _unpadded_b64(valid["hashes"]["sha256"], urlsafe=False)
    except (ValueError, binascii.Error):
        raise AttachmentError("invalid") from None
    if len(key) != 32 or len(iv) != 16 or len(expected) != 32:
        raise AttachmentError("invalid")
    if not hmac.compare_digest(hashlib.sha256(ciphertext).digest(), expected):
        raise AttachmentError("integrity")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def staged_name(attachment: Mapping[str, Any]) -> str:
    """A bridge-generated ``document_<hex>.<ext>`` name (the Telegram cleanup pattern)."""
    mimetype = str(attachment.get("mimetype") or "").strip().lower()
    if attachment.get("kind") == "image" and mimetype in IMAGE_EXTENSIONS:
        return f"document_{secrets.token_hex(16)}{IMAGE_EXTENSIONS[mimetype]}"
    return core_media.build_document_file_name(attachment.get("name") or None, mimetype or None)


def store(directory: Path, data: bytes, attachment: Mapping[str, Any]) -> Path:
    """Write ``data`` to a fresh private 0600 file under a private 0700 directory."""
    directory_fd = core_media.open_private_document_directory(directory)
    try:
        name = staged_name(attachment)
        descriptor = core_media.open_private_document_file(directory_fd, name)
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            descriptor = -1
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return directory / name
    finally:
        os.close(directory_fd)


async def stage(transport: Any, attachment: Mapping[str, Any], directory: Path, settings: Any) -> Path:
    """Download → verify/decrypt → bound → store one attachment; returns the local path."""
    file = attachment.get("file")
    if not isinstance(file, Mapping):
        raise AttachmentError("invalid")
    if transport is None:
        raise AttachmentError("download")
    limit = check_declared(attachment, settings)
    try:
        core_media.cleanup_stale_document_files(directory, max_age_seconds=STALE_MEDIA_SECONDS)
    except OSError:
        raise AttachmentError("storage") from None
    # AES-CTR keeps the length, so the ciphertext cap is the plaintext cap.
    ciphertext = await transport.download_media(str(file.get("url")), max_bytes=limit)
    plaintext = decrypt(ciphertext, file)
    if len(plaintext) > limit:
        raise AttachmentError("oversize")
    try:
        return store(directory, plaintext, attachment)
    except AttachmentError:
        raise
    except Exception:  # noqa: BLE001 - storage refusal (unsafe dir, disk) is not a stop
        raise AttachmentError("storage") from None


def remove(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
