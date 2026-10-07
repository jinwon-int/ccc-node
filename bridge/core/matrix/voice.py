"""Matrix voice processing; decrypted input and generated speech stay private."""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import wave

from telegram_bot.core.matrix import media
from telegram_bot.utils.audio_processor import AudioProcessor
from telegram_bot.utils.transcription import WhisperTranscriber
from telegram_bot.utils.tts import MacOSTtsSynthesizer

VOICE_TIMEOUT_S = 120
REPLY_DIRNAME = "voice-replies"
_REPLY_NAME = re.compile(r"document_[0-9a-f]{32}\.ogg")
VOICE_FAILURES = {
    "duration": "❌ Voice message exceeds MAX_VOICE_DURATION.",
    "configuration": "❌ Set OPENAI_API_KEY to enable voice transcription.",
    "provider": "❌ Matrix voice transcription currently requires TRANSCRIPTION_PROVIDER=whisper.",
}
VOICE_FAILED = "❌ Could not transcribe this voice message. Please try again."


async def transcribe(path: Path, settings: Any) -> str:
    """Decode bounded PCM locally, check actual duration, then invoke Whisper.

    A sender's duration is only a preflight hint. Decode at most limit + 1
    seconds and reject the resulting WAV before any provider request when
    the actual duration exceeds the limit. ffmpeg cannot fetch remote inputs.
    """
    limit = int(getattr(settings, "max_voice_duration", 300))
    processor = AudioProcessor(
        ffmpeg_path=getattr(settings, "ffmpeg_path", None),
        input_args=(
            "-nostdin", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "ogg,mp3,wav,mov,amr,flac,aac,matroska,webm",
        ),
        ffmpeg_args=("-vn", "-ac", "1", "-ar", "16000", "-t", str(limit + 1)),
    )
    converted = media.store(path.parent, b"", {"name": "voice.wav", "mimetype": "audio/wav"})
    try:
        async with asyncio.timeout(VOICE_TIMEOUT_S):
            await processor.convert_audio(path, converted)
            with wave.open(str(converted), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
            if duration > limit:
                raise media.AttachmentError("duration")
            if duration <= 0:
                raise ValueError("empty-audio")
            transcriber = WhisperTranscriber(
                api_key=settings.openai_api_key,
                model=getattr(settings, "whisper_model", "whisper-1"),
                base_url=getattr(settings, "openai_base_url", None),
            )
            try:
                text = await transcriber.transcribe_audio(converted, duration_seconds=math.ceil(duration))
                if not text.strip():
                    raise ValueError("empty-transcription")
                return text.strip()
            finally:
                await transcriber.client.close()
    finally:
        media.remove(converted)


def check_configuration(settings: Any) -> None:
    """Refuse unsupported/missing configuration before downloading audio."""
    if getattr(settings, "transcription_provider", "whisper") != "whisper":
        raise media.AttachmentError("provider")
    if not str(getattr(settings, "openai_api_key", "") or "").strip():
        raise media.AttachmentError("configuration")


async def synthesize(text: str, settings: Any, directory: Path) -> bytes:
    """Reuse the existing macOS say → Ogg/Opus backend; clean partial outputs too."""
    # The parent was already created privately while staging the input.
    with tempfile.TemporaryDirectory(prefix="voice-", dir=directory) as temporary:
        async with asyncio.timeout(VOICE_TIMEOUT_S):
            tts = MacOSTtsSynthesizer(ffmpeg_path=getattr(settings, "ffmpeg_path", None))
            path, _cleanup, _voice = await tts.synthesize_to_telegram_voice(
                text=text, output_dir=Path(temporary),
                persona=getattr(settings, "voice_reply_persona", "Tingting"),
            )
            from telegram_bot.core.matrix.outbound_media import read_deliverable

            return read_deliverable(path.resolve(), max_bytes=50_000_000, root=Path(temporary))


def reply_name(payload: str) -> str | None:
    """Only bridge-owned generated voice files qualify for automatic cleanup."""
    try:
        record = json.loads(payload)
        if not isinstance(record, dict):
            return None
        name = Path(record["path"]).name
        return name if record.get("voice") is True and _REPLY_NAME.fullmatch(name) else None
    except (ValueError, KeyError, TypeError):
        return None


def cleanup_replies(directory: Path, retained: set[str]) -> None:
    """Remove orphan speech files using a directory fd, never following a symlink."""
    try:
        descriptor = os.open(directory / REPLY_DIRNAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        for name in os.listdir(descriptor):
            if _REPLY_NAME.fullmatch(name) and name not in retained:
                try:
                    os.unlink(name, dir_fd=descriptor)
                except OSError:
                    pass
    finally:
        os.close(descriptor)
