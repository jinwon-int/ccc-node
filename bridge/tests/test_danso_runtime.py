"""Offline Telegram -> Danso subprocess tests; no real provider credentials."""
import asyncio
import json
import os
from pathlib import Path
import signal
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from telegram_bot.core.agent_runtime import SessionRequest
from telegram_bot.core.danso_runtime import build_danso_runtime, probe_danso_readiness
from telegram_bot.core.project_chat import ProjectChatHandler
from telegram_bot.utils.config import Settings
from test_project_chat_codex import _settings
from test_session_provider import bare_bot, make_manager, make_update


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    real_which = shutil.which
    # These tests exercise the bridge with a fake CLI, not a sandbox installation.
    monkeypatch.setattr("telegram_bot.core.danso_runtime.shutil.which",
                        lambda name: "/usr/bin/bwrap" if name == "bwrap" else real_which(name))
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    bridge_root = tmp_path / "bridge"
    bridge_root.mkdir(mode=0o700)
    (bridge_root / ".telegram_bot").mkdir(mode=0o700)
    binary = tmp_path / "danso"
    binary.write_text('''#!/usr/bin/python3
import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
root=Path(args[args.index('--cwd')+1])
(root/'argv.json').write_text(json.dumps(args))
(root/'environment.json').write_text(json.dumps(sorted(os.environ)))
journal=Path(args[args.index('--session')+1])
fd=os.open(journal,os.O_APPEND|os.O_WRONLY|os.O_CREAT,0o600)
with os.fdopen(fd,'a') as f:f.write('fixture turn\\n')
message=args[-1]
if message == 'wait':
 (root/'pid').write_text(str(os.getpid()))
 time.sleep(30)
if message == 'slow':time.sleep(.1)
usage={'requests':2,'inputTokens':10,'outputTokens':3,'cacheReadTokens':4,'cacheWriteTokens':0,'totalTokens':17}
if message == 'invalid':usage['totalTokens']=99
for prefix in ('DANSO_USAGE','PIRI_USAGE'):print(prefix+'='+json.dumps(usage),file=sys.stderr)
if message == 'fail':
 print('PRIVATE_PROVIDER_BODY',file=sys.stderr)
 print('DANSO_ERROR='+json.dumps({'version':1,'category':'provider','exit_code':3}),file=sys.stderr)
 sys.exit(3)
print('completed')
''')
    binary.chmod(0o700)
    settings = Settings.load(project_root=bridge_root, bot_env_file=tmp_path/'absent', environ={
        'TELEGRAM_BOT_TOKEN': '123456:synthetic', 'ALLOWED_USER_IDS': '[7]',
        'CCC_AGENT_PROVIDER': 'danso', 'CCC_DANSO_CLI_PATH': str(binary),
        'CCC_DANSO_WORKSPACE': str(workspace),
        'CCC_DANSO_STATE_DIR': str(tmp_path/'private'), 'OPENAI_API_KEY': 'fixture-key',
        'CCC_DANSO_TIMEOUT_SECONDS': '3',
    })
    return settings


@pytest.mark.anyio
async def test_telegram_turn_resume_default_effort_and_usage(configured):
    runtime = build_danso_runtime(configured)
    settings = _settings(Path(configured.danso_workspace), 'danso')
    settings.turn_admission_timeout_seconds = .01
    settings.turn_admission_retries = 1
    handler = ProjectChatHandler(settings=settings, agent_runtime=runtime)
    handler._usage_meter = Mock()
    response = await handler.process_message('slow', 7, 9,
        approval_policy='untrusted', sandbox_policy={'type':'dangerFullAccess'})
    assert response.success, response.error
    assert 'completed' in response.content
    argv = json.loads((Path(configured.danso_workspace)/'argv.json').read_text())
    assert argv[argv.index('--reasoning-effort')+1] == 'medium'
    assert argv[argv.index('--provider')+1] == 'openai'
    assert '--unsafe-no-sandbox' not in argv
    assert '--compact-at-bytes' in argv
    handler._usage_meter.record.assert_called_once_with('danso','interactive',requests=2,input_tokens=14,output_tokens=3)
    journal = runtime.root/(response.session_id+'.jsonl')
    before = journal.read_bytes()
    restarted = build_danso_runtime(configured)
    session = await restarted.start_or_resume(SessionRequest(working_directory=str(configured.danso_workspace),session_id=response.session_id,effort='high'))
    events = [e async for e in session.send_turn('next')]
    assert events[-1].kind == 'completion'
    assert journal.read_bytes() == before + b'fixture turn\n'
    assert journal.stat().st_mode & 0o777 == 0o600
    await handler.close()


