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
from telegram_bot.core.danso_worker import TASK_RESUME_CONTROL
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
if '--help' in args:
 print(' '.join(['--long-task','--resume-task','--task-stage-requests',
                 '--task-status',
                 '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                 '--task-pause-after-stage','--task-progress','--tool-home']))
 raise SystemExit(0)
if '--task-status' in args:
 session=Path(args[args.index('--session')+1])
 print(json.dumps({'version':1,'kind':'long_task_status','state':'paused',
       'session_id':session.stem,'stage':1,'elapsed_ms':1000,
       'limits':{'wall_seconds':21600,'stage_requests':16,
                 'max_requests':1024,'max_tokens':10000000,'repeat_limit':3},
       'usage':{'requests':2,'reported_tokens':17},'pending':None,
       'resume_allowed':True}))
 raise SystemExit(0)
root=Path(args[args.index('--cwd')+1])
(root/'argv.json').write_text(json.dumps(args))
(root/'environment.json').write_text(json.dumps(sorted(os.environ)))
(root/'environment-values.json').write_text(json.dumps({key: os.environ.get(key) for key in ('HOME', 'PATH')}))
journal=Path(args[args.index('--session')+1])
fd=os.open(journal,os.O_APPEND|os.O_WRONLY|os.O_CREAT,0o600)
with os.fdopen(fd,'a') as f:f.write('fixture turn\\n')
message=args[-1]
if '--task-progress' in args:
 print('DANSO_TASK='+json.dumps({'version':1,'state':'checkpoint','stage':1,
       'requests':2,'reported_tokens':17,'elapsed_seconds':1}),file=sys.stderr)
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
        'CCC_DANSO_LONG_TASK_ENABLED': 'false',
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
    assert argv[argv.index('--sandbox') + 1] == 'host'
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
async def test_compaction_default_and_explicit_override_reach_native_cli(configured, tmp_path):
    runtime = build_danso_runtime(configured)
    assert configured.danso_provider_timeout_seconds == 180
    assert configured.danso_long_task_enabled is False
    assert configured.danso_long_task_timeout_seconds == 21600
    assert runtime.provider_timeout == 180
    session = await runtime.start_or_resume(SessionRequest(working_directory=configured.danso_workspace))
    events = [event async for event in session.send_turn("ok")]
    assert events[-1].kind == "completion"
    argv = json.loads((Path(configured.danso_workspace) / "argv.json").read_text())
    assert configured.danso_compact_at_bytes == 128 * 1024
    assert argv[argv.index("--compact-at-bytes") + 1] == str(128 * 1024)

    overridden = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-override",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:synthetic",
            "ALLOWED_USER_IDS": "[7]",
            "CCC_AGENT_PROVIDER": "danso",
            "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
            "CCC_DANSO_WORKSPACE": configured.danso_workspace,
            "CCC_DANSO_STATE_DIR": str(tmp_path / "private-override"),
            "OPENAI_API_KEY": "fixture-key",
            "CCC_DANSO_TIMEOUT_SECONDS": "3",
            "CCC_DANSO_PROVIDER_TIMEOUT_SECONDS": "42",
            "CCC_DANSO_COMPACT_AT_BYTES": "32768",
        },
    )
    runtime = build_danso_runtime(overridden)
    assert overridden.danso_provider_timeout_seconds == 42
    assert runtime.provider_timeout == 42
    session = await runtime.start_or_resume(SessionRequest(working_directory=overridden.danso_workspace))
    events = [event async for event in session.send_turn("ok")]
    assert events[-1].kind == "completion"
    argv = json.loads((Path(overridden.danso_workspace) / "argv.json").read_text())
    assert argv[argv.index("--compact-at-bytes") + 1] == "32768"
    assert argv[argv.index("--provider-timeout-seconds") + 1] == "42"


@pytest.mark.anyio
async def test_optional_tool_home_reaches_native_child_without_replacing_provider_home(configured, tmp_path):
    tool_home = tmp_path / "tool-home"
    tool_home.mkdir(mode=0o700)
    settings = configured.model_copy(update={"danso_tool_home": str(tool_home)})
    runtime = build_danso_runtime(settings)
    assert runtime.tool_home == str(tool_home)
    assert runtime.environment["HOME"] == str(Path(settings.danso_state_dir) / "home")
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    events = [event async for event in session.send_turn("tool-home")]
    assert events[-1].kind == "completion"
    argv = json.loads((Path(settings.danso_workspace) / "argv.json").read_text())
    assert argv[argv.index("--tool-home") + 1] == str(tool_home)
    environment = json.loads((Path(settings.danso_workspace) / "environment-values.json").read_text())
    assert environment["HOME"] == runtime.environment["HOME"]
    assert environment["HOME"] != str(tool_home)


def test_tool_home_is_optional_host_only_and_probed_only_when_configured(configured, tmp_path):
    assert configured.danso_tool_home is None
    tool_home = tmp_path / "tool-home"
    tool_home.mkdir(mode=0o700)
    selected = configured.model_copy(update={"danso_tool_home": str(tool_home)})
    assert probe_danso_readiness(selected) == (True, "")

    relative = configured.model_copy(update={"danso_tool_home": "tool-home"})
    ready, reason = probe_danso_readiness(relative)
    assert not ready and "absolute" in reason

    invalid_component = configured.model_copy(update={"danso_tool_home": "/tmp/tool:home"})
    ready, reason = probe_danso_readiness(invalid_component)
    assert not ready and "PATH component" in reason

    bubblewrap = selected.model_copy(update={"danso_sandbox": "bubblewrap"})
    ready, reason = probe_danso_readiness(bubblewrap)
    assert not ready and "host" in reason

    old_binary = tmp_path / "old-danso"
    old_binary.write_text("#!/bin/sh\nprintf '%s\\n' old-help\n")
    old_binary.chmod(0o700)
    old_default = configured.model_copy(update={"danso_cli_path": str(old_binary)})
    assert probe_danso_readiness(old_default) == (True, "")
    old_selected = old_default.model_copy(update={"danso_tool_home": str(tool_home)})
    ready, reason = probe_danso_readiness(old_selected)
    assert not ready and "--tool-home" in reason


@pytest.mark.anyio
async def test_default_long_task_profile_forwards_budgets_progress_and_explicit_resume(configured, tmp_path):
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-long-task",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:synthetic",
            "ALLOWED_USER_IDS": "[7]",
            "CCC_AGENT_PROVIDER": "danso",
            "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
            "CCC_DANSO_WORKSPACE": configured.danso_workspace,
            "CCC_DANSO_STATE_DIR": str(tmp_path / "long-private"),
            "OPENAI_API_KEY": "fixture-key",
            "CCC_DANSO_TASK_PAUSE_AFTER_STAGE": "1",
        },
    )
    runtime = build_danso_runtime(settings)
    assert settings.danso_provider_timeout_seconds == 180
    assert settings.danso_long_task_enabled is True
    assert settings.danso_timeout_seconds == 3600
    assert settings.process_timeout_seconds == 21660
    assert runtime.timeout == 21600
    assert runtime.outer_timeout == 21660
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    first = [event async for event in session.send_turn("long task")]
    assert first[0].kind == "task_progress"
    assert first[-1].kind == "completion"
    argv = json.loads((Path(settings.danso_workspace) / "argv.json").read_text())
    for flag, value in (
        ("--task-stage-requests", "16"),
        ("--task-max-requests", "1024"),
        ("--task-max-tokens", "10000000"),
        ("--task-repeat-limit", "3"),
        ("--task-pause-after-stage", "1"),
    ):
        assert argv[argv.index(flag) + 1] == value
    assert "--long-task" in argv and "--task-progress" in argv
    assert "--resume-task" not in argv
    before = (Path(runtime.root) / (session.session_id + ".jsonl")).read_bytes()

    spoof = [event async for event in session.send_turn(TASK_RESUME_CONTROL)]
    assert len(spoof) == 1 and spoof[0].code == "danso_input"
    assert json.loads((Path(settings.danso_workspace) / "argv.json").read_text()) == argv

    session.authorize_task_resume()
    resumed = [event async for event in session.send_turn(TASK_RESUME_CONTROL)]
    assert resumed[0].kind == "task_progress"
    assert resumed[-1].kind == "completion"
    resume_argv = json.loads((Path(settings.danso_workspace) / "argv.json").read_text())
    assert "--resume-task" in resume_argv
    assert "--" not in resume_argv
    assert (Path(runtime.root) / (session.session_id + ".jsonl")).read_bytes() == before + b"fixture turn\n"

    # Exercise the provider-neutral event router as well as the adapter: a
    # checkpoint must refresh the heartbeat state without becoming answer text.
    handler = ProjectChatHandler(settings=settings, agent_runtime=runtime)
    response = await handler.process_message("long task through handler", 7, 9)
    assert response.success, response.error
    assert response.content == "completed"
    await handler.close()


