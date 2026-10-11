"""Matrix media output (#2001): encrypt, upload and describe one agent deliverable.

The mirror of ``core/matrix/media.py``. The Telegram bridge sends the files an
answer names (``core/deliverables.py``) as photos/documents. A Matrix room is
end-to-end encrypted, so the file is AES-256-CTR encrypted locally (spec
``EncryptedFile`` v2), the *ciphertext* is uploaded through the media API, and
the room event carries the key, IV and ciphertext SHA-256 — the homeserver
never sees the plaintext.

Failures are an :class:`OutboundMediaError` with a short reason code, or a
``ConnectionError`` for a retryable transport problem; none of them is a
``SafetyStop``. Nothing here logs file names, URLs or keys.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import stat
from pathlib import Path
from typing import Any, Mapping

UPLOAD_TIMEOUT_S = 300
_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})
IMAGE_MIMETYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


class OutboundMediaError(RuntimeError):
    """A body-free, non-retryable reason a file could not be sent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _b64(raw: bytes, *, urlsafe: bool = False) -> str:
    encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return encode(raw).decode("ascii").rstrip("=")


def encrypt(plaintext: bytes) -> tuple[bytes, dict[str, Any]]:
    """AES-256-CTR encrypt ``plaintext``; returns ``(ciphertext, EncryptedFile-without-url)``.

    The IV is 8 random bytes plus a zero 64-bit counter, as the spec and every
    client do (a full random IV can overflow the counter on some decoders).
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = os.urandom(32)
    iv = os.urandom(8) + b"\x00" * 8
    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return ciphertext, {
        "v": "v2",
        "key": {"kty": "oct", "alg": "A256CTR", "ext": True, "k": _b64(key, urlsafe=True), "key_ops": ["encrypt", "decrypt"]},
        "iv": _b64(iv),
        "hashes": {"sha256": _b64(hashlib.sha256(ciphertext).digest())},
    }


def mimetype_of(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def file_content(name: str, mimetype: str, size: int, mxc: str, file_info: Mapping[str, Any], *, voice: bool = False) -> dict[str, Any]:
    """The ``m.room.message`` content for an encrypted image or file."""
    msgtype = "m.image" if mimetype in IMAGE_MIMETYPES else "m.file"
    content = {
        "msgtype": msgtype,
        "body": name,
        "filename": name,
        "info": {"mimetype": mimetype, "size": size},
        "file": {**dict(file_info), "url": mxc, "mimetype": mimetype},
    }
    if voice:
        content["msgtype"] = "m.audio"
        content["org.matrix.msc3245.voice"] = {}
    return content


def _timeout_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"allow_redirects": False}  # never replay the bearer token elsewhere
    try:
        import aiohttp

        kwargs["timeout"] = aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT_S)
    except (ImportError, AttributeError):
        pass
    return kwargs


async def upload_limit(http: Any, homeserver: str) -> int | None:
    """The homeserver's ``m.upload.size`` (authenticated media config), ``None`` if unknown."""
    url = homeserver.rstrip("/") + "/_matrix/client/v1/media/config"
    try:
        async with http.request("GET", url, **_timeout_kwargs()) as response:
            if response.status != 200:
                return None
            data = await response.json()
    except Exception:  # noqa: BLE001 - an unknown limit falls back to the bridge cap
        return None
    size = data.get("m.upload.size") if isinstance(data, dict) else None
    return size if type(size) is int and size > 0 else None


async def upload(http: Any, homeserver: str, ciphertext: bytes) -> str:
    """POST the ciphertext; returns its ``mxc://`` URI.

    The file name is deliberately not sent: it lives inside the encrypted
    event, never in the plaintext upload request.
    """
    url = homeserver.rstrip("/") + "/_matrix/media/v3/upload"
    headers = {"Content-Type": "application/octet-stream"}
    try:
        async with http.request("POST", url, data=ciphertext, headers=headers, **_timeout_kwargs()) as response:
            status = int(response.status)
            if status == 413:
                raise OutboundMediaError("too-large")
            if status in _RETRYABLE:
                raise ConnectionError("matrix-upload-temporary-error")
            if status != 200:
                raise OutboundMediaError("upload-refused")
            try:
                data = await response.json(content_type=None)
            except Exception:  # noqa: BLE001 - a 200 that is not JSON will not become JSON on retry
                raise OutboundMediaError("upload-invalid-response") from None
    except (OutboundMediaError, ConnectionError):
        raise
    except Exception:  # noqa: BLE001 - network trouble is retryable, never a stop
        raise ConnectionError("matrix-upload-failed") from None
    uri = data.get("content_uri") if isinstance(data, dict) else None
    if not isinstance(uri, str) or not uri.startswith("mxc://") or len(uri) > 255:
        raise OutboundMediaError("upload-invalid-response")
    return uri


def read_deliverable(path: Path, *, max_bytes: int, root: Path | None = None) -> bytes:
    """Read a regular file once, bounded, re-checking what was queued.

    The path was resolved and scope-checked when the file was queued; the row
    may wait (a muted room keeps it), so the check is repeated here: the path
    must still resolve to itself (no symlink swapped in), stay inside
    ``root``, and the final component is opened with ``O_NOFOLLOW``.
    """
    from telegram_bot.core.paths import is_within_project_root

    try:
        if path.resolve() != path:
            raise OutboundMediaError("path-changed")
        if root is not None and not is_within_project_root(path, root):
            raise OutboundMediaError("outside-root")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OutboundMediaError:
        raise
    except FileNotFoundError:
        raise OutboundMediaError("missing") from None
    except OSError:
        raise OutboundMediaError("unreadable") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OutboundMediaError("missing")
        if info.st_size > max_bytes:
            raise OutboundMediaError("too-large")
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OutboundMediaError:
        raise
    except OSError:
        raise OutboundMediaError("unreadable") from None
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise OutboundMediaError("too-large")
    return data


__all__ = [
    "IMAGE_MIMETYPES",
    "OutboundMediaError",
    "encrypt",
    "file_content",
    "mimetype_of",
    "read_deliverable",
    "upload",
    "upload_limit",
]
