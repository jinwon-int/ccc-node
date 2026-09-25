"""Matrix photo/file input (#1795): admission, store column, download/decrypt/stage.

Encrypted fixtures are built with ``cryptography`` exactly as the Matrix spec
(``EncryptedFile`` v2) describes, so nothing here needs matrix-nio; when nio is
installed the decrypt path is also cross-checked against its implementation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core.matrix import media as mm
from telegram_bot.core.matrix.attachments import (
    ATTACHMENT_PLACEHOLDER,
    decode_attachment,
    encode_attachment,
    media_attachment,
    media_caption,
    parse_mxc,
)
from telegram_bot.core.matrix.state import MatrixStore, Policy, Request, job_digest, scope_of

BOT = "@bot:example.org"
OWNER = "@owner:example.org"
KID = "@kid:example.org"
DM = "!dm:example.org"
FAMILY = "!family:example.org"
MXC = "mxc://example.org/AbCdEf123"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _b64(raw: bytes, *, urlsafe: bool = False) -> str:
    encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return encode(raw).decode().rstrip("=")


def encrypt(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Spec v2 encryption: AES-256-CTR, IV with a zero low 64-bit counter."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key, iv = os.urandom(32), os.urandom(8) + b"\x00" * 8
    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(data) + encryptor.finalize()
    file = {
        "url": MXC,
        "key": {"kty": "oct", "alg": "A256CTR", "ext": True, "k": _b64(key, urlsafe=True), "key_ops": ["encrypt", "decrypt"]},
        "iv": _b64(iv),
        "hashes": {"sha256": _b64(hashlib.sha256(ciphertext).digest())},
        "v": "v2",
    }
    return ciphertext, file


def image_content(file: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "msgtype": "m.image",
        "body": "IMG_0001.jpg",
        "info": {"mimetype": "image/jpeg", "size": 1234, "w": 640, "h": 480},
        "file": file if file is not None else encrypt(b"jpeg")[1],
    }
    content.update(extra)
    return content


def policy() -> Policy:
    return Policy(BOT, frozenset({OWNER, KID}), frozenset({BOT}), {DM: "direct", FAMILY: "mention"}, 0)


def admit(content: dict[str, Any], *, room: str = DM, sender: str = OWNER, event_id: str = "$photo") -> Request | None:
    now = int(time.time() * 1000)
    event = {"type": "m.room.message", "event_id": event_id, "sender": sender, "origin_server_ts": now, "content": content}
    return policy().admit(room, event, decrypted=True, now_ms=now)


# --- admission ---------------------------------------------------------------


def test_direct_room_admits_caption_less_encrypted_photo() -> None:
    req = admit(image_content())
    assert req is not None and req.attachment is not None
    assert req.body == ATTACHMENT_PLACEHOLDER and req.reply_to is None
    attachment = decode_attachment(req.attachment)
    assert attachment is not None
    assert attachment["kind"] == "image" and attachment["captioned"] is False and "caption" not in attachment
    assert attachment["mimetype"] == "image/jpeg" and (attachment["size"], attachment["w"], attachment["h"]) == (1234, 640, 480)
    assert attachment["file"]["url"] == MXC and attachment["file"]["v"] == "v2"


def test_caption_rule_follows_matrix_v1_10_filename_semantics() -> None:
    assert media_caption({"body": "IMG.jpg"}) == ""  # body is only the file name
    assert media_caption({"body": "IMG.jpg", "filename": "IMG.jpg"}) == ""
    assert media_caption({"body": " 이거 뭐야? ", "filename": "IMG.jpg"}) == "이거 뭐야?"
    req = admit(image_content(body="영수증 합계 알려줘", filename="receipt.jpg"))
    assert req is not None and req.body == "영수증 합계 알려줘"
    stored = decode_attachment(req.attachment)
    assert stored is not None and stored["name"] == "receipt.jpg" and stored["captioned"] is True


def test_long_caption_is_the_job_body_not_part_of_the_capped_attachment_json() -> None:
    caption = "가" * 5000  # 15 000 bytes: under MAX_TEXT_BYTES, over the 8 KiB attachment cap
    req = admit(image_content(body=caption, filename="a.jpg"))
    assert req is not None and req.body == caption
    assert len(req.attachment.encode()) < 1024  # type: ignore[union-attr]
    assert admit(image_content(body="가" * 6000, filename="a.jpg")) is None  # over MAX_TEXT_BYTES, like text


def test_family_room_needs_an_explicit_address_even_for_media() -> None:
    assert admit(image_content(), room=FAMILY, sender=KID) is None  # caption-less photo: ignored
    assert admit(image_content(body="그냥 사진", filename="a.jpg"), room=FAMILY, sender=KID) is None
    captioned = admit(image_content(body="@bot 이게 뭐야", filename="a.jpg"), room=FAMILY, sender=KID)
    assert captioned is not None and captioned.body == "@bot 이게 뭐야"
    pill = admit(image_content(**{"m.mentions": {"user_ids": [BOT]}}), room=FAMILY, sender=KID)
    assert pill is not None and pill.body == ATTACHMENT_PLACEHOLDER


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.pop("file"),  # plaintext room media carries only `url`
        lambda c: c.update(url=MXC, file=None),
        lambda c: c["file"].update(v="v1"),
        lambda c: c["file"].update(url="https://evil.example/x"),
        lambda c: c["file"]["key"].update(alg="A128CBC"),
        lambda c: c["file"]["hashes"].pop("sha256"),
        lambda c: c["file"].update(iv="not base64!"),
        lambda c: c["file"].update(iv="AAAA-AAAAAAAAAAAAAAAAA"),  # iv is standard base64, not url-safe
        lambda c: c["file"]["key"].update(k="A" * 42 + "+"),  # k is base64url, not standard
        lambda c: c.update({"m.relates_to": {"rel_type": "m.replace", "event_id": "$x"}}),
        lambda c: c.update(msgtype="m.location"),
    ],
)
def test_plaintext_malformed_or_edited_media_is_never_admitted(mutate: Any) -> None:
    content = image_content()
    mutate(content)
    assert admit(content) is None


def test_text_admission_and_digest_are_unchanged() -> None:
    req = admit({"msgtype": "m.text", "body": "hello"})
    assert req is not None and req.attachment is None and req.body == "hello"
    legacy = hashlib.sha256(json.dumps([DM, OWNER, "hello"]).encode()).hexdigest()
    assert job_digest(DM, OWNER, "hello", None) == legacy


def test_attachment_codec_is_bounded_and_rejects_corrupt_rows() -> None:
    attachment = media_attachment(image_content())
    assert attachment is not None
    assert decode_attachment(encode_attachment(attachment)) == attachment
    assert decode_attachment(None) is None and decode_attachment("") is None
    assert decode_attachment("{broken") is None and decode_attachment('{"file": {}}') is None
    with pytest.raises(ValueError, match="attachment too large"):
        encode_attachment({**attachment, "name": "x" * 9000})
    assert parse_mxc(MXC) == ("example.org", "AbCdEf123")
    assert parse_mxc("mxc://example.org/../etc") is None and parse_mxc(None) is None


# --- store -------------------------------------------------------------------


def test_store_migrates_attachment_column_and_drops_the_key_after_the_turn(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with MatrixStore(directory, BOT) as store:
        store.db.execute("ALTER TABLE jobs DROP COLUMN attachment")  # a pre-#1795 inbox
    with MatrixStore(directory, BOT) as store:
        columns = {row[1] for row in store.db.execute("PRAGMA table_info(jobs)")}
        assert "attachment" in columns
        photo, text = admit(image_content()), admit({"msgtype": "m.text", "body": "hi"}, event_id="$text")
        assert photo is not None and text is not None
        store.accept_batch([photo, text], None)
        store.accept_batch([photo], None)  # replayed sync: identical identity, no conflict
        job = store.claim()
        assert job is not None and job["event_id"] == "$photo" and decode_attachment(job["attachment"]) is not None
        store.finish("$photo", "설명")
        row = store.db.execute("SELECT attachment,state FROM jobs WHERE event_id='$photo'").fetchone()
        assert row["attachment"] is None and row["state"] == "ready"
        # Same event id with a different attachment is an identity conflict.
        other = Request("$photo", DM, OWNER, ATTACHMENT_PLACEHOLDER, scope_of(BOT, DM, OWNER), None,
                        encode_attachment(media_attachment(image_content())))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="event identity conflict"):
            store.accept_batch([other], None)
        bogus = Request("$bogus", DM, OWNER, ATTACHMENT_PLACEHOLDER, scope_of(BOT, DM, OWNER), None, '{"kind":"image"}')
        with pytest.raises(ValueError, match="invalid attachment"):
            store.accept_batch([bogus], None)
    # A turn left `running` by a crash forgets its key when the store reopens.
    with MatrixStore(tmp_path / "state3", BOT) as store:
        store.accept_batch([admit(image_content(), event_id="$killed")], None)  # type: ignore[list-item]
        assert store.claim() is not None
    with MatrixStore(tmp_path / "state3", BOT) as store:
        row = store.db.execute("SELECT attachment,state FROM jobs WHERE event_id='$killed'").fetchone()
        assert row["attachment"] is None and row["state"] == "uncertain"
    # An interrupted attachment turn also forgets its key.
    with MatrixStore(tmp_path / "state2", BOT) as store:
        store.accept_batch([admit(image_content(), event_id="$crash")], None)  # type: ignore[list-item]
        assert store.claim() is not None
        store.uncertain_job("$crash")
        assert store.db.execute("SELECT attachment FROM jobs WHERE event_id='$crash'").fetchone()[0] is None


def test_attachment_key_survives_only_while_queued(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with MatrixStore(directory, BOT) as store:
        store.accept_batch([admit(image_content(), event_id="$queued")], None)  # type: ignore[list-item]
    raw = sqlite3.connect(directory / "inbox.sqlite3")
    try:
        stored = raw.execute("SELECT attachment FROM jobs").fetchone()[0]
    finally:
        raw.close()
    assert decode_attachment(stored) is not None
    assert stat.S_IMODE(os.stat(directory / "inbox.sqlite3").st_mode) == 0o600


# --- decrypt / stage ---------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 16, 17, 70_000])
def test_decrypt_round_trips_and_detects_tampering(size: int) -> None:
    data = os.urandom(size)
    ciphertext, file = encrypt(data)
    assert mm.decrypt(ciphertext, file) == data
    with pytest.raises(mm.AttachmentError) as tampered:
        mm.decrypt(ciphertext + b"x", file)
    assert tampered.value.reason == "integrity"
    wrong = dict(file, key={**file["key"], "k": _b64(os.urandom(31), urlsafe=True)})
    with pytest.raises(mm.AttachmentError) as bad_key:
        mm.decrypt(ciphertext, wrong)
    assert bad_key.value.reason == "invalid"


def test_decrypt_matches_nio_when_available() -> None:
    attachments = pytest.importorskip("nio.crypto.attachments")
    data = os.urandom(5000)
    ciphertext, keys = attachments.encrypt_attachment(data)
    file = {"url": MXC, "key": keys["key"], "iv": keys["iv"], "hashes": keys["hashes"], "v": "v2"}
    assert mm.decrypt(ciphertext, file) == data


class FakeResponse:
    def __init__(self, status: int, body: bytes, content_length: int | None = None, chunk: int = 7) -> None:
        self.status = status
        self.content_length = content_length
        self._body = body
        self._chunk = chunk
        self.content = self

    async def iter_chunked(self, size: int) -> Any:
        for start in range(0, len(self._body), self._chunk):
            yield self._body[start : start + self._chunk]

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeHttp:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.anyio
async def test_download_uses_the_authenticated_endpoint_and_bounds_the_body() -> None:
    http = FakeHttp(FakeResponse(200, b"ciphertext-bytes"))
    assert await mm.download_ciphertext(http, "https://hs.example.org/", MXC, max_bytes=100) == b"ciphertext-bytes"
    method, url, kwargs = http.calls[0]
    assert (method, url) == ("GET", "https://hs.example.org/_matrix/client/v1/media/download/example.org/AbCdEf123")
    assert kwargs["allow_redirects"] is False
    for response, reason in (
        (FakeResponse(403, b""), "download"),
        (FakeResponse(200, b"", content_length=101), "oversize"),
        (FakeResponse(200, b"x" * 101), "oversize"),
        (ConnectionResetError("boom"), "download"),
    ):
        with pytest.raises(mm.AttachmentError) as err:
            await mm.download_ciphertext(FakeHttp(response), "https://hs", MXC, max_bytes=100)
        assert err.value.reason == reason
    with pytest.raises(mm.AttachmentError) as bad:
        await mm.download_ciphertext(http, "https://hs", "mxc://bad", max_bytes=100)
    assert bad.value.reason == "invalid"


class FakeTransport:
    def __init__(self, ciphertext: bytes) -> None:
        self.ciphertext = ciphertext
        self.calls: list[tuple[str, int]] = []

    async def download_media(self, mxc: str, *, max_bytes: int) -> bytes:
        self.calls.append((mxc, max_bytes))
        return self.ciphertext


def settings(**overrides: Any) -> SimpleNamespace:
    values = dict(max_document_size_mb=10, image_context_guard=False, telegram_max_image_bytes=5 * 1024 * 1024,
                  telegram_max_image_pixels=4_000_000)
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.anyio
async def test_stage_writes_a_private_file_with_an_image_suffix(tmp_path: Path) -> None:
    data = b"\xff\xd8\xff" + os.urandom(4000)
    ciphertext, file = encrypt(data)
    attachment = media_attachment(image_content(file))
    assert attachment is not None
    directory = tmp_path / "matrix-media"
    transport = FakeTransport(ciphertext)
    path = await mm.stage(transport, attachment, directory, settings())
    assert path.parent == directory and path.suffix == ".jpg" and path.read_bytes() == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert transport.calls == [(MXC, 10_000_000)]
    mm.remove(path)
    mm.remove(path)  # idempotent
    mm.remove(None)
    assert list(directory.iterdir()) == []


@pytest.mark.anyio
async def test_stage_keeps_a_safe_document_suffix_and_sweeps_stale_files(tmp_path: Path) -> None:
    ciphertext, file = encrypt(b"%PDF-1.7")
    attachment = media_attachment({"msgtype": "m.file", "body": "보고서.pdf", "info": {"mimetype": "application/pdf"}, "file": file})
    directory = tmp_path / "matrix-media"
    directory.mkdir(mode=0o700)
    stale = directory / ("document_" + "0" * 32 + ".png")
    stale.write_bytes(b"old")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    path = await mm.stage(FakeTransport(ciphertext), attachment, directory, settings())  # type: ignore[arg-type]
    assert path.suffix == ".pdf" and not stale.exists()


@pytest.mark.anyio
async def test_stage_rejects_declared_oversize_before_downloading(tmp_path: Path) -> None:
    ciphertext, file = encrypt(b"x")
    transport = FakeTransport(ciphertext)
    big = media_attachment(image_content(file, info={"mimetype": "image/png", "size": 10_000_001}))
    with pytest.raises(mm.AttachmentError) as err:
        await mm.stage(transport, big, tmp_path / "m", settings())  # type: ignore[arg-type]
    assert err.value.reason == "oversize" and transport.calls == []
    # The image guard applies the Telegram image byte and pixel caps.
    guarded = settings(image_context_guard=True, telegram_max_image_bytes=100_000, telegram_max_image_pixels=1_000_000)
    wide = media_attachment(image_content(file, info={"mimetype": "image/png", "w": 2000, "h": 1000}))
    with pytest.raises(mm.AttachmentError):
        await mm.stage(transport, wide, tmp_path / "m", guarded)  # type: ignore[arg-type]
    assert mm.size_limit({"kind": "image"}, guarded) == 100_000
    assert mm.size_limit({"kind": "file"}, guarded) == 10_000_000
    assert transport.calls == []


@pytest.mark.anyio
async def test_stage_reports_integrity_and_storage_failures_without_leaving_files(tmp_path: Path) -> None:
    ciphertext, file = encrypt(b"payload")
    attachment = media_attachment(image_content(file))
    with pytest.raises(mm.AttachmentError) as integrity:
        await mm.stage(FakeTransport(ciphertext[:-1] + bytes([ciphertext[-1] ^ 0x01])), attachment, tmp_path / "m", settings())  # type: ignore[arg-type]
    assert integrity.value.reason == "integrity"
    assert not any((tmp_path / "m").glob("document_*"))
    target = tmp_path / "link"
    target.symlink_to(tmp_path)
    with pytest.raises(mm.AttachmentError) as storage:
        await mm.stage(FakeTransport(ciphertext), attachment, target, settings())  # type: ignore[arg-type]
    assert storage.value.reason == "storage"
    not_a_dir = tmp_path / "file-not-dir"
    not_a_dir.write_bytes(b"x")
    with pytest.raises(mm.AttachmentError) as blocked:
        await mm.stage(FakeTransport(ciphertext), attachment, not_a_dir, settings())  # type: ignore[arg-type]
    assert blocked.value.reason == "storage"
    with pytest.raises(mm.AttachmentError) as missing:
        await mm.stage(None, attachment, tmp_path / "m", settings())  # type: ignore[arg-type]
    assert missing.value.reason == "download"