@pytest.mark.anyio
async def test_resume_inherits_saved_task_limits_and_remaining_deadline(configured, tmp_path):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-status',
                    '--task-stage-requests','--task-max-requests','--task-max-tokens',
                    '--task-repeat-limit','--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
session = Path(args[args.index('--session') + 1])
if '--task-status' in args:
    print(json.dumps({'version': 1, 'kind': 'long_task_status', 'state': 'paused',
          'session_id': session.stem, 'stage': 4, 'elapsed_ms': 3000,
          'limits': {'wall_seconds': 17, 'stage_requests': 4,
                     'max_requests': 5, 'max_tokens': 500, 'repeat_limit': 2},
          'usage': {'requests': 2, 'reported_tokens': 100}, 'pending': None,
          'resume_allowed': True}))
    raise SystemExit(0)
root = Path(args[args.index('--cwd') + 1])
(root / 'argv.json').write_text(json.dumps(args))
fd = open(session, 'a')
fd.write('fixture turn\\n')
fd.close()
stage = 4 if '--resume-task' in args else 0
requests = 2 if stage else 0
tokens = 100 if stage else 0
print('DANSO_TASK=' + json.dumps({'version': 1, 'state': 'checkpoint',
      'stage': stage, 'requests': requests, 'reported_tokens': tokens,
      'elapsed_seconds': 3 if stage else 0}), file=sys.stderr)
usage = {'requests': 1, 'inputTokens': 1, 'outputTokens': 0,
         'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'totalTokens': 1}
for prefix in ('DANSO_USAGE', 'PIRI_USAGE'):
    print(prefix + '=' + json.dumps(usage), file=sys.stderr)
print('completed')
""")
    binary.chmod(0o700)
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / 'absent-inherited-limits',
        environ={
            'TELEGRAM_BOT_TOKEN': '123456:synthetic', 'ALLOWED_USER_IDS': '[7]',
            'CCC_AGENT_PROVIDER': 'danso', 'CCC_DANSO_CLI_PATH': configured.danso_cli_path,
            'CCC_DANSO_WORKSPACE': configured.danso_workspace,
            'CCC_DANSO_STATE_DIR': str(tmp_path / 'inherited-limits'),
            'OPENAI_API_KEY': 'fixture-key', 'CCC_DANSO_LONG_TASK_ENABLED': 'true',
            'CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS': '3', 'CLAUDE_PROCESS_TIMEOUT': '30',
            'CCC_DELEGATED_TASK_STALL_SECONDS': '10',
        },
    )
    runtime = build_danso_runtime(settings)
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    first = [event async for event in session.send_turn('first')]
    assert first[-1].kind == 'completion'
    session.authorize_task_resume()
    resumed = [event async for event in session.send_turn(TASK_RESUME_CONTROL)]
    assert resumed[-1].kind == 'completion'
    argv = json.loads((Path(settings.danso_workspace) / 'argv.json').read_text())
    assert '--resume-task' in argv and '--task-progress' in argv
    assert '--timeout-seconds' not in argv
    for flag in ('--task-stage-requests', '--task-max-requests',
                 '--task-max-tokens', '--task-repeat-limit',
                 '--task-pause-after-stage'):
        assert flag not in argv


@pytest.mark.anyio
async def test_resume_refuses_when_outer_deadline_cannot_cover_saved_task(configured, tmp_path):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-status',
                    '--task-stage-requests','--task-max-requests','--task-max-tokens',
                    '--task-repeat-limit','--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
session = Path(args[args.index('--session') + 1])
if '--task-status' in args:
    print(json.dumps({'version': 1, 'kind': 'long_task_status', 'state': 'paused',
          'session_id': session.stem, 'stage': 1, 'elapsed_ms': 0,
          'limits': {'wall_seconds': 17, 'stage_requests': 4,
                     'max_requests': 5, 'max_tokens': 500, 'repeat_limit': 2},
          'usage': {'requests': 1, 'reported_tokens': 1}, 'pending': None,
          'resume_allowed': True}))
    raise SystemExit(0)
root = Path(args[args.index('--cwd') + 1])
if '--resume-task' in args:
    (root / 'provider-dispatch').write_text('unexpected')
fd = open(session, 'a')
fd.write('fixture turn\\n')
fd.close()
usage = {'requests': 1, 'inputTokens': 1, 'outputTokens': 0,
         'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'totalTokens': 1}
for prefix in ('DANSO_USAGE', 'PIRI_USAGE'):
    print(prefix + '=' + json.dumps(usage), file=sys.stderr)
print('completed')
""")
    binary.chmod(0o700)
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / 'absent-short-outer',
        environ={
            'TELEGRAM_BOT_TOKEN': '123456:synthetic', 'ALLOWED_USER_IDS': '[7]',
            'CCC_AGENT_PROVIDER': 'danso', 'CCC_DANSO_CLI_PATH': configured.danso_cli_path,
            'CCC_DANSO_WORKSPACE': configured.danso_workspace,
            'CCC_DANSO_STATE_DIR': str(tmp_path / 'short-outer'),
            'OPENAI_API_KEY': 'fixture-key', 'CCC_DANSO_LONG_TASK_ENABLED': 'true',
            'CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS': '3', 'CLAUDE_PROCESS_TIMEOUT': '13',
            'CCC_DELEGATED_TASK_STALL_SECONDS': '10',
        },
    )
    runtime = build_danso_runtime(settings)
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    assert [event async for event in session.send_turn('first')][-1].kind == 'completion'
    session.authorize_task_resume()
    events = [event async for event in session.send_turn(TASK_RESUME_CONTROL)]
    assert len(events) == 1 and events[0].code == 'danso_task_resume_unavailable'
    assert not (Path(settings.danso_workspace) / 'provider-dispatch').exists()


@pytest.mark.anyio
async def test_reserved_resume_marker_never_dispatches_without_explicit_resume(configured):
    runtime = build_danso_runtime(configured)
    handler = ProjectChatHandler(settings=configured, agent_runtime=runtime)

    response = await handler.process_message(TASK_RESUME_CONTROL, 7, 9)

    assert not response.success
    assert response.error == "danso_input"
    assert not (Path(configured.danso_workspace) / "argv.json").exists()
    await handler.close()


@pytest.mark.anyio
async def test_native_paused_checkpoint_is_explicit_resume_error(configured, tmp_path):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, sys
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-stage-requests',
                    '--task-status',
                    '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                    '--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
