# ruff: noqa: E402
import asyncio
import hashlib
import json
import logging
import re
from typing import Awaitable, Callable, Optional
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    CommandHandler,
    ContextTypes,
)
from telegram_bot.core import revert as revert_ops
from telegram_bot.core import restart_handoff
from telegram_bot.core.usage import UsageSnapshot, render_usage
from telegram_bot.memory.distill_types import DistillTrigger
from .conversation_paths import resolve_conversation_file
from telegram_bot.utils.chat_logger import log_debug
from telegram_bot.utils import tg_errors

logger = logging.getLogger(__name__)
STALE_MESSAGE_SECONDS = 20 * 60  # 20 minutes


from telegram_bot.core.bot_shared import (
    _esc_md2,
)

class BotCommandMixin:
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return

        user = self._require_user(update)
        message = self._require_message(update)
        log_debug(user.id, "command", "/start")
        welcome_text = f"👋 Hello, {user.first_name}! Send a message to start chatting, or use /skills to view available skills."
        await message.reply_text(welcome_text)
        log_debug(user.id, "bot", welcome_text)

    async def _cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Schedule an owner-only restart outside the bridge's systemd cgroup."""
        if not await self._check_access(update):
            return
        user = self._require_user(update)
        message = self._require_message(update)
        chat = self._require_chat(update)
        log_debug(user.id, "command", "/restart")

        allowed = list(dict.fromkeys(getattr(self._config, "allowed_user_ids", [])))
        if (
            getattr(self._config, "restart_handoff", "off") != "systemd"
            or len(allowed) != 1
            or allowed[0] != user.id
            or getattr(chat, "type", None) != "private"
        ):
            reply = (
                "⛔ Safe restart is unavailable. It requires systemd opt-in and "
                "a private chat with the sole allowlisted owner."
            )
            await message.reply_text(reply)
            log_debug(user.id, "bot", reply)
            return

        try:
            scheduled = await asyncio.to_thread(
                restart_handoff.schedule_restart,
                data_dir=self._config.bot_data_dir,
                chat_id=chat.id,
                unit=self._config.restart_service_unit,
                delay_seconds=self._config.restart_delay_seconds,
            )
        except restart_handoff.RestartHandoffError as exc:
            reply = f"❌ Restart was not scheduled ({exc.code}). The bridge is still running."
            await message.reply_text(reply)
            log_debug(user.id, "bot", reply)
            return

        reply = (
            f"♻️ Restart scheduled ({scheduled.request_id[:8]}). "
            "I will report when the replacement bridge is healthy."
        )
        await message.reply_text(reply)
        log_debug(user.id, "bot", reply)

    async def _cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Render local/read-only provider usage without starting an agent turn."""

        if not await self._check_access(update):
            return
        user_id = self._require_user(update).id
        message = self._require_message(update)
        chat = self._require_chat(update)
        provider = self._active_provider()
        conversation_key = self._conversation_key(user_id, chat.id)
        log_debug(user_id, "command", "/usage")
        session = await self._session_manager.get_session(conversation_key)
        session_id = session.get("session_id") if session.get("provider") == provider else None
        if not isinstance(session_id, str) or not session_id:
            session_id = None
        try:
            snapshot = await self._project_chat.get_usage(
                user_id, chat.id, session_id
            )
        except Exception:
            logger.warning("Provider usage read failed for %s", provider)
            snapshot = UsageSnapshot(provider=provider)
        reply = render_usage(snapshot)
        usage_meter = getattr(self._project_chat, "usage_meter", None)
        if usage_meter is not None:
            try:
                reply = f"{reply}\n\n{usage_meter.render_report(days=7)}"
            except Exception:
                logger.warning("Local usage meter report failed")
        await message.reply_text(reply)
        log_debug(user_id, "bot", reply)

    async def _cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return

        user_id = self._require_user(update).id
        message = self._require_message(update)
        chat = self._require_chat(update)
        log_debug(user_id, "command", "/skills")

        await message.chat.send_action(action="typing")

        prompt = (
            "List all installed skills, grouped by global and project.\n"
            "Output format requirements (strictly follow):\n"
            "- Use Telegram HTML format, group titles in <b>Title</b> bold\n"
            "- One skill per line, format: /skill_name description\n"
            "- Do NOT use Markdown syntax (no ## or **)\n"
            "- Do NOT output any extra introductory text or status lines"
        )
        response = await self._project_chat.process_message(
            user_message=prompt,
            user_id=user_id,
            chat_id=chat.id,
            new_session=True,
            permission_callback=self._permission_callback,
            approval_policy=self._codex_approval_policy(),
            approvals_reviewer=self._codex_approvals_reviewer(),
            sandbox_policy=self._codex_sandbox_policy(),
            approval_callback=self._codex_approval_callback,
            typing_callback=lambda: message.chat.send_action(action="typing"),
            status_callback=self._make_status_callback(context.bot, chat.id),
            notification_bot=context.bot,
            interim_message_callback=self._make_interim_reply_callback(message),
        )
        await self._save_session_id(
            self._conversation_key(user_id, chat.id),
            response,
            user_id=user_id,
            chat_id=chat.id,
            request_text=prompt,
            turn_marker=f"telegram-message:{message.message_id}",
        )
        # PATCH 2026-05-04: use _reply_smart to auto-split >4096 char responses
        # (Telegram message size limit). Capo's 23TM project has 31+ skills,
        # full /skills listing exceeds limit → "Message is too long" error.
        await self._reply_smart(message, response.content, parse_mode="HTML", user_id=user_id)
        log_debug(user_id, "bot", response.content)

    async def _cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return
        user_id = self._require_user(update).id
        message = self._require_message(update)
        chat = self._require_chat(update)
        conversation_key = self._conversation_key(user_id, chat.id)
        log_debug(user_id, "command", "/new")

        self._deny_codex_approvals(user_id, chat.id)
        self._invalidate_codex_approvals(user_id, chat.id)

        cancelled_voice = await self._cancel_user_voice_tasks(conversation_key)
        if cancelled_voice:
            logger.info(
                "Cancelled %s active voice task(s) for user %s on /new",
                cancelled_voice,
                user_id,
            )

        # Cancel any ongoing streaming in this Telegram conversation
        await self._cancel_user_streaming(user_id, chat.id)
        tasks = getattr(self, "_tasks", None)
        active_task = (
            tasks.active(conversation_key) or tasks.active(user_id)
            if tasks is not None
            else None
        )
        if active_task and not active_task.done():
            active_task.cancel()

        session = await self._session_manager.get_session(conversation_key)
        await self._enqueue_previous_codex_session(
            session,
            DistillTrigger.NEW_COMMAND,
            user_id=user_id,
            chat_id=chat.id,
        )
        active_provider = self._active_provider()
        provider_changed = session.get("provider") != active_provider
        updates = {
            "provider": active_provider,
            "session_id": None,
            "new_session": True,
        }
        session["session_id"] = None
        session["new_session"] = True

        # Claude keeps settings.json synchronization. Codex must not read it.
        settings_model = session.get("model")
        if active_provider == "claude":
            try:
                with open(self._config.claude_settings_path, "r") as f:
                    settings_model = json.load(f).get("model")
            except Exception:
                settings_model = None
        elif session.get("provider") != active_provider:
            settings_model = None
            updates["model"] = None

        if active_provider == "claude" and session.get("model") != settings_model:
            old_model = session.get("model")
            session["model"] = settings_model
            updates["model"] = settings_model
            effective = self._get_real_model(session)
            logger.info(
                f"User {user_id}: model synced {old_model!r} -> {settings_model!r} (effective: {effective!r}) on /new"
            )
            log_debug(
                user_id,
                "model",
                f"Auto-synced model: {old_model} -> {settings_model} (effective: {effective})",
            )

        await self._session_manager.patch_session(
            conversation_key,
            updates=updates,
            remove_fields={"effort"} if provider_changed else (),
        )
        self._runtime_active_sessions.discard(conversation_key)
        provider_label = "Claude Code" if active_provider == "claude" else "Codex"
        reply = f"🆕 Switched to new session mode. Your next message will start a new {provider_label} session."
        await message.reply_text(reply)
        log_debug(user_id, "bot", reply)

    @staticmethod
    def _explicit_distill_discriminator(session: dict) -> str:
        marker = session.get("last_user_message_at")
        if not isinstance(marker, str) or not marker:
            marker = "missing-turn-marker"
        digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()[:32]
        return f"explicit-turn-v1-{digest}"

    async def _cmd_distill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Durably request write-back for the current Codex thread without reset."""

        del context
        if not await self._check_access(update):
            return
        user_id = self._require_user(update).id
        message = self._require_message(update)
        chat = self._require_chat(update)
        log_debug(user_id, "command", "/distill")

        if self._active_provider() != "codex":
            reply = "ℹ️ /distill is available only for active Codex sessions."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        conversation_key = self._conversation_key(user_id, chat.id)
        tasks = getattr(self, "_tasks", None)
        active_task = (
            tasks.active(conversation_key) or tasks.active(user_id)
            if tasks is not None
            else None
        )
        if active_task is not None and not active_task.done():
            reply = (
                "⏳ The current Codex turn is still running. "
                "Run /distill again after it finishes."
            )
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return
        session = await self._session_manager.get_session(conversation_key)
        thread_id = session.get("session_id")
        if (
            str(session.get("provider", "")).strip().lower() != "codex"
            or not isinstance(thread_id, str)
            or not thread_id
        ):
            reply = "ℹ️ There is no active Codex session to distill."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        try:
            job = await self._enqueue_previous_codex_session(
                session,
                DistillTrigger.EXPLICIT,
                user_id=user_id,
                chat_id=chat.id,
                discriminator=self._explicit_distill_discriminator(session),
            )
        except Exception:
            logger.warning("Explicit Codex distill request could not be recorded")
            reply = "⚠️ Codex memory distill request could not be recorded."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        if job is None:
            reply = "⚠️ Codex memory distill is unavailable on this bridge."
        else:
            reply = (
                "✅ Codex memory distill request recorded. "
                "The current session remains active."
            )
        await message.reply_text(reply)
        log_debug(user_id, "bot", reply)

    async def _cmd_memory_promote(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Promote one validated private local fact after an explicit DM command."""

        if not await self._check_access(update):
            return
        message = self._require_message(update)
        if (
            getattr(self._config, "bridge_memory_mode", "off") != "audience-scoped"
            or getattr(self, "_memory_promoter", None) is None
            or getattr(self, "_distill_local_sink_worker", None) is None
        ):
            await message.reply_text(
                "ℹ️ Explicit memory promotion is unavailable on this bridge."
            )
            return

        user_id = self._require_user(update).id
        chat_id = self._require_chat(update).id
        from telegram_bot.core.memory_audience import resolve_memory_audience

        audience = resolve_memory_audience(
            self._config,
            user_id=user_id,
            chat_id=chat_id,
        )
        if audience is None or audience.kind != "private":
            await message.reply_text(
                "❌ Memory promotion is allowed only from your private DM."
            )
            return
        args = context.args or []
        if (
            len(args) != 1
            or re.fullmatch(r"distill-[0-9a-f]{12}", args[0]) is None
        ):
            await message.reply_text(
                "Usage: /memory_promote distill-<12 lowercase hex>"
            )
            return

        fact_id = args[0]
        try:
            result = await asyncio.to_thread(
                self._memory_promoter.promote,
                source_scope=audience.scope,
                fact_id=fact_id,
            )
            await self._distill_local_sink_worker.refresh_route(
                audience="shared",
                scope="shared",
            )
        except LookupError:
            await message.reply_text(
                "ℹ️ That fact was not found in your private memory."
            )
            return
        except ValueError:
            logger.warning("Private memory promotion rejected by validation")
            await message.reply_text(
                "⚠️ That private fact is not eligible for promotion."
            )
            return
        except Exception:
            logger.warning("Private memory promotion or shared index refresh failed")
            await message.reply_text(
                "⚠️ Memory promotion could not be completed. You can retry safely."
            )
            return

        if result.promoted:
            reply = (
                f"✅ Promoted {fact_id} to shared memory as "
                f"{result.destination_fact_id}."
            )
        else:
            reply = (
                f"✅ {fact_id} was already promoted as "
                f"{result.destination_fact_id}; shared memory was refreshed."
            )
        await message.reply_text(reply)

    def _get_real_model(self, session: dict) -> str:
        """Get current model from session or ~/.claude/settings.json"""
        if model := session.get("model"):
            return model
        try:
            with open(self._config.claude_settings_path, "r") as f:
                return json.load(f).get("model", "sonnet")
        except Exception:
            return "sonnet"

    async def _cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return
        user_id = self._require_user(update).id
        message = self._require_message(update)
        chat = self._require_chat(update)
        conversation_key = self._conversation_key(user_id, chat.id)
        log_debug(user_id, "command", "/model")
        session = await self._session_manager.get_session(conversation_key)
        active_provider = self._active_provider()

        if context.args:
            name = context.args[0]
            updates = {"provider": active_provider, "model": name}
            remove_fields = set()
            reset_note = None
            if session["provider"] != active_provider:
                await self._enqueue_previous_codex_session(
                    session,
                    DistillTrigger.PROVIDER_SWITCH,
                    user_id=user_id,
                    chat_id=chat.id,
                )
                updates.update(session_id=None, new_session=True)
                remove_fields.add("effort")
            elif active_provider == "codex":
                reset_note = await self._codex_model_effort_reset_note(session, name)
                if reset_note:
                    remove_fields.add("effort")
            await self._session_manager.patch_session(
                conversation_key,
                updates=updates,
                remove_fields=remove_fields,
            )
            label = dict(self.MODELS).get(name, name) if active_provider == "claude" else name
            logger.info(f"User {user_id}: model set to {name!r} via /model command")
            reply = f"✅ Switched to {label}"
            if reset_note:
                reply = f"{reply}\n{reset_note}"
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        if active_provider == "codex":
            try:
                models = await self._project_chat.list_runtime_models()
            except Exception:
                logger.warning("Codex model browsing failed")
                reply = "⚠️ Codex model list is unavailable. Use /model <codex-model>."
                await message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                return
            buttons = [
                [InlineKeyboardButton(
                    model.display_name[:64],
                    callback_data=f"model:codex:{model.id}",
                )]
                for model in models
                if len(f"model:codex:{model.id}".encode("utf-8")) <= 64
            ]
            if not buttons:
                reply = "📭 No Codex models are available. Use /model <codex-model>."
                await message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                return
            reply = "🤖 Select Codex model:"
            await message.reply_text(reply, reply_markup=InlineKeyboardMarkup(buttons))
            log_debug(user_id, "bot", reply)
            return

        current_model = self._get_real_model(session)
        models = list(self.MODELS)
        if current_model not in dict(models):
            models.append((current_model, current_model))
        buttons = [
            [
                InlineKeyboardButton(
                    f"{label} (current)" if name == current_model else label,
                    callback_data=f"model:{name}",
                )
            ]
            for name, label in models
        ]
        reply = "🤖 Select Claude Code model:"
        await message.reply_text(reply, reply_markup=InlineKeyboardMarkup(buttons))
        log_debug(user_id, "bot", reply)

    @staticmethod
    def _selected_codex_model(models, session: dict):
        selected_id = session.get("model")
        if selected_id:
            return next((model for model in models if model.id == selected_id), None)
        return next((model for model in models if model.is_default), models[0] if models else None)

    async def _codex_model_effort_reset_note(self, session: dict, model_id: str):
        current_effort = session.get("effort")
        if not current_effort:
            return None
        try:
            models = tuple(await self._project_chat.list_runtime_models())
        except Exception:
            logger.warning("Codex model effort compatibility lookup failed", exc_info=True)
            return (
                f"ℹ️ Reasoning effort {current_effort} could not be validated; "
                "reset to model default."
            )
        model = next((item for item in models if item.id == model_id), None)
        if model is not None and current_effort in model.supported_reasoning_efforts:
            return None
        default_label = (
            model.default_reasoning_effort
            if model is not None and model.default_reasoning_effort
            else "provider default"
        )
        return (
            f"ℹ️ Reasoning effort {current_effort} is unsupported; "
            f"reset to model default ({default_label})."
        )

    async def _apply_codex_effort_selection(
        self, conversation_key, model, requested: str
    ) -> str:
        if requested == "default":
            await self._session_manager.patch_session(
                conversation_key,
                updates={"provider": "codex"},
                remove_fields={"effort"},
            )
            default_label = model.default_reasoning_effort or "provider default"
            return (
                f"✅ Reasoning effort reset to model default ({default_label}) "
                f"for {model.display_name}"
            )
        if requested not in model.supported_reasoning_efforts:
            supported = ", ".join(model.supported_reasoning_efforts)
            return (
                f"❌ Unsupported effort for {model.display_name}: {requested}. "
                f"Supported: {supported}, default"
            )
        await self._session_manager.patch_session(
            conversation_key,
            updates={"provider": "codex", "effort": requested},
        )
        return f"✅ Reasoning effort set to {requested} for {model.display_name}"

    async def _cmd_effort(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return
        user_id = self._require_user(update).id
        message = self._require_message(update)
        chat = self._require_chat(update)
        log_debug(user_id, "command", "/effort")
        if self._active_provider() != "codex":
            reply = "⚠️ /effort is available only when the Codex provider is active."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        conversation_key = self._conversation_key(user_id, chat.id)
        session, provider_switched = await self._switch_provider_if_needed(
            conversation_key, user_id, chat.id
        )
        try:
            models = tuple(await self._project_chat.list_runtime_models())
        except Exception:
            logger.warning("Codex effort browsing failed", exc_info=True)
            reply = "⚠️ Codex effort options are unavailable."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return
        model = self._selected_codex_model(models, session)
        requested = context.args[0] if context.args else None
        if model is None:
            if requested == "default":
                await self._session_manager.patch_session(
                    conversation_key,
                    updates={"provider": "codex"},
                    remove_fields={"effort"},
                )
                reply = "✅ Reasoning effort reset to provider default"
            else:
                reply = (
                    "📭 The selected Codex model does not advertise reasoning "
                    "effort options."
                )
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return
        if not model.supported_reasoning_efforts and requested != "default":
            reply = "📭 The selected Codex model does not advertise reasoning effort options."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        if requested is not None:
            reply = await self._apply_codex_effort_selection(
                conversation_key, model, requested
            )
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        current = session.get("effort")
        buttons = []
        for effort in model.supported_reasoning_efforts:
            callback_data = f"effort:codex:{effort}"
            if len(callback_data.encode("utf-8")) > 64:
                continue
            suffixes = []
            if effort == model.default_reasoning_effort:
                suffixes.append("model default")
            if effort == current:
                suffixes.append("current")
            label = f"{effort} ({', '.join(suffixes)})" if suffixes else effort
            buttons.append([
                InlineKeyboardButton(label, callback_data=callback_data)
            ])
        buttons.append([
            InlineKeyboardButton("Use model default", callback_data="effort:codex:default")
        ])
        current_label = current or "model default"
        default_label = model.default_reasoning_effort or "provider default"
        reply = (
            f"🧠 Select reasoning effort for {model.display_name}:\n"
            f"Current: {current_label} · Model default: {default_label}"
        )
        await message.reply_text(reply, reply_markup=InlineKeyboardMarkup(buttons))
        log_debug(user_id, "bot", reply)

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return
        user_id = self._require_user(update).id
        message = self._require_message(update)
        chat = self._require_chat(update)
        conversation_key = self._conversation_key(user_id, chat.id)
        log_debug(user_id, "command", "/resume")
        active_provider = self._active_provider()
        if (
            active_provider == "claude"
            and getattr(self._config, "bridge_memory_mode", "off")
            == "audience-scoped"
        ):
            # Claude transcript discovery is one global filesystem view. Until
            # the SDK exposes an audience-scoped browser, listing it could put
            # a DM preview into a group or another user's DM. Existing session
            # ids remain usable only through their route-bound session record.
            await self._session_manager.patch_session(
                conversation_key,
                remove_fields={"resume_list"},
            )
            reply = (
                "🔒 Claude session browsing is disabled while private memory "
                "is audience-scoped. Use /new to start a fresh session."
            )
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return
        stored_provider = await self._session_provider(
            conversation_key
        )
        if stored_provider != active_provider:
            reply = (
                f"❌ Provider mismatch: this session is {stored_provider}, "
                f"but the active provider is {active_provider}. Use /new first."
            )
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return
        if active_provider == "codex":
            try:
                codex_sessions = await self._project_chat.list_runtime_sessions(limit=10)
            except Exception:
                logger.warning("Codex session browsing failed")
                reply = "⚠️ Codex session history is unavailable."
                await message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                return
            if not codex_sessions:
                reply = "📭 No Codex session history found."
                await message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                return
            resume_list = []
            lines = ["📋 Session History\n"]
            for index, item in enumerate(codex_sessions, 1):
                label = item.title or item.preview or item.id
                label = re.sub(r"https?://\S+", "", label)
                label = " ".join(label.split())[:120] or item.id
                resume_list.append([item.id, label, "codex"])
                details = " · ".join(
                    " ".join(value.split())[:80]
                    for value in (item.model, item.cwd)
                    if value
                )
                provider_tag = f"codex · {details}" if details else "codex"
                lines.append(f"{index}. {label} [{provider_tag}]")
            lines.append("\nReply with a number to switch to that session:")
            await self._session_manager.patch_session(
                conversation_key,
                updates={"resume_list": resume_list},
            )
            reply = "\n".join(lines)
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return
        sessions = self._project_chat.list_sessions(limit=10)
        if not sessions:
            reply = "📭 No session history found."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        # Store session list for later selection in this Telegram conversation.
        await self._session_manager.patch_session(
            conversation_key,
            updates={
                "resume_list": [[sid, msg, "claude"] for sid, msg, _ in sessions]
            },
        )

        def _esc_resume_text(text: str) -> str:
            text = re.sub(r"https?://\S+", "", text).strip()
            return _esc_md2(text)

        def relative_time(mtime: float) -> str:
            delta = int(self._clock.time() - mtime)
            if delta < 60:
                return f"{delta} seconds ago"
            if delta < 3600:
                return f"{delta // 60} minutes ago"
            if delta < 86400:
                return f"{delta // 3600} hours ago"
            return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

        NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        lines = ["📋 *Session History*\n"]
        for i, (sid, msg, mtime) in enumerate(sessions, 1):
            ts = relative_time(mtime)
            esc = _esc_resume_text(msg.replace("\n", " "))
            if i > 1:
                lines.append("")
            num = NUM_EMOJI[i - 1] if i <= len(NUM_EMOJI) else f"*{i}\\.*"
            lines.append(f"{num} {esc} \\[claude\\]")
            lines.append(_esc_resume_text(ts))
        lines.append(f"\n{_esc_md2('Reply with a number to switch to that session:')}")
        reply = "\n".join(lines)
        await message.reply_text(reply, parse_mode="MarkdownV2")
        log_debug(user_id, "bot", reply)

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop - interrupt active execution and clear queue.

        This command has priority handling - it bypasses queue limits and
        immediately cancels any running task for the user.
        """
        if not await self._check_access(update):
            return
        user_id = self._require_user(update).id
        message = self._require_message(update)
        chat = self._require_chat(update)
        conversation_key = self._conversation_key(user_id, chat.id)
        log_debug(user_id, "command", "/stop")

        self._deny_codex_approvals(user_id, chat.id)
        self._invalidate_codex_approvals(user_id, chat.id)

        cancelled_voice = await self._cancel_user_voice_tasks(conversation_key)
        if cancelled_voice:
            logger.info(
                "Cancelled %s active voice task(s) for user %s on /stop",
                cancelled_voice,
                user_id,
            )

        # Cancel any ongoing streaming in this Telegram conversation
        await self._cancel_user_streaming(user_id, chat.id)

        # Cancel the currently executing task (priority stop)
        active_task = self._tasks.active(conversation_key) or self._tasks.active(user_id)
        task_cancelled = False
        if active_task and not active_task.done():
            active_task.cancel()
            task_cancelled = True
            logger.info(
                "Cancelled active task for user %s via priority /stop command",
                user_id,
            )

        try:
            killed = await self._project_chat.stop(user_id, chat_id=chat.id)
        except TypeError:
            # Some tests/older adapters expose stop(user_id) only.
            killed = await self._project_chat.stop(user_id)
        cleared = self._clear_user_queue(conversation_key)

        # Build response message - simple and friendly
        if task_cancelled or killed or cleared:
            reply = "⏸️ Paused"
        else:
            reply = "ℹ️ Nothing running"
        await message.reply_text(reply)
        log_debug(user_id, "bot", reply)

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /history - display recent messages from current session."""
        if not await self._check_access(update):
            return
        user_id = self._require_user(update).id
        message = self._require_message(update)
        chat = self._require_chat(update)
        log_debug(user_id, "command", "/history")

        conversation_key = self._conversation_key(user_id, chat.id)
        session = await self._session_manager.get_session(conversation_key)
        session_id = session.get("session_id")

        if not session_id:
            reply = "📭 No active session. Start a conversation first."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        if session["provider"] == "codex":
            try:
                history = await self._project_chat.read_runtime_session(session_id, limit=5)
            except Exception:
                logger.warning("Codex history browsing failed")
                reply = "⚠️ Codex history is unavailable for this session."
                await message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                return
            messages = [
                {
                    "role": item.role,
                    "content": item.content,
                    "timestamp": item.timestamp or "",
                }
                for item in history.messages
            ]
        else:
            messages = self._project_chat.get_recent_messages(session_id, limit=5)

        if not messages:
            reply = "📭 No history available for this session."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        lines = [
            "📜 Recent History (last 5 messages)",
            f"Provider: {session['provider']}\n",
        ]
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            timestamp = msg["timestamp"]

            # Format emoji indicator
            emoji = "🧑" if role == "user" else "🤖"
            role_label = "User" if role == "user" else "Assistant"

            # Format timestamp
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts_str = timestamp[:19] if len(timestamp) >= 19 else timestamp

            # Truncate content
            if len(content) > 500:
                content = content[:500] + "..."

            lines.append(f"{emoji} {role_label} [{ts_str}]")
            lines.append(content)
            lines.append("")

        reply = "\n".join(lines).strip()

        # Ensure total length under 4000 chars
        if len(reply) > 4000:
            reply = reply[:3997] + "..."

        await message.reply_text(reply)
        log_debug(user_id, "bot", reply)

    async def _cmd_revert(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /revert - revert conversation to a previous message."""
        if not await self._check_access(update):
            return
        user_id = self._require_user(update).id
        message = self._require_message(update)
        log_debug(user_id, "command", "/revert")

        session = await self._session_manager.get_session(user_id)
        session_id = session.get("session_id")

        if not session_id:
            reply = "📭 No active session. Start a conversation first."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        # Get conversation history — offloaded so reading a large transcript for
        # /revert browsing never stalls the event loop (#456).
        messages = await asyncio.to_thread(
            self._project_chat.get_conversation_history, session_id, limit=50
        )

        if not messages:
            reply = "📭 No conversation history available to revert."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        # Display message selection UI
        keyboard = self._build_history_keyboard(messages, page=0)
        reply = "🔄 Select a message to revert to:"
        await message.reply_text(reply, reply_markup=keyboard)
        log_debug(user_id, "bot", reply)

    async def _handle_revert_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ):
        """Handle revert-related callback queries."""
        query = self._require_callback_query(update)
        user_id = self._require_user(update).id
        chat_id = update.effective_chat.id if update.effective_chat else None

        # Parse callback data
        parts = data.split(":")
        if len(parts) < 3:
            await query.edit_message_text("❌ Invalid callback data")
            return

        action = parts[1]  # "select", "page", or "mode"

        session = await self._session_manager.get_session(user_id)
        session_id = session.get("session_id")

        if not session_id:
            await query.edit_message_text("❌ No active session")
            return

        if action == "page":
            # Handle pagination
            page = int(parts[2])
            messages = await asyncio.to_thread(
                self._project_chat.get_conversation_history, session_id, limit=50
            )
            keyboard = self._build_history_keyboard(messages, page=page)
            await query.edit_message_reply_markup(reply_markup=keyboard)

        elif action == "select":
            # Handle message selection - show revert mode options
            msg_index = int(parts[2])
            keyboard = self._build_revert_mode_keyboard(msg_index)

            # Get selected message details for context
            messages = await asyncio.to_thread(
                self._project_chat.get_conversation_history, session_id, limit=50
            )
            selected_msg = next((m for m in messages if m["index"] == msg_index), None)

            if selected_msg:
                content_preview = selected_msg.get("content", "")[:200]

                reply = (
                    f"🔄 Selected message:\n\n"
                    f"{content_preview}...\n\n"
                    f"Choose revert mode:"
                )
            else:
                reply = "🔄 Choose revert mode:"

            await query.edit_message_text(reply, reply_markup=keyboard)

        elif action == "mode":
            # Handle mode selection - execute revert
            msg_index = int(parts[2])
            mode = parts[3]

            if mode == "cancel":
                await query.edit_message_text("❌ Revert cancelled")
                return

            # Execute revert operation
            await query.edit_message_text("⏳ Reverting to selected message...")

            # Get selected message info BEFORE revert (since it will be deleted)
            messages = await asyncio.to_thread(
                self._project_chat.get_conversation_history, session_id, limit=50
            )
            selected_msg = next((m for m in messages if m["index"] == msg_index), None)

            timestamp_str = ""
            content_preview = ""
            if selected_msg:
                timestamp = selected_msg.get("timestamp", "")
                try:
                    from datetime import datetime

                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    timestamp_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    timestamp_str = (
                        timestamp[:19] if len(timestamp) >= 19 else timestamp
                    )

                # Get content preview
                content = selected_msg.get("content", "")
                content_preview = content[:80] + "..." if len(content) > 80 else content
                content_preview = content_preview.replace("\n", " ")

            try:
                success = await self._execute_revert(
                    user_id, session_id, msg_index, mode, chat_id=chat_id
                )

                if success:
                    if timestamp_str and content_preview:
                        await query.edit_message_text(
                            f"✅ Reverted to before:\n\n"
                            f"[{timestamp_str}]\n"
                            f"{content_preview}\n\n"
                            f"Conversation state restored."
                        )
                    elif timestamp_str:
                        await query.edit_message_text(
                            f"✅ Reverted to before [{timestamp_str}]. Conversation state restored."
                        )
                    else:
                        await query.edit_message_text(
                            "✅ Revert completed successfully."
                        )
                else:
                    await query.edit_message_text("❌ Revert operation failed")

            except Exception as e:
                logger.error(f"Revert operation failed: {e}", exc_info=True)
                await query.edit_message_text(f"❌ Revert failed: {e}")

    async def _execute_revert(
        self, user_id: int, session_id: str, msg_index: int, mode: str,
        chat_id: Optional[int] = None,
    ) -> bool:
        """Execute revert operation based on selected mode.

        Args:
            user_id: Telegram user ID
            session_id: Current session ID
            msg_index: Index of message to revert to in JSONL file
            mode: Revert mode (full, conv, code, summary)
            chat_id: Telegram chat ID, so the right per-conversation task/stream
                is cancelled in group chats (queue keys are conversation-scoped)

        Returns:
            True if revert succeeded, False otherwise
        """
        try:
            # Cancel any active operations first
            await self._cancel_active_operations(user_id, chat_id)

            if mode == "summary":
                # Summarize mode: inject summary request message
                return await self._execute_summarize_mode(
                    user_id, session_id, msg_index
                )
            else:
                # Revert modes: truncate conversation and/or code
                # Note: Code revert (mode="code" or mode="full") currently only reverts
                # conversation state. Full code state restoration would require SDK-level
                # file tracking, which is not yet implemented. The conversation revert
                # ensures the SDK will regenerate code from the restored conversation state.
                success = await self._execute_conversation_revert(
                    user_id, session_id, msg_index, mode
                )
                if success:
                    # Clear runtime state after revert
                    await self._clear_user_state(user_id)
                return success

        except Exception as e:
            logger.error(f"Execute revert failed: {e}", exc_info=True)
            return False

    async def _cancel_active_operations(self, user_id: int, chat_id: Optional[int] = None) -> None:
        """Cancel active streaming and voice tasks before revert.

        The run queue is keyed per conversation (``user_id:chat_id`` in groups),
        so cancellation must use the conversation key — falling back to the bare
        ``user_id`` key for DMs / legacy entries — or a group chat's in-flight
        task is missed and keeps running while the conversation is truncated.
        """
        conversation_key = self._conversation_key(user_id, chat_id)

        # Cancel streaming (scoped to this conversation)
        await self._cancel_user_streaming(user_id, chat_id)

        # Cancel active task
        active_task = self._tasks.active(conversation_key) or self._tasks.active(user_id)
        if active_task and not active_task.done():
            active_task.cancel()
            try:
                await active_task
            except asyncio.CancelledError:
                pass

        # Cancel voice transcription
        voice_tasks = self._user_voice_tasks.get(conversation_key, set())
        for task in list(voice_tasks):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _clear_user_state(self, user_id: int) -> None:
        """Clear runtime state after revert operation."""
        # Clear active stream (cancels pending futures + disconnects the SDK client)
        await self._project_chat.clear_user_stream(user_id)

        # Clear pending permission futures (no-op if the stream was just cleared)
        self._project_chat.clear_pending_permissions(user_id)

        # Update session manager
        # Clear approve-all flag similar to /new command without replacing
        # unrelated fields written by another concurrent update.
        await self._session_manager.patch_session(
            user_id, remove_fields={"approve_all_outside_access"}
        )

    async def _execute_conversation_revert(
        self, user_id: int, session_id: str, msg_index: int, mode: str
    ) -> bool:
        """Revert conversation by truncating JSONL file to selected message.

        Args:
            mode: "full", "conv", or "code"
        """
        filepath = resolve_conversation_file(self._project_chat.conversations_dir, session_id)
        if filepath is None:
            logger.warning(
                "Rejected conversation path outside conversations dir: session_id=%r",
                session_id,
            )
            return False

        if revert_ops.truncate_jsonl_to(filepath, msg_index):
            logger.info(
                f"User {user_id}: conversation reverted to before message {msg_index} (mode: {mode})"
            )
            return True
        return False

    async def _execute_summarize_mode(
        self, user_id: int, session_id: str, msg_index: int
    ) -> bool:
        """Execute summarize mode by injecting summary request.

        Note: This is a simplified implementation that just informs the user.
        Full implementation would inject a system message requesting summary.
        """
        # For now, just return success - full implementation would require
        # injecting a message into the conversation stream
        logger.info(
            f"User {user_id}: summarize mode requested from message {msg_index}"
        )
        return True

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Global error handler for uncaught exceptions in handlers."""
        # Telegram rejects no-op edits (identical text + reply markup) with a 400
        # "message is not modified". Inline-button / callback edit paths can hit
        # this (e.g. tapping the same option twice). It's harmless — the message
        # already shows the intended content — so log quietly instead of alarming
        # the user with "❌ Internal error".
        if tg_errors.is_not_modified(context.error):
            logger.debug(
                "Ignored Telegram 'message is not modified' (no-op edit): %s",
                context.error,
            )
            return
        logger.error("Unhandled exception:", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(
                    update.effective_chat.id, f"❌ Internal error: {context.error}"
                )
            except Exception:
                pass

    async def _cancel_user_streaming(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        """Cancel streaming for a user/conversation"""
        try:
            return await self._project_chat.cancel_user_streaming(user_id, chat_id)
        except Exception as e:
            logger.error(f"Failed to cancel streaming for user {user_id}: {e}")
            return False

    def _clear_user_queue(self, user_id: int) -> int:
        return self._tasks.clear(user_id)

    async def _enqueue_user_task(
        self,
        user_id: int,
        run_task: Callable[[], Awaitable[None]],
        on_overflow: Callable[[], Awaitable[None]],
    ) -> bool:
        return await self._tasks.enqueue(user_id, run_task, on_overflow)

    async def _cmd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /command xxx - forward as Claude Code slash command"""
        if not await self._check_access(update):
            return
        message = self._require_message(update)
        user_id = self._require_user(update).id
        chat = self._require_chat(update)
        app = self._require_application()
        text = message.text or ""
        log_debug(user_id, "command", text)
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            reply = "Usage: /command <command_name> [args]\nExample: /command commit"
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        conversation_key = self._conversation_key(user_id, chat.id)
        slash_cmd = "/" + parts[1]

        async def run_task():
            session, _ = await self._switch_provider_if_needed(
                conversation_key, user_id, chat.id
            )
            try:
                await message.chat.send_action(action="typing")
            except Exception:
                pass
            response = await self._project_chat.process_message(
                user_message=slash_cmd,
                user_id=user_id,
                chat_id=chat.id,
                session_id=self._effective_session_id(conversation_key, session),
                model=session.get("model"),
                effort=session.get("effort"),
                approval_policy=self._codex_approval_policy(),
                approvals_reviewer=self._codex_approvals_reviewer(),
                sandbox_policy=self._codex_sandbox_policy(),
                permission_callback=self._permission_callback,
                approval_callback=self._codex_approval_callback,
                typing_callback=lambda: message.chat.send_action(action="typing"),
                status_callback=self._make_status_callback(app.bot, chat.id),
                bot=app.bot,
                interim_message_callback=self._make_interim_reply_callback(message),
            )
            await self._save_session_id(
                conversation_key,
                response,
                user_id=user_id,
                chat_id=chat.id,
                request_text=slash_cmd,
                turn_marker=f"telegram-message:{message.message_id}",
            )
            await self._reply_smart(
                message,
                response.content,
                parse_mode="Markdown",
                force_options=response.has_options,
                streamed=response.streamed,
                user_id=user_id,
            )

        async def on_overflow():
            reply = "⏳ Processing previous messages, please wait or send /stop to terminate."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)

        await self._enqueue_user_task(conversation_key, run_task, on_overflow)

    async def _exec_slash_command(self, update: Update, slash_cmd: str):
        """Execute a slash command via Claude Code CLI and reply."""
        message = self._require_message(update)
        user_id = self._require_user(update).id
        chat = self._require_chat(update)
        app = self._require_application()
        conversation_key = self._conversation_key(user_id, chat.id)

        async def run_task():
            session, _ = await self._switch_provider_if_needed(
                conversation_key, user_id, chat.id
            )
            await message.chat.send_action(action="typing")
            try:
                response = await self._project_chat.process_message(
                    user_message=slash_cmd,
                    user_id=user_id,
                    chat_id=chat.id,
                    session_id=self._effective_session_id(conversation_key, session),
                    model=session.get("model"),
                    effort=session.get("effort"),
                    approval_policy=self._codex_approval_policy(),
                    approvals_reviewer=self._codex_approvals_reviewer(),
                    sandbox_policy=self._codex_sandbox_policy(),
                    permission_callback=self._permission_callback,
                    approval_callback=self._codex_approval_callback,
                    typing_callback=lambda: message.chat.send_action(action="typing"),
                    status_callback=self._make_status_callback(app.bot, chat.id),
                    bot=app.bot,
                    interim_message_callback=self._make_interim_reply_callback(message),
                )
                await self._save_session_id(
                    conversation_key,
                    response,
                    user_id=user_id,
                    chat_id=chat.id,
                    request_text=slash_cmd,
                    turn_marker=f"telegram-message:{message.message_id}",
                )
                await self._reply_smart(
                    message,
                    response.content,
                    parse_mode="Markdown",
                    force_options=response.has_options,
                    streamed=response.streamed,
                    user_id=user_id,
                )
            except Exception as e:
                logger.error(f"Skill execution failed: {e}", exc_info=True)
                await message.reply_text(f"❌ Execution failed: {str(e)}")

        async def on_overflow():
            reply = "⏳ Processing previous messages, please wait or send /stop to terminate."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)

        await self._enqueue_user_task(conversation_key, run_task, on_overflow)

    async def _cmd_skill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /skill xxx [args] - forward as Claude Code slash command (/xxx [args])"""
        if not await self._check_access(update):
            return
        message = self._require_message(update)
        user_id = self._require_user(update).id
        text = message.text or ""
        log_debug(user_id, "command", text)
        parts = text.split(maxsplit=2)  # /skill, name, args
        if len(parts) < 2:
            reply = "Usage: /skill <skill_name> [args]\nExample: /skill post-url-to-x https://example.com"
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        skill_name = parts[1]
        args = parts[2] if len(parts) > 2 else ""
        await self._exec_slash_command(update, f"/{skill_name} {args}".strip())

    async def _handle_skill_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle skill commands like /baoyu-post-to-x - forward to Claude Code CLI"""
        if not await self._check_access(update):
            return
        message = self._require_message(update)
        if not message.text:
            return

        text = message.text
        parts = text.split(maxsplit=1)
        command = parts[0]
        cmd_name = command.lstrip("/").split("@")[0]

        # Check if a CommandHandler exists for this command in group 0
        # If yes, it was already handled, so skip
        app = self._require_application()
        for handler in app.handlers.get(0, []):
            if isinstance(handler, CommandHandler) and cmd_name in handler.commands:
                return

        # This is an unknown command - treat as skill
        args = parts[1] if len(parts) > 1 else ""
        user_id = self._require_user(update).id
        log_debug(user_id, "command", text)

        await self._exec_slash_command(update, f"/{cmd_name} {args}".strip())
