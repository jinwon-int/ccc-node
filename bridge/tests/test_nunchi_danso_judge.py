"""Cron auth binding uses explicit local protected files and no unrelated keys."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from telegram_bot.memory import nunchi_judge


def test_settings_use_ordered_owned_references_not_copied_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / ".config/ccc-node"
    directory.mkdir(parents=True, mode=0o700)
    auth = tmp_path / "existing.env"
    auth.write_text("ZAI_API_KEY=synthetic-fixture-only\nCCC_DANSO_AUTH_MODE=zai\nTELEGRAM_BOT_TOKEN=excluded\n")
    auth.chmod(0o600)
    pin = tmp_path / "pin.env"
    pin.write_text("CCC_DANSO_CLI_PATH=/fixture/pinned-danso\nDANSO_GLM_ENDPOINT=coding\n")
    pin.chmod(0o600)
    manifest = directory / "nunchi-judge.json"
    manifest.write_text(json.dumps(dict(environment_files=[str(auth), str(pin)])))
    manifest.chmod(0o600)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    captured = {}
    monkeypatch.setattr(nunchi_judge.Config, "load", lambda **kwargs: captured.update(kwargs))
    nunchi_judge.load_settings()
    env = captured["environ"]
    assert env["ZAI_API_KEY"] == "synthetic-fixture-only"
    assert env["CCC_DANSO_CLI_PATH"] == "/fixture/pinned-danso"
    assert env["DANSO_GLM_ENDPOINT"] == "coding"
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "synthetic" not in manifest.read_text()
    auth.chmod(0o644)
    with pytest.raises(Exception, match="unsafe"):
        nunchi_judge.load_settings()


def test_judge_uses_same_tool_free_isolated_native_boundary(tmp_path):
    script = tmp_path / "danso"
    script.write_text('''#!/usr/bin/python3
import os, pathlib, sys
a=sys.argv
assert '--no-tools' in a and a[a.index('--max-turns')+1]=='1'
assert not list(pathlib.Path.cwd().iterdir())
assert os.environ['ZAI_API_KEY']=='synthetic-fixture-only'
p=pathlib.Path(a[a.index('--system-context-file')+1])
assert p.stat().st_mode & 0o777 == 0o600
assert 'fixture judge input' in p.read_text()
print('{"verdict":"human","rationale":"synthetic","supersede_proposal":null}')
''')
    script.chmod(0o700)
    settings = SimpleNamespace(danso_cli_path=str(script), danso_model="fixture-model",
                               danso_auth_mode="zai", zai_api_key="synthetic-fixture-only",
                               danso_glm_base_url=None, danso_glm_endpoint="coding")
    result = asyncio.run(nunchi_judge.judge(b"fixture judge input", settings, timeout=5))
    assert json.loads(result)["verdict"] == "human"
    with pytest.raises(ValueError):
        asyncio.run(nunchi_judge.judge(b"x" * 32769, settings))