stage0 = args[-1] == 'stage0'
code = 2 if stage0 else 3
print('DANSO_TASK=' + json.dumps({'version': 1, 'state': 'paused',
      'stage': 0 if stage0 else 2,
      'requests': 0 if stage0 else (1024 if args[-1] == 'exhaust' else 8),
      'reported_tokens': 100, 'elapsed_seconds': 1}), file=sys.stderr)
print('DANSO_ERROR=' + json.dumps({'version': 1, 'category': 'request_budget',
      'exit_code': code}), file=sys.stderr)
raise SystemExit(code)
""")
    binary.chmod(0o700)
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-paused",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:synthetic",
            "ALLOWED_USER_IDS": "[7]",
            "CCC_AGENT_PROVIDER": "danso",
            "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
            "CCC_DANSO_WORKSPACE": configured.danso_workspace,
            "CCC_DANSO_STATE_DIR": str(tmp_path / "paused-private"),
            "OPENAI_API_KEY": "fixture-key",
            "CCC_DANSO_LONG_TASK_ENABLED": "true",
            "CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS": "3",
            "CLAUDE_PROCESS_TIMEOUT": "13",
            "CCC_DELEGATED_TASK_STALL_SECONDS": "10",
        },
    )
    runtime = build_danso_runtime(settings)
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    events = [event async for event in session.send_turn("pause")]
    assert [event.kind for event in events] == ["task_progress", "error"]
    assert events[-1].code == "danso_task_paused"
    assert "/task_resume" in events[-1].message
    exhausted = [event async for event in session.send_turn("exhaust")]
    assert exhausted[-1].code == "danso_task_paused"
    assert "/task_resume" not in exhausted[-1].message
    assert "remaining budget" in exhausted[-1].message
    stage_zero = [event async for event in session.send_turn("stage0")]
    assert stage_zero[-1].code == "danso_task_paused"
    assert "/task_resume" in stage_zero[-1].message
    handler = ProjectChatHandler(settings=settings, agent_runtime=runtime)
    response = await handler.process_message("pause through handler", 7, 9)
    assert not response.success
    assert response.content.startswith("⏸ Paused at a saved checkpoint.")
    assert "/task_resume" in response.content
    await handler.close()


def test_long_task_preflight_requires_outer_deadline_and_new_cli(configured, tmp_path):
    base = {
        "TELEGRAM_BOT_TOKEN": "123456:synthetic",
        "ALLOWED_USER_IDS": "[7]",
        "CCC_AGENT_PROVIDER": "danso",
        "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
        "CCC_DANSO_WORKSPACE": configured.danso_workspace,
        "CCC_DANSO_STATE_DIR": str(tmp_path / "long-preflight"),
        "OPENAI_API_KEY": "fixture-key",
        "CCC_DANSO_LONG_TASK_ENABLED": "true",
    }
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-preflight",
        environ={**base, "CLAUDE_PROCESS_TIMEOUT": "21600"},
    )
    ready, reason = probe_danso_readiness(settings)
    assert not ready and "LONG_TASK_TIMEOUT_SECONDS" in reason
    assert not Path(settings.danso_state_dir).exists()

    old_binary = tmp_path / "old-danso"
    old_binary.write_text("#!/bin/sh\nprintf '%s\\n' old-help\n")
    old_binary.chmod(0o700)
    old = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-old",
        environ={**base, "CCC_DANSO_CLI_PATH": str(old_binary), "CLAUDE_PROCESS_TIMEOUT": "21660"},
    )
    ready, reason = probe_danso_readiness(old)
    assert not ready and "long-task CLI" in reason

    invalid_budget = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-invalid-budget",
        environ={
            **base,
            "CLAUDE_PROCESS_TIMEOUT": "21660",
            "CCC_DANSO_TASK_STAGE_REQUESTS": "17",
            "CCC_DANSO_TASK_MAX_REQUESTS": "16",
        },
    )
    ready, reason = probe_danso_readiness(invalid_budget)
    assert not ready and "STAGE_REQUESTS" in reason

    with pytest.raises(ValueError, match="CCC_DANSO_TASK_REPEAT_LIMIT"):
        Settings.load(
            project_root=configured.project_root,
            bot_env_file=tmp_path / "absent-invalid-repeat",
            environ={
                **base,
                "CLAUDE_PROCESS_TIMEOUT": "21660",
                "CCC_DANSO_TASK_REPEAT_LIMIT": "1",
            },
        )


def test_transport_metadata_is_strict_optional_and_body_free():
    from telegram_bot.core.danso_worker import _failure, _transport

    record = json.dumps({
        "version": 1, "phase": "response_body", "elapsed_ms": 180001,
        "request_bytes": 30502, "attempts": 2,
    }, separators=(",", ":"))
    stderr = (
        'DANSO_ERROR={"version":1,"category":"provider_timeout","exit_code":3}\n'
        f"DANSO_TRANSPORT={record}\n"
    ).encode()
    event = _failure(stderr, 3)
    assert event.code == "danso_provider_timeout"
    assert ("phase=response_body, elapsed_ms=180001, request_bytes=30502, "
            "attempts=2" in event.message)
    assert _transport(stderr.decode(), "provider_timeout", 2) is None

    base = 'DANSO_ERROR={"version":1,"category":"provider","exit_code":3}\n'
    records = [
        '{"version":1,"phase":"response_body","elapsed_ms":1,"request_bytes":1,"extra":"PRIVATE"}',
        '{"version":1,"phase":"PRIVATE","elapsed_ms":1,"request_bytes":1}',
        '{"version":1,"phase":"response_body","elapsed_ms":true,"request_bytes":1}',
        '{"version":1,"phase":"response_body","elapsed_ms":1,"request_bytes":524289}',
        '{"version":1,"phase":"response_body","elapsed_ms":1,"request_bytes":1,"request_bytes":2}',
        '{"version":1,"phase":"response_body","elapsed_ms":1,"request_bytes":1',
        # The wire-retry attempt count is 1-based; zero and non-int refuse.
        '{"version":1,"phase":"response_body","elapsed_ms":1,"request_bytes":1,"attempts":0}',
        '{"version":1,"phase":"response_body","elapsed_ms":1,"request_bytes":1,"attempts":true}',
        # A pre-attempts record (4 keys) is a stale schema: ignored, not fatal.
        '{"version":1,"phase":"response_body","elapsed_ms":1,"request_bytes":1}',
    ]
    for item in records:
        event = _failure((base + "DANSO_TRANSPORT=" + item + "\nPRIVATE_URL").encode(), 3)
        assert event.code == "danso_provider"
        assert "phase=" not in event.message
        assert "PRIVATE" not in event.message

    provider_exit_two = (
        'DANSO_ERROR={"version":1,"category":"provider","exit_code":2}\n'
        f"DANSO_TRANSPORT={record}\n"
    ).encode()
    event = _failure(provider_exit_two, 2)
    assert event.code == "danso_provider"
    assert "phase=" not in event.message


def test_task_progress_is_strict_bounded_and_body_free():
    from telegram_bot.core.danso_worker import _task_progress

    valid = (
        'DANSO_TASK={"version":1,"state":"checkpoint","stage":2,'
        '"requests":7,"reported_tokens":99,"elapsed_seconds":10}'
    )
    event = _task_progress(valid)
    assert event is not None
    assert (event.state, event.stage, event.requests, event.reported_tokens) == (
        "checkpoint", 2, 7, 99
    )
    malformed = [
        valid.replace('"state":"checkpoint"', '"state":"private"'),
        valid.replace('"version":1', '"version":2'),
        valid.replace('"stage":2', '"stage":true'),
        valid.replace('"stage":2', '"stage":-1'),
        valid.replace('"elapsed_seconds":10', '"elapsed_seconds":10,"extra":"secret"'),
        valid.replace('"stage":2', '"stage":2,"stage":3'),
    ]
    assert all(_task_progress(item) is None for item in malformed)


@pytest.mark.anyio
@pytest.mark.parametrize("prefix", ["DANSO_TRANSPORT=", "DANSO_PROVIDER="])
async def test_transport_record_on_success_is_adapter_failure(configured, prefix):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, sys
print('done')
usage = {'requests': 1, 'inputTokens': 1, 'outputTokens': 0,
         'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'totalTokens': 1}
for prefix in ('DANSO_USAGE', 'PIRI_USAGE'):
    print(prefix + '=' + json.dumps(usage), file=sys.stderr)
print('DANSO_TRANSPORT=' + json.dumps({'version': 1, 'phase': 'connect',
      'elapsed_ms': 1, 'request_bytes': 1, 'attempts': 1}), file=sys.stderr)
""".replace('DANSO_TRANSPORT=', prefix))
    binary.chmod(0o700)
    runtime = build_danso_runtime(configured)
    session = await runtime.start_or_resume(SessionRequest(working_directory=configured.danso_workspace))
    events = [event async for event in session.send_turn("success-with-diagnostic")]
    assert [event.kind for event in events] == ["error"]
    assert events[0].code == "danso_adapter_error"


