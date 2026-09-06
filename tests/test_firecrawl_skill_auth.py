"""Offline regression tests: no real credentials, network, or account credits."""
import importlib.util
import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "piri/skills/web"


@pytest.fixture
def helpers(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.syspath_prepend(str(ROOT))
    modules = {}
    for name in ("web_search", "web_fetch", "web_developer"):
        spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return modules


def stored(tmp_path, text):
    folder = tmp_path / ".hermes"
    folder.mkdir(exist_ok=True)
    (folder / ".env").write_text(text)


@pytest.mark.parametrize("name", ["web_search", "web_fetch", "web_developer"])
@pytest.mark.parametrize("source", ["environment", "stored", "blank_env", "missing"])
def test_all_requests_share_key_resolution(helpers, monkeypatch, tmp_path, name, source):
    if source != "missing":
        stored(tmp_path, '# ignored\nOTHER=x\nFIRECRAWL_API_KEY="stored-test-only"\n')
    if source == "environment":
        monkeypatch.setenv("FIRECRAWL_API_KEY", " env-test-only ")
    elif source == "blank_env":
        monkeypatch.setenv("FIRECRAWL_API_KEY", "  ")
    expected = "env-test-only" if source == "environment" else (
        None if source == "missing" else "stored-test-only"
    )
    requests = []

    def respond(req, **kwargs):
        requests.append(req)
        return io.BytesIO(json.dumps({"success": True, "data": {}, "results": []}).encode())

    module = helpers[name]
    monkeypatch.setattr(module.urllib.request, "urlopen", respond)
    if name == "web_search":
        module._search_firecrawl("synthetic", 1)
    elif name == "web_fetch":
        module._request({"url": "https://example.com"})
    else:
        module._post({"query": "synthetic", "k": 1})
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == (
        f"Bearer {expected}" if expected else None
    )


@pytest.mark.parametrize("name", ["web_search", "web_fetch", "web_developer"])
@pytest.mark.parametrize("keyed", [False, True])
def test_http_error_is_diagnostic_but_never_leaks(helpers, monkeypatch, capsys, name, keyed):
    secret = "fake-test-only-do-not-log"
    if keyed:
        monkeypatch.setenv("FIRECRAWL_API_KEY", secret)

    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            f"https://example.com/{secret}", 429, secret, {}, io.BytesIO(secret.encode())
        )

    module = helpers[name]
    monkeypatch.setattr(module.urllib.request, "urlopen", fail)
    if name == "web_search":
        assert module._search_firecrawl("synthetic", 1) == 69
    elif name == "web_fetch":
        assert module._request({}) is None
    else:
        assert module._post({}) is None
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert f"HTTP 429; auth={'keyed' if keyed else 'keyless'}" in output.err


@pytest.mark.parametrize("contents", [b"\xff", b"# comment\n", b"FIRECRAWL_API_KEY=\n"])
def test_unusable_stored_key_is_keyless(helpers, tmp_path, contents):
    folder = tmp_path / ".hermes"
    folder.mkdir()
    (folder / ".env").write_bytes(contents)
    assert helpers["web_search"]._firecrawl_key() == ""


def test_package_import_without_script_path_injection():
    result = subprocess.run(
        [sys.executable, "-c", "from piri.skills.web import web_fetch, web_developer"],
        cwd=ROOT.parents[2], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name,code", [("web_search", 64), ("web_fetch", 64), ("web_developer", 2)])
def test_installed_scripts_start_without_pythonpath(tmp_path, monkeypatch, name, code):
    for script in ROOT.glob("*.py"):
        (tmp_path / script.name).write_bytes(script.read_bytes())
    monkeypatch.delenv("PYTHONPATH", raising=False)
    result = subprocess.run(
        [sys.executable, str(tmp_path / f"{name}.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == code, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_network_error_does_not_print_exception_details(helpers):
    error = urllib.error.URLError("fake-credential-in-url")
    assert helpers["web_search"]._firecrawl_error(error, "fake-key") == "URLError; auth=keyed"
