"""Audience boundaries and real local materialization; no provider calls."""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.agent_runtime import SessionRequest
from telegram_bot.core.danso_runtime import build_danso_runtime, probe_danso_readiness
from telegram_bot.core.memory_audience import MemoryAudience, audience_from_danso_environment
from test_danso_runtime import configured, anyio_backend  # noqa: F401


@pytest.fixture
def memory_settings(configured, tmp_path, monkeypatch):  # noqa: F811
    loader = tmp_path / 'loader.py'
    loader.write_text('''#!/usr/bin/python3
import os,json
print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext':
 'PRIVATE_SENTINEL' if os.environ['CCC_MEMORY_AUDIENCE']=='private' else 'SHARED_SENTINEL'}}))
''')
    loader.chmod(0o700)
    monkeypatch.setenv('CCC_CODEX_MEMORY_LOADER', str(loader))
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'UNRELATED_SECRET')
    return configured.model_copy(update={
        'bridge_memory_mode': 'audience-scoped',
        'codex_memory_materializer_path': str(Path(__file__).resolve().parents[2] / 'scripts/ccc_codex_memory.py'),
        'bridge_memory_audience_root': str(tmp_path / 'audiences'),
    })


def route(settings, scope='private-'+'a'*32):
    return MemoryAudience('shared' if scope == 'shared' else 'private', scope,
                          Path(settings.bridge_memory_audience_root)).danso_environment(settings)


def request(settings, environment, session_id=None):
    return SessionRequest(working_directory=settings.danso_workspace,
                          memory_environment=environment, session_id=session_id)


@pytest.mark.anyio
async def test_real_materializer_refresh_and_scoped_resume(memory_settings):
    settings = memory_settings
    assert probe_danso_readiness(settings)[0]
    runtime = build_danso_runtime(settings)
    private = route(settings)
    session = await runtime.start_or_resume(request(settings, private))
    for _ in range(2):
        events = [e async for e in session.send_turn('synthetic')]
        assert events[-1].kind == 'completion', events
        argv = json.loads((Path(settings.danso_workspace)/'argv.json').read_text())
        path = Path(argv[argv.index('--system-context-file')+1])
        text = path.read_text()
        assert 'PRIVATE_SENTINEL' in text
        assert 'Automatic memory' in text
        assert 'CCC_STATE_DIR/working-state' not in text
        assert 'PRIVATE_SENTINEL' not in json.dumps(argv)
        assert set(session.runtime.environment) == {'PATH','HOME','OPENAI_API_KEY'}
    resumed = await build_danso_runtime(settings).start_or_resume(request(settings, private, session.session_id))
    assert resumed.session_id == session.session_id
    for env in (route(settings, 'shared'), route(settings, 'private-'+'b'*32)):
        with pytest.raises(ValueError, match='journal is unavailable'):
            await runtime.start_or_resume(request(settings, env, session.session_id))
    shared = await runtime.start_or_resume(request(settings, route(settings, 'shared')))
    assert [e async for e in shared.send_turn('synthetic')][-1].kind == 'completion'
    shared_text = Path(route(settings,'shared')['CCC_DANSO_BOOTSTRAP_CONTEXT_FILE']).read_text()
    assert 'SHARED_SENTINEL' in shared_text and 'PRIVATE_SENTINEL' not in shared_text
    assert private['CCC_MEMORY_LEGACY_PRIVATE_READS'] == '1'
    assert route(settings,'shared')['CCC_MEMORY_LEGACY_PRIVATE_READS'] == '0'


@pytest.mark.anyio
async def test_audience_runtime_preserves_tool_home_and_private_provider_home(memory_settings, tmp_path):
    tool_home = tmp_path / 'audience-tool-home'
    tool_home.mkdir(mode=0o700)
    settings = memory_settings.model_copy(update={'danso_tool_home': str(tool_home)})
    runtime = build_danso_runtime(settings)
    session = await runtime.start_or_resume(request(settings, route(settings)))
    events = [event async for event in session.send_turn('audience tool')]
    assert events[-1].kind == 'completion'
    argv = json.loads((Path(settings.danso_workspace) / 'argv.json').read_text())
    assert argv[argv.index('--tool-home') + 1] == str(tool_home)
    environment = json.loads((Path(settings.danso_workspace) / 'environment-values.json').read_text())
    assert environment['HOME'] == runtime.environment['HOME']
    assert environment['HOME'] != str(tool_home)