@pytest.mark.anyio
@pytest.mark.parametrize('message,code',[('fail','danso_provider'),('invalid','danso_adapter_error')])
async def test_failure_is_terminal_private_and_never_retried(configured,message,code):
    runtime = build_danso_runtime(configured)
    settings = _settings(Path(configured.danso_workspace), 'danso')
    settings.turn_admission_retries = 2
    settings.turn_admission_retry_timeout_seconds = 1
    handler = ProjectChatHandler(settings=settings,agent_runtime=runtime)
    response = await handler.process_message(message,7,9)
    assert not response.success
    assert 'PRIVATE_PROVIDER_BODY' not in repr(response)
    assert len(list(runtime.root.glob('*.jsonl'))) == 1
    assert next(runtime.root.glob('*.jsonl')).read_text() == 'fixture turn\n'
    # Also verify the adapter's native error mapping, not inferred text.
    session = await runtime.start_or_resume(SessionRequest(working_directory=str(configured.danso_workspace)))
    events = [e async for e in session.send_turn(message)]
    assert len(events) == 1 and events[0].code == code and not events[0].retryable
    await handler.close()


@pytest.mark.anyio
@pytest.mark.parametrize('cancel',[False,True])
async def test_interrupt_or_cancel_reaps_child_and_preserves_journal(configured,cancel):
    runtime = build_danso_runtime(configured)
    session = await runtime.start_or_resume(SessionRequest(working_directory=str(configured.danso_workspace)))
    async def collect():
        return [e async for e in session.send_turn('wait')]
    task=asyncio.create_task(collect())
    marker=Path(configured.danso_workspace)/'pid'
    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(.01)
    assert marker.exists()
    pid=int(marker.read_text())
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await session.interrupt()
        events=await task
        assert events[0].code == 'danso_cancelled'
    with pytest.raises(ProcessLookupError):
        os.kill(pid,signal.SIGCONT)
    assert (runtime.root/(session.session_id+'.jsonl')).exists()


