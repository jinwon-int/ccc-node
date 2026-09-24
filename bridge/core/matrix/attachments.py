"""Pure Matrix media attachment helpers (#1795).

Shared by admission (``state``), staging (``media``) and the runner (``bot``);
dependency-free so frontends and tests can import it without the Matrix stack.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


MEDIA_MSGTYPES = {"m.image": "image", "m.file": "file", "m.video": "video", "m.audio": "audio"}
# Stored as the job body when a photo has no caption (bounded_text rejects an
# empty body). The caption itself is the job body; the attachment JSON only
# records ``captioned`` so a long caption never hits the JSON size cap.
ATTACHMENT_PLACEHOLDER = "(attachment)"
MAX_ATTACHMENT_JSON_BYTES = 8_192
_MXC_PATTERN = re.compile(r"mxc://([A-Za-z0-9.:-]{1,255})/([A-Za-z0-9_-]{1,255})")
# Spec: the JWK ``k`` is unpadded base64url; ``iv`` and hashes are unpadded base64.
_B64URL_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_B64_PATTERN = re.compile(r"[A-Za-z0-9+/]{1,128}")


def parse_mxc(url: Any) -> tuple[str, str] | None:
    """``(server, media_id)`` of a well-formed ``mxc://`` URI, else ``None``."""
    match = _MXC_PATTERN.fullmatch(url) if isinstance(url, str) else None
    return (match.group(1), match.group(2)) if match else None


def encrypted_file(value: Any) -> dict[str, Any] | None:
    """Validate an ``EncryptedFile`` (spec §end-to-end attachments); ``None`` if malformed."""
    if not isinstance(value, dict) or parse_mxc(value.get("url")) is None or value.get("v") != "v2":
        return None
    key, hashes, iv = value.get("key"), value.get("hashes"), value.get("iv")
    if not isinstance(key, dict) or not isinstance(hashes, dict):
        return None
    k, digest = key.get("k"), hashes.get("sha256")
    if key.get("kty") != "oct" or key.get("alg") != "A256CTR":
        return None
    if not (isinstance(k, str) and _B64URL_PATTERN.fullmatch(k)):
        return None
    if not all(isinstance(x, str) and _B64_PATTERN.fullmatch(x) for x in (digest, iv)):
        return None
    return {
        "url": value["url"],
        "key": {"kty": "oct", "alg": "A256CTR", "ext": True, "k": k, "key_ops": ["encrypt", "decrypt"]},
        "iv": iv,
        "hashes": {"sha256": digest},
        "v": "v2",
    }


def media_caption(content: Mapping[str, Any]) -> str:
    """The user-written caption of a media event, or ``""``.

    Matrix v1.10: when ``filename`` is present and differs from ``body``,
    ``body`` is a caption; otherwise ``body`` is only the file name.
    """
    body, filename = content.get("body"), content.get("filename")
    if isinstance(body, str) and isinstance(filename, str) and filename and body.strip() and body != filename:
        return body.strip()
    return ""


def media_attachment(content: Mapping[str, Any]) -> dict[str, Any] | None:
    """The admitted attachment description of an encrypted media event."""
    kind = MEDIA_MSGTYPES.get(content.get("msgtype"))  # type: ignore[arg-type]
    file = encrypted_file(content.get("file"))
    if kind is None or file is None:
        return None
    raw_info = content.get("info")
    info: Mapping[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    name = content.get("filename") if isinstance(content.get("filename"), str) else content.get("body")
    mimetype = info.get("mimetype")
    size, width, height = info.get("size"), info.get("w"), info.get("h")

    def count(value: Any) -> int | None:
        return value if type(value) is int and 0 <= value <= 2**53 else None

    return {
        "kind": kind,
        "name": name[:255] if isinstance(name, str) else "",
        "mimetype": mimetype[:127] if isinstance(mimetype, str) else "",
        "size": count(size),
        "w": count(width),
        "h": count(height),
        "captioned": bool(media_caption(content)),
        "file": file,
    }


def encode_attachment(attachment: Mapping[str, Any]) -> str:
    text = json.dumps(attachment, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_ATTACHMENT_JSON_BYTES:
        raise ValueError("attachment too large")
    return text


def decode_attachment(raw: Any) -> dict[str, Any] | None:
    """Parse a stored attachment column; ``None`` for text jobs or corrupt rows."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or encrypted_file(value.get("file")) is None:
        return None
    return value