@pytest.mark.anyio
async def test_reader_overflow_terminates_owned_process_promptly(configured):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import os, sys, time
from pathlib import Path
args = sys.argv[1:]
root = Path(args[args.index('--cwd') + 1])
(root / 'pid').write_text(str(os.getpid()))
os.write(1, b'x' * (1024 * 1024 + 1))
time.sleep(30)
""")
    binary.chmod(0o700)
    runtime = build_danso_runtime(configured)
    session = await runtime.start_or_resume(SessionRequest(working_directory=configured.danso_workspace))
    marker = Path(configured.danso_workspace) / "pid"
    async def collect():
        return [event async for event in session.send_turn("overflow")]

    events = await asyncio.wait_for(collect(), timeout=2)
    assert len(events) == 1 and events[0].code == "danso_adapter_error"
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, signal.SIGCONT)


@pytest.mark.anyio
async def test_oversized_reserved_progress_line_fails_closed(configured):
    configured.danso_long_task_enabled = True
    configured.danso_long_task_timeout_seconds = 3
    configured.process_timeout_seconds = 13
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import os, sys, time
from pathlib import Path
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-stage-requests',
                    '--task-status',
                    '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                    '--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
root = Path(args[args.index('--cwd') + 1])
(root / 'pid').write_text(str(os.getpid()))
os.write(2, b'DANSO_TASK=' + b'x' * (64 * 1024) + b'\\n')
time.sleep(30)
""")
    binary.chmod(0o700)
    runtime = build_danso_runtime(configured)
    session = await runtime.start_or_resume(SessionRequest(working_directory=configured.danso_workspace))
    events = [event async for event in session.send_turn("oversized progress")]
    assert len(events) == 1 and events[0].code == "danso_adapter_error"
    with pytest.raises(ProcessLookupError):
        os.kill(int((Path(configured.danso_workspace) / "pid").read_text()), signal.SIGCONT)


@pytest.mark.anyio
async def test_malformed_reserved_progress_lines_count_toward_stderr_cap(configured):
    configured.danso_long_task_enabled = True
    configured.danso_long_task_timeout_seconds = 3
    configured.process_timeout_seconds = 13
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import os, sys, time
from pathlib import Path
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-status','--task-stage-requests',
                    '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                    '--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
root = Path(args[args.index('--cwd') + 1])
(root / 'pid').write_text(str(os.getpid()))
os.write(2, b'DANSO_TASK=malformed\\n' * 90000)
time.sleep(30)
""")
    binary.chmod(0o700)
    runtime = build_danso_runtime(configured)
    session = await runtime.start_or_resume(SessionRequest(working_directory=configured.danso_workspace))
    events = [event async for event in session.send_turn('malformed progress')]
    assert len(events) == 1 and events[0].code == 'danso_adapter_error'
    with pytest.raises(ProcessLookupError):
        os.kill(int((Path(configured.danso_workspace) / 'pid').read_text()), signal.SIGCONT)


@pytest.mark.anyio
async def test_new_task_stage_one_checkpoint_does_not_arm_pause(configured):
    configured.danso_long_task_enabled = True
    configured.danso_long_task_timeout_seconds = 3
    configured.process_timeout_seconds = 13
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, signal, sys
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-status','--task-stage-requests',
                    '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                    '--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
if '--task-progress' in args:
    print('DANSO_TASK=' + json.dumps({'version':1,'state':'checkpoint','stage':1,
          'requests':1,'reported_tokens':1,'elapsed_seconds':1}),
          file=sys.stderr, flush=True)
signal.pause()
""")
    binary.chmod(0o700)
    runtime = build_danso_runtime(configured)
    session = await runtime.start_or_resume(SessionRequest(working_directory=configured.danso_workspace))
    stream = session.send_turn('stage one before handshake')
    first = await anext(stream)
    assert first.kind == 'task_progress' and first.stage == 1
    assert not session.request_task_pause()
    await session.interrupt()
    rest = [event async for event in stream]
    assert rest and rest[-1].code == 'danso_cancelled'


@pytest.mark.anyio
async def test_status_reader_cap_reaps_child_before_resume_dispatch(configured, tmp_path):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-status','--task-stage-requests',
                    '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                    '--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
session = Path(args[args.index('--session') + 1])
if '--task-status' in args:
    (session.parent / 'status-pid').write_text(str(os.getpid()))
    os.write(1, b'x' * (64 * 1024 + 1))
    time.sleep(30)
    raise SystemExit(0)
root = Path(args[args.index('--cwd') + 1])
if '--resume-task' in args:
    (root / 'provider-dispatch').write_text('unexpected')
