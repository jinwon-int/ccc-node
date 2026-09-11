"""Conversation-bound, explicit recovery choices; startup never dispatches work."""
from __future__ import annotations

import asyncio
import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_bot.core.danso_worker import TASK_RESUME_CONTROL
from telegram_bot.core.dead_session_recovery import parse_conversation_route
from telegram_bot.core.memory_audience import resolve_memory_audience

logger = logging.getLogger(__name__)
OFFER = 'danso_recovery_offer'
NOTIFIED = 'danso_recovery_notified'


def keyboard(token):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('이어서 진행', callback_data=f'drecover:{token}:continue'),
         InlineKeyboardButton('새 작업 시작', callback_data=f'drecover:{token}:new')],
        [InlineKeyboardButton('상태만 확인', callback_data=f'drecover:{token}:view')],
    ])


def binding(current):
    fields = {'provider', 'session_id', 'new_session', OFFER}
    return {'expected': {k: current[k] for k in fields if k in current},
            'absent_fields': fields - current.keys()}


class DansoRecoveryMixin:
    # mypy: disable-error-code="attr-defined"
    def _danso_recovery_enabled(self):
        return (self._active_provider() == 'danso'
                and bool(getattr(self._config, 'danso_long_task_enabled', False)))

    def _danso_recovery_route(self, user_id, chat_id):
        audience = resolve_memory_audience(self._config, user_id=user_id, chat_id=chat_id)
        return None if audience is None else [audience.kind, audience.scope]

    def _danso_recovery_guard(self, key, user_id, chat_id, epoch, route):
        return (self._danso_recovery_enabled() and self._check_user_access(user_id)
                and self._conversation_key(user_id, chat_id) == key
                and self._task_resume_generation(key) == epoch
                and self._danso_recovery_route(user_id, chat_id) == route)

    async def _offer_danso_recovery(self, key, user_id, chat_id, *, force=False):
        if not self._danso_recovery_enabled() or not self._check_user_access(user_id):
            return False
        current = await self._session_manager.get_session(key)
        sid = current.get('session_id')
        if current.get('provider') != 'danso' or not sid or current.get('new_session'):
            return False
        epoch = self._task_resume_generation(key)
        route = self._danso_recovery_route(user_id, chat_id)
        try:
            snapshot = await self._project_chat.inspect_danso_recovery(sid, user_id, chat_id)
        except Exception as error:
            logger.info('Danso recovery inspection unavailable: %s', type(error).__name__)
            return False
        if snapshot.state in {'completed', 'not_long_task'}:
            return False
        if (not force and current.get(NOTIFIED) == snapshot.fingerprint
                and isinstance(current.get(OFFER), dict)
                and current[OFFER].get('fingerprint') == snapshot.fingerprint):
            return False
        if not self._danso_recovery_guard(key, user_id, chat_id, epoch, route):
            return False
        token = secrets.token_hex(12)
        offer = dict(token=token, session_id=sid, fingerprint=snapshot.fingerprint,
                     user_id=user_id, chat_id=chat_id, route=route)
        saved = await self._session_manager.patch_session_if(
            key, **binding(current), updates={OFFER: offer},
            guard=lambda: self._danso_recovery_guard(key, user_id, chat_id, epoch, route),
        )
        if not saved:
            return False
        try:
            await self._require_application().bot.send_message(
                chat_id=chat_id, text=snapshot.render(), reply_markup=keyboard(token),
            )
        except Exception as error:
            logger.warning('Danso recovery delivery failed: %s', type(error).__name__)
            return False
        await self._session_manager.patch_session_if(
            key, expected={OFFER: offer, 'session_id': sid, 'provider': 'danso'},
            updates={NOTIFIED: snapshot.fingerprint},
        )
        return True

    async def _recover_danso_tasks(self, application):
        if not self._danso_recovery_enabled():
            return
        try:
            async with asyncio.timeout(10):
                await self._scan_danso_recovery()
        except TimeoutError:
            logger.info('Danso recovery startup budget reached; /task_recover remains available')

    async def _scan_danso_recovery(self):
        sent = 0
        for raw_key, current in list((await self._session_manager.list_sessions()).items())[:100]:
            if current.get('provider') != 'danso':
                continue
            try:
                key, user_id, chat_id = parse_conversation_route(raw_key)
                # Shared keys without an unambiguous recipient never select a
                # guessed chat. /task_recover remains available in that chat.
                if user_id <= 0 or chat_id == 0 or key != self._conversation_key(user_id, chat_id):
                    continue
                sent += await self._offer_danso_recovery(key, user_id, chat_id)
            except Exception as error:
                logger.info('Danso startup recovery skipped: %s', type(error).__name__)
            if sent >= 10:
                break

    async def _cmd_task_recover(self, update, context):
        if not await self._check_access(update):
            return
        user_id, chat_id = self._require_user(update).id, self._require_chat(update).id
        shown = await self._offer_danso_recovery(
            self._conversation_key(user_id, chat_id), user_id, chat_id, force=True,
        )
        if not shown:
            await self._require_message(update).reply_text(
                '현재 복구할 작업이 없거나 실행 중이라 기록을 읽을 수 없습니다. 잠시 후 다시 확인해 주세요.')

    async def _recovery_choice_snapshot(self, key, user_id, chat_id, token, epoch, route):
        if not self._danso_recovery_guard(key, user_id, chat_id, epoch, route):
            return None
        current = await self._session_manager.get_session(key)
        offer = current.get(OFFER)
        if (not isinstance(offer, dict) or offer.get('token') != token
                or offer.get('user_id') != user_id or offer.get('chat_id') != chat_id
                or offer.get('route') != route or current.get('provider') != 'danso'
                or current.get('new_session') or current.get('session_id') != offer.get('session_id')):
            return None
        snapshot = await self._project_chat.inspect_danso_recovery(
            offer['session_id'], user_id, chat_id)
        if (snapshot.fingerprint != offer.get('fingerprint')
                or not self._danso_recovery_guard(key, user_id, chat_id, epoch, route)):
            return None
        return current, offer, snapshot

    async def _handle_danso_recovery(self, update, data):
        query = self._require_callback_query(update)
        parts = data.split(':')
        if len(parts) != 3 or parts[2] not in {'continue', 'new', 'view'}:
            return
        _, token, action = parts
        user_id, chat_id = self._require_user(update).id, self._require_chat(update).id
        key = self._conversation_key(user_id, chat_id)
        epoch, route = self._task_resume_generation(key), self._danso_recovery_route(user_id, chat_id)

        async def run():
            try:
                found = await self._recovery_choice_snapshot(key, user_id, chat_id, token, epoch, route)
            except Exception:
                found = None
            if found is None:
                await query.edit_message_text('작업 상태가 바뀌었거나 만료된 버튼입니다. /task_recover 로 다시 확인해 주세요.')
                return
            current, offer, snapshot = found
            if action == 'view':
                await query.edit_message_text(snapshot.render(), reply_markup=keyboard(token))
                return
            # Claim exactly once, only after queue admission and a fresh read.
            claimed = await self._session_manager.patch_session_if(
                key, **binding(current), remove_fields={OFFER},
                guard=lambda: self._danso_recovery_guard(key, user_id, chat_id, epoch, route),
            )
            if not claimed:
                await query.edit_message_text('이미 처리했거나 만료된 선택입니다.')
                return
            if not self._danso_recovery_guard(key, user_id, chat_id, epoch, route):
                return
            if action == 'new':
                changed = await self._start_new_after_recovery(key, user_id, chat_id, route, offer)
                await query.edit_message_text(
                    '새 작업을 입력해 주세요. 이전 작업 기록은 보존했습니다.' if changed
                    else '작업 상태가 바뀌었습니다. /task_recover 로 다시 확인해 주세요.')
                return
            await query.edit_message_text('선택을 확인했습니다. 저장된 상태에 맞춰 이어서 진행합니다.')
            if not self._danso_recovery_guard(key, user_id, chat_id, epoch, route):
                return
            await self._continue_danso_recovery(key, user_id, chat_id, epoch, route, current, snapshot)

        async def overflow():
            await query.edit_message_text('다른 작업을 처리 중입니다. /task_recover 로 다시 확인해 주세요.')
        await self._enqueue_user_task(key, run, overflow)

    async def _start_new_after_recovery(self, key, user_id, chat_id, route, offer):
        epoch = self._bump_task_resume_generation(key)
        changed = await self._session_manager.patch_session_if(
            key, expected={'provider': 'danso', 'session_id': offer['session_id']},
            updates={'session_id': None, 'new_session': True}, remove_fields={OFFER, NOTIFIED},
            guard=lambda: self._danso_recovery_guard(key, user_id, chat_id, epoch, route),
        )
        if changed:
            self._runtime_active_sessions.discard(key)
        return changed

    async def _continue_danso_recovery(self, key, user_id, chat_id, epoch, route, current, snapshot):
        app = self._require_application()
        safe_resume = snapshot.resume_allowed
        prompt = TASK_RESUME_CONTROL if safe_resume else snapshot.continuation()
        def guard():
            return self._danso_recovery_guard(key, user_id, chat_id, epoch, route)
        response = await self._project_chat.process_message(
            user_message=prompt, user_id=user_id, chat_id=chat_id,
            session_id=current['session_id'] if safe_resume else None,
            new_session=not safe_resume, resume_task=safe_resume, dispatch_guard=guard,
            model=current.get('model'), effort=current.get('effort'),
            approval_policy=self._codex_approval_policy(),
            approvals_reviewer=self._codex_approvals_reviewer(),
            sandbox_policy=self._codex_sandbox_policy(), approval_callback=self._codex_approval_callback,
            status_callback=self._make_status_callback(app.bot, chat_id),
            bot=app.bot, notification_bot=app.bot, sensitive_log_event='danso-recovery',
            interim_message_callback=self._make_interim_send_callback(chat_id),
        )
        # Session identity was persisted by the guarded start recorder before
        # dispatch. Do not overwrite a later /new with a post-turn save.
        await self._send_smart(chat_id, response.content)
        if not response.success:
            await self._offer_danso_recovery(key, user_id, chat_id, force=True)
