import logging
from datetime import datetime, timezone
from pathlib import Path as FilePath
from typing import Any, Iterable, List, Optional

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
from telegram import Update

from telegram_bot.core import paths as path_scope
from telegram_bot.session.manager import session_manager
from telegram_bot.utils.config import config

logger = logging.getLogger(__name__)
STALE_MESSAGE_SECONDS = 20 * 60  # 20 minutes


class BotAccessMixin:
    def _check_user_access(self, user_id: int) -> bool:
        """Check if user has permission to use the bot"""
        if not config.allowed_user_ids:
            return True  # Allow all users if not configured
        return user_id in config.allowed_user_ids

    async def _check_access(self, update: Update) -> bool:
        """Check if user has permission to use this bot

        Returns:
            bool: True if user has permission, False otherwise
        """
        # Drop stale messages (> 20 min old)
        msg = update.message or update.callback_query and update.callback_query.message
        if msg and msg.date:
            age = (datetime.now(timezone.utc) - msg.date).total_seconds()
            if age > STALE_MESSAGE_SECONDS:
                logger.debug(
                    f"Dropping stale message ({age:.0f}s old) from {update.effective_user}"
                )
                return False

        user = update.effective_user
        if not user:
            return False

        # Check if user is in the allowed list
        if not self._check_user_access(user.id):
            # Send different rejection messages based on update type
            if update.message:
                if update.message.voice:
                    await update.message.reply_text(
                        "⛔ You don't have permission to send voice messages to this bot.\n"
                        "Please contact the admin for access."
                    )
                else:
                    await update.message.reply_text(
                        "⛔ Sorry, you don't have permission to use this bot.\n"
                        "Please contact the admin for access."
                    )
            elif update.callback_query:
                await update.callback_query.answer(
                    "⛔ No permission to use this feature", show_alert=True
                )
            return False
        return True

    @staticmethod
    def _is_priority_command(text: str) -> bool:
        """Check if a command should be processed with priority (bypass queue).

        Priority commands are processed immediately without queue limit checks.
        Currently /stop and /revert are priority commands.
        """
        return text.strip() in ("/stop", "/revert")

    @staticmethod
    def _project_root() -> FilePath:
        from telegram_bot.core.project_chat import PROJECT_ROOT

        return PROJECT_ROOT

    @staticmethod
    def _is_within_project_root(path: FilePath) -> bool:
        return path_scope.is_within_project_root(path, BotAccessMixin._project_root())

    @staticmethod
    def _resolve_candidate_path(raw_path: str) -> FilePath:
        return path_scope.resolve_candidate_path(raw_path, BotAccessMixin._project_root())

    @staticmethod
    def _iter_strings(value: Any) -> Iterable[str]:
        return path_scope.iter_strings(value)

    @staticmethod
    def _extract_paths_from_command(command: str) -> List[str]:
        return path_scope.extract_paths_from_command(command)

    def _extract_path_candidates(self, tool_name: str, tool_input: Any) -> List[str]:
        return path_scope.extract_path_candidates(tool_name, tool_input)

    def _extract_outside_paths(self, tool_name: str, tool_input: Any) -> List[str]:
        return path_scope.extract_outside_paths(
            tool_name, tool_input, project_root=self._project_root()
        )

    async def _consume_outside_approval_once(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        session_key = self._conversation_key(user_id, chat_id)
        session = await session_manager.get_session(session_key)
        if not session.get("outside_path_approved_once"):
            return False
        session["outside_path_approved_once"] = False
        session.pop("pending_outside_paths", None)
        await session_manager.update_session(session_key, session)
        return True

    async def _maybe_capture_outside_approval(self, user_id: int, text: str, chat_id: Optional[int] = None) -> None:
        session_key = self._conversation_key(user_id, chat_id)
        session = await session_manager.get_session(session_key)
        pending = session.get("pending_outside_paths")
        if not pending:
            return

        normalized = text.strip().lower()
        allow = self._ALLOW_OUTSIDE_ONCE_TOKEN.lower() in normalized or normalized in {
            "1",
            "allow",
            "yes",
            "y",
        }
        deny = self._DENY_OUTSIDE_TOKEN.lower() in normalized or normalized in {
            "2",
            "deny",
            "no",
            "n",
        }

        if allow:
            session["outside_path_approved_once"] = True
            session.pop("pending_outside_paths", None)
            await session_manager.update_session(session_key, session)
        elif deny:
            session["outside_path_approved_once"] = False
            session.pop("pending_outside_paths", None)
            await session_manager.update_session(session_key, session)

    async def _permission_callback(
        self, chat_id: int, user_id: int, tool_name: str, tool_input: Any
    ):
        """Handle tool permission requests.

        All interactive requests are denied so Claude falls back to numbered
        text options, which the bot detects and presents as inline keyboard buttons.
        """
        if tool_name == "AskUserQuestion":
            return PermissionResultDeny(
                message=(
                    "AskUserQuestion is not available in this environment. "
                    "IMPORTANT: Do NOT mention this to the user. Do NOT say the tool is unavailable or that user didn't select. "
                    "Instead, you MUST output the question and options in this EXACT format:\n\n"
                    "[Question text here]\n\n"
                    "1. [First option]\n"
                    "2. [Second option]\n"
                    "3. [Third option]\n\n"
                    "Example:\n"
                    "确认发布内容：\n"
                    "[show the content here with absolute file paths like /Users/.../image.png]\n\n"
                    "1. 去发布\n"
                    "2. 重新生成\n"
                    "3. 取消\n\n"
                    "The system will automatically convert these numbered options into clickable buttons for the user."
                )
            )

        outside_paths = self._extract_outside_paths(tool_name, tool_input)
        if outside_paths:
            session_key = self._conversation_key(user_id, chat_id)
            if await self._consume_outside_approval_once(user_id, chat_id):
                return PermissionResultAllow()

            session = await session_manager.get_session(session_key)
            session["pending_outside_paths"] = outside_paths[:5]
            await session_manager.update_session(session_key, session)

            preview = "\n".join(f"- {path}" for path in outside_paths[:5])
            return PermissionResultDeny(
                message=(
                    "Detected access to paths outside PROJECT_ROOT. Requires confirmation before proceeding.\n"
                    f"{preview}\n"
                    "Please output the following two options to the user and wait for a reply:\n"
                    f"1. {self._ALLOW_OUTSIDE_ONCE_TOKEN} (Allow this external path access)\n"
                    f"2. {self._DENY_OUTSIDE_TOKEN} (Deny)"
                )
            )

        return PermissionResultAllow()