@pytest.mark.anyio
async def test_missing_tampered_route_rejected_before_journal_creation(memory_settings):
    runtime = build_danso_runtime(memory_settings)
    env = route(memory_settings)
    for candidate in (None, {**env,'CCC_MEMORY_SCOPE':'../escape'},
                      {**env,'CCC_MEMORY_AUDIENCE':'shared'},
                      {**env,'CCC_DANSO_BOOTSTRAP_CONTEXT_FILE':'/tmp/other'},
                      {**env,'DANSO_CHATGPT_AUTH_FILE':'/tmp/secret'}):
        with pytest.raises(ValueError):
            await runtime.start_or_resume(request(memory_settings,candidate))
    assert not list(runtime.root.iterdir())


@pytest.mark.anyio
async def test_refresh_failure_does_not_use_old_snapshot_or_launch(memory_settings,monkeypatch):
    runtime = build_danso_runtime(memory_settings)
    session = await runtime.start_or_resume(request(memory_settings, route(memory_settings)))
    assert [e async for e in session.send_turn('first')][-1].kind == 'completion'
    argv_path = Path(memory_settings.danso_workspace)/'argv.json'
    before = argv_path.stat().st_mtime_ns
    monkeypatch.setattr('telegram_bot.core.danso_memory._run_materializer_command', AsyncMock(return_value=False))
    events = [e async for e in session.send_turn('second')]
    assert events[0].code == 'danso_adapter_error'
    assert argv_path.stat().st_mtime_ns == before


@pytest.mark.anyio
async def test_interrupt_during_refresh_never_starts_native(memory_settings,monkeypatch):
    session = await build_danso_runtime(memory_settings).start_or_resume(request(memory_settings,route(memory_settings)))
    assert [e async for e in session.send_turn('prime')][-1].kind == 'completion'
    argv_path = Path(memory_settings.danso_workspace)/'argv.json'
    before = argv_path.stat().st_mtime_ns
    entered, release = asyncio.Event(), asyncio.Event()
    async def refresh(*args, **kwargs):
        entered.set()
        await release.wait()
        return True
    monkeypatch.setattr('telegram_bot.core.danso_memory._run_materializer_command',refresh)
    async def collect(): return [e async for e in session.send_turn('synthetic')]
    task = asyncio.create_task(collect())
    await entered.wait()
    await session.interrupt()
    release.set()
    assert (await task)[0].code == 'danso_cancelled'
    assert argv_path.stat().st_mtime_ns == before


@pytest.mark.parametrize('scope',['shared','private-'+'a'*32])
def test_route_round_trip_and_no_codex_credential_dependency(memory_settings,scope):
    env = route(memory_settings,scope)
    assert audience_from_danso_environment(memory_settings,env).scope == scope
    assert not any(k in env for k in ('CODEX_HOME','PIRI_CODING_AGENT_SESSION_DIR','OPENAI_API_KEY'))


@pytest.mark.anyio
async def test_telegram_handler_resolves_private_memory_route(memory_settings):
    from telegram_bot.core.project_chat import ProjectChatHandler
    handler = ProjectChatHandler(settings=memory_settings, agent_runtime=build_danso_runtime(memory_settings))
    try:
        response = await handler.process_message('synthetic', 7, 7)
        assert response.success, response.error
        argv = json.loads((Path(memory_settings.danso_workspace)/'argv.json').read_text())
        path = Path(argv[argv.index('--system-context-file')+1])
        assert path.parent.parent.parent.name.startswith('private-')
        assert 'PRIVATE_SENTINEL' in path.read_text()
    finally:
        await handler.close()


