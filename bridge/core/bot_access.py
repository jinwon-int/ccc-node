import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path as FilePath
from typing import Any, Iterable, List, Optional

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
from telegram import Update

from telegram_bot.core import paths as path_scope
from telegram_bot.core import tool_policy

logger = logging.getLogger(__name__)
STALE_MESSAGE_SECONDS = 20 * 60  # 20 minutes


class BotAccessMixin:
    def _check_user_access(self, user_id: int) -> bool:
        """Check if user has permission to use the bot"""
        if not self._config.allowed_user_ids:
            return True  # Allow all users if not configured
        return user_id in self._config.allowed_user_ids

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

    def _project_root(self) -> FilePath:
        return FilePath(self._config.project_root).resolve()

    def _bash_policy(self) -> str:
        execution_profile = tool_policy.resolve_execution_profile(
            getattr(self._config, "execution_profile", tool_policy.EXECUTION_STRICT_PROJECT),
            allowed_user_ids=getattr(self._config, "allowed_user_ids", []),
            require_allowlist=getattr(self._config, "require_allowlist", True),
        )
        return tool_policy.effective_bash_policy(
            tool_policy.resolve_bash_policy(getattr(self._config, "bash_policy", None)),
            execution_profile,
        )

    def _codex_approval_policy(self) -> str:
        """Map bridge approval UX to Codex app-server's supported policy."""

        policy = self._bash_policy()
        if policy == tool_policy.BASH_AUTO_APPROVE:
            return "never"
        if policy == tool_policy.BASH_AUTO_REVIEW:
            return "on-request"
        return "untrusted"

    def _codex_approvals_reviewer(self) -> str | None:
        """Route eligible boundary reviews to Codex only in auto-review mode."""

        return (
            "auto_review"
            if self._bash_policy() == tool_policy.BASH_AUTO_REVIEW
            else None
        )

    def _codex_sandbox_policy(self) -> dict[str, object] | None:
        """Keep automatic Codex modes inside a network-off workspace boundary."""

        if self._bash_policy() not in {
            tool_policy.BASH_AUTO_APPROVE,
            tool_policy.BASH_AUTO_REVIEW,
        }:
            return None
        return {"type": "workspaceWrite", "networkAccess": False}

    def _is_within_project_root(self, path: FilePath) -> bool:
        return path_scope.is_within_project_root(path, self._project_root())

    def _resolve_candidate_path(self, raw_path: str) -> FilePath:
        return path_scope.resolve_candidate_path(raw_path, self._project_root())

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

    @staticmethod
    def _approval_flag(approval_kind: str) -> str:
        return (
            "bash_approved_once"
            if approval_kind == "bash"
            else "outside_path_approved_once"
        )

    @staticmethod
    def _approval_digest(tool_name: str, tool_input: Any) -> str:
        """Bind an approval token to the exact canonicalized tool request."""

        canonical = json.dumps(
            {"tool": tool_name, "input": tool_input},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def _consume_outside_approval_once(
        self,
        user_id: int,
        chat_id: Optional[int] = None,
        *,
        approval_kind: str = "outside-path",
        approval_digest: Optional[str] = None,
    ) -> bool:
        session_key = self._conversation_key(user_id, chat_id)
        flag = self._approval_flag(approval_kind)
        expected: dict[str, Any] = {flag: True}
        remove_fields = {
            "pending_outside_paths",
            "pending_approval_kind",
            "pending_approval_digest",
        }
        if approval_kind == "bash":
            if not approval_digest:
                return False
            session = await self._session_manager.get_session(session_key)
            approved_digest = session.get("bash_approved_digest")
            if not session.get(flag) or not approved_digest:
                return False
            if approved_digest != approval_digest:
                await self._session_manager.patch_session_if(
                    session_key,
                    expected={flag: True, "bash_approved_digest": approved_digest},
                    updates={flag: False},
                    remove_fields=remove_fields | {"bash_approved_digest"},
                )
                logger.warning(
                    "bash_approval_digest_mismatch user_id=%s chat_id=%s",
                    user_id,
                    chat_id,
                )
                return False
            expected["bash_approved_digest"] = approval_digest
            remove_fields.add("bash_approved_digest")
        consumed = await self._session_manager.patch_session_if(
            session_key,
            expected=expected,
            updates={flag: False},
            remove_fields=remove_fields,
        )
        if not consumed and approval_kind == "bash":
            logger.warning(
                "bash_approval_unavailable_or_digest_mismatch user_id=%s chat_id=%s",
                user_id,
                chat_id,
            )
        return consumed

    async def _maybe_capture_outside_approval(
        self, user_id: int, text: str, chat_id: Optional[int] = None
    ) -> None:
        session_key = self._conversation_key(user_id, chat_id)
        session = await self._session_manager.get_session(session_key)
        pending = session.get("pending_outside_paths")
        if not pending:
            return

        normalized = " ".join(text.strip().lower().split())
        allow_token = self._ALLOW_OUTSIDE_ONCE_TOKEN.lower()
        deny_token = self._DENY_OUTSIDE_TOKEN.lower()
        allow = (
            normalized == allow_token
            or normalized.startswith(f"{allow_token} (")
            or normalized.startswith(f"1. {allow_token} (")
        )
        deny = (
            normalized == deny_token
            or normalized.startswith(f"{deny_token} (")
            or normalized.startswith(f"2. {deny_token} (")
        )
        if not allow and not deny:
            return

        approval_kind = session.get("pending_approval_kind", "outside-path")
        pending_digest = session.get("pending_approval_digest")
        flag = self._approval_flag(approval_kind)
        other_flag = self._approval_flag(
            "outside-path" if approval_kind == "bash" else "bash"
        )
        expected: dict[str, Any] = {"pending_outside_paths": pending}
        if "pending_approval_kind" in session:
            expected["pending_approval_kind"] = session["pending_approval_kind"]
        if "pending_approval_digest" in session:
            expected["pending_approval_digest"] = pending_digest

        updates: dict[str, Any] = {flag: bool(allow), other_flag: False}
        remove_fields = {
            "pending_outside_paths",
            "pending_approval_kind",
            "pending_approval_digest",
        }
        if approval_kind == "bash":
            remove_fields.add("bash_approved_digest")
            if allow and pending_digest:
                updates["bash_approved_digest"] = pending_digest
                remove_fields.discard("bash_approved_digest")
            elif allow:
                updates[flag] = False
                logger.warning(
                    "bash_approval_missing_digest user_id=%s chat_id=%s",
                    user_id,
                    chat_id,
                )
            elif deny:
                logger.info(
                    "bash_approval_reply_denied user_id=%s chat_id=%s",
                    user_id,
                    chat_id,
                )

        applied = await self._session_manager.patch_session_if(
            session_key,
            expected=expected,
            updates=updates,
            remove_fields=remove_fields,
        )
        if applied and allow and approval_kind == "bash" and pending_digest:
            logger.info(
                "bash_approval_reply_allowed user_id=%s chat_id=%s",
                user_id,
                chat_id,
            )

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

        if tool_name == "Bash":
            policy = self._bash_policy()
            if policy == "auto-approve":
                logger.info(
                    "bash_auto_approved user_id=%s chat_id=%s", user_id, chat_id
                )
                return PermissionResultAllow()

            if policy not in (
                tool_policy.BASH_APPROVE_EACH,
                tool_policy.BASH_AUTO_REVIEW,
            ):
                logger.warning(
                    "bash_disabled_denied user_id=%s chat_id=%s", user_id, chat_id
                )
                return PermissionResultDeny(
                    message=(
                        "Bash is disabled by the fail-closed bridge policy. "
                        "An operator may select CCC_BRIDGE_BASH_POLICY=auto-approve, "
                        "auto-review, or approve-each, but PROJECT_ROOT is not an OS sandbox."
                    )
                )

            request_digest = self._approval_digest(tool_name, tool_input)
            if await self._consume_outside_approval_once(
                user_id,
                chat_id,
                approval_kind="bash",
                approval_digest=request_digest,
            ):
                logger.info(
                    "bash_approval_consumed user_id=%s chat_id=%s", user_id, chat_id
                )
                return PermissionResultAllow()

            candidates = self._extract_path_candidates(tool_name, tool_input)[:4]
            pending = ["Bash command requires per-call approval", *candidates]
            session_key = self._conversation_key(user_id, chat_id)
            await self._session_manager.patch_session(
                session_key,
                updates={
                    "pending_outside_paths": pending,
                    "pending_approval_kind": "bash",
                    "pending_approval_digest": request_digest,
                },
            )
            logger.warning(
                "bash_approval_required user_id=%s chat_id=%s preview_paths=%s",
                user_id,
                chat_id,
                len(candidates),
            )
            preview = "\n".join(f"- {path}" for path in candidates)
            if preview:
                preview = f"\nInformational path-like tokens (not a sandbox check):\n{preview}"
            return PermissionResultDeny(
                message=(
                    "Every Bash call requires explicit per-call confirmation because "
                    "PROJECT_ROOT is not an OS sandbox."
                    f"{preview}\n"
                    "Please output the following two options to the user and wait for a reply:\n"
                    f"1. {self._ALLOW_OUTSIDE_ONCE_TOKEN} (Allow this Bash call once)\n"
                    f"2. {self._DENY_OUTSIDE_TOKEN} (Deny)"
                )
            )

        outside_paths = self._extract_outside_paths(tool_name, tool_input)
        if outside_paths:
            session_key = self._conversation_key(user_id, chat_id)
            if await self._consume_outside_approval_once(user_id, chat_id):
                return PermissionResultAllow()

            await self._session_manager.patch_session(
                session_key,
                updates={
                    "pending_outside_paths": outside_paths[:5],
                    "pending_approval_kind": "outside-path",
                    "pending_approval_digest": None,
                },
            )
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