fd = open(session, 'a')
fd.write('fixture turn\\n')
fd.close()
usage = {'requests': 1, 'inputTokens': 1, 'outputTokens': 0,
         'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'totalTokens': 1}
for prefix in ('DANSO_USAGE', 'PIRI_USAGE'):
    print(prefix + '=' + json.dumps(usage), file=sys.stderr)
print('completed')
""")
    binary.chmod(0o700)
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / 'absent-status-cap',
        environ={
            'TELEGRAM_BOT_TOKEN': '123456:synthetic', 'ALLOWED_USER_IDS': '[7]',
            'CCC_AGENT_PROVIDER': 'danso', 'CCC_DANSO_CLI_PATH': configured.danso_cli_path,
            'CCC_DANSO_WORKSPACE': configured.danso_workspace,
            'CCC_DANSO_STATE_DIR': str(tmp_path / 'status-cap'),
            'OPENAI_API_KEY': 'fixture-key', 'CCC_DANSO_LONG_TASK_ENABLED': 'true',
            'CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS': '3', 'CLAUDE_PROCESS_TIMEOUT': '30',
            'CCC_DELEGATED_TASK_STALL_SECONDS': '10',
        },
    )
    runtime = build_danso_runtime(settings)
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    assert [event async for event in session.send_turn('first')][-1].kind == 'completion'
    session.authorize_task_resume()
    events = [event async for event in session.send_turn(TASK_RESUME_CONTROL)]
    assert len(events) == 1 and events[0].code == 'danso_task_resume_unavailable'
    with pytest.raises(ProcessLookupError):
        os.kill(int((runtime.root / 'status-pid').read_text()), signal.SIGCONT)


@pytest.mark.anyio
async def test_successful_status_probe_reaps_inherited_process_group(configured, tmp_path):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-status','--task-stage-requests',
                    '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                    '--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
session = Path(args[args.index('--session') + 1])
if '--task-status' in args:
    child = os.fork()
    if child == 0:
        os.close(1)
        os.close(2)
        time.sleep(30)
        os._exit(0)
    (session.parent / 'status-child-pid').write_text(str(child))
    status = {'version': 1, 'kind': 'long_task_status', 'state': 'paused',
              'session_id': session.stem, 'stage': 1, 'elapsed_ms': 0,
              'limits': {'wall_seconds': 3, 'stage_requests': 1,
                         'max_requests': 5, 'max_tokens': 500, 'repeat_limit': 2},
              'usage': {'requests': 1, 'reported_tokens': 1}, 'pending': None,
              'resume_allowed': True}
    print(json.dumps(status), flush=True)
    raise SystemExit(0)
root = Path(args[args.index('--cwd') + 1])
fd = open(session, 'a')
fd.write('fixture turn\\n')
fd.close()
usage = {'requests': 1, 'inputTokens': 1, 'outputTokens': 0,
         'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'totalTokens': 1}
for prefix in ('DANSO_USAGE','PIRI_USAGE'):
    print(prefix + '=' + json.dumps(usage), file=sys.stderr)
print('completed')
""")
    binary.chmod(0o700)
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / 'absent-status-group',
        environ={
            'TELEGRAM_BOT_TOKEN': '123456:synthetic', 'ALLOWED_USER_IDS': '[7]',
            'CCC_AGENT_PROVIDER': 'danso', 'CCC_DANSO_CLI_PATH': configured.danso_cli_path,
            'CCC_DANSO_WORKSPACE': configured.danso_workspace,
            'CCC_DANSO_STATE_DIR': str(tmp_path / 'status-group'),
            'OPENAI_API_KEY': 'fixture-key', 'CCC_DANSO_LONG_TASK_ENABLED': 'true',
            'CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS': '3', 'CLAUDE_PROCESS_TIMEOUT': '30',
            'CCC_DELEGATED_TASK_STALL_SECONDS': '10',
        },
    )
    runtime = build_danso_runtime(settings)
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    assert [event async for event in session.send_turn('first')][-1].kind == 'completion'
    session.authorize_task_resume()
    events = [event async for event in session.send_turn(TASK_RESUME_CONTROL)]
    assert events[-1].kind == 'completion'
    with pytest.raises(ProcessLookupError):
        os.kill(int((runtime.root / 'status-child-pid').read_text()), signal.SIGCONT)


@pytest.mark.anyio
async def test_stop_during_status_probe_reaps_child_and_never_dispatches(configured, tmp_path):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-status','--task-stage-requests',
                    '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                    '--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
session = Path(args[args.index('--session') + 1])
if '--task-status' in args:
    (session.parent / 'status-pid').write_text(str(os.getpid()))
    time.sleep(30)
    raise SystemExit(0)
root = Path(args[args.index('--cwd') + 1])
if '--resume-task' in args:
    (root / 'provider-dispatch').write_text('unexpected')
fd = open(session, 'a')
fd.write('fixture turn\\n')
fd.close()
usage = {'requests': 1, 'inputTokens': 1, 'outputTokens': 0,
         'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'totalTokens': 1}
for prefix in ('DANSO_USAGE', 'PIRI_USAGE'):
    print(prefix + '=' + json.dumps(usage), file=sys.stderr)
print('completed')
""")
    binary.chmod(0o700)
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / 'absent-status-stop',
        environ={
            'TELEGRAM_BOT_TOKEN': '123456:synthetic', 'ALLOWED_USER_IDS': '[7]',
            'CCC_AGENT_PROVIDER': 'danso', 'CCC_DANSO_CLI_PATH': configured.danso_cli_path,
            'CCC_DANSO_WORKSPACE': configured.danso_workspace,
            'CCC_DANSO_STATE_DIR': str(tmp_path / 'status-stop'),
            'OPENAI_API_KEY': 'fixture-key', 'CCC_DANSO_LONG_TASK_ENABLED': 'true',
            'CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS': '3', 'CLAUDE_PROCESS_TIMEOUT': '30',
            'CCC_DELEGATED_TASK_STALL_SECONDS': '10',
        },
    )
    runtime = build_danso_runtime(settings)
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    assert [event async for event in session.send_turn('first')][-1].kind == 'completion'
    session.authorize_task_resume()
    async def collect():
        return [event async for event in session.send_turn(TASK_RESUME_CONTROL)]
    task = asyncio.create_task(collect())
    marker = runtime.root / 'status-pid'
    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(.01)
    assert marker.exists()
    await session.interrupt()
    events = await asyncio.wait_for(task, timeout=2)
    assert len(events) == 1 and events[0].code == 'danso_cancelled'
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), signal.SIGCONT)
    assert not (Path(settings.danso_workspace) / 'provider-dispatch').exists()


@pytest.mark.anyio
async def test_reader_wait_set_excludes_closed_stdout(configured, monkeypatch):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, os, sys, time
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-stage-requests',
                    '--task-status',
                    '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                    '--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
print('completed', flush=True)
os.close(1)
time.sleep(.2)
usage = {'requests': 1, 'inputTokens': 1, 'outputTokens': 0,
         'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'totalTokens': 1}
for prefix in ('DANSO_USAGE', 'PIRI_USAGE'):
    print(prefix + '=' + json.dumps(usage), file=sys.stderr, flush=True)
""")
    binary.chmod(0o700)
    from telegram_bot.core import danso_worker

    real_wait = danso_worker.asyncio.wait
    observed = []

    async def checked_wait(tasks, *args, **kwargs):
        assert all(not task.done() for task in tasks)
        observed.append(len(tasks))
        return await real_wait(tasks, *args, **kwargs)

    monkeypatch.setattr(danso_worker.asyncio, "wait", checked_wait)
    runtime = build_danso_runtime(configured)
    session = await runtime.start_or_resume(SessionRequest(working_directory=configured.danso_workspace))
    events = [event async for event in session.send_turn("closed stdout")]
    assert events[-1].kind == "completion"
    assert observed


@pytest.mark.anyio
async def test_progress_survives_consumer_pause(configured, tmp_path):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, sys, time
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-stage-requests',
                    '--task-status',
                    '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                    '--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
if '--task-progress' in args:
    for stage in (1, 2):
        print('DANSO_TASK=' + json.dumps({'version': 1, 'state': 'checkpoint',
              'stage': stage, 'requests': stage, 'reported_tokens': stage,
              'elapsed_seconds': stage}), file=sys.stderr, flush=True)
    time.sleep(1)
