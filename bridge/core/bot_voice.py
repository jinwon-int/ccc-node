# ruff: noqa: E402
import asyncio
import logging
import platform
import time
from pathlib import Path as FilePath
from typing import Any, List, Optional, Tuple

from telegram import (
    Update,
    Message,
)
from telegram.ext import (
    ContextTypes,
)
from telegram_bot.core import media
from telegram_bot.core.bot_shared import build_reply_context_prefix
from telegram_bot.utils.chat_logger import log_debug
from telegram_bot.utils.transcription import (
    EmptyTranscriptionError,
    TranscriptionError,
    VolcengineFileFastTranscriber,
    WhisperTranscriber,
)
from telegram_bot.utils.tts import MacOSTtsSynthesizer, VoicePersonaNotAvailableError
from telegram_bot.utils.tos_uploader import TOSUploadError, VolcengineTOSUploader

logger = logging.getLogger(__name__)
STALE_MESSAGE_SECONDS = 20 * 60  # 20 minutes



class BotVoiceMixin:
    def _voice_config(self):
        return self._config

    def _prune_voice_tasks(self, key: Any) -> set[asyncio.Task]:
        tasks = self._user_voice_tasks.get(key)
        if not tasks:
            tasks = set()
            self._user_voice_tasks[key] = tasks
            return tasks
        done = {t for t in tasks if t.done()}
        tasks.difference_update(done)
        return tasks

    def _track_voice_task(self, key: Any, task: asyncio.Task) -> None:
        # key is the conversation key (user_id:chat_id in groups, bare user_id in
        # DMs) so /stop and /new only cancel voice work in the current chat.
        tasks = self._prune_voice_tasks(key)
        tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            current = self._user_voice_tasks.get(key)
            if current is not None:
                current.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                logger.debug("Voice task cancelled for conversation %s", key)
            except Exception as exc:
                logger.error(
                    "Voice task failed for conversation %s: %s", key, exc, exc_info=True
                )

        task.add_done_callback(_on_done)

    async def _cancel_user_voice_tasks(self, key: Any) -> int:
        tasks = self._prune_voice_tasks(key)
        cancelled = 0
        for task in list(tasks):
            if not task.done():
                task.cancel()
                cancelled += 1
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        tasks.clear()
        return cancelled

    async def _cleanup_stale_audio_files(
        self, audio_dir: FilePath, max_age_seconds: int
    ) -> int:
        return await self._audio_processor.cleanup_stale_audio_files(
            audio_dir=audio_dir,
            max_age_seconds=max_age_seconds,
        )

    @staticmethod
    def _resolve_voice_extension(mime_type: Optional[str]) -> str:
        return media.resolve_voice_extension(mime_type)

    @staticmethod
    def _build_voice_file_name(user_id: int, extension: str) -> str:
        return media.build_voice_file_name(user_id, extension)

    def _get_whisper_transcriber(self) -> WhisperTranscriber:
        if self._whisper_transcriber is None:
            self._whisper_transcriber = WhisperTranscriber(
                api_key=self._voice_config().openai_api_key,
                model=self._voice_config().whisper_model,
                base_url=self._voice_config().openai_base_url,
            )
        return self._whisper_transcriber

    def _get_volcengine_transcriber(self) -> VolcengineFileFastTranscriber:
        if self._volcengine_transcriber is None:
            self._volcengine_transcriber = VolcengineFileFastTranscriber(
                app_id=self._voice_config().volcengine_app_id,
                token=self._voice_config().volcengine_token,
                cluster=self._voice_config().volcengine_cluster,
                resource_id=self._voice_config().volcengine_resource_id,
                model_name=self._voice_config().volcengine_model_name,
                submit_endpoint=self._voice_config().volcengine_submit_endpoint,
                query_endpoint=self._voice_config().volcengine_query_endpoint,
                request_timeout=self._voice_config().volcengine_timeout_seconds,
                max_retries=self._voice_config().volcengine_max_retries,
                initial_backoff=self._voice_config().volcengine_initial_backoff,
                poll_interval_seconds=self._voice_config().volcengine_poll_interval_seconds,
                max_poll_seconds=self._voice_config().volcengine_max_poll_seconds,
            )
        return self._volcengine_transcriber

    def _get_volcengine_tos_uploader(self) -> VolcengineTOSUploader:
        if self._volcengine_tos_uploader is None:
            self._volcengine_tos_uploader = VolcengineTOSUploader(
                access_key=self._voice_config().volcengine_access_key,
                secret_access_key=self._voice_config().volcengine_secret_access_key,
                endpoint=self._voice_config().volcengine_tos_endpoint,
                region=self._voice_config().volcengine_tos_region,
                bucket_name=self._voice_config().volcengine_tos_bucket_name,
                signed_url_ttl_seconds=self._voice_config().volcengine_tos_signed_url_ttl_seconds,
            )
        return self._volcengine_tos_uploader

    def _get_tts_synthesizer(self) -> MacOSTtsSynthesizer:
        if self._tts_synthesizer is None:
            self._tts_synthesizer = MacOSTtsSynthesizer(
                ffmpeg_path=self._voice_config().ffmpeg_path,
            )
        return self._tts_synthesizer

    def _get_transcription_provider(self) -> str:
        return str(getattr(self._voice_config(), "transcription_provider", "whisper")).strip().lower()

    @staticmethod
    def _is_macos() -> bool:
        return media.is_macos()

    @staticmethod
    def _count_hanzi(text: str) -> int:
        return media.count_hanzi(text)

    @staticmethod
    def _count_english_words(text: str) -> int:
        return media.count_english_words(text)

    def _resolve_next_reply_mode(
        self, *, current_mode: str, message_source: str, user_text: str
    ) -> str:
        del current_mode, user_text
        return media.resolve_next_reply_mode(message_source, is_macos=self._is_macos())

    @staticmethod
    def _normalize_reply_mode(mode: Optional[str]) -> str:
        return media.normalize_reply_mode(mode)

    def _get_voice_delivery_strategy(self, content: str) -> str:
        return media.voice_delivery_strategy(content)

    async def _send_voice_message(self, message: Message, content: str) -> None:
        tts = self._get_tts_synthesizer()
        persona = getattr(self._voice_config(), "voice_reply_persona", "")
        voice_path: Optional[FilePath] = None
        cleanup_paths: List[FilePath] = []
        selected_voice = ""
        try:
            (
                voice_path,
                cleanup_paths,
                selected_voice,
            ) = await tts.synthesize_to_telegram_voice(
                text=content,
                output_dir=self._audio_dir,
                persona=persona,
            )
            with open(voice_path, "rb") as voice_stream:
                await message.reply_voice(voice=voice_stream)
            logger.info(
                "Voice reply sent persona=%s selected_voice=%s output=%s",
                persona,
                selected_voice,
                voice_path,
            )
        finally:
            await self._audio_processor.cleanup_audio_files(cleanup_paths)

    async def _send_content_artifacts(
        self, message: Message, content: str, force_options: bool
    ) -> None:
        resolved_paths = self._resolve_paths(content)
        in_root_paths, outside_paths = self._split_paths_by_scope(resolved_paths)
        logger.debug(
            f"_send_content_artifacts: resolved={len(resolved_paths)} paths, "
            f"in_root={len(in_root_paths)}, outside={len(outside_paths)}, "
            f"paths={[str(p) for p in resolved_paths]}"
        )
        await self._send_file_paths(message.chat.id, in_root_paths)

        # Inline option buttons are opt-in (CCC_TELEGRAM_OPTION_BUTTONS). When
        # off (default), the numbered options remain in the message text and the
        # user types their choice — no tap-to-select keyboard.
        if force_options and getattr(self._voice_config(), "enable_option_buttons", False):
            options = self._extract_options(content)
            kb = self._build_option_keyboard(options)
            if kb:
                await message.reply_text("Please select:", reply_markup=kb)

    @staticmethod
    def _merge_voice_preview(content: str, voice_input_preview: Optional[str]) -> str:
        preview = str(voice_input_preview or "").strip()
        if not preview:
            return content
        body = str(content or "").strip()
        if not body:
            return preview
        return f"{preview}\n\n{body}"

    async def _send_reply_by_mode(
        self,
        *,
        message: Message,
        user_id: int,
        content: str,
        parse_mode: str,
        force_options: bool,
        streamed: bool,
        reply_mode: str,
        voice_input_preview: Optional[str] = None,
    ) -> None:
        content_with_preview = self._merge_voice_preview(content, voice_input_preview)
        preview_text = str(voice_input_preview or "").strip()
        preview_sent_first = False
        if reply_mode != "voice":
            await self._reply_smart(
                message,
                content_with_preview,
                parse_mode=parse_mode,
                force_options=force_options,
                streamed=streamed,
            )
            return

        if not self._is_macos():
            logger.info(
                "Voice reply disabled on non-macOS user_id=%s platform=%s",
                user_id,
                platform.system(),
            )
            await self._reply_smart(
                message,
                content_with_preview,
                parse_mode=parse_mode,
                force_options=force_options,
                streamed=streamed,
            )
            return

        strategy = self._get_voice_delivery_strategy(content)
        if strategy == "text_only":
            logger.info(
                "Voice reply text-only fallback user_id=%s reason=long_reply hanzi_count=%s english_word_count=%s char_count=%s",
                user_id,
                self._count_hanzi(content),
                self._count_english_words(content),
                len(content),
            )
            await self._reply_smart(
                message,
                content_with_preview,
                parse_mode=parse_mode,
                force_options=force_options,
                streamed=streamed,
            )
            return

        try:
            if strategy == "voice_only" and preview_text:
                await message.reply_text(preview_text)
                preview_sent_first = True
            await self._send_voice_message(message, content)
        except VoicePersonaNotAvailableError as exc:
            error_message = (
                f"❌ 当前配置的 VOICE_REPLY_PERSONA=`{exc.persona}` 在本机不可用。\n"
                "建议执行 `say -v ?` 查看完整可用音色。\n"
                "请将 VOICE_REPLY_PERSONA 设置为输出第一列中的名称。"
            )
            await message.reply_text(error_message, parse_mode="Markdown")
            logger.error(
                "Voice reply persona unavailable user_id=%s persona=%s available_voice_count=%s",
                user_id,
                exc.persona,
                len(exc.available_voices),
            )
            fallback_content = content if preview_sent_first else content_with_preview
            await self._reply_smart(
                message,
                fallback_content,
                parse_mode=parse_mode,
                force_options=force_options,
                streamed=streamed,
            )
            return
        except Exception as exc:
            logger.error(
                "Voice reply synthesis failed user_id=%s fallback=text error=%s",
                user_id,
                exc,
                exc_info=True,
            )
            fallback_content = content if preview_sent_first else content_with_preview
            await self._reply_smart(
                message,
                fallback_content,
                parse_mode=parse_mode,
                force_options=force_options,
                streamed=streamed,
            )
            return

        if strategy == "voice_and_text":
            await self._reply_smart(
                message,
                content_with_preview,
                parse_mode=parse_mode,
                force_options=force_options,
                streamed=streamed,
            )
            return

        await self._send_content_artifacts(message, content, force_options)

    @staticmethod
    def _redact_telegram_file_url(url: str) -> str:
        return media.redact_telegram_file_url(url)

    async def _build_telegram_file_url(self, file_id: str) -> str:
        app = self._require_application()
        telegram_file = await app.bot.get_file(file_id)
        file_path = str(getattr(telegram_file, "file_path", "") or "").strip()
        if file_path.startswith(("http://", "https://")):
            return file_path

        normalized_path = file_path.lstrip("/")
        if not normalized_path:
            raise RuntimeError("Telegram file path is unavailable.")

        return f"https://api.telegram.org/file/bot{self._voice_config().telegram_bot_token}/{normalized_path}"

    @staticmethod
    def _resolve_image_extension(mime_type: Optional[str], file_name: Optional[str] = None) -> str:
        return media.resolve_image_extension(mime_type, file_name)

    @staticmethod
    def _build_image_file_name(user_id: int, extension: str) -> str:
        return media.build_image_file_name(user_id, extension)

    @staticmethod
    def _select_inbound_image(message: Message) -> Tuple[Optional[Any], str]:
        return media.select_inbound_image(message)

    @staticmethod
    def _build_image_prompt(image_path: FilePath, caption: str) -> str:
        return media.build_image_prompt(image_path, caption)

    async def _download_telegram_file(self, file_id: str, destination: FilePath) -> None:
        app = self._require_application()
        telegram_file = await app.bot.get_file(file_id)
        logger.debug("Downloading Telegram file to %s", destination)
        if hasattr(telegram_file, "download_to_drive"):
            await telegram_file.download_to_drive(custom_path=str(destination))
            return
        if hasattr(app.bot, "download_file"):
            await app.bot.download_file(
                telegram_file.file_path, custom_path=str(destination)
            )
            return
        raise RuntimeError("Telegram file download API is unavailable.")

    async def _download_voice_file(self, voice, destination: FilePath) -> None:
        await self._download_telegram_file(voice.file_id, destination)

    async def _download_image_file(self, image, destination: FilePath) -> None:
        await self._download_telegram_file(image.file_id, destination)

    async def _prepare_audio_for_whisper(
        self, source_path: FilePath, cleanup_paths: List[FilePath]
    ) -> FilePath:
        detected_format = await self._audio_processor.detect_audio_format(source_path)
        logger.debug("Detected voice format %s for %s", detected_format, source_path)

        if detected_format == "mp3":
            return source_path

        if detected_format not in {"amr", "ogg"}:
            return source_path

        ffmpeg_ready = await self._audio_processor.check_ffmpeg_available()
        if not ffmpeg_ready:
            raise RuntimeError(
                "ffmpeg is not installed. Install ffmpeg and retry voice message processing."
            )

        converted_path = source_path.with_suffix(".mp3")
        cleanup_paths.append(converted_path)
        converted = await self._audio_processor.convert_audio(
            source_path, converted_path
        )
        return converted

    async def _handle_photo_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        del context
        if not await self._check_access(update):
            return
        message = self._require_message(update)
        image, image_kind = self._select_inbound_image(message)
        if image is None:
            return

        user_id = self._require_user(update).id
        chat = self._require_chat(update)
        conversation_key = self._conversation_key(user_id, chat.id)
        file_id = getattr(image, "file_id", "")
        caption = getattr(message, "caption", None) or ""
        log_debug(user_id, "image", f"{image_kind}:{file_id} caption_len={len(caption)}")

        async def run_task():
            self._image_dir.mkdir(parents=True, exist_ok=True)
            start = time.perf_counter()
            cleanup_paths: List[FilePath] = []
            outcome = "failed"
            try:
                extension = self._resolve_image_extension(
                    getattr(image, "mime_type", None), getattr(image, "file_name", None)
                )
                image_path = self._image_dir / self._build_image_file_name(
                    user_id=user_id, extension=extension
                )
                cleanup_paths.append(image_path)
                try:
                    await self._download_image_file(image, image_path)
                except Exception as exc:
                    logger.error(
                        "Image file download failed for user %s: %s",
                        user_id,
                        exc,
                        exc_info=True,
                    )
                    await message.reply_text(
                        "❌ Failed to download your image. Please retry."
                    )
                    outcome = "download_failed"
                    return

                prompt = self._build_image_prompt(image_path, caption)
                reply_prefix = build_reply_context_prefix(
                    message,
                    bot_user_id=self._own_bot_id(),
                    owner_user_id=user_id,
                )
                if reply_prefix:
                    prompt = f"{reply_prefix}\n\n{prompt}"
                await self._process_user_message_text(
                    update,
                    user_id,
                    prompt,
                    message_source="image",
                )
                outcome = "success"
            except asyncio.CancelledError:
                outcome = "cancelled"
                logger.info("Image processing cancelled for user %s", user_id)
                raise
            finally:
                await self._audio_processor.cleanup_audio_files(cleanup_paths)
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                logger.info(
                    "Image processing result user_id=%s kind=%s outcome=%s elapsed_ms=%s",
                    user_id,
                    image_kind,
                    outcome,
                    elapsed_ms,
                )

        async def on_overflow():
            reply = (
                f"⏳ Image queue is full ({self._MAX_INFLIGHT_MESSAGES} active tasks). "
                "Please wait or send /stop to terminate running tasks."
            )
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)

        await self._enqueue_user_task(conversation_key, run_task, on_overflow)

    async def _handle_voice_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        del context
        if not await self._check_access(update):
            return
        message = self._require_message(update)
        if not message.voice:
            return

        user_id = self._require_user(update).id
        chat = self._require_chat(update)
        conversation_key = self._conversation_key(user_id, chat.id)
        voice = message.voice
        log_debug(user_id, "voice", f"voice:{voice.file_id} duration={voice.duration}")

        async def run_task():
            task = asyncio.current_task()
            if task is not None:
                self._track_voice_task(conversation_key, task)

            self._audio_dir.mkdir(parents=True, exist_ok=True)
            start = time.perf_counter()
            cleanup_paths: List[FilePath] = []
            outcome = "failed"

            try:
                if voice.duration and voice.duration > self._voice_config().max_voice_duration:
                    await message.reply_text(
                        f"❌ Voice message is too long. Max duration is {self._voice_config().max_voice_duration} seconds."
                    )
                    outcome = "duration_limit_exceeded"
                    return

                provider = self._get_transcription_provider()
                if provider == "whisper":
                    extension = self._resolve_voice_extension(
                        getattr(voice, "mime_type", None)
                    )
                    file_name = self._build_voice_file_name(
                        user_id=user_id, extension=extension
                    )
                    source_path = self._audio_dir / file_name
                    cleanup_paths.append(source_path)
                    logger.debug("Voice temp file path: %s", source_path)

                    try:
                        await self._download_voice_file(voice, source_path)
                    except Exception as exc:
                        logger.error(
                            "Voice file download failed for user %s: %s",
                            user_id,
                            exc,
                            exc_info=True,
                        )
                        await message.reply_text(
                            "❌ Failed to download your voice message. Please retry."
                        )
                        outcome = "download_failed"
                        return

                    try:
                        audio_path = await self._prepare_audio_for_whisper(
                            source_path, cleanup_paths
                        )
                    except Exception as exc:
                        logger.error(
                            "Voice conversion failed for user %s: %s",
                            user_id,
                            exc,
                            exc_info=True,
                        )
                        await message.reply_text(
                            "❌ Failed to convert audio for transcription. "
                            "Please ensure ffmpeg is installed and try again."
                        )
                        outcome = "conversion_failed"
                        return

                    try:
                        transcriber = self._get_whisper_transcriber()
                    except ValueError:
                        await message.reply_text(
                            "❌ Voice transcription is not configured. Please set OPENAI_API_KEY."
                        )
                        outcome = "missing_openai_key"
                        return

                    try:
                        text = await transcriber.transcribe_audio(
                            audio_path, duration_seconds=voice.duration
                        )
                    except EmptyTranscriptionError:
                        await message.reply_text(
                            "❌ No speech was detected in your voice message. Please try again."
                        )
                        outcome = "empty_transcription"
                        return
                    except TranscriptionError as exc:
                        logger.error(
                            "Whisper transcription failed for user %s: %s",
                            user_id,
                            exc,
                        )
                        await message.reply_text(
                            "❌ Failed to transcribe your voice message. Please try again later."
                        )
                        outcome = "transcription_failed"
                        return
                elif provider == "volcengine":
                    try:
                        transcriber = self._get_volcengine_transcriber()
                        tos_uploader = self._get_volcengine_tos_uploader()
                    except ValueError as exc:
                        logger.error(
                            "Volcengine transcription is not configured for user %s: %s",
                            user_id,
                            exc,
                        )
                        await message.reply_text(
                            "❌ Voice transcription is not configured. "
                            "Please set Volcengine credentials in .env."
                        )
                        outcome = "missing_volcengine_key"
                        return
                    except RuntimeError as exc:
                        logger.error(
                            "Volcengine transcription dependency is unavailable for user %s: %s",
                            user_id,
                            exc,
                        )
                        await message.reply_text(
                            "❌ Voice transcription dependency is missing. "
                            "Please install requirements and restart the bot."
                        )
                        outcome = "missing_volcengine_dependency"
                        return

                    extension = self._resolve_voice_extension(
                        getattr(voice, "mime_type", None)
                    )
                    file_name = self._build_voice_file_name(
                        user_id=user_id, extension=extension
                    )
                    source_path = self._audio_dir / file_name
                    cleanup_paths.append(source_path)
                    logger.debug("Voice temp file path: %s", source_path)

                    try:
                        await self._download_voice_file(voice, source_path)
                    except Exception as exc:
                        logger.error(
                            "Voice file download failed for user %s: %s",
                            user_id,
                            exc,
                            exc_info=True,
                        )
                        await message.reply_text(
                            "❌ Failed to download your voice message. Please retry."
                        )
                        outcome = "download_failed"
                        return

                    uploaded_object_key: Optional[str] = None
                    try:
                        uploaded = await asyncio.to_thread(
                            tos_uploader.upload_file_with_object_key,
                            source_path,
                            user_id,
                        )
                        audio_url = uploaded.signed_url
                        uploaded_object_key = uploaded.object_key
                    except TOSUploadError as exc:
                        logger.error(
                            "Failed to upload voice file to TOS for user %s: %s",
                            user_id,
                            exc,
                            exc_info=True,
                        )
                        await message.reply_text(
                            "❌ Failed to prepare your voice file for transcription. Please retry."
                        )
                        outcome = "tos_upload_failed"
                        return
                    except Exception:
                        logger.error(
                            "Unexpected TOS preparation error for user %s",
                            user_id,
                            exc_info=True,
                        )
                        await message.reply_text(
                            "❌ Failed to prepare your voice file for transcription. Please retry."
                        )
                        outcome = "tos_upload_failed"
                        return

                    try:
                        text = await transcriber.transcribe_audio(
                            audio_url, duration_seconds=voice.duration
                        )
                    except EmptyTranscriptionError:
                        await message.reply_text(
                            "❌ No speech was detected in your voice message. Please try again."
                        )
                        outcome = "empty_transcription"
                        return
                    except TranscriptionError as exc:
                        logger.error(
                            "Volcengine transcription failed for user %s: %s",
                            user_id,
                            exc,
                        )
                        await message.reply_text(
                            "❌ Failed to transcribe your voice message. Please try again later."
                        )
                        outcome = "transcription_failed"
                        return
                    finally:
                        if uploaded_object_key:
                            try:
                                await asyncio.to_thread(
                                    tos_uploader.delete_object, uploaded_object_key
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Failed to delete temporary TOS voice object for user %s key=%s: %s",
                                    user_id,
                                    uploaded_object_key,
                                    exc,
                                    exc_info=True,
                                )
                else:
                    logger.error(
                        "Unsupported transcription provider '%s' for user %s",
                        provider,
                        user_id,
                    )
                    await message.reply_text(
                        "❌ Voice transcription provider is invalid. "
                        "Please check TRANSCRIPTION_PROVIDER."
                    )
                    outcome = "invalid_provider"
                    return

                preview = f"🎤 Voice: {text}"
                reply_prefix = build_reply_context_prefix(
                    message,
                    bot_user_id=self._own_bot_id(),
                    owner_user_id=user_id,
                )
                task_text = f"{reply_prefix}\n\n{text}" if reply_prefix else text
                await self._process_user_message_text(
                    update,
                    user_id,
                    task_text,
                    message_source="voice",
                    voice_input_preview=preview,
                )
                outcome = "success"
            except asyncio.CancelledError:
                outcome = "cancelled"
                logger.info("Voice processing cancelled for user %s", user_id)
                raise
            finally:
                await self._audio_processor.cleanup_audio_files(cleanup_paths)
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                logger.info(
                    "Voice processing result user_id=%s duration=%s outcome=%s elapsed_ms=%s",
                    user_id,
                    voice.duration,
                    outcome,
                    elapsed_ms,
                )

        async def on_overflow():
            reply = (
                f"⏳ Voice queue is full ({self._MAX_INFLIGHT_MESSAGES} active tasks). "
                "Please wait or send /stop to terminate running tasks."
            )
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)

        await self._enqueue_user_task(conversation_key, run_task, on_overflow)

