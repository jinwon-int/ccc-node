"""Inbound callback-query routing mixin for TelegramBot.

Extracted from bot_delivery.py (#584 P2-2): outbound delivery and inbound
callback routing are different responsibilities. This mixin owns the
`perm:`-family inline-button dispatcher (`ca:` Codex approvals, `extsend:`
external-file confirmations, `opt:` numbered options, `revert:`, `effort:`,
`model:`, and pending-question fallthrough). All state lives on TelegramBot;
the mixin contract is exercised by the composition tests.
"""

# ruff: noqa: E402
# mypy: disable-error-code="attr-defined"
import logging
import secrets
from pathlib import Path as FilePath
from typing import List

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.core.bot_delivery import MAX_SEND_FILE_BYTES
from telegram_bot.memory.distill_types import DistillTrigger
from telegram_bot.utils.chat_logger import log_debug

logger = logging.getLogger(__name__)


class BotCallbackMixin:
    async def _handle_callback(  # noqa: C901 -- #348 baseline hotspot
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

        if data.startswith("ca:"):
            await self._resolve_codex_approval(user_id, chat.id, data)
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
                        and resolved.stat().st_size < MAX_SEND_FILE_BYTES
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
                session, _ = await self._switch_provider_if_needed(
                    conversation_key, user_id, chat_id
                )
                await app.bot.send_chat_action(chat_id, action="typing")
                try:
                    response = await self._project_chat.process_message(
                        user_message=choice,
                        user_id=user_id,
                        chat_id=chat_id,
                        session_id=self._effective_session_id(conversation_key, session),
                        model=session.get("model"),
                        effort=session.get("effort"),
                        approval_policy=self._codex_approval_policy(),
                        approvals_reviewer=self._codex_approvals_reviewer(),
                        sandbox_policy=self._codex_sandbox_policy(),
                        permission_callback=self._permission_callback,
                        approval_callback=self._codex_approval_callback,
                        typing_callback=lambda: app.bot.send_chat_action(
                            chat_id, action="typing"
                        ),
                        status_callback=self._make_status_callback(app.bot, chat_id),
                        bot=app.bot,
                        interim_message_callback=self._make_interim_send_callback(
                            chat_id
                        ),
                    )
                    await self._save_session_id(
                        conversation_key,
                        response,
                        user_id=user_id,
                        chat_id=chat_id,
                    )
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

        # Handle effort selection
        if data.startswith("effort:"):
            parts = data.split(":", 2)
            callback_provider = parts[1] if len(parts) == 3 else ""
            requested = parts[2] if len(parts) == 3 else ""
            active_provider = self._active_provider()
            if callback_provider != active_provider or active_provider != "codex":
                await query.edit_message_text(
                    f"❌ Provider mismatch: selected effort is {callback_provider or 'unknown'}, "
                    f"but the active provider is {active_provider}."
                )
                return
            session_key = self._conversation_key(user_id, chat.id)
            session, provider_switched = await self._switch_provider_if_needed(
                session_key, user_id, chat.id
            )
            try:
                models = tuple(await self._project_chat.list_runtime_models())
            except Exception:
                logger.warning("Codex effort callback browsing failed", exc_info=True)
                await query.edit_message_text("⚠️ Codex effort options are unavailable.")
                return
            model = self._selected_codex_model(models, session)
            if model is None or not model.supported_reasoning_efforts:
                await query.edit_message_text(
                    "📭 The selected Codex model does not advertise reasoning effort options."
                )
                return
            reply = await self._apply_codex_effort_selection(
                session_key, model, requested
            )
            await query.edit_message_text(reply)
            log_debug(user_id, "bot", reply)
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
            session = await self._session_manager.get_session(session_key)
            updates = {"provider": active_provider, "model": model_name}
            remove_fields = set()
            reset_note = None
            if stored_provider != active_provider:
                await self._enqueue_previous_codex_session(
                    session,
                    DistillTrigger.PROVIDER_SWITCH,
                    user_id=user_id,
                    chat_id=chat.id,
                )
                updates.update(session_id=None, new_session=True)
                remove_fields.add("effort")
            elif active_provider == "codex":
                reset_note = await self._codex_model_effort_reset_note(session, model_name)
                if reset_note:
                    remove_fields.add("effort")
            await self._session_manager.patch_session(
                session_key,
                updates=updates,
                remove_fields=remove_fields,
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
            if reset_note:
                reply = f"{reply}\n{reset_note}"
            await query.edit_message_text(reply)
            log_debug(user_id, "bot", reply)
            return

        # Check if there's a pending question
        pending = await self._session_manager.get_pending_question(user_id)
        if pending:
            await self._session_manager.clear_pending_question(user_id)
            await query.edit_message_text(f"✅ Selected: {data}\n\nContinuing...")