print('completed')
usage = {'requests': 1, 'inputTokens': 1, 'outputTokens': 0,
         'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'totalTokens': 1}
for prefix in ('DANSO_USAGE', 'PIRI_USAGE'):
    print(prefix + '=' + json.dumps(usage), file=sys.stderr)
""")
    binary.chmod(0o700)
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-progress-pause",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:synthetic",
            "ALLOWED_USER_IDS": "[7]",
            "CCC_AGENT_PROVIDER": "danso",
            "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
            "CCC_DANSO_WORKSPACE": configured.danso_workspace,
            "CCC_DANSO_STATE_DIR": str(tmp_path / "progress-private"),
            "OPENAI_API_KEY": "fixture-key",
            "CCC_DANSO_LONG_TASK_ENABLED": "true",
            "CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS": "3",
            "CLAUDE_PROCESS_TIMEOUT": "13",
            "CCC_DELEGATED_TASK_STALL_SECONDS": "10",
        },
    )
    runtime = build_danso_runtime(settings)
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    stream = session.send_turn("progress pause")
    first = await anext(stream)
    assert first.kind == "task_progress" and first.stage == 1
    await asyncio.sleep(.05)
    second = await asyncio.wait_for(anext(stream), timeout=.5)
    assert second.kind == "task_progress" and second.stage == 2
    rest = [event async for event in stream]
    assert rest[-1].kind == "completion"


@pytest.mark.anyio
async def test_task_pause_signals_native_parent_after_ready_checkpoint(configured, tmp_path):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, signal, sys
args = sys.argv[1:]
if '--help' in args:
    print(' '.join(['--long-task','--resume-task','--task-stage-requests',
                    '--task-status',
                    '--task-max-requests','--task-max-tokens','--task-repeat-limit',
                    '--task-pause-after-stage','--task-progress']))
    raise SystemExit(0)
if '--task-progress' in args:
    print('DANSO_TASK=' + json.dumps({'version': 1, 'state': 'checkpoint',
          'stage': 0, 'requests': 0, 'reported_tokens': 0,
          'elapsed_seconds': 0}), file=sys.stderr, flush=True)
signal.pause()
""")
    binary.chmod(0o700)
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-task-pause",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:synthetic",
            "ALLOWED_USER_IDS": "[7]",
            "CCC_AGENT_PROVIDER": "danso",
            "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
            "CCC_DANSO_WORKSPACE": configured.danso_workspace,
            "CCC_DANSO_STATE_DIR": str(tmp_path / "task-pause-private"),
            "OPENAI_API_KEY": "fixture-key",
            "CCC_DANSO_LONG_TASK_ENABLED": "true",
            "CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS": "3",
            "CLAUDE_PROCESS_TIMEOUT": "13",
            "CCC_DELEGATED_TASK_STALL_SECONDS": "10",
        },
    )
    runtime = build_danso_runtime(settings)
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    stream = session.send_turn("pause me")
    first = await anext(stream)
    assert first.kind == "task_progress" and first.stage == 0
    assert session.request_task_pause()
    async def collect():
        return [event async for event in stream]

    events = await asyncio.wait_for(collect(), timeout=2)
    assert events and events[-1].kind == "error"


@pytest.mark.anyio
async def test_normal_mode_keeps_task_records_as_ordinary_stderr(configured):
    binary = Path(configured.danso_cli_path)
    binary.write_text("""#!/usr/bin/python3
import json, sys
print('completed')
print('DANSO_TASK={\\"version\\":1,\\"state\\":\\"checkpoint\\",\\"stage\\":1,\\"requests\\":1,\\"reported_tokens\\":1,\\"elapsed_seconds\\":1}', file=sys.stderr)
usage = {'requests': 1, 'inputTokens': 1, 'outputTokens': 0,
         'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'totalTokens': 1}
for prefix in ('DANSO_USAGE', 'PIRI_USAGE'):
    print(prefix + '=' + json.dumps(usage), file=sys.stderr)
""")
    binary.chmod(0o700)
    runtime = build_danso_runtime(configured)
    session = await runtime.start_or_resume(SessionRequest(working_directory=configured.danso_workspace))
    events = [event async for event in session.send_turn("ordinary")]
    assert [event.kind for event in events] == [
        "text_delta", "message_completed", "result", "completion"
    ]


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
    ('bridge_memory_mode','curated'),
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
    assert probe_danso_readiness(other)[0]
    assert not probe_danso_readiness(other.model_copy(update={"danso_sandbox":"bubblewrap"}))[0]


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


@pytest.mark.anyio
async def test_bridge_ledgers_stay_outside_danso_task_workspace(configured):
    from telegram_bot.__main__ import build_context
    context = build_context(configured)
    context.session_manager.initialize()
    handler = context.project_chat
    assert handler.project_root == Path(configured.project_root)
    private = Path(configured.bot_data_dir)
    assert handler._usage_meter._path.is_relative_to(private)
    assert handler._cost_ledger._path.is_relative_to(private)
    assert handler._async_completion_journal.root.is_relative_to(private)
    response = await handler.process_message("ok", 7, 9)
    assert response.success
    workspace = Path(configured.danso_workspace)
    args = json.loads((workspace / "argv.json").read_text())
    assert args[args.index("--cwd") + 1] == str(workspace)
    assert not (workspace / ".telegram_bot").exists()
    assert handler._usage_meter._path.exists()
    await handler.close()

@pytest.mark.anyio
async def test_explicit_bubblewrap_is_forwarded(configured):
    runtime = build_danso_runtime(configured.model_copy(update={"danso_sandbox":"bubblewrap"}))
    session = await runtime.start_or_resume(SessionRequest(working_directory=configured.danso_workspace))
    events = [event async for event in session.send_turn("hello")]
    assert events
    argv = json.loads((Path(configured.danso_workspace)/"argv.json").read_text())
    assert argv[argv.index("--sandbox")+1] == "bubblewrap"


def test_invalid_sandbox_rejected_before_state_creation(configured):
    ok, _ = probe_danso_readiness(configured.model_copy(update={"danso_sandbox":"invalid"}))
    assert not ok
    assert not Path(configured.danso_state_dir).exists()


@pytest.fixture
def chatgpt_configured(configured, tmp_path):
    auth_home = tmp_path / "isolated-auth"
    auth_home.mkdir(mode=0o700)
    auth_file = auth_home / "danso-auth.json"
    # Readiness inspects metadata only; the fake CLI never reads these bytes.
    auth_file.write_text("PRIVATE_AUTH_FILE_CONTENT")
    auth_file.chmod(0o600)
    configured.danso_auth_mode = "chatgpt"
    configured.danso_chatgpt_auth_file = str(auth_file)
    return configured


@pytest.mark.anyio
async def test_chatgpt_mode_selects_subscription_without_platform_key(chatgpt_configured):
    settings = chatgpt_configured
    settings.openai_api_key = None
    settings.danso_chatgpt_base_url = "http://127.0.0.1:43210/codex"
    assert probe_danso_readiness(settings) == (True, "")
    runtime = build_danso_runtime(settings)
    assert runtime.provider == "openai-codex"
    assert runtime.root.name == "chatgpt-journals"
    assert set(runtime.environment) == {"PATH", "HOME", "DANSO_CHATGPT_AUTH_FILE", "DANSO_CHATGPT_BASE_URL"}
    session = await runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    events = [e async for e in session.send_turn("ok")]
    assert events[-1].kind == "completion"
    argv = json.loads((Path(settings.danso_workspace) / "argv.json").read_text())
    assert argv[argv.index("--provider") + 1] == "openai-codex"
    assert argv[argv.index("--model") + 1] == "gpt-6-astra"
    assert argv[argv.index("--reasoning-effort") + 1] == "medium"
    assert "PRIVATE_AUTH_FILE_CONTENT" not in repr(events)


def test_chatgpt_mode_does_not_forward_voice_key_or_platform_endpoint(chatgpt_configured):
    settings = chatgpt_configured
    settings.openai_api_key = "PRIVATE_WHISPER_KEY"
    settings.danso_base_url = "https://unrelated.example/v1"
    runtime = build_danso_runtime(settings)
    assert "OPENAI_API_KEY" not in runtime.environment
    assert "DANSO_OPENAI_BASE_URL" not in runtime.environment
    assert "PRIVATE_WHISPER_KEY" not in repr(runtime.environment)


@pytest.mark.parametrize("mutation", ["relative", "missing", "file-mode", "parent-mode", "symlink", "symlink-loop",
                                     "hardlink", "oversize", "workspace", "pending", "codex-reappeared", "endpoint"])
