"""Interim bubbles must arrive while the owned CLI is blocked, not at EOF."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.danso_runtime import build_danso_runtime
from telegram_bot.core.project_chat import ProjectChatHandler
from test_danso_runtime import configured as configured
from test_project_chat_codex import _settings


@pytest.fixture
def anyio_backend():
    return 'asyncio'


SCRIPT = '''#!/usr/bin/python3
import json, os, sys, time
from pathlib import Path
args=sys.argv[1:]
if '--help' in args:
 print('--progress-jsonl --long-task --resume-task --task-status --task-stage-requests --task-max-requests --task-max-tokens --task-repeat-limit --task-pause-after-stage --task-progress --tool-home')
 raise SystemExit(0)
root=Path(args[args.index('--cwd')+1])
journal=Path(args[args.index('--session')+1])
(root/'argv.json').write_text(json.dumps(args))
(root/'pid').write_text(str(os.getpid()))
fd=os.open(journal,os.O_WRONLY|os.O_CREAT,0o600)
os.close(fd)
def emit(record): print(json.dumps(record),flush=True)
def message(role, content, **kw): emit(dict(type='message',message=dict(role=role,content=content,**kw)))
emit(dict(type='session',version=3))
if '--long-task' in args:
 print('DANSO_TASK='+json.dumps(dict(version=1,state='checkpoint',stage=0,requests=0,reported_tokens=0,elapsed_seconds=0)),file=sys.stderr,flush=True)
message('user',[dict(type='text',text='PRIVATE_USER_INPUT')])
message('assistant',[dict(type='text',text='설정을 확인했고 검증을 실행합니다.'),dict(type='thinking',thinking='PRIVATE_REASONING'),dict(type='toolCall',id='t1',name='bash',arguments=dict(command='PRIVATE_COMMAND'))],stopReason='toolUse')
emit(dict(type='danso_progress',version=1,sequence=1,phase='started',tool='bash'))
# No more stdout/stderr until the test confirms a delivered interim bubble.
for _ in range(600):
 if (root/'release').exists(): break
 time.sleep(.01)
else: raise SystemExit(9)
message('toolResult',[dict(type='text',text='PRIVATE_TOOL_RESULT')])
emit(dict(type='danso_progress',version=1,sequence=1,phase='settled',tool='bash',success=True))
message('assistant',[dict(type='text',text='검증이 끝났습니다.')],stopReason='stop')
usage=dict(requests=2,inputTokens=10,outputTokens=3,cacheReadTokens=4,cacheWriteTokens=0,totalTokens=17)
for prefix in ('DANSO_USAGE','PIRI_USAGE'): print(prefix+'='+json.dumps(usage),file=sys.stderr,flush=True)
'''


@pytest.mark.anyio
@pytest.mark.parametrize('long_task, outcome', [(False, 'success'), (True, 'success'),
                                             (True, 'paused'), (False, 'cancel')])
async def test_completed_interim_bubble_before_final_without_draft_streaming(configured, long_task, outcome):
    binary = Path(configured.danso_cli_path)
    script = SCRIPT
    if outcome == 'paused':
        script = script.replace("message('assistant',[dict(type='text',text='검증이 끝났습니다.')],stopReason='stop')",
            "print('DANSO_TASK='+json.dumps(dict(version=1,state='paused',stage=1,requests=2,reported_tokens=17,elapsed_seconds=1)),file=sys.stderr,flush=True)\n"
            "print('DANSO_ERROR='+json.dumps(dict(version=1,category='request_budget',exit_code=2)),file=sys.stderr,flush=True)\n"
            "raise SystemExit(2)")
    binary.write_text(script)
    selected = configured.model_copy(update={'danso_long_task_enabled': long_task})
    runtime = build_danso_runtime(selected)
    assert runtime.progress_jsonl
    settings = _settings(Path(selected.danso_workspace), 'danso')
    assert not settings.enable_streaming
    handler = ProjectChatHandler(settings=settings, agent_runtime=runtime)
    delivered = asyncio.Event()
    send = AsyncMock()

    async def interim(text):
        await send(chat_id=9, text=text)
        delivered.set()

    task = asyncio.create_task(handler.process_message('inspect and verify', 7, 9,
                                interim_message_callback=interim))
    try:
        await asyncio.wait_for(delivered.wait(), 3)
        assert not task.done(), 'bubble was buffered until completion'
        send.assert_awaited_once_with(chat_id=9, text='설정을 확인했고 검증을 실행합니다.')
        argv = json.loads((Path(selected.danso_workspace)/'argv.json').read_text())
        assert '--progress-jsonl' in argv and '-p' not in argv
        assert ('--long-task' in argv) == long_task
        if outcome == 'cancel':
            pid = int((Path(selected.danso_workspace)/'pid').read_text())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not Path(f'/proc/{pid}').exists()
            send.assert_awaited_once()
            return
        (Path(selected.danso_workspace)/'release').touch()
        response = await asyncio.wait_for(task, 3)
        if outcome == 'paused':
            assert not response.success and response.failure_class == 'danso_task_paused'
            assert '/task_resume' in response.content
        else:
            assert response.success, response.error
            assert response.content == '검증이 끝났습니다.'
        assert 'PRIVATE' not in str(send.call_args_list) + response.content
        send.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await handler.close()


def test_progress_default_opt_out_and_legacy_capability(configured):
    assert configured.danso_progress_enabled
    assert not build_danso_runtime(configured).progress_jsonl  # legacy fixture lacks flag
    Path(configured.danso_cli_path).write_text(SCRIPT)
    assert build_danso_runtime(configured).progress_jsonl
    disabled = configured.model_copy(update={'danso_progress_enabled': False})
    assert not build_danso_runtime(disabled).progress_jsonl


def test_documented_environment_opt_out(configured, tmp_path):
    loaded = type(configured).load(project_root=configured.project_root,
        bot_env_file=tmp_path/'absent-progress-env', environ={
            'TELEGRAM_BOT_TOKEN': '123456:synthetic', 'ALLOWED_USER_IDS': '[7]',
            'CCC_DANSO_PROGRESS_ENABLED': 'false',
        })
    assert not loaded.danso_progress_enabled


@pytest.mark.anyio
async def test_checkpoint_is_not_starved_by_continuous_stdout(configured):
    from telegram_bot.core.agent_runtime import SessionRequest
    binary = Path(configured.danso_cli_path)
    body = '''def emit(record): print(json.dumps(record),flush=True)
def checkpoint(stage):
 print('DANSO_TASK='+json.dumps(dict(version=1,state='checkpoint',stage=stage,requests=stage,reported_tokens=stage,elapsed_seconds=stage)),file=sys.stderr,flush=True)
emit(dict(type='session',version=3))
checkpoint(0)
for seq in range(1,201):
 emit(dict(type='danso_progress',version=1,sequence=seq,phase='started',tool='bash'))
 emit(dict(type='danso_progress',version=1,sequence=seq,phase='settled',tool='bash',success=True))
 if seq==2: checkpoint(1)
emit(dict(type='message',message=dict(role='assistant',stopReason='stop',content=[dict(type='text',text='done')])))
usage=dict(requests=2,inputTokens=10,outputTokens=3,cacheReadTokens=4,cacheWriteTokens=0,totalTokens=17)
for prefix in ('DANSO_USAGE','PIRI_USAGE'): print(prefix+'='+json.dumps(usage),file=sys.stderr,flush=True)
'''
    binary.write_text(SCRIPT[:SCRIPT.index('def emit')] + body)
    runtime = build_danso_runtime(configured.model_copy(update={'danso_long_task_enabled': True}))
    session = await runtime.start_or_resume(SessionRequest(working_directory=configured.danso_workspace))
    tools_seen, checkpoint_seen = 0, False
    async for event in session.send_turn('continuous work'):
        if event.kind in {'tool_started', 'tool_completed'}:
            tools_seen += 1
            await asyncio.sleep(.001)
        if event.kind == 'task_progress' and event.stage == 1:
            assert tools_seen < 50, 'checkpoint was buffered behind stdout'
            checkpoint_seen = True
    assert checkpoint_seen and tools_seen == 400
    assert event.kind == 'completion'
