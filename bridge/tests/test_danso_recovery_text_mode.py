"""Text-mode Danso recovery (#1718): numbered typed answer instead of buttons."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.bot_danso_recovery import OFFER, RECOVERY_TEXT_MENU
from test_danso_recovery import snapshot
from test_session_provider import bare_bot, make_manager, make_update

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return 'asyncio'


async def text_bot(tmp_path):
    manager = make_manager(tmp_path, 'danso')
    await manager.patch_session('7:9', updates={'provider': 'danso', 'session_id': 'sid'})
    handler = SimpleNamespace(
        inspect_danso_recovery=AsyncMock(return_value=snapshot()),
        process_message=AsyncMock(return_value=SimpleNamespace(
            success=True, content='done', session_id='new-sid')))
    bot = bare_bot(manager, provider='danso', project_chat=handler)
    bot._config.danso_long_task_enabled = True
    bot._config.danso_recovery_text_mode = True
    bot._config.allowed_user_ids = [7]
    bot._config.bridge_memory_mode = 'off'
    bot.application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    bot._send_smart = AsyncMock()

    async def enqueue(key, run, overflow):
        await run()
    bot._enqueue_user_task = enqueue
    return bot, manager, handler


def typed(text, user=7, chat=9):
    return make_update(user_id=user, chat_id=chat, text=text), text


@pytest.mark.anyio
async def test_offer_appends_numbered_menu_without_keyboard(tmp_path):
    bot, manager, handler = await text_bot(tmp_path)
    assert await bot._offer_danso_recovery('7:9', 7, 9)
    call = bot.application.bot.send_message.await_args
    assert RECOVERY_TEXT_MENU in call.kwargs['text']
    assert call.kwargs['reply_markup'] is None
    assert (await manager.get_session('7:9'))[OFFER]['token']


@pytest.mark.anyio
async def test_default_mode_keeps_keyboard_and_no_menu(tmp_path):
    bot, manager, handler = await text_bot(tmp_path)
    bot._config.danso_recovery_text_mode = False
    assert await bot._offer_danso_recovery('7:9', 7, 9)
    call = bot.application.bot.send_message.await_args
    assert RECOVERY_TEXT_MENU not in call.kwargs['text']
    assert call.kwargs['reply_markup'] is not None


@pytest.mark.anyio
async def test_typed_one_claims_offer_and_resumes(tmp_path):
    bot, manager, handler = await text_bot(tmp_path)
    assert await bot._offer_danso_recovery('7:9', 7, 9)
    update, text = typed('1')
    assert await bot._maybe_answer_danso_recovery_text(update, 7, text)
    assert OFFER not in await manager.get_session('7:9')
    handler.process_message.assert_awaited_once()
    kwargs = handler.process_message.call_args.kwargs
    assert kwargs['session_id'] is None  # default snapshot: resume_allowed=False
    assert kwargs['resume_task'] is False and kwargs['new_session'] is True


@pytest.mark.anyio
@pytest.mark.parametrize('digit', ['2', '3'])
async def test_typed_choices_route_without_provider_for_new_and_view(tmp_path, digit):
    bot, manager, handler = await text_bot(tmp_path)
    await bot._offer_danso_recovery('7:9', 7, 9)
    update, text = typed(digit)
    assert await bot._maybe_answer_danso_recovery_text(update, 7, text)
    stored = await manager.get_session('7:9')
    if digit == '2':
        assert stored['session_id'] is None and stored['new_session'] is True
    else:
        assert stored['session_id'] == 'sid' and OFFER in stored
    handler.process_message.assert_not_awaited()


@pytest.mark.anyio
async def test_non_choice_text_never_intercepts(tmp_path):
    bot, manager, handler = await text_bot(tmp_path)
    await bot._offer_danso_recovery('7:9', 7, 9)
    update, text = typed('밥 먹었어?')
    assert await bot._maybe_answer_danso_recovery_text(update, 7, text) is False
    assert OFFER in await manager.get_session('7:9')


@pytest.mark.anyio
async def test_typed_choice_from_other_user_or_expired_offer_rejects(tmp_path):
    bot, manager, handler = await text_bot(tmp_path)
    update, text = typed('1')
    assert await bot._maybe_answer_danso_recovery_text(update, 7, text) is False
    await bot._offer_danso_recovery('7:9', 7, 9)
    update, text = typed('1', user=8)
    assert await bot._maybe_answer_danso_recovery_text(update, 8, text) is False
    handler.process_message.assert_not_awaited()
