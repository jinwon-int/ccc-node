"""Hermetic encrypted voice input, bounded audio conversion and durable replies."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
import wave

import pytest

from telegram_bot.core import media as core_media
from telegram_bot.core.matrix import media, voice
from telegram_bot.core.matrix.attachments import media_attachment
from telegram_bot.core.matrix.state import FILE_JOB_BODY, MatrixStore, Request, scope_of
from telegram_bot.utils.audio_processor import AudioProcessor, communicate_audio_process
from test_matrix_bot import (  # noqa: F401 - shared fixture
    FakeSink, _MediaTransport, _bot, _encrypted, _media_job,
    _resume_existing_session, matrix_config as _matrix_config,
)
from test_matrix_file_send import _http
from test_matrix_transport import running

matrix_config = _matrix_config


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def audio_job(*, duration: Any = 1000, caption: str = "") -> tuple[dict[str, Any], bytes]:
    ciphertext, file = _encrypted(b"OggS synthetic speech")
    content = {
        "msgtype": "m.audio", "body": caption or "voice.ogg", "file": file,
        "info": {"mimetype": "audio/ogg", "duration": duration},
        "org.matrix.msc3245.voice": {},
    }
    if caption:
        content["filename"] = "voice.ogg"
    return _media_job(content), ciphertext


def wav_file(path: Path, seconds: int) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 16000 * seconds)


@pytest.mark.parametrize("duration,expected", [(1000, 1000), (True, None), (-1, None), ("1000", None)])
def test_admission_preserves_only_valid_millisecond_duration(duration: Any, expected: Any) -> None:
    _, file = _encrypted(b"x")
    attachment = media_attachment({"msgtype": "m.audio", "file": file, "info": {"duration": duration}})
    assert attachment is not None and attachment["duration"] == expected


@pytest.mark.anyio
@pytest.mark.parametrize("caption", ["", "Explain this", "/new"])
async def test_voice_transcript_preview_and_agent_turn(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caption: str,
) -> None:
    bot, chat, manager = _bot(tmp_path, openai_api_key="test-key", max_voice_duration=300)
    _resume_existing_session(bot, manager)
    job, ciphertext = audio_job(caption=caption)
    bot._transport = _MediaTransport(ciphertext)

    async def transcribe(path: Path, settings: Any) -> str:
        assert path.read_bytes() == b"OggS synthetic speech" and path.stat().st_mode & 0o777 == 0o600
        return "/new"

    monkeypatch.setattr(voice, "transcribe", transcribe)
    sink = FakeSink()
    result = await bot.run_turn(job, sink=sink, session_id=None, room_kind="direct")
    assert sink.interims == ["🎤 Voice: /new"]
    assert len(chat.calls) == 1 and chat.calls[0]["user_message"].endswith("/new")
    if caption:
        assert chat.calls[0]["user_message"].startswith(caption + "\n\n[Voice transcript]")
    assert result.text == "answer"
    assert not list((tmp_path / "data" / "matrix-media").iterdir())


@pytest.mark.anyio
@pytest.mark.parametrize("settings,reason", [
    ({}, "configuration"),
    ({"openai_api_key": "test-key", "transcription_provider": "volcengine"}, "provider"),
    ({"openai_api_key": "test-key", "max_voice_duration": 1}, "duration"),
])
async def test_preflight_answers_without_download_or_agent(
    tmp_path: Path, matrix_config: dict[str, Any], settings: dict[str, Any], reason: str,
) -> None:
    bot, chat, _ = _bot(tmp_path, **settings)
    job, ciphertext = audio_job(duration=2000)
    transport = _MediaTransport(ciphertext)
    bot._transport = transport
    result = await bot.run_turn(job, sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == voice.VOICE_FAILURES[reason]
    assert transport.downloads == 0 and chat.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["", RuntimeError("test provider failure")])
async def test_failed_transcription_cleans_input_without_agent(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch, outcome: Any,
) -> None:
    bot, chat, _ = _bot(tmp_path, openai_api_key="test-key")
    job, ciphertext = audio_job()
    bot._transport = _MediaTransport(ciphertext)
    transcribe = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    monkeypatch.setattr(voice, "transcribe", transcribe)
    result = await bot.run_turn(job, sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == voice.VOICE_FAILED and chat.calls == []
    assert not list((tmp_path / "data" / "matrix-media").iterdir())


@pytest.mark.anyio
async def test_stop_during_transcription_cleans_input_and_never_starts_agent(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, chat, _ = _bot(tmp_path, openai_api_key="test-key")
    job, ciphertext = audio_job()
    bot._transport = _MediaTransport(ciphertext)
    started = asyncio.Event()

    async def transcribe(path: Path, settings: Any) -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    monkeypatch.setattr(voice, "transcribe", transcribe)
    task = asyncio.create_task(bot.run_turn(job, sink=FakeSink(), session_id=None, room_kind="direct"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()  # transport /stop cancels this exact runner task
    with pytest.raises(asyncio.CancelledError):
        await task
    assert chat.calls == [] and bot._active_sink is None
    assert not list((tmp_path / "data" / "matrix-media").iterdir())


@pytest.mark.anyio
@pytest.mark.parametrize("seconds,limit,accepted", [(2, 2, True), (3, 2, False), (0, 2, False)])
async def test_actual_duration_checked_before_whisper_and_private_wav_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: int, limit: int, accepted: bool,
) -> None:
    path = media.store(tmp_path / "media", b"ogg", {"name": "voice.ogg", "mimetype": "audio/ogg"})
    seen: dict[str, Any] = {}

    async def convert(processor: AudioProcessor, source: Path, output: Path) -> Path:
        assert output.stat().st_mode & 0o777 == 0o600
        assert processor.input_args[:3] == ["-nostdin", "-protocol_whitelist", "file,pipe"]
        assert "-format_whitelist" in processor.input_args
        assert processor.ffmpeg_args[-2:] == ["-t", str(limit + 1)]
        seen["converted"] = output
        wav_file(output, seconds)
        return output

    client = SimpleNamespace(close=AsyncMock())
    transcriber = SimpleNamespace(client=client, transcribe_audio=AsyncMock(return_value=" words "))
    def factory(**kwargs: Any) -> Any:
        return transcriber
    monkeypatch.setattr(AudioProcessor, "convert_audio", convert)
    monkeypatch.setattr(voice, "WhisperTranscriber", factory)
    settings = SimpleNamespace(openai_api_key="test-key", max_voice_duration=limit)
    if accepted:
        assert await voice.transcribe(path, settings) == "words"
        transcriber.transcribe_audio.assert_awaited_once()
        client.close.assert_awaited_once()
    else:
        with pytest.raises((media.AttachmentError, ValueError)):
            await voice.transcribe(path, settings)
        transcriber.transcribe_audio.assert_not_awaited()
    assert not seen["converted"].exists() and path.exists()


@pytest.mark.anyio
async def test_cancel_reaps_audio_subprocess() -> None:
    started, killed = asyncio.Event(), asyncio.Event()

    class Process:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            started.set()
            await killed.wait()
            return b"", b""

        def kill(self) -> None:
            self.returncode = -9
            killed.set()

    process = Process()
    task = asyncio.create_task(communicate_audio_process(process))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed.is_set() and process.returncode == -9


@pytest.mark.anyio
@pytest.mark.parametrize("supported", [True, False])
async def test_voice_reply_uses_existing_platform_gate_and_text_survives_tts_failure(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch, supported: bool,
) -> None:
    bot, chat, manager = _bot(tmp_path, openai_api_key="test-key")
    _resume_existing_session(bot, manager)
    job, ciphertext = audio_job()
    transport = _MediaTransport(ciphertext)
    transport.enqueue_voice = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("queue failed"))
    bot._transport = transport
    monkeypatch.setattr(core_media, "is_macos", lambda: supported)
    monkeypatch.setattr(voice, "transcribe", AsyncMock(return_value="hello"))
    synthesis = AsyncMock(return_value=b"OggS reply")
    monkeypatch.setattr(voice, "synthesize", synthesis)
    result = await bot.run_turn(job, sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == "answer" and len(chat.calls) == 1
    assert synthesis.await_count == int(supported)


@pytest.mark.anyio
async def test_synthesis_cleans_partial_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def synthesize(self: Any, **kwargs: Any) -> Any:
        (kwargs["output_dir"] / "partial.aiff").write_bytes(b"partial")
        raise RuntimeError("say failed")

    monkeypatch.setattr(voice.MacOSTtsSynthesizer, "synthesize_to_telegram_voice", synthesize)
    with pytest.raises(RuntimeError, match="say failed"):
        await voice.synthesize("hello", SimpleNamespace(), tmp_path)
    assert not list(tmp_path.iterdir())


def begin_turn(transport: Any, event: str) -> None:
    room, owner = transport.c["rooms"][0], transport.c["owner"]
    req = Request(event, room, owner, "voice request", scope_of(transport.c["account"], room, owner))
    transport.store.accept_batch([req], None)
    assert transport.store.claim()["event_id"] == event


@pytest.mark.anyio
async def test_voice_reply_is_durable_encrypted_audio_after_text_and_cleaned(tmp_path: Path) -> None:
    async with running(tmp_path) as harness:
        transport = harness.f
        begin_turn(transport, "$voice-turn")
        transport.enqueue_voice(transport.c["rooms"][0], b"OggS reply", key="v1", after="$voice-turn")
        # Same key must neither double-send nor leave an orphan speech file.
        transport.enqueue_voice(transport.c["rooms"][0], b"OggS reply", key="v1", after="$voice-turn")
        directory = Path(transport.c["state_directory"]) / voice.REPLY_DIRNAME
        assert len(list(directory.iterdir())) == 1 and directory.stat().st_mode & 0o777 == 0o700
        assert transport.store.outbox() == []
        transport.store.finish("$voice-turn", "text answer", "session")
        text, audio = transport.store.outbox()
        assert audio["body"] == FILE_JOB_BODY and text["reply"] == "text answer"
        path = Path(json.loads(audio["reply"])["path"])
        assert path.exists() and path.stat().st_mode & 0o777 == 0o600
        transport.http, calls = _http()
        transport._encrypted_raw = AsyncMock(return_value="$audio")
        assert await transport._deliver(audio)
        content = transport._encrypted_raw.await_args.args[2]
        uploaded = [call[2] for call in calls if call[0] == "POST"][0]
        assert content["msgtype"] == "m.audio" and content["org.matrix.msc3245.voice"] == {}
        assert "url" not in content and uploaded != b"OggS reply"
        assert media.decrypt(uploaded, content["file"]) == b"OggS reply"
        assert not path.exists()


@pytest.mark.anyio
async def test_voice_reply_discard_on_cancel_and_retained_on_crash(tmp_path: Path) -> None:
    async with running(tmp_path) as harness:
        transport = harness.f
        directory = Path(transport.c["state_directory"])
        begin_turn(transport, "$cancelled")
        transport.enqueue_voice(transport.c["rooms"][0], b"speech", key="cancel", after="$cancelled")
        transport.store.uncertain_job("$cancelled")
        transport.store.resolve_uncertain("$cancelled", "cancelled")
        assert not list((directory / voice.REPLY_DIRNAME).iterdir())
        transport.store.delivered("$cancelled")
        begin_turn(transport, "$finished")
        transport.enqueue_voice(transport.c["rooms"][0], b"speech", key="retain", after="$finished")
        transport.store.finish("$finished", "answer")
        orphan = media.store(directory / voice.REPLY_DIRNAME, b"orphan", {"name": "voice.ogg", "mimetype": "audio/ogg"})
        transport.store.close()
        transport.store = MatrixStore(directory, transport.c["account"])
        assert not orphan.exists()
        assert len(list((directory / voice.REPLY_DIRNAME).iterdir())) == 1


def test_voice_cleanup_never_follows_symlink_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    retained = outside / ("document_" + "a" * 32 + ".ogg")
    retained.write_bytes(b"keep")
    state = tmp_path / "state"
    state.mkdir()
    (state / voice.REPLY_DIRNAME).symlink_to(outside)
    voice.cleanup_replies(state, set())
    assert retained.read_bytes() == b"keep"


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
@pytest.mark.parametrize("seconds,accepted", [(1, True), (3, False)])
async def test_real_ffmpeg_conversion_checks_actual_length_before_mocked_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: int, accepted: bool,
) -> None:
    path = media.store(tmp_path / "media", b"", {"name": "voice.wav", "mimetype": "audio/wav"})
    wav_file(path, seconds)
    transcriber = SimpleNamespace(client=SimpleNamespace(close=AsyncMock()), transcribe_audio=AsyncMock(return_value="hello"))
    monkeypatch.setattr(voice, "WhisperTranscriber", lambda **kwargs: transcriber)
    settings = SimpleNamespace(openai_api_key="test-key", max_voice_duration=2)
    if accepted:
        assert await voice.transcribe(path, settings) == "hello"
    else:
        with pytest.raises(media.AttachmentError, match="duration"):
            await voice.transcribe(path, settings)
        transcriber.transcribe_audio.assert_not_awaited()
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
async def test_audio_playlist_cannot_read_another_local_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = tmp_path / "other.wav"
    wav_file(secret, 1)
    path = media.store(tmp_path / "media", f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{secret}\n#EXT-X-ENDLIST\n".encode(),
                       {"name": "voice.ogg", "mimetype": "audio/ogg"})
    provider = AsyncMock()
    monkeypatch.setattr(voice, "WhisperTranscriber", provider)
    with pytest.raises(RuntimeError, match="ffmpeg conversion failed"):
        await voice.transcribe(path, SimpleNamespace(openai_api_key="test-key", max_voice_duration=2))
    provider.assert_not_called()
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.anyio
async def test_transcription_timeout_cleans_the_converted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = media.store(tmp_path / "media", b"audio", {"name": "voice.ogg", "mimetype": "audio/ogg"})

    async def convert(processor: AudioProcessor, source: Path, output: Path) -> Path:
        wav_file(output, 1)
        return output

    client = SimpleNamespace(close=AsyncMock())

    async def blocked(*args: Any, **kwargs: Any) -> str:
        await asyncio.Event().wait()
        return "unreachable"

    monkeypatch.setattr(AudioProcessor, "convert_audio", convert)
    monkeypatch.setattr(voice, "VOICE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(voice, "WhisperTranscriber", lambda **kwargs: SimpleNamespace(client=client, transcribe_audio=blocked))
    with pytest.raises(TimeoutError):
        await voice.transcribe(path, SimpleNamespace(openai_api_key="test-key", max_voice_duration=2))
    client.close.assert_awaited_once()
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.anyio
@pytest.mark.parametrize("status", [403, 503])
async def test_failed_voice_upload_keeps_only_retryable_speech_files(tmp_path: Path, status: int) -> None:
    async with running(tmp_path) as harness:
        transport = harness.f
        begin_turn(transport, "$voice-turn")
        transport.enqueue_voice(transport.c["rooms"][0], b"speech", key="v1", after="$voice-turn")
        transport.store.finish("$voice-turn", "text answer")
        row = [job for job in transport.store.outbox() if job["body"] == FILE_JOB_BODY][0]
        path = Path(json.loads(row["reply"])["path"])
        transport.http, _ = _http(upload_status=status)
        if status == 503:
            with pytest.raises(ConnectionError):
                await transport._deliver(row)
            assert path.exists()
            for _attempt in range(2):
                try:
                    await transport._deliver(row)
                except ConnectionError:
                    pass
        else:
            assert await transport._deliver(row)
        assert not path.exists()
        assert not [job for job in transport.store.outbox() if job["body"] == FILE_JOB_BODY]
