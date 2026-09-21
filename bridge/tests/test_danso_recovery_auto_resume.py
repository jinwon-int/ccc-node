"""Restart-only auto-resume (owner decision 2026-09-21): ready/paused +
resume_allowed dispatches the explicit resume path; everything else keeps the menu."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.bot_danso_recovery import (
    AUTO_RESUME_NOTICE, AUTO_RESUME_RETRY_NOTICE, OFFER)
from test_danso_recovery import snapshot
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


# ---- #1880: delayed single retry after a stale-lock (`danso_session`) refusal

def session_failure(code='danso_session'):
    return SimpleNamespace(success=False, content='❌ Processing failed: Worker failed: category=session',
                           session_id='sid', failure_code=code)


def no_sleep(bot, monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr('telegram_bot.core.bot_danso_recovery.asyncio.sleep', fake_sleep)
    return slept


@pytest.mark.anyio
async def test_stale_lock_failure_retries_once_after_delay_then_succeeds(tmp_path, monkeypatch):
    bot, manager, handler = await auto_bot(tmp_path, 'paused', True)
    bot._config.danso_recovery_auto_resume_retry_delay_seconds = 7
    slept = no_sleep(bot, monkeypatch)
    handler.process_message.side_effect = [session_failure(), SimpleNamespace(success=True, content='done', session_id='sid')]
    await bot._recover_danso_tasks(bot.application)
    assert handler.process_message.await_count == 2
    first, second = [c.kwargs for c in handler.process_message.await_args_list]
    keys = ('user_message', 'session_id', 'resume_task', 'new_session', 'user_id', 'chat_id')
    assert {k: first[k] for k in keys} == {k: second[k] for k in keys}  # identical explicit-resume dispatch
    assert first['resume_task'] is True and first['session_id'] == 'sid'
    assert slept == [7]
    assert handler.inspect_danso_recovery.await_count >= 2  # re-inspected before retrying
    texts = sent_texts(bot)
    assert AUTO_RESUME_RETRY_NOTICE.format(delay=7) in texts
    assert all(c.kwargs.get('reply_markup') is None for c in bot.application.bot.send_message.await_args_list)
    bot._send_smart.assert_awaited_once_with(9, 'done')  # first failure text is not delivered
    assert OFFER not in await manager.get_session('7:9')


@pytest.mark.anyio
async def test_second_failure_gets_menu_and_never_a_third_attempt(tmp_path, monkeypatch):
    bot, manager, handler = await auto_bot(tmp_path, 'paused', True)
    no_sleep(bot, monkeypatch)
    handler.process_message.side_effect = [session_failure(), session_failure()]
    await bot._recover_danso_tasks(bot.application)
    assert handler.process_message.await_count == 2
    assert bot.application.bot.send_message.await_args.kwargs['reply_markup'] is not None
    assert (await manager.get_session('7:9'))[OFFER]['token']


@pytest.mark.anyio
@pytest.mark.parametrize('why', ['other_code', 'delay_zero', 'journal_changed', 'no_longer_resumable'])
async def test_no_retry_when_code_delay_or_journal_disqualify(tmp_path, monkeypatch, why):
    bot, manager, handler = await auto_bot(tmp_path, 'paused', True)
    slept = no_sleep(bot, monkeypatch)
    failure = session_failure('danso_failed' if why == 'other_code' else 'danso_session')
    handler.process_message.side_effect = [failure, SimpleNamespace(success=True, content='done', session_id='sid')]
    if why == 'delay_zero':
        bot._config.danso_recovery_auto_resume_retry_delay_seconds = 0
    if why in {'journal_changed', 'no_longer_resumable'}:
        # Offer + choice snapshot see the resumable journal; the pre-retry
        # re-inspection (third read onward) sees it changed.
        first = snapshot('paused', True)
        changed = (replace(first, fingerprint='b' * 64) if why == 'journal_changed'
                   else snapshot('pending_provider', False))
        reads = []

        async def inspect(*a, **k):
            reads.append(1)
            return first if len(reads) <= 2 else changed
        handler.inspect_danso_recovery = AsyncMock(side_effect=inspect)
    await bot._recover_danso_tasks(bot.application)
    assert handler.process_message.await_count == 1
    assert (slept == []) == (why in {'other_code', 'delay_zero'})
    assert bot.application.bot.send_message.await_args.kwargs['reply_markup'] is not None  # menu fallback


@pytest.mark.anyio
async def test_user_chosen_continue_never_retries(tmp_path, monkeypatch):
    from test_danso_recovery import callback
    bot, manager, handler = await auto_bot(tmp_path, 'paused', True, enabled=False)
    slept = no_sleep(bot, monkeypatch)
    handler.process_message.side_effect = [session_failure(), session_failure()]
    assert await bot._offer_danso_recovery('7:9', 7, 9)
    update, data = callback((await manager.get_session('7:9'))[OFFER]['token'])
    await bot._handle_danso_recovery(update, data)
    assert handler.process_message.await_count == 1 and slept == []


# ---- pause → restart: a menu already offered for this journal must not block the automatic resume

@pytest.mark.anyio
async def test_restart_auto_resumes_even_when_menu_was_offered_before_restart(tmp_path):
    bot, manager, handler = await auto_bot(tmp_path, 'paused', True)
    # The cooperative pause ended the previous turn with danso_task_paused and
    # the bridge offered the menu right away (same journal fingerprint, NOTIFIED set).
    await bot._offer_danso_recovery_if_failed(SimpleNamespace(success=False), '7:9', 7, 9)
    before = await manager.get_session('7:9')
    assert before[OFFER]['fingerprint'] == snapshot('paused', True).fingerprint
    assert before['danso_recovery_notified'] == snapshot('paused', True).fingerprint
    handler.process_message.assert_not_awaited()
    bot.application.bot.send_message.reset_mock()
    await bot._recover_danso_tasks(bot.application)  # restart scan
    handler.process_message.assert_awaited_once()
    assert handler.process_message.call_args.kwargs['resume_task'] is True
    assert OFFER not in await manager.get_session('7:9')
    assert all(c.kwargs.get('reply_markup') is None for c in bot.application.bot.send_message.await_args_list)


@pytest.mark.anyio
async def test_restart_with_pending_menu_and_auto_off_still_deduplicates(tmp_path):
    bot, manager, handler = await auto_bot(tmp_path, 'paused', True, enabled=False)
    await bot._offer_danso_recovery_if_failed(SimpleNamespace(success=False), '7:9', 7, 9)
    bot.application.bot.send_message.reset_mock()
    await bot._recover_danso_tasks(bot.application)
    handler.process_message.assert_not_awaited()
    bot.application.bot.send_message.assert_not_awaited()  # no duplicate menu