@pytest.mark.anyio
async def test_environment_is_explicit_and_model_options_are_accurate(configured,monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','PRIVATE_UNRELATED_SECRET')
    runtime=build_danso_runtime(configured)
    models=await runtime.list_models()
    assert models[0].id == 'gpt-6-astra'
    assert models[0].default_reasoning_effort == 'medium'
    assert 'none' not in models[0].supported_reasoning_efforts
    request=SessionRequest(working_directory=str(configured.danso_workspace))
    session=await runtime.start_or_resume(request)
    assert [e async for e in session.send_turn('ok')][-1].kind == 'completion'
    names=json.loads((Path(configured.danso_workspace)/'environment.json').read_text())
    assert 'TELEGRAM_BOT_TOKEN' not in names
    assert set(runtime.environment) == {'PATH','HOME','OPENAI_API_KEY'}
    assert configured.openai_api_key not in repr(configured)


@pytest.mark.parametrize('field,value',[
    ('bridge_memory_mode','curated'),('bridge_memory_mode','audience-scoped'),
    ('memory_distill_provider','claude'),('memory_distill_provider','codex'),('memory_distill_provider','piri'),
    ('danso_model','wrong-model'),('openai_api_key',None),('danso_state_dir','relative'),
    ('danso_cli_path','/nonexistent/danso'),('process_timeout_seconds',1),
    ('danso_workspace',None),('danso_workspace','relative'),
])
def test_preflight_rejects_unsupported_configuration_without_writes(configured,field,value):
    settings=configured.model_copy(update={field:value})
    ok,reason=probe_danso_readiness(settings)
    assert not ok and reason
    assert not Path(configured.danso_state_dir).exists()


def test_private_state_and_missing_bwrap_fail_closed(configured,monkeypatch):
    state=Path(configured.danso_state_dir)
    state.symlink_to(configured.danso_workspace,target_is_directory=True)
    assert not probe_danso_readiness(configured)[0]
    assert not (Path(configured.danso_workspace)/'journals').exists()
    other=configured.model_copy(update={'danso_state_dir':str(state.parent/'other')})
    monkeypatch.setattr('telegram_bot.core.danso_runtime.shutil.which',lambda name:None if name=='bwrap' else configured.danso_cli_path)
    assert not probe_danso_readiness(other)[0]


@pytest.mark.anyio
@pytest.mark.parametrize('kwargs',[{'effort':'none'},{'model':'wrong'}, {'memory_environment':{}}, {'sandbox_policy':{}}, {'approval_policy':'untrusted'}, {'session_id':'../../other'}, {'session_id':'11111111-1111-1111-1111-111111111111'}])
async def test_invalid_session_policy_or_missing_resume_never_launches(configured,kwargs):
    runtime=build_danso_runtime(configured)
    with pytest.raises((ValueError,OSError)):
        await runtime.start_or_resume(SessionRequest(working_directory=str(configured.danso_workspace),**kwargs))
    assert not (Path(configured.danso_workspace)/'argv.json').exists()


@pytest.mark.anyio
async def test_commands_and_persistence_are_danso_scoped(configured,tmp_path):
    runtime=build_danso_runtime(configured)
    manager=make_manager(tmp_path,'danso')
    project_chat=SimpleNamespace(list_runtime_models=AsyncMock(side_effect=runtime.list_models))
    bot=bare_bot(manager,provider='danso',project_chat=project_chat)
    bot._config.danso_model='gpt-6-astra'
    await manager.store.set('7:9',{'provider':'danso','session_id':'current'})
    assert bot._effective_session_id('7:9',await manager.get_session('7:9')) == 'current'
    update=make_update()
    await bot._cmd_model(update,SimpleNamespace(args=[]))
    assert update.message.replies[0][1]['reply_markup'].inline_keyboard[0][0].callback_data == 'model:danso:gpt-6-astra'
    await bot._cmd_effort(update,SimpleNamespace(args=['high']))
    assert (await manager.get_session('7:9'))['effort'] == 'high'
    await bot._cmd_effort(update,SimpleNamespace(args=['none']))
    assert (await manager.get_session('7:9'))['effort'] == 'high'
    before=await manager.get_session('7:9')
    await bot._cmd_model(update,SimpleNamespace(args=['wrong']))
    await bot._cmd_resume(update,SimpleNamespace(args=['foreign']))
    assert await manager.get_session('7:9') == before
    await bot._cmd_resume(update,SimpleNamespace(args=[]))
    assert 'auto-resumes: current' in update.message.replies[-1][0]
    await bot._cmd_history(update,SimpleNamespace())
    assert 'Danso does not expose' in update.message.replies[-1][0]
    await bot._cmd_revert(update,SimpleNamespace())
    assert 'unavailable for Danso' in update.message.replies[-1][0]
    other=await manager.get_session('7:10')
    assert 'session_id' not in other


def test_application_composes_danso_without_other_provider(configured):
    from telegram_bot.__main__ import build_context, create_app
    context = build_context(configured)
    assert context.agent_runtime.model == "gpt-6-astra"
    assert context.agent_runtime.default_effort == "medium"
    assert context.distill_extraction_worker is None
    bot = create_app(context)
    assert bot._active_provider() == "danso"
    assert bot._probe_agent_readiness() == (True, "")


def test_health_renderer_names_danso():
    from telegram_bot.utils.health_render import _agent_label
    assert _agent_label("danso") == "Danso"


@pytest.mark.anyio
async def test_first_failed_turn_persists_identity_before_tools_and_restart(configured):
    from telegram_bot.__main__ import build_context
    context = build_context(configured)
    context.session_manager.initialize()
    response = await context.project_chat.process_message("fail", 7, 9)
    assert not response.success
    stored = await context.session_manager.get_session("7:9")
    assert stored["session_id"] == response.session_id
    assert stored["provider"] == "danso"
    restarted = build_context(configured)
    restarted.session_manager.initialize()
    recovered = await restarted.session_manager.get_session("7:9")
    assert recovered["session_id"] == response.session_id
    response2 = await restarted.project_chat.process_message("ok", 7, 9, session_id=recovered["session_id"])
    assert response2.session_id == response.session_id and response2.success
    journal = context.agent_runtime.root / (response.session_id + ".jsonl")
    assert journal.read_text() == "fixture turn\nfixture turn\n"
    await context.project_chat.close()
    await restarted.project_chat.close()


@pytest.mark.anyio
async def test_session_persistence_failure_prevents_process_launch(configured):
    runtime = build_danso_runtime(configured)
    recorder = AsyncMock(side_effect=OSError("synthetic storage failure"))
    handler = ProjectChatHandler(settings=_settings(Path(configured.danso_workspace), "danso"),
                                 agent_runtime=runtime, session_started_recorder=recorder)
    response = await handler.process_message("ok", 7, 9)
    assert not response.success
    recorder.assert_awaited_once()
    assert not (Path(configured.danso_workspace) / "argv.json").exists()
    await handler.close()


@pytest.mark.anyio
async def test_danso_does_not_abandon_journal_on_automatic_daily_reset(tmp_path):
    from datetime import datetime, timedelta, timezone
    manager = make_manager(tmp_path, "danso")
    manager.settings.auto_new_session_after_hours = 24
    now = datetime.now(timezone.utc)
    await manager.set_last_user_message_at("7:9", now - timedelta(days=2))
    assert not await manager.should_start_new_session("7:9", now=now)


def test_usage_fallback_never_labels_danso_as_claude():
    from telegram_bot.core.usage import UsageSnapshot, render_usage
    report = render_usage(UsageSnapshot(provider="danso"))
    assert "Danso" in report and "Claude" not in report


@pytest.mark.parametrize("protected", ["project_root", "bot_data_dir"])
def test_task_workspace_cannot_expose_bridge_control_or_credential_files(configured, protected):
    settings = configured.model_copy(update={"danso_workspace": str(getattr(configured, protected))})
    ok, reason = probe_danso_readiness(settings)
    assert not ok and "bridge configuration or session storage" in reason
    assert not Path(configured.danso_state_dir).exists()


def test_health_writer_and_renderer_preserve_danso_identity(tmp_path):
    from telegram_bot.utils.health import RuntimeHealthReporter
    from telegram_bot.utils.health_render import render_status_lines
    reporter = RuntimeHealthReporter(tmp_path, agent_provider="danso")
    reporter.record_agent_ok()
    state = json.loads((tmp_path / "health.json").read_text())
    assert state["agent"]["provider"] == "danso"
    assert reporter.snapshot()["agent"]["provider"] == "danso"
    assert "Danso" in str(render_status_lines(tmp_path / "health.json", str(os.getpid()), 90, "danso"))


@pytest.mark.anyio
async def test_missing_journal_is_explicit_and_new_session_is_the_recovery(configured):
    from telegram_bot.__main__ import build_context
    context = build_context(configured)
    context.session_manager.initialize()
    binary = Path(configured.danso_cli_path)
    normal = binary.read_text()
    binary.write_text("#!/bin/sh\nexit 2\n")
    failed = await context.project_chat.process_message("ok", 7, 9)
    assert not failed.success
    stored = await context.session_manager.get_session("7:9")
    assert stored["session_id"] == failed.session_id
    assert not list(context.agent_runtime.root.glob("*.jsonl"))
    binary.write_text(normal)
    resumed = await context.project_chat.process_message("ok", 7, 9, session_id=stored["session_id"])
    assert not resumed.success and "use /new" in resumed.error
    assert not (Path(configured.danso_workspace) / "argv.json").exists()
    fresh = await context.project_chat.process_message("ok", 7, 9, new_session=True)
    assert fresh.success and fresh.session_id != stored["session_id"]
    await context.project_chat.close()


def test_workspace_cannot_expose_package_or_redirected_bot_env(configured, monkeypatch):
    import telegram_bot.core.danso_runtime as module
    package = Path(module.__file__).resolve().parents[1]
    assert not probe_danso_readiness(configured.model_copy(update={"danso_workspace": str(package)}))[0]
    monkeypatch.setenv("CCC_BOT_ENV_FILE", str(Path(configured.danso_workspace) / "bot.env"))
    assert not probe_danso_readiness(configured)[0]
