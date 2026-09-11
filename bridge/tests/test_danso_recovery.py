"""Recovery buttons are evidence-bound user consent, never implicit replay."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from telegram_bot.core.danso_recovery import RecoverySnapshot, recovery_record, _summarize
from telegram_bot.core.danso_worker import _failure, TASK_RESUME_CONTROL
from telegram_bot.core.bot_danso_recovery import OFFER, NOTIFIED
from test_session_provider import bare_bot, make_manager, make_update


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def snapshot(state='pending_provider', allowed=False):
    return RecoverySnapshot('a' * 64, state, allowed, 'Fix the test', 'Edited a file; tests remain', 2, 13)


def diagnostic(state='pending_provider', reason='uncertain_work', allowed=False, action='new_session'):
    return dict(version=1, state=state, reason=reason, resume_allowed=allowed, action=action)


def failure(records):
    return _failure(('DANSO_ERROR={"version":1,"category":"runtime","exit_code":2}\n'
                     + '\n'.join('DANSO_RECOVERY=' + r for r in records)).encode(), 2).message


def test_native_recovery_metadata_and_legacy_fallback():
    assert '/task_recover' in failure([json.dumps(diagnostic())])
    assert '/task_resume' in failure([json.dumps(diagnostic('paused', 'explicit_resume_required', True, 'resume_task'))])
    assert 'task state' not in failure([]).lower()
    assert '/task_recover' not in failure([json.dumps(diagnostic())] * 2)


@pytest.mark.parametrize('change', [
    {'version': True}, {'resume_allowed': 1}, {'state': 'PRIVATE'},
    {'reason': 'explicit_resume_required'}, {'action': 'resume_task'},
    {'resume_allowed': True}, {'extra': 'PRIVATE'}, {'state': []},
])
def test_malformed_recovery_does_not_leak_or_authorize(change):
    text = failure([json.dumps(diagnostic() | change)])
    assert 'PRIVATE' not in text and '/task_recover' not in text and '/task_resume' not in text


def test_duplicate_keys_and_untrusted_error_text_are_not_advice():
    value = json.dumps(diagnostic())[:-1] + ',"action":"new_session"}'
    assert '/task_recover' not in failure([value])
    assert '/task_recover' not in failure(['PRIVATE'])
    with pytest.raises(ValueError):
        recovery_record(diagnostic('completed', 'terminal_task', True, 'resume_task'))


def test_summary_redacts_before_truncating_and_does_not_invent_completion():
    rows = [
        {'type': 'message', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'old request'}]}},
        {'customType': 'danso.operation.v1', 'data': {'state': 'settled'}},
        {'type': 'message', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'fix api_key=' + 'x'*60}]}},
        {'type': 'message', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'tests pending'}]}},
        {'customType': 'danso.operation.v1', 'data': {'state': 'started'}},
    ]
    result = _summarize('\n'.join(map(json.dumps, rows)).encode(), snapshot())
    assert 'x'*20 not in result.task
    assert result.settled_tools == 0
    assert '완료 검증 아님' in result.render()
    assert 'reference data' in result.continuation() and 'Do not replay' in result.continuation()


async def setup_bot(tmp_path, state='pending_provider', allowed=False):
    manager = make_manager(tmp_path, 'danso')
    await manager.patch_session('7:9', updates={'provider': 'danso', 'session_id': 'sid'})
    handler = SimpleNamespace(inspect_danso_recovery=AsyncMock(return_value=snapshot(state, allowed)),
                              process_message=AsyncMock(return_value=SimpleNamespace(success=True, content='done', session_id='new-sid')))
    bot = bare_bot(manager, provider='danso', project_chat=handler)
    bot._config.danso_long_task_enabled = True
    bot._config.allowed_user_ids = [7]
    bot._config.bridge_memory_mode = 'off'
    bot.application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    bot._save_session_id = AsyncMock()
    bot._send_smart = AsyncMock()
    bot._codex_approval_policy = Mock(return_value=None)
    bot._codex_approvals_reviewer = Mock(return_value=None)
    bot._codex_sandbox_policy = Mock(return_value=None)
    bot._codex_approval_callback = AsyncMock()
    bot._make_status_callback = Mock(return_value=None)

    async def enqueue(key, run, overflow):
        await run()
    bot._enqueue_user_task = enqueue
    return bot, manager, handler


def callback(token, action='continue', user=7, chat=9):
    update = make_update(user_id=user, chat_id=chat)
    update.callback_query = SimpleNamespace(edit_message_text=AsyncMock())
    return update, f'drecover:{token}:{action}'


@pytest.mark.anyio
async def test_startup_is_read_only_deduplicated_and_delivery_failure_retries(tmp_path):
    bot, manager, handler = await setup_bot(tmp_path)
    bot.application.bot.send_message.side_effect = RuntimeError('synthetic')
    await bot._recover_danso_tasks(bot.application)
    assert NOTIFIED not in await manager.get_session('7:9')
    bot.application.bot.send_message.side_effect = None
    await bot._recover_danso_tasks(bot.application)
    await bot._recover_danso_tasks(bot.application)
    assert bot.application.bot.send_message.await_count == 2
    handler.process_message.assert_not_awaited()
    assert (await manager.get_session('7:9'))['session_id'] == 'sid'


@pytest.mark.anyio
@pytest.mark.parametrize(('state', 'allowed'), [('paused', True), ('ready', True), ('failed', False), ('pending_tools', False), ('pending_provider', False)])
async def test_continue_is_one_shot_and_selects_resume_or_evidence_first_new_task(tmp_path, state, allowed):
    bot, manager, handler = await setup_bot(tmp_path, state, allowed)
    assert await bot._offer_danso_recovery('7:9', 7, 9)
    offer = (await manager.get_session('7:9'))[OFFER]
    update, data = callback(offer['token'])
    await bot._handle_danso_recovery(update, data)
    await bot._handle_danso_recovery(update, data)
    handler.process_message.assert_awaited_once()
    kwargs = handler.process_message.call_args.kwargs
    assert kwargs['resume_task'] is allowed and kwargs['new_session'] is not allowed
    assert kwargs['session_id'] == ('sid' if allowed else None)
    assert (kwargs['user_message'] == TASK_RESUME_CONTROL) is allowed
    if not allowed:
        assert 'First inspect' in kwargs['user_message']
    assert kwargs['dispatch_guard']()


@pytest.mark.anyio
@pytest.mark.parametrize('action', ['new', 'view'])
async def test_new_and_status_do_not_call_provider(tmp_path, action):
    bot, manager, handler = await setup_bot(tmp_path)
    await bot._offer_danso_recovery('7:9', 7, 9)
    offer = (await manager.get_session('7:9'))[OFFER]
    update, data = callback(offer['token'], action)
    await bot._handle_danso_recovery(update, data)
    handler.process_message.assert_not_awaited()
    stored = await manager.get_session('7:9')
    assert stored['session_id'] == (None if action == 'new' else 'sid')


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['user', 'chat', 'provider', 'journal', 'new', 'queue', 'typing'])
async def test_stale_or_foreign_choices_never_dispatch(tmp_path, change):
    bot, manager, handler = await setup_bot(tmp_path)
    await bot._offer_danso_recovery('7:9', 7, 9)
    token = (await manager.get_session('7:9'))[OFFER]['token']
    update, data = callback(token, user=8 if change == 'user' else 7, chat=10 if change == 'chat' else 9)
    if change == 'provider':
        bot._config.agent_provider = 'codex'
    elif change == 'journal':
        handler.inspect_danso_recovery.return_value = replace(snapshot(), fingerprint='b'*64)
    elif change == 'new':
        await manager.patch_session('7:9', updates={'session_id': None, 'new_session': True})
    elif change == 'queue':
        async def enqueue(key, run, overflow):
            bot._bump_task_resume_generation(key)
            await run()
        bot._enqueue_user_task = enqueue
    elif change == 'typing':
        async def changed(*args, **kwargs):
            bot._bump_task_resume_generation('7:9')
        update.callback_query.edit_message_text.side_effect = changed
    await bot._handle_danso_recovery(update, data)
    handler.process_message.assert_not_awaited()


@pytest.mark.anyio
async def test_offer_creation_cas_and_dispatch_guard_run_under_store_lock(tmp_path):
    bot, manager, handler = await setup_bot(tmp_path)
    await asyncio.gather(bot._offer_danso_recovery('7:9', 7, 9), bot._offer_danso_recovery('7:9', 7, 9))
    assert bot.application.bot.send_message.await_count == 1
    allowed = True
    await manager.store._lock.acquire()
    pending = asyncio.create_task(manager.patch_session_if('7:9', expected={}, updates={'session_id': 'wrong'}, guard=lambda: allowed))
    await asyncio.sleep(0)
    allowed = False
    manager.store._lock.release()
    assert not await pending
    assert (await manager.get_session('7:9'))['session_id'] == 'sid'


@pytest.mark.anyio
async def test_startup_respects_owner_and_route_and_never_scans_other_audience(tmp_path):
    bot, manager, handler = await setup_bot(tmp_path)
    await manager.patch_session('8:9', updates={'provider': 'danso', 'session_id': 'other'})
    await manager.patch_session('0:0', updates={'provider': 'danso', 'session_id': 'shared'})
    await bot._recover_danso_tasks(bot.application)
    handler.inspect_danso_recovery.assert_awaited_once_with('sid', 7, 9)


@pytest.mark.anyio
async def test_restart_reoffers_consumed_but_not_dispatched_choice(tmp_path):
    bot, manager, handler = await setup_bot(tmp_path)
    await bot._recover_danso_tasks(bot.application)
    await manager.patch_session('7:9', remove_fields={OFFER})
    await bot._recover_danso_tasks(bot.application)
    assert bot.application.bot.send_message.await_count == 2
    handler.process_message.assert_not_awaited()


@pytest.mark.anyio
async def test_native_inspection_refuses_a_journal_that_changes_between_reads(monkeypatch):
    from telegram_bot.core import danso_recovery as recovery
    session = SimpleNamespace(runtime=SimpleNamespace(root='/fixture'), session_id='sid',
                              _read_task_status=AsyncMock(return_value=snapshot()))
    reads = iter([(b'first', None), (b'second', None)])
    monkeypatch.setattr(recovery, '_read_locked', lambda *args: next(reads))
    with pytest.raises(ValueError, match='changed'):
        await recovery.inspect_session(session)


@pytest.mark.anyio
async def test_epoch_change_during_store_commit_does_not_bind_new_session(tmp_path):
    from telegram_bot.__main__ import _build_session_started_recorder
    bot, manager, handler = await setup_bot(tmp_path)
    settings = SimpleNamespace(agent_provider='danso', telegram_session_scope='per-user-chat')
    recorder = _build_session_started_recorder(settings, manager)
    allowed = True
    await manager.store._lock.acquire()
    pending = asyncio.create_task(recorder(7, 9, 'new-id', dispatch_guard=lambda: allowed))
    await asyncio.sleep(0)
    allowed = False
    manager.store._lock.release()
    with pytest.raises(ValueError, match='expired'):
        await pending
    assert (await manager.get_session('7:9'))['session_id'] == 'sid'


@pytest.mark.anyio
async def test_audience_change_rejects_before_reading_another_route(tmp_path):
    bot, manager, handler = await setup_bot(tmp_path)
    bot._danso_recovery_route = Mock(return_value=['private', 'scope-a'])
    await bot._offer_danso_recovery('7:9', 7, 9)
    token = (await manager.get_session('7:9'))[OFFER]['token']
    bot._danso_recovery_route.return_value = ['private', 'scope-b']
    handler.inspect_danso_recovery.reset_mock()
    update, data = callback(token)
    await bot._handle_danso_recovery(update, data)
    handler.inspect_danso_recovery.assert_not_awaited()
    handler.process_message.assert_not_awaited()


# ── #1690: opt: and /task_resume must offer recovery after a Danso failure ──

def opt_update():
    update = make_update(user_id=7, chat_id=9)
    update.callback_query = SimpleNamespace(
        id='cb-1', data='opt:yes', answer=AsyncMock(), edit_message_text=AsyncMock())
    return update


def danso_response(success):
    return SimpleNamespace(success=success, content='done' if success else 'boom',
                           has_options=False, streamed=False)


def make_app():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(),
                                               send_chat_action=AsyncMock()))


async def setup_opt_bot(tmp_path, response):
    bot, manager, handler = await setup_bot(tmp_path)
    session = await manager.get_session('7:9')
    handler.process_message = AsyncMock(return_value=response)
    bot._maybe_capture_outside_approval = AsyncMock()
    bot._switch_provider_if_needed = AsyncMock(return_value=(session, False))
    bot._effective_session_id = lambda key, current: current.get('session_id')
    bot.application = make_app()
    return bot, manager, handler


@pytest.mark.anyio
async def test_opt_failure_offers_recovery_after_the_response_never_recalls(tmp_path):
    bot, manager, handler = await setup_opt_bot(tmp_path, danso_response(False))
    order = []
    bot._send_smart = AsyncMock(side_effect=lambda *a, **k: order.append('send'))
    handler.inspect_danso_recovery = AsyncMock(
        side_effect=lambda *a, **k: (order.append('inspect'), snapshot('failed'))[1])
    await bot._handle_callback(opt_update(), SimpleNamespace(application=bot.application))
    handler.process_message.assert_awaited_once()
    assert order == ['send', 'inspect'], 'failure reply must precede the offer'
    assert OFFER in await manager.get_session('7:9')
    bot.application.bot.send_message.assert_awaited_once()


@pytest.mark.anyio
async def test_opt_success_and_non_danso_failure_never_offer(tmp_path):
    bot, manager, handler = await setup_opt_bot(tmp_path, danso_response(True))
    await bot._handle_callback(opt_update(), SimpleNamespace(application=bot.application))
    handler.process_message.assert_awaited_once()
    assert OFFER not in await manager.get_session('7:9')
    bot.application.bot.send_message.assert_not_awaited()

    claude_manager = make_manager(tmp_path / 'claude', 'claude')
    await claude_manager.patch_session('7:9', updates={'provider': 'claude', 'session_id': 'sid'})
    claude_bot = bare_bot(claude_manager, provider='claude', project_chat=SimpleNamespace(
        process_message=AsyncMock(return_value=danso_response(False)),
        inspect_danso_recovery=AsyncMock(return_value=snapshot('failed'))))
    claude_bot._config.danso_long_task_enabled = True
    claude_bot._config.allowed_user_ids = [7]
    claude_bot._config.bridge_memory_mode = 'off'
    claude_bot.application = make_app()
    claude_bot._save_session_id = AsyncMock()
    claude_bot._send_smart = AsyncMock()
    claude_bot._maybe_capture_outside_approval = AsyncMock()
    claude_bot._effective_session_id = lambda key, current: current.get('session_id')
    claude_bot._switch_provider_if_needed = AsyncMock(
        return_value=(await claude_manager.get_session('7:9'), False))
    claude_bot._enqueue_user_task = bot._enqueue_user_task
    await claude_bot._handle_callback(opt_update(), SimpleNamespace(application=claude_bot.application))
    claude_bot._project_chat.process_message.assert_awaited_once()
    claude_bot._project_chat.inspect_danso_recovery.assert_not_awaited()
    claude_bot.application.bot.send_message.assert_not_awaited()


@pytest.mark.anyio
async def test_task_resume_failure_offers_recovery_after_the_reply(tmp_path):
    bot, manager, handler = await setup_bot(tmp_path)
    handler.process_message = AsyncMock(return_value=danso_response(False))
    bot._effective_session_id = lambda key, current: current.get('session_id')
    bot._reply_smart = AsyncMock()
    bot._tasks = SimpleNamespace(active=Mock(return_value=None))
    bot._switch_provider_if_needed = AsyncMock(
        return_value=(await manager.get_session('7:9'), False))
    await bot._cmd_task_resume(make_update(user_id=7, chat_id=9), SimpleNamespace(args=[]))
    handler.process_message.assert_awaited_once()
    assert handler.process_message.await_args.kwargs.get('resume_task') is True
    bot._reply_smart.assert_awaited_once()
    assert OFFER in await manager.get_session('7:9')
    bot.application.bot.send_message.assert_awaited_once()


@pytest.mark.anyio
async def test_task_resume_binding_change_mid_call_never_offers(tmp_path):
    bot, manager, handler = await setup_bot(tmp_path)
    bot._effective_session_id = lambda key, current: current.get('session_id')

    async def failing_but_session_moves(**kwargs):
        await manager.patch_session('7:9', updates={'new_session': True})
        return danso_response(False)

    handler.process_message = AsyncMock(side_effect=failing_but_session_moves)
    bot._reply_smart = AsyncMock()
    bot._tasks = SimpleNamespace(active=Mock(return_value=None))
    bot._switch_provider_if_needed = AsyncMock(
        return_value=(await manager.get_session('7:9'), False))
    await bot._cmd_task_resume(make_update(user_id=7, chat_id=9), SimpleNamespace(args=[]))
    bot._reply_smart.assert_awaited_once()
    assert OFFER not in await manager.get_session('7:9')
    bot.application.bot.send_message.assert_not_awaited()


def test_recovery_summary_discloses_unknown_usage_and_repeat_cost():
    result = replace(snapshot('paused', True), unknown_usage_requests=1,
                     interruption_reason='signal_termination')
    assert '토큰 사용량은 미확인' in result.render()
    assert '비용이 추가' in result.render()