def test_chatgpt_metadata_preflight_fails_without_state_writes(chatgpt_configured, mutation, tmp_path):
    settings = chatgpt_configured
    auth = Path(settings.danso_chatgpt_auth_file)
    if mutation == "relative":
        settings.danso_chatgpt_auth_file = "relative.json"
    elif mutation == "missing":
        settings.danso_chatgpt_auth_file = str(auth.parent / "missing.json")
    elif mutation == "file-mode":
        auth.chmod(0o644)
    elif mutation == "parent-mode":
        auth.parent.chmod(0o755)
    elif mutation == "symlink":
        link = tmp_path / "auth-link"
        link.symlink_to(auth)
        settings.danso_chatgpt_auth_file = str(link)
    elif mutation == "symlink-loop":
        link = tmp_path / "auth-loop"
        link.symlink_to(link)
        settings.danso_chatgpt_auth_file = str(link)
    elif mutation == "hardlink":
        os.link(auth, auth.parent / "copy")
    elif mutation == "oversize":
        auth.write_bytes(b"x" * 65537)
    elif mutation == "workspace":
        settings.danso_workspace = str(auth.parent)
    elif mutation == "pending":
        (auth.parent / ".danso-refresh-pending").symlink_to("/missing")
    elif mutation == "codex-reappeared":
        (auth.parent / "auth.json").write_text("placeholder")
    elif mutation == "endpoint":
        settings.danso_chatgpt_base_url = "https://unrelated.example"
    ready, message = probe_danso_readiness(settings)
    assert not ready and message
    assert "PRIVATE_AUTH_FILE_CONTENT" not in message
    assert not Path(settings.danso_state_dir).exists()


def test_chatgpt_auth_requires_explicit_mode(configured, tmp_path):
    configured.danso_chatgpt_auth_file = str(tmp_path / "auth.json")
    ready, message = probe_danso_readiness(configured)
    assert not ready and "CCC_DANSO_AUTH_MODE=chatgpt" in message


@pytest.mark.anyio
async def test_auth_mode_change_does_not_reuse_or_convert_journal(chatgpt_configured):
    settings = chatgpt_configured
    oauth_runtime = build_danso_runtime(settings)
    session = await oauth_runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace))
    assert [e async for e in session.send_turn("ok")][-1].kind == "completion"
    journal = oauth_runtime.root / (session.session_id + ".jsonl")
    original = journal.read_bytes()
    settings.danso_auth_mode = "api-key"
    settings.danso_chatgpt_auth_file = None
    api_runtime = build_danso_runtime(settings)
    with pytest.raises(ValueError, match="unavailable"):
        await api_runtime.start_or_resume(SessionRequest(working_directory=settings.danso_workspace, session_id=session.session_id))
    assert journal.read_bytes() == original


@pytest.mark.anyio
async def test_danso_snapshot_routes_and_budget_gated_composition(configured):
    from telegram_bot.__main__ import build_context
    from test_danso_distill import journal
    from telegram_bot.memory.distill_types import TranscriptBounds, SnapshotUnavailableError
    from telegram_bot.memory.danso_backend import DansoDistillBackend
    settings = configured.model_copy(update={
        "bridge_memory_mode":"audience-scoped",
        "codex_memory_materializer_path":str(Path(__file__).resolve().parents[2] / "scripts/ccc_codex_memory.py"),
        "memory_distill_provider":"danso", "memory_distill_model":"gpt-5.6-luna",
        "usage_budget_tokens_danso":500000,
    })
    context = build_context(settings)
    worker = context.distill_extraction_worker
    assert isinstance(worker._backend, DansoDistillBackend)
    assert worker._usage_meter is context.project_chat.usage_meter
    assert worker._extractor_provider == "danso"
    assert worker._wiki_enabled is False
    assert context.distill_snapshot_worker._runtime is context.agent_runtime
    root = context.agent_runtime.root
    scope = "private-" + "a" * 32
    ident, _ = journal(root / scope, cwd=Path(settings.danso_workspace))
    snapshot = await context.agent_runtime.read_session_snapshot(ident, bounds=TranscriptBounds(),
        memory_audience="private", memory_scope=scope)
    assert snapshot.byte_count > 0
    with pytest.raises(SnapshotUnavailableError):
        await context.agent_runtime.read_session_snapshot(ident, bounds=TranscriptBounds(),
            memory_audience="shared", memory_scope="shared")
    with pytest.raises(ValueError):
        await context.agent_runtime.read_session_snapshot(ident, bounds=TranscriptBounds(),
            memory_audience="private", memory_scope="../escape")
    assert build_context(settings.model_copy(update={"usage_budget_tokens_danso":0})).distill_extraction_worker is None
    assert build_context(settings.model_copy(update={"memory_distill_provider":"off"})).distill_snapshot_worker is None


def test_danso_extraction_requires_memory_route_and_separates_backlog(configured):
    from telegram_bot.__main__ import build_context
    from telegram_bot.memory.distill_journal import DistillJournal
    from telegram_bot.memory.distill_types import DistillTrigger
    old = DistillJournal(configured.bot_data_dir / "distill-journal")
    old.initialize()
    job = old.enqueue_once(provider="codex", thread_id="old-session", trigger=DistillTrigger.CHECKPOINT)
    before = old.job_path(job.job_id).read_bytes()
    settings = configured.model_copy(update={
        "bridge_memory_mode":"audience-scoped", "memory_distill_provider":"danso",
        "usage_budget_tokens_danso":500000,
        "codex_memory_materializer_path":str(Path(__file__).resolve().parents[2] / "scripts/ccc_codex_memory.py"),
    })
    context = build_context(settings)
    assert context.distill_journal.root == configured.bot_data_dir / "danso-distill-journal"
    assert list(context.distill_journal.root.glob("*.json")) == []
    assert old.job_path(job.job_id).read_bytes() == before
    with pytest.raises(ValueError, match="audience-scoped"):
        build_context(settings.model_copy(update={"bridge_memory_mode":"off"}))
    disabled = build_context(settings.model_copy(update={"bridge_memory_mode":"off", "memory_distill_provider":"auto"}))
    assert disabled.distill_snapshot_worker is None
    assert disabled.distill_extraction_worker is None
    assert old.job_path(job.job_id).read_bytes() == before


def test_zai_auth_mode_requires_key(configured, tmp_path):
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-zai-missing",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:synthetic",
            "ALLOWED_USER_IDS": "[7]",
            "CCC_AGENT_PROVIDER": "danso",
            "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
            "CCC_DANSO_WORKSPACE": configured.danso_workspace,
            "CCC_DANSO_STATE_DIR": str(tmp_path / "private-zai-missing"),
            "CCC_DANSO_AUTH_MODE": "zai",
        },
    )
    ok, detail = probe_danso_readiness(settings)
    assert not ok
    assert "ZAI_API_KEY" in detail


def test_zai_auth_mode_rejects_chatgpt_auth_file(configured, tmp_path):
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-zai-conflict",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:synthetic",
            "ALLOWED_USER_IDS": "[7]",
            "CCC_AGENT_PROVIDER": "danso",
            "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
            "CCC_DANSO_WORKSPACE": configured.danso_workspace,
            "CCC_DANSO_STATE_DIR": str(tmp_path / "private-zai-conflict"),
            "CCC_DANSO_AUTH_MODE": "zai",
            "ZAI_API_KEY": "fixture-zai-key",
            "DANSO_CHATGPT_AUTH_FILE": "/synthetic/danso-auth.json",
        },
    )
    ok, detail = probe_danso_readiness(settings)
    assert not ok
    assert "chatgpt" in detail


