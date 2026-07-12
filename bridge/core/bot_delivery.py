# ruff: noqa: E402
import logging
import secrets
from pathlib import Path as FilePath
from typing import Any, Dict, List, Optional, Tuple

import telegram.error
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
)
from telegram.ext import (
    ContextTypes,
)
from telegram_bot.core import ui
from telegram_bot.core import paths as path_scope
from telegram_bot.core.bot_shared import build_reply_context_prefix
from telegram_bot.utils.chat_logger import log_debug
from telegram_bot.utils.tg_format import wrap_markdown_tables
from telegram_bot.utils.tg_robust import send_with_retry
from telegram_bot.utils import tg_md
from telegram_bot.utils import tg_readable
from telegram_bot.utils import tg_entities

logger = logging.getLogger(__name__)
STALE_MESSAGE_SECONDS = 20 * 60  # 20 minutes




class BotDeliveryMixin:
    async def _handle_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle text messages - use project chat or answer pending questions"""
        if not await self._check_access(update):
            return
        message = self._require_message(update)
        if not message.text:
            return

        user_id = self._require_user(update).id
        chat = self._require_chat(update)
        conversation_key = self._conversation_key(user_id, chat.id)
        text = message.text
        session = await self._session_manager.get_session(conversation_key)

        # Check resume selection (user replies with a number)
        resume_list = session.get("resume_list")
        if resume_list and text.strip().isdigit():
            log_debug(user_id, "user", text)
            idx = int(text.strip()) - 1
            if 0 <= idx < len(resume_list):
                entry = resume_list[idx]
                sid, msg = entry[:2]
                provider = entry[2] if len(entry) > 2 else "claude"
                active_provider = self._active_provider()
                if provider != active_provider:
                    reply = (
                        f"❌ Provider mismatch: selected session is {provider}, "
                        f"but the active provider is {active_provider}."
                    )
                    await message.reply_text(reply)
                    log_debug(user_id, "bot", reply)
                    return
                await self._session_manager.patch_session(
                    conversation_key,
                    updates={
                        "provider": provider,
                        "session_id": sid,
                        "new_session": False,
                    },
                    remove_fields={"resume_list"},
                )
                self._runtime_active_sessions.add(conversation_key)
                reply = f"✅ Switched to session: {msg}"
                await message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                # Claude's legacy transcript path provides a progress summary.
                # Codex selections must not access Claude transcript files.
                if provider == "claude":
                    last_msg = self._project_chat.get_session_last_assistant_message(sid)
                    if last_msg:
                        progress = f"📋 {last_msg}"
                        await message.reply_text(progress)
                        log_debug(user_id, "bot", progress)
                return
            else:
                reply = "❌ Invalid number, please try again."
                await message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                return

        # Clear resume list if user sends non-number
        if resume_list:
            await self._session_manager.patch_session(
                conversation_key, remove_fields={"resume_list"}
            )

        # Capture explicit outside-path approval/denial from user replies.
        await self._maybe_capture_outside_approval(user_id, text, chat.id)

        # Check if there's a pending question
        pending = await self._session_manager.get_pending_question(conversation_key)
        if pending:
            log_debug(user_id, "user", f"[answer] {text}")
            await self._session_manager.clear_pending_question(conversation_key)
            reply = f"✅ Answer received: {text}\n\nContinuing..."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        # Inject replied-to (quoted) original as context so the agent knows
        # which prior message the user is referencing. The special branches
        # above (resume selection, pending-question answer) return early and
        # deliberately keep the raw text.
        reply_prefix = build_reply_context_prefix(
            message,
            bot_user_id=self._own_bot_id(),
            owner_user_id=user_id,
        )
        task_text = f"{reply_prefix}\n\n{text}" if reply_prefix else text

        async def run_task():
            await self._process_user_message_text(update, user_id, task_text)

        async def on_overflow():
            reply = "⏳ Processing previous messages, please wait or send /stop to terminate."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)

        await self._enqueue_user_task(conversation_key, run_task, on_overflow)

    def _resolve_paths(self, content: str) -> List[FilePath]:
        """Extract file paths and resolve relative ones against injected PROJECT_ROOT."""
        paths = []
        seen = set()
        for m in self._FILE_PATH_RE.findall(content):
            p = FilePath(m.strip())
            if not p.is_absolute():
                p = self._project_root() / p
            p = p.resolve()
            if p not in seen and p.is_file() and p.stat().st_size < 10 * 1024 * 1024:
                seen.add(p)
                paths.append(p)
        return paths

    def _split_paths_by_scope(
        self, paths: List[FilePath]
    ) -> Tuple[List[FilePath], List[FilePath]]:
        return path_scope.split_paths_by_scope(paths, self._project_root())

    def _extract_options(self, text: str) -> List[str]:
        """Extract numbered options from text like '1. xxx\n2. xxx'."""
        return ui.extract_options(text)

    def _build_option_keyboard(
        self, options: List[str]
    ) -> Optional[InlineKeyboardMarkup]:
        """Build inline keyboard from extracted options."""
        return ui.build_option_keyboard(options)

    def _build_history_keyboard(
        self, messages: List[Dict[str, Any]], page: int = 0, page_size: int = 10
    ) -> InlineKeyboardMarkup:
        """Build inline keyboard for message history selection."""
        return ui.build_history_keyboard(messages, page, page_size)

    @staticmethod
    def _format_relative_time(timestamp: str) -> str:
        """Format timestamp as relative time (see ui.format_relative_time)."""
        return ui.format_relative_time(timestamp)

    def _build_revert_mode_keyboard(self, msg_index: int) -> InlineKeyboardMarkup:
        """Build inline keyboard for revert mode selection."""
        return ui.build_revert_mode_keyboard(msg_index)

    async def _send_file_paths(self, chat_id: int, paths: List[FilePath]) -> None:
        app = self._require_application()
        bot = app.bot
        logger.debug(f"_send_file_paths: sending {len(paths)} files to chat {chat_id}")
        for p in paths:
            try:
                logger.debug(f"Sending file: {p} (suffix: {p.suffix.lower()})")
                if p.suffix.lower() in self._IMAGE_EXTS:
                    with open(p, "rb") as f:
                        await bot.send_photo(chat_id, photo=f)
                    logger.info(f"Sent photo: {p}")
                else:
                    with open(p, "rb") as f:
                        await bot.send_document(chat_id, document=f)
                    logger.info(f"Sent document: {p}")
            except Exception as e:
                logger.warning(f"Failed to send file {p}: {e}")

    async def _prompt_outside_file_confirmation(
        self, chat_id: int, user_id: int, paths: List[FilePath]
    ) -> None:
        session_key = self._conversation_key(user_id, chat_id)
        request_token = secrets.token_urlsafe(12)
        await self._session_manager.patch_session(
            session_key,
            updates={
                "pending_external_files": [str(p) for p in paths],
                "pending_external_files_token": request_token,
            },
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Send external files",
                        callback_data=f"extsend:{request_token}:allow",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel", callback_data=f"extsend:{request_token}:deny"
                    )
                ],
            ]
        )
        app = self._require_application()
        await app.bot.send_message(
            chat_id,
            "File paths outside PROJECT_ROOT detected. Confirmation required before sending.",
            reply_markup=kb,
        )

    @staticmethod
    def _split_text(text: str, limit: int = 4000) -> List[str]:
        """Split text into chunks no longer than limit (see ui.split_text)."""
        return ui.split_text(text, limit)

    async def _deliver_markdown(self, content: str, op, base_parse_mode: str = "Markdown"):
        """Split *content* and send each chunk via ``op(text, parse_mode)``.

        ``op`` is a callable ``(text, parse_mode|None) -> awaitable`` returning a
        fresh awaitable each call. Markdown is rendered to Telegram **MarkdownV2**
        (GFM tables -> aligned code blocks, special chars escaped) when the
        telegramify renderer is available; on the rare per-chunk parse error we
        fall back to clean plain text for that chunk only. ``HTML`` callers keep
        HTML; if the renderer is unavailable we use the legacy
        ``wrap_markdown_tables`` + base-parse-mode path.
        """
        # Per-bubble size: non-streaming replies split into the same digestible
        # messages as the streaming path (CCC_TELEGRAM_MAX_BUBBLE_CHARS), bounded
        # by the Telegram hard limit.
        limit = max(
            200,
            min(
                int(getattr(self._config, "telegram_max_bubble_chars", 4000)),
                tg_md.TELEGRAM_LIMIT,
            ),
        )

        # HTML callers (e.g. /skills listing) keep their existing behavior.
        if base_parse_mode == "HTML":
            for part in self._split_text(wrap_markdown_tables(content), limit):
                try:
                    await send_with_retry(lambda p=part: op(p, "HTML"))
                except telegram.error.BadRequest:
                    await send_with_retry(lambda p=part: op(p, None))
            return

        # Normalize layout for mobile readability (loose-spacing etc.) before the
        # MarkdownV2 conversion — mirrors the streaming finalize path so the
        # non-streaming delivery path (the default since live streaming is
        # opt-in, see CCC_TELEGRAM_STREAMING) renders identically. Shared helper
        # keeps both paths from drifting. Content-preserving, idempotent,
        # fail-open.
        render_text = tg_readable.render_for_delivery(
            content,
            enabled=getattr(self._config, "enable_readable_renderer", False),
            loose=getattr(self._config, "enable_loose_spacing", False),
            spacing=getattr(self._config, "spacing_lines", 1),
        )

        # Entity path (opt-in via CCC_TELEGRAM_ENTITY_RENDERER, default on):
        # send (text + MessageEntity[]) without parse_mode, avoiding MarkdownV2
        # escape failures. Mirrors the streaming finalize path so both delivery
        # paths render identically. Fail-open: to_entity_chunks returns None when
        # the renderer is unavailable (-> MarkdownV2 below), and each chunk
        # degrades to plain text on the rare BadRequest (per-message, so no
        # duplication on partial failure).
        if getattr(self._config, "enable_entity_renderer", False):
            entity_chunks = tg_entities.to_entity_chunks(render_text, limit)
            if entity_chunks:
                for text, entities in entity_chunks:
                    try:
                        await send_with_retry(
                            lambda t=text, e=entities: op(t, None, e or None)
                        )
                    except telegram.error.BadRequest:
                        await send_with_retry(lambda t=text: op(t, None))
                return

        if tg_md.available():
            # Convert the whole message to MarkdownV2 first, THEN split on
            # entity-safe boundaries with split_markdownv2. Splitting the raw
            # markdown before conversion is unsafe: MarkdownV2 escaping expands
            # the text (~1.2x, more for tables/symbol-dense content), so a
            # sub-limit raw chunk can exceed TELEGRAM_LIMIT once escaped and was
            # silently dropped to plain text (all formatting lost). Per-part
            # plain fallback only on the rare BadRequest.
            md2 = tg_md.to_markdownv2(render_text)
            if md2 is not None:
                for part in tg_md.split_markdownv2(md2, limit):
                    try:
                        await send_with_retry(lambda p=part: op(p, "MarkdownV2"))
                    except telegram.error.BadRequest:
                        await send_with_retry(lambda p=part: op(p, None))
                return
            # conversion unavailable/failed -> legacy path below

        # Legacy fallback: telegramify unavailable -> wrap tables + base parse mode.
        for part in self._split_text(wrap_markdown_tables(render_text), limit):
            try:
                await send_with_retry(lambda p=part: op(p, base_parse_mode))
            except telegram.error.BadRequest:
                await send_with_retry(lambda p=part: op(p, None))

    async def _reply_smart(
        self,
        message,
        content: str,
        parse_mode: str = "Markdown",
        force_options: bool = False,
        streamed: bool = False,
    ):
        """Reply with text (splitting if needed), send referenced files, and add option buttons."""
        # Skip text sending if already streamed
        if not streamed:
            await self._deliver_markdown(
                content,
                lambda t, pm=None, ents=None: message.reply_text(
                    t, parse_mode=pm, entities=ents
                ),
                base_parse_mode=parse_mode,
            )

        await self._send_content_artifacts(message, content, force_options)

    async def _send_smart(
        self,
        chat_id: int,
        content: str,
        user_id: Optional[int] = None,
        force_options: bool = False,
        streamed: bool = False,
    ):
        """Send text to chat_id (splitting if needed) with file and option detection."""
        app = self._require_application()
        bot = app.bot

        # Skip text sending if already streamed
        if not streamed:
            await self._deliver_markdown(
                content,
                lambda t, pm=None, ents=None: bot.send_message(
                    chat_id, t, parse_mode=pm, entities=ents
                ),
            )

        resolved_paths = self._resolve_paths(content)
        in_root_paths, _ = self._split_paths_by_scope(resolved_paths)
        await self._send_file_paths(chat_id, in_root_paths)
        # Inline option buttons are opt-in (CCC_TELEGRAM_OPTION_BUTTONS); default
        # off, so numbered options stay as text and the user types their choice.
        if force_options and getattr(self._config, "enable_option_buttons", False):
            options = self._extract_options(content)
            kb = self._build_option_keyboard(options)
            if kb:
                await bot.send_message(chat_id, "Please select:", reply_markup=kb)

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle callback queries from inline keyboards"""
        if not await self._check_access(update):
            return

        query = self._require_callback_query(update)
        await query.answer()

        user_id = self._require_user(update).id
        chat = self._require_chat(update)
        app = self._require_application()
        data = query.data
        if data is None:
            return

        if data.startswith("extsend:"):
            parts = data.split(":", 2)
            if len(parts) != 3 or parts[2] not in {"allow", "deny"}:
                await query.edit_message_text("ℹ️ This file request has expired.")
                return

            _, request_token, decision = parts
            session_key = self._conversation_key(user_id, chat.id)
            session = await self._session_manager.get_session(session_key)
            pending = session.get("pending_external_files", [])
            pending_token = session.get("pending_external_files_token")
            if not pending_token or not secrets.compare_digest(
                str(request_token), str(pending_token)
            ):
                await query.edit_message_text("ℹ️ This file request has expired.")
                return

            consumed = await self._session_manager.patch_session_if(
                session_key,
                expected={
                    "pending_external_files": pending,
                    "pending_external_files_token": pending_token,
                },
                remove_fields={
                    "pending_external_files",
                    "pending_external_files_token",
                },
            )
            if not consumed:
                await query.edit_message_text("ℹ️ This file request has expired.")
                return

            if decision == "deny":
                await query.edit_message_text("❌ External file sending cancelled.")
                return

            if not pending:
                await query.edit_message_text("ℹ️ No pending external files.")
                return

            await query.edit_message_text("✅ Confirmed. Sending external files...")
            paths: List[FilePath] = []
            for raw in pending:
                p = FilePath(raw)
                try:
                    resolved = p.resolve(strict=False)
                    if (
                        resolved.is_file()
                        and resolved.stat().st_size < 10 * 1024 * 1024
                    ):
                        paths.append(resolved)
                except Exception:
                    continue
            await self._send_file_paths(chat.id, paths)
            return

        # Handle permission request buttons
        # Handle numbered option buttons (from Claude's text-based choices)
        if data.startswith("opt:"):
            choice = data.split(":", 1)[1]
            await query.edit_message_text(f"✅ Selected: {choice}")
            # Send choice back to Claude as a new message
            chat_id = chat.id
            await self._maybe_capture_outside_approval(user_id, choice, chat_id)
            conversation_key = self._conversation_key(user_id, chat_id)

            async def run_task():
                session, _ = await self._align_active_provider(
                    conversation_key
                )
                await app.bot.send_chat_action(chat_id, action="typing")
                try:
                    response = await self._project_chat.process_message(
                        user_message=choice,
                        user_id=user_id,
                        chat_id=chat_id,
                        session_id=self._effective_session_id(conversation_key, session),
                        model=session.get("model"),
                        permission_callback=self._permission_callback,
                        typing_callback=lambda: app.bot.send_chat_action(
                            chat_id, action="typing"
                        ),
                        status_callback=self._make_status_callback(app.bot, chat_id),
                        bot=app.bot,
                    )
                    await self._save_session_id(conversation_key, response)
                    await self._send_smart(
                        chat_id,
                        response.content,
                        user_id=user_id,
                        force_options=response.has_options,
                        streamed=response.streamed,
                    )
                except Exception as e:
                    logger.error(f"Option reply failed: {e}", exc_info=True)
                    await app.bot.send_message(chat_id, f"❌ Processing failed: {e}")

            async def on_overflow():
                await app.bot.send_message(
                    chat_id,
                    "⏳ Processing previous messages, please wait or send /stop to terminate.",
                )

            conversation_key = self._conversation_key(user_id, chat_id)
            await self._enqueue_user_task(conversation_key, run_task, on_overflow)
            return

        # Handle revert callbacks
        if data.startswith("revert:"):
            await self._handle_revert_callback(update, context, data)
            return

        # Handle model selection
        if data.startswith("model:"):
            parts = data.split(":", 2)
            callback_provider = parts[1] if len(parts) == 3 else "claude"
            model_name = parts[2] if len(parts) == 3 else parts[1]
            log_debug(user_id, "callback", f"model:{model_name}")
            session_key = self._conversation_key(user_id, chat.id)
            active_provider = self._active_provider()
            if callback_provider != active_provider:
                await query.edit_message_text(
                    f"❌ Provider mismatch: selected model is {callback_provider}, "
                    f"but the active provider is {active_provider}."
                )
                return
            stored_provider = await self._session_provider(
                session_key
            )
            updates = {"provider": active_provider, "model": model_name}
            if stored_provider != active_provider:
                updates.update(session_id=None, new_session=True)
            await self._session_manager.patch_session(
                session_key,
                updates=updates,
            )
            label = (
                dict(self.MODELS).get(model_name, model_name)
                if active_provider == "claude"
                else model_name
            )
            logger.info(
                "User %s: model set to %r via callback in chat %s",
                user_id,
                model_name,
                chat.id,
            )
            reply = f"✅ Model switched to: {label}"
            await query.edit_message_text(reply)
            log_debug(user_id, "bot", reply)
            return

        # Check if there's a pending question
        pending = await self._session_manager.get_pending_question(user_id)
        if pending:
            await self._session_manager.clear_pending_question(user_id)
            await query.edit_message_text(f"✅ Selected: {data}\n\nContinuing...")

    async def _set_bot_commands(self):
        """Set bot commands menu"""
        commands = [
            BotCommand("new", "New session"),
            BotCommand("stop", "Stop execution"),
            BotCommand("model", "Switch model"),
            BotCommand("resume", "Resume session"),
            BotCommand("history", "View message history"),
            BotCommand("revert", "Revert conversation"),
            BotCommand("skills", "List skills"),
            BotCommand("skill", "Run skill"),
            BotCommand("command", "Run command"),
        ]
        for scope in (
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
        ):
            app = self._require_application()
            await app.bot.set_my_commands(commands, scope=scope)
        logger.info("Bot commands set")

