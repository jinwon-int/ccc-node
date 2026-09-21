"""Restart-only auto-resume (owner decision 2026-09-21): ready/paused +
resume_allowed dispatches the explicit resume path; everything else keeps the menu."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.bot_danso_recovery import AUTO_RESUME_NOTICE, OFFER
from telegram_bot.core.danso_worker import TASK_RESUME_CONTROL
from test_danso_recovery import setup_bot

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return 'asyncio'


async def auto_bot(tmp_path, state='paused', allowed=True, enabled=True):
    bot, manager, handler = await setup_bot(tmp_path, state, allowed)
    bot._config.danso_recovery_auto_resume = enabled
    return bot, manager, handler


def sent_texts(bot):
    return [c.kwargs['text'] for c in bot.application.bot.send_message.await_args_list]


@pytest.mark.anyio
@pytest.mark.parametrize('state', ['paused', 'ready'])
async def test_restart_auto_resumes_resumable_task_through_explicit_path(tmp_path, state):
    bot, manager, handler = await auto_bot(tmp_path, state, True)
    await bot._recover_danso_tasks(bot.application)
    handler.process_message.assert_awaited_once()
    kwargs = handler.process_message.call_args.kwargs
    assert kwargs['user_message'] == TASK_RESUME_CONTROL
    assert kwargs['resume_task'] is True and kwargs['new_session'] is False
    assert kwargs['session_id'] == 'sid'
    texts = sent_texts(bot)
    assert texts[0].startswith(AUTO_RESUME_NOTICE.split('{')[0])
    assert state in texts[0] and 'Fix the test' in texts[0]
    assert all('번호로 답해' not in t for t in texts)
    assert OFFER not in await manager.get_session('7:9')  # one-shot claim consumed
    # Restart loop with a byte-identical journal: no second automatic resume,
    # the user gets the menu instead.
    await bot._recover_danso_tasks(bot.application)
    handler.process_message.assert_awaited_once()
    assert bot.application.bot.send_message.await_args.kwargs['reply_markup'] is not None
    assert (await manager.get_session('7:9'))[OFFER]['token']


@pytest.mark.anyio
@pytest.mark.parametrize(('state', 'allowed'), [
    ('paused', False), ('ready', False), ('pending_provider', False),
    ('pending_tools', False), ('final_pending', False), ('failed', False), ('blocked', False),
])
async def test_restart_keeps_menu_for_every_non_resumable_state(tmp_path, state, allowed):
    bot, manager, handler = await auto_bot(tmp_path, state, allowed)
    await bot._recover_danso_tasks(bot.application)
    handler.process_message.assert_not_awaited()
    assert bot.application.bot.send_message.await_count == 1
    assert bot.application.bot.send_message.await_args.kwargs['reply_markup'] is not None
    assert (await manager.get_session('7:9'))[OFFER]['token']


@pytest.mark.anyio
async def test_opt_in_off_keeps_menu_even_when_resumable(tmp_path):
    bot, manager, handler = await auto_bot(tmp_path, 'paused', True, enabled=False)
    await bot._recover_danso_tasks(bot.application)
    handler.process_message.assert_not_awaited()
    assert bot.application.bot.send_message.await_args.kwargs['reply_markup'] is not None


@pytest.mark.anyio
async def test_task_recover_and_post_failure_offers_never_auto_resume(tmp_path):
    bot, manager, handler = await auto_bot(tmp_path, 'paused', True)
    assert await bot._offer_danso_recovery('7:9', 7, 9, force=True)  # /task_recover path
    await bot._offer_danso_recovery_if_failed(SimpleNamespace(success=False), '7:9', 7, 9)
    handler.process_message.assert_not_awaited()
    assert all(c.kwargs['reply_markup'] is not None
               for c in bot.application.bot.send_message.await_args_list)


@pytest.mark.anyio
async def test_failed_auto_resume_falls_back_to_menu_without_looping(tmp_path):
    bot, manager, handler = await auto_bot(tmp_path, 'paused', True)
    handler.process_message.return_value = SimpleNamespace(success=False, content='boom', session_id='sid')
    await bot._recover_danso_tasks(bot.application)
    handler.process_message.assert_awaited_once()
    last = bot.application.bot.send_message.await_args.kwargs
    assert last['reply_markup'] is not None  # post-failure offer is a menu, not another resume
    assert (await manager.get_session('7:9'))[OFFER]['token']


@pytest.mark.anyio
async def test_notice_delivery_failure_never_resumes_silently(tmp_path):
    bot, manager, handler = await auto_bot(tmp_path, 'paused', True)
    bot.application.bot.send_message = AsyncMock(side_effect=RuntimeError('synthetic'))
    await bot._recover_danso_tasks(bot.application)
    handler.process_message.assert_not_awaited()
