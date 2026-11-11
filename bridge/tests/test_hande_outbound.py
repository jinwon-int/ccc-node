"""「한데」 outbound attachments (#2764): spool writing and the ``HandeBot`` selection."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core.hande import outbound
from telegram_bot.core.hande.bot import HandeBot
from telegram_bot.core.hande.state import HandeConfigError, validate_config
from telegram_bot.core.matrix.bot import FILES_DIRECT_ONLY

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700)
    return directory


def _prepare(path: Path, spool: Path, root: Path | None = None, **kwargs: Any) -> Any:
    return outbound.prepare(
        path,
        spool_dir=str(spool),
        spool_name=outbound.new_spool_name(),
        root=root if root is not None else path.parent,
        **kwargs,
    )


# -- outbound.prepare / spool -------------------------------------------------------


def test_prepare_writes_a_private_spool_copy_and_describes_it(tmp_path: Path, spool: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(PNG)
    item = _prepare(source, spool)
    target = spool / item.spool_name
    assert outbound.OUT_NAME_PATTERN.fullmatch(item.spool_name)
    assert target.read_bytes() == PNG
    assert target.stat().st_mode & 0o777 == 0o600
    assert not target.is_symlink()
    assert item.attachment == {
        "v": 1,
        "path": str(target),
        "kind": "image",
        "mime": "image/png",
        "name": "chart.png",
        "size": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    assert len(json.dumps(item.attachment).encode()) <= outbound.MAX_ATTACHMENT_JSON_BYTES


def test_spool_file_follows_the_bots_group_when_another_uid_owns_the_spool(
    tmp_path: Path, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("hello")
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 4242)
    item = _prepare(source, spool)
    info = (spool / item.spool_name).stat()
    assert info.st_mode & 0o777 == 0o640
    assert info.st_gid == spool.stat().st_gid


def test_bidi_name_is_left_out_and_the_line_says_file(tmp_path: Path, spool: Path) -> None:
    source = tmp_path / "invoice‮gpj.exe.txt"
    source.write_text("x")
    item = _prepare(source, spool)
    assert "name" not in item.attachment
    assert item.display == outbound.NAMELESS
    assert outbound.not_sent_line(None, "no_ack") == "📎 첨부를 보내지 못했습니다: 파일(no_ack)"


def test_not_sent_line_never_carries_free_text_reasons() -> None:
    assert outbound.not_sent_line("a.pdf", "upload") == "📎 첨부를 보내지 못했습니다: a.pdf(upload)"
    assert outbound.not_sent_line("a.pdf", "/etc/passwd") == "📎 첨부를 보내지 못했습니다: a.pdf(error)"


@pytest.mark.parametrize(
    ("name", "data", "mime", "kind"),
    [
        ("photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 16, "image/jpeg", "image"),
        ("anim.gif", b"GIF89a" + b"\x00" * 16, "image/gif", "image"),
        ("pic.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp", "image"),
        ("fake.png", b"not an image at all", "application/octet-stream", "file"),
        ("doc.pdf", b"%PDF-1.4\n", "application/pdf", "file"),
        ("table.csv", b"a,b\n1,2\n", "text/csv", "file"),
        ("drawing.svg", b"<svg/>", "image/svg+xml", "file"),
    ],
)
def test_mime_and_kind_are_sniffed(
    tmp_path: Path, spool: Path, name: str, data: bytes, mime: str, kind: str
) -> None:
    source = tmp_path / name
    source.write_bytes(data)
    item = _prepare(source, spool)
    assert (item.attachment["mime"], item.attachment["kind"]) == (mime, kind)


def test_an_other_writable_spool_is_refused(tmp_path: Path, spool: Path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("a")
    os.chmod(spool, 0o777)
    try:
        with pytest.raises(outbound.OutboundAttachmentError) as caught:
            _prepare(source, spool)
        assert caught.value.reason == "spool"
        assert list(spool.iterdir()) == []
    finally:
        os.chmod(spool, 0o700)


def test_file_outside_root_oversize_and_empty_are_refused(tmp_path: Path, spool: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    with pytest.raises(outbound.OutboundAttachmentError) as caught:
        _prepare(outside, spool, root=root)
    assert caught.value.reason == "file"
    big = root / "big.txt"
    big.write_bytes(b"x" * 10)
    with pytest.raises(outbound.OutboundAttachmentError) as caught:
        _prepare(big, spool, root=root, max_bytes=5)
    assert caught.value.reason == "oversize"
    empty = root / "empty.txt"
    empty.write_bytes(b"")
    with pytest.raises(outbound.OutboundAttachmentError) as caught:
        _prepare(empty, spool, root=root)
    assert caught.value.reason == "file"
    assert list(spool.iterdir()) == []


def test_spool_write_never_follows_or_reuses_an_existing_name(tmp_path: Path, spool: Path) -> None:
    name = outbound.new_spool_name()
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (spool / name).symlink_to(victim)
    with pytest.raises(outbound.OutboundAttachmentError):
        outbound.write_spool_file(str(spool), name, b"data")
    assert victim.read_text() == "keep"
    with pytest.raises(outbound.OutboundAttachmentError):
        outbound.write_spool_file(str(spool), "../escape.bin", b"data")


def test_remove_spool_files_only_touches_out_names(spool: Path) -> None:
    ours = outbound.new_spool_name()
    (spool / ours).write_bytes(b"x")
    inbound = spool / ("ab" * 16 + ".bin")
    inbound.write_bytes(b"x")
    removed = outbound.remove_spool_files(
        str(spool), {ours, outbound.new_spool_name(), inbound.name}
    )
    assert removed == 1
    assert not (spool / ours).exists() and inbound.exists()


# -- config --------------------------------------------------------------------------


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "account": "@bridgenode:hande",
        "owner": "@owner:hande",
        "rooms": ["agent", "fleet"],
        "family_rooms": ["fleet"],
        "family_users": [],
        "socket_path": "/run/ccc-hande/responder.sock",
    }
    config.update(overrides)
    return config


def test_attachment_out_frames_defaults_off_and_needs_the_spool() -> None:
    assert validate_config(_config())["attachment_out_frames"] is False
    with pytest.raises(HandeConfigError, match="attachment_spool_dir"):
        validate_config(_config(attachment_out_frames=True))
    with pytest.raises(HandeConfigError):
        validate_config(_config(attachment_out_frames="yes"))
    config = validate_config(
        _config(attachment_out_frames=True, attachment_spool_dir="/var/lib/nrb/attachments/")
    )
    assert config["attachment_out_frames"] is True
    assert config["attachment_spool_dir"] == "/var/lib/nrb/attachments"


# -- HandeBot selection (the Telegram/Matrix rule) -------------------------------------


class _Transport:
    def __init__(self, on: bool) -> None:
        self.attachment_out_frames = on
        self.staged: list[tuple[list[Path], Path | None, list[str]]] = []

    def room_kind(self, room_id: str) -> str:
        return "family" if room_id == "fleet" else "direct"

    def stage_attachments(
        self, paths: list[Path], *, root: Path | None, notes: list[str] | None = None
    ) -> bool:
        self.staged.append((list(paths), root, list(notes or ())))
        return True


def _bot(tmp_path: Path, transport: _Transport) -> HandeBot:
    config = tmp_path / "hande.json"
    config.write_text(json.dumps(_config()), encoding="utf-8")
    os.chmod(config, 0o600)
    settings = SimpleNamespace(
        agent_provider="codex",
        project_root=tmp_path,
        execution_profile="owner-operator",
        bash_policy="approve-each",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_settings_path=tmp_path / "claude" / "settings.json",
        bot_data_dir=tmp_path / "data",
        hande_config_path=config,
        hande_socket_path=None,
        telegram_session_scope="per-user-chat",
        bridge_memory_mode="off",
        auto_new_session_after_hours=None,
        matrix_startup_banner=False,
        channel="hande",
    )
    bot = HandeBot(settings, project_chat=object(), session_manager=object())
    bot._transport = transport
    return bot


def _files(directory: Path, names: list[str]) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        path = directory / name
        path.write_text(name)
        paths.append(path.resolve())
    return paths


def test_selection_is_off_with_the_knob_off(tmp_path: Path) -> None:
    transport = _Transport(on=False)
    bot = _bot(tmp_path, transport)
    (report,) = _files(tmp_path / "out", ["report.pdf"])
    bot._enqueue_deliverables("agent", f"Saved {report}")
    assert transport.staged == []


def test_selection_follows_the_auto_send_rule_and_caps_at_four(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    transport = _Transport(on=True)
    bot = _bot(tmp_path, transport)
    inside = _files(tmp_path / "out", [f"r{index}.pdf" for index in range(5)])
    (elsewhere,) = _files(tmp_path_factory.mktemp("elsewhere"), ["leak.csv"])
    _files(tmp_path / "src", ["app.py"])  # source code: never a deliverable
    answer = "\n".join(
        [*(str(path) for path in inside), str(elsewhere), "src/app.py", "out/missing.pdf"]
    )
    bot._enqueue_deliverables("agent", answer)
    assert len(transport.staged) == 1
    paths, root, notes = transport.staged[0]
    assert paths == inside[:4]
    assert root == tmp_path.resolve()
    assert notes == [outbound.FILES_SKIPPED.format(count=2)]  # one outside + one over four


def test_selection_in_a_family_room_sends_no_file(tmp_path: Path) -> None:
    transport = _Transport(on=True)
    bot = _bot(tmp_path, transport)
    (report,) = _files(tmp_path / "out", ["report.pdf"])
    bot._enqueue_deliverables("fleet", f"Saved {report}")
    assert transport.staged == [([], tmp_path.resolve(), [FILES_DIRECT_ONLY.format(count=1)])]


def test_selection_without_files_stages_nothing(tmp_path: Path) -> None:
    transport = _Transport(on=True)
    bot = _bot(tmp_path, transport)
    bot._enqueue_deliverables("agent", "No files here, only out/missing.pdf")
    assert transport.staged == []