@pytest.mark.anyio
async def test_canonical_loader_blocks_global_promises_and_detached_refresh(memory_settings, tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / 'operator-home'
    hooks = home / '.claude/hooks'
    hooks.mkdir(parents=True, mode=0o700)
    marker = tmp_path / 'unexpected-refresh'
    (hooks / 'refresh-memory.sh').write_text(f'#!/bin/bash\necho ran > {marker}\n')
    waits = home / '.telegram_bot/external-wait'
    waits.mkdir(parents=True)
    (waits / 'waits.json').write_text(json.dumps({'w1': {'wait_id':'w1', 'state':'monitoring',
        'repo':'private/repo','pr_number':7,'summary':'PRIVATE_PROMISE_SENTINEL'}}))
    settings = memory_settings.model_copy(update={'claude_settings_path':home/'.claude/settings.json'})
    monkeypatch.setenv('CCC_CODEX_MEMORY_LOADER', str(repo/'claude/hooks/load-memory.sh'))
    monkeypatch.setenv('CCC_EXTERNAL_WAIT_HOME', str(waits))
    monkeypatch.setenv('CCC_MEMORY_INJECT_PENDING_PROMISES','1')
    monkeypatch.setenv('CCC_MEMORY_NO_REFRESH','0')
    shared = route(settings,'shared')
    memory = Path(shared['CCC_MEMORY_DIR'])
    memory.mkdir(parents=True, mode=0o700)
    (memory/'MEMORY.md').write_text('SHARED_CANONICAL_SENTINEL')
    session = await build_danso_runtime(settings).start_or_resume(request(settings,shared))
    path = await session.runtime.system_context_loader()
    text = path.read_text()
    assert 'SHARED_CANONICAL_SENTINEL' in text
    assert 'PRIVATE_PROMISE_SENTINEL' not in text and 'private/repo' not in text
    await asyncio.sleep(.1)
    assert not marker.exists()


@pytest.mark.anyio
async def test_loader_environment_does_not_inherit_credentials_or_native_home(memory_settings,tmp_path,monkeypatch):
    captured = {}
    async def materialize(path, command, timeout, *, environment):
        captured.update(environment)
        file = Path(environment['CCC_DANSO_BOOTSTRAP_CONTEXT_FILE'])
        file.write_text('safe')
        file.chmod(0o600)
        return True
    monkeypatch.setattr('telegram_bot.core.danso_memory._run_materializer_command',materialize)
    for key in ('OPENAI_API_KEY','DANSO_CHATGPT_AUTH_FILE','TELEGRAM_BOT_TOKEN','CODEX_AUTH_TOKEN'):
        monkeypatch.setenv(key,'PRIVATE_CREDENTIAL_SENTINEL')
    monkeypatch.setenv('HOME',str(tmp_path/'native-home'))
    session = await build_danso_runtime(memory_settings).start_or_resume(request(memory_settings,route(memory_settings)))
    await session.runtime.system_context_loader()
    assert 'PRIVATE_CREDENTIAL_SENTINEL' not in json.dumps(captured)
    assert captured['HOME'] == str(Path(memory_settings.claude_settings_path).parent.parent)
    assert captured['HOME'] != str(tmp_path/'native-home')
    assert captured['CCC_MEMORY_NO_REFRESH'] == '1'
    assert captured['CCC_MEMORY_INJECT_PENDING_PROMISES'] == '0'
    assert captured['CCC_MEMORY_INJECT_DETACHED_JOBS'] == '0'


@pytest.mark.anyio
@pytest.mark.parametrize('cancel',[False,True])
async def test_interrupt_reaps_real_materializer_and_loader(memory_settings,tmp_path,monkeypatch,cancel):
    pidfile=tmp_path/'loader-pid'
    loader=tmp_path/'hanging.py'
    loader.write_text(f"import os,time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n")
    monkeypatch.setenv('CCC_CODEX_MEMORY_LOADER',str(loader))
    session=await build_danso_runtime(memory_settings).start_or_resume(request(memory_settings,route(memory_settings)))
    async def collect(): return [e async for e in session.send_turn('synthetic')]
    task=asyncio.create_task(collect())
    deadline=time.monotonic()+3
    while not pidfile.exists() and time.monotonic()<deadline:
        await asyncio.sleep(.01)
    assert pidfile.exists()
    pid=int(pidfile.read_text())
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,2)
    else:
        await asyncio.wait_for(session.interrupt(),2)
        assert (await asyncio.wait_for(task,2))[0].code == 'danso_cancelled'
    status=Path(f'/proc/{pid}/stat')
    assert not status.exists() or status.read_text().split()[2]=='Z'
    assert not (Path(memory_settings.danso_workspace)/'argv.json').exists()