@pytest.mark.anyio
async def test_zai_auth_mode_routes_glm_with_mode_default_model_and_env(configured, tmp_path):
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-zai",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:synthetic",
            "ALLOWED_USER_IDS": "[7]",
            "CCC_AGENT_PROVIDER": "danso",
            "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
            "CCC_DANSO_WORKSPACE": configured.danso_workspace,
            "CCC_DANSO_STATE_DIR": str(tmp_path / "private-zai"),
            "CCC_DANSO_AUTH_MODE": "zai",
            "ZAI_API_KEY": "fixture-zai-key",
            "DANSO_GLM_ENDPOINT": "coding",
            "CCC_DANSO_TIMEOUT_SECONDS": "3",
        },
    )
    assert settings.danso_max_turns == 64
    assert settings.danso_max_output_tokens == 16384
    runtime = build_danso_runtime(settings)
    assert runtime.provider == "glm"
    # Issue #70: the zai default model follows the auth mode.
    assert runtime.model == "glm-5.3-flash"
    assert runtime.root.name == "glm-journals"
    assert runtime.environment.get("ZAI_API_KEY") == "fixture-zai-key"
    assert runtime.environment.get("DANSO_GLM_ENDPOINT") == "coding"
    session = await runtime.start_or_resume(
        SessionRequest(working_directory=settings.danso_workspace))
    events = [event async for event in session.send_turn("ok")]
    assert events[-1].kind == "completion"
    argv = json.loads((Path(settings.danso_workspace) / "argv.json").read_text())
    assert argv[argv.index("--provider") + 1] == "glm"
    assert argv[argv.index("--model") + 1] == "glm-5.3-flash"
    assert argv[argv.index("--max-output-tokens") + 1] == "16384"
    child_env = json.loads(
        (Path(settings.danso_workspace) / "environment.json").read_text())
    assert "ZAI_API_KEY" in child_env
    assert "DANSO_GLM_ENDPOINT" in child_env
    assert "OPENAI_API_KEY" not in child_env


@pytest.mark.anyio
async def test_zai_explicit_model_and_output_cap_reach_native_cli(configured, tmp_path):
    settings = Settings.load(
        project_root=configured.project_root,
        bot_env_file=tmp_path / "absent-zai-explicit",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:synthetic",
            "ALLOWED_USER_IDS": "[7]",
            "CCC_AGENT_PROVIDER": "danso",
            "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
            "CCC_DANSO_WORKSPACE": configured.danso_workspace,
            "CCC_DANSO_STATE_DIR": str(tmp_path / "private-zai-explicit"),
            "CCC_DANSO_AUTH_MODE": "zai",
            "ZAI_API_KEY": "fixture-zai-key",
            "CCC_DANSO_MODEL": "glm-5.3",
            "CCC_DANSO_MAX_OUTPUT_TOKENS": "8192",
            "CCC_DANSO_TIMEOUT_SECONDS": "3",
        },
    )
    runtime = build_danso_runtime(settings)
    assert runtime.model == "glm-5.3", "an explicit CCC_DANSO_MODEL wins"
    session = await runtime.start_or_resume(
        SessionRequest(working_directory=settings.danso_workspace))
    events = [event async for event in session.send_turn("ok")]
    assert events[-1].kind == "completion"
    argv = json.loads((Path(settings.danso_workspace) / "argv.json").read_text())
    assert argv[argv.index("--model") + 1] == "glm-5.3"
    assert argv[argv.index("--max-output-tokens") + 1] == "8192"


def test_long_task_defaults_preserve_explicit_opt_out_and_timeout_overrides(configured, tmp_path):
    base = {
        "TELEGRAM_BOT_TOKEN": "123456:synthetic", "ALLOWED_USER_IDS": "[7]",
        "CCC_AGENT_PROVIDER": "danso", "CCC_DANSO_CLI_PATH": configured.danso_cli_path,
        "CCC_DANSO_WORKSPACE": configured.danso_workspace,
        "CCC_DANSO_STATE_DIR": str(tmp_path / "defaults-private"),
        "OPENAI_API_KEY": "fixture-key",
    }
    def load(**overrides):
        return Settings.load(project_root=configured.project_root,
                             bot_env_file=tmp_path / "absent-defaults",
                             environ={**base, **overrides})

    # A pinned legacy outer deadline is not silently rewritten.
    pinned = load(CLAUDE_PROCESS_TIMEOUT="21600")
    assert pinned.process_timeout_seconds == 21600
    ready, reason = probe_danso_readiness(pinned)
    assert not ready and "at least 10s" in reason

    # An ordinary-mode timeout alone does not opt out of default long tasks.
    legacy_ordinary_limit = load(CCC_DANSO_TIMEOUT_SECONDS="300")
    assert legacy_ordinary_limit.danso_timeout_seconds == 300
    assert build_danso_runtime(legacy_ordinary_limit).timeout == 21600

    ordinary = load(CCC_DANSO_LONG_TASK_ENABLED="false")
    runtime = build_danso_runtime(ordinary)
    assert runtime.long_task is False
    assert runtime.timeout == 3600
    explicit = load(CCC_DANSO_LONG_TASK_ENABLED="false", CCC_DANSO_TIMEOUT_SECONDS="300")
    assert build_danso_runtime(explicit).timeout == 300

    old_binary = tmp_path / "old-default-danso"
    old_binary.write_text("#!/bin/sh\nprintf '%s\\n' old-help\n")
    old_binary.chmod(0o700)
    ready, reason = probe_danso_readiness(load(CCC_DANSO_CLI_PATH=str(old_binary)))
    assert not ready and "long-task" in reason
    assert probe_danso_readiness(load(CCC_DANSO_CLI_PATH=str(old_binary),
                                     CCC_DANSO_LONG_TASK_ENABLED="false")) == (True, "")


@pytest.mark.anyio
async def test_silent_native_long_task_retains_status_until_completion(configured, monkeypatch):
    configured.danso_long_task_enabled = True
    configured.danso_long_task_timeout_seconds = 5
    binary = Path(configured.danso_cli_path)
    binary.write_text(binary.read_text().replace(
        "if message == 'slow':time.sleep(.1)",
        "if message == 'slow':\n while not (root/'release').exists():time.sleep(.005)",
    ))
    for name, value in {
        'heartbeat_enabled': True,
        'heartbeat_threshold_seconds': .005,
        'heartbeat_update_interval_seconds': .005,
        'heartbeat_stall_seconds': .02,
    }.items():
        setattr(configured, name, value)
    # Full-suite collection can replace the module registry with a sibling
    # test stub. Patch the globals used by this imported handler, not a newly
    # imported module object that may be a different instance.
    monkeypatch.setitem(ProjectChatHandler._maybe_update_heartbeat.__globals__,
                        "config", configured)
    runtime = build_danso_runtime(configured)
    handler = ProjectChatHandler(settings=configured, agent_runtime=runtime)
    handler._typing_interval_seconds = .005
    calls = []

    async def status(text, message_id=None):
        calls.append((text, message_id))
        if text is not None and 'Waiting for progress' in text and 'stage 1' in text:
            (Path(configured.danso_workspace) / 'release').touch()
        return None if text is None else 1234

    try:
        response = await asyncio.wait_for(
            handler.process_message('slow', 7, 9, status_callback=status), timeout=10,
        )
        assert response.success, response.error
        assert any(text is not None and 'Waiting for progress' in text
                   and 'stage 1' in text for text, _ in calls)
        assert all(text is not None for text, _ in calls[:-1])
        assert calls[-1] == (None, 1234)
    finally:
        await handler.close()
